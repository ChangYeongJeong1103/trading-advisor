"""
hyperliquid/features.py — Rolling feature engineering (per coin).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5 pipeline step 3):
  Consume the Hyperliquid NormalizedEvent (snapshot-shaped) stream and produce
  per-coin rolling stats for the detector.

  Major difference from Polymarket:
    Polymarket → each NormalizedEvent is 1 trade (size_usd > 0).
                  features = sum(size_usd) over 5min window.
    Hyperliquid → each NormalizedEvent is a snapshot (cumulative `day_ntl_vlm_usd`).
                  features = day_ntl_vlm_usd[t=now] - day_ntl_vlm_usd[t=now-5min].
                  → "snapshot from 5 min ago" must remain in the buffer → retain 30 min+.

  v1 P3 baseline (vol_only — per user decision):
    - vol_zscore_5min     : z-score of the 5-min USD volume delta
                            (baseline = mean / stdev of 6 prior 5-min volume deltas
                             over the past 30 min)
    - current_vol_usd     : most recent 5-min USD volume delta (debug)
    - baseline_mean_usd   : baseline mean (debug)
    - baseline_std_usd    : baseline stdev (debug)
    - day_ntl_vlm_usd     : most recent cumulative volume snapshot (debug, for comparison)
    - mark_px             : most recent mark price (debug, shown in alert body)

  Walking-skeleton: z-score is forced to 0 when baseline < 30 min →
  detector only emits NORMAL. Becomes fully functional after ~35-40 min of accumulation.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · cumulative → delta is computed as "day_ntl_vlm of the most recent t1 snapshot"
    minus "day_ntl_vlm of the snapshot closest to (t1 - 5min) or earlier".
    With 5s polling we receive 12 snapshots per minute → the 5-min-ago snapshot is nearly exact.

  · When cumulative resets (right after daemon restart or Hyperliquid 24h
    rollover), delta can be negative → clamp via max(0, delta) + warning log.
    24h rollover happens about once per day → v1 impact is minimal (z-score treated as 0).

  · In-memory rolling buffer only. P3 walking-skeleton is fine with 35~40 min of history.
    Baseline re-accumulates on daemon restart — acceptable.

  · Per-coin state. Automatically routed by NormalizedEvent.symbol on add_events.

  · Not thread-safe. v1 channel calls it from one task only — sufficient.

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

from ...core.schemas import CHANNEL_HYPERLIQUID, FeatureSnapshot, NormalizedEvent

logger = logging.getLogger(__name__)


# =====================================================================
# Per-coin snapshot buffer entry
# =====================================================================
@dataclass(frozen=True, slots=True)
class _Snapshot:
    """One snapshot in the rolling buffer.

    Carries the cumulative day_ntl_vlm_usd and mark_px from Hyperliquid.
    Delta computation (cur - 5min ago) needs cumulative → keep it.
    """
    ts: datetime                # UTC, ts_source
    day_ntl_vlm: float          # cumulative 24h notional volume (USD)
    mark_px: float              # markPx at that instant
    open_interest_coins: float | None = None
    funding_rate: float | None = None


# =====================================================================
# Per-coin state
# =====================================================================
@dataclass
class _CoinState:
    """Rolling buffer for a single coin."""
    buf: Deque[_Snapshot] = field(default_factory=deque)
    last_prune_ts: datetime | None = None


# =====================================================================
# HyperliquidFeatures — main class
# =====================================================================
class HyperliquidFeatures:
    """Hyperliquid per-coin rolling feature engine.

    Usage:
        features = HyperliquidFeatures()
        features.add_events([normalized_btc_snap1, normalized_eth_snap1, ...])
        snapshot = features.compute_snapshot(coin="BTC", now=...)
        if snapshot is not None:
            detector.evaluate(snapshot)

    compute_snapshot can return None when:
        - The coin's buffer is empty (no snapshot yet)
    """

    def __init__(
        self,
        *,
        current_window_s: int = 300,        # 5min — "current" volume delta window
        baseline_chunk_count: int = 6,      # 6 × 5min = 30min baseline
        max_history_s: int | None = None,   # default = current + baseline + slack
    ) -> None:
        """
        Args:
            current_window_s: "last X seconds" delta window. Default 5 min.
            baseline_chunk_count: number of chunks used for the baseline mean.
                Default 6 → 30-min baseline.
            max_history_s: max history retained in the deque. None →
                current + baseline + 60s slack.
        """
        if current_window_s < 60:
            raise ValueError("current_window_s too small (>=60s) — Hyperliquid snapshot delta stability")
        if baseline_chunk_count < 2:
            raise ValueError("baseline_chunk_count too small (>=2)")

        self._current_window_s = current_window_s
        self._baseline_chunk_count = baseline_chunk_count

        if max_history_s is None:
            max_history_s = (
                current_window_s * (baseline_chunk_count + 1)
                + 60  # slack
            )
        self._max_history_s = max_history_s

        self._states: dict[str, _CoinState] = {}

        # baseline ref — stamped into FeatureSnapshot.baseline_ref
        self._baseline_ref = (
            f"hyperliquid:rolling:cur{current_window_s}s:base{baseline_chunk_count}x"
            f"{current_window_s}s:v2"
        )

    # ─────────────────────────────────────────────────────────────────
    # Input
    # ─────────────────────────────────────────────────────────────────
    def add_events(self, events: Iterable[NormalizedEvent]) -> None:
        """Push NormalizedEvents into per-coin buffers.

        - Silently skip events from channels other than Hyperliquid (defensive).
        - Skip + warn if meta is missing or has None for day_ntl_vlm_usd / mark_px.
        """
        for ev in events:
            if ev.channel != CHANNEL_HYPERLIQUID:
                continue
            day_ntl = ev.meta.get("day_ntl_vlm_usd")
            mark_px = ev.meta.get("mark_px")
            oi_coins = ev.meta.get("open_interest_coins")
            funding = ev.meta.get("funding_rate")
            if day_ntl is None or mark_px is None:
                logger.debug(
                    "HyperliquidFeatures: skip event (missing meta) symbol=%s",
                    ev.symbol,
                )
                continue
            try:
                day_ntl_f = float(day_ntl)
                mark_px_f = float(mark_px)
            except (TypeError, ValueError):
                logger.debug(
                    "HyperliquidFeatures: skip event (bad meta types) symbol=%s",
                    ev.symbol,
                )
                continue

            state = self._states.setdefault(ev.symbol, _CoinState())
            oi_coins_f = _safe_float(oi_coins)
            funding_f = _safe_float(funding)
            state.buf.append(_Snapshot(
                ts=ev.ts_source,
                day_ntl_vlm=day_ntl_f,
                mark_px=mark_px_f,
                open_interest_coins=oi_coins_f,
                funding_rate=funding_f,
            ))

    # ─────────────────────────────────────────────────────────────────
    # Output — called once per cycle per coin
    # ─────────────────────────────────────────────────────────────────
    def compute_snapshot(
        self,
        coin: str,
        now: datetime,
    ) -> FeatureSnapshot | None:
        """Produce the current FeatureSnapshot for coin. None if buffer empty."""
        state = self._states.get(coin)
        if state is None or not state.buf:
            return None

        # 1) Prune old entries
        self._prune(state, now)

        # 2) Most recent snapshot — for debug / display
        latest = state.buf[-1]

        # 3) 5-min delta computation:
        #    day_ntl_vlm at "most recent ts"  -  day_ntl_vlm at snapshot closest to "now - 5min"
        cur_w = self._current_window_s
        cur_vol_usd = self._volume_delta(state, now, cur_w)

        # 4) baseline: 6 chunks × cur_w sec each
        chunk_vols: list[float] = []
        for i in range(self._baseline_chunk_count):
            chunk_end = now - timedelta(seconds=cur_w * (i + 1))
            v = self._volume_delta(state, chunk_end, cur_w)
            # may go negative on cumulative reset / missing data → clamp to 0 (defensive)
            chunk_vols.append(max(0.0, v))

        # 5) baseline mean / std — real z-score when enough data, otherwise 0
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
            vol_zscore = (cur_vol_usd - baseline_mean) / denom
        else:
            baseline_mean = statistics.mean(chunk_vols) if chunk_vols else 0.0
            baseline_std = 0.0
            vol_zscore = 0.0  # walking-skeleton: ensure NORMAL during warmup

        # 6) P9.2.P1 additional derived features
        #    - OI delta: distinguishes position accumulation / liquidation (insider vs panic)
        #    - funding delta: detects leverage-bias shifts
        #    - price impact per volume: price impact relative to volume
        oi_delta_usd_5min = self._oi_notional_delta(state, now, cur_w)
        funding_delta_5min = self._funding_delta(state, now, cur_w)
        price_return_5min = self._mark_return(state, now, cur_w)
        price_return_abs_5min = abs(price_return_5min)
        impact_bps_per_1m_5min = self._impact_bps_per_1m(
            abs_return=price_return_abs_5min,
            vol_usd=max(0.0, cur_vol_usd),
        )

        baseline_impacts: list[float] = []
        for i in range(self._baseline_chunk_count):
            chunk_end = now - timedelta(seconds=cur_w * (i + 1))
            chunk_vol = max(0.0, self._volume_delta(state, chunk_end, cur_w))
            chunk_abs_ret = abs(self._mark_return(state, chunk_end, cur_w))
            baseline_impacts.append(
                self._impact_bps_per_1m(abs_return=chunk_abs_ret, vol_usd=chunk_vol)
            )
        impact_baseline_mean = (
            statistics.mean(baseline_impacts) if baseline_impacts else 0.0
        )
        impact_ratio_vs_baseline = (
            impact_bps_per_1m_5min / max(impact_baseline_mean, 1e-6)
            if impact_bps_per_1m_5min > 0
            else 0.0
        )

        latest_oi_notional = 0.0
        has_open_interest = 0.0
        if latest.open_interest_coins is not None:
            latest_oi_notional = latest.open_interest_coins * latest.mark_px
            has_open_interest = 1.0

        latest_funding = 0.0
        has_funding = 0.0
        if latest.funding_rate is not None:
            latest_funding = latest.funding_rate
            has_funding = 1.0

        features: dict[str, float] = {
            "vol_zscore_5min": float(vol_zscore),
            "current_vol_usd": float(max(0.0, cur_vol_usd)),
            "baseline_mean_usd": float(baseline_mean),
            "baseline_std_usd": float(baseline_std),
            "day_ntl_vlm_usd": float(latest.day_ntl_vlm),
            "mark_px": float(latest.mark_px),
            "baseline_ready": 1.0 if baseline_ready else 0.0,
            "open_interest_notional_usd": float(latest_oi_notional),
            "oi_delta_usd_5min": float(oi_delta_usd_5min),
            "oi_delta_ratio_5min": float(
                oi_delta_usd_5min / max(abs(latest_oi_notional - oi_delta_usd_5min), 1.0)
            ),
            "has_open_interest": float(has_open_interest),
            "funding_rate": float(latest_funding),
            "funding_delta_5min": float(funding_delta_5min),
            "has_funding": float(has_funding),
            "price_return_5min": float(price_return_5min),
            "price_return_abs_5min": float(price_return_abs_5min),
            "impact_bps_per_1m_5min": float(impact_bps_per_1m_5min),
            "impact_bps_per_1m_baseline": float(impact_baseline_mean),
            "impact_ratio_vs_baseline": float(impact_ratio_vs_baseline),
        }

        return FeatureSnapshot(
            channel=CHANNEL_HYPERLIQUID,
            symbol=coin,
            ts=now,
            features=features,
            baseline_ref=self._baseline_ref,
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    def _volume_delta(
        self,
        state: _CoinState,
        end_ts: datetime,
        window_s: int,
    ) -> float:
        """USD volume delta between [end_ts - window_s, end_ts].

        Computed as the difference of cumulative day_ntl_vlm_usd between two snapshots.

        Returns:
            float: USD delta. 0.0 if no suitable snapshot is found.
                   Clamped to 0.0 when a cumulative reset / negative is detected.
        """
        if not state.buf:
            return 0.0

        snap_after_end, snap_before_start = self._window_edges(state, end_ts, window_s)
        if snap_after_end is None or snap_before_start is None:
            return 0.0

        delta = snap_after_end.day_ntl_vlm - snap_before_start.day_ntl_vlm
        if delta < 0:
            # cumulative reset (24h rollover or right after daemon restart)
            return 0.0
        return delta

    def _oi_notional_delta(
        self,
        state: _CoinState,
        end_ts: datetime,
        window_s: int,
    ) -> float:
        """5-min OI notional change (USD, signed)."""
        snap_after_end, snap_before_start = self._window_edges(state, end_ts, window_s)
        if snap_after_end is None or snap_before_start is None:
            return 0.0
        if (
            snap_after_end.open_interest_coins is None
            or snap_before_start.open_interest_coins is None
        ):
            return 0.0

        end_notional = snap_after_end.open_interest_coins * snap_after_end.mark_px
        start_notional = snap_before_start.open_interest_coins * snap_before_start.mark_px
        return end_notional - start_notional

    def _funding_delta(
        self,
        state: _CoinState,
        end_ts: datetime,
        window_s: int,
    ) -> float:
        """5-min funding change (signed)."""
        snap_after_end, snap_before_start = self._window_edges(state, end_ts, window_s)
        if snap_after_end is None or snap_before_start is None:
            return 0.0
        if snap_after_end.funding_rate is None or snap_before_start.funding_rate is None:
            return 0.0
        return snap_after_end.funding_rate - snap_before_start.funding_rate

    def _mark_return(
        self,
        state: _CoinState,
        end_ts: datetime,
        window_s: int,
    ) -> float:
        """5-min mark price return (signed)."""
        snap_after_end, snap_before_start = self._window_edges(state, end_ts, window_s)
        if snap_after_end is None or snap_before_start is None:
            return 0.0
        denom = max(abs(snap_before_start.mark_px), 1e-9)
        return (snap_after_end.mark_px - snap_before_start.mark_px) / denom

    def _window_edges(
        self,
        state: _CoinState,
        end_ts: datetime,
        window_s: int,
    ) -> tuple[_Snapshot | None, _Snapshot | None]:
        """Return both edge snapshots: (latest at-or-before end, latest at-or-before start)."""
        start_ts = end_ts - timedelta(seconds=window_s)
        snap_after_end: _Snapshot | None = None
        snap_before_start: _Snapshot | None = None

        for s in reversed(state.buf):
            if snap_after_end is None and s.ts <= end_ts:
                snap_after_end = s
            if s.ts <= start_ts:
                snap_before_start = s
                break
        return snap_after_end, snap_before_start

    @staticmethod
    def _impact_bps_per_1m(*, abs_return: float, vol_usd: float) -> float:
        """Price impact per $1M volume (bps). Lower values suggest stealth accumulation."""
        if abs_return <= 0 or vol_usd <= 0:
            return 0.0
        return (abs_return * 10_000.0) * (1_000_000.0 / vol_usd)

    def _prune(self, state: _CoinState, now: datetime) -> None:
        """Remove entries from the left (oldest) of the deque that exceed max_history."""
        cutoff = now - timedelta(seconds=self._max_history_s)
        while state.buf and state.buf[0].ts < cutoff:
            state.buf.popleft()
        state.last_prune_ts = now

    def _has_enough_history(self, state: _CoinState, now: datetime) -> bool:
        """Do we have enough history for baseline computation (>= end of baseline window)?"""
        if not state.buf:
            return False
        oldest = state.buf[0].ts
        required_age = self._current_window_s * (self._baseline_chunk_count + 1)
        return (now - oldest).total_seconds() >= required_age

    # ─────────────────────────────────────────────────────────────────
    # For debug / testing
    # ─────────────────────────────────────────────────────────────────
    def buffer_size(self, coin: str) -> int:
        """Current number of snapshots in the coin buffer. For monitoring."""
        s = self._states.get(coin)
        return len(s.buf) if s else 0

    def known_coins(self) -> list[str]:
        """Registered coins (those that have ever received add_events)."""
        return list(self._states.keys())


# =====================================================================
# Backward-compat — skeleton signature
# =====================================================================
def compute_features(
    events: list[NormalizedEvent],
    baseline: dict | None = None,  # noqa: ARG001 — unused in v1
) -> FeatureSnapshot | None:
    """Skeleton-compat entry. One-shot use (test / debug).

    Creates a new HyperliquidFeatures instance internally, adds all events, and
    returns the first coin's snapshot at the most recent ts.
    The real daemon owns a HyperliquidFeatures instance inside HyperliquidChannel.
    """
    if not events:
        return None
    f = HyperliquidFeatures()
    f.add_events(events)
    coins = f.known_coins()
    if not coins:
        return None
    now = max(e.ts_source for e in events) if events else datetime.now(timezone.utc)
    return f.compute_snapshot(coins[0], now)


def _safe_float(v: object) -> float | None:
    """Return float if meta value parses; otherwise None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
