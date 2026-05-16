"""
reporters/yaml_report.py — ReplayResult → human-readable per-event YAML.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Why YAML: human-readable + diffs cleanly (run-to-run comparison).
    JSON makes timestamps and lists long single lines that are hard to review.

  · We don't dump every cycle (including NORMAL). Even if a 60-minute event
    would fit under 60 lines if everything were dumped, "only changed lines"
    is dramatically more productive to review.

    → "Changed line" definition:
       (a) some channel's tier differs from the previous cycle, OR
       (b) a decision (state_change) occurred, OR
       (c) fused_event.tier_floor differs from the previous cycle.

  · Passing Pydantic model_dump straight to yaml.safe_dump can break enums and
    datetimes into !python/object tags → dict via mode="json" first, then dump.

────────────────────────────────────────────────────────────────────────
Output structure (overview):

    event:        # HistoricalEvent metadata (minus narrative_md)
    metrics:      # all of ReplayMetrics
    transitions:  # cycles that produced a state_change
    timeline:     # only "changed" cycles (ascending sim_clock)

────────────────────────────────────────────────────────────────────────
Reference: docs/p10-replay-framework.md §6
"""

from __future__ import annotations

# --- standard library ---
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

# --- third-party ---
import yaml

# --- local ---
from ..schemas import ReplayMinute, ReplayResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _offset_minutes(sim_clock: datetime, announce: datetime) -> str:
    """Format sim_clock as 'T-NN.N min' / 'T+NN.N min'. For report readability."""
    delta_min = (sim_clock - announce).total_seconds() / 60.0
    sign = "+" if delta_min >= 0 else "-"
    return f"T{sign}{abs(delta_min):.1f}min"


def _channel_tier_snapshot(minute: ReplayMinute) -> dict[str, str]:
    """One-line per-channel tier summary for this cycle. For prev-vs-curr comparison."""
    # If signal=None, treat as NORMAL (the channel didn't fire).
    return {
        ch: (sig.tier.value if sig else "NORMAL")
        for ch, sig in minute.per_channel_signals.items()
    }


def _is_interesting(prev: ReplayMinute | None, curr: ReplayMinute) -> bool:
    """Decide whether this cycle is 'interesting' enough to record in the timeline."""
    # decision (state_change) occurred → always interesting.
    if curr.decision is not None and curr.decision.state_change is not None:
        return True

    # First cycle — nothing to compare against, record it anyway (baseline).
    if prev is None:
        return True

    # tier_floor changed → channel-wide risk level changed.
    prev_floor = prev.fused_event.tier_floor.value if prev.fused_event else "NORMAL"
    curr_floor = curr.fused_event.tier_floor.value if curr.fused_event else "NORMAL"
    if prev_floor != curr_floor:
        return True

    # Any channel's tier changed.
    if _channel_tier_snapshot(prev) != _channel_tier_snapshot(curr):
        return True

    return False


def _serialize_minute(minute: ReplayMinute, announce: datetime) -> dict[str, Any]:
    """ReplayMinute → YAML-friendly dict (1 timeline entry)."""
    entry: dict[str, Any] = {
        "sim_clock": minute.sim_clock.isoformat(),
        "offset": _offset_minutes(minute.sim_clock, announce),
    }

    # per-channel summary — None flattens to NORMAL/0.0.
    channels: dict[str, Any] = {}
    for ch, sig in minute.per_channel_signals.items():
        if sig is None:
            channels[ch] = {"tier": "NORMAL", "score": 0.0}
        else:
            channels[ch] = {
                "tier": sig.tier.value,
                "score": round(sig.score, 4),
                "symbol": sig.symbol,
                "direction": sig.direction.value,
                "fired": list(sig.fired_detectors),
                "reasons": list(sig.reason_codes),
            }
    entry["channels"] = channels

    # fused snapshot — tier_floor / state / boost.
    # (fused_event always non-None in normal runs; defensive None check below.)
    if minute.fused_event is not None:
        fe = minute.fused_event
        entry["fused"] = {
            "tier_floor": fe.tier_floor.value,
            "state": fe.state.value,
            "fused_score": round(fe.fused_score, 4),
            "boost_applied": fe.boost_applied,
            "agreeing_channels": fe.agreeing_channels,
            "agreeing_direction": (
                fe.agreeing_direction.value if fe.agreeing_direction else None
            ),
            "rationale": fe.rationale,
        }

    # decision — detailed only when state_change exists.
    if minute.decision is not None:
        d = minute.decision
        entry["decision"] = {
            "action": d.recommended_action.value,
            "delivery_tier": d.delivery_tier.value,
            "state_change": (
                {"from": d.state_change[0].value, "to": d.state_change[1].value}
                if d.state_change
                else None
            ),
            "notes": d.notes,
        }

    return entry


def _event_metadata(result: ReplayResult) -> dict[str, Any]:
    """Just the core HistoricalEvent metadata (excluding long text like narrative_md)."""
    e = result.event
    return {
        "event_id": e.event_id,
        "announcement_ts": e.announcement_ts.isoformat(),
        "announcement_source": e.announcement_source,
        "primary_channel": e.primary_channel,
        "primary_symbols": list(e.primary_symbols),
        "secondary_channels": list(e.secondary_channels),
        "insider_likelihood": e.insider_likelihood.value,
        "pre_event_window_minutes": e.pre_event_window_minutes,
        "peak_signal_offset_minutes": e.peak_signal_offset_minutes,
        "profit_estimate_usd": e.profit_estimate_usd,
        "position_size_usd": e.position_size_usd,
        "position_type": e.position_type,
        "notable_pattern": e.notable_pattern,
        "source_path": e.source_path,
    }


def _metrics_dict(result: ReplayResult) -> dict[str, Any]:
    """ReplayMetrics → YAML dict (Enum, datetime → str)."""  # noqa: D401
    m = result.metrics
    return {
        "max_tier_reached": m.max_tier_reached.value,
        "first_alert_ts": m.first_alert_ts.isoformat() if m.first_alert_ts else None,
        "first_alert_tier": m.first_alert_tier.value if m.first_alert_tier else None,
        "detection_latency_s": m.detection_latency_s,
        "warning_time_s": m.warning_time_s,
        "fp_count": m.fp_count,
        "channels_fired": list(m.channels_fired),
        "target_match_score": m.target_match_score,
    }


def _transitions(result: ReplayResult) -> list[dict[str, Any]]:
    """Short list of only the cycles that produced a state_change (the most important info)."""
    out: list[dict[str, Any]] = []
    announce = result.event.announcement_ts
    for m in result.minutes:
        if m.decision is None or m.decision.state_change is None:
            continue
        prev_t, new_t = m.decision.state_change
        out.append(
            {
                "sim_clock": m.sim_clock.isoformat(),
                "offset": _offset_minutes(m.sim_clock, announce),
                "from": prev_t.value,
                "to": new_t.value,
                "delivery_tier": m.decision.delivery_tier.value,
                "rationale": (m.fused_event.rationale if m.fused_event else ""),
            }
        )
    return out


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
def write_yaml_report(
    result: ReplayResult,
    yaml_path: Path,
    *,
    full_timeline: bool = False,
) -> None:
    """ReplayResult → per-event YAML report file.

    Args:
        result: one event replay result.
        yaml_path: output file path (e.g. .../<event_id>/report.yaml).
        full_timeline: True → record every cycle including NORMAL/unchanged.
                       Default False (only changed cycles — for human review).
    """
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    announce = result.event.announcement_ts

    # Only changed cycles in the timeline (or everything if full_timeline=True).
    timeline: list[dict[str, Any]] = []
    prev: ReplayMinute | None = None
    for m in result.minutes:
        if full_timeline or _is_interesting(prev, m):
            timeline.append(_serialize_minute(m, announce))
        prev = m

    payload: dict[str, Any] = {
        "event": _event_metadata(result),
        "replay": {
            "started_at": result.started_at.isoformat(),
            "finished_at": result.finished_at.isoformat(),
            "total_minutes": len(result.minutes),
            "interesting_minutes": len(timeline),
            "detector_config_hash": result.detector_config_hash,
        },
        "metrics": _metrics_dict(result),
        "transitions": _transitions(result),
        "timeline": timeline,
    }

    # atomic write — tmp → rename.
    tmp_path = yaml_path.with_suffix(yaml_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            payload,
            f,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=120,
        )
    tmp_path.replace(yaml_path)

    logger.info(
        "yaml_report: event=%s → %s (%d transitions, %d interesting cycles)",
        result.event.event_id, yaml_path,
        len(payload["transitions"]), len(timeline),
    )


__all__ = ["write_yaml_report"]
