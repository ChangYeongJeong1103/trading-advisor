"""
cme/cme_insider_v1.py — CME insider-flow detector (P12-B).

────────────────────────────────────────────────────────────────────────
Responsibilities:
  Receives the FeatureSnapshot produced by CMEFeatures and emits a
  ChannelSignal for insider-style bursts. Runs alongside the existing
  vol_z_v1 / price_jump_v1 detectors and contributes to channel tier = max(...).

  v1 spec (verified against P12 historical events — 3/23, 4/17, 4/21):

    4 conditions per bucket:
      C1. Size              (notional ≥ symbol-specific threshold)
      C2. Range             (|max-min|/mid ≥ 0.30%) — high-low band, unsigned
      C3. Trade count spike (count ≥ max(floor, baseline_mean × 5))
      C4. Persistence       (previous bucket notional ≥ size_thr × 0.5)

    Tier:
      n ≥ 3 → EMERGENCY
      n ≥ 2 → RISK_OFF
      n ≥ 1 → WATCH
      else  → NORMAL

    Multi-trigger (P12-D extension):
      BZ/CL: evaluate 1min / 2min / 5min buckets in parallel (windows treated equally),
        final_tier = max(...). Instant bursts are caught by 1min; bucket-boundary
        misalignment by 2min; and the "gradual accumulation" pattern (4-5 min
        accumulation like 5/7 BZ) by 5min.
      ES/GC: only the 5min vol_z bucket (needs time-of-day normalization).

      Winner selection (for reasons / direction annotation) — window length has
      no priority; content-driven:
        1) bucket with the most n_conditions
        2) tie → bucket with larger |signed_change|
        3) tie → bucket with larger size_value

────────────────────────────────────────────────────────────────────────
Per-symbol size mode (user-decided lock 2026-04-23, P12-D adds 5min):

  · BZ, CL → size_mode=absolute_musd   (gold-standard, time-of-day-independent)
       BZ : 1/2min size_thr=18.0$M, 5min size_thr=30.0$M
       CL : 1/2min size_thr=100.0$M, 5min size_thr=100.0$M
  · ES, GC → size_mode=vol_z_5min      (z-score to normalize across time-of-day)
       ES size_thr=6.0   (sigma)
       GC size_thr=6.0   (sigma) — insufficient historical data, P12-C verification pending

  ↑ ES / GC evaluate only the single vol_z_5min bucket (1min/2min vol_z have
    baselines too short to be stable).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · "baseline not mature" — if features.baseline_ready=0, always NORMAL.
  · "symbol unknown" — symbols not in INSIDER_THRESHOLDS return NORMAL +
    UNKNOWN_SYMBOL reason (silent).
  · score is piecewise — proportional to n_conditions (0/1/2/3/4 → 0/0.4/0.6/0.8/0.95).
    Used for score comparison in the fusion engine. Tier matters more.
  · direction (P12-D): compare (last - first)/first of the winner bucket against
        the dead-zone. Default dead-zone = range_thr_pct / 2 (BZ 0.30% → ±0.15%).
        Since the winner is content-driven (no window-length priority), the most
        decisive bucket is naturally selected, so the 5min bucket from the
        "gradual accumulation" pattern (5/7 BZ) wins on |signed| over 1/2min.
        Avoids the NEUTRAL hardcode of v1 (P12-B) — range alone cannot indicate
        direction → use a signed metric.

  · aggressor / absorption (P12-D): compute imbalance from per-aggressor-side
        (Ask=BUY / Bid=SELL) USD totals.
        AGGR_IMB_<bucket> = (buy_vol - sell_vol) / total, [-1, +1].
    If direction and the imbalance sign are *opposite* and |imbalance| ≥ 0.20,
    it is the "ABSORPTION" signature — price moves one way but aggressors dominate
    the other side = someone is absorbing the flow with a large hidden bid/ask.
    Iceberg pattern.

────────────────────────────────────────────────────────────────────────
Architecture: §5.4.1 (channel tier), §5.2 (score)
"""

from __future__ import annotations

# ── stdlib ───────────────────────────────────────────────────────────
import logging
from dataclasses import dataclass, field
from typing import Literal

# ── local ─────────────────────────────────────────────────────────────
from ...core.schemas import (
    CHANNEL_CME,
    ChannelSignal,
    Direction,
    FeatureSnapshot,
    Tier,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Per-symbol threshold spec
# =====================================================================
@dataclass(frozen=True, slots=True)
class InsiderSymbolThresholds:
    """insider_v1 threshold set for one symbol.

    Attributes:
        size_mode: "absolute_musd" → compared against notional_musd_<freq>.
                   "vol_z_5min"   → compared against vol_zscore_5min (single 5min eval).
        size_thr:  1min/2min threshold matching size_mode ($M or sigma).
        size_thr_5min: (absolute_musd only) separate threshold for the 5min bucket ($M).
                       None disables 5min bucket evaluation → fall back to
                       1min/2min only. Enabled for BZ/CL in P12-D.
        range_thr_pct: C2 — threshold on 1/2/5min high-low range (%, unsigned).
                       e.g. 0.30 = 0.30% (max-min)/mid.
        count_mult:   C3 — multiplier on baseline trade count (e.g. 5.0).
        count_floor_1min: C3 — absolute floor for 1min bucket (protects small symbols).
        count_floor_2min: C3 — absolute floor for 2min bucket.
        count_floor_5min: C3 — absolute floor for 5min bucket (added in P12-D).
        persist_ratio:    C4 — multiplier on the previous bucket's size_thr (e.g. 0.5).
    """
    size_mode: Literal["absolute_musd", "vol_z_5min"]
    size_thr: float
    size_thr_5min: float | None = None
    range_thr_pct: float = 0.30
    count_mult: float = 5.0
    count_floor_1min: int = 50
    count_floor_2min: int = 80
    count_floor_5min: int = 200
    persist_ratio: float = 0.5
    # P12-D addition — direction (UP/DOWN/NEUTRAL) dead-zone (absolute %).
    # If the winning bucket's signed price change (last - first)/first is
    #   ≥ +dead_zone → UP
    #   ≤ -dead_zone → DOWN
    #   else         → NEUTRAL
    # Default = range_thr_pct / 2 = 0.15%. That is, the direction is annotated only
    # when at least half the range nets to one side; otherwise it is considered
    # two-sided oscillation and labelled NEUTRAL.
    direction_dead_zone_pct: float = 0.15


# v1 default thresholds — locked after P12 verification.
#
# BZ/CL — absolute notional mode, verified by 3 events × 3-day baseline replay.
# ES    — vol_z_5min mode, conservatively set to 6.0σ. The maximum threshold
#         that still captures the ES burst peak vol_z of the 3 events
#         (3/23: 60, 4/17: 17, 4/21: 7.5) — at 8σ we would miss the 7.5σ burst
#         on 4/21. Raised from 4σ → 6σ for false-positive headroom in normal markets.
# GC    — insufficient historical data (0 GC samples across the 3 events) → spec
#         cannot be verified. Excluded from the detector for now (UNKNOWN_SYMBOL
#         fallback → NORMAL). To be added in P12-C after baseline data is fetched.
DEFAULT_INSIDER_THRESHOLDS: dict[str, InsiderSymbolThresholds] = {
    # P12-D: add 5min bucket for BZ/CL — catches the "gradual accumulation" pattern.
    # 5min size_thr comes from the P12-A prior analysis (3/23, 4/17, 4/21 events):
    #   BZ 5min peak ≥ $30M, CL 5min peak ≥ $100M.
    "BZ": InsiderSymbolThresholds(
        size_mode="absolute_musd", size_thr=18.0, size_thr_5min=30.0,
    ),
    "CL": InsiderSymbolThresholds(
        size_mode="absolute_musd", size_thr=100.0, size_thr_5min=100.0,
    ),
    "ES": InsiderSymbolThresholds(
        size_mode="vol_z_5min", size_thr=6.0,
    ),
    # "GC": disabled until P12-C baseline verification is complete.
}


# =====================================================================
# Bucket evaluation result (internal)
# =====================================================================
@dataclass(frozen=True, slots=True)
class _BucketEval:
    """Per-bucket (1min / 2min / 5min) condition evaluation result."""
    label: str                # "1min", "2min", "5min"
    c1_size: bool
    c2_range: bool            # high-low band ≥ range_thr_pct
    c3_count: bool
    c4_persist: bool
    n: int
    tier: Tier
    # raw values for debugging.
    size_value: float
    range_pct: float          # (max - min) / mid · 100, unsigned
    count_value: float
    count_threshold: float
    prev_size_value: float


# =====================================================================
# CMEInsiderV1Detector
# =====================================================================
class CMEInsiderV1Detector:
    """CME channel-level insider-flow detector (P12-B v1).

    Runs in parallel with the existing CMEDetector and emits a ChannelSignal.
    CMEChannel merges both detectors' ChannelSignals using max-tier-wins.

    Attributes:
        thresholds: symbol → InsiderSymbolThresholds. Unmapped symbols return NORMAL.
    """

    DETECTOR_NAME = "cme_insider_v1"

    # P12-D — absorption signature threshold: when aggressor_imbalance has the
    # opposite sign to the price direction and |imbalance| ≥ this, annotate
    # "iceberg accumulation".
    # Example: direction=UP & imb<=-0.20 → price rises but sell-aggressor
    # dominates = someone is absorbing buys (insider signature).
    _ABSORPTION_IMB_THRESHOLD: float = 0.20

    def __init__(
        self,
        thresholds: dict[str, InsiderSymbolThresholds] | None = None,
    ) -> None:
        self.thresholds = thresholds or DEFAULT_INSIDER_THRESHOLDS

    # ─────────────────────────────────────────────────────────────────
    # Public API — called by fusion loop / replay reporter.
    # ─────────────────────────────────────────────────────────────────
    def evaluate(self, snapshot: FeatureSnapshot) -> ChannelSignal:
        """FeatureSnapshot → ChannelSignal."""
        cfg = self.thresholds.get(snapshot.symbol)
        if cfg is None:
            return self._normal_signal(
                snapshot, reasons=["UNKNOWN_SYMBOL_INSIDER_V1"],
            )

        f = snapshot.features
        baseline_ready = bool(f.get("baseline_ready", 0.0) >= 1.0)
        if not baseline_ready:
            return self._normal_signal(
                snapshot, reasons=["WARMUP_BASELINE_INSUFFICIENT"],
            )

        # ── BZ/CL: evaluate 1min, 2min, 5min in parallel (all equal); final tier = max
        # ── ES/GC: single 5min vol_z bucket (rough — verify in P12-C) ─
        if cfg.size_mode == "absolute_musd":
            evals = [
                self._eval_bucket(cfg, f, label="1min"),
                self._eval_bucket(cfg, f, label="2min"),
            ]
            # 5min bucket — only for symbols (BZ/CL) where size_thr_5min is set.
            if cfg.size_thr_5min is not None:
                evals.append(self._eval_bucket(cfg, f, label="5min"))
        else:
            evals = [self._eval_5min_volz(cfg, f)]

        # Final tier = max(...)
        final_tier = Tier.max_of([e.tier for e in evals])

        # Pick the winner among buckets that reached the final tier.
        # No window-length priority — all windows are treated equally.
        # Selection criteria (content-driven):
        #   1) bucket with more n_conditions (more satisfied = more certain)
        #   2) tie → bucket with larger |signed_change| (clearer direction)
        #   3) tie → bucket with larger size_value (larger executed size)
        candidates = [e for e in evals if e.tier == final_tier]
        winner = max(
            candidates,
            key=lambda e: (
                e.n,
                abs(self._signed_change_pct(f, e.label)),
                e.size_value,
            ),
        )

        reasons = self._build_reasons(cfg, winner)
        # mock_spike marker (to visually distinguish from production alerts) — features
        # only exposes the 5min count so pass it through as-is.
        if float(f.get("mock_spike_count_5min", 0.0)) > 0.0:
            reasons.append("MOCK_SPIKE")

        score = self._score(winner.n)

        fired: list[str] = []
        if final_tier != Tier.NORMAL:
            fired.append(self.DETECTOR_NAME)

        # P12-D direction — use only the winner bucket's signed change.
        # If a longer window of the same tier exists, the winner selection has
        # already chosen it, so no fallback is needed (the "gradual accumulation"
        # pattern is naturally captured with 5min as winner).
        direction = self._resolve_direction(cfg, f, winner.label)

        # Always add NET_CHANGE to reasons (audit-friendly even on NEUTRAL).
        # Lets you verify "why NEUTRAL" from email/X just by reading the alert.
        if final_tier != Tier.NORMAL:
            primary_pct = self._signed_change_pct(f, winner.label)
            reasons.append(f"NET_CHANGE_{winner.label}={primary_pct:+.2f}%")

            # P12-D — aggressor imbalance + absorption signature.
            # Always annotate the winning bucket's buy/sell aggressor breakdown.
            buy_m = (
                float(f.get(f"aggressor_buy_vol_{winner.label}", 0.0)) / 1e6
            )
            sell_m = (
                float(f.get(f"aggressor_sell_vol_{winner.label}", 0.0)) / 1e6
            )
            agg_imb = float(f.get(f"aggressor_imbalance_{winner.label}", 0.0))
            if buy_m + sell_m > 0:
                reasons.append(
                    f"AGGR_IMB_{winner.label}={agg_imb:+.2f} "
                    f"(buy=${buy_m:.1f}M sell=${sell_m:.1f}M)"
                )

            # ABSORPTION pattern — direction and aggressor sign mismatch:
            #   price UP but sell-aggressor dominates → iceberg buying (a strong
            #     buyer absorbs retail/algo selling with a hidden large bid).
            #   price DOWN but buy-aggressor dominates → iceberg selling.
            # The strongest signature of a "gradual accumulation" pattern like 5/7 BZ.
            if direction == Direction.UP and agg_imb <= -self._ABSORPTION_IMB_THRESHOLD:
                reasons.append("ABSORPTION_BUYING")
            elif direction == Direction.DOWN and agg_imb >= self._ABSORPTION_IMB_THRESHOLD:
                reasons.append("ABSORPTION_SELLING")

        return ChannelSignal(
            channel=CHANNEL_CME,
            symbol=snapshot.symbol,
            ts=snapshot.ts,
            score=score,
            tier=final_tier,
            direction=direction,
            confidence=1.0,
            features_ref=snapshot.id,
            fired_detectors=fired,
            reason_codes=reasons,
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal — single bucket evaluation (BZ/CL absolute mode)
    # ─────────────────────────────────────────────────────────────────
    def _eval_bucket(
        self,
        cfg: InsiderSymbolThresholds,
        f: dict[str, float],
        label: str,
    ) -> _BucketEval:
        """absolute_musd mode — evaluate 4 conditions for the 1min/2min/5min bucket.

        size_thr is split per label (1/2min = cfg.size_thr, 5min = cfg.size_thr_5min).
        count_floor is also split per label.
        """
        size = float(f.get(f"notional_musd_{label}", 0.0))
        prev_size = float(f.get(f"notional_musd_prev_{label}", 0.0))
        # price_jump_pct_<label> = (max - min) / mid ratio (0~1). Convert to %.
        # The internal feature key keeps the legacy name (price_jump_pct_*) —
        # semantically it is the high-low range.
        range_pct = float(f.get(f"price_jump_pct_{label}", 0.0)) * 100.0
        count = float(f.get(f"trade_count_{label}", 0.0))
        cnt_baseline = float(f.get(f"trade_count_baseline_{label}", 0.0))

        # P12-D — per-label size_thr / count_floor mapping.
        if label == "5min":
            # When size_thr_5min is None, fall back to 0.0 (callers skip the 5min
            # eval entirely in that case → control never reaches here).
            size_thr_for_label = (
                cfg.size_thr_5min if cfg.size_thr_5min is not None else 0.0
            )
            floor = cfg.count_floor_5min
        elif label == "2min":
            size_thr_for_label = cfg.size_thr
            floor = cfg.count_floor_2min
        else:  # "1min"
            size_thr_for_label = cfg.size_thr
            floor = cfg.count_floor_1min

        cnt_threshold = max(float(floor), cnt_baseline * cfg.count_mult)

        c1 = size >= size_thr_for_label
        c2 = range_pct >= cfg.range_thr_pct
        c3 = count >= cnt_threshold
        c4 = prev_size >= size_thr_for_label * cfg.persist_ratio
        n = int(c1) + int(c2) + int(c3) + int(c4)

        return _BucketEval(
            label=label,
            c1_size=c1, c2_range=c2, c3_count=c3, c4_persist=c4,
            n=n, tier=self._tier_from_n(n),
            size_value=size, range_pct=range_pct,
            count_value=count, count_threshold=cnt_threshold,
            prev_size_value=prev_size,
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal — 5min vol_z mode for ES/GC (rough v1)
    # ─────────────────────────────────────────────────────────────────
    def _eval_5min_volz(
        self, cfg: InsiderSymbolThresholds, f: dict[str, float],
    ) -> _BucketEval:
        """ES/GC: evaluate using only vol_zscore_5min + price_jump_pct_5min (= 5min range).

        Why:
            For ES/GC the time-of-day volume gap is too large for absolute size
            thresholds to be meaningful. We reuse vol_zscore_5min (5min vol z-score,
            30min baseline) as the size proxy. 1min/2min vol_z have baselines too
            short to be stable.

        Trade-off:
            5min granularity → latency 4 minutes worse than BZ/CL's 1min.
            ES/GC lack historical event data; formal verification arrives in P12-C.
        """
        size_z = float(f.get("vol_zscore_5min", 0.0))
        # It would be nice to have a `prev` (previous 5min vol_z) for ES/GC as well,
        # but it's not currently exposed in features → C4 is always False for now.
        # Add it in P12-C.
        # high-low range uses the 5min window.
        range_pct = float(f.get("price_jump_pct_5min", 0.0)) * 100.0
        # ES/GC do not expose trade_count baselines either — C3 is also False.
        # → effectively a 2-condition (size, range) evaluation. tier:
        #     size && range → RISK_OFF
        #     size only     → WATCH
        #     range only    → WATCH
        c1 = size_z >= cfg.size_thr
        c2 = range_pct >= cfg.range_thr_pct
        n = int(c1) + int(c2)  # 0~2 only.
        # ES/GC v1 tier mapping (rough):
        if n >= 2:
            tier = Tier.RISK_OFF
        elif n >= 1:
            tier = Tier.WATCH
        else:
            tier = Tier.NORMAL
        return _BucketEval(
            label="5min",
            c1_size=c1, c2_range=c2, c3_count=False, c4_persist=False,
            n=n, tier=tier,
            size_value=size_z, range_pct=range_pct,
            count_value=0.0, count_threshold=0.0, prev_size_value=0.0,
        )

    # ─────────────────────────────────────────────────────────────────
    # Tier / score helpers
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _tier_from_n(n: int) -> Tier:
        """4-condition n → tier (BZ/CL rule)."""
        if n >= 3:
            return Tier.EMERGENCY
        if n >= 2:
            return Tier.RISK_OFF
        if n >= 1:
            return Tier.WATCH
        return Tier.NORMAL

    @staticmethod
    def _score(n: int) -> float:
        """n_conditions → [0, 1] score. piecewise — for fusion comparison."""
        # 0/1/2/3/4 → 0/0.40/0.60/0.80/0.95
        table = {0: 0.0, 1: 0.40, 2: 0.60, 3: 0.80, 4: 0.95}
        return table.get(n, 0.0)

    @staticmethod
    def _signed_change_pct(f: dict[str, float], label: str) -> float:
        """Return the signed % change matching the winning bucket label.

        Returns:
            float: in % (e.g. +0.49 = +0.49% UP).
                   Returns 0.0 when the feature is absent (legacy snapshot compat).
        """
        # features.py stores it as a 0~1 ratio → convert to %.
        return float(f.get(f"price_change_pct_signed_{label}", 0.0)) * 100.0

    def _resolve_direction(
        self,
        cfg: InsiderSymbolThresholds,
        f: dict[str, float],
        label: str,
    ) -> Direction:
        """Direction from the winning bucket's signed change and dead-zone.

        Since winner selection already picks the longest bucket among ties of the
        same tier ("gradual accumulation" naturally maps to a 5min winner), no
        fallback is required.

        Args:
            cfg: threshold spec for the symbol.
            f: features dict.
            label: "1min" / "2min" / "5min" — label of the winning bucket.

        Returns:
            Direction.UP / DOWN / NEUTRAL.
        """
        signed_pct = self._signed_change_pct(f, label)
        if signed_pct >= cfg.direction_dead_zone_pct:
            return Direction.UP
        if signed_pct <= -cfg.direction_dead_zone_pct:
            return Direction.DOWN
        return Direction.NEUTRAL

    @staticmethod
    def _build_reasons(
        cfg: InsiderSymbolThresholds, winner: _BucketEval,
    ) -> list[str]:
        """Render the winning bucket's conditions into human-readable reason_codes."""
        reasons: list[str] = [f"INSIDER_V1_BUCKET={winner.label}"]
        if winner.c1_size:
            unit = "$M" if cfg.size_mode == "absolute_musd" else "σ"
            reasons.append(f"C1_SIZE={winner.size_value:.1f}{unit}")
        if winner.c2_range:
            # high-low band — unsigned. Annotate the window (e.g. 5min high-low).
            reasons.append(
                f"C2_RANGE={winner.range_pct:.2f}% ({winner.label} high-low)"
            )
        if winner.c3_count:
            reasons.append(
                f"C3_COUNT={winner.count_value:.0f}≥"
                f"{winner.count_threshold:.0f}"
            )
        if winner.c4_persist:
            reasons.append(f"C4_PERSIST_PREV={winner.prev_size_value:.1f}")
        return reasons

    def _normal_signal(
        self, snapshot: FeatureSnapshot, reasons: list[str],
    ) -> ChannelSignal:
        """Helper for NORMAL signals (warmup / unknown symbol, etc.)."""
        return ChannelSignal(
            channel=CHANNEL_CME,
            symbol=snapshot.symbol,
            ts=snapshot.ts,
            score=0.0,
            tier=Tier.NORMAL,
            direction=Direction.NEUTRAL,
            confidence=0.3,
            features_ref=snapshot.id,
            fired_detectors=[],
            reason_codes=reasons,
        )


__all__ = [
    "CMEInsiderV1Detector",
    "DEFAULT_INSIDER_THRESHOLDS",
    "InsiderSymbolThresholds",
]
