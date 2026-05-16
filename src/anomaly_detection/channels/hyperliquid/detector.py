"""
hyperliquid/detector.py — Hyperliquid channel-level tier decision. v4 (P9.2.P3).

Core goals:
  Reduce false "volume spike = insider" calls, and distinguish panic flow /
  insider-like flow / new-whale flow / distributed-cluster flow.

v4 detector pool:
  1) vol_z_v1       : volume anomaly (alone allowed only up to RISK_OFF)
  2) insider_v1     : combination of vol + OI growth + funding skew + low impact
  3) panic_filter_v1: demote when vol spike + OI decline/flat + sharp drop + high impact
  4) new_whale_v1   : 5-min cumulative notional of wallets first seen within 24h (P9.2.P2)
  5) cluster_v1     : distributed cluster where fresh wallets enter the same coin × same side
                      × similar price band, split up (P9.2.P3 — D5 distributed betting)
                      The channel queries wallet_store and fills features dict keys:
                        cluster_top_sum_notional_usd  : sum notional of the top cluster
                        cluster_top_n_wallets         : distinct wallet count of top cluster
                        cluster_warmup_ready          : cold-start guard (=new_whale_warmup_ready)
                        cluster_top_side_code         : +1.0=B / -1.0=A / 0.0=unknown

Design points:
  - Still NORMAL if baseline is immature (warmup protection)
  - EMERGENCY only allowed with multiple corroborating signals (prevents single-detector hyperreactivity)
    Exception: new_whale_v1 / cluster_v1 EMERGENCY may bypass since they are strong signals on their own.
  - Direction priority: strong cluster > strong new_whale > price_return > funding/OI
  - panic_filter does not demote when new_whale_v1 or cluster_v1 is strong
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...core.schemas import (
    CHANNEL_HYPERLIQUID,
    ChannelSignal,
    Direction,
    FeatureSnapshot,
    Tier,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Threshold config — tune in one place (until D11 thresholds.yaml lands)
# =====================================================================
@dataclass(frozen=True, slots=True)
class HyperliquidDetectorConfig:
    """P9.2.P1 thresholds.

    For conservative operation we forbid vol-only EMERGENCY,
    and decide insider/panic via combinations of conditions.
    """
    vol_z_watch: float = 4.0
    vol_z_riskoff: float = 6.0
    vol_z_emergency: float = 9.0

    # vol-only detector is capped at RISK_OFF
    allow_vol_only_emergency: bool = False

    # insider_v1 condition thresholds
    insider_vol_z_min: float = 6.0
    insider_oi_delta_usd_min: float = 2_000_000.0
    insider_oi_ratio_min: float = 0.01
    insider_funding_abs_min: float = 0.00002
    insider_funding_delta_min: float = 0.00001
    insider_price_return_abs_max: float = 0.02
    insider_impact_ratio_max: float = 0.90
    insider_watch_min_conditions: int = 2
    # P11(b).G1 (user-decided lock 2026-04-22):
    # 3 → 4. Only when all 4 conditions (vol + oi + funding + stealth) hit
    # simultaneously does insider_v1 reach EMERGENCY. On huge markets like
    # BTC/ETH, vol+oi+stealth coincide routinely (3 cond → 60 EMERGENCY noise
    # over 3 days), so requiring funding to move too captures only the real
    # insider pattern. Expected to remove ~91% of current noise (vol+insider mix).
    insider_emergency_min_conditions: int = 4

    # panic_filter_v1 thresholds
    panic_vol_z_min: float = 4.0
    panic_price_drop_5min: float = -0.01
    panic_oi_delta_usd_max: float = 0.0
    panic_impact_ratio_min: float = 1.30

    # EMERGENCY guard
    # P11(b).F2-strict (user-decided lock 2026-04-22):
    # 2 → 3. system EMERGENCY only when at least 3 of the 4 detectors
    # (vol_z, insider, new_whale, cluster) fire at RISK_OFF+ simultaneously.
    # User intuition — "real insider events tend to fire many detectors at once".
    min_riskoff_detectors_for_emergency: int = 3

    # ── new_whale_v1 (P9.2.P2 — D2: New whale emergence) ──
    # Tier mapping by 5-min cumulative taker notional (USD) of fresh wallets.
    # User-approved thresholds (Q3=$2M / $10M / $25M).
    new_whale_watch_usd: float = 2_000_000.0
    new_whale_riskoff_usd: float = 10_000_000.0
    new_whale_emergency_usd: float = 25_000_000.0
    # P11(b).F2-strict (user-decided lock 2026-04-22):
    # True → False. new_whale standalone EMERGENCY must also pass the EMERGENCY
    # guard (3 detectors RISK_OFF+). Accidental spikes from a single detector
    # (e.g. a large wallet making normal institutional trades) auto-demote to RISK_OFF.
    new_whale_bypass_emergency_guard: bool = False

    # ── cluster_v1 (P9.2.P3 — D5: Distributed betting) ──
    # Cluster candidate (n_wallets, sum_notional_usd) → tier via AND.
    # User-approved thresholds: 3w/$5M (WATCH), 5w/$15M (RISK_OFF), 8w/$30M (EMERGENCY).
    cluster_watch_n_wallets: int = 3
    cluster_watch_sum_usd: float = 5_000_000.0
    cluster_riskoff_n_wallets: int = 5
    cluster_riskoff_sum_usd: float = 15_000_000.0
    cluster_emergency_n_wallets: int = 8
    cluster_emergency_sum_usd: float = 30_000_000.0
    # P11(b).F2-strict (user-decided lock 2026-04-22):
    # True → False. cluster standalone EMERGENCY also subject to EMERGENCY guard.
    cluster_bypass_emergency_guard: bool = False


# =====================================================================
# HyperliquidDetector
# =====================================================================
class HyperliquidDetector:
    """Hyperliquid channel tier-decision logic (panic vs insider split v2)."""

    def __init__(self, config: HyperliquidDetectorConfig | None = None) -> None:
        self.config = config or HyperliquidDetectorConfig()

    # ─────────────────────────────────────────────────────────────────
    # Main — called once per cycle
    # ─────────────────────────────────────────────────────────────────
    def evaluate(self, features: FeatureSnapshot) -> ChannelSignal:
        """FeatureSnapshot → ChannelSignal.

        Args:
            features: HyperliquidFeatures.compute_snapshot() result.

        Returns:
            ChannelSignal: always returned (may be NORMAL). features_ref carries features.id.
        """
        f = features.features
        vol_z = float(f.get("vol_zscore_5min", 0.0))
        baseline_ready = bool(f.get("baseline_ready", 0.0) >= 1.0)
        oi_delta_usd = float(f.get("oi_delta_usd_5min", 0.0))
        oi_delta_ratio = float(f.get("oi_delta_ratio_5min", 0.0))
        funding_rate = float(f.get("funding_rate", 0.0))
        funding_delta_5min = float(f.get("funding_delta_5min", 0.0))
        price_return_5min = float(f.get("price_return_5min", 0.0))
        price_return_abs_5min = float(f.get("price_return_abs_5min", 0.0))
        impact_ratio = float(f.get("impact_ratio_vs_baseline", 0.0))

        # P9.2.P2 — keys the channel fills by querying wallet_store.
        # Missing or zero when warmup is incomplete or wallet_store is absent.
        new_whale_max_cum5m_usd = float(f.get("new_whale_max_cum5m_usd", 0.0))
        new_whale_count_24h = int(f.get("new_whale_count_24h", 0))
        new_whale_warmup_ready = bool(f.get("new_whale_warmup_ready", 0.0) >= 1.0)
        # last_side encoding: features dict is float-only so receive as a number.
        #   +1.0 = "B" (taker buy)   /  -1.0 = "A" (taker sell)  /  0.0 = unknown
        new_whale_last_side_raw = float(f.get("new_whale_last_side_code", 0.0))
        if new_whale_last_side_raw >= 0.5:
            new_whale_last_side = "B"
        elif new_whale_last_side_raw <= -0.5:
            new_whale_last_side = "A"
        else:
            new_whale_last_side = ""

        # P9.2.P3 — cluster_v1 inputs (channel fills via wallet_store cluster query).
        cluster_top_sum_usd = float(f.get("cluster_top_sum_notional_usd", 0.0))
        cluster_top_n_wallets = int(f.get("cluster_top_n_wallets", 0))
        cluster_warmup_ready = bool(f.get("cluster_warmup_ready", 0.0) >= 1.0)
        cluster_top_side_raw = float(f.get("cluster_top_side_code", 0.0))
        if cluster_top_side_raw >= 0.5:
            cluster_top_side = "B"
        elif cluster_top_side_raw <= -0.5:
            cluster_top_side = "A"
        else:
            cluster_top_side = ""

        # ── Warmup: NORMAL if baseline is immature ──
        if not baseline_ready:
            return ChannelSignal(
                channel=CHANNEL_HYPERLIQUID,
                symbol=features.symbol,
                ts=features.ts,
                score=0.0,
                tier=Tier.NORMAL,
                direction=Direction.NEUTRAL,
                confidence=0.3,                # baseline insufficient → low confidence
                features_ref=features.id,
                fired_detectors=[],
                reason_codes=["WARMUP_BASELINE_INSUFFICIENT"],
            )

        # ── Detector 1: vol_z_v1 (volume anomaly) ──
        vol_tier = self._vol_z_tier(vol_z)
        if not self.config.allow_vol_only_emergency:
            vol_tier = self._cap_tier(vol_tier, Tier.RISK_OFF)
        vol_score = self._score(vol_z)
        vol_reason = f"VOL_Z={vol_z:.1f}" if vol_tier != Tier.NORMAL else ""

        # ── Detector 2: insider_v1 (combination detector) ──
        insider_tier, insider_score, insider_reason = self._insider_signal(
            vol_z=vol_z,
            oi_delta_usd=oi_delta_usd,
            oi_delta_ratio=oi_delta_ratio,
            funding_rate=funding_rate,
            funding_delta_5min=funding_delta_5min,
            price_return_abs_5min=price_return_abs_5min,
            impact_ratio=impact_ratio,
        )

        # ── Detector 4: new_whale_v1 (P9.2.P2) ──
        new_whale_tier, new_whale_score, new_whale_reason = self._new_whale_signal(
            warmup_ready=new_whale_warmup_ready,
            max_cum5m_usd=new_whale_max_cum5m_usd,
            count_24h=new_whale_count_24h,
        )

        # ── Detector 5: cluster_v1 (P9.2.P3 — distributed betting) ──
        cluster_tier, cluster_score, cluster_reason = self._cluster_signal(
            warmup_ready=cluster_warmup_ready,
            top_sum_notional_usd=cluster_top_sum_usd,
            top_n_wallets=cluster_top_n_wallets,
        )

        tier_pairs = [
            (vol_tier, "vol_z_v1", vol_reason, vol_score),
            (insider_tier, "insider_v1", insider_reason, insider_score),
            (new_whale_tier, "new_whale_v1", new_whale_reason, new_whale_score),
            (cluster_tier, "cluster_v1", cluster_reason, cluster_score),
        ]

        fired = [name for t, name, _, _ in tier_pairs if t != Tier.NORMAL and name]
        reasons = [r for _, _, r, _ in tier_pairs if r]
        final_tier = Tier.max_of([t for t, _, _, _ in tier_pairs])
        score = max((s for _, _, _, s in tier_pairs), default=0.0)
        score = max(0.0, min(1.0, score))

        # EMERGENCY guard: allow only when at least 2 strong detectors hit at once.
        # Exception: new_whale_v1 / cluster_v1 EMERGENCY are strong single signals → may bypass.
        if final_tier == Tier.EMERGENCY:
            new_whale_emergency = (
                new_whale_tier == Tier.EMERGENCY
                and self.config.new_whale_bypass_emergency_guard
            )
            cluster_emergency = (
                cluster_tier == Tier.EMERGENCY
                and self.config.cluster_bypass_emergency_guard
            )
            if not (new_whale_emergency or cluster_emergency):
                riskoff_plus_cnt = sum(
                    1 for t, _, _, _ in tier_pairs if t in (Tier.RISK_OFF, Tier.EMERGENCY)
                )
                if riskoff_plus_cnt < self.config.min_riskoff_detectors_for_emergency:
                    final_tier = Tier.RISK_OFF
                    reasons.append("EMERGENCY_GUARD_DOWNGRADED")

        # panic_filter_v1: downgrade tier on panic-flow signature.
        # However, do not downgrade if new_whale_v1 or cluster_v1 is RISK_OFF/EMERGENCY
        # (fresh-wallet large/distributed entry is more likely an actual insider).
        panic = self._is_panic_flow(
            vol_z=vol_z,
            oi_delta_usd=oi_delta_usd,
            price_return_5min=price_return_5min,
            impact_ratio=impact_ratio,
        )
        new_whale_strong = new_whale_tier in (Tier.RISK_OFF, Tier.EMERGENCY)
        cluster_strong = cluster_tier in (Tier.RISK_OFF, Tier.EMERGENCY)
        wallet_strong = new_whale_strong or cluster_strong
        if panic and final_tier in (Tier.RISK_OFF, Tier.EMERGENCY) and not wallet_strong:
            final_tier = Tier.WATCH
            score = min(score, 0.55)
            reasons.append("PANIC_FILTER_DOWNGRADED")
            if "panic_filter_v1" not in fired:
                fired.append("panic_filter_v1")

        direction = self._resolve_direction(
            price_return_5min=price_return_5min,
            funding_rate=funding_rate,
            oi_delta_usd=oi_delta_usd,
            new_whale_last_side=new_whale_last_side,
            new_whale_strong=new_whale_strong,
            cluster_top_side=cluster_top_side,
            cluster_strong=cluster_strong,
        )
        confidence = 0.7 if (panic and not wallet_strong) else 1.0

        return ChannelSignal(
            channel=CHANNEL_HYPERLIQUID,
            symbol=features.symbol,
            ts=features.ts,
            score=score,
            tier=final_tier,
            direction=direction,
            confidence=confidence,
            features_ref=features.id,
            fired_detectors=fired,
            reason_codes=reasons,
        )

    # ─────────────────────────────────────────────────────────────────
    # Per-detector rules
    # ─────────────────────────────────────────────────────────────────
    def _vol_z_tier(self, vol_z: float) -> Tier:
        """vol_z_v1 detector tier (vol_only)."""
        c = self.config
        if vol_z >= c.vol_z_emergency:
            return Tier.EMERGENCY
        if vol_z >= c.vol_z_riskoff:
            return Tier.RISK_OFF
        if vol_z >= c.vol_z_watch:
            return Tier.WATCH
        return Tier.NORMAL

    def _insider_signal(
        self,
        *,
        vol_z: float,
        oi_delta_usd: float,
        oi_delta_ratio: float,
        funding_rate: float,
        funding_delta_5min: float,
        price_return_abs_5min: float,
        impact_ratio: float,
    ) -> tuple[Tier, float, str]:
        """Insider-like accumulation combination detector.

        Score the conditions to raise the tier; record reasons in a reason-code string.
        """
        c = self.config
        cond_vol = vol_z >= c.insider_vol_z_min
        cond_oi = (
            oi_delta_usd >= c.insider_oi_delta_usd_min
            or oi_delta_ratio >= c.insider_oi_ratio_min
        )
        cond_funding = (
            abs(funding_rate) >= c.insider_funding_abs_min
            or abs(funding_delta_5min) >= c.insider_funding_delta_min
        )
        # Low price impact for the same volume suggests a "quiet accumulation".
        cond_stealth = (
            price_return_abs_5min <= c.insider_price_return_abs_max
            and (impact_ratio <= c.insider_impact_ratio_max or impact_ratio == 0.0)
        )

        cond_cnt = sum((cond_vol, cond_oi, cond_funding, cond_stealth))
        if cond_cnt >= c.insider_emergency_min_conditions:
            tier = Tier.EMERGENCY
            score = 0.90
        elif cond_cnt >= c.insider_watch_min_conditions:
            tier = Tier.RISK_OFF
            score = 0.75
        elif cond_cnt == 1:
            tier = Tier.WATCH
            score = 0.55
        else:
            tier = Tier.NORMAL
            score = 0.0

        if tier == Tier.NORMAL:
            return tier, score, ""

        reason = (
            f"INSIDER cond={cond_cnt}/4 OIΔ={oi_delta_usd:+.0f} "
            f"fund={funding_rate:+.5f} impact_ratio={impact_ratio:.2f}"
        )
        return tier, score, reason

    def _is_panic_flow(
        self,
        *,
        vol_z: float,
        oi_delta_usd: float,
        price_return_5min: float,
        impact_ratio: float,
    ) -> bool:
        """Detect panic-like flow.

        Even with a vol spike, a sharp drop + high impact + no OI accumulation lowers insider likelihood.
        """
        c = self.config
        return (
            vol_z >= c.panic_vol_z_min
            and oi_delta_usd <= c.panic_oi_delta_usd_max
            and price_return_5min <= c.panic_price_drop_5min
            and impact_ratio >= c.panic_impact_ratio_min
        )

    @staticmethod
    def _cap_tier(value: Tier, cap: Tier) -> Tier:
        """Tier upper-bound helper."""
        if value.rank() > cap.rank():
            return cap
        return value

    @staticmethod
    def _resolve_direction(
        *,
        price_return_5min: float,
        funding_rate: float,
        oi_delta_usd: float,
        new_whale_last_side: str = "",
        new_whale_strong: bool = False,
        cluster_top_side: str = "",
        cluster_strong: bool = False,
    ) -> Direction:
        """Direction priority: strong cluster > strong new_whale > price → OI/funding."""
        # -1) When cluster_v1 is strong, its side has the highest confidence
        #     (a multi-fresh-wallet consensus is stronger than a single wallet's last_side).
        if cluster_strong:
            if cluster_top_side == "B":
                return Direction.UP
            if cluster_top_side == "A":
                return Direction.DOWN

        # 0) When new_whale_v1 fires strongly, the fresh wallet's last side has high confidence.
        if new_whale_strong:
            if new_whale_last_side == "B":
                return Direction.UP
            if new_whale_last_side == "A":
                return Direction.DOWN
            # if side missing, fall through

        # 1) Use the price direction when it is clear
        if price_return_5min >= 0.001:
            return Direction.UP
        if price_return_5min <= -0.001:
            return Direction.DOWN

        # 2) When price change is small, fall back to OI + funding combination
        if oi_delta_usd > 0 and funding_rate > 0:
            return Direction.UP
        if oi_delta_usd > 0 and funding_rate < 0:
            return Direction.DOWN
        if funding_rate > 0:
            return Direction.UP
        if funding_rate < 0:
            return Direction.DOWN
        return Direction.NEUTRAL

    # ─────────────────────────────────────────────────────────────────
    # new_whale_v1 — P9.2.P2 D2: New whale emergence
    # ─────────────────────────────────────────────────────────────────
    def _new_whale_signal(
        self,
        *,
        warmup_ready: bool,
        max_cum5m_usd: float,
        count_24h: int,
    ) -> tuple[Tier, float, str]:
        """Tier based on 5-min cumulative notional from fresh wallets (first seen within 24h).

        Cold-start protection: NORMAL whenever warmup_ready=False
            (every wallet looks "new" for 24h after daemon/store boot, so the
             channel only sets warmup_ready=True after sufficient time has elapsed).

        threshold (config.new_whale_*_usd):
            cum5m >= emergency  → EMERGENCY  (single-detector EMERGENCY allowed)
            cum5m >= riskoff    → RISK_OFF
            cum5m >= watch      → WATCH

        Returns:
            (tier, score [0..1], reason_string)
        """
        c = self.config

        if not warmup_ready:
            return (Tier.NORMAL, 0.0, "")

        if max_cum5m_usd <= 0.0 or count_24h <= 0:
            return (Tier.NORMAL, 0.0, "")

        if max_cum5m_usd >= c.new_whale_emergency_usd:
            tier = Tier.EMERGENCY
            score = 0.92
        elif max_cum5m_usd >= c.new_whale_riskoff_usd:
            tier = Tier.RISK_OFF
            score = 0.78
        elif max_cum5m_usd >= c.new_whale_watch_usd:
            tier = Tier.WATCH
            score = 0.55
        else:
            return (Tier.NORMAL, 0.0, "")

        reason = (
            f"NEW_WHALE n24h={count_24h} "
            f"max_cum5m=${max_cum5m_usd/1_000_000:.2f}M"
        )
        return (tier, score, reason)

    # ─────────────────────────────────────────────────────────────────
    # cluster_v1 — P9.2.P3 D5: Distributed betting detection
    # ─────────────────────────────────────────────────────────────────
    def _cluster_signal(
        self,
        *,
        warmup_ready: bool,
        top_sum_notional_usd: float,
        top_n_wallets: int,
    ) -> tuple[Tier, float, str]:
        """Tier for fresh-wallet clusters (same coin × same side × same price band).

        Cold-start protection: NORMAL when warmup_ready=False.

        Threshold mapping (AND):
            n_wallets >= emergency_n  AND  sum >= emergency_usd  → EMERGENCY
            n_wallets >= riskoff_n    AND  sum >= riskoff_usd    → RISK_OFF
            n_wallets >= watch_n      AND  sum >= watch_usd      → WATCH

        Both must hold — many wallets but small notional is noise; large notional
        but few wallets belongs to P2's new_whale_v1 territory.
        """
        c = self.config

        if not warmup_ready:
            return (Tier.NORMAL, 0.0, "")

        if top_sum_notional_usd <= 0.0 or top_n_wallets <= 0:
            return (Tier.NORMAL, 0.0, "")

        if (
            top_n_wallets >= c.cluster_emergency_n_wallets
            and top_sum_notional_usd >= c.cluster_emergency_sum_usd
        ):
            tier = Tier.EMERGENCY
            score = 0.93
        elif (
            top_n_wallets >= c.cluster_riskoff_n_wallets
            and top_sum_notional_usd >= c.cluster_riskoff_sum_usd
        ):
            tier = Tier.RISK_OFF
            score = 0.80
        elif (
            top_n_wallets >= c.cluster_watch_n_wallets
            and top_sum_notional_usd >= c.cluster_watch_sum_usd
        ):
            tier = Tier.WATCH
            score = 0.58
        else:
            return (Tier.NORMAL, 0.0, "")

        reason = (
            f"CLUSTER n_wallets={top_n_wallets} "
            f"sum=${top_sum_notional_usd/1_000_000:.2f}M"
        )
        return (tier, score, reason)

    # ─────────────────────────────────────────────────────────────────
    # Score mapping — [0, 1]
    # ─────────────────────────────────────────────────────────────────
    def _score(self, vol_z: float) -> float:
        """Map vol_z to score [0,1].

        Mapping (piecewise linear):
          vol_z  0 → 0.00
                 4 → 0.50 (WATCH cutoff)
                 6 → 0.70 (RISK_OFF)
                 8 → 0.85 (EMERGENCY)
                10 → ~0.92
                ∞ → 0.95
        """
        c = self.config

        if vol_z <= 0:
            return 0.0
        if vol_z < c.vol_z_watch:
            return 0.5 * (vol_z / c.vol_z_watch)             # 0 → 0.5
        if vol_z < c.vol_z_riskoff:
            t = (vol_z - c.vol_z_watch) / (c.vol_z_riskoff - c.vol_z_watch)
            return 0.50 + 0.20 * t                            # 0.50 → 0.70
        if vol_z < c.vol_z_emergency:
            t = (vol_z - c.vol_z_riskoff) / (c.vol_z_emergency - c.vol_z_riskoff)
            return 0.70 + 0.15 * t                            # 0.70 → 0.85

        # Smoothly map 8 ~ ∞ to 0.85 ~ 0.95 (8 → 0.85, 10 → ~0.92, ∞ → 0.95)
        extra = max(0.0, vol_z - c.vol_z_emergency)
        return min(0.95, 0.85 + 0.10 * (extra / (extra + 2.0)))
