"""
replay/metrics.py — Compute ReplayMetrics from a sequence of ReplayMinutes.

────────────────────────────────────────────────────────────────────────
v0 stage (this file):
  · max_tier_reached         — highest system_state tier reached within the window
  · first_alert_ts           — sim_clock of the first WATCH+ transition
  · first_alert_tier         — the new tier of that transition
  · warning_time_s           — announcement_ts - first_alert_ts (positive = before announcement)
  · channels_fired           — sorted list of channels that emitted WATCH+

Items left blank in v0 (to be filled in P10.4):
  · detection_latency_s      — first_alert_ts - first_anomaly_observed_ts
        Ground truth (the first suspicious-trade time from the event md narrative)
        is not yet typed in the frontmatter, so v0 leaves it None. P10.4 will
        parse the narrative or add a frontmatter field.
  · fp_count                 — false-positive counting rule not yet defined in v0.
        Set to 0 for now; P10.4 will introduce rules like "alerts outside the
        window" or "alerts on a different symbol".
  · target_match_score       — comparison against the event md's target_tier_timeline.
        Requires narrative parsing → P10.4.

────────────────────────────────────────────────────────────────────────
Reference: docs/p10-replay-framework.md §5 (metric definitions)
"""

from __future__ import annotations

# --- standard library ---
from typing import Sequence

# --- local ---
from ..core.schemas import Tier
from .schemas import HistoricalEvent, ReplayMetrics, ReplayMinute


def compute_metrics(event: HistoricalEvent, minutes: Sequence[ReplayMinute]) -> ReplayMetrics:
    """Compute ReplayMetrics from one event's replay minute timeline.

    Args:
        event: the replayed historical event (announcement_ts is the reference point).
        minutes: time-ordered list of ReplayMinute (the runner guarantees order).

    Returns:
        ReplayMetrics: only the v0 fields are populated (rest are None / 0 / sentinel).
    """
    # ── Accumulators ──
    max_tier: Tier = Tier.NORMAL
    first_alert_ts = None
    first_alert_tier: Tier | None = None
    channels_fired_set: set[str] = set()

    # ── Iterate one cycle at a time ──
    for m in minutes:
        # No cycle should have fused_event=None (runner always creates one).
        # Defensive guard.
        if m.fused_event is not None:
            # Update max_tier (using system_state).
            if m.fused_event.state.rank() > max_tier.rank():
                max_tier = m.fused_event.state

        # First alert: the first cycle where state_manager confirms a WATCH+ transition.
        # m.decision is only set on cycles that produced a transition (runner's stored_decision).
        if first_alert_ts is None and m.decision is not None and m.decision.state_change is not None:
            old_tier, new_tier = m.decision.state_change
            # Only escalation (lower → higher) counts as an alert.
            if new_tier.rank() > old_tier.rank() and new_tier.rank() >= Tier.WATCH.rank():
                first_alert_ts = m.sim_clock
                first_alert_tier = new_tier

        # Track which channels emitted WATCH+ — walk per_channel_signals.
        for ch_name, sig in m.per_channel_signals.items():
            if sig is not None and sig.tier.rank() >= Tier.WATCH.rank():
                channels_fired_set.add(ch_name)

    # ── derived ──
    warning_time_s: float | None = None
    if first_alert_ts is not None:
        # Positive = warned before announcement. Negative = late, after announcement.
        delta = event.announcement_ts - first_alert_ts
        warning_time_s = delta.total_seconds()

    return ReplayMetrics(
        max_tier_reached=max_tier,
        first_alert_ts=first_alert_ts,
        first_alert_tier=first_alert_tier,
        # Items to fill in P10.4 — v0 is a placeholder.
        detection_latency_s=None,
        warning_time_s=warning_time_s,
        fp_count=0,
        channels_fired=sorted(channels_fired_set),
        target_match_score=None,
    )
