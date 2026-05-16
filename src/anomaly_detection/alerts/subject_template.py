"""
alerts/subject_template.py — Channel emoji + tier prefix for the email
subject + format lock (user decision 2026-04-21).

────────────────────────────────────────────────────────────────────────
Role:
  Unify emoji + format so that a single email subject visually identifies
  "which channel × which symbol → which tier" at a glance.

  Both the production renderer (alerts/renderer/email.py) and the prototype
  (replay reporters) import the same mapping from here.

────────────────────────────────────────────────────────────────────────
Locked mapping (user decision):

  channel emoji (middle of the subject — "which channel"):
    cme         📊
    polymarket  🔷
    hyperliquid 🟡
    x           𝕏

  tier prefix (front of the subject — "how dangerous" visual cue):
    EMERGENCY   🚨🚨   (double siren — demands immediate action)
    RISK_OFF    ⚠️     (single warning)
    WATCH       📋     (clipboard — digest-style, batched normally)

  ※ The earlier v1 WATCH = 📊 collided with CME's channel emoji, so it
    was changed to 📋. This way a CME EMERGENCY and an X-channel WATCH
    digest are immediately distinguishable in the inbox.

────────────────────────────────────────────────────────────────────────
Subject format:

  alert (per-channel):
    "{tier_prefix} {channel_emoji} {CHANNEL_SHORT} · {symbol} → {TIER}"
    e.g. "🚨🚨 📊 CME · BZ → EMERGENCY"
         "⚠️ 🔷 POLY · maduro → RISK_OFF"
         "🚨🚨 🟡 HL · BTC → EMERGENCY"

  watch digest (multiple symbols bundled):
    "{tier_prefix} {channel_emoji} {CHANNEL_SHORT} digest · {N} symbols"
    e.g. "📋 📊 CME digest · 3 symbols"

  The cross-tag suffix is appended separately in production (architecture §6.5.5).

────────────────────────────────────────────────────────────────────────
Usage:

    from anomaly_detection.alerts.subject_template import build_alert_subject

    subj = build_alert_subject(
        channel="cme", symbol="BZ", tier=Tier.EMERGENCY,
    )
    # → "🚨🚨 📊 CME · BZ → EMERGENCY"

  Long or whitespace-bearing symbols are passed through as-is (no truncation —
  Gmail subjects are fine up to ~78 chars).
"""

from __future__ import annotations

from ..core.schemas import (
    CHANNEL_CME,
    CHANNEL_HYPERLIQUID,
    CHANNEL_POLYMARKET,
    CHANNEL_TRUTH_SOCIAL,
    CHANNEL_X,
    Tier,
)

# ─────────────────────────────────────────────────────────────────────
# Locked mapping (user decision — also update docs/anomaly-upgrade-plan when changing).
# ─────────────────────────────────────────────────────────────────────

# channel → single-emoji visual identifier.
# truth_social — user decision (final 2026-05-15): drop the emoji entirely
# and identify with the short "Truthsocial" wordmark only.
# "🚨🚨 Truthsocial · {topic} → EMERGENCY"
CHANNEL_EMOJI: dict[str, str] = {
    CHANNEL_CME:           "📊",
    CHANNEL_POLYMARKET:    "🔷",
    CHANNEL_HYPERLIQUID:   "🟡",
    CHANNEL_X:             "𝕏",
    CHANNEL_TRUTH_SOCIAL:  "",
}

# Channel short alias (compact identifier inside the subject).
# User decision (2026-04-21): for the X channel, the 𝕏 emoji is itself the
# brand mark, so a separate text label looks redundant → leave empty and
# the builder skips it.
# Truth Social brand recognition is not as strong as X, so keep the
# "Truthsocial" text alongside (user decision 2026-05-15).
CHANNEL_SHORT: dict[str, str] = {
    CHANNEL_CME:           "CME",
    CHANNEL_POLYMARKET:    "POLY",
    CHANNEL_HYPERLIQUID:   "HL",
    CHANNEL_X:             "",
    CHANNEL_TRUTH_SOCIAL:  "Truthsocial",
}

# tier → prefix prepended to the subject.
TIER_PREFIX: dict[Tier, str] = {
    Tier.EMERGENCY: "🚨🚨",
    Tier.RISK_OFF:  "⚠️",
    Tier.WATCH:     "📋",
    # NORMAL has no alert at all (channel_alerts.py's normal_skip).
}


# ─────────────────────────────────────────────────────────────────────
# Builders — imported by both production renderer and prototype.
# ─────────────────────────────────────────────────────────────────────
def channel_emoji(channel: str) -> str:
    """Channel name → emoji. Unknown channels return an empty string (silent skip)."""
    return CHANNEL_EMOJI.get(channel, "")


def channel_short(channel: str) -> str:
    """Channel name → SHORT alias. Unknown falls back to upper(channel)."""
    return CHANNEL_SHORT.get(channel, channel.upper())


def tier_prefix(tier: Tier) -> str:
    """Tier → subject prefix. Empty string for NORMAL (no alerts there)."""
    return TIER_PREFIX.get(tier, "")


def build_alert_subject(
    *,
    channel: str,
    symbol: str,
    tier: Tier,
) -> str:
    """Build the email subject for one alert (channel × symbol × tier).

    Args:
        channel: one of ALL_CHANNELS.
        symbol: symbol at fire time (e.g. "BZ", "BTC", "maduro").
        tier: WATCH | RISK_OFF | EMERGENCY (NORMAL doesn't send alerts).

    Returns:
        Format: "{tier_prefix} {channel_emoji} {CHANNEL_SHORT} · {symbol} → {TIER}"

    Examples:
        >>> build_alert_subject(channel="cme", symbol="BZ", tier=Tier.EMERGENCY)
        '🚨🚨 📊 CME · BZ → EMERGENCY'
        >>> build_alert_subject(channel="polymarket", symbol="maduro", tier=Tier.RISK_OFF)
        '⚠️ 🔷 POLY · maduro → RISK_OFF'
        >>> build_alert_subject(channel="x", symbol="@elonmusk", tier=Tier.WATCH)
        '📋 𝕏 · @elonmusk → WATCH'

    Note:
        When CHANNEL_SHORT is empty (e.g. X channel), the short part is
        skipped and only the emoji acts as the identifier (user decision
        2026-04-21).
    """
    # head is a space-joined (prefix, emoji?, short?). Empty entries are
    # skipped automatically to avoid double spaces / dangling spaces. User
    # decision (2026-05-15): truth_social uses only the "Truthsocial"
    # wordmark, no emoji.
    parts: list[str] = [tier_prefix(tier)]
    emoji = channel_emoji(channel)
    if emoji:
        parts.append(emoji)
    short = channel_short(channel)
    if short:
        parts.append(short)
    head = " ".join(p for p in parts if p)
    return f"{head} · {symbol} → {tier.value}"


def build_watch_digest_subject(
    *,
    channel: str,
    symbol_count: int,
) -> str:
    """Build the email subject for a WATCH digest (multiple symbols bundled).

    Args:
        channel: one of ALL_CHANNELS.
        symbol_count: number of symbols included in the digest.

    Returns:
        Format: "{watch_prefix} {channel_emoji} {CHANNEL_SHORT} digest · {N} symbols"

    Examples:
        >>> build_watch_digest_subject(channel="cme", symbol_count=3)
        '📋 📊 CME digest · 3 symbols'
    """
    return (
        f"{tier_prefix(Tier.WATCH)} "
        f"{channel_emoji(channel)} "
        f"{channel_short(channel)} digest · {symbol_count} symbol"
        f"{'s' if symbol_count != 1 else ''}"
    )


__all__ = [
    "CHANNEL_EMOJI",
    "CHANNEL_SHORT",
    "TIER_PREFIX",
    "channel_emoji",
    "channel_short",
    "tier_prefix",
    "build_alert_subject",
    "build_watch_digest_subject",
]
