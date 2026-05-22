"""
channels/base.py — Channel abstract interface (architecture §2, §3).

Every channel (polymarket, hyperliquid, cme, x) inherits this base class.
Core (registry, orchestrator, fusion engine) only knows this interface →
adding a new channel is easy (just register it).

────────────────────────────────────────────────────────────────────────
Lifecycle (architecture §3 Process View, §6.6 Failure Mode):

  __init__(config, deps)         # inject config + storage/cost/health deps
  await start()                  # start collector + feature + detector loop
  ...   (running)
  await stop()                   # graceful shutdown (WS close, buffer flush)

Health monitoring:
  property last_event_ts         # polled by health.make_staleness_check
  property is_running             # track start/stop state

Signal output:
  get_current_signal() → polled by the fusion engine each cycle.
  Returns the signal in the recent fresh window if any, otherwise None.
  None = "this channel is currently NORMAL" (fusion engine treats it as NORMAL tier).

────────────────────────────────────────────────────────────────────────
Why polling model (instead of push):

  - The fusion engine snapshots every channel's current state on a fixed cadence
    (e.g. 5s) — comparisons are consistent (no race conditions).
  - The channel does NOT call the fusion engine directly → one-way dependency.
  - From a channel's perspective: just run the detector and update latest_signal.

  Downside: short burst signals occurring/dissipating between polls can be missed.
  → Solved by each channel keeping its latest_signal for K seconds via an internal "sticky window".

  Pushing is reconsidered in the P9 detection deep-dive.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar, Literal

from ..core.schemas import ChannelSignal

# P12-F: per-channel fetch status surfaced by the weekly health digest.
#   ok      — the most recent fetch attempt succeeded.
#   fail    — the most recent fetch attempt raised / returned 4xx/5xx.
#   unknown — the channel has not attempted a fetch yet, or tracking is
#             not wired into its poll loop yet.
FetchStatus = Literal["ok", "fail", "unknown"]


@dataclass(frozen=True)
class ChannelFetchHealth:
    """Snapshot of the channel's most recent fetch attempt (P12-F).

    "fetch attempt" = one communication event with the upstream data source.
        Polymarket: one REST trades-API call
        Hyperliquid: one WS reconnect / REST snapshot
        TruthSocial: one collector.fetch_recent (GCS read or direct)
        X: one collector.fetch_recent_posts
        CME: one streamer/poll cycle iteration

    success / failure here is "did the communication itself succeed" — NOT
    "was there fresh data". The weekly digest's job is to catch silent
    failures (Cloudflare block, expired API key, etc.), not slow markets.
    """
    status: FetchStatus
    last_attempt_at: datetime | None  # None right after boot
    error: str = ""                   # populated only when status == 'fail'


class Channel(ABC):
    """Abstract base class for all channels.

    Subclasses must implement 4 abstract members:
      - name (override the ClassVar)
      - last_event_ts (property)
      - start() / stop() (async)
      - get_current_signal()

    P12-F (opt-in): to populate fetch_health accurately, call the helpers
    `_record_fetch_ok()` / `_record_fetch_fail()` from inside the channel's
    poll loop try/except. Without these calls fetch_health stays 'unknown'.
    """

    # Override with one of schemas.CHANNEL_* constants in the subclass.
    # e.g.: class PolymarketChannel(Channel): name = CHANNEL_POLYMARKET
    name: ClassVar[str] = "abstract"

    # ─────────────────────────────────────────────────────────────────
    # P12-F: fetch health tracking — opt-in (channels call _record_fetch_*)
    # ─────────────────────────────────────────────────────────────────
    # Defaults — if a channel never calls _record_fetch_*, status stays
    # 'unknown'. Used inside a single instance only; poll loops are single
    # asyncio tasks so no race.
    _last_fetch_status: FetchStatus = "unknown"
    _last_fetch_at: datetime | None = None
    _last_fetch_error: str = ""

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    @abstractmethod
    async def start(self) -> None:
        """Start the channel — kicks off collector / feature / detector loop.

        Called by the orchestrator inside an asyncio.gather.
        Typically the implementation spawns an internal task and returns immediately.
        If it raises, the orchestrator treats it as a contract-test failure.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown — WebSocket close, in-flight flush, cancel internal tasks.

        Called on SIGTERM (Cloud Run instance replacement) or on a contract-test failure.
        Must be idempotent (safe to call multiple times).
        """
        ...

    # ─────────────────────────────────────────────────────────────────
    # Signal output (polled by the fusion engine)
    # ─────────────────────────────────────────────────────────────────
    @abstractmethod
    def get_current_signal(self) -> ChannelSignal | None:
        """Return the most recent valid ChannelSignal.

        Returns:
            ChannelSignal: a signal emitted by the detector within the recent fresh window.
            None: no signal within that window → fusion treats as NORMAL tier.

        Implementation guide:
          - Keep "last signal + ts" inside the channel.
          - On get_current_signal, return None if (now - signal.ts) exceeds the fresh
            window (e.g. 60s) — stale signals auto-expire.
          - Sync function — lets the fusion engine call frequently without locks.
        """
        ...

    # ─────────────────────────────────────────────────────────────────
    # Health monitoring (read-only properties)
    # ─────────────────────────────────────────────────────────────────
    @property
    @abstractmethod
    def last_event_ts(self) -> datetime | None:
        """ts (UTC) of the most recently processed RawEvent.

        monitoring.health.make_staleness_check polls this to determine
        UNHEALTHY/DEGRADED. None right after boot.
        """
        ...

    @property
    def is_running(self) -> bool:
        """True from after start() until before stop().

        Default implementation: always False. Subclasses should override with their internal flag.
        """
        return False

    # ─────────────────────────────────────────────────────────────────
    # P12-F: Fetch health helpers
    # ─────────────────────────────────────────────────────────────────
    def _record_fetch_ok(self) -> None:
        """Channel calls this from its poll loop after a successful fetch.

        Example:
            try:
                data = await self._collector.fetch_recent(...)
            except Exception as e:
                self._record_fetch_fail(e)
                return
            self._record_fetch_ok()
        """
        self._last_fetch_status = "ok"
        self._last_fetch_at = datetime.now(timezone.utc)
        self._last_fetch_error = ""

    def _record_fetch_fail(self, error: BaseException | str) -> None:
        """Channel calls this from the except branch of its poll loop.

        error: stored as a truncated str() summary — surfaces in the weekly
            digest email as a debugging hint.
        """
        self._last_fetch_status = "fail"
        self._last_fetch_at = datetime.now(timezone.utc)
        if isinstance(error, BaseException):
            self._last_fetch_error = f"{type(error).__name__}: {error!s}"[:500]
        else:
            self._last_fetch_error = str(error)[:500]

    def fetch_health(self) -> ChannelFetchHealth:
        """Snapshot of the most recent fetch attempt — read by weekly_digest.

        Default: 'unknown' until the channel records its first fetch attempt.
        """
        return ChannelFetchHealth(
            status=self._last_fetch_status,
            last_attempt_at=self._last_fetch_at,
            error=self._last_fetch_error,
        )

    # ─────────────────────────────────────────────────────────────────
    # Convenience
    # ─────────────────────────────────────────────────────────────────
    def __repr__(self) -> str:
        return f"<Channel name={self.name} running={self.is_running}>"
