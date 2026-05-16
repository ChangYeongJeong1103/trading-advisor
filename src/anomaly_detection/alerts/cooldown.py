"""
alerts/cooldown.py — Per-(channel, symbol, tier) alert cooldown v2 (P10.5 lock).

────────────────────────────────────────────────────────────────────────
Role:
  Single source of truth for the v2 cooldown logic, **shared** between the
  production daemon and the replay reporter, used to prevent alert fatigue.

  P10.5 analysis (user decision locked 2026-04-21):
    · cooldown unit = (channel, symbol, tier) — independent per symbol.
      e.g., a CME BZ EMERGENCY does not silence a CME CL EMERGENCY within 24h.
    · cooldown duration = 24h (= 1440 min). v1's 60min was too short — for
      an oscillating Polymarket event we got 278 alerts in 6 days (≈ 1 per 31 min).
    · downgrade alerts = silent.   tiers below the user's max-seen tier
      within 24h are silent (= demote_silent). "Don't tell me when risk eases" (user).
    · Within the same (ch, sym), a tier escalation passes through.
      (Only when this exact (ch, sym, tier) key is the first time within 24h.)

────────────────────────────────────────────────────────────────────────
Public API:

  · `_decide_emit(sig, channel, last_sent, seen_window, cooldown)` — pure
    decision function. The caller (replay reporter / production dispatcher)
    carries the state dicts (easy to test).

  · `class ChannelAlertCooldown` — stateful wrapper for the production daemon.
    Holds last_sent + seen_window internally; one call to `decide(sig, channel)`
    returns (emit, reason) and updates state automatically.

  · `_LastSent`, `_SeenWindow`, `_evict_expired`, `_max_tier_in_window` —
    helpers that the replay reporter already used. Kept as-is (compatibility).

────────────────────────────────────────────────────────────────────────
Usage 1 — replay reporter (caller-managed state):

    last_sent: dict = {}
    seen_window: dict = {}
    for sig in stream:
        emit, reason = _decide_emit(sig, "cme", last_sent, seen_window,
                                    cooldown=timedelta(minutes=1440))
        if emit:
            ...emit...
            last_sent[(ch, sym, tier)] = _LastSent(ts=sig.ts)
            seen_window.setdefault((ch, sym), deque()).append((sig.ts, tier))

Usage 2 — production daemon (self-managed state):

    cooldown = ChannelAlertCooldown(cooldown_minutes=1440)
    for sig in live_signals:
        emit, reason = cooldown.decide(sig, channel="cme")
        if emit:
            ...dispatch email...
"""

from __future__ import annotations

# ── Standard library ────────────────────────────────────────────────
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque

# ── Local ────────────────────────────────────────────────────────────
from ..core.schemas import ChannelSignal, Tier

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# State containers — module-private helper types.
# ─────────────────────────────────────────────────────────────────────
@dataclass
class _LastSent:
    """Last emit time per (channel, symbol, tier)."""
    ts: datetime


# (channel, symbol) → deque[(emit_ts, emit_tier)]  ← rolling 24h sliding window.
_SeenWindow = Deque[tuple[datetime, Tier]]


def _evict_expired(window: _SeenWindow, cutoff: datetime) -> None:
    """Pop entries from the left of the deque older than cutoff (in-place)."""
    while window and window[0][0] < cutoff:
        window.popleft()


def _max_tier_in_window(window: _SeenWindow) -> Tier | None:
    """Max tier among emits in the window. None if empty."""
    if not window:
        return None
    return max((tier for _, tier in window), key=lambda t: t.rank())


# ─────────────────────────────────────────────────────────────────────
# Pure decision — used by the replay reporter on historical replays.
# ─────────────────────────────────────────────────────────────────────
def _decide_emit(
    sig: ChannelSignal,
    channel: str,
    last_sent: dict[tuple[str, str, Tier], _LastSent],
    seen_window: dict[tuple[str, str], _SeenWindow],
    cooldown: timedelta,
) -> tuple[bool, str]:
    """Should this signal be emitted as an alert? (pure — caller manages state)

    Args:
        sig: ChannelSignal for one (minute × channel).
        channel: channel name (not on sig — caller provides).
        last_sent: dict of (channel, symbol, tier) → last emit time.
        seen_window: dict of (channel, symbol) → deque of (ts, tier) emitted within the last 24h.
        cooldown: minimum re-alert interval for the same (channel, symbol, tier) key.

    Returns:
        (emit, reason_code).
        If emit=True, reason_code is one of: 'initial' / 'escalation(prev=<X>)' /
            'cooldown_expired'.
        If emit=False: 'normal_skip' / 'demote_silent' / 'suppressed_cooldown'.

    Note:
        No side effects. When emit=True, the caller updates last_sent /
        seen_window directly.
    """
    # Rule 1: NORMAL is not an alert.
    if sig.tier == Tier.NORMAL:
        return False, "normal_skip"

    cs_key = (channel, sig.symbol)
    window = seen_window.get(cs_key)

    # Evict expired entries from the window, then compute max tier.
    if window is not None:
        _evict_expired(window, sig.ts - cooldown)
    max_seen = _max_tier_in_window(window) if window else None

    # Rule 2: silent if below the user's max-seen tier within 24h (no downgrade alerts).
    if max_seen is not None and sig.tier.rank() < max_seen.rank():
        return False, "demote_silent"

    # (channel, symbol, tier) cooldown check.
    cst_key = (channel, sig.symbol, sig.tier)
    last = last_sent.get(cst_key)

    if last is None:
        # Rule 3: this exact (ch, sym, tier) is the first time within 24h.
        if max_seen is None:
            # First time even for this (ch, sym) — a truly fresh alert.
            return True, "initial"
        # max_seen < sig.tier (rule 2 already filtered equal/lower) — escalation.
        return True, f"escalation(prev={max_seen.value})"

    # Rule 4: even for the same key, re-emit as a reminder once 24h has passed.
    elapsed = sig.ts - last.ts
    if elapsed >= cooldown:
        return True, "cooldown_expired"

    # Rule 5: otherwise — repeated alert within the same (ch, sym, tier) cooldown.
    return False, "suppressed_cooldown"


# ─────────────────────────────────────────────────────────────────────
# Stateful wrapper — for the production daemon (live in-memory state).
# ─────────────────────────────────────────────────────────────────────
class ChannelAlertCooldown:
    """In-memory cooldown state holder for the production daemon.

    Each fusion-loop cycle in the daemon calls `decide(sig, channel)` once
    per channel signal. It returns (emit, reason) and the internal state
    (last_sent / seen_window) is updated automatically.

    Attributes:
        cooldown: timedelta. Minimum re-alert interval for the same
            (channel, symbol, tier) key. Default 24h (1440 min).

    Note:
        State is lost on restart (in-memory only). If needed, add a layer
        that persists to SignalStore + hydrates on startup (out of scope
        until after P11(b)).
    """

    def __init__(self, cooldown_minutes: int = 1440) -> None:
        """Args:
            cooldown_minutes: default 1440 (24h). Can be reduced for tests.
        """
        self._cooldown: timedelta = timedelta(minutes=cooldown_minutes)
        self._last_sent: dict[tuple[str, str, Tier], _LastSent] = {}
        self._seen_window: dict[tuple[str, str], _SeenWindow] = {}

    @property
    def cooldown_minutes(self) -> int:
        """Return the current cooldown in minutes (for audit / email footer)."""
        return int(self._cooldown.total_seconds() // 60)

    def decide(
        self, sig: ChannelSignal, channel: str,
    ) -> tuple[bool, str]:
        """Should this signal be emitted? When emit=True, internal state is updated automatically.

        Args:
            sig: the channel signal that just arrived (live).
            channel: channel name (not on sig — caller provides).

        Returns:
            (emit, reason_code) — same meaning as `_decide_emit`.
        """
        emit, reason = _decide_emit(
            sig,
            channel=channel,
            last_sent=self._last_sent,
            seen_window=self._seen_window,
            cooldown=self._cooldown,
        )
        if emit:
            cst_key = (channel, sig.symbol, sig.tier)
            self._last_sent[cst_key] = _LastSent(ts=sig.ts)

            cs_key = (channel, sig.symbol)
            window = self._seen_window.setdefault(cs_key, deque())
            window.append((sig.ts, sig.tier))
        return emit, reason

    def snapshot_active_keys(self) -> int:
        """Number of (channel, symbol, tier) keys currently in cooldown (for debug)."""
        return len(self._last_sent)


__all__ = [
    "ChannelAlertCooldown",
    "_LastSent",
    "_SeenWindow",
    "_decide_emit",
    "_evict_expired",
    "_max_tier_in_window",
]
