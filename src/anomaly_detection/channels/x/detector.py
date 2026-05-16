"""
x/detector.py — X channel-level tier decision.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5.4.1, plan §8 P5):

  FeatureSnapshot → ChannelSignal.

  v1 P5 baseline rules (plan §8 P5):

    n_unique_accounts_15min == 1                            → WATCH
    n_unique_accounts_15min >= 2                            → RISK_OFF
    n_unique_accounts_15min >= 3 AND magnitude_count >= 1   → EMERGENCY

  Additional rules:
    - Demote one tier when sum_account_weight is below a floor (low-credibility correction)
    - Compare direction_buy_count / direction_sell_count to decide Direction
      (X mentions are often magnitude/keyword-focused so direction is frequently NEUTRAL)

  P9 deep-dive: NER, account historical accuracy, post sentiment, etc.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · "MOCK_SPIKE" reason marker auto-attached — distinguishes from production noise.

  · X channel has no baseline warmup (features fix baseline_ready=1.0).

  · score maps a weighted sum of (unique_accounts, magnitude, weight) to [0,1].

────────────────────────────────────────────────────────────────────────
Plan: §8 P5 (v0 detector), architecture §5.4.1 channel tier
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...core.schemas import (
    CHANNEL_X,
    ChannelSignal,
    Direction,
    FeatureSnapshot,
    Tier,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class XDetectorConfig:
    """v1 P5 baseline thresholds. Moves to thresholds.yaml in P9."""
    accounts_watch: int = 1
    accounts_riskoff: int = 2
    accounts_emergency: int = 3
    magnitude_required_for_emergency: int = 1
    # weight floor: demote one tier if sum_weight is below this (low-cred correction)
    weight_floor_riskoff: float = 1.5      # e.g. 0.9 + 0.85 combined
    weight_floor_emergency: float = 2.4    # e.g. 0.9 × 3 - margin


class XDetector:
    """X channel tier-decision logic (P5 baseline)."""

    def __init__(self, config: XDetectorConfig | None = None) -> None:
        self.config = config or XDetectorConfig()

    def evaluate(self, features: FeatureSnapshot) -> ChannelSignal:
        f = features.features
        n_acc = int(f.get("n_unique_accounts_15min", 0.0))
        sum_w = float(f.get("sum_account_weight_15min", 0.0))
        mag_count = int(f.get("magnitude_count_15min", 0.0))
        buy_count = int(f.get("direction_buy_count_15min", 0.0))
        sell_count = int(f.get("direction_sell_count_15min", 0.0))
        mock_spike = float(f.get("mock_spike_count_15min", 0.0)) > 0.0

        # ── tier decision ──
        c = self.config
        if n_acc >= c.accounts_emergency and mag_count >= c.magnitude_required_for_emergency:
            tier = Tier.EMERGENCY
        elif n_acc >= c.accounts_riskoff:
            tier = Tier.RISK_OFF
        elif n_acc >= c.accounts_watch:
            tier = Tier.WATCH
        else:
            tier = Tier.NORMAL

        # ── weight floor correction (low-credibility demote by one tier) ──
        if tier == Tier.EMERGENCY and sum_w < c.weight_floor_emergency:
            tier = Tier.RISK_OFF
            low_cred_demoted = True
        elif tier == Tier.RISK_OFF and sum_w < c.weight_floor_riskoff:
            tier = Tier.WATCH
            low_cred_demoted = True
        else:
            low_cred_demoted = False

        # ── direction ──
        direction = Direction.NEUTRAL
        if buy_count > sell_count + 1:
            direction = Direction.UP
        elif sell_count > buy_count + 1:
            direction = Direction.DOWN

        # ── reasons & fired ──
        fired: list[str] = []
        if tier != Tier.NORMAL:
            fired.append("x_corroboration_v1")
        if mag_count > 0 and tier == Tier.EMERGENCY:
            fired.append("x_magnitude_v1")

        reasons: list[str] = []
        if n_acc > 0:
            reasons.append(f"X_ACCOUNTS={n_acc}")
        if mag_count > 0:
            reasons.append(f"X_MAGNITUDE={mag_count}")
        if sum_w > 0:
            reasons.append(f"X_WEIGHT={sum_w:.2f}")
        if low_cred_demoted:
            reasons.append("LOW_CREDIBILITY_DEMOTED")
        if mock_spike:
            reasons.append("MOCK_SPIKE")

        # ── score ──
        score = self._score(n_acc, sum_w, mag_count)

        # ── confidence ── (slightly lower on low-cred demote)
        confidence = 0.7 if low_cred_demoted else 1.0

        return ChannelSignal(
            channel=CHANNEL_X,
            symbol=features.symbol,
            ts=features.ts,
            score=score,
            tier=tier,
            direction=direction,
            confidence=confidence,
            features_ref=features.id,
            fired_detectors=fired,
            reason_codes=reasons,
        )

    # ─────────────────────────────────────────────────────────────────
    def _score(self, n_acc: int, sum_w: float, mag_count: int) -> float:
        """X score [0,1]. Piecewise sum of (acc count + weight + magnitude)."""
        if n_acc <= 0:
            return 0.0
        # base: account count → [0, 0.6]
        if n_acc == 1:
            base = 0.30
        elif n_acc == 2:
            base = 0.50
        else:
            extra = n_acc - 3
            base = 0.65 + 0.05 * (extra / (extra + 1.0))     # saturate

        # weight bonus: larger sum_w → +0.0~0.20
        weight_bonus = 0.20 * min(1.0, sum_w / 3.0)

        # magnitude bonus: +0.10 when mag_count > 0
        mag_bonus = 0.10 if mag_count > 0 else 0.0

        return max(0.0, min(1.0, base + weight_bonus + mag_bonus))
