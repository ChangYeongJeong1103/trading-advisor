"""
data_sources/null.py — No-signal placeholder data source.

────────────────────────────────────────────────────────────────────────
Two purposes:

  1. Skeleton smoke test — confirm the runner's main loop / fusion /
     state_manager all flow cleanly with no real sources wired up.
     If all 4 channels are NullDataSource then every cycle is NORMAL → metrics
     should accurately report "0 alerts".

  2. Inactive channel filler — even when an event has fewer than 4 active
     channels (primary + secondary), the runner always includes all 4 in
     fusion to match production semantics. Inactive channels are auto-filled
     with NullDataSource and emit NORMAL.

────────────────────────────────────────────────────────────────────────
Note:
  NullDataSource itself does not emit ChannelSignal — it just signals "no data".
  ChannelSignal=None → the fusion engine treats it as NORMAL tier.

  Emitting ChannelSignal=None is actually the job of ChannelReplay (runner.py)'s
  NullChannelReplay.
"""

from __future__ import annotations

# --- standard library ---
from datetime import datetime
from typing import ClassVar

# --- local ---
from ..schemas import BarTick, HistoricalEvent


class NullDataSource:
    """Responds "no data" to every call. Used for skeletons + inactive channels.

    Instantiable for any channel name — the runner has scenarios where all 4
    channels use NullDataSource (skeleton smoke), as well as where only 1–3
    do (e.g. CME real, others null).

    Args:
        channel: the channel name this source stands in for. Not used by
            supports() / get_bar(), but useful for logging / debug.
    """

    # ClassVar override — the Protocol declares ClassVar[str], but NullDataSource
    # can be any channel, so we use an instance attr and treat ClassVar as a sentinel.
    channel: ClassVar[str] = "null"

    def __init__(self, channel: str = "null") -> None:
        # Instance-level shadow — show a different channel per instance.
        # mypy may warn about ClassVar override but the pattern is intentional.
        self.channel = channel

    def supports(self, event: HistoricalEvent) -> bool:
        """Always True — accepts any event (but get_bar always returns None)."""
        return True

    async def warmup(self, event: HistoricalEvent) -> None:
        """Nothing to do — no fetch."""
        # explicit pass — intent is clear (silent no-op).
        return None

    def get_bar(self, symbol: str, sim_clock: datetime) -> BarTick | None:
        """Always None — no data (handled as NORMAL)."""
        return None

    async def close(self) -> None:
        """Nothing to do."""
        return None

    def __repr__(self) -> str:
        return f"<NullDataSource channel={self.channel}>"
