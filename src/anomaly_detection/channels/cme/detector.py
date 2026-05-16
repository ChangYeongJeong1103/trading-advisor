"""
cme/detector.py — CME channel-level tier decision.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5.4.1, §5.2):
  FeatureSnapshot → ChannelSignal.

  v1 P4 baseline pool (Polymarket pattern + % price jump):

    detector "vol_z_v1":
      vol_zscore_5min >= 4   → WATCH
      vol_zscore_5min >= 6   → RISK_OFF
      vol_zscore_5min >= 8 AND price_jump_pct_5min >= 0.005 → EMERGENCY
                              (0.5% in 5min — allows burst lingering)

      ↑ Why EMERGENCY uses 5-min cumulative change (P10 verification lesson):
        When a burst ends in one minute, price_jump_pct_1min drops immediately
        in the next minute (the price settled at the new baseline, so intra-minute
        change is small). But the burst's _impact_ persists as long as
        vol_zscore_5min stays alive over 5 minutes. price_jump_pct_5min is on
        the same time scale so the two signals live together and die together →
        EMERGENCY is naturally sustained for 5 minutes then released.

    detector "price_jump_v1":
      price_jump_pct_1min >= 0.003 → WATCH      (0.3%)
      price_jump_pct_1min >= 0.005 → RISK_OFF   (0.5%)
      ↑ 1min as-is — separately catches fresh price moves (e.g. instant news reaction).

  Final channel tier = max(vol_z_v1, price_jump_v1).
  Score is piecewise linear → [0, 1] (architecture §5.2).

  Walking-skeleton rule. The P9 deep-dive adds proper detectors like options
  sweeps, 0DTE OTM skew, cross-asset correlation, etc.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · "baseline not mature" — features baseline_ready=0 always returns NORMAL.

  · price_jump unit is % (different from Polymarket's absolute [0,1]).

  · "MOCK_SPIKE" reason code is auto-attached — when features mock_spike_count_5min > 0,
    surfaced in reason_codes. Visually distinct from real CME (prevents ops noise).

  · direction is hardcoded NEUTRAL in v1. CME net buy/sell imbalance arrives in P9.

────────────────────────────────────────────────────────────────────────
Architecture: §5.2 (fusion input), §5.4.1 (channel tier), §5.2 (score norm)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...core.schemas import (
    CHANNEL_CME,
    ChannelSignal,
    Direction,
    FeatureSnapshot,
    Tier,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Threshold config
# =====================================================================
@dataclass(frozen=True, slots=True)
class CMEDetectorConfig:
    """v1 P4 baseline thresholds. Moves to thresholds.yaml in P9.

    Z-score thresholds (5min USD volume):
      vol_z_watch    : 4.0
      vol_z_riskoff  : 6.0
      vol_z_emergency: 8.0   (+ price_jump_pct >= 0.005 simultaneously)

    Price-jump (% in 1min):
      pj_watch       : 0.003 (0.3%)
      pj_riskoff     : 0.005 (0.5%)
      pj_emergency   : 0.005 (paired with vol_z 8+)
    """
    vol_z_watch: float = 4.0
    vol_z_riskoff: float = 6.0
    vol_z_emergency: float = 8.0
    pj_watch: float = 0.003
    pj_riskoff: float = 0.005
    pj_emergency: float = 0.005


# =====================================================================
# CMEDetector
# =====================================================================
class CMEDetector:
    """CME channel tier-decision logic (P2 pattern + % price jump)."""

    def __init__(self, config: CMEDetectorConfig | None = None) -> None:
        self.config = config or CMEDetectorConfig()

    def evaluate(self, features: FeatureSnapshot) -> ChannelSignal:
        """FeatureSnapshot → ChannelSignal."""
        f = features.features
        vol_z = float(f.get("vol_zscore_5min", 0.0))
        # pj_1min: fresh price jump (used by price_jump_v1 detector + score).
        pj_1min = float(f.get("price_jump_pct_1min", 0.0))
        # pj_5min: lingering price jump (used by vol_z_v1's EMERGENCY rule).
        # Falls back to 1min when absent — backward compat (old feature instances).
        pj_5min = float(f.get("price_jump_pct_5min", pj_1min))
        baseline_ready = bool(f.get("baseline_ready", 0.0) >= 1.0)
        mock_spike = float(f.get("mock_spike_count_5min", 0.0)) > 0.0

        # ── Warmup ──
        if not baseline_ready:
            return ChannelSignal(
                channel=CHANNEL_CME,
                symbol=features.symbol,
                ts=features.ts,
                score=0.0,
                tier=Tier.NORMAL,
                direction=Direction.NEUTRAL,
                confidence=0.3,
                features_ref=features.id,
                fired_detectors=[],
                reason_codes=["WARMUP_BASELINE_INSUFFICIENT"],
            )

        # vol_z detector's EMERGENCY uses the 5min cumulative change (allows lingering).
        vol_tier = self._vol_z_tier(vol_z, pj_5min)
        # price_jump detector uses the fresh 1min change as-is.
        pj_tier = self._price_jump_tier(pj_1min)
        final_tier = Tier.max_of([vol_tier, pj_tier])

        fired: list[str] = []
        if vol_tier != Tier.NORMAL:
            fired.append("vol_z_v1")
        if pj_tier != Tier.NORMAL:
            fired.append("price_jump_v1")

        reasons: list[str] = []
        if vol_z >= self.config.vol_z_watch:
            reasons.append(f"VOL_Z={vol_z:.1f}")
        if pj_1min >= self.config.pj_watch:
            reasons.append(f"PRICE_JUMP_PCT_1M={pj_1min * 100:.2f}%")
        # 5min jump is only surfaced when EMERGENCY triggers (simplifies visualization).
        if final_tier == Tier.EMERGENCY:
            reasons.append(f"PRICE_JUMP_PCT_5M={pj_5min * 100:.2f}%")
            reasons.append("EMERGENCY_AND_RULE_5M")
        # ── MOCK_SPIKE marker ── prefix to visually distinguish from real production alerts
        if mock_spike:
            reasons.append("MOCK_SPIKE")

        score = self._score(vol_z, pj_1min)

        return ChannelSignal(
            channel=CHANNEL_CME,
            symbol=features.symbol,
            ts=features.ts,
            score=score,
            tier=final_tier,
            direction=Direction.NEUTRAL,
            confidence=1.0,
            features_ref=features.id,
            fired_detectors=fired,
            reason_codes=reasons,
        )

    # ─────────────────────────────────────────────────────────────────
    def _vol_z_tier(self, vol_z: float, pj_5min: float) -> Tier:
        """vol_z + 5min price jump → tier.

        Only EMERGENCY uses 5min cumulative change (catch burst aftershocks).
        RISK_OFF / WATCH are decided by vol_z alone — volume blowing up alone is a risk signal.
        """
        c = self.config
        if vol_z >= c.vol_z_emergency and pj_5min >= c.pj_emergency:
            return Tier.EMERGENCY
        if vol_z >= c.vol_z_riskoff:
            return Tier.RISK_OFF
        if vol_z >= c.vol_z_watch:
            return Tier.WATCH
        return Tier.NORMAL

    def _price_jump_tier(self, pj_1min: float) -> Tier:
        """Pure 1min price-jump detector — catches fresh price moves (volume-independent)."""
        c = self.config
        if pj_1min >= c.pj_riskoff:
            return Tier.RISK_OFF
        if pj_1min >= c.pj_watch:
            return Tier.WATCH
        return Tier.NORMAL

    # ─────────────────────────────────────────────────────────────────
    def _score(self, vol_z: float, pj: float) -> float:
        """Combine vol_z + pj into score [0,1] (same pattern as Polymarket detector)."""
        c = self.config

        # vol_z piecewise linear
        if vol_z <= 0:
            vol_score = 0.0
        elif vol_z < c.vol_z_watch:
            vol_score = 0.5 * (vol_z / c.vol_z_watch)
        elif vol_z < c.vol_z_riskoff:
            t = (vol_z - c.vol_z_watch) / (c.vol_z_riskoff - c.vol_z_watch)
            vol_score = 0.50 + 0.20 * t
        elif vol_z < c.vol_z_emergency:
            t = (vol_z - c.vol_z_riskoff) / (c.vol_z_emergency - c.vol_z_riskoff)
            vol_score = 0.70 + 0.15 * t
        else:
            extra = max(0.0, vol_z - c.vol_z_emergency)
            vol_score = 0.85 + 0.10 * (extra / (extra + 2.0))

        # pj piecewise linear (in %)
        if pj <= 0:
            pj_score = 0.0
        elif pj < c.pj_watch:
            pj_score = 0.30 * (pj / c.pj_watch)
        elif pj < c.pj_riskoff:
            t = (pj - c.pj_watch) / (c.pj_riskoff - c.pj_watch)
            pj_score = 0.30 + 0.25 * t
        else:
            extra = max(0.0, pj - c.pj_riskoff)
            # Beyond 0.5% saturates quickly
            pj_score = 0.55 + 0.25 * (extra / (extra + 0.005))

        return max(0.0, min(1.0, max(vol_score, pj_score)))
