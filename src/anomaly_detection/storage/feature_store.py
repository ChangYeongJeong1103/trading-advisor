"""
storage/feature_store.py — Persist FeatureSnapshot as Parquet (architecture §4.2).

────────────────────────────────────────────────────────────────────────
Role:
  Persist FeatureSnapshots produced by channel feature engines (rolling
  z-score, percentile, etc.). Main purposes:
    - P10 historical event replay (features beat raw for threshold tuning)
    - Detector training data (P9 deep-dive)
    - Audit / debugging

  Same pattern as RawStore (channel + UTC date partition, batched write,
  atomic flush, retention). Only the schema + retention differ.

  ────────── Note on code duplication ──────────
  80%+ identical to raw_store.py. We deliberately duplicate in v1 for
  readability. Refactor into a _ParquetStore base class when a third Parquet
  store appears (P9~).
  ─────────────────────────────────────────────

────────────────────────────────────────────────────────────────────────
File layout (same as RawStore):

  data/anomaly/features/
    polymarket/
      2026-04-13/
        103045_a3f8.parquet
    hyperliquid/
      ...
    cme/
      ...
    x/
      ...

────────────────────────────────────────────────────────────────────────
Parquet schema (5 columns):

  id            : string
  channel       : string
  symbol        : string
  ts            : timestamp[us, UTC]
  features_json : string                  (JSON-serialized dict[str, float])
  baseline_ref  : string

  features has different keys per channel (polymarket: vol_zscore_5min,
  cme: oi_delta_5min) so struct/map types don't fit → JSON string.

────────────────────────────────────────────────────────────────────────
Retention:
  v1 policy (architecture §4.2): 30 days (longer than raw's 7 days).
  Historical features are needed often during detector tuning.

Architecture: §4.2 Storage Layout
Plan: §11 D3 (feature_store=Parquet)
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

from ..core.schemas import FeatureSnapshot


# =====================================================================
# Parquet schema definition
# =====================================================================
_TS_TYPE = pa.timestamp("us", tz="UTC")

_FEATURE_SCHEMA = pa.schema([
    pa.field("id", pa.string(), nullable=False),
    pa.field("channel", pa.string(), nullable=False),
    pa.field("symbol", pa.string(), nullable=False),
    pa.field("ts", _TS_TYPE, nullable=False),
    pa.field("features_json", pa.string(), nullable=False),
    pa.field("baseline_ref", pa.string(), nullable=False),
])


# =====================================================================
# FeatureStore
# =====================================================================
class FeatureStore:
    """FeatureSnapshot → Parquet persistence (partitioned by channel + date, batched writes)."""

    def __init__(
        self,
        base_path: Path,
        max_buffer_size: int = 1000,
        register_atexit: bool = True,
    ) -> None:
        """
        Args:
            base_path: data/anomaly/features/ path. Auto-created if missing.
            max_buffer_size: a channel buffer auto-flushes once it exceeds this size.
            register_atexit: True → register auto-flush at process exit.
                Recommended False in tests.
        """
        self._base_path = base_path
        self._base_path.mkdir(parents=True, exist_ok=True)

        self._max_buffer_size = max_buffer_size

        self._buffers: dict[str, list[FeatureSnapshot]] = {}
        self._lock = threading.Lock()

        if register_atexit:
            atexit.register(self.flush)

    # ─────────────────────────────────────────────────────────────────
    # Public API — write
    # ─────────────────────────────────────────────────────────────────
    def append(self, snapshot: FeatureSnapshot) -> None:
        """Append a FeatureSnapshot to the buffer. Auto-flush on overflow."""
        should_flush_channel: str | None = None
        with self._lock:
            buf = self._buffers.setdefault(snapshot.channel, [])
            buf.append(snapshot)
            if len(buf) >= self._max_buffer_size:
                should_flush_channel = snapshot.channel

        if should_flush_channel is not None:
            self.flush_channel(should_flush_channel)

    def flush(self) -> dict[str, int]:
        """Flush every channel buffer. Returns {channel: rows_written}."""
        with self._lock:
            channels = list(self._buffers.keys())
        return {ch: self.flush_channel(ch) for ch in channels}

    def flush_channel(self, channel: str) -> int:
        """Flush one channel's buffer."""
        with self._lock:
            buf = self._buffers.get(channel)
            if not buf:
                return 0
            self._buffers[channel] = []
            snapshots_to_write = buf

        return self._write_batch(channel, snapshots_to_write)

    def get_buffer_size(self, channel: str) -> int:
        with self._lock:
            return len(self._buffers.get(channel, []))

    # ─────────────────────────────────────────────────────────────────
    # Public API — read
    # ─────────────────────────────────────────────────────────────────
    def read_range(
        self,
        channel: str,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
    ) -> Iterator[FeatureSnapshot]:
        """Yield FeatureSnapshots with ts in [start, end).

        Args:
            channel: channel name to read.
            start: start time (inclusive, UTC).
            end: end time (exclusive, UTC).
            symbol: if set, filter to that symbol. None = all symbols.

        Yields:
            FeatureSnapshot.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start/end must be timezone-aware (UTC)")

        channel_dir = self._base_path / channel
        if not channel_dir.exists():
            return

        cur = start.date()
        end_date = end.date()
        while cur <= end_date:
            date_dir = channel_dir / cur.isoformat()
            if date_dir.exists():
                yield from self._read_date_dir(date_dir, start, end, symbol)
            cur += timedelta(days=1)

    def apply_retention(self, days: int, today: date | None = None) -> int:
        """Delete date directories older than `days`.

        Args:
            days: retention days (architecture §4.2 → features is 30).
            today: reference date (UTC). None → current UTC date.

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
                try:
                    d = date.fromisoformat(date_dir.name)
                except ValueError:
                    continue
                if d < cutoff:
                    self._rmtree(date_dir)
                    deleted += 1
        return deleted

    # ─────────────────────────────────────────────────────────────────
    # Internal — write
    # ─────────────────────────────────────────────────────────────────
    def _write_batch(self, channel: str, snapshots: list[FeatureSnapshot]) -> int:
        """Group by UTC date and atomically write one Parquet per group."""
        if not snapshots:
            return 0

        groups: dict[date, list[FeatureSnapshot]] = {}
        for snap in snapshots:
            d = snap.ts.astimezone(timezone.utc).date()
            groups.setdefault(d, []).append(snap)

        for d, group in groups.items():
            self._write_single_file(channel, d, group)

        return len(snapshots)

    def _write_single_file(
        self,
        channel: str,
        d: date,
        snapshots: list[FeatureSnapshot],
    ) -> None:
        """Atomically write one Parquet for a single (channel, date) partition."""
        time_str = snapshots[0].ts.strftime("%H%M%S")

        out_dir = self._base_path / channel / d.isoformat()
        out_dir.mkdir(parents=True, exist_ok=True)

        fname = f"{time_str}_{uuid.uuid4().hex[:8]}.parquet"
        final_path = out_dir / fname
        tmp_path = out_dir / f".{fname}.tmp"

        table = self._snapshots_to_table(snapshots)
        pq.write_table(table, tmp_path, compression="zstd")
        tmp_path.replace(final_path)

    @staticmethod
    def _snapshots_to_table(snapshots: list[FeatureSnapshot]) -> pa.Table:
        """FeatureSnapshot list → PyArrow Table."""
        # (transposes into per-column lists)
        ids: list[str] = []
        channels: list[str] = []
        symbols: list[str] = []
        tss: list[datetime] = []
        features: list[str] = []
        baselines: list[str] = []

        for snap in snapshots:
            ids.append(snap.id)
            channels.append(snap.channel)
            symbols.append(snap.symbol)
            tss.append(snap.ts)
            # features is dict[str, float] — stringify as JSON.
            features.append(json.dumps(snap.features, ensure_ascii=False))
            baselines.append(snap.baseline_ref)

        return pa.Table.from_pydict(
            {
                "id": ids,
                "channel": channels,
                "symbol": symbols,
                "ts": tss,
                "features_json": features,
                "baseline_ref": baselines,
            },
            schema=_FEATURE_SCHEMA,
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal — read
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _read_date_dir(
        date_dir: Path,
        start: datetime,
        end: datetime,
        symbol: str | None,
    ) -> Iterator[FeatureSnapshot]:
        """Read every .parquet under date_dir; filter by ts + optional symbol."""
        for parquet_file in sorted(date_dir.glob("*.parquet")):
            try:
                table = pq.read_table(parquet_file)
            except (OSError, pa.ArrowInvalid):
                continue

            data = table.to_pydict()
            n = len(data["id"])
            for i in range(n):
                ts = data["ts"][i]
                if ts < start or ts >= end:
                    continue
                if symbol is not None and data["symbol"][i] != symbol:
                    continue
                yield FeatureSnapshot(
                    id=data["id"][i],
                    channel=data["channel"][i],
                    symbol=data["symbol"][i],
                    ts=ts,
                    features=json.loads(data["features_json"][i]),
                    baseline_ref=data["baseline_ref"][i],
                )

    # ─────────────────────────────────────────────────────────────────
    # Internal — misc
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _rmtree(path: Path) -> None:
        """Recursive rm. Lightweight alternative to shutil.rmtree."""
        for child in path.iterdir():
            if child.is_dir():
                FeatureStore._rmtree(child)
            else:
                child.unlink()
        path.rmdir()
