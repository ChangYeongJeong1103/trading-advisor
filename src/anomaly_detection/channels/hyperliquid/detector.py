"""
hyperliquid/detector.py — Hyperliquid channel-level tier decision. v5 (P13).

Why v5 was needed (user observation 2026-07-26):
  v4 lined up 5 detectors and set the tier by "how many fired at once". But 3
  of those 5 (vol_z_v1 / insider_v1 / panic_filter_v1) were just different
  readings of the SAME 5 features (volume, OI, funding, price, impact), so
  co-firing was not cross-validation — it was counting one number twice:
    · insider_v1's volume condition (z>=20) equalled vol_z_v1's RISK_OFF
      threshold (20), so whenever insider fired, vol_z had already fired —
      strict containment. The "2 detectors agree" guard was really 1 piece
      of evidence.
    · insider_v1 and panic_filter_v1 were mutually exclusive on OI sign
      (up vs down) and impact direction (low vs high), so panic could never
      demote insider — 0 demotions across 400k random cases (dead code).

v5 structure — separate "what happened" from "what kind of thing it was":

  [Layer 1] Evidence axes — only things that read DIFFERENT data.
            This is the only layer where we count.
    A1 vol_z_v1     : volume z-score
    A2 oi_shift_v1  : MAGNITUDE of the open-interest move (sign is layer 2's job)
    A3 new_whale_v1 : 5-min cumulative notional of wallets first seen within 24h
    A4 cluster_v1   : fresh wallets splitting into the same coin × side × price band
                      (P9.2.P3 — D5). The channel queries wallet_store and fills:
                        cluster_top_sum_notional_usd  : top cluster summed notional
                        cluster_top_n_wallets         : top cluster distinct wallets
                        cluster_warmup_ready          : cold-start guard
                        cluster_top_side_code         : +1.0=B / -1.0=A / 0.0=unknown

  [Layer 2] Flow profile — never counted; only decides WHAT KIND of flow it is.
    · ACCUMULATION (insider_v1)      : OI building + low impact + quiet price.
                                       Add a funding skew and it becomes "strong".
    · CAPITULATION (panic_filter_v1) : OI unwinding + sharp drop + high impact.
    · UNCLASSIFIED                   : neither profile fits.
    funding / impact / price are inputs to this classification rather than axes:
    they do not tell us that something happened, only what the something was.

Tier decision:
    evidence = Σ(axis strength)  (per axis 0=NORMAL / 1=WATCH / 2=RISK_OFF / 3=EMERGENCY)
    base  = tier implied by the evidence total
    floor = tier of the single strongest axis
    tier  = max(base, floor)
  Then three caps:
    1) With only one axis firing, cap at that axis's solo ceiling.
       Volume/OI alone → WATCH; cluster alone → RISK_OFF.
       Exception: new_whale $25M+ may reach EMERGENCY alone to preserve the
       historical China-tariff case ($133M in 5 minutes).
    2) EMERGENCY requires at least 2 DIFFERENT axes.
    3) Anything that is not a strong ACCUMULATION caps at RISK_OFF.
       The historical new-whale exception bypasses both rules 2 and 3.
       CAPITULATION is already visible to the whole market, so it emails but
       never posts to X — this system's value is catching quiet accumulation
       that nobody has noticed yet.

Design points:
  - Still NORMAL if baseline is immature (warmup protection)
  - Direction priority: strong cluster > strong new_whale > price_return > funding/OI
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


# Axis strength (0-3) → Tier. The index IS the strength.
_TIER_BY_STRENGTH: tuple[Tier, ...] = (
    Tier.NORMAL,
    Tier.WATCH,
    Tier.RISK_OFF,
    Tier.EMERGENCY,
)

# Layer-2 flow profile names (surfaced in fired_detectors / reason_codes).
FLOW_ACCUMULATION = "ACCUMULATION"
FLOW_CAPITULATION = "CAPITULATION"
FLOW_UNCLASSIFIED = "UNCLASSIFIED"

# profile → legacy detector name, so renderer / LLM assessor label dicts keep working.
_FLOW_DETECTOR_NAME: dict[str, str] = {
    FLOW_ACCUMULATION: "insider_v1",
    FLOW_CAPITULATION: "panic_filter_v1",
}


@dataclass(frozen=True, slots=True)
class _Axis:
    """One evidence axis that fired."""

    name: str
    strength: int          # 1=WATCH / 2=RISK_OFF / 3=EMERGENCY
    solo_max_tier: Tier    # highest tier allowed when this axis fires alone
    reason: str


# =====================================================================
# Threshold config — tuned in one place (until D11 thresholds.yaml lands)
# =====================================================================
@dataclass(frozen=True, slots=True)
class HyperliquidDetectorConfig:
    """v5 thresholds — layer 1 (axes) and layer 2 (classification) listed apart."""

    # ─────────────────────────────────────────────────────────────────
    # [Layer 1] A1 — volume axis
    # ─────────────────────────────────────────────────────────────────
    # P13 (user decision 2026-07-26): thresholds raised sharply (4/6/9 →
    # 10/20/40). Measured on 7 days of Hyperliquid 5m candles (BTC/ETH/SOL):
    # z>=6 happens 36.5 times per day, so "6 sigma" is not rare at all. Volume
    # is heavy-tailed, so a Gaussian z-score is a poor measure of rarity.
    # z=20 corresponds to roughly 23x the baseline volume.
    vol_z_watch: float = 10.0
    vol_z_riskoff: float = 20.0
    vol_z_emergency: float = 40.0
    # Solo ceiling for volume — even at z=50 a spike still occurs 2.1 times a
    # day, so raising thresholds alone cannot stop the email noise (all 45 SOL
    # RISK_OFF signals were volume-only). WATCH shows up in the digest only.
    vol_solo_max_tier: Tier = Tier.WATCH

    # ─────────────────────────────────────────────────────────────────
    # [Layer 1] A2 — open-interest shift axis (magnitude, sign ignored)
    # ─────────────────────────────────────────────────────────────────
    # Require BOTH an absolute floor and a relative ratio. The absolute figure
    # alone ignores market scale ($2M is large for SOL, noise for BTC).
    # Actual open interest (2026-07-26): BTC $2.38B / ETH $2.05B / SOL $0.33B.
    #   → 0.5% = BTC $11.9M, ETH $10.2M, SOL $1.7M (the absolute floor binds for SOL)
    # The real 2026-07-26 event moved OI by $15.4M = 0.65% of BTC OI → WATCH strength.
    oi_delta_usd_min: float = 2_000_000.0
    oi_ratio_watch: float = 0.005
    oi_ratio_riskoff: float = 0.01
    oi_ratio_emergency: float = 0.02
    # OI also swings for benign reasons (liquidations, hedges, rollovers),
    # so it may not send an email on its own.
    oi_solo_max_tier: Tier = Tier.WATCH

    # ─────────────────────────────────────────────────────────────────
    # [Layer 1] A3 — new_whale axis (P9.2.P2 — D2: New whale emergence)
    # ─────────────────────────────────────────────────────────────────
    # 5-min cumulative taker notional (USD) from fresh wallets.
    # User-approved thresholds (Q3=$2M / $10M / $25M).
    new_whale_watch_usd: float = 2_000_000.0
    new_whale_riskoff_usd: float = 10_000_000.0
    new_whale_emergency_usd: float = 25_000_000.0
    # P10 historical lock: in the 2025-10-10 China-tariff case, a fresh wallet
    # added a $133M short within 5 minutes and produced the first warning 26.6h
    # before the announcement. Therefore a $25M+ new whale remains a solo
    # EMERGENCY even when OI/funding data is unavailable. This is an actor-level
    # discrete event, unlike aggregate volume z-score.
    new_whale_solo_max_tier: Tier = Tier.EMERGENCY
    new_whale_emergency_bypass_profile: bool = True

    # ─────────────────────────────────────────────────────────────────
    # [Layer 1] A4 — cluster axis (P9.2.P3 — D5: Distributed betting)
    # ─────────────────────────────────────────────────────────────────
    # (n_wallets, sum_notional_usd) as an AND condition.
    # User-approved: 3w/$5M (WATCH), 5w/$15M (RISK_OFF), 8w/$30M (EMERGENCY).
    cluster_watch_n_wallets: int = 3
    cluster_watch_sum_usd: float = 5_000_000.0
    cluster_riskoff_n_wallets: int = 5
    cluster_riskoff_sum_usd: float = 15_000_000.0
    cluster_emergency_n_wallets: int = 8
    cluster_emergency_sum_usd: float = 30_000_000.0
    cluster_solo_max_tier: Tier = Tier.RISK_OFF

    # ─────────────────────────────────────────────────────────────────
    # [Layer 1] combination rules
    # ─────────────────────────────────────────────────────────────────
    # evidence total → tier boundaries. Encodes the principle that several
    # axes lighting up together is stronger evidence than one extreme axis.
    evidence_watch: int = 1
    evidence_riskoff: int = 3
    evidence_emergency: int = 4
    # EMERGENCY always needs at least 2 DIFFERENT data sources.
    # v4 demanded 3, which was unreachable in practice — 0 EMERGENCY in 30 days
    # against 57 EMERGENCY_GUARD_DOWNGRADED. Root cause: only 2 truly
    # independent axes existed while 3 votes were required. With the axes
    # properly separated in v5, 2 is the correct value.
    min_axes_for_emergency: int = 2

    # ─────────────────────────────────────────────────────────────────
    # [Layer 2] ACCUMULATION (insider-like) classification
    # ─────────────────────────────────────────────────────────────────
    accum_oi_usd_min: float = 2_000_000.0
    accum_oi_ratio_min: float = 0.005
    accum_price_abs_max: float = 0.02      # price must stay quiet to be "stealthy"
    accum_impact_max: float = 0.90         # low price impact for the volume traded
    # Funding skew — its presence marks a STRONG accumulation and unlocks
    # EMERGENCY. Same intent as v4's "4/4 conditions" (P11(b).G1): genuine
    # insider accumulation shows up as positioning skew in funding. The
    # difference is that v4 counted it as a condition, whereas v5 uses it only
    # as the qualification for promotion to EMERGENCY.
    accum_funding_abs_min: float = 0.00002
    accum_funding_delta_min: float = 0.00001

    # ─────────────────────────────────────────────────────────────────
    # [Layer 2] CAPITULATION (panic) classification
    # ─────────────────────────────────────────────────────────────────
    # In v4 this was a filter that demoted others (and, thanks to the mutually
    # exclusive conditions, dead code). In v5 it is recognized as a valid flow
    # in its own right — a liquidation cascade in a downtrend is worth knowing.
    capit_oi_usd_max: float = 0.0          # OI flat or shrinking
    capit_price_drop_max: float = -0.01    # 5-min drop of 1% or more
    capit_impact_min: float = 1.30         # large price impact for the volume

    # ─────────────────────────────────────────────────────────────────
    # [Layer 2] tier ceiling
    # ─────────────────────────────────────────────────────────────────
    # Highest tier for any flow that is not a strong ACCUMULATION.
    # CAPITULATION is a public event the whole market already sees, so email
    # (RISK_OFF) is enough. UNCLASSIFIED means we could not characterize it,
    # so EMERGENCY would be overconfident.
    non_accumulation_max_tier: Tier = Tier.RISK_OFF


# =====================================================================
# HyperliquidDetector
# =====================================================================
class HyperliquidDetector:
    """Hyperliquid channel tier logic (v5 — evidence axes / flow profile)."""

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

        # P9.2.P2 — keys the channel fills in from wallet_store.
        # Missing or 0 when warmup is incomplete or there is no wallet_store.
        new_whale_max_cum5m_usd = float(f.get("new_whale_max_cum5m_usd", 0.0))
        new_whale_count_24h = int(f.get("new_whale_count_24h", 0))
        new_whale_warmup_ready = bool(f.get("new_whale_warmup_ready", 0.0) >= 1.0)
        # last_side encoding: the features dict is float-only, so it arrives as a number.
        #   +1.0 = "B" (taker buy)   /  -1.0 = "A" (taker sell)  /  0.0 = unknown
        new_whale_last_side = self._decode_side(
            float(f.get("new_whale_last_side_code", 0.0))
        )

        # P9.2.P3 — cluster axis inputs (filled by the channel's wallet_store query).
        cluster_top_sum_usd = float(f.get("cluster_top_sum_notional_usd", 0.0))
        cluster_top_n_wallets = int(f.get("cluster_top_n_wallets", 0))
        cluster_warmup_ready = bool(f.get("cluster_warmup_ready", 0.0) >= 1.0)
        cluster_top_side = self._decode_side(
            float(f.get("cluster_top_side_code", 0.0))
        )

        # ── Warmup: always NORMAL while the baseline is immature ──
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

        # ═════════════════════════════════════════════════════════════
        # [Layer 1] Evidence axes — only things reading different data
        # ═════════════════════════════════════════════════════════════
        c = self.config
        axes: list[_Axis] = []

        axes.extend(self._volume_axis(vol_z))
        axes.extend(self._oi_axis(oi_delta_usd, oi_delta_ratio))
        axes.extend(self._new_whale_axis(
            warmup_ready=new_whale_warmup_ready,
            max_cum5m_usd=new_whale_max_cum5m_usd,
            count_24h=new_whale_count_24h,
        ))
        axes.extend(self._cluster_axis(
            warmup_ready=cluster_warmup_ready,
            top_sum_notional_usd=cluster_top_sum_usd,
            top_n_wallets=cluster_top_n_wallets,
        ))

        if not axes:
            return ChannelSignal(
                channel=CHANNEL_HYPERLIQUID,
                symbol=features.symbol,
                ts=features.ts,
                score=0.0,
                tier=Tier.NORMAL,
                direction=Direction.NEUTRAL,
                confidence=1.0,
                features_ref=features.id,
                fired_detectors=[],
                reason_codes=[],
            )

        evidence = sum(a.strength for a in axes)
        # P10 historical lock — guarantee that the China-tariff event's
        # $133M/5min fresh-wallet short remains EMERGENCY even without
        # OI/funding data.
        historical_new_whale_emergency = (
            c.new_whale_emergency_bypass_profile
            and any(a.name == "new_whale_v1" and a.strength >= 3 for a in axes)
        )
        # base  = accumulated evidence across axes, floor = the single strongest axis.
        # Take the higher of the two — both "one extreme axis" and "several at
        # once" are legitimate ways to be significant.
        tier = self._max_tier(
            self._tier_from_evidence(evidence),
            _TIER_BY_STRENGTH[max(a.strength for a in axes)],
        )

        # ═════════════════════════════════════════════════════════════
        # [Layer 2] Flow profile — no counting, only the kind of flow
        # ═════════════════════════════════════════════════════════════
        profile, profile_reason, strong_accumulation = self._classify_flow(
            oi_delta_usd=oi_delta_usd,
            oi_delta_ratio=oi_delta_ratio,
            funding_rate=funding_rate,
            funding_delta_5min=funding_delta_5min,
            price_return_5min=price_return_5min,
            price_return_abs_5min=price_return_abs_5min,
            impact_ratio=impact_ratio,
        )

        # ═════════════════════════════════════════════════════════════
        # Three tier ceilings
        # ═════════════════════════════════════════════════════════════
        caps: list[str] = []

        # 1) Only one axis → cap at that axis's solo ceiling.
        if len(axes) == 1 and tier.rank() > axes[0].solo_max_tier.rank():
            tier = axes[0].solo_max_tier
            caps.append(f"SOLO_AXIS_CAP={axes[0].name}")

        # 2) EMERGENCY requires at least N different axes.
        # The historical $25M+ new-whale actor signal is exempt.
        if (
            tier == Tier.EMERGENCY
            and len(axes) < c.min_axes_for_emergency
            and not historical_new_whale_emergency
        ):
            tier = Tier.RISK_OFF
            caps.append(f"EMERGENCY_NEEDS_{c.min_axes_for_emergency}_AXES")

        # 3) No EMERGENCY unless this is a strong ACCUMULATION.
        # Historical replay lacks funding, so the new-whale lock is exempt.
        if (
            tier == Tier.EMERGENCY
            and not strong_accumulation
            and not historical_new_whale_emergency
        ):
            tier = c.non_accumulation_max_tier
            caps.append(f"EMERGENCY_NEEDS_ACCUMULATION(flow={profile})")

        # ═════════════════════════════════════════════════════════════
        # Assemble the result
        # ═════════════════════════════════════════════════════════════
        fired = [a.name for a in axes]
        flow_detector = _FLOW_DETECTOR_NAME.get(profile)
        if flow_detector:
            fired.append(flow_detector)

        reasons = [a.reason for a in axes]
        if profile_reason:
            reasons.append(profile_reason)
        if historical_new_whale_emergency:
            reasons.append("HISTORICAL_NEW_WHALE_EMERGENCY")
        reasons.extend(caps)

        new_whale_strong = self._axis_strong(axes, "new_whale_v1")
        cluster_strong = self._axis_strong(axes, "cluster_v1")
        direction = self._resolve_direction(
            price_return_5min=price_return_5min,
            funding_rate=funding_rate,
            oi_delta_usd=oi_delta_usd,
            new_whale_last_side=new_whale_last_side,
            new_whale_strong=new_whale_strong,
            cluster_top_side=cluster_top_side,
            cluster_strong=cluster_strong,
        )

        return ChannelSignal(
            channel=CHANNEL_HYPERLIQUID,
            symbol=features.symbol,
            ts=features.ts,
            score=self._score(tier, evidence),
            tier=tier,
            direction=direction,
            # Could not characterize it → "something is there but we do not
            # know what" → lower confidence.
            confidence=1.0 if profile != FLOW_UNCLASSIFIED else 0.7,
            features_ref=features.id,
            fired_detectors=fired,
            reason_codes=reasons,
        )

    # ═════════════════════════════════════════════════════════════════
    # [Layer 1] Evidence axes
    # ═════════════════════════════════════════════════════════════════
    def _volume_axis(self, vol_z: float) -> list[_Axis]:
        """A1 — volume z-score axis."""
        c = self.config
        if vol_z >= c.vol_z_emergency:
            strength = 3
        elif vol_z >= c.vol_z_riskoff:
            strength = 2
        elif vol_z >= c.vol_z_watch:
            strength = 1
        else:
            return []
        return [_Axis("vol_z_v1", strength, c.vol_solo_max_tier, f"VOL_Z={vol_z:.1f}")]

    def _oi_axis(self, oi_delta_usd: float, oi_delta_ratio: float) -> list[_Axis]:
        """A2 — MAGNITUDE of the open-interest move.

        The sign is deliberately ignored here. A surge (accumulation) and a
        collapse (liquidation) are both anomalies; deciding which one it is
        belongs to the layer-2 classification. The axis only answers "how big".
        """
        c = self.config
        abs_usd = abs(oi_delta_usd)
        abs_ratio = abs(oi_delta_ratio)

        # Below the absolute floor it is noise regardless of market size.
        if abs_usd < c.oi_delta_usd_min:
            return []

        if abs_ratio >= c.oi_ratio_emergency:
            strength = 3
        elif abs_ratio >= c.oi_ratio_riskoff:
            strength = 2
        elif abs_ratio >= c.oi_ratio_watch:
            strength = 1
        else:
            return []

        reason = f"OI_SHIFT {oi_delta_usd:+,.0f} ({oi_delta_ratio:+.2%})"
        return [_Axis("oi_shift_v1", strength, c.oi_solo_max_tier, reason)]

    def _new_whale_axis(
        self,
        *,
        warmup_ready: bool,
        max_cum5m_usd: float,
        count_24h: int,
    ) -> list[_Axis]:
        """A3 — 5-min cumulative notional from fresh wallets (first seen within 24h).

        Cold-start protection: empty list whenever warmup_ready=False.
            (Every wallet looks "new" for 24h after daemon/store boot, so the
             channel only sets warmup_ready=True after enough time has passed.)
        """
        c = self.config
        if not warmup_ready or max_cum5m_usd <= 0.0 or count_24h <= 0:
            return []

        if max_cum5m_usd >= c.new_whale_emergency_usd:
            strength = 3
        elif max_cum5m_usd >= c.new_whale_riskoff_usd:
            strength = 2
        elif max_cum5m_usd >= c.new_whale_watch_usd:
            strength = 1
        else:
            return []

        reason = (
            f"NEW_WHALE n24h={count_24h} "
            f"max_cum5m=${max_cum5m_usd / 1_000_000:.2f}M"
        )
        return [_Axis("new_whale_v1", strength, c.new_whale_solo_max_tier, reason)]

    def _cluster_axis(
        self,
        *,
        warmup_ready: bool,
        top_sum_notional_usd: float,
        top_n_wallets: int,
    ) -> list[_Axis]:
        """A4 — fresh-wallet cluster (same coin × same side × same price band).

        Both the wallet count and the summed notional must hold: many wallets
        with small notional is noise, while large notional from few wallets
        belongs to the new_whale axis.
        """
        c = self.config
        if not warmup_ready or top_sum_notional_usd <= 0.0 or top_n_wallets <= 0:
            return []

        if (
            top_n_wallets >= c.cluster_emergency_n_wallets
            and top_sum_notional_usd >= c.cluster_emergency_sum_usd
        ):
            strength = 3
        elif (
            top_n_wallets >= c.cluster_riskoff_n_wallets
            and top_sum_notional_usd >= c.cluster_riskoff_sum_usd
        ):
            strength = 2
        elif (
            top_n_wallets >= c.cluster_watch_n_wallets
            and top_sum_notional_usd >= c.cluster_watch_sum_usd
        ):
            strength = 1
        else:
            return []

        reason = (
            f"CLUSTER n_wallets={top_n_wallets} "
            f"sum=${top_sum_notional_usd / 1_000_000:.2f}M"
        )
        return [_Axis("cluster_v1", strength, c.cluster_solo_max_tier, reason)]

    # ═════════════════════════════════════════════════════════════════
    # [Layer 2] Flow classification
    # ═════════════════════════════════════════════════════════════════
    def _classify_flow(
        self,
        *,
        oi_delta_usd: float,
        oi_delta_ratio: float,
        funding_rate: float,
        funding_delta_5min: float,
        price_return_5min: float,
        price_return_abs_5min: float,
        impact_ratio: float,
    ) -> tuple[str, str, bool]:
        """Decide whether the same anomaly is quiet accumulation or capitulation.

        Returns:
            (profile, reason_string, strong_accumulation)
            Only strong_accumulation=True can be promoted to EMERGENCY.
        """
        c = self.config

        # ── ACCUMULATION: OI builds while price stays quiet and impact is low ──
        oi_building = (
            oi_delta_usd >= c.accum_oi_usd_min
            and oi_delta_ratio >= c.accum_oi_ratio_min
        )
        # impact_ratio == 0.0 means "not measurable", so treat it as quiet.
        quiet_price = (
            price_return_abs_5min <= c.accum_price_abs_max
            and (impact_ratio <= c.accum_impact_max or impact_ratio == 0.0)
        )
        if oi_building and quiet_price:
            funding_skew = (
                abs(funding_rate) >= c.accum_funding_abs_min
                or abs(funding_delta_5min) >= c.accum_funding_delta_min
            )
            reason = (
                f"FLOW=ACCUMULATION OIΔ={oi_delta_usd:+,.0f} "
                f"fund={funding_rate:+.5f} impact={impact_ratio:.2f}"
                + ("" if funding_skew else " (funding flat)")
            )
            return FLOW_ACCUMULATION, reason, funding_skew

        # ── CAPITULATION: OI unwinds, price drops hard, impact is large ──
        if (
            oi_delta_usd <= c.capit_oi_usd_max
            and price_return_5min <= c.capit_price_drop_max
            and impact_ratio >= c.capit_impact_min
        ):
            reason = (
                f"FLOW=CAPITULATION OIΔ={oi_delta_usd:+,.0f} "
                f"ret={price_return_5min:+.2%} impact={impact_ratio:.2f}"
            )
            return FLOW_CAPITULATION, reason, False

        return FLOW_UNCLASSIFIED, "FLOW=UNCLASSIFIED", False

    # ═════════════════════════════════════════════════════════════════
    # Combination helpers
    # ═════════════════════════════════════════════════════════════════
    def _tier_from_evidence(self, evidence: int) -> Tier:
        """Axis strength total → tier."""
        c = self.config
        if evidence >= c.evidence_emergency:
            return Tier.EMERGENCY
        if evidence >= c.evidence_riskoff:
            return Tier.RISK_OFF
        if evidence >= c.evidence_watch:
            return Tier.WATCH
        return Tier.NORMAL

    def _score(self, tier: Tier, evidence: int) -> float:
        """Score in [0, 1]: the tier sets the floor, evidence fine-tunes it.

        A tier and score that disagree distort the fusion stage (WATCH with
        0.95, say), so the score is confined to a band per tier.
        """
        base, cap = {
            Tier.NORMAL: (0.0, 0.0),
            Tier.WATCH: (0.40, 0.55),
            Tier.RISK_OFF: (0.65, 0.80),
            Tier.EMERGENCY: (0.85, 0.95),
        }[tier]
        return min(cap, base + 0.03 * evidence)

    @staticmethod
    def _max_tier(a: Tier, b: Tier) -> Tier:
        return a if a.rank() >= b.rank() else b

    @staticmethod
    def _axis_strong(axes: list[_Axis], name: str) -> bool:
        """Did this axis fire at RISK_OFF+ strength (used for direction priority)."""
        return any(a.name == name and a.strength >= 2 for a in axes)

    @staticmethod
    def _decode_side(code: float) -> str:
        """Float side code from the features dict → "B" / "A" / ""."""
        if code >= 0.5:
            return "B"
        if code <= -0.5:
            return "A"
        return ""

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
        # -1) When the cluster axis is strong its side is the most reliable
        #     (agreement across several fresh wallets beats one wallet's last side).
        if cluster_strong:
            if cluster_top_side == "B":
                return Direction.UP
            if cluster_top_side == "A":
                return Direction.DOWN

        # 0) When the new_whale axis is strong, the fresh wallet's last side is reliable.
        if new_whale_strong:
            if new_whale_last_side == "B":
                return Direction.UP
            if new_whale_last_side == "A":
                return Direction.DOWN
            # fall through when no side is known

        # 1) Use price when its direction is clear
        if price_return_5min >= 0.001:
            return Direction.UP
        if price_return_5min <= -0.001:
            return Direction.DOWN

        # 2) With little price movement, fall back to OI + funding
        if oi_delta_usd > 0 and funding_rate > 0:
            return Direction.UP
        if oi_delta_usd > 0 and funding_rate < 0:
            return Direction.DOWN
        if funding_rate > 0:
            return Direction.UP
        if funding_rate < 0:
            return Direction.DOWN
        return Direction.NEUTRAL
