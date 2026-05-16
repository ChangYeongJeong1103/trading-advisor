"""
replay/schemas.py — Pydantic models for the Replay framework (P10.3).

────────────────────────────────────────────────────────────────────────
What this file defines:

    HistoricalEvent     — one data/anomaly/historical_events/<id>.md as a typed object.
    BarTick             — a 1-minute historical bar (channel-agnostic payload).
    ReplayMinute        — one cycle's per-channel signals + fusion + decision result.
    ReplayResult        — one event's full replay timeline + metric summary.
    ReplayMetrics       — detection_latency / warning_time / fp_count / max_tier.
    InsiderLikelihood   — qualitative label enum from the frontmatter.

────────────────────────────────────────────────────────────────────────
Design decisions (why this way):

  · We reuse ChannelSignal / FusedAnomalyEvent / DecisionRecord from
    core/schemas.py as-is. Replay only adds two layers on top of the
    production schema: "event-level metadata" and "minute-level snapshot".

  · Same policy as core/_BaseSchema (frozen=True, extra="forbid"):
    → audit integrity + prevent schema drift.

  · All datetimes are UTC. Naive datetimes raise ValidationError.

  · BarTick.payload is the channel-specific dict, kept as-is. "What is in
    this bar" is each data_source's responsibility. The schema only enforces
    "1-minute granularity + channel + symbol + ts".

────────────────────────────────────────────────────────────────────────
References:
  · docs/p10-replay-framework.md §3.1 (Schema blueprint)
  · src/anomaly/core/schemas.py    (the production schemas we reuse)
  · data/anomaly/historical_events/README.md (event md format)
"""

from __future__ import annotations

# --- standard library ---
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

# --- third-party ---
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- local (reuse production schemas) ---
from ..core.schemas import (
    CHANNEL_CME,
    CHANNEL_HYPERLIQUID,
    CHANNEL_POLYMARKET,
    CHANNEL_X,
    ChannelSignal,
    DecisionRecord,
    FusedAnomalyEvent,
    Tier,
)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _ensure_utc(value: datetime) -> datetime:
    """Raise ValidationError on naive datetime. If tz-aware, convert to UTC.

    Why: replay needs exact times. Naive datetimes are the #1 source of silent
    timezone bugs. Fail-fast is the safe choice.
    """
    if value.tzinfo is None:
        raise ValueError(
            "datetime must be timezone-aware (UTC). "
            "Naive datetimes are rejected to avoid silent timezone bugs."
        )
    return value.astimezone(timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Base config — inherited by every replay schema. Same policy as core/_BaseSchema.
# ─────────────────────────────────────────────────────────────────────
class _ReplayBase(BaseModel):
    """Base class for every replay Pydantic model. Do not use directly."""

    model_config = ConfigDict(
        # Audit integrity — once constructed, instances cannot be modified.
        # Same as core/_BaseSchema.
        frozen=True,
        # Only allow known fields. Catches frontmatter typos immediately.
        extra="forbid",
    )


# ─────────────────────────────────────────────────────────────────────
# 1. InsiderLikelihood — qualitative frontmatter rating (parser normalizes it)
# ─────────────────────────────────────────────────────────────────────
class InsiderLikelihood(str, Enum):
    """Suspicion strength of insider trading for an event (user's qualitative call).

    Not used in replay metric computation itself (FP is judged against the
    narrative ground truth). It is useful for grouping / sorting in reports,
    so we model it as a typed enum.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"
    EXTREME = "extreme"

    @classmethod
    def parse(cls, value: str | None) -> "InsiderLikelihood":
        """Parse a frontmatter string into the enum. Unknown values fall back to MEDIUM (safe)."""
        if value is None:
            return cls.MEDIUM
        # Normalize whitespace / case / hyphens. "Very High" → "very_high".
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        try:
            return cls(normalized)
        except ValueError:
            # Unknown label — warn but keep going with MEDIUM (don't block replay).
            import warnings
            warnings.warn(
                f"Unknown insider_likelihood='{value}', defaulting to MEDIUM",
                stacklevel=2,
            )
            return cls.MEDIUM


# Valid channel names for 4 channels. Same as core/schemas.ALL_CHANNELS but a set is
# more convenient for validation.
_VALID_CHANNELS: frozenset[str] = frozenset(
    {CHANNEL_POLYMARKET, CHANNEL_HYPERLIQUID, CHANNEL_CME, CHANNEL_X}
)


# ─────────────────────────────────────────────────────────────────────
# 2. HistoricalEvent — typed view of one event md
# ─────────────────────────────────────────────────────────────────────
class HistoricalEvent(_ReplayBase):
    """Typed view of one data/anomaly/historical_events/<event_id>.md frontmatter.

    The markdown narrative (the body below the frontmatter) is preserved raw
    in the separate `narrative_md` field — for direct quoting in reports.

    Replay window calculation (not in the parser stage — done via methods here):
      window_start = announcement_ts - max(pre_event_window_minutes, 60)
      window_end   = announcement_ts + post_event_window_minutes
    → even if pre_event_window is too short, guarantee at least 60 minutes of
      baseline lookback.
    """

    # ── required fields (every event md has these) ─────────────────────
    event_id: str = Field(
        description="filename minus .md. e.g. '2025-04-09_liberation_day'",
        min_length=1,
    )
    announcement_ts: datetime = Field(
        description="official announcement time (UTC, tz-aware). Reference point for detection latency."
    )
    announcement_source: str = Field(
        description="one-line source for the announcement. e.g. 'Trump Truth Social post'"
    )
    primary_channel: str = Field(
        description="channel where the strongest signal is expected"
    )
    primary_symbols: list[str] = Field(
        description="main symbol list for this event. CME=['ES','BZ'], Polymarket=[slug], ...",
        min_length=1,
    )
    secondary_channels: list[str] = Field(
        default_factory=list,
        description="supporting channels — included in replay fusion for verification.",
    )
    insider_likelihood: InsiderLikelihood = Field(
        default=InsiderLikelihood.MEDIUM,
        description="user's qualitative rating. For report grouping.",
    )
    pre_event_window_minutes: int = Field(
        ge=1,
        description="max duration (minutes) of suspicious activity preceding the announcement.",
    )
    peak_signal_offset_minutes: int = Field(
        description="time of the strongest signal (minutes; negative=before, positive=after announcement).",
    )
    profit_estimate_usd: float = Field(
        ge=0.0,
        description="estimated insider profit (USD). For report display; not used in replay math.",
    )
    position_size_usd: float = Field(
        ge=0.0,
        description="estimated position size (USD). For report display.",
    )
    position_type: str = Field(
        description="one-line position type. e.g. 'SPY bullish call options (long)'"
    )

    # ── optional fields (only some events have them) ────────────────────
    related_events: list[str] = Field(
        default_factory=list,
        description="related event_id list. For cross-event pattern analysis (e.g. 3 repeated Brent bursts).",
    )
    related_x_status_ids: list[str] = Field(
        default_factory=list,
        description="X status ID list. Used when the data source fetches them.",
    )
    notable_pattern: str | None = Field(
        default=None,
        description="one-line notable pattern. e.g. '1-minute burst, 6× usual volume'.",
    )

    # ── meta populated by the parser (not present in the frontmatter) ──
    source_path: str = Field(
        description="absolute path of the .md file this event was loaded from (for audit / report citation)."
    )
    narrative_md: str = Field(
        default="",
        description="raw text of the markdown body below the frontmatter. For report quoting.",
    )

    # ── default replay window (used unless overridden) ─────────────────
    extra_lookback_minutes: int = Field(
        default=60,
        ge=0,
        description="extra baseline lookback (minutes) when pre_event_window_minutes is too short.",
    )
    post_event_window_minutes: int = Field(
        default=30,
        ge=0,
        description="replay continues N minutes after the announcement (observe post-event tier stabilization).",
    )

    # ────────────── validators ──────────────
    @field_validator("announcement_ts")
    @classmethod
    def _ts_must_be_utc(cls, v: datetime) -> datetime:
        """Require tz-aware."""
        return _ensure_utc(v)

    @field_validator("primary_channel")
    @classmethod
    def _primary_channel_known(cls, v: str) -> str:
        """Must be one of the 4 defined channels."""
        if v not in _VALID_CHANNELS:
            raise ValueError(
                f"primary_channel='{v}' is not one of {sorted(_VALID_CHANNELS)}"
            )
        return v

    @field_validator("secondary_channels")
    @classmethod
    def _secondary_channels_known(cls, v: list[str]) -> list[str]:
        """Every secondary channel must also be a valid channel."""
        for ch in v:
            if ch not in _VALID_CHANNELS:
                raise ValueError(
                    f"secondary_channels contains unknown '{ch}', "
                    f"must be subset of {sorted(_VALID_CHANNELS)}"
                )
        return v

    @model_validator(mode="after")
    def _no_duplicate_channels(self) -> "HistoricalEvent":
        """primary must not also appear in secondary (avoids double-counting in fusion)."""
        if self.primary_channel in self.secondary_channels:
            raise ValueError(
                f"primary_channel '{self.primary_channel}' must not appear in "
                f"secondary_channels {self.secondary_channels}"
            )
        return self

    # ────────────── derived helpers ──────────────
    @property
    def all_channels(self) -> list[str]:
        """primary + secondary combined. Replay runner activates only these channels.

        The remaining channel (whichever of the 4 isn't listed) participates in
        fusion by emitting NORMAL.
        """
        return [self.primary_channel, *self.secondary_channels]

    @property
    def window_start(self) -> datetime:
        """Replay start time. (announcement - max(pre_event, extra_lookback)) UTC.

        If pre_event_window is 7 days (Maduro), lookback is 7 days as-is;
        if it's 18 minutes (Liberation Day), the 60-minute extra_lookback wins.
        """
        lookback_min = max(self.pre_event_window_minutes, self.extra_lookback_minutes)
        return self.announcement_ts - timedelta(minutes=lookback_min)

    @property
    def window_end(self) -> datetime:
        """Replay end time. announcement + post_event_window."""
        return self.announcement_ts + timedelta(minutes=self.post_event_window_minutes)

    @property
    def total_minutes(self) -> int:
        """Total replay cycles (1-minute granularity). Used for progress bar / cost estimate."""
        delta = self.window_end - self.window_start
        return int(delta.total_seconds() // 60)


# ─────────────────────────────────────────────────────────────────────
# 3. BarTick — one minute of historical data (payload varies per channel)
# ─────────────────────────────────────────────────────────────────────
class BarTick(_ReplayBase):
    """One channel's data for one minute. Emitted by the data_source, consumed by the runner.

    The payload shape varies by channel:
      · CME       — {"open": 5610.5, "high": ..., "volume": ..., "trades": [...]}
      · Polymarket— {"trades": [...], "yes_share": 0.07, "vol_usd": 12500}
      · Hyperliquid — {"oi_open": ..., "oi_close": ..., "trades": [...]}
      · X         — {"tweets": [{"id": ..., "text": ..., "ts": ...}, ...]}

    "0 trades in a minute" is possible. In that case payload is empty dict or
    a flat-bar marker. If the data_source returns None, the runner treats it as
    "this minute = no data".
    """

    channel: str = Field(description="polymarket | hyperliquid | cme | x")
    symbol: str = Field(description="symbol covered by this bar. Matches event.primary_symbols.")
    ts: datetime = Field(
        description="bar start time (UTC). The bar covers [ts, ts+bar_seconds)."
    )
    bar_seconds: int = Field(
        default=60,
        ge=1,
        description="bar length (seconds). v0 = 60 fixed. Per-channel override possible later.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="channel-specific raw data. The detector interprets it with its own schema.",
    )

    @field_validator("channel")
    @classmethod
    def _channel_known(cls, v: str) -> str:
        if v not in _VALID_CHANNELS:
            raise ValueError(f"channel='{v}' must be in {sorted(_VALID_CHANNELS)}")
        return v

    @field_validator("ts")
    @classmethod
    def _ts_must_be_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


# ─────────────────────────────────────────────────────────────────────
# 4. ReplayMinute — one cycle's result snapshot
# ─────────────────────────────────────────────────────────────────────
class ReplayMinute(_ReplayBase):
    """One-minute cycle result (per-channel signals + fused + decision).

    The runner emits one of these on every sim_clock tick and appends to
    ReplayResult.minutes. Empty cycles (all channels NORMAL → fused.tier_floor
    = NORMAL) are still recorded for visualization completeness (but decision is
    None — no state change).
    """

    sim_clock: datetime = Field(description="simulator time for this cycle (UTC).")

    # channel name → ChannelSignal | None.
    # None means "this channel's detector did not fire" (treated as NORMAL).
    per_channel_signals: dict[str, ChannelSignal | None] = Field(default_factory=dict)

    fused_event: FusedAnomalyEvent | None = Field(
        default=None,
        description="fusion engine result. Even with all channels NORMAL, one NORMAL fused event is produced.",
    )
    decision: DecisionRecord | None = Field(
        default=None,
        description="only set on cycles where state_manager produced a transition.",
    )

    @field_validator("sim_clock")
    @classmethod
    def _ts_must_be_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


# ─────────────────────────────────────────────────────────────────────
# 5. ReplayMetrics — quantitative metrics for one event (CSV summary columns)
# ─────────────────────────────────────────────────────────────────────
class ReplayMetrics(_ReplayBase):
    """metrics.py fills this once an event's replay completes and attaches it to ReplayResult.

    See docs/p10-replay-framework.md §5 for field definitions.
    """

    max_tier_reached: Tier = Field(description="highest system_state tier reached within the window.")

    # None = no alert (tier ≥ WATCH) was ever emitted during the window.
    first_alert_ts: datetime | None = Field(
        default=None,
        description="sim_clock time of the first WATCH-or-higher alert.",
    )
    first_alert_tier: Tier | None = Field(
        default=None,
        description="tier of that first alert (typically WATCH).",
    )

    # primary metric (goal: median ≤ 60s).
    detection_latency_s: float | None = Field(
        default=None, ge=0.0,
        description="first_alert_ts - first_anomaly_observed_ts (seconds). None=no alert.",
    )

    # informational.
    warning_time_s: float | None = Field(
        default=None,
        description="announcement_ts - first_alert_ts (seconds). positive=before announcement, negative=after.",
    )

    fp_count: int = Field(
        default=0, ge=0,
        description="number of RISK_OFF+ alerts within the window unrelated to ground-truth (= false positives).",
    )

    # Channels that fired within the window (channels emitting only NORMAL are excluded).
    channels_fired: list[str] = Field(
        default_factory=list,
        description="sorted list of channels that emitted WATCH or higher.",
    )

    # 0~1. Actual escalation match vs. the target_tier_timeline (event md narrative).
    # None in v0 stage (the scorer lands in P10.4). Placeholder for now.
    target_match_score: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="actual-escalation agreement vs. the event md target tier timeline [0,1].",
    )


# ─────────────────────────────────────────────────────────────────────
# 6. ReplayResult — full timeline + metrics package for one event
# ─────────────────────────────────────────────────────────────────────
class ReplayResult(_ReplayBase):
    """All results for one event's replay. The reporter consumes this and exports CSV/YAML/PNG.

    Frozen — once built, immutable. The runner collects into a mutable list
    and then freezes it as a ReplayResult in one shot.
    """

    event: HistoricalEvent
    started_at: datetime = Field(description="wall-clock time replay began (audit).")
    finished_at: datetime = Field(description="wall-clock time replay ended (audit).")
    minutes: list[ReplayMinute] = Field(
        description="cycle snapshots sorted in ascending sim_clock order.",
    )
    metrics: ReplayMetrics

    # A trace of which detector / config was used. For comparing multiple replay runs of the same event.
    detector_config_hash: str | None = Field(
        default=None,
        description="SHA256 of DetectorConfig (including overrides). For re-run comparisons.",
    )

    @field_validator("started_at", "finished_at")
    @classmethod
    def _ts_must_be_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)
