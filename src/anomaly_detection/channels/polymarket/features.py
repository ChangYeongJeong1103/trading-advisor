"""
polymarket/features.py — Rolling feature engineering (per symbol). v2 (P9.1).

────────────────────────────────────────────────────────────────────────
Role (architecture §5 pipeline step 3 / docs/p9-detection-design.md):
    Consumes a NormalizedEvent stream and produces per-symbol rolling
    statistics that feed the detector.

    v1 (P2 baseline) features:
        - vol_zscore_5min       : z-score over the prior 30 min (6 chunks, in-memory)
        - price_jump_1min       : (max - min) of last_trade_price over the last 1 min
        - current_vol_usd       : USD total over the last 5 min (debug)
        - baseline_mean_usd     : mean of the baseline chunks above (debug)
        - baseline_std_usd      : stdev of the baseline chunks above (debug)
        - n_trades_5min         : trade count over the last 5 min (debug)
        - baseline_ready        : 1.0 if the baseline is warmed up, else 0.0

    v2 (P9.1) ADD — directional + robust + time-of-day:
        # M2 mid-price (more stable than last trade price)
        - mid_price_jump_1min   : (max - min) of mid_price over 1 min (falls back to price)
        - has_mid_price         : 1.0 if a mid price is available, else 0.0

        # M4 + directional features
        - buy_count_5min        : buy-side (YES/BUY/LONG) trade count, last 5 min
        - sell_count_5min       : sell-side (NO/SELL/SHORT) trade count, last 5 min
        - buy_vol_usd_5min      : buy-side USD volume, last 5 min
        - sell_vol_usd_5min     : sell-side USD volume, last 5 min
        - imbalance_5min        : (buy_vol - sell_vol) / (buy_vol + sell_vol), in [-1, 1].
                                  0 when there are no trades. Primary input to
                                  detector_v2 directional_v1.
        - same_side_run_length  : Number of consecutive same-side trades counting
                                  back from the most recent. Signals strong directional
                                  conviction.

        # CUSUM (online change detection)
        - cusum_pos             : Cumulative upward deviation (price drifting above target)
        - cusum_neg             : Cumulative downward deviation

        # M1 time-of-day baseline (SQLite-backed)
        - vol_zscore_tod_v1     : (current_vol - tod_median) / max(tod_mad, ...).
                                  0 when baseline_store is None or has too few samples.
        - tod_baseline_n        : sample count matching this time-of-day bucket
        - tod_baseline_median   : (debug)
        - tod_baseline_mad      : (debug)

    P10.3 wallet concentration features (insider suspicion):
        - unique_wallets_5min          : distinct wallets in the last 5 min
        - top_wallet_share_5min        : USD share of the #1 wallet, in [0, 1]
        - top3_wallet_share_5min       : combined share of the top 3 wallets, in [0, 1]
        - wallet_hhi_5min              : Herfindahl–Hirschman Index, Σ(s_i)^2
        - directional_wallets_5min     : wallets betting on the dominant side
        - directional_wallet_ratio_5min: directional / unique (catches 38-wallet splits)
        - wallet_concentration_score   : composite [0, 1] (HHI / dir_ratio / few-wallet
                                         bonus). Higher → more likely insider; lower →
                                         more likely retail panic.

────────────────────────────────────────────────────────────────────────
Design decisions (v2):

    · CUSUM is incremental — updated on every sample inside `add_events()`.
      We do NOT recompute it on every compute_snapshot() cycle. Implications:
          - CUSUM evolves the moment a new trade arrives.
          - target = EMA of price (a slowly-tracking reference).
          - k = drift slack (default 0.005 = 0.5%).
          - The detector decides the alert threshold.
      → features.py only exposes the raw cusum_pos / cusum_neg.

    · add_events sorts each new batch by ts ASC before processing — Polymarket's
      REST API returns trades newest-first, but run_length and CUSUM need
      chronological order to be correct.

    · Bucket recording (M1 baseline_store update):
      On every compute_snapshot we record all 5-min buckets up to and including
      "the most recently completed 5-min boundary as of now". We catch up every
      bucket newer than last_recorded_bucket_end in one go, but only when buf has
      enough history (otherwise skip — typical right after a daemon restart).

    · v1 and v2 features all live in the same FeatureSnapshot.features dict.
      Detector v2 consumes the new keys; v1 detector still works (keys are
      additive, never removed).

────────────────────────────────────────────────────────────────────────
Architecture: §5 Pipeline step 3, §4.1 FeatureSnapshot
Plan: docs/p9-detection-design.md M1 / M2 / M4 + CUSUM family
"""

from __future__ import annotations

# Standard library only.
import logging
import statistics
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque

from ...core.schemas import CHANNEL_POLYMARKET, FeatureSnapshot, NormalizedEvent, Side
from ...storage.polymarket_baseline_store import (
    PolymarketBaselineStore,
    align_to_bucket_end,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Constants — magic numbers live in one place. Keep docstring + design
# doc in sync when these change.
# =====================================================================
_BUCKET_WIDTH_S: int = 300                  # 5-min bucket used by the M1 baseline
_DEFAULT_CUSUM_K: float = 0.005             # 0.5% drift slack (price is in [0, 1])
_DEFAULT_CUSUM_EMA_ALPHA: float = 0.05      # target EMA smoothing — slow on purpose
_RUN_LENGTH_SCAN_MAX: int = 200             # cap on how many samples to scan for run_length


# =====================================================================
# Per-sample buffer entry — v2 adds side / mid_price
# =====================================================================
@dataclass(frozen=True, slots=True)
class _Sample:
    """Minimal info for a single trade kept in the rolling buffer (v2 + wallet)."""
    ts: datetime              # UTC, ts_source
    size_usd: float           # USD-converted trade size
    price: float              # last_trade_price (NormalizedEvent.price)
    side: Side | None         # for buy/sell classification (None = not a sided trade)
    mid_price: float | None   # (best_bid + best_ask) / 2; None if unavailable
    # P10.3 wallet concentration feature input.
    # production: NormalizedEvent.meta["proxy_wallet"] (Polymarket trader address).
    # Empty string if missing — bucketed as "unknown" during distribution math.
    wallet: str               # proxy wallet address (lowercase); "" = unknown


# =====================================================================
# Per-symbol state — v2 adds CUSUM and bucket-tracking fields
# =====================================================================
@dataclass
class _SymbolState:
    """Rolling buffer plus incremental state for a single symbol."""
    buf: Deque[_Sample] = field(default_factory=deque)
    last_prune_ts: datetime | None = None

    # ── CUSUM state (incremental; updated inside add_events) ──
    cusum_pos: float = 0.0
    cusum_neg: float = 0.0
    # target = EMA of price. None means "initialise from the next sample".
    cusum_target: float | None = None

    # ── M1 baseline-store recording cursor ──
    # The last bucket_end we called record_bucket() with. Newer completed
    # buckets are appended on subsequent calls.
    last_recorded_bucket_end: datetime | None = None


# =====================================================================
# Helper — buy/sell classification (handles the Side enum variants)
# =====================================================================
def _classify_side(side: Side | None) -> str | None:
    """Normalise a Side enum into "buy" | "sell" | None.

    Polymarket: YES → buy ("yes outcome"), NO → sell.
    Hyperliquid etc.: BUY/LONG → buy, SELL/SHORT → sell.
    None or an unknown value → None (excluded from aggregates).
    """
    if side is None:
        return None
    if side in (Side.BUY, Side.YES, Side.LONG):
        return "buy"
    if side in (Side.SELL, Side.NO, Side.SHORT):
        return "sell"
    return None


def _floor_to_bucket_boundary(ts: datetime) -> datetime:
    """Floor ts to the nearest 5-min boundary. 14:23:45 → 14:20:00, 14:25:00 → 14:25:00."""
    floored_min = (ts.minute // 5) * 5
    return ts.replace(minute=floored_min, second=0, microsecond=0)


# =====================================================================
# PolymarketFeatures v2 — main class
# =====================================================================
class PolymarketFeatures:
    """Polymarket per-symbol rolling feature engine (v2).

    Usage:
        store = PolymarketBaselineStore(Path("data/poly_baseline.db"))  # optional
        features = PolymarketFeatures(baseline_store=store)
        features.add_events([normalized1, normalized2, ...])
        snapshot = features.compute_snapshot(symbol="iran-strike", now=...)
        if snapshot is not None:
            detector.evaluate(snapshot)
    """

    def __init__(
        self,
        *,
        # ── existing v1 parameters (kept for compatibility) ──
        current_window_s: int = 300,        # 5 min — "current" volume window
        baseline_chunk_count: int = 6,      # 6 × 5 min = 30 min in-memory baseline
        price_jump_window_s: int = 60,      # 1 min — price max-min window
        max_history_s: int | None = None,
        # ── v2 (P9.1) additions ──
        baseline_store: PolymarketBaselineStore | None = None,
        cusum_k: float = _DEFAULT_CUSUM_K,
        cusum_ema_alpha: float = _DEFAULT_CUSUM_EMA_ALPHA,
        cusum_reset_threshold: float = 0.10,  # restart CUSUM once it grows past this
        tod_min_samples: int = 5,             # min samples to trust the tod baseline
    ) -> None:
        """
        Args:
            current_window_s: Width of the "last X seconds" window. Default 5 min.
            baseline_chunk_count: Number of in-memory baseline chunks. Default 6 → 30 min.
            price_jump_window_s: Window for the price/mid jump calc. Default 1 min.
            max_history_s: Max deque history. None means "auto compute".
            baseline_store: Time-of-day SQLite store. None disables all tod_* features (set to 0).
            cusum_k: CUSUM drift slack (0.005 = 0.5%). Price space is [0, 1].
            cusum_ema_alpha: EMA smoothing for the target. Smaller = target moves slower.
                              When CUSUM exceeds cusum_reset_threshold we reset to 0 so
                              we can re-fire after the detector has already classified.
            cusum_reset_threshold: Reset CUSUM to 0 once it exceeds this.
            tod_min_samples: Below this many samples, vol_zscore_tod_v1 is forced to 0.
        """
        if current_window_s < 30:
            raise ValueError("current_window_s must be >= 30")
        if baseline_chunk_count < 2:
            raise ValueError("baseline_chunk_count must be >= 2")
        if not (0.0 < cusum_ema_alpha < 1.0):
            raise ValueError("cusum_ema_alpha must be in (0, 1)")
        if cusum_k < 0:
            raise ValueError("cusum_k must be >= 0")
        if cusum_reset_threshold <= 0:
            raise ValueError("cusum_reset_threshold must be > 0")

        self._current_window_s = current_window_s
        self._baseline_chunk_count = baseline_chunk_count
        self._price_jump_window_s = price_jump_window_s

        if max_history_s is None:
            max_history_s = (
                current_window_s * (baseline_chunk_count + 1)
                + 60
            )
        self._max_history_s = max_history_s

        # v2 new fields
        self._baseline_store = baseline_store
        self._cusum_k = cusum_k
        self._cusum_alpha = cusum_ema_alpha
        self._cusum_reset = cusum_reset_threshold
        self._tod_min_samples = tod_min_samples

        self._states: dict[str, _SymbolState] = {}

        # baseline_ref — tagged v2
        self._baseline_ref = (
            f"polymarket:rolling:cur{current_window_s}s:base{baseline_chunk_count}x"
            f"{current_window_s}s:tod{'on' if baseline_store else 'off'}:v2"
        )

    # ─────────────────────────────────────────────────────────────────
    # Ingest — sort + buffer push + CUSUM update
    # ─────────────────────────────────────────────────────────────────
    def add_events(self, events: Iterable[NormalizedEvent]) -> None:
        """Push NormalizedEvents into the per-symbol buffer (sorted ts ASC first).

        - Events from non-Polymarket channels are silently skipped (defensive).
        - Events with price=None are skipped (not a sized trade — e.g. snapshot only).
        - Inside a batch, events are sorted ts ASC before pushing
          (so CUSUM / run_length stay correct).
        - CUSUM updates on every sample (incremental).

        Note:
            A batch may contain mixed-symbol events — each one is routed to its own
            buffer. Cross-symbol ts order does not matter; only intra-symbol order does.
        """
        # Group by symbol first, then sort each group ts ASC.
        by_symbol: dict[str, list[NormalizedEvent]] = {}
        for ev in events:
            if ev.channel != CHANNEL_POLYMARKET:
                continue
            if ev.price is None:
                continue
            by_symbol.setdefault(ev.symbol, []).append(ev)

        for symbol, evs in by_symbol.items():
            evs.sort(key=lambda e: e.ts_source)  # ASC
            state = self._states.setdefault(symbol, _SymbolState())

            for ev in evs:
                # Extract mid_price (None if missing or non-numeric).
                mid_raw = ev.meta.get("mid_price") if ev.meta else None
                try:
                    mid_price = float(mid_raw) if mid_raw is not None else None
                except (TypeError, ValueError):
                    mid_price = None

                wallet_raw = ev.meta.get("proxy_wallet") if ev.meta else None
                wallet = str(wallet_raw).lower() if wallet_raw else ""

                sample = _Sample(
                    ts=ev.ts_source,
                    size_usd=ev.size_usd,
                    price=ev.price,  # type: ignore[arg-type]  # None already filtered above
                    side=ev.side,
                    mid_price=mid_price,
                    wallet=wallet,
                )
                state.buf.append(sample)

                # Update CUSUM — prefer mid_price; fall back to last_trade_price.
                obs = mid_price if mid_price is not None else ev.price
                self._update_cusum(state, float(obs))  # type: ignore[arg-type]

    # ─────────────────────────────────────────────────────────────────
    # CUSUM incremental update
    # ─────────────────────────────────────────────────────────────────
    def _update_cusum(self, state: _SymbolState, obs: float) -> None:
        """Two-sided CUSUM update.

        S_pos[t] = max(0, S_pos[t-1] + (obs - target - k))
        S_neg[t] = max(0, S_neg[t-1] + (target - obs - k))

        target = EMA of price. On the very first sample we seed target = obs and
        leave both CUSUM accumulators at 0. When either accumulator exceeds
        reset_threshold we drop it back to 0 so we can re-fire later.
        """
        if state.cusum_target is None:
            # First sample — seed target, keep CUSUM at 0.
            state.cusum_target = obs
            return

        # Update target EMA (slowly).
        target = state.cusum_target
        new_target = self._cusum_alpha * obs + (1.0 - self._cusum_alpha) * target
        state.cusum_target = new_target

        # CUSUM update — uses the OLD target (the reference before this sample).
        diff = obs - target
        state.cusum_pos = max(0.0, state.cusum_pos + diff - self._cusum_k)
        state.cusum_neg = max(0.0, state.cusum_neg - diff - self._cusum_k)

        # Reset on overflow (we assume the detector already caught the event).
        if state.cusum_pos > self._cusum_reset:
            state.cusum_pos = 0.0
        if state.cusum_neg > self._cusum_reset:
            state.cusum_neg = 0.0

    # ─────────────────────────────────────────────────────────────────
    # Output — called once per symbol per cycle
    # ─────────────────────────────────────────────────────────────────
    def compute_snapshot(
        self,
        symbol: str,
        now: datetime,
    ) -> FeatureSnapshot | None:
        """Return the current FeatureSnapshot for `symbol` (v2). None if buffer is empty."""
        state = self._states.get(symbol)
        if state is None or not state.buf:
            return None

        # 1) Prune stale entries.
        self._prune(state, now)

        # 2) M1 — record completed 5-min buckets to the baseline store (if configured).
        if self._baseline_store is not None:
            self._record_completed_buckets(symbol, state, now)

        # 3) Compute features.
        cur_w = self._current_window_s

        # ── current-window samples ──
        cur_start = now - timedelta(seconds=cur_w)
        cur_samples = [s for s in state.buf if s.ts >= cur_start]

        current_vol_usd = sum(s.size_usd for s in cur_samples)
        n_trades_5min = float(len(cur_samples))

        # ── v1 in-memory baseline (6 chunks × 5 min) ──
        chunk_vols: list[float] = []
        for i in range(self._baseline_chunk_count):
            chunk_end = now - timedelta(seconds=cur_w * (i + 1))
            chunk_start = chunk_end - timedelta(seconds=cur_w)
            v = sum(s.size_usd for s in state.buf if chunk_start <= s.ts < chunk_end)
            chunk_vols.append(v)

        baseline_ready = (
            len(chunk_vols) >= 2
            and any(v > 0 for v in chunk_vols)
            and self._has_enough_history(state, now)
        )
        if baseline_ready:
            baseline_mean = statistics.mean(chunk_vols)
            try:
                baseline_std = statistics.stdev(chunk_vols)
            except statistics.StatisticsError:
                baseline_std = 0.0
            denom = max(baseline_std, max(baseline_mean * 0.05, 1.0))
            vol_zscore = (current_vol_usd - baseline_mean) / denom
        else:
            baseline_mean = statistics.mean(chunk_vols) if chunk_vols else 0.0
            baseline_std = 0.0
            vol_zscore = 0.0

        # ── v1 price_jump (last_trade_price) ──
        pj_start = now - timedelta(seconds=self._price_jump_window_s)
        pj_window = [s for s in state.buf if s.ts >= pj_start]
        price_jump_1min = (
            (max(s.price for s in pj_window) - min(s.price for s in pj_window))
            if pj_window else 0.0
        )

        # ── v2 M2 mid_price_jump ──
        mid_window = [s.mid_price for s in pj_window if s.mid_price is not None]
        if mid_window:
            mid_price_jump_1min = max(mid_window) - min(mid_window)
            has_mid_price = 1.0
        else:
            mid_price_jump_1min = price_jump_1min  # fall back to last_trade_price
            has_mid_price = 0.0

        # ── v2 M4 directional features ──
        buy_count = 0
        sell_count = 0
        buy_vol = 0.0
        sell_vol = 0.0
        for s in cur_samples:
            cls = _classify_side(s.side)
            if cls == "buy":
                buy_count += 1
                buy_vol += s.size_usd
            elif cls == "sell":
                sell_count += 1
                sell_vol += s.size_usd

        total_directional_vol = buy_vol + sell_vol
        if total_directional_vol > 0:
            imbalance = (buy_vol - sell_vol) / total_directional_vol
        else:
            imbalance = 0.0

        same_side_run = self._compute_same_side_run(state)

        # ── P10.3 wallet concentration features ──
        # cur_samples (last 5 min) is enough for a single-bar burst decision.
        # Insider signature: "lots of volume, few wallets" (a handful dominates).
        # Retail-panic signature: "lots of volume, many wallets" (dispersed).
        wallet_features = self._compute_wallet_features(cur_samples)

        # ── v2 M1 time-of-day baseline ──
        tod_median = 0.0
        tod_mad = 0.0
        tod_n = 0
        vol_zscore_tod = 0.0
        if self._baseline_store is not None:
            tod_median, tod_mad, tod_n = self._baseline_store.get_baseline(symbol, now)
            if tod_n >= self._tod_min_samples:
                # Robust z-score: 1.4826 × MAD ≈ σ (normal-distribution assumption).
                # Noise floor: max(MAD, median*0.05, 1.0) — prevents div-by-zero.
                denom_tod = max(1.4826 * tod_mad, max(tod_median * 0.05, 1.0))
                vol_zscore_tod = (current_vol_usd - tod_median) / denom_tod

        # ── Assemble FeatureSnapshot ──
        features: dict[str, float] = {
            # v1 (kept for compatibility)
            "vol_zscore_5min": float(vol_zscore),
            "price_jump_1min": float(price_jump_1min),
            "current_vol_usd": float(current_vol_usd),
            "baseline_mean_usd": float(baseline_mean),
            "baseline_std_usd": float(baseline_std),
            "n_trades_5min": float(n_trades_5min),
            "baseline_ready": 1.0 if baseline_ready else 0.0,
            # v2 mid-price (M2)
            "mid_price_jump_1min": float(mid_price_jump_1min),
            "has_mid_price": float(has_mid_price),
            # v2 directional (M4)
            "buy_count_5min": float(buy_count),
            "sell_count_5min": float(sell_count),
            "buy_vol_usd_5min": float(buy_vol),
            "sell_vol_usd_5min": float(sell_vol),
            "imbalance_5min": float(imbalance),
            "same_side_run_length": float(same_side_run),
            # v2 CUSUM (incremental state)
            "cusum_pos": float(state.cusum_pos),
            "cusum_neg": float(state.cusum_neg),
            # v2 time-of-day (M1)
            "vol_zscore_tod_v1": float(vol_zscore_tod),
            "tod_baseline_n": float(tod_n),
            "tod_baseline_median": float(tod_median),
            "tod_baseline_mad": float(tod_mad),
            # P10.3 wallet concentration (insider vs retail panic separator)
            **wallet_features,
        }

        return FeatureSnapshot(
            channel=CHANNEL_POLYMARKET,
            symbol=symbol,
            ts=now,
            features=features,
            baseline_ref=self._baseline_ref,
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    def _prune(self, state: _SymbolState, now: datetime) -> None:
        """Drop entries from the left (oldest) of the deque past max_history."""
        cutoff = now - timedelta(seconds=self._max_history_s)
        while state.buf and state.buf[0].ts < cutoff:
            state.buf.popleft()
        state.last_prune_ts = now

    def _has_enough_history(self, state: _SymbolState, now: datetime) -> bool:
        """Do we have enough history to fill the full baseline window?"""
        if not state.buf:
            return False
        oldest = state.buf[0].ts
        required_age = self._current_window_s * (self._baseline_chunk_count + 1)
        return (now - oldest).total_seconds() >= required_age

    @staticmethod
    def _compute_wallet_features(samples: list[_Sample]) -> dict[str, float]:
        """5-min window wallet-concentration statistics.

        Returned features (all float, safely defaulted to 0):

            unique_wallets_5min       : distinct wallets (proxy addresses).
                                        Unknown wallets ("") are excluded.
            top_wallet_share_5min     : USD share of the #1 wallet, in [0, 1].
                                        1.0 = single-wallet dominance.
            top3_wallet_share_5min    : combined share of the top 3 wallets, in [0, 1].
            wallet_hhi_5min           : Σ(s_i)^2  (Herfindahl–Hirschman Index).
                                        Uniform → 0; single wallet → 1; N uniform → 1/N.
            directional_wallets_5min  : Unique wallets that bet on the dominant side
                                        (whichever of buy/sell has the larger volume).
                                        Input to wallet_split_score.
            directional_wallet_ratio  : directional_wallets / unique_wallets, in [0, 1].
                                        ≈1.0 in the "wallet-splitting" scenario
                                        (e.g. 38 accounts all leaning the same way).
            wallet_concentration_score: composite score in [0, 1].
                                        Higher → insider-like (few-wallet dominance OR
                                        many-wallet consensus on a direction).

                                        formula:
                                          0.5 × HHI + 0.3 × directional_ratio
                                          + 0.2 × bonus_for_few_wallets
                                        (bonus = exponential decay:
                                         1 wallet → ~1.0, 50+ wallets → ~0.0)

        Notes:
            - If samples is empty or every wallet is unknown, all values are 0.
            - "Unknown" wallets still count toward the USD total but are not
              counted as unique addresses (avoids over-counting). Production data
              always has a wallet field.
        """
        if not samples:
            return {
                "unique_wallets_5min": 0.0,
                "top_wallet_share_5min": 0.0,
                "top3_wallet_share_5min": 0.0,
                "wallet_hhi_5min": 0.0,
                "directional_wallets_5min": 0.0,
                "directional_wallet_ratio_5min": 0.0,
                "wallet_concentration_score": 0.0,
            }

        # Aggregate per wallet: (total_usd, buy_usd, sell_usd).
        per_wallet_total: dict[str, float] = {}
        per_wallet_buy: dict[str, float] = {}
        per_wallet_sell: dict[str, float] = {}

        total_usd = 0.0
        for s in samples:
            w = s.wallet
            if not w:
                # Unknown wallet — counted in the USD total but skipped for wallet stats.
                total_usd += s.size_usd
                continue
            per_wallet_total[w] = per_wallet_total.get(w, 0.0) + s.size_usd
            total_usd += s.size_usd
            cls = _classify_side(s.side)
            if cls == "buy":
                per_wallet_buy[w] = per_wallet_buy.get(w, 0.0) + s.size_usd
            elif cls == "sell":
                per_wallet_sell[w] = per_wallet_sell.get(w, 0.0) + s.size_usd

        unique_n = len(per_wallet_total)
        if unique_n == 0 or total_usd <= 0:
            return {
                "unique_wallets_5min": 0.0,
                "top_wallet_share_5min": 0.0,
                "top3_wallet_share_5min": 0.0,
                "wallet_hhi_5min": 0.0,
                "directional_wallets_5min": 0.0,
                "directional_wallet_ratio_5min": 0.0,
                "wallet_concentration_score": 0.0,
            }

        # share = each wallet's USD share (total_usd includes the unknown bucket).
        shares = sorted(
            (v / total_usd for v in per_wallet_total.values()),
            reverse=True,
        )
        top_share = shares[0]
        top3_share = sum(shares[:3])
        hhi = sum(s * s for s in shares)

        # directional: wallets betting on the dominant side (whichever direction).
        total_buy = sum(per_wallet_buy.values())
        total_sell = sum(per_wallet_sell.values())
        if total_buy >= total_sell:
            dominant_per_wallet = per_wallet_buy
        else:
            dominant_per_wallet = per_wallet_sell
        # "Meaningful directional commitment" — count a wallet as part of the
        # dominant camp if at least half of its own volume is on that side.
        directional_n = 0
        for w, total_w in per_wallet_total.items():
            if total_w > 0 and dominant_per_wallet.get(w, 0.0) / total_w >= 0.5:
                directional_n += 1
        directional_ratio = directional_n / unique_n if unique_n > 0 else 0.0

        # Bonus: fewer wallets ⇒ higher insider likelihood (exponential decay).
        # 1 wallet → 1.0, 5 → ~0.56, 10 → ~0.27, 30 → ~0.02.
        import math
        few_wallet_bonus = math.exp(-(unique_n - 1) / 7.0)

        # Composite score — directional_ratio (direction consensus) is the strongest
        # insider signature; HHI (few-wallet dominance) is the secondary cue;
        # few_wallet_bonus is a safety net.
        #
        #   weights:  HHI 0.20  +  dir_ratio 0.50  +  few_bonus 0.20  +  consensus_bonus 0.15
        #
        # consensus_bonus: +0.15 when unique_wallets >= 5 AND dir_ratio ~ 1.0.
        #   Captures the "many wallets, all leaning the same way" pattern
        #   (the 38-wallet split insider signature). Without this gate, a 1-2
        #   wallet random case would inflate the directional weight alone.
        consensus_bonus = 0.0
        if unique_n >= 5 and directional_ratio >= 0.95:
            consensus_bonus = 0.15

        score = (
            0.20 * hhi
            + 0.50 * directional_ratio
            + 0.20 * few_wallet_bonus
            + consensus_bonus
        )
        score = max(0.0, min(1.0, score))

        return {
            "unique_wallets_5min": float(unique_n),
            "top_wallet_share_5min": float(top_share),
            "top3_wallet_share_5min": float(top3_share),
            "wallet_hhi_5min": float(hhi),
            "directional_wallets_5min": float(directional_n),
            "directional_wallet_ratio_5min": float(directional_ratio),
            "wallet_concentration_score": float(score),
        }

    def _compute_same_side_run(self, state: _SymbolState) -> int:
        """Number of consecutive same-side samples counting back from the newest.

        - A None side (e.g. a snapshot-only sample) terminates the run.
        - Scan at most _RUN_LENGTH_SCAN_MAX samples (cost guard).
        - Even when every sample is on the same side, the result is capped —
          this is intentional (runs ≥ 200 are semantically identical).
        """
        run = 0
        last_cls: str | None = None
        scanned = 0
        # buf is a deque — iterate from the right.
        for sample in reversed(state.buf):
            scanned += 1
            if scanned > _RUN_LENGTH_SCAN_MAX:
                break
            cls = _classify_side(sample.side)
            if cls is None:
                break
            if last_cls is None:
                last_cls = cls
                run = 1
            elif cls == last_cls:
                run += 1
            else:
                break
        return run

    def _record_completed_buckets(
        self,
        symbol: str,
        state: _SymbolState,
        now: datetime,
    ) -> None:
        """Record all freshly-completed 5-min buckets to baseline_store.

        - now=14:23 → most recently completed bucket_end = 14:20
          (= floor_to_bucket_boundary(now))
        - now=14:25 → 14:25 (same as the floor)

        Start at last_recorded_bucket_end + 5 min and walk every boundary up to
        floor_now. If buf is too short to cover a bucket fully (no data older
        than its start), skip — recording partial sums would poison the baseline.
        """
        assert self._baseline_store is not None  # the caller guarantees this

        # Edge case: empty buffer on first call → nothing to record.
        # (A freshly-polled slug with zero trades.) If last_recorded_bucket_end
        # already exists, we can still advance, so fall through.
        if not state.buf and state.last_recorded_bucket_end is None:
            return

        floor_now = _floor_to_bucket_boundary(now)
        if state.last_recorded_bucket_end is None:
            # First call — start at the first boundary AFTER the oldest sample in buf.
            # (Earlier buckets may not have any data and would be unreliable.)
            # The early return above guarantees buf is non-empty here.
            oldest_ts = state.buf[0].ts
            # Start at the boundary AFTER oldest_ts.
            next_be = align_to_bucket_end(oldest_ts)
            # If oldest_ts is exactly on a boundary, jump to the next 5 min.
            if next_be == oldest_ts:
                next_be = next_be + timedelta(seconds=_BUCKET_WIDTH_S)
        else:
            next_be = state.last_recorded_bucket_end + timedelta(seconds=_BUCKET_WIDTH_S)

        # Record every completed bucket up to floor_now.
        while next_be <= floor_now:
            bucket_start = next_be - timedelta(seconds=_BUCKET_WIDTH_S)
            # If the oldest sample in buf is newer than bucket_start, the bucket
            # has partial data → skip but still advance the cursor.
            oldest_ts = state.buf[0].ts if state.buf else now
            if oldest_ts > bucket_start:
                # Bucket's leading portion isn't in buf → untrusted, skip.
                logger.debug(
                    "polymarket bucket skip (insufficient history): symbol=%s be=%s oldest=%s",
                    symbol, next_be.isoformat(), oldest_ts.isoformat(),
                )
            else:
                vol = sum(
                    s.size_usd for s in state.buf
                    if bucket_start <= s.ts < next_be
                )
                try:
                    self._baseline_store.record_bucket(symbol, next_be, vol)
                except Exception as e:
                    # Don't let a baseline_store error break the detector loop.
                    logger.warning(
                        "PolymarketFeatures: record_bucket failed (%s, %s): %s",
                        symbol, next_be.isoformat(), e,
                    )

            state.last_recorded_bucket_end = next_be
            next_be = next_be + timedelta(seconds=_BUCKET_WIDTH_S)

    # ─────────────────────────────────────────────────────────────────
    # Debug / testing helpers
    # ─────────────────────────────────────────────────────────────────
    def buffer_size(self, symbol: str) -> int:
        """Current sample count in the symbol buffer. Used for monitoring."""
        s = self._states.get(symbol)
        return len(s.buf) if s else 0

    def known_symbols(self) -> list[str]:
        """All symbols that have ever been touched by add_events."""
        return list(self._states.keys())

    def cusum_state(self, symbol: str) -> tuple[float, float, float | None]:
        """Debug helper — (cusum_pos, cusum_neg, target)."""
        s = self._states.get(symbol)
        if s is None:
            return (0.0, 0.0, None)
        return (s.cusum_pos, s.cusum_neg, s.cusum_target)


# =====================================================================
# Backward-compat — skeleton signature
# =====================================================================
def compute_features(
    events: list[NormalizedEvent],
    baseline: dict | None = None,  # noqa: ARG001 — unused in v1 (in-memory baseline)
) -> FeatureSnapshot | None:
    """Skeleton-compatible entry. One-shot use (testing / debugging)."""
    if not events:
        return None
    f = PolymarketFeatures()
    f.add_events(events)
    symbols = f.known_symbols()
    if not symbols:
        return None
    now = max(e.ts_source for e in events) if events else datetime.now(timezone.utc)
    return f.compute_snapshot(symbols[0], now)
