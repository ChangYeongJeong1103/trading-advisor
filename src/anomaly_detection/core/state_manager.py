"""
core/state_manager.py — Hysteresis-based system state transitions (architecture §5.4.3).

────────────────────────────────────────────────────────────────────────
Role:
  Each fusion cycle delivers a new FusedAnomalyEvent.state.
  The same state must persist for the dwell time before we finalize the transition.
  → Prevents alert flapping from a one-cycle spike (architecture §5.4.3).

  Pure logic. Only state is kept in memory. Storage / alerts are the caller's
  responsibility.

────────────────────────────────────────────────────────────────────────
Dwell time (tier-asymmetric, architecture §5.4.3 decision):

  Escalation (lower → higher tier): fast — recognize danger quickly
    - Enter WATCH:      hold 60s
    - Enter RISK_OFF:   30s
    - Enter EMERGENCY:  10s   (shortest — critical alert)

  De-escalation (higher → lower tier): slow — anti-flap
    - Leave WATCH:      180s
    - Leave RISK_OFF:   300s
    - Leave EMERGENCY:  600s  (longest — once we hit emergency, cool down properly)

  Values are tuned in P9. The v0 defaults are the architecture doc recommendations.

────────────────────────────────────────────────────────────────────────
Flap prevention mechanism (concrete):

  current=NORMAL with WATCH for just 1 cycle then back to NORMAL →
  the pending candidate (WATCH) disappears next cycle → no transition.

  current=NORMAL → WATCH for 5s → NORMAL → WATCH for 5s ...
  repeating? Before the WATCH dwell (60s) is reached we keep resetting → no
  transition.
  In that case the user sees "blinking but no alert" — that is the intended
  behavior.

────────────────────────────────────────────────────────────────────────
Intermediate flip:

  current=NORMAL, WATCH for 5s, then EMERGENCY appears?
  → pending candidate updates WATCH→EMERGENCY, dwell counter restarts from 0.
  → If EMERGENCY holds for 10s, jump NORMAL→EMERGENCY directly (skip WATCH).

  Why this is correct:
    - WATCH and EMERGENCY are different candidates. Counts are not summed.
    - EMERGENCY's own dwell is 10s, so it triggers quickly (this is fine).

Architecture: §5.4.3 Hysteresis, §6.6 Failure mode (anti-flapping)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock

from .schemas import FusedAnomalyEvent, Tier

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Config — tier-asymmetric dwell times
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HysteresisConfig:
    """Dwell-time configuration (seconds). Tier-asymmetric — fast to escalate, slow to de-escalate.

    Args:
        escalate_dwell_s: target tier (= the higher tier being entered) → it
            must hold for N seconds for the escalation to be confirmed.
        deescalate_dwell_s: source tier (= the higher tier being left) → a
            lower signal must hold for N seconds for the de-escalation to be confirmed.
        # NORMAL is never a key in either side:
        #   - escalation targets are always WATCH+ (entering NORMAL = de-escalation)
        #   - de-escalation sources are also WATCH+ (leaving NORMAL = escalation)
    """

    escalate_dwell_s: dict[Tier, float] = field(
        default_factory=lambda: {
            Tier.WATCH: 60.0,       # NORMAL → WATCH (or any tier → WATCH)
            Tier.RISK_OFF: 30.0,    # → RISK_OFF
            Tier.EMERGENCY: 10.0,   # → EMERGENCY (shortest)
        }
    )
    deescalate_dwell_s: dict[Tier, float] = field(
        default_factory=lambda: {
            Tier.WATCH: 180.0,      # WATCH → (lower) — 3 min
            Tier.RISK_OFF: 300.0,   # RISK_OFF → (lower) — 5 min
            Tier.EMERGENCY: 600.0,  # EMERGENCY → (lower) — 10 min
        }
    )

    def required_dwell(self, from_tier: Tier, to_tier: Tier) -> float:
        """How many seconds to_tier must hold to confirm the from→to transition.

        Raises:
            ValueError: from == to (not a transition — caller bug).
        """
        if from_tier == to_tier:
            raise ValueError(f"from == to ({from_tier}) — not a transition")

        if to_tier.rank() > from_tier.rank():
            # Escalation — dwell of the target tier.
            return self.escalate_dwell_s[to_tier]
        # De-escalation — dwell of the source (leaving) tier.
        return self.deescalate_dwell_s[from_tier]


# ─────────────────────────────────────────────────────────────────────
# StateManager
# ─────────────────────────────────────────────────────────────────────
class StateManager:
    """Track current system state + pending candidate and confirm transitions.

    Thread-safety: a Lock protects observe(). When the orchestrator calls it
    from a single task, the lock is essentially uncontended (overhead is
    negligible).
    """

    def __init__(
        self,
        config: HysteresisConfig | None = None,
        initial_state: Tier = Tier.NORMAL,
    ) -> None:
        self._cfg = config or HysteresisConfig()
        self._current: Tier = initial_state
        self._pending: Tier | None = None        # Next candidate (when different from current).
        self._pending_since: datetime | None = None
        self._lock = Lock()

    # ─────────────────────────────────────────────────────────────────
    # Core — take one fusion result and decide on a transition.
    # ─────────────────────────────────────────────────────────────────
    def observe(
        self,
        event: FusedAnomalyEvent,
        now: datetime | None = None,
    ) -> tuple[Tier, Tier] | None:
        """Look at a new fusion result and decide whether a transition is confirmed.

        Args:
            event: the result FusionEngine just produced.
            now: time of this observation (UTC). Defaults to event.ts.

        Returns:
            (old, new) tuple — transition confirmed (decision policy fires the alert).
            None — no change (current_state stays).

        Side effects:
            self._current / self._pending / self._pending_since are updated.
        """
        observed = event.state
        now = now or event.ts

        with self._lock:
            # Case A: observation matches current
            #   → pending candidate (if any) is gone (anti-flap). Reset the counter.
            if observed == self._current:
                if self._pending is not None:
                    logger.debug(
                        "StateManager: pending %s cleared (back to current %s)",
                        self._pending.value, self._current.value,
                    )
                self._pending = None
                self._pending_since = None
                return None

            # Case B: observation differs from current — transition candidate.
            if self._pending != observed:
                # New candidate (or candidate change) — reset the counter.
                if self._pending is not None and self._pending != observed:
                    logger.debug(
                        "StateManager: pending changed %s → %s (counter reset)",
                        self._pending.value, observed.value,
                    )
                self._pending = observed
                self._pending_since = now
                return None

            # Case C: same candidate again → check whether dwell is satisfied.
            assert self._pending_since is not None  # type narrow
            elapsed_s = (now - self._pending_since).total_seconds()
            required_s = self._cfg.required_dwell(self._current, observed)

            if elapsed_s < required_s:
                logger.debug(
                    "StateManager: pending %s holding (%.1fs / %.1fs)",
                    observed.value, elapsed_s, required_s,
                )
                return None

            # Confirmed — transition!
            old = self._current
            self._current = observed
            self._pending = None
            self._pending_since = None
            logger.info(
                "StateManager: TRANSITION %s → %s (held %.1fs ≥ %.1fs required)",
                old.value, observed.value, elapsed_s, required_s,
            )
            return (old, observed)

    # ─────────────────────────────────────────────────────────────────
    # Read-only inspection (health check / debug)
    # ─────────────────────────────────────────────────────────────────
    @property
    def current_state(self) -> Tier:
        return self._current

    @property
    def pending(self) -> tuple[Tier, datetime] | None:
        """The current pending candidate + when we first saw it. None if absent."""
        with self._lock:
            if self._pending is None or self._pending_since is None:
                return None
            return (self._pending, self._pending_since)

    def snapshot(self) -> dict:
        """Status summary for debug / health."""
        with self._lock:
            return {
                "current_state": self._current.value,
                "pending": self._pending.value if self._pending else None,
                "pending_since_iso": (
                    self._pending_since.isoformat() if self._pending_since else None
                ),
            }

    # ─────────────────────────────────────────────────────────────────
    # Emergency — external force reset
    # ─────────────────────────────────────────────────────────────────
    def force_reset(self, to_state: Tier = Tier.NORMAL) -> None:
        """Force-clear every internal state. Use on daemon restart / kill-switch.

        Note: does not emit a transition event (silent reset).
        """
        with self._lock:
            logger.warning(
                "StateManager: FORCE RESET %s → %s",
                self._current.value, to_state.value,
            )
            self._current = to_state
            self._pending = None
            self._pending_since = None


__all__ = ["HysteresisConfig", "StateManager"]
