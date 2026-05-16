"""
anomaly.replay — P10 Historical event replay framework.

Package layout (see docs/p10-replay-framework.md §3):

    schemas.py            — Pydantic models (HistoricalEvent, BarTick, ReplayResult, ...)
    event_library.py      — markdown frontmatter parser → list[HistoricalEvent]
    data_sources/         — historical data fetchers (CME Databento, Polymarket, X, HL)
    runner.py             — ReplayRunner: 1-min bar discrete-step main loop
    metrics.py            — detection_latency / warning_time / fp_count
    reporters/            — CSV / YAML / matplotlib PNG output
    cli.py                — `python -m anomaly.replay <event_id>`

Design principles (recap, see design doc §2):

  · Replay clock = 1-minute bar discrete step (no asyncio sleep).
  · Multi-channel from day 1 — verifies fusion + boost rules.
  · Each event is independent — fresh detector instance per event.
  · In-memory storage only — never touches production stores.
  · Real data first, fixture fallback only when the source is nearly impossible (HL old events).
"""

from .schemas import (  # noqa: F401
    BarTick,
    HistoricalEvent,
    InsiderLikelihood,
    ReplayMetrics,
    ReplayMinute,
    ReplayResult,
)
from .event_library import EventLibrary, EventLibraryError  # noqa: F401
from .runner import (  # noqa: F401
    DEFAULT_CHANNEL_WEIGHTS,
    ChannelReplay,
    NullChannelReplay,
    ReplayRunner,
)
from .metrics import compute_metrics  # noqa: F401
from .channel_replays import CmeChannelReplay  # noqa: F401
from .reporters import (  # noqa: F401
    write_per_channel_csv,
    write_summary_row,
    write_timeline_plot,
    write_yaml_report,
)
