"""
alerts/throttle.py — Dedup + cooldown + WATCH digest queue (architecture §6.5.3).

────────────────────────────────────────────────────────────────────────
Role:
  The router calls should_send for every decision. Send/skip is decided
  by these policies:

  1) state-change-only — if state_change=None, do not send (audit only)
       (decision_policy already produces NONE delivery, but throttle is
        an additional safeguard)

  2) DIGEST tier (WATCH) — do not send immediately, accumulate via
       queue_for_digest. The router calls flush_digest at 06:00 Bay Area
       daily and bundles them into a single message.

  3) same-symbol cooldown — if the same (symbol, target_tier) pair fires
       again within the cooldown, drop it (default 5 min — alerts.cooldown_minutes)

  4) Otherwise (REALTIME, URGENT) → return True → router dispatches immediately

────────────────────────────────────────────────────────────────────────
v0 simplifications (refined in P9):
  - 5-min batch (group multiple symbols) — TODO P9
  - EMERGENCY heartbeat (1h reminder) — handled by router as a separate task
  - Adaptive cooldown — TODO P9

  Symbol extraction: not on DecisionRecord directly, so the caller (router)
  passes primary_symbol as an argument. The router is responsible for picking
  the symbol from the highest-tier contributing signal in fused_event.

────────────────────────────────────────────────────────────────────────
Thread/concurrency:
  The daemon's fusion loop is a single asyncio task, so races are rare.
  But digest flush may run as a separate task, so safety is guaranteed
  with a threading.Lock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

from ..core.schemas import DecisionRecord, DeliveryTier, FusedAnomalyEvent, Tier

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ThrottleConfig:
    """Throttle policy parameters (defaults are recommended values from architecture §6.5.3)."""
    cooldown_minutes: int = 5
    debounce_seconds: int = 1  # 24h safeguard (architecture §6.5.3)


# ─────────────────────────────────────────────────────────────────────
# Internal: digest queue entry
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DigestEntry:
    """One entry accumulated in the WATCH digest queue."""
    decision: DecisionRecord
    fused_event: FusedAnomalyEvent
    primary_symbol: str
    queued_at: datetime


# ─────────────────────────────────────────────────────────────────────
# AlertThrottle
# ─────────────────────────────────────────────────────────────────────
class AlertThrottle:
    """Decide send/skip + accumulate WATCH digest."""

    def __init__(self, config: ThrottleConfig | None = None) -> None:
        self._cfg = config or ThrottleConfig()
        # (symbol, target_tier) → last sent time (UTC).
        self._last_sent: dict[tuple[str, Tier], datetime] = {}
        # WATCH (DIGEST) accumulation queue. Cleared on flush_digest.
        self._digest_queue: list[DigestEntry] = []
        self._lock = Lock()

    # ─────────────────────────────────────────────────────────────────
    # Core: should we send?
    # ─────────────────────────────────────────────────────────────────
    def should_send(
        self,
        decision: DecisionRecord,
        primary_symbol: str,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """Should this decision be sent right now?

        Args:
            decision: DecisionRecord.
            primary_symbol: symbol portion of the cooldown key (chosen by the router).
            now: current time (UTC). Defaults to datetime.now(UTC).

        Returns:
            (send, reason):
                send=True  → router dispatches
                send=False → drop (or moved into digest queue)
                reason     → human-readable rationale (audit/log).
        """
        now = now or datetime.now(timezone.utc)

        # 1) state-change-only
        if decision.state_change is None:
            return False, "no state change"

        # 2) DIGEST → into the queue (no immediate send)
        if decision.delivery_tier == DeliveryTier.DIGEST:
            return False, "DIGEST tier → queued for daily digest"

        # 3) NONE — defensive guard
        if decision.delivery_tier == DeliveryTier.NONE:
            return False, "delivery_tier=NONE"

        # 4) cooldown / debounce check
        target_tier = decision.state_change[1]
        key = (primary_symbol, target_tier)

        with self._lock:
            last = self._last_sent.get(key)
            if last is not None:
                gap = now - last
                # debounce safeguard (1 second)
                if gap < timedelta(seconds=self._cfg.debounce_seconds):
                    return False, f"debounced ({gap.total_seconds():.2f}s < {self._cfg.debounce_seconds}s)"
                # cooldown
                cooldown = timedelta(minutes=self._cfg.cooldown_minutes)
                if gap < cooldown:
                    remaining = (cooldown - gap).total_seconds()
                    return False, (
                        f"cooldown active ({remaining:.0f}s remaining for "
                        f"{primary_symbol}/{target_tier.value})"
                    )

            # OK to send — record sent_at NOW (safe even if the router does
            # not later call mark_sent; failed sends can be retried).
            self._last_sent[key] = now

        return True, "ok"

    # ─────────────────────────────────────────────────────────────────
    # WATCH digest queue
    # ─────────────────────────────────────────────────────────────────
    def queue_for_digest(
        self,
        decision: DecisionRecord,
        fused_event: FusedAnomalyEvent,
        primary_symbol: str,
        now: datetime | None = None,
    ) -> None:
        """Accumulate a WATCH decision into the daily digest queue."""
        now = now or datetime.now(timezone.utc)
        entry = DigestEntry(
            decision=decision, fused_event=fused_event,
            primary_symbol=primary_symbol, queued_at=now,
        )
        with self._lock:
            self._digest_queue.append(entry)
        logger.info(
            "Throttle: queued %s/%s for digest (queue_size=%d)",
            primary_symbol, decision.recommended_action.value, len(self._digest_queue),
        )

    def flush_digest(self) -> list[DigestEntry]:
        """Return all accumulated digest entries and clear the queue.

        The caller (router) takes them and bundles them into one message.
        """
        with self._lock:
            entries = list(self._digest_queue)
            self._digest_queue.clear()
        if entries:
            logger.info("Throttle: flushed digest queue (%d entries)", len(entries))
        return entries

    def digest_queue_size(self) -> int:
        with self._lock:
            return len(self._digest_queue)

    # ─────────────────────────────────────────────────────────────────
    # Debug / maintenance
    # ─────────────────────────────────────────────────────────────────
    def reset_cooldowns(self) -> None:
        """Forcibly clear all cooldowns. For tests / restart after kill-switch."""
        with self._lock:
            self._last_sent.clear()

    def snapshot(self) -> dict:
        """Summary of current internal state (health/debug)."""
        with self._lock:
            return {
                "active_cooldowns": len(self._last_sent),
                "digest_queue_size": len(self._digest_queue),
            }


__all__ = ["AlertThrottle", "ThrottleConfig", "DigestEntry"]
