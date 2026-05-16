"""
storage/raw_store.py — Persist RawEvent as Parquet (architecture §4.2).

────────────────────────────────────────────────────────────────────────
Role:
  Persist the RawEvent (source's raw payload) received by channel collectors.
  Main purpose: replay / debugging / audit (NOT real-time detection input —
  that's NormalizedEvent).

  Creating one file per event would explode overhead → batch:
    1) Accumulate in a per-channel in-memory buffer
    2) When buffer exceeds max_buffer_size OR flush() is called → write one Parquet file
    3) An atexit handler guarantees a flush (normal daemon shutdown)

────────────────────────────────────────────────────────────────────────
File layout:

  data/anomaly/raw/
    polymarket/
      2026-04-13/
        103045_a3f8.parquet   ← HHMMSS_uuidshort, one batch
        104102_b7e2.parquet
      2026-04-14/
        ...
    hyperliquid/
      ...
    cme/
      ...
    x/
      ...

  → channel filter = scan only one directory
  → date filter = look at directory name and skip
  → retention = rm -rf old date directories

────────────────────────────────────────────────────────────────────────
Parquet schema (7 columns):

  id           : string                   (RawEvent.id, uuid hex)
  channel      : string
  source       : string                   ("ws" | "rest" | "webhook" | "scrape" | "dune")
  symbol       : string
  ts_source    : timestamp[us, UTC]
  ts_ingest    : timestamp[us, UTC]
  payload_json : string                   (JSON-serialized payload dict)

  Payload schema varies by channel → unified columns are impossible → JSON string.
  A single json.loads is enough during audit / replay.

────────────────────────────────────────────────────────────────────────
Concurrency:

  threading.Lock protects the buffer (safe with asyncio + future thread executors).
  Other threads may append during flush → short lock + swap-buffer pattern.

Architecture: §4.2 Storage Layout, §4.3 Time / id discipline
Plan: §11 D3 (raw_store=Parquet)
"""

from __future__ import annotations

import atexit
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..core.schemas import RawEvent


# =====================================================================
# Parquet schema definition — single source of truth. Imported by both read and write.
# =====================================================================
# Force UTC — architecture §4.3 (every timestamp is UTC).
_TS_TYPE = pa.timestamp("us", tz="UTC")

_RAW_SCHEMA = pa.schema([
    pa.field("id", pa.string(), nullable=False),
    pa.field("channel", pa.string(), nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("symbol", pa.string(), nullable=False),
    pa.field("ts_source", _TS_TYPE, nullable=False),
    pa.field("ts_ingest", _TS_TYPE, nullable=False),
    pa.field("payload_json", pa.string(), nullable=False),
])


# =====================================================================
# RawStore
# =====================================================================
class RawStore:
    """RawEvent → Parquet persistence (partitioned by channel + date, batched writes)."""

    def __init__(
        self,
        base_path: Path,
        max_buffer_size: int = 1000,
        register_atexit: bool = True,
    ) -> None:
        """
        Args:
            base_path: data/anomaly/raw/ path. Auto-created if missing.
            max_buffer_size: a channel buffer auto-flushes when it exceeds this size.
                v1 recommendation: 1000 (≈ 1MB Parquet file, append latency < 100ms).
            register_atexit: True → auto-flush is registered on process exit.
                Recommended False in tests (so flushes don't fire on every test exit).
        """
        self._base_path = base_path
        self._base_path.mkdir(parents=True, exist_ok=True)

        self._max_buffer_size = max_buffer_size

        # channel → list[RawEvent]. Use swap pattern to keep the lock short.
        self._buffers: dict[str, list[RawEvent]] = {}
        self._lock = threading.Lock()

        if register_atexit:
            # Guarantee a buffer flush on graceful daemon shutdown (Ctrl+C, SIGTERM, etc.)
            atexit.register(self.flush)

    # ─────────────────────────────────────────────────────────────────
    # Public API — write
    # ─────────────────────────────────────────────────────────────────
    def append(self, event: RawEvent) -> None:
        """Add a RawEvent to the buffer. Auto-flushes when the buffer overflows.

        Thread-safe. Called by the channel collector for every event.
        """
        # Buffer update + overflow check are inside the lock.
        should_flush_channel: str | None = None
        with self._lock:
            buf = self._buffers.setdefault(event.channel, [])
            buf.append(event)
            if len(buf) >= self._max_buffer_size:
                should_flush_channel = event.channel

        # Flush happens outside the lock (disk I/O can take a while — don't block
        # appends to other channels).
        if should_flush_channel is not None:
            self.flush_channel(should_flush_channel)

    def flush(self) -> dict[str, int]:
        """Flush every channel buffer to disk.

        Returns:
            dict[str, int]: {channel: rows written}. Empty channels are included as 0.
        """
        # Snapshot which channels currently exist.
        with self._lock:
            channels = list(self._buffers.keys())

        result: dict[str, int] = {}
        for ch in channels:
            result[ch] = self.flush_channel(ch)
        return result

    def flush_channel(self, channel: str) -> int:
        """Flush one channel's buffer. 1 batch = 1 Parquet file.

        Returns:
            int: rows written. 0 if the buffer was empty.
        """
        # swap-buffer: grab the lock briefly to take the buffer.
        with self._lock:
            buf = self._buffers.get(channel)
            if not buf:
                return 0
            self._buffers[channel] = []
            events_to_write = buf  # this list is ours now.

        # Disk I/O outside the lock.
        n_written = self._write_batch(channel, events_to_write)
        return n_written

    def get_buffer_size(self, channel: str) -> int:
        """Current row count in the buffer. For monitoring."""
        with self._lock:
            return len(self._buffers.get(channel, []))

    # ─────────────────────────────────────────────────────────────────
    # Public API — read (replay / audit)
    # ─────────────────────────────────────────────────────────────────
    def read_range(
        self,
        channel: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[RawEvent]:
        """Yield RawEvents whose ts_source is in [start, end).

        Args:
            channel: channel to read.
            start: start time (inclusive, UTC).
            end: end time (exclusive, UTC).

        Yields:
            RawEvent: every event whose ts_source is in [start, end).
                Files are not sorted globally (only by append order within a
                single batch). If sorting is needed, the caller sorts.

        Note:
            Generator so large data replays don't blow up memory. Do not wrap with list().
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start/end must be timezone-aware (UTC)")

        channel_dir = self._base_path / channel
        if not channel_dir.exists():
            return

        # Candidate date directories: start.date through end.date
        # (UTC — partitions are UTC date).
        cur = start.date()
        end_date = end.date()
        while cur <= end_date:
            date_dir = channel_dir / cur.isoformat()
            if date_dir.exists():
                yield from self._read_date_dir(date_dir, start, end)
            cur += timedelta(days=1)

    def apply_retention(self, days: int, today: date | None = None) -> int:
        """Delete date directories older than `days`.

        Args:
            days: retention days (architecture §4.2 → raw is 7 days).
            today: reference date (UTC). None → current UTC date. For test convenience.

        Returns:
            int: number of directories deleted.
        """
        if days < 1:
            raise ValueError("days must be >= 1")

        cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=days)

        deleted = 0
        for channel_dir in self._base_path.iterdir():
            if not channel_dir.is_dir():
                continue
            for date_dir in channel_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                # Ensure the directory name looks like a date (protects against junk dirs).
                try:
                    d = date.fromisoformat(date_dir.name)
                except ValueError:
                    continue
                if d < cutoff:
                    # rm -rf one-liner
                    self._rmtree(date_dir)
                    deleted += 1
        return deleted

    # ─────────────────────────────────────────────────────────────────
    # Internal — write helpers
    # ─────────────────────────────────────────────────────────────────
    def _write_batch(self, channel: str, events: list[RawEvent]) -> int:
        """Group events by UTC date and atomically write one Parquet per group.

        File path: <base>/<channel>/<UTC date>/<HHMMSS>_<uuid8>.parquet
        Atomic: write to tmp then rename → no corrupt files even on mid-write crash.

        If a batch straddles midnight (rare but possible), split by UTC date so
        retention still works correctly.
        """
        if not events:
            return 0

        # UTC date → events for that date.
        groups: dict[date, list[RawEvent]] = {}
        for ev in events:
            d = ev.ts_source.astimezone(timezone.utc).date()
            groups.setdefault(d, []).append(ev)

        for d, group_events in groups.items():
            self._write_single_file(channel, d, group_events)

        return len(events)

    def _write_single_file(self, channel: str, d: date, events: list[RawEvent]) -> None:
        """Atomically write one Parquet file for a (channel, date) partition."""
        # File name uses the first event's time (within the group everyone is on
        # the same date, so no collision concerns).
        time_str = events[0].ts_source.strftime("%H%M%S")

        out_dir = self._base_path / channel / d.isoformat()
        out_dir.mkdir(parents=True, exist_ok=True)

        # Short uuid (8 chars) — avoid same-second collisions.
        fname = f"{time_str}_{uuid.uuid4().hex[:8]}.parquet"
        final_path = out_dir / fname
        tmp_path = out_dir / f".{fname}.tmp"

        table = self._events_to_table(events)
        # ZSTD compression — fast and a good ratio (raw payload is JSON, compresses well).
        pq.write_table(table, tmp_path, compression="zstd")
        tmp_path.replace(final_path)  # atomic on POSIX

    @staticmethod
    def _events_to_table(events: list[RawEvent]) -> pa.Table:
        """RawEvent list → PyArrow Table (transposed into per-column lists)."""
        ids: list[str] = []
        channels: list[str] = []
        sources: list[str] = []
        symbols: list[str] = []
        ts_sources: list[datetime] = []
        ts_ingests: list[datetime] = []
        payloads: list[str] = []

        for ev in events:
            ids.append(ev.id)
            channels.append(ev.channel)
            sources.append(ev.source.value)  # Enum → str
            symbols.append(ev.symbol)
            ts_sources.append(ev.ts_source)
            ts_ingests.append(ev.ts_ingest)
            # payload dict → JSON string. default=str handles datetime and other types.
            payloads.append(json.dumps(ev.payload, default=str, ensure_ascii=False))

        return pa.Table.from_pydict(
            {
                "id": ids,
                "channel": channels,
                "source": sources,
                "symbol": symbols,
                "ts_source": ts_sources,
                "ts_ingest": ts_ingests,
                "payload_json": payloads,
            },
            schema=_RAW_SCHEMA,
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal — read helpers
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _read_date_dir(
        date_dir: Path,
        start: datetime,
        end: datetime,
    ) -> Iterator[RawEvent]:
        """Read every .parquet under date_dir and filter to [start, end)."""
        for parquet_file in sorted(date_dir.glob("*.parquet")):
            try:
                table = pq.read_table(parquet_file)
            except (OSError, pa.ArrowInvalid):
                # Skip corrupt files (audit logging is the caller's job).
                continue

            # Convert to column-wise python lists, then rebuild row-wise.
            data = table.to_pydict()
            n = len(data["id"])
            for i in range(n):
                ts = data["ts_source"][i]
                # PyArrow returns a tz-aware datetime.
                if ts < start or ts >= end:
                    continue
                yield RawEvent(
                    id=data["id"][i],
                    channel=data["channel"][i],
                    source=data["source"][i],
                    symbol=data["symbol"][i],
                    ts_source=ts,
                    ts_ingest=data["ts_ingest"][i],
                    payload=json.loads(data["payload_json"][i]),
                )

    # ─────────────────────────────────────────────────────────────────
    # Internal — misc
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _rmtree(path: Path) -> None:
        """Recursive rm. Lightweight alternative to shutil.rmtree (no extra deps)."""
        for child in path.iterdir():
            if child.is_dir():
                RawStore._rmtree(child)
            else:
                child.unlink()
        path.rmdir()
