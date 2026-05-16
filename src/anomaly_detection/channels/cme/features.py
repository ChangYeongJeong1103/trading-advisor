"""
cme/features.py — Rolling feature engineering (per CME symbol).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5 pipeline step 3):
  Consume the CME mock NormalizedEvent stream and compute per-symbol rolling stats.

  v1 P4 baseline (same pattern as Polymarket — trade-level aggregation):

    - vol_zscore_5min   : z-score of 5-min USD volume (vs 30-min baseline)
    - price_jump_pct_1min : (max - min) / mid_price over 1 min (% change)
                          CME prices vary widely in absolute terms (CL 78 vs ES 5400),
                          so we unify on % — different from Polymarket's [0,1] prices.
    - price_jump_pct_5min : (max - min) / mid_price over 5 min (% change)
                          Lingering effect after a burst — stays high while the burst
                          remains in the window. Same time scale as vol_zscore_5min.
                          Used in the EMERGENCY rule (vol+price simultaneous).
    - current_vol_usd   : sum of USD volume over the last 5 minutes (debug)
    - baseline_mean_usd / baseline_std_usd / n_trades_5min / mock_spike_count
                        : debug & mock-spike audit

  Walking-skeleton: z-score is 0 if baseline < 30 minutes → detector NORMAL.
  Becomes fully functional after ~35-40 minutes of accumulation.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · In-memory rolling buffer only (same as Polymarket / Hyperliquid).
    Baseline accumulates again on daemon restart — acceptable.

  · price_jump unit is % (Polymarket: absolute [0,1] / Hyperliquid: unused / CME: %).

  · Per-symbol state. Not thread-safe — called only from one channel task.

  · mock_spike_count counts trade.meta.is_mock_spike → exposed in the feature dict
    for audit/debug (0 on real CME).

────────────────────────────────────────────────────────────────────────
Architecture: §5 Pipeline step 3 (feature engine), §4.1 FeatureSnapshot
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque

from ...core.schemas import CHANNEL_CME, FeatureSnapshot, NormalizedEvent

logger = logging.getLogger(__name__)


# =====================================================================
# Per-symbol buffer entry
# =====================================================================
@dataclass(frozen=True, slots=True)
class _Sample:
    """One trade in the rolling buffer."""
    ts: datetime
    size_usd: float
    price: float
    is_mock_spike: bool
    # P12-D — aggressor side (sourced from the CME Databento `side` column).
    # "buy"  : 'A' (Ask hit) — buyer-initiated
    # "sell" : 'B' (Bid hit) — seller-initiated
    # None   : 'N' / unknown — no aggressor info
    aggressor_side: str | None = None


# =====================================================================
# Per-symbol state
# =====================================================================
@dataclass
class _SymbolState:
    buf: Deque[_Sample] = field(default_factory=deque)
    last_prune_ts: datetime | None = None


# =====================================================================
# CMEFeatures — main class
# =====================================================================
class CMEFeatures:
    """CME per-symbol rolling feature engine (Polymarket pattern + % price change).

    Usage:
        features = CMEFeatures()
        features.add_events([normalized1, normalized2, ...])
        snapshot = features.compute_snapshot(symbol="CL", now=...)
        if snapshot is not None:
            detector.evaluate(snapshot)
    """

    def __init__(
        self,
        *,
        current_window_s: int = 300,             # 5min — current vol window
        baseline_chunk_count: int = 6,           # 6 × 5min = 30min baseline
        price_jump_window_s: int = 60,           # 1min — fresh price max-min
        price_jump_window_long_s: int = 300,     # 5min — lingering price max-min
        # ── P12-B / P12-D (cme_insider_v1) extra windows ───────────────
        # Expose notional / trade_count / jump / aggressor separately for
        # 1/2/5min buckets. insider_v1 detector consumes them via multi-trigger (max tier).
        insider_short_bucket_s: int = 60,        # 1min
        insider_mid_bucket_s: int = 120,         # 2min
        insider_long_bucket_s: int = 300,        # 5min (added in P12-D)
        # trade_count baseline averages N prior shorter buckets.
        # 1/2min: 30 chunks (= 30~60min). 5min: 12 chunks (= 1h) — memory cap.
        insider_count_baseline_chunks: int = 30,
        insider_count_baseline_chunks_5min: int = 12,
        max_history_s: int | None = None,
    ) -> None:
        if current_window_s < 30:
            raise ValueError("current_window_s too small (>=30s)")
        if baseline_chunk_count < 2:
            raise ValueError("baseline_chunk_count too small (>=2)")
        # The longer window must be at least as long as the shorter one — otherwise the meaning inverts.
        if price_jump_window_long_s < price_jump_window_s:
            raise ValueError(
                f"price_jump_window_long_s ({price_jump_window_long_s}) "
                f"must be >= price_jump_window_s ({price_jump_window_s})"
            )
        if insider_mid_bucket_s < insider_short_bucket_s:
            raise ValueError("insider_mid_bucket_s < insider_short_bucket_s")
        if insider_long_bucket_s < insider_mid_bucket_s:
            raise ValueError("insider_long_bucket_s < insider_mid_bucket_s")
        if insider_count_baseline_chunks < 2:
            raise ValueError("insider_count_baseline_chunks too small (>=2)")
        if insider_count_baseline_chunks_5min < 2:
            raise ValueError(
                "insider_count_baseline_chunks_5min too small (>=2)"
            )

        self._current_window_s = current_window_s
        self._baseline_chunk_count = baseline_chunk_count
        self._price_jump_window_s = price_jump_window_s
        self._price_jump_window_long_s = price_jump_window_long_s
        self._insider_short_bucket_s = insider_short_bucket_s
        self._insider_mid_bucket_s = insider_mid_bucket_s
        self._insider_long_bucket_s = insider_long_bucket_s
        self._insider_count_baseline_chunks = insider_count_baseline_chunks
        self._insider_count_baseline_chunks_5min = (
            insider_count_baseline_chunks_5min
        )

        if max_history_s is None:
            max_history_s = (
                current_window_s * (baseline_chunk_count + 1)
                + 60
            )
        # history must not be shorter than the long price_jump window — safety.
        max_history_s = max(max_history_s, price_jump_window_long_s + 60)
        # Cover all insider_v1 trade_count baselines:
        #   max of 2min × (30+1) = 62min, 5min × (12+1) = 65min.
        insider_required = max(
            insider_mid_bucket_s * (insider_count_baseline_chunks + 1) + 60,
            insider_long_bucket_s * (insider_count_baseline_chunks_5min + 1)
            + 60,
        )
        max_history_s = max(max_history_s, insider_required)
        self._max_history_s = max_history_s

        self._states: dict[str, _SymbolState] = {}

        self._baseline_ref = (
            f"cme:rolling:cur{current_window_s}s:base{baseline_chunk_count}x"
            f"{current_window_s}s:v1"
        )

    # ─────────────────────────────────────────────────────────────────
    # Input
    # ─────────────────────────────────────────────────────────────────
    def add_events(self, events: Iterable[NormalizedEvent]) -> None:
        """Push NormalizedEvents into per-symbol buffers.

        - Silently skip events from channels other than CME.
        - Skip if price is None.
        """
        for ev in events:
            if ev.channel != CHANNEL_CME:
                continue
            if ev.price is None:
                continue
            is_spike = bool(ev.meta.get("is_mock_spike", False))
            # P12-D aggressor: ev.side is Side.BUY/SELL/None.
            agg = ev.side.value if ev.side is not None else None
            state = self._states.setdefault(ev.symbol, _SymbolState())
            state.buf.append(_Sample(
                ts=ev.ts_source,
                size_usd=ev.size_usd,
                price=ev.price,
                is_mock_spike=is_spike,
                aggressor_side=agg,
            ))

    # ─────────────────────────────────────────────────────────────────
    # Output — called once per cycle per symbol
    # ─────────────────────────────────────────────────────────────────
    def compute_snapshot(
        self,
        symbol: str,
        now: datetime,
    ) -> FeatureSnapshot | None:
        """Produce the current FeatureSnapshot for symbol. None if buffer empty."""
        state = self._states.get(symbol)
        if state is None or not state.buf:
            return None

        # 1) prune
        self._prune(state, now)

        cur_w = self._current_window_s

        # 2) Current-window sum
        cur_start = now - timedelta(seconds=cur_w)
        cur_samples = [s for s in state.buf if s.ts >= cur_start]
        current_vol_usd = sum(s.size_usd for s in cur_samples)
        n_trades_5min = float(len(cur_samples))
        mock_spike_count = float(sum(1 for s in cur_samples if s.is_mock_spike))

        # 3) baseline: 6 chunks × cur_w sec each
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

        # 4) price_jump (1min): fresh price move — volatility within the past 1 min.
        #    Sensitive to single-news instantaneous jumps (consumed by the price_jump_v1 detector).
        price_jump_pct_1min = self._compute_price_jump(
            state, now, window_s=self._price_jump_window_s
        )

        # 5) price_jump (5min): lingering price move — max-min over the last 5 min.
        #    Persists naturally while the burst stays in the window → same time scale as
        #    vol_zscore_5min. Used by the EMERGENCY rule (vol+price simultaneous).
        price_jump_pct_5min = self._compute_price_jump(
            state, now, window_s=self._price_jump_window_long_s
        )

        # 5b) signed price change (1min, 5min) — for direction inference (P12-D).
        #     Paired with price_jump_pct (unsigned range). cme_insider_v1 selects
        #     the matching one for the winning bucket and derives UP/DOWN/NEUTRAL.
        price_change_signed_1min = self._compute_price_change_signed(
            state, now, window_s=self._price_jump_window_s,
        )
        price_change_signed_5min = self._compute_price_change_signed(
            state, now, window_s=self._price_jump_window_long_s,
        )

        # 6) ── P12-B insider_v1 features (1min / 2min buckets) ────────
        # Consumed by cme_insider_v1 detector as a dual-trigger. Per bucket:
        #   notional_musd_<freq>      : USD volume in the bucket / 1e6
        #   trade_count_<freq>        : trade count in the bucket
        #   trade_count_baseline_<freq>: mean count over the prior N buckets (spike baseline)
        #   notional_musd_prev_<freq> : notional from 1 bucket ago (persistence)
        #   price_jump_pct_<freq>     : max-min/mid change within the bucket
        # ──────────────────────────────────────────────────────────────
        insider_features = self._compute_insider_features(state, now)

        features: dict[str, float] = {
            "vol_zscore_5min": float(vol_zscore),
            "price_jump_pct_1min": float(price_jump_pct_1min),
            "price_jump_pct_5min": float(price_jump_pct_5min),
            "price_change_pct_signed_1min": float(price_change_signed_1min),
            "price_change_pct_signed_5min": float(price_change_signed_5min),
            "current_vol_usd": float(current_vol_usd),
            "baseline_mean_usd": float(baseline_mean),
            "baseline_std_usd": float(baseline_std),
            "n_trades_5min": float(n_trades_5min),
            "mock_spike_count_5min": mock_spike_count,
            "baseline_ready": 1.0 if baseline_ready else 0.0,
            **insider_features,
        }

        return FeatureSnapshot(
            channel=CHANNEL_CME,
            symbol=symbol,
            ts=now,
            features=features,
            baseline_ref=self._baseline_ref,
        )

    # ─────────────────────────────────────────────────────────────────
    # P12-B insider_v1 helpers
    # ─────────────────────────────────────────────────────────────────
    def _compute_insider_features(
        self, state: _SymbolState, now: datetime,
    ) -> dict[str, float]:
        """Compute insider_v1 input features for the 1min + 2min buckets.

        Args:
            state: trade buffer for this symbol.
            now: current time (UTC).

        Returns:
            dict with keys:
              notional_musd_1min, notional_musd_2min
              trade_count_1min, trade_count_2min
              trade_count_baseline_1min, trade_count_baseline_2min
              notional_musd_prev_1min, notional_musd_prev_2min
              price_jump_pct_2min
        """
        out: dict[str, float] = {}
        # P12-D: handle 1/2/5min buckets uniformly. Only the 5min uses a
        # different baseline chunk count (memory cap).
        bucket_specs = (
            ("1min", self._insider_short_bucket_s,
             self._insider_count_baseline_chunks),
            ("2min", self._insider_mid_bucket_s,
             self._insider_count_baseline_chunks),
            ("5min", self._insider_long_bucket_s,
             self._insider_count_baseline_chunks_5min),
        )
        for label, bucket_s, n_baseline_chunks in bucket_specs:
            # Current bucket [now - bucket_s, now)
            cur_start = now - timedelta(seconds=bucket_s)
            cur_samples = [s for s in state.buf if cur_start <= s.ts < now]
            cur_vol_usd = sum(s.size_usd for s in cur_samples)
            cur_count = len(cur_samples)

            # Previous bucket [now - 2*bucket_s, now - bucket_s)
            prev_start = now - timedelta(seconds=bucket_s * 2)
            prev_end = cur_start
            prev_samples = [s for s in state.buf if prev_start <= s.ts < prev_end]
            prev_vol_usd = sum(s.size_usd for s in prev_samples)

            # trade_count baseline: mean trade count over N prior buckets.
            # current and prev are excluded from baseline (they may themselves be the spike).
            baseline_counts: list[float] = []
            for i in range(2, 2 + n_baseline_chunks):
                b_end = now - timedelta(seconds=bucket_s * i)
                b_start = b_end - timedelta(seconds=bucket_s)
                cnt = sum(1 for s in state.buf if b_start <= s.ts < b_end)
                baseline_counts.append(float(cnt))
            baseline_mean_count = (
                statistics.mean(baseline_counts) if baseline_counts else 0.0
            )

            out[f"notional_musd_{label}"] = float(cur_vol_usd / 1e6)
            out[f"notional_musd_prev_{label}"] = float(prev_vol_usd / 1e6)
            out[f"trade_count_{label}"] = float(cur_count)
            out[f"trade_count_baseline_{label}"] = float(baseline_mean_count)

            # P12-D — identify absorption patterns via aggressor breakdown.
            buy_vol_usd, sell_vol_usd, agg_imb = self._aggressor_stats(cur_samples)
            out[f"aggressor_buy_vol_{label}"] = float(buy_vol_usd)
            out[f"aggressor_sell_vol_{label}"] = float(sell_vol_usd)
            out[f"aggressor_imbalance_{label}"] = float(agg_imb)

        # price_jump_pct_2min — 1min is already computed in main snapshot,
        # 5min is also exposed in main snapshot as price_jump_pct_5min.
        out["price_jump_pct_2min"] = float(self._compute_price_jump(
            state, now, window_s=self._insider_mid_bucket_s,
        ))
        # signed 2min change — for direction (1min/5min already in main snapshot).
        out["price_change_pct_signed_2min"] = float(
            self._compute_price_change_signed(
                state, now, window_s=self._insider_mid_bucket_s,
            )
        )
        return out

    @staticmethod
    def _aggressor_stats(
        samples: list[_Sample],
    ) -> tuple[float, float, float]:
        """USD totals on each aggressor side → (buy_vol, sell_vol, imbalance).

        Returns:
            (buy_vol_usd, sell_vol_usd, imbalance):
              imbalance = (buy - sell) / (buy + sell), range [-1, +1].
              imbalance=0.0 when both are 0.

        Note:
            Mismatches such as "imbalance > 0 (buy dominant) + price DOWN" or
            "imbalance < 0 (sell dominant) + price UP" are signatures of
            absorption / iceberg patterns (consumed by LLM/detector).
        """
        buy_vol = sum(s.size_usd for s in samples if s.aggressor_side == "buy")
        sell_vol = sum(s.size_usd for s in samples if s.aggressor_side == "sell")
        total = buy_vol + sell_vol
        imb = (buy_vol - sell_vol) / total if total > 0 else 0.0
        return buy_vol, sell_vol, imb

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    def _compute_price_jump(
        self,
        state: _SymbolState,
        now: datetime,
        *,
        window_s: int,
    ) -> float:
        """(max - min) / mid_price inside the given window → % change.

        Args:
            state: per-symbol buffer.
            now: reference time (UTC).
            window_s: window length in seconds. e.g. 1min=60, 5min=300.

        Returns:
            float: % change (0.0 ~ 1.0). 0.0 if buffer empty.

        Note:
            Direction-agnostic by definition (up and down are both detected the same way).
            While a burst sits inside the window, max/min are spread by it →
            naturally lingers until the window leaves the burst behind.
            Use _compute_price_change_signed when you need direction (added in P12-D).
        """
        win_start = now - timedelta(seconds=window_s)
        prices = [s.price for s in state.buf if s.ts >= win_start]
        if not prices:
            return 0.0
        pmax = max(prices)
        pmin = min(prices)
        # mid_price is never 0/negative, but guard anyway.
        pmid = (pmax + pmin) / 2.0 if (pmax + pmin) > 0 else 1.0
        if pmid <= 0:
            return 0.0
        return (pmax - pmin) / pmid

    def _compute_price_change_signed(
        self,
        state: _SymbolState,
        now: datetime,
        *,
        window_s: int,
    ) -> float:
        """(last - first) / first within the given window → signed % change.

        Added in P12-D — used for direction (UP/DOWN/NEUTRAL) inference.

        Args:
            state: per-symbol buffer.
            now: reference time (UTC).
            window_s: window length in seconds.

        Returns:
            float: signed % change (e.g. +0.0049 = +0.49% UP, -0.003 = -0.30% DOWN).
                   0.0 if no samples or only one sample.

        Note:
            Paired with price_jump_pct (range, unsigned). Reading both metrics in the
            same window distinguishes "range 0.49% but net change +0.45% — almost
            entirely one direction" from "range 0.49% but net change +0.05% — two-sided oscillation".
        """
        win_start = now - timedelta(seconds=window_s)
        samples = sorted(
            (s for s in state.buf if s.ts >= win_start),
            key=lambda s: s.ts,
        )
        if len(samples) < 2:
            return 0.0
        first_price = samples[0].price
        last_price = samples[-1].price
        if first_price <= 0:
            return 0.0
        return (last_price - first_price) / first_price

    def _prune(self, state: _SymbolState, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._max_history_s)
        while state.buf and state.buf[0].ts < cutoff:
            state.buf.popleft()
        state.last_prune_ts = now

    def _has_enough_history(self, state: _SymbolState, now: datetime) -> bool:
        if not state.buf:
            return False
        oldest = state.buf[0].ts
        required_age = self._current_window_s * (self._baseline_chunk_count + 1)
        return (now - oldest).total_seconds() >= required_age

    # ─────────────────────────────────────────────────────────────────
    # For debug / testing
    # ─────────────────────────────────────────────────────────────────
    def buffer_size(self, symbol: str) -> int:
        s = self._states.get(symbol)
        return len(s.buf) if s else 0

    def known_symbols(self) -> list[str]:
        return list(self._states.keys())


# =====================================================================
# Backward-compat — skeleton signature
# =====================================================================
def compute_features(
    events: list[NormalizedEvent],
    baseline: dict | None = None,  # noqa: ARG001
) -> FeatureSnapshot | None:
    """Skeleton-compat entry. One-shot use (test/debug)."""
    if not events:
        return None
    f = CMEFeatures()
    f.add_events(events)
    symbols = f.known_symbols()
    if not symbols:
        return None
    now = max(e.ts_source for e in events) if events else datetime.now(timezone.utc)
    return f.compute_snapshot(symbols[0], now)
