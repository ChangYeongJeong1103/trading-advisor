"""
data_sources/base.py — HistoricalDataSource Protocol.

────────────────────────────────────────────────────────────────────────
Role:
  Common interface for per-channel historical 1-min bar fetchers.
  Every real source (Databento, GraphQL, snscrape, ...) implements this protocol.

Design decisions:
  · Use Protocol (typing.Protocol) — duck-typed, no inheritance required.
    Lighter than ABC. Structural typing also makes mypy work well.

  · warmup() is heavy — pre-fetch the entire event window + disk cache.
    Reason: calling the network every 1-minute cycle makes replay too slow
    and explodes cost. The cost cap is also far safer with a single warmup
    than a per-cycle call.

  · get_bar() is sync — only an in-memory dict lookup of the warmed cache.
    No async needed.

  · A source is activated for an event only if supports() returns True.
    e.g. the CME source needs event.primary_symbols to contain CME roots like
    ES/CL/BZ/NG to return True. With only Polymarket slugs → False → null fallback.

────────────────────────────────────────────────────────────────────────
Reference: docs/p10-replay-framework.md §3.2
"""

from __future__ import annotations

# --- standard library ---
from datetime import datetime
from typing import ClassVar, Protocol, runtime_checkable

# --- local ---
from ..schemas import BarTick, HistoricalEvent


@runtime_checkable
class HistoricalDataSource(Protocol):
    """Historical 1-min bar fetcher for one channel.

    Concrete implementations only need to override the channel ClassVar.
    """

    # Which channel this is for. e.g. "cme", "polymarket".
    # One of the core/schemas.CHANNEL_* constants.
    channel: ClassVar[str]

    def supports(self, event: HistoricalEvent) -> bool:
        """Can this source provide data for the given event?

        Args:
            event: the event to check.

        Returns:
            True → warmup may be called. False → runner falls back to NullDataSource.

        Example impl (CME):
            return any(sym in CME_VALID_ROOTS for sym in event.primary_symbols)
        """
        ...

    async def warmup(self, event: HistoricalEvent) -> None:
        """Pre-fetch the bars for the entire event window (event.window_start ~ window_end).

        Implementation guide:
          · Do all network/disk fetches here (no repeat calls).
          · Store results in an internal dict[(symbol, ts), BarTick] on self.
          · Costly sources (Databento etc.) should call through cost_tracker.
          · On cache hit, skip the fetch — disk → memory only.

        Raises:
            Implementation-defined — the runner catches above and gracefully skips.
        """
        ...

    def get_bar(self, symbol: str, sim_clock: datetime) -> BarTick | None:
        """Look up one bar from the in-memory cache populated by warmup.

        Args:
            symbol: one of event.primary_symbols.
            sim_clock: bar start time (UTC). Bar covers [sim_clock, sim_clock+60s).

        Returns:
            BarTick: when data exists at that time.
            None: when missing (e.g. market closed, 0 trades). Detector stays NORMAL.
        """
        ...

    async def close(self) -> None:
        """Release resources acquired in warmup (HTTP sessions, DB connections, ...).

        Idempotent. Must be safe even if already closed.
        """
        ...
