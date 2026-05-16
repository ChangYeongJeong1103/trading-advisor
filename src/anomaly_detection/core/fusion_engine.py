"""
core/fusion_engine.py — ChannelSignals → FusedAnomalyEvent (architecture §5.2, §5.4).

────────────────────────────────────────────────────────────────────────
Role:
  On every fusion cycle (e.g. 5 seconds), take the current signal from every
  channel and summarize "what is the system's overall risk tier right now"
  into one line (FusedAnomalyEvent).

  Close to a pure function (no state). Side effects (storage writes, alert
  dispatch) are the orchestrator's responsibility.

────────────────────────────────────────────────────────────────────────
v0 logic (3 steps):

  1) tier_floor = max(per_channel_tiers)
        — The strongest single channel sets the system tier's base.
        — None signals are treated as NORMAL.
        — A single strong signal is never drowned out (architecture §5.4.2 decision).

  2) Corroboration boost
        — If ≥2 channels are at WATCH+ in the same direction (UP/DOWN), boost
          the tier by one step.
        — NEUTRAL direction is excluded from agreement counting.
        — Record the reason in boost_applied (for audit / alert body).

  3) fused_score (secondary, reference)
        — noisy-OR: 1 - Π(1 - score_i × eff_weight_i)
        — eff_weight_i = config.weight × confidence × health
        — Not used for state decisions; only audit / alert body / debug.

────────────────────────────────────────────────────────────────────────
Why corroboration is a "boost" and not a "required condition":
  If we reject a single channel's EMERGENCY just because the others are quiet,
  we miss real danger (architecture §5.4.2). Corroboration is only used as a
  bonus that bumps our confidence one step.

Architecture: §5.2 (fusion flow), §5.4 (2-tier state definition)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .schemas import ChannelSignal, Direction, FusedAnomalyEvent, Tier

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Internal helper: simplified per-channel snapshot struct
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _ChannelView:
    """View of a single channel's current state, simplified for fusion computation."""
    name: str
    signal_id: str | None        # None = no signal (treated as NORMAL)
    tier: Tier
    score: float
    direction: Direction
    confidence: float
    eff_weight: float            # config weight × confidence × health


def _build_view(
    name: str,
    signal: ChannelSignal | None,
    base_weight: float,
    health: float,
) -> _ChannelView:
    """Convert a ChannelSignal (or None) into a fusion-computation view."""
    if signal is None:
        # No signal = treated as NORMAL. score 0, weight meaningless.
        return _ChannelView(
            name=name, signal_id=None,
            tier=Tier.NORMAL, score=0.0,
            direction=Direction.NEUTRAL, confidence=0.0,
            eff_weight=0.0,
        )

    eff = max(0.0, min(1.0, base_weight * health * signal.confidence))
    return _ChannelView(
        name=name, signal_id=signal.id,
        tier=signal.tier, score=signal.score,
        direction=signal.direction, confidence=signal.confidence,
        eff_weight=eff,
    )


# ─────────────────────────────────────────────────────────────────────
# Corroboration: number of channels agreeing in direction at WATCH+ + that direction
# ─────────────────────────────────────────────────────────────────────
def _count_agreement(views: list[_ChannelView]) -> tuple[int, Direction | None]:
    """Number of channels at WATCH+ agreeing in direction, and that direction.

    NEUTRAL direction is not counted (corroboration is meaningless without a direction).
    Pick whichever of UP / DOWN has more; if tied, return None (= no consensus).
    """
    up = [v for v in views if v.tier.rank() >= Tier.WATCH.rank() and v.direction == Direction.UP]
    down = [v for v in views if v.tier.rank() >= Tier.WATCH.rank() and v.direction == Direction.DOWN]

    if len(up) > len(down):
        return len(up), Direction.UP
    if len(down) > len(up):
        return len(down), Direction.DOWN
    if len(up) == 0 and len(down) == 0:
        return 0, None
    # Tied — treat as no consensus. The two sides are equal counts.
    return 0, None


# ─────────────────────────────────────────────────────────────────────
# Apply boost: tier_floor + corroboration → final state
# ─────────────────────────────────────────────────────────────────────
# P11(b).1 — global kill-switch for the corroboration boost (user decision lock 2026-04-22).
# Reason: WATCH happens routinely (134 events/day), so the moment one channel
# becomes RISK_OFF, the chance of another channel being at WATCH in the same
# direction is nearly 100% → fusion escalates RISK_OFF to EMERGENCY almost
# every time. 55% of our actual noise came from this boost.
# Insider trading fires strongly within a single channel (multi-channel
# corroboration is not required). Therefore, without the boost,
# state = tier_floor (the strongest channel's tier).
#
# When False, _apply_boost returns (tier_floor, None) immediately → effect 0
# even if the rest of the boost logic runs. Kept as config so it can be
# re-enabled with a single line if we later decide "actually, we do need boost".
_CORROBORATION_BOOST_ENABLED: bool = False


def _apply_boost(
    tier_floor: Tier,
    agree_count: int,
    agree_direction: Direction | None,
) -> tuple[Tier, str | None]:
    """Apply a corroboration boost on top of tier_floor and return the final state + reason.

    Boost rule (v0):
      - If ≥2 channels at WATCH+ agree in direction → bump tier by 1 step.
      - If already EMERGENCY, nowhere higher to go (boost no-op).
      - If tier_floor is NORMAL (= no channel is WATCH+) the boost is a no-op.

    P11(b).1 (2026-04-22): when _CORROBORATION_BOOST_ENABLED=False, always
    return (tier_floor, None) (kill-switch).
    """
    # Kill-switch — when boost is disabled, return base tier as-is.
    if not _CORROBORATION_BOOST_ENABLED:
        return tier_floor, None

    if agree_count < 2 or agree_direction is None:
        return tier_floor, None
    if tier_floor == Tier.NORMAL:
        # All channels NORMAL — agreement is impossible (defensive guard).
        return tier_floor, None

    next_tier_map = {
        Tier.NORMAL: Tier.WATCH,
        Tier.WATCH: Tier.RISK_OFF,
        Tier.RISK_OFF: Tier.EMERGENCY,
        Tier.EMERGENCY: Tier.EMERGENCY,  # cap
    }
    boosted = next_tier_map[tier_floor]
    if boosted == tier_floor:
        # Already at cap (EMERGENCY) — boost no-op, only the reason is recorded for audit.
        return tier_floor, (
            f"corroboration {agree_count}× {agree_direction.value} "
            f"(already at {tier_floor.value} cap)"
        )

    reason = (
        f"{tier_floor.value}→{boosted.value} "
        f"(corroboration: {agree_count} channels {agree_direction.value})"
    )
    return boosted, reason


# ─────────────────────────────────────────────────────────────────────
# noisy-OR fused_score (secondary reference metric)
# ─────────────────────────────────────────────────────────────────────
def _noisy_or(views: list[_ChannelView]) -> float:
    """fused = 1 - Π(1 - score_i × eff_weight_i)  (over every channel).

    Each term is in [0,1] so the result is in [0,1]. If any single channel is
    strong, fused is strong. NORMAL/None channels have score 0 so they fall
    out automatically (1-0 = 1, multiplicative identity).
    """
    product = 1.0
    for v in views:
        p_i = max(0.0, min(1.0, v.score * v.eff_weight))
        product *= (1.0 - p_i)
    return max(0.0, min(1.0, 1.0 - product))


# ─────────────────────────────────────────────────────────────────────
# Human-readable one-line rationale
# ─────────────────────────────────────────────────────────────────────
def _build_rationale(
    state: Tier,
    views: list[_ChannelView],
    boost_applied: str | None,
) -> str:
    """Single-line reason string for alerts / logs."""
    contributors = [v for v in views if v.tier.rank() > 0]  # > NORMAL
    if state == Tier.NORMAL or not contributors:
        return "NORMAL — all channels quiet"

    # Show only channels at the highest tier (e.g. "cme RISK_OFF, polymarket WATCH" → "cme RISK_OFF")
    top_tier = Tier.max_of([v.tier for v in contributors])
    top = [v for v in contributors if v.tier == top_tier]

    parts = [f"{v.name}={v.tier.value}" for v in top]
    base = f"{state.value} — " + ", ".join(parts)
    if boost_applied:
        base += f" | boost: {boost_applied}"
    return base


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
def fuse(
    signals: dict[str, ChannelSignal | None],
    weights: dict[str, float],
    health: dict[str, float] | None = None,
) -> FusedAnomalyEvent:
    """Combine every channel's current signal into the system-wide tier.

    Args:
        signals: result of registry.snapshot_signals(). {channel_name: ChannelSignal | None}.
        weights: config channel.weight. {channel_name: float [0,1]}.
                 Channels in signals but not in weights get weight 0 (= ignored).
        health:  optional health multiplier. {channel_name: float [0,1]}.
                 Missing channels default to 1.0 (healthy). UNHEALTHY → 0 or low.

    Returns:
        FusedAnomalyEvent: system tier right now + detailed audit info.
            - state: final system tier (tier_floor + boost)
            - tier_floor: max(per_channel_tiers) — base before boost
            - boost_applied: corroboration boost reason (None if none)
            - fused_score: noisy-OR result (secondary, reference)

    Note:
        If signals is empty, return a NORMAL FusedAnomalyEvent (graceful).
        Pure function — same input → same output. Storage / alerts are the caller's job.
    """
    health = health or {}

    # 1) Convert every channel into a view (key set from signals).
    views: list[_ChannelView] = [
        _build_view(
            name=name,
            signal=signal,
            base_weight=weights.get(name, 0.0),
            health=health.get(name, 1.0),
        )
        for name, signal in signals.items()
    ]

    # 2) tier_floor = strongest tier across all channels.
    tier_floor = Tier.max_of([v.tier for v in views])

    # 3) Corroboration → boost.
    agree_count, agree_direction = _count_agreement(views)
    state, boost_applied = _apply_boost(tier_floor, agree_count, agree_direction)

    # 4) fused_score (secondary).
    fused_score = _noisy_or(views)

    # 5) Assemble the FusedAnomalyEvent.
    per_channel_scores = {v.name: v.score for v in views}
    per_channel_tiers = {v.name: v.tier for v in views}
    per_channel_signal = {v.name: v.signal_id for v in views}
    weights_applied = {v.name: v.eff_weight for v in views}
    contributing = [v.signal_id for v in views if v.tier.rank() > 0 and v.signal_id is not None]
    rationale = _build_rationale(state, views, boost_applied)

    event = FusedAnomalyEvent(
        fused_score=fused_score,
        state=state,
        per_channel_scores=per_channel_scores,
        per_channel_tiers=per_channel_tiers,
        per_channel_signal=per_channel_signal,
        tier_floor=tier_floor,
        boost_applied=boost_applied,
        contributing=contributing,
        agreeing_channels=agree_count,
        agreeing_direction=agree_direction,
        weights=weights_applied,
        rationale=rationale,
    )

    if state != Tier.NORMAL:
        logger.info(
            "Fusion: state=%s tier_floor=%s boost=%s fused=%.2f rationale=%s",
            state.value, tier_floor.value, boost_applied, fused_score, rationale,
        )
    return event


__all__ = ["fuse"]
