"""
core/registry.py — Channel registration / lookup / lifecycle.

────────────────────────────────────────────────────────────────────────
Role (architecture §2.1, §3):
  At daemon startup, every enabled channel instance is registered here.
  Orchestrator / fusion engine do not import channels directly; they go
  through the registry → adding a new channel is easy.

  Main responsibilities:
    1) Register channel instances (reject name conflicts)
    2) start_all / stop_all (asyncio.gather + per-channel exception isolation)
    3) Iteration helper (the fusion engine polls every channel each cycle)

────────────────────────────────────────────────────────────────────────
How it connects to config:

  Orchestrator's startup flow (to be implemented in P1 Step 3):
    cfg = load_config()
    registry = ChannelRegistry()
    if cfg.channel_enabled("polymarket"):
        registry.register(PolymarketChannel(cfg, deps))
    if cfg.channel_enabled("hyperliquid"):
        registry.register(HyperliquidChannel(cfg, deps))
    ...
    await registry.start_all()

  Disabled channels are never instantiated → save memory/resources.

────────────────────────────────────────────────────────────────────────
Failure isolation:
  start_all / stop_all isolate exceptions from one channel so they don't
  kill the others (asyncio.gather(return_exceptions=True)).
  Result is returned as dict[channel_name, Exception | None] → callers
  handle partial failures in contract tests.

Architecture: §2.1 Component responsibility, §3 Process View
"""

from __future__ import annotations

import asyncio
import logging

from ..channels.base import Channel
from .schemas import ChannelSignal

logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Single container for every Channel instance in the daemon."""

    def __init__(self) -> None:
        # name → Channel instance. dict preserves insertion order (Python 3.7+).
        self._channels: dict[str, Channel] = {}

    # ─────────────────────────────────────────────────────────────────
    # Registration
    # ─────────────────────────────────────────────────────────────────
    def register(self, channel: Channel) -> None:
        """Register a channel instance.

        Args:
            channel: Channel subclass instance. .name is used as the key.

        Raises:
            ValueError: when the same name is already registered (caller bug).
            ValueError: when name is "abstract" (the Channel base itself).
        """
        if channel.name == "abstract" or not channel.name:
            raise ValueError(
                f"Channel must override `name` (got: {channel.name!r})"
            )
        if channel.name in self._channels:
            raise ValueError(
                f"Channel already registered: {channel.name!r}. "
                "ChannelRegistry does not allow duplicate names."
            )
        self._channels[channel.name] = channel
        logger.info("ChannelRegistry: registered %s", channel.name)

    def unregister(self, name: str) -> Channel | None:
        """Unregister. Typically called only on daemon shutdown. Returns None if absent."""
        ch = self._channels.pop(name, None)
        if ch is not None:
            logger.info("ChannelRegistry: unregistered %s", name)
        return ch

    # ─────────────────────────────────────────────────────────────────
    # Lookup
    # ─────────────────────────────────────────────────────────────────
    def get(self, name: str) -> Channel | None:
        """Look up a channel by name. None if absent."""
        return self._channels.get(name)

    def all(self) -> list[Channel]:
        """All registered channels (in registration order)."""
        return list(self._channels.values())

    def names(self) -> list[str]:
        """All registered channel names (in registration order)."""
        return list(self._channels.keys())

    def __len__(self) -> int:
        return len(self._channels)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._channels

    # ─────────────────────────────────────────────────────────────────
    # For the fusion engine — snapshot signals from every channel each cycle.
    # ─────────────────────────────────────────────────────────────────
    def snapshot_signals(self) -> dict[str, ChannelSignal | None]:
        """Snapshot the current signal of every channel in one shot.

        Returns:
            dict: {channel_name: ChannelSignal | None}.
                None means "no fresh signal right now (= channel is NORMAL)".

        Note:
            One dict comprehension to ensure a consistent snapshot.
            (Short enough under the Python GIL that there's no race.)
        """
        return {name: ch.get_current_signal() for name, ch in self._channels.items()}

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle — start_all / stop_all (failure-isolated)
    # ─────────────────────────────────────────────────────────────────
    async def start_all(self) -> dict[str, Exception | None]:
        """Concurrently call start() on every registered channel.

        An exception from one channel does not stop the others from starting.

        Returns:
            dict[name, Exception|None]: per-channel outcome.
                None = success, Exception = failure. Callers handle this in
                contract tests.
        """
        if not self._channels:
            logger.warning("ChannelRegistry.start_all: no channels registered")
            return {}

        names = list(self._channels.keys())
        coros = [self._channels[n].start() for n in names]
        results = await asyncio.gather(*coros, return_exceptions=True)

        outcome: dict[str, Exception | None] = {}
        for name, res in zip(names, results, strict=True):
            if isinstance(res, BaseException):
                logger.error("Channel start FAILED: %s — %s", name, res)
                outcome[name] = res if isinstance(res, Exception) else Exception(str(res))
            else:
                logger.info("Channel started: %s", name)
                outcome[name] = None
        return outcome

    async def stop_all(self) -> dict[str, Exception | None]:
        """Concurrently call stop() on every registered channel (graceful shutdown).

        Idempotent — safe to call multiple times (assuming each channel.stop is
        idempotent).

        Returns:
            dict[name, Exception|None].
        """
        if not self._channels:
            return {}

        names = list(self._channels.keys())
        coros = [self._channels[n].stop() for n in names]
        results = await asyncio.gather(*coros, return_exceptions=True)

        outcome: dict[str, Exception | None] = {}
        for name, res in zip(names, results, strict=True):
            if isinstance(res, BaseException):
                logger.error("Channel stop FAILED: %s — %s", name, res)
                outcome[name] = res if isinstance(res, Exception) else Exception(str(res))
            else:
                logger.info("Channel stopped: %s", name)
                outcome[name] = None
        return outcome

    # ─────────────────────────────────────────────────────────────────
    # Debugging
    # ─────────────────────────────────────────────────────────────────
    def __repr__(self) -> str:
        return f"<ChannelRegistry channels={self.names()}>"


__all__ = ["ChannelRegistry"]
