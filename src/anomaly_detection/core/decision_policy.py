"""
core/decision_policy.py — FusedAnomalyEvent + state_change → DecisionRecord (architecture §5.5).

────────────────────────────────────────────────────────────────────────
Role:
  Once state_manager confirms a transition, decision_policy takes it and
  decides "what should the user do now" and "how should we tell them".

  In v0, it's a simple mapping:
    state (or the transition's target tier) → recommended_action + delivery_tier
  Refined in P9~P10 (market hours, per-symbol differentiation, position context, ...).

  Pure function. No state. Storage / alert dispatch are the caller's responsibility.

────────────────────────────────────────────────────────────────────────
v0 mapping (architecture §5.5, §6.5.1):

  No state_change (no change) → record only (no alert)
    recommended_action = NO_ACTION
    delivery_tier      = NONE

  → WATCH (escalation)              → MONITOR        + DIGEST
  → RISK_OFF (escalation)           → REDUCE_RISK    + REALTIME
  → EMERGENCY (escalation)          → EXIT_OR_HEDGE  + URGENT

  → NORMAL (de-escalation, "all clear") → NO_ACTION + REALTIME (single closure alert)
  → WATCH (de-escalation, coming down from EMERGENCY/RISK_OFF) → MONITOR + REALTIME
  → RISK_OFF (de-escalation, coming down from EMERGENCY) → REDUCE_RISK + REALTIME

  On de-escalation, always REALTIME — "the crisis is over" is only useful if the
  user is notified immediately. DIGEST is for new escalations to WATCH (low-priority batched).

────────────────────────────────────────────────────────────────────────
delivery_channels is left empty:
  This module decides policy only. Where it was actually sent (email / telegram)
  is recorded by alerts/router.py after dispatch (DecisionRecord update).

external_links is also empty:
  alerts/link_builder.py fills it just before rendering (channel→URL mapping).
  decision_policy doesn't know external URLs — responsibilities are separated.

Architecture: §5.5 Decision policy, §6.5.1 Tiered alerting
"""

from __future__ import annotations

import logging

from .schemas import (
    DecisionRecord,
    DeliveryTier,
    FusedAnomalyEvent,
    RecommendedAction,
    Tier,
)

logger = logging.getLogger(__name__)

POLICY_VERSION = "v0.1-baseline"


# ─────────────────────────────────────────────────────────────────────
# Mapping: target tier → (action, delivery_tier) for ESCALATION
# ─────────────────────────────────────────────────────────────────────
_ESCALATION_MAP: dict[Tier, tuple[RecommendedAction, DeliveryTier]] = {
    Tier.NORMAL: (RecommendedAction.NO_ACTION, DeliveryTier.NONE),  # rarely used (no escalation to NORMAL)
    Tier.WATCH: (RecommendedAction.MONITOR, DeliveryTier.DIGEST),
    Tier.RISK_OFF: (RecommendedAction.REDUCE_RISK, DeliveryTier.REALTIME),
    Tier.EMERGENCY: (RecommendedAction.EXIT_OR_HEDGE, DeliveryTier.URGENT),
}

# Every de-escalation is REALTIME (notify crisis-clear immediately).
# Action maps to "the recommendation for the tier just reached"
# (e.g. EMERGENCY→WATCH → MONITOR).
_DEESCALATION_ACTION: dict[Tier, RecommendedAction] = {
    Tier.NORMAL: RecommendedAction.NO_ACTION,       # full clear
    Tier.WATCH: RecommendedAction.MONITOR,           # partial clear — still observe
    Tier.RISK_OFF: RecommendedAction.REDUCE_RISK,    # partial clear — still reducing
    Tier.EMERGENCY: RecommendedAction.EXIT_OR_HEDGE,  # rarely used
}


# ─────────────────────────────────────────────────────────────────────
# notes generation (human-readable single line)
# ─────────────────────────────────────────────────────────────────────
def _build_notes(
    event: FusedAnomalyEvent,
    state_change: tuple[Tier, Tier] | None,
) -> str:
    """One-line description for the alert body / audit log."""
    if state_change is None:
        return f"No state change (current={event.state.value}). {event.rationale}"

    old, new = state_change
    direction = "ESCALATION" if new.rank() > old.rank() else "DE-ESCALATION"
    base = f"{direction}: {old.value} → {new.value}"
    if event.rationale:
        base += f". {event.rationale}"
    if event.boost_applied:
        base += f" [boost: {event.boost_applied}]"
    return base


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
def decide(
    event: FusedAnomalyEvent,
    state_change: tuple[Tier, Tier] | None,
) -> DecisionRecord:
    """Produce a DecisionRecord from FusedAnomalyEvent + state_change.

    Args:
        event: the fusion result just produced.
        state_change: the transition confirmed by state_manager. None = no change.

    Returns:
        DecisionRecord:
            - state_change=None: NO_ACTION + DeliveryTier.NONE (audit only)
            - escalation:        target tier → action + delivery_tier (mapping)
            - de-escalation:     target tier → action + REALTIME (closure alert)

        delivery_channels / external_links / cooldown_until are left empty —
        alerts/router fills them on dispatch.

    Pure function: same input → same output. No side effects.
    """
    # ── Case 1: No change ──
    if state_change is None:
        return DecisionRecord(
            ts=event.ts,
            fused_event_ref=event.id,
            recommended_action=RecommendedAction.NO_ACTION,
            policy_version=POLICY_VERSION,
            notes=_build_notes(event, None),
            state_change=None,
            delivery_tier=DeliveryTier.NONE,
            delivery_channels=[],
            external_links={},
        )

    old, new = state_change

    # ── Case 2: Escalation (lower → higher) ──
    if new.rank() > old.rank():
        action, delivery = _ESCALATION_MAP[new]
        decision = DecisionRecord(
            ts=event.ts,
            fused_event_ref=event.id,
            recommended_action=action,
            policy_version=POLICY_VERSION,
            notes=_build_notes(event, state_change),
            state_change=state_change,
            delivery_tier=delivery,
        )
        logger.info(
            "Decision (escalation): %s→%s action=%s delivery=%s",
            old.value, new.value, action.value, delivery.value,
        )
        return decision

    # ── Case 3: De-escalation (higher → lower) ──
    # Always REALTIME — "crisis cleared" is only useful when delivered immediately.
    action = _DEESCALATION_ACTION[new]
    decision = DecisionRecord(
        ts=event.ts,
        fused_event_ref=event.id,
        recommended_action=action,
        policy_version=POLICY_VERSION,
        notes=_build_notes(event, state_change),
        state_change=state_change,
        delivery_tier=DeliveryTier.REALTIME,
    )
    logger.info(
        "Decision (de-escalation): %s→%s action=%s delivery=REALTIME",
        old.value, new.value, action.value,
    )
    return decision


__all__ = ["decide", "POLICY_VERSION"]
