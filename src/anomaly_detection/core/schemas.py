"""
core/schemas.py — Canonical data contracts (shared by every channel + core).

This file is the system's "common language".
Every Channel ↔ Channel, Channel ↔ Core, and Core ↔ Alert layer message uses
only the schemas defined here. No one passes raw dicts around.

────────────────────────────────────────────────────────────────────────
Design decisions (why Pydantic v2):
  1. Automatic validation → enforce score ∈ [0,1], tier as a valid enum, etc.,
                            via the model definition. Bad data raises
                            ValidationError → fail-fast (§6.4).
  2. Frozen (immutable)   → once a ChannelSignal is emitted it cannot be
                            mutated. Audit integrity.
  3. extra="forbid"       → reject unknown fields. Prevents schema drift.
  4. Free JSON serialize  → SQLite/Parquet writes, email body generation, and
                            log output all just call .model_dump_json().
  5. Cross-reference id   → every model has a uuid4 `id`, so FusedAnomalyEvent
                            can carry only the ids of multiple ChannelSignals
                            (saves memory).

Schema catalog (architecture §4.1):
  RawEvent           — raw payload as sent by the source
  NormalizedEvent    — the channel normalizer's unified-schema output
  FeatureSnapshot    — feature engine output (z-score, percentile, etc.)
  ChannelSignal      — one channel's verdict (score + tier + direction + reason)
  FusedAnomalyEvent  — multi-channel verdict (per_channel_tiers + tier_floor)
  DecisionRecord     — final decision + delivery metadata

Architecture: §4.1 Canonical Types
Plan: §3.1 Goal #3, #4 — fusion + two-stage state I/O schema
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────────────────────────────────────────────────
# Channel name constants — avoid typos. Never write "polymarket" directly
# in code; always use CHANNEL_POLYMARKET. Used uniformly across registry,
# config, fusion, etc.
# ─────────────────────────────────────────────────────────────────────
CHANNEL_POLYMARKET: str = "polymarket"
CHANNEL_HYPERLIQUID: str = "hyperliquid"
CHANNEL_CME: str = "cme"
CHANNEL_X: str = "x"
CHANNEL_TRUTH_SOCIAL: str = "truth_social"

# The five v1 channels — to add a new channel, only this list + registry need updating.
ALL_CHANNELS: tuple[str, ...] = (
    CHANNEL_POLYMARKET,
    CHANNEL_HYPERLIQUID,
    CHANNEL_CME,
    CHANNEL_X,
    CHANNEL_TRUTH_SOCIAL,
)


# ─────────────────────────────────────────────────────────────────────
# Enums — prevent string typos + IDE autocomplete + safe serialization.
# All inherit (str, Enum) so JSON serialization yields a plain string.
# ─────────────────────────────────────────────────────────────────────
class Tier(str, Enum):
    """Four-step risk tier (shared by both channel- and system-level, §5.4).

    Expresses "how defensive should we be right now".
    """

    NORMAL = "NORMAL"
    WATCH = "WATCH"
    RISK_OFF = "RISK_OFF"
    EMERGENCY = "EMERGENCY"

    def rank(self) -> int:
        """Tier as an integer rank. Easier for comparisons / max().

        Returns:
            int: NORMAL=0, WATCH=1, RISK_OFF=2, EMERGENCY=3.
        """
        # dict lookup — the enum declaration order happens to match the semantic
        # rank order, but the explicit dict keeps this safe if the enum order is
        # ever changed.
        return {"NORMAL": 0, "WATCH": 1, "RISK_OFF": 2, "EMERGENCY": 3}[self.value]

    @classmethod
    def max_of(cls, tiers: list["Tier"]) -> "Tier":
        """Return the highest (most dangerous) tier in the list. Fusion engine's
        max-tier-wins.

        Args:
            tiers: tier list. If empty, returns NORMAL.

        Returns:
            Tier: the top rank.
        """
        if not tiers:
            return cls.NORMAL
        return max(tiers, key=lambda t: t.rank())


class Direction(str, Enum):
    """Price/position direction. Used by the corroboration agreement check (§5.2)."""

    UP = "up"          # long pressure / yes betting / upward price pressure
    DOWN = "down"      # short pressure / no betting / downward price pressure
    NEUTRAL = "neutral"


class Side(str, Enum):
    """The side of a single fill. Each channel expresses it differently, so we union.

    NormalizedEvent uses this unified vocabulary.
    """

    BUY = "buy"        # ordinary buy
    SELL = "sell"      # ordinary sell
    YES = "yes"        # Polymarket "yes" baking
    NO = "no"          # Polymarket "no" baking
    LONG = "long"      # Perp long open
    SHORT = "short"    # Perp short open


class Source(str, Enum):
    """Which channel transport this RawEvent arrived on."""

    WS = "ws"            # WebSocket subscribe (Polymarket, Hyperliquid realtime)
    REST = "rest"        # REST polling (Unusual Whales, etc.)
    WEBHOOK = "webhook"  # Inbound HTTP push (TradingView)
    SCRAPE = "scrape"    # snscrape (X)
    DUNE = "dune"        # Dune Analytics SQL backfill


class DeliveryTier(str, Enum):
    """Alert delivery strength. AlertRouter uses this to pick the channel (§6.5.1)."""

    NONE = "none"          # do not send (NORMAL)
    DIGEST = "digest"      # WATCH — bundled into the 06:00 Bay Area daily digest
    REALTIME = "realtime"  # RISK_OFF — immediate email
    URGENT = "urgent"      # EMERGENCY — immediate email + Telegram


class RecommendedAction(str, Enum):
    """Recommended action to show the user (§5.5).

    The one-line text the human reads is generated in decision_policy; here we
    only model the category.
    """

    NO_ACTION = "no_action"
    MONITOR = "monitor"           # WATCH — increase monitoring
    REDUCE_RISK = "reduce_risk"   # RISK_OFF — stop new entries, reduce leverage
    EXIT_OR_HEDGE = "exit_or_hedge"  # EMERGENCY — de-risk / hedge immediately


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _now_utc() -> datetime:
    """Current UTC datetime — default for every timestamp. (architecture §4.3 — all UTC)"""
    return datetime.now(timezone.utc)


def _new_id() -> str:
    """uuid4 hex (32 chars) — the unique id used by every schema."""
    return uuid.uuid4().hex


# ─────────────────────────────────────────────────────────────────────
# Base config — shared settings every schema inherits.
#
# frozen=True            → instances are immutable post-construction (audit safety)
# extra="forbid"         → unknown fields raise ValidationError (fail-fast)
# validate_assignment    → meaningless with frozen=True but kept explicit
# ser_json_timedelta     → ISO 8601 (Parquet/SQLite/JSON compatible)
# ─────────────────────────────────────────────────────────────────────
class _BaseSchema(BaseModel):
    """Base class for every anomaly schema. Do not use directly."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
    )


# ─────────────────────────────────────────────────────────────────────
# 1. RawEvent — source's raw payload
# ─────────────────────────────────────────────────────────────────────
class RawEvent(_BaseSchema):
    """Raw payload as sent by the source. Appended as-is to raw_store.

    Used for replay / debugging / audit. Payload shape varies by source, hence dict.
    """

    id: str = Field(default_factory=_new_id, description="uuid4 hex")
    channel: str = Field(description="polymarket | hyperliquid | cme | x")
    source: Source = Field(description="which transport it arrived through")
    symbol: str = Field(description='e.g. "BTC-PERP", "CL", "Yes:Iran-strike-by-Feb28"')
    ts_source: datetime = Field(description="event time as reported by the source (UTC)")
    ts_ingest: datetime = Field(default_factory=_now_utc, description="time we received it")
    payload: dict[str, Any] = Field(description="source payload as-is (varies by channel)")


# ─────────────────────────────────────────────────────────────────────
# 2. NormalizedEvent — unified schema
# ─────────────────────────────────────────────────────────────────────
class NormalizedEvent(_BaseSchema):
    """A RawEvent converted to the unified shape by the channel normalizer.

    From this point on every channel emits events of the same shape →
    the feature engine processes them consistently.
    """

    id: str = Field(default_factory=_new_id)
    channel: str
    symbol: str
    ts_source: datetime
    ts_ingest: datetime
    side: Side | None = Field(
        default=None,
        description="None means not a sided trade (e.g. market metadata update)",
    )
    size_usd: float = Field(ge=0.0, description="trade size in USD")
    price: float | None = Field(default=None, description="fill price (if available)")
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="channel-specific extras (wallet, leverage, account_age, etc.)",
    )
    raw_ref: str = Field(description="originating RawEvent.id — for tracing")


# ─────────────────────────────────────────────────────────────────────
# 3. FeatureSnapshot — feature engine output
# ─────────────────────────────────────────────────────────────────────
class FeatureSnapshot(_BaseSchema):
    """Rolling-window-based feature stats. Input to the detectors.

    Keys in the `features` dict are channel-specific, but the schema is shared.
    (e.g. polymarket → "vol_zscore_5min", "prob_jump_1min")
    (e.g. cme       → "vol_zscore_1min", "oi_delta_5min", "options_sweep_premium")
    """

    id: str = Field(default_factory=_new_id)
    channel: str
    symbol: str
    ts: datetime = Field(default_factory=_now_utc)
    features: dict[str, float] = Field(description="feature_name → value")
    baseline_ref: str = Field(
        description="identifier of the baseline used (e.g. 'polymarket:CL:30d:v1') — reproducible"
    )


# ─────────────────────────────────────────────────────────────────────
# 4. ChannelSignal — a channel's verdict
# ─────────────────────────────────────────────────────────────────────
class ChannelSignal(_BaseSchema):
    """The verdict emitted by one channel (architecture §5.4.1).

    Why both score and tier:
      - tier   → primary input to system_state (max-tier-wins, §5.4.2)
      - score  → input to fused_score (reference/audit/boost assist)

    P9 score normalization note (architecture §5.2):
      In v1, score is in raw detector units. Internally each channel maps it
      step-wise into [0,1]. Proper normalization (empirical CDF, etc.) lands
      in P9.
    """

    id: str = Field(default_factory=_new_id)
    channel: str
    symbol: str
    ts: datetime = Field(default_factory=_now_utc)

    score: float = Field(ge=0.0, le=1.0, description="this channel's anomaly strength [0,1]")
    tier: Tier = Field(description="this channel's own tier decision")
    direction: Direction = Field(default=Direction.NEUTRAL, description="price pressure direction")
    confidence: float = Field(
        ge=0.0, le=1.0, default=1.0,
        description="self confidence — lower with few samples / incomplete data",
    )

    features_ref: str | None = Field(
        default=None, description="FeatureSnapshot.id used to produce this signal"
    )
    fired_detectors: list[str] = Field(
        default_factory=list,
        description="internal detectors that fired (e.g. ['vol_z_v1', 'prob_jump_v1'])",
    )
    reason_codes: list[str] = Field(
        default_factory=list,
        description='human-readable codes (e.g. ["VOL_SPIKE_8x", "OB_IMBAL_+0.7"])',
    )


# ─────────────────────────────────────────────────────────────────────
# 5. FusedAnomalyEvent — multi-channel aggregation
# ─────────────────────────────────────────────────────────────────────
class FusedAnomalyEvent(_BaseSchema):
    """The fusion engine's aggregated result across every ChannelSignal
    (architecture §5.2).

    Key fields:
      tier_floor       — max(per_channel_tiers.values()) — primary input to system_state
      boost_applied    — reason boost was applied (None if not)
      state            — final system tier after tier_floor + boost
      fused_score      — noisy-OR result. Reference/audit/boost assist (secondary)
    """

    id: str = Field(default_factory=_new_id)
    ts: datetime = Field(default_factory=_now_utc)

    fused_score: float = Field(
        ge=0.0, le=1.0,
        description="noisy-OR (1 - Π(1 - score_i × weight_i)). secondary, for reference/audit",
    )
    state: Tier = Field(description="final system tier after tier_floor + boost")

    per_channel_scores: dict[str, float] = Field(description="channel → score [0,1]")
    per_channel_tiers: dict[str, Tier] = Field(description="channel → own tier (primary input to system state)")
    per_channel_signal: dict[str, str | None] = Field(
        description="channel → ChannelSignal.id (None = no signal from this channel)"
    )

    tier_floor: Tier = Field(description="max(per_channel_tiers.values()) — base before boost")
    boost_applied: str | None = Field(
        default=None,
        description='boost reason (e.g. "WATCH→RISK_OFF (corroboration: polymarket+cme up)")',
    )

    contributing: list[str] = Field(
        default_factory=list,
        description="list of ChannelSignal.id whose tier > NORMAL",
    )
    agreeing_channels: int = Field(
        default=0, ge=0, description="number of channels agreeing in direction at WATCH+"
    )
    agreeing_direction: Direction | None = Field(
        default=None, description="the agreed direction (None if no agreement)"
    )
    weights: dict[str, float] = Field(
        default_factory=dict,
        description="weight applied to each channel (health × config weight)",
    )
    rationale: str = Field(default="", description="one-line human-readable explanation")


# ─────────────────────────────────────────────────────────────────────
# 6. DecisionRecord — final decision + delivery metadata
# ─────────────────────────────────────────────────────────────────────
class DecisionRecord(_BaseSchema):
    """Final decision after passing through decision policy + state manager
    (architecture §5.5).

    If state_change is None → "no change" → no alert is sent.
    If state_change is set → push candidate → router dispatches if throttle passes.
    """

    id: str = Field(default_factory=_new_id)
    ts: datetime = Field(default_factory=_now_utc)
    fused_event_ref: str = Field(description="the FusedAnomalyEvent.id that triggered this decision")
    recommended_action: RecommendedAction
    policy_version: str = Field(description='e.g. "v0.1-baseline" (bumped in P9)')
    notes: str = Field(default="")

    # ── delivery / notification metadata ──
    state_change: tuple[Tier, Tier] | None = Field(
        default=None,
        description="(prev_state, new_state). None = no change → no alert",
    )
    delivery_tier: DeliveryTier = Field(
        default=DeliveryTier.NONE,
        description="determines the alert router's dispatch strength",
    )
    delivery_channels: list[str] = Field(
        default_factory=list,
        description='channels actually used to send (e.g. ["email", "telegram"])',
    )
    cooldown_until: datetime | None = Field(
        default=None, description="time after which the same symbol may be re-sent"
    )
    external_links: dict[str, str] = Field(
        default_factory=dict,
        description="visual links embedded in email/telegram body (channel → URL)",
    )
