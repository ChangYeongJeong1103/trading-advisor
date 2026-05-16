"""
replay/runner.py — ReplayRunner: 1-min bar discrete-step main loop.

────────────────────────────────────────────────────────────────────────
Role:
  Take one HistoricalEvent, advance sim_clock by 1 minute at a time, and on
  each cycle run the same pipeline as production: (1) ask each channel detector
  for a signal → (2) fusion → (3) state_manager → (4) decision.

  Note: we do **not** use the asyncio loop's "wall-clock sleep". Bars are a
  function of time itself, so 1 cycle = 1 bar = 1-minute advance. A 24h replay
  = 1440 cycles, finishing in seconds.

────────────────────────────────────────────────────────────────────────
Core abstractions:

  ChannelReplay (Protocol)
    : Encapsulates "data fetch + feature engine + detector" for one channel.
      The Runner only needs to know this protocol — it doesn't see channel internals.

  NullChannelReplay
    : Used for skeleton smoke runs / as an inactive channel filler.
      step() always returns None (= NORMAL).

  ReplayRunner
    : Holds all 4 channels and runs the main loop. Each cycle: fuse + state +
      decide. Results from each cycle accumulate into ReplayMinute → ReplayResult.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Runner always calls fuse() with all 4 channels (same as production).
    Any channel below the active count is auto-filled with NullChannelReplay.
    → keeps fusion engine's boost rules and weight semantics consistent with
      production.

  · fuse() / state_manager.observe() / decide() are imported directly from
    production code. Replay only wraps production logic — no code duplication.

  · FusedAnomalyEvent.ts is filled in by fuse() via _now_utc(), so the runner
    overrides it with sim_clock via model_copy. Pydantic v2 frozen models
    support model_copy(update=...) — audit integrity is preserved.

  · Each event = fresh state_manager (initial_state=NORMAL).
    Events do not share dwell timers.

  · In-memory only. We never touch production stores (signal_store /
    decision_store / etc.).

────────────────────────────────────────────────────────────────────────
References:
  · docs/p10-replay-framework.md §2.1 (replay clock), §3 (module layout)
  · src/anomaly/core/orchestrator.py (production fusion loop — for comparison)
  · src/anomaly/core/fusion_engine.py (fuse function)
  · src/anomaly/core/state_manager.py (StateManager + HysteresisConfig)
  · src/anomaly/core/decision_policy.py (decide function)
"""

from __future__ import annotations

# --- standard library ---
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import ClassVar, Protocol, runtime_checkable

# --- local: production pipeline (reused as-is) ---
from ..core.decision_policy import decide
from ..core.fusion_engine import fuse
from ..core.schemas import ALL_CHANNELS, ChannelSignal, Tier
from ..core.state_manager import HysteresisConfig, StateManager

# --- local: replay schemas + metrics ---
from .metrics import compute_metrics
from .schemas import HistoricalEvent, ReplayMinute, ReplayResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# ChannelReplay Protocol — how the runner sees a single channel.
# ─────────────────────────────────────────────────────────────────────
@runtime_checkable
class ChannelReplay(Protocol):
    """Replay-side wrapper for one channel. Encapsulates data source + detector.

    Examples of real impls (added in P10.3 #3):
      · CmeChannelReplay  — wraps CmeDatabentoSource + CME feature_engine + detector
      · PolymarketChannelReplay
      · XChannelReplay
      · HyperliquidChannelReplay

    Skeleton impl:
      · NullChannelReplay  — step() always returns None (this file).
    """

    # Which channel. One of the core/schemas.CHANNEL_* constants.
    channel: ClassVar[str]

    async def warmup(self, event: HistoricalEvent) -> None:
        """Pre-fetch every bar in the event window + train the feature_engine baseline.

        The runner awaits this once before starting the main loop. May be heavy.
        """
        ...

    async def step(self, sim_clock: datetime) -> ChannelSignal | None:
        """ChannelSignal the detector emits for this minute (sim_clock ~ sim_clock+60s).

        Returns:
            ChannelSignal: detector fired.
            None: no data or detector quiet (= treated as NORMAL).
        """
        ...

    async def close(self) -> None:
        """Release resources acquired in warmup. Idempotent."""
        ...


# ─────────────────────────────────────────────────────────────────────
# NullChannelReplay — skeleton + inactive-channel filler.
# ─────────────────────────────────────────────────────────────────────
class NullChannelReplay:
    """Always returns None from step(). Fusion engine treats it as NORMAL tier.

    Because the runner guarantees 4 channels, any channel not in the event's
    active channels is auto-filled with NullChannelReplay. A skeleton smoke
    test can have all 4 be NullChannelReplay.
    """

    channel: ClassVar[str] = "null"

    def __init__(self, channel: str) -> None:
        # Carry the channel name as an instance attr; the ClassVar is a sentinel.
        self.channel = channel

    async def warmup(self, event: HistoricalEvent) -> None:
        return None

    async def step(self, sim_clock: datetime) -> ChannelSignal | None:
        return None

    async def close(self) -> None:
        return None

    def __repr__(self) -> str:
        return f"<NullChannelReplay channel={self.channel}>"


# ─────────────────────────────────────────────────────────────────────
# Default channel weights — same intent as production config; v0 placeholder.
# Tuned from historical replay results in P10.5.
# ─────────────────────────────────────────────────────────────────────
DEFAULT_CHANNEL_WEIGHTS: dict[str, float] = {
    # Equal weight 1.0 for every channel. Used as fused_score input, so it has
    # little impact on NORMAL operation.
    "polymarket": 1.0,
    "hyperliquid": 1.0,
    "cme": 1.0,
    "x": 1.0,
}


# ─────────────────────────────────────────────────────────────────────
# ReplayRunner — main loop.
# ─────────────────────────────────────────────────────────────────────
class ReplayRunner:
    """Owns the main loop for replaying one event.

    Usage:

        runner = ReplayRunner(
            channels={
                "cme": MyCmeChannelReplay(),    # real impl
                "polymarket": NullChannelReplay("polymarket"),  # skeleton
                ...
            },
        )
        result: ReplayResult = await runner.run(event)

    Idempotent — the same runner instance can run() multiple events sequentially.
    A fresh state_manager is created for each event (no carry-over).
    """

    def __init__(
        self,
        channels: dict[str, ChannelReplay] | None = None,
        weights: dict[str, float] | None = None,
        hysteresis_config: HysteresisConfig | None = None,
        bar_minutes: int = 1,
    ) -> None:
        """
        Args:
            channels: channel name → ChannelReplay. If fewer than 4, missing
                channels are auto-filled with NullChannelReplay. If None, all 4
                are Null (skeleton).
            weights: channel weights passed to fuse(). If None, DEFAULT_CHANNEL_WEIGHTS.
            hysteresis_config: state_manager dwell times. If None, production defaults
                (60/30/10s escalate, 180/300/600s deescalate).
            bar_minutes: time advance per cycle (minutes). v0 = 1 fixed.
        """
        # ── channels: guarantee 4 ──
        provided = channels or {}
        # Ensure an instance for every entry in ALL_CHANNELS. Fill missing with NullChannelReplay.
        self._channels: dict[str, ChannelReplay] = {
            ch: provided.get(ch, NullChannelReplay(ch)) for ch in ALL_CHANNELS
        }
        # Reject immediately if `provided` has names not in ALL_CHANNELS (catches typos).
        unknown = set(provided.keys()) - set(ALL_CHANNELS)
        if unknown:
            raise ValueError(
                f"Unknown channels in 'channels' arg: {sorted(unknown)}. "
                f"Must be subset of {list(ALL_CHANNELS)}."
            )

        # ── weights ──
        self._weights: dict[str, float] = dict(weights or DEFAULT_CHANNEL_WEIGHTS)

        # ── state_manager is created fresh per run() — only the config is kept here ──
        self._hysteresis_config = hysteresis_config or HysteresisConfig()

        # ── bar size ──
        if bar_minutes < 1:
            raise ValueError(f"bar_minutes must be >= 1, got {bar_minutes}")
        self._bar_minutes = bar_minutes

    # ─────────────────────────────────────────────────────────────────
    # Public — run 1 event.
    # ─────────────────────────────────────────────────────────────────
    async def run(self, event: HistoricalEvent) -> ReplayResult:
        """Replay one historical event end-to-end.

        Args:
            event: typed object loaded from data/anomaly/historical_events/<id>.md.

        Returns:
            ReplayResult: every cycle's ReplayMinute + computed ReplayMetrics.

        Side effects:
            · Calls ChannelReplay.warmup() / step() / close().
            · Never touches production stores (in-memory only).
            · Logging — one or two INFO lines (start/end); DEBUG at the cycle level.
        """
        started_at = datetime.now(timezone.utc)
        logger.info(
            "ReplayRunner: starting event=%s window=%s ~ %s (%d minutes)",
            event.event_id,
            event.window_start.isoformat(),
            event.window_end.isoformat(),
            event.total_minutes,
        )

        # ── 1) Warmup all channels (parallel) ──
        # gather → concurrent fetch — parallelizes Databento + GraphQL IO.
        # If one channel fails the others still proceed. Raises propagate as-is.
        await asyncio.gather(*(ch.warmup(event) for ch in self._channels.values()))

        # ── 2) Fresh state_manager (no carry-over between events) ──
        state_mgr = StateManager(
            config=self._hysteresis_config,
            initial_state=Tier.NORMAL,
        )

        # ── 3) Main loop ──
        minutes_buffer: list[ReplayMinute] = []
        sim_clock = event.window_start
        bar_delta = timedelta(minutes=self._bar_minutes)
        # window_end inclusive (≤). Goes through the final cycle of announcement + post_event_window.
        cycles = 0
        try:
            while sim_clock <= event.window_end:
                replay_minute = await self._run_one_cycle(sim_clock, state_mgr)
                minutes_buffer.append(replay_minute)
                sim_clock += bar_delta
                cycles += 1
        finally:
            # ── 4) Cleanup — try close even on failure ──
            await asyncio.gather(
                *(ch.close() for ch in self._channels.values()),
                return_exceptions=True,
            )

        finished_at = datetime.now(timezone.utc)
        elapsed = (finished_at - started_at).total_seconds()

        # ── 5) Compute metrics ──
        metrics = compute_metrics(event, minutes_buffer)

        logger.info(
            "ReplayRunner: finished event=%s cycles=%d elapsed=%.2fs "
            "max_tier=%s first_alert=%s",
            event.event_id, cycles, elapsed,
            metrics.max_tier_reached.value,
            metrics.first_alert_ts.isoformat() if metrics.first_alert_ts else "—",
        )

        return ReplayResult(
            event=event,
            started_at=started_at,
            finished_at=finished_at,
            minutes=minutes_buffer,
            metrics=metrics,
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal — one cycle.
    # ─────────────────────────────────────────────────────────────────
    async def _run_one_cycle(
        self,
        sim_clock: datetime,
        state_mgr: StateManager,
    ) -> ReplayMinute:
        """One-minute cycle: signals → fuse → state → decide → ReplayMinute."""

        # ── 3a) Call step() on every channel concurrently ──
        # gather → parallel step across 4 channels. step() is usually an in-memory
        # cache lookup (so near-instant), but real sources (Databento async fetch)
        # may await.
        signal_results = await asyncio.gather(
            *(ch.step(sim_clock) for ch in self._channels.values()),
            return_exceptions=False,
        )
        # dict[channel_name, ChannelSignal | None]
        signals: dict[str, ChannelSignal | None] = dict(zip(self._channels.keys(), signal_results))

        # ── 3b) Fusion ──
        fused = fuse(signals=signals, weights=self._weights, health=None)
        # Replace the ts that fuse() filled via _now_utc() with sim_clock
        # (replay consistency). Pydantic v2 frozen → use model_copy(update=...).
        fused = fused.model_copy(update={"ts": sim_clock})

        # ── 3c) State manager (use sim_clock as 'now') ──
        transition = state_mgr.observe(event=fused, now=sim_clock)

        # ── 3d) Decision ──
        # decide() returns a NO_ACTION DecisionRecord even when transition is None.
        # We only keep cycles where a transition actually happened in ReplayMinute
        # (removes noise from the timeline). For audit, all info lives in fused_event.
        decision_record = decide(event=fused, state_change=transition)
        stored_decision = decision_record if transition is not None else None

        return ReplayMinute(
            sim_clock=sim_clock,
            per_channel_signals=signals,
            fused_event=fused,
            decision=stored_decision,
        )


__all__ = [
    "ChannelReplay",
    "NullChannelReplay",
    "ReplayRunner",
    "DEFAULT_CHANNEL_WEIGHTS",
]
