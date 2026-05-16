"""
alerts/live_timeline.py — In-memory rolling timeline buffer for the daemon (P11(a).4.2).

────────────────────────────────────────────────────────────────────────
Role:
  The production daemon (orchestrator) pushes one `ReplayMinute` snapshot
  per fusion cycle into this buffer. The buffer holds the last 24h, and
  at the moment an alert fires, `to_replay_result_at(alert_clock)` synthesizes
  a `ReplayResult` object that the plot function can consume.

  Why we do this:
    `email_plot.write_alert_window_plot` and its helpers (`_channel_series`,
    `_system_state_series`, `_tier_floor_series`) are designed to take a
    `ReplayResult` — the same plot code is reused by both replay and production.
    So if we shape the production data into the "ReplayResult" form, the entire
    plot pipeline can be reused.

  Fields the plot actually reads from ReplayResult:
    · result.minutes               — list of ReplayMinute in ascending sim_clock order
    · result.event.announcement_ts — X-axis reference point (set to alert_clock when live)
    · result.event.event_id        — shown in the footer (set to "live")

  Other fields like ReplayMetrics aren't used by the plot, but ReplayResult
  is a frozen pydantic model, so we fill in dummy values. To prevent this
  synthetic object from leaking elsewhere, only the dispatcher uses it.

────────────────────────────────────────────────────────────────────────
Size / RAM:
  daemon cycle = 5s, 24h = 17280 cycles. Each ReplayMinute (with 4 channel
  signals + fused_event) is ~2KB. 24h total ≈ 35MB — easily affordable
  for a single daemon.

  Eviction policy: on each append, popleft heads older than sim_clock - max_age.
"""

from __future__ import annotations

# ── Standard library ────────────────────────────────────────────────
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Deque

# ── Local ────────────────────────────────────────────────────────────
from ..core.schemas import ChannelSignal, DecisionRecord, FusedAnomalyEvent
from ..replay.schemas import (
    HistoricalEvent,
    InsiderLikelihood,
    ReplayMetrics,
    ReplayMinute,
    ReplayResult,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Dummy field values for the synthetic ReplayResult (things the plot does not see).
# When changing these, double-check that the plot code does not reference them.
# ─────────────────────────────────────────────────────────────────────
_LIVE_EVENT_ID: str = "live"
_LIVE_DUMMY_SOURCE: str = "live daemon"
_LIVE_PRIMARY_CHANNEL: str = "cme"   # not read by the plot, just a placeholder.
_LIVE_DUMMY_PATH: str = "<live>"


class LiveTimelineBuffer:
    """Daemon's rolling timeline buffer (in-memory, 24h sliding window).

    Thread safety: a single fusion loop (asyncio task) does both append and
    read, so no extra lock is needed. If another task wants to read, the
    caller must synchronize.

    Attributes:
        max_age: timedelta. Retention window (default 24h).
    """

    def __init__(self, max_age_hours: int = 24) -> None:
        """Args:
            max_age_hours: hours of recent history kept in the deque (default 24).
        """
        self._buf: Deque[ReplayMinute] = deque()
        self._max_age: timedelta = timedelta(hours=max_age_hours)

    @property
    def size(self) -> int:
        """Number of snapshots currently in the buffer."""
        return len(self._buf)

    @property
    def max_age_hours(self) -> int:
        """Retention window (in hours)."""
        return int(self._max_age.total_seconds() // 3600)

    # ─────────────────────────────────────────────────────────────────
    # Producer side — called every cycle by the orchestrator.
    # ─────────────────────────────────────────────────────────────────
    def append(
        self,
        sim_clock: datetime,
        per_channel_signals: dict[str, ChannelSignal | None],
        fused_event: FusedAnomalyEvent | None = None,
        decision: DecisionRecord | None = None,
    ) -> None:
        """Append one cycle snapshot to the buffer + evict expired entries.

        Args:
            sim_clock: this cycle's time (UTC, tz-aware). In production this is
                usually `datetime.now(tz=UTC)`.
            per_channel_signals: result of registry.snapshot_signals().
            fused_event: fusion engine result (optional). Used by the plot's system_state lane.
            decision: only on a state_manager transition (optional).
        """
        if sim_clock.tzinfo is None:
            # ReplayMinute schema requires UTC — fail-fast.
            raise ValueError("sim_clock must be tz-aware (UTC)")

        rm = ReplayMinute(
            sim_clock=sim_clock,
            per_channel_signals=per_channel_signals,
            fused_event=fused_event,
            decision=decision,
        )
        self._buf.append(rm)

        # Evict expired (head from left).
        cutoff = sim_clock - self._max_age
        while self._buf and self._buf[0].sim_clock < cutoff:
            self._buf.popleft()

    # ─────────────────────────────────────────────────────────────────
    # Consumer side — the dispatcher calls once just before an alert fires.
    # ─────────────────────────────────────────────────────────────────
    def to_replay_result_at(
        self,
        alert_clock: datetime,
    ) -> ReplayResult:
        """Wrap the current buffer snapshots into a synthetic `ReplayResult`.

        Args:
            alert_clock: the moment the alert fired — set as announcement_ts on
                the synthetic event. Used as the plot function's X-axis reference (T = 0).

        Returns:
            ReplayResult — directly consumable by `email_plot.write_alert_window_plot`.
            `minutes` is a list view of the buffer snapshots (not a deepcopy);
            schemas are frozen, so external code cannot mutate them.

        Note:
            ReplayResult / HistoricalEvent are frozen pydantic, but in production
            there is no .md file, so all required fields are filled with dummies.
            `model_construct` bypasses validation on a frozen model — we trade a
            little safety for simplicity. Don't expose externally (use only inside
            the dispatcher).
        """
        if alert_clock.tzinfo is None:
            raise ValueError("alert_clock must be tz-aware (UTC)")

        # deque → list (ReplayResult.minutes requires a list; the contents are frozen).
        minutes: list[ReplayMinute] = list(self._buf)

        # Synthetic HistoricalEvent — fill only required fields. Use model_construct
        # to bypass the validator (safe for frozen models).
        synthetic_event = HistoricalEvent.model_construct(
            event_id=_LIVE_EVENT_ID,
            announcement_ts=alert_clock,
            announcement_source=_LIVE_DUMMY_SOURCE,
            primary_channel=_LIVE_PRIMARY_CHANNEL,
            primary_symbols=["live"],
            secondary_channels=[],
            insider_likelihood=InsiderLikelihood.MEDIUM,
            pre_event_window_minutes=self.max_age_hours * 60,
            peak_signal_offset_minutes=0,
            profit_estimate_usd=0.0,
            position_size_usd=0.0,
            position_type="live",
            related_events=[],
            related_x_status_ids=[],
            notable_pattern=None,
            source_path=_LIVE_DUMMY_PATH,
            narrative_md="",
            extra_lookback_minutes=60,
            post_event_window_minutes=0,
        )

        # Synthetic ReplayMetrics — not read by the plot; just placeholder values.
        # max_tier_reached is taken from the most recent fused_event's state (NORMAL if none).
        from ..core.schemas import Tier
        last_state: Tier = Tier.NORMAL
        for m in reversed(minutes):
            if m.fused_event is not None:
                last_state = m.fused_event.state
                break

        synthetic_metrics = ReplayMetrics.model_construct(
            max_tier_reached=last_state,
            first_alert_ts=alert_clock,
            first_alert_tier=None,
            detection_latency_s=None,
            warning_time_s=None,
            fp_count=0,
            channels_fired=[],
            target_match_score=None,
        )

        return ReplayResult.model_construct(
            event=synthetic_event,
            started_at=(minutes[0].sim_clock if minutes else alert_clock),
            finished_at=alert_clock,
            minutes=minutes,
            metrics=synthetic_metrics,
            detector_config_hash=None,
        )


__all__ = ["LiveTimelineBuffer"]
