"""
core/orchestrator.py — Daemon main loop. Wires every component + runs the fusion cycle.

────────────────────────────────────────────────────────────────────────
Role (architecture §3 Process View, §5 Pipeline):
  At daemon startup, wire every component together. On every fusion_interval
  (default 5 seconds), run the next cycle:

    1) registry.snapshot_signals()       — capture every channel's current signal
    2) Insert only new ChannelSignals into signal_store (id-based tracking)
    3) fuse(signals, weights, health)    — produce a FusedAnomalyEvent
    4) signal_store.insert_fused_event   — every-5s full audit
    5) state_manager.observe(event)      — hysteresis → confirm transition
    6) decide(event, state_change)       — produce a DecisionRecord
    7) On state_change:
        - decision_store.insert(record)  — transition audit (only when changed)
        - alert_router.dispatch(record)  — actually send alerts (if available)

────────────────────────────────────────────────────────────────────────
Design decisions:

  · DecisionRecord is inserted only when state_change != None.
    Reason: saving "no change" decisions every 5s would be
            17,280/day × 365 ≈ 6.3M rows/year — pointless.
            fused_anomaly_events already provides full audit (every cycle), so
            decision_store is "transitions only".

  · Only new ChannelSignals get inserted: a channel may keep emitting the same
    id over multiple cycles (sticky window). Only an id change is a "new signal".
    Prevents duplicate-insert SQLite IntegrityError.

  · Exceptions in a single cycle are catch + log + proceed to the next cycle.
    The loop itself does not die. asyncio.CancelledError is re-raised
    (graceful-shutdown signal).

  · Alert router is optional. Through v1 P7 it's None — fall back to INFO log.

────────────────────────────────────────────────────────────────────────
Architecture: §3 Process view, §5 Pipeline, §6.6 Failure mode
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from ..channels.base import Channel
from ..monitoring.metrics import MetricRegistry
from ..storage.decision_store import DecisionStore
from ..storage.signal_store import SignalStore
from .decision_policy import decide
from .fusion_engine import fuse
from .registry import ChannelRegistry
from .schemas import ChannelSignal, DecisionRecord
from .state_manager import StateManager

# P11(a).4 — per-channel email path (cooldown + plot + SMTP).
# Optional — if not injected, behavior is unchanged (system-level alert_router only).
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..alerts.channel_dispatcher import ChannelAlertDispatcher
    from ..alerts.live_timeline import LiveTimelineBuffer

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Alert router Protocol — None / log-only fallback through v1 P7
# ─────────────────────────────────────────────────────────────────────
class AlertRouter(Protocol):
    """Responsible for alert dispatch. alerts/router.py implements this protocol (in P7)."""

    async def dispatch(self, decision: DecisionRecord) -> list[str]:
        """Take a Decision and actually send it. Return list of channels delivered (e.g. ['email', 'telegram'])."""
        ...


# health_provider: each cycle, returns channel name → health multiplier [0,1].
# When None, every channel is assumed health=1.0 (healthy).
HealthProvider = Callable[[], dict[str, float]]


# ─────────────────────────────────────────────────────────────────────
# AnomalyOrchestrator
# ─────────────────────────────────────────────────────────────────────
class AnomalyOrchestrator:
    """Daemon main loop — wires every component + runs the fusion cycle."""

    def __init__(
        self,
        *,
        registry: ChannelRegistry,
        signal_store: SignalStore,
        decision_store: DecisionStore,
        state_manager: StateManager,
        weights: dict[str, float],
        alert_router: AlertRouter | None = None,
        health_provider: HealthProvider | None = None,
        metrics: MetricRegistry | None = None,
        fusion_interval_s: float = 5.0,
        timeline_buffer: "LiveTimelineBuffer | None" = None,
        channel_dispatcher: "ChannelAlertDispatcher | None" = None,
    ) -> None:
        """
        Args:
            registry: the registered channels.
            signal_store: store for ChannelSignal + FusedAnomalyEvent.
            decision_store: store for DecisionRecord.
            state_manager: hysteresis-based transition decider.
            weights: channel name → weight (config.channels[*].weight).
            alert_router: if present, dispatch on transitions. None → log only.
            health_provider: per-cycle health multiplier provider. None → assume 1.0.
            metrics: if present, record fusion performance / error counters.
            fusion_interval_s: cycle interval (seconds).
            timeline_buffer: P11(a) — 24h rolling buffer that receives every
                cycle's snapshot. channel_dispatcher uses it as data for alert plots.
            channel_dispatcher: P11(a/d) — per-channel cooldown + email
                + (optional) telegram dispatch. Each cycle, hand it the signals
                snapshot; it dispatches per medium only the signals that pass cooldown.
        """
        self._registry = registry
        self._signal_store = signal_store
        self._decision_store = decision_store
        self._state_manager = state_manager
        self._weights = weights
        self._alert_router = alert_router
        self._health_provider = health_provider
        self._metrics = metrics
        self._interval = max(0.1, float(fusion_interval_s))
        self._timeline_buffer = timeline_buffer
        self._channel_dispatcher = channel_dispatcher

        # Channel name → id of the most recently inserted ChannelSignal (de-duplication).
        self._last_seen_signal_id: dict[str, str] = {}

        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._cycles_run = 0

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Start every channel + spawn the fusion-loop task.

        Idempotent: no-op if already running.
        Continues even if some channels fail to start (callers handle this in
        contract tests).
        """
        if self._loop_task is not None and not self._loop_task.done():
            logger.warning("Orchestrator.start: already running, ignoring")
            return

        logger.info("Orchestrator: starting %d channel(s)", len(self._registry))
        results = await self._registry.start_all()
        failed = [name for name, exc in results.items() if exc is not None]
        if failed:
            logger.warning("Orchestrator: %d channel(s) failed to start: %s", len(failed), failed)

        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._fusion_loop(), name="anomaly-fusion-loop")
        logger.info("Orchestrator: fusion loop started (interval=%.2fs)", self._interval)

    async def stop(self) -> None:
        """Safely stop the fusion loop + stop all channels + close the stores.

        Idempotent. Safe to start again afterwards (supports restart scenarios).
        """
        logger.info("Orchestrator: stopping (cycles_run=%d)", self._cycles_run)
        self._stopping.set()

        if self._loop_task is not None:
            try:
                # The loop wakes from its next sleep, checks the stopping flag,
                # and exits naturally. If it doesn't finish in time, cancel.
                await asyncio.wait_for(self._loop_task, timeout=self._interval + 2.0)
            except asyncio.TimeoutError:
                logger.warning("Orchestrator: loop did not stop in time, cancelling")
                self._loop_task.cancel()
                try:
                    await self._loop_task
                except (asyncio.CancelledError, BaseException):
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self._loop_task = None

        await self._registry.stop_all()

        # Storage flush — close safely.
        try:
            self._signal_store.close()
        except Exception as e:
            logger.error("Orchestrator: signal_store.close failed: %s", e)
        try:
            self._decision_store.close()
        except Exception as e:
            logger.error("Orchestrator: decision_store.close failed: %s", e)

        logger.info("Orchestrator: stopped")

    # ─────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────
    async def _fusion_loop(self) -> None:
        """One cycle per interval. Catch every exception except CancelledError."""
        while not self._stopping.is_set():
            cycle_start = datetime.now(timezone.utc)
            try:
                await self._run_one_cycle(cycle_start)
                if self._metrics:
                    self._metrics.counter("fusion_cycles_total").inc()
            except asyncio.CancelledError:
                logger.info("Orchestrator: loop cancelled")
                raise
            except Exception as e:
                logger.exception("Orchestrator: cycle failed (continuing): %s", e)
                if self._metrics:
                    self._metrics.counter("fusion_errors_total").inc()

            self._cycles_run += 1

            # Wait until the next interval — wake immediately if stopping is set.
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue  # normal — proceed to next cycle
            else:
                break    # stopping is set → exit

    async def _run_one_cycle(self, now: datetime) -> None:
        """One fusion cycle (steps 3–7 of the architecture §5 pipeline)."""
        # ─ 1) snapshot — capture every channel's current signal (consistent point in time)
        signals: dict[str, ChannelSignal | None] = self._registry.snapshot_signals()

        # ─ 2) Insert only new ChannelSignals (detected via id change)
        for name, sig in signals.items():
            if sig is None:
                continue
            if self._last_seen_signal_id.get(name) == sig.id:
                continue  # same signal still in its sticky window — skip
            try:
                self._signal_store.insert_channel_signal(sig)
                self._last_seen_signal_id[name] = sig.id
            except Exception as e:
                # Duplicate id, etc. — log + retry next cycle.
                logger.error("signal_store.insert_channel_signal(%s) failed: %s", name, e)

        # ─ 3) fusion
        health = self._health_provider() if self._health_provider else {}
        event = fuse(signals, self._weights, health)

        # ─ 4) Save the FusedAnomalyEvent (5s full audit)
        try:
            self._signal_store.insert_fused_event(event)
        except Exception as e:
            logger.error("signal_store.insert_fused_event failed: %s", e)

        # ─ 5) state_manager — was a transition confirmed?
        state_change = self._state_manager.observe(event, now=now)

        # ─ 6) decide
        decision = decide(event, state_change)

        # ─ 6.5) P11(a) timeline-buffer push — data source for plots.
        #         decision is only populated on transitions.
        if self._timeline_buffer is not None:
            try:
                self._timeline_buffer.append(
                    sim_clock=now,
                    per_channel_signals=signals,
                    fused_event=event,
                    decision=decision if state_change is not None else None,
                )
            except Exception as e:
                logger.error("timeline_buffer.append failed: %s", e)

        # ─ 6.6) P11(a/d) per-channel alert dispatch — cooldown + plot + SMTP
        #         + (optional) telegram. Independent of transitions — cooldown.decide
        #         judges at the channel level.
        if self._channel_dispatcher is not None:
            try:
                await self._channel_dispatcher.maybe_dispatch(signals, sim_clock=now)
            except Exception as e:
                logger.error("channel_dispatcher.maybe_dispatch failed: %s", e)

        # ─ 7) decision_store + alert only on transitions
        if state_change is not None:
            try:
                self._decision_store.insert(decision)
            except Exception as e:
                logger.error("decision_store.insert failed: %s", e)

            if self._alert_router is not None:
                try:
                    delivered = await self._alert_router.dispatch(decision)
                    logger.info(
                        "Alert dispatched: %s (channels=%s)",
                        decision.recommended_action.value, delivered,
                    )
                except Exception as e:
                    logger.error("alert_router.dispatch failed: %s", e)
            else:
                # Before P7 alerts is implemented — log-only fallback.
                logger.warning(
                    "ALERT (no router): %s | %s",
                    decision.recommended_action.value, decision.notes,
                )

            if self._metrics:
                self._metrics.counter(
                    "transitions_total",
                    labels={
                        "from": state_change[0].value,
                        "to": state_change[1].value,
                    },
                ).inc()

    # ─────────────────────────────────────────────────────────────────
    # Debug / external inspection
    # ─────────────────────────────────────────────────────────────────
    @property
    def cycles_run(self) -> int:
        return self._cycles_run

    @property
    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()


__all__ = ["AnomalyOrchestrator", "AlertRouter", "HealthProvider"]
