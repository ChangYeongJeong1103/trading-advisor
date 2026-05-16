"""
cme/live_streamer.py — Databento Live (WebSocket) → Parquet → GCS uploader.

────────────────────────────────────────────────────────────────────────
Responsibilities (P9.3.P3):

  Streams *real-time* L0 trades for Channel 3 (CME) 24/7, storing them in GCS
  as 5-minute micro-batch Parquet files. When a TV trigger fires, CMEEnricher
  reads only the ±15-minute slice from GCS for instant post-analysis (VPIN,
  side imbalance, block trades). Permanently solves the PENDING issue caused
  by the ~24h Historical API lag.

────────────────────────────────────────────────────────────────────────
Deployment:

  GCP Compute Engine `e2-micro` VM (us-west1-b) — always on (24/7).
  Auto-started / restarted on crash via systemd unit `cme-streamer.service`.
  Service account `cme-streamer-sa` → GCS bucket write + Secret Manager read.

────────────────────────────────────────────────────────────────────────
Storage layout (Hive partitioning — pyarrow.dataset can push down filters):

  gs://anomaly-cme-trades/trades/symbol={CL,BZ,ES,GC}/date=YYYY-MM-DD/HHMM.parquet

  · File granularity: 5-minute micro-batches (minutes 00, 05, 10, ..., 55).
  · File count: 4 symbols × 288 buckets/day = ~1.15K/day, ~420K/year.
  · Compression: snappy (Parquet default). Active 5-min for CL/ES = ~30K rows ~ 0.5MB.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · **Subscribe runs in a blocking thread, records delivered via asyncio queue**:
    The Databento Live SDK's async iterator compatibility varies by SDK version,
    so we adopted the most reliable pattern (blocking `for record in client:` → queue).

  · **Subscribe with continuous symbols** (e.g. `CL.c.0`) → contract roll auto-handled.
    Map instrument_id → root dynamically via `SymbolMappingMsg`.

  · **Reconnect logic**: exponential backoff on WebSocket disconnect (1→60 sec),
    infinite retry forever. Failure alerting is handled by a separate health check (Step F).

  · **Buffer-overflow guard**: when the in-memory deque exceeds 100k rows,
    force-flush mid-bucket. Active CME can reach up to ~150k rows in 5 minutes,
    so this gives a safety margin.

  · **Buffer unit is a 5-minute wall-clock bucket** (`HH:00, HH:05, ...`):
    records arriving in the same bucket are combined into one file. Crossing a
    bucket boundary immediately flushes the previous bucket.

  · **GCS upload is atomic** — write to a temp file then a single PUT
    (no multi-part; files are small).

────────────────────────────────────────────────────────────────────────
Env vars (injected by entrypoint script):

  DATABENTO_API_KEY     — Databento Live API key ('db-' prefix)
  CME_GCS_BUCKET        — GCS bucket name (default: 'anomaly-cme-trades')
  CME_LOCAL_BUFFER_DIR  — temp parquet write location (default: '/tmp/cme_streamer')
"""

# ── stdlib ─────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── 3rd-party ──────────────────────────────────────────────────────────
# Note: `databento`, `google-cloud-storage`, `pyarrow` are needed only at VM runtime.
# enricher / health-monitor may import this module just for the `CME_TO_CONTINUOUS`
# constant, but those packages might not be installed in local / Cloud Run envs.
# So each import is wrapped in try/except, ensuring module load does not fail
# (the packages are only required at runtime call sites).
try:  # noqa: SIM105
    import databento as db  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    db = None  # type: ignore[assignment]

try:
    import pandas as pd  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    pd = None  # type: ignore[assignment]

try:
    import pyarrow as pa  # type: ignore
    import pyarrow.parquet as pq  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    pa = None  # type: ignore[assignment]
    pq = None  # type: ignore[assignment]

try:
    from google.cloud import storage  # type: ignore  # uses VM's default SA automatically
except ModuleNotFoundError:  # pragma: no cover
    storage = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# =====================================================================
# Constants
# =====================================================================
# ⚠️ Intentional DRY violation — this module is deployed self-contained on the VM,
# so we deliberately break the import dependency on `databento_client.py`. When
# adding symbols, update both places at the same time.
CME_TO_CONTINUOUS: dict[str, str] = {
    "CL": "CL.c.0",       # WTI front month
    "BZ": "BZ.c.0",       # Brent (CME) front month
    "ES": "ES.c.0",       # E-mini S&P front month
    "GC": "GC.c.0",       # Gold front month
}

DEFAULT_DATASET: str = "GLBX.MDP3"
DEFAULT_SCHEMA: str = "trades"

DEFAULT_BUCKET: str = "anomaly-cme-trades"
DEFAULT_LOCAL_BUFFER: Path = Path("/tmp/cme_streamer")

# 5 minutes = 300 seconds. Bucket boundaries align exactly with wall-clock HH:00, HH:05, etc.
BUCKET_SECONDS: int = 300

# Force flush when the in-memory buffer exceeds this row count (safety margin during active CME).
MAX_BUFFER_ROWS_PER_SYMBOL: int = 100_000

# WebSocket reconnect backoff (seconds)
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 60.0

# Heartbeat (P9.3.P3 Step D):
#   Every 60 seconds, overwrite a status JSON at a fixed GCS path.
#   The Daemon's StreamerHealthMonitor reads this file every 5 minutes and
#   emits a streamer-down alert if `now - ts > 6 minutes`.
HEARTBEAT_INTERVAL_SEC: int = 60
HEARTBEAT_BLOB: str = "_health/heartbeat.json"

# Reverse map of continuous symbols (`CL.c.0` → `CL`)
_CONTINUOUS_TO_ROOT: dict[str, str] = {v: k for k, v in CME_TO_CONTINUOUS.items()}


# =====================================================================
# Helpers
# =====================================================================
def _bucket_start_utc(ts: datetime, bucket_seconds: int = BUCKET_SECONDS) -> datetime:
    """Return the start time of the 5-min bucket containing the given UTC datetime.

    Example: 12:07:33 → 12:05:00. Floored against the epoch for wall-clock alignment.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    epoch_sec = int(ts.timestamp())
    floored = (epoch_sec // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _gcs_blob_name(root: str, bucket_start: datetime) -> str:
    """Hive-partitioned GCS object key.

    Example: trades/symbol=CL/date=2026-04-20/1205.parquet
    """
    date_str = bucket_start.strftime("%Y-%m-%d")
    time_str = bucket_start.strftime("%H%M")
    return f"trades/symbol={root}/date={date_str}/{time_str}.parquet"


def _raw_symbol_to_root(raw_symbol: str) -> str | None:
    """The first 2 characters of contract codes like `CLM6`, `ESH6` is the root.

    Returns None when the root is not in our watchlist (ignored).
    """
    if not raw_symbol:
        return None
    root = raw_symbol[:2]
    return root if root in CME_TO_CONTINUOUS else None


# =====================================================================
# Per-symbol rolling buffer
# =====================================================================
@dataclass
class _SymbolBuffer:
    """In-memory buffer for a single symbol + current bucket time."""

    root: str
    current_bucket: datetime | None = None       # bucket currently being collected
    rows: deque[dict[str, Any]] = field(default_factory=deque)


# =====================================================================
# CMELiveStreamer — main class
# =====================================================================
class CMELiveStreamer:
    """Databento Live WebSocket → 5-minute micro-batch Parquet → GCS.

    Lifecycle:
        streamer = CMELiveStreamer(api_key=..., gcs_bucket=...)
        await streamer.run()    # forever; reconnects internally

    Public API:
        run() / stop() / health_status()
    """

    def __init__(
        self,
        *,
        api_key: str,
        gcs_bucket: str = DEFAULT_BUCKET,
        roots: list[str] | None = None,
        local_buffer_dir: Path = DEFAULT_LOCAL_BUFFER,
        dataset: str = DEFAULT_DATASET,
        schema: str = DEFAULT_SCHEMA,
        bucket_seconds: int = BUCKET_SECONDS,
    ) -> None:
        if not api_key or not api_key.startswith("db-"):
            raise ValueError("api_key must start with 'db-' (DATABENTO_API_KEY env var)")
        if roots is None:
            roots = list(CME_TO_CONTINUOUS.keys())
        for r in roots:
            if r not in CME_TO_CONTINUOUS:
                raise ValueError(f"unknown root {r!r} — add to CME_TO_CONTINUOUS first")

        self._api_key = api_key
        self._gcs_bucket_name = gcs_bucket
        self._roots = roots
        self._dataset = dataset
        self._schema = schema
        self._bucket_seconds = bucket_seconds

        # Local temp folder (parquet write before GCS upload)
        self._local_dir = local_buffer_dir
        self._local_dir.mkdir(parents=True, exist_ok=True)

        # GCS client — VM uses its default SA (`storage.objectAdmin`)
        self._gcs = storage.Client()
        self._bucket = self._gcs.bucket(gcs_bucket)

        # Per-root in-memory buffer
        self._buffers: dict[str, _SymbolBuffer] = {
            r: _SymbolBuffer(root=r) for r in roots
        }

        # instrument_id (int) → root (str) — populated by SymbolMappingMsg
        self._inst_to_root: dict[int, str] = {}

        # health / metrics
        self._stop_event = asyncio.Event()
        self._records_received: int = 0
        self._records_dropped: int = 0
        self._files_uploaded: int = 0
        self._last_record_ts: datetime | None = None
        self._last_upload_ts: datetime | None = None

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        """Forever loop — restarts with backoff when a session dies.

        Calling `stop()` from outside performs a graceful shutdown (final buffer flush).
        The heartbeat task runs independently of the session and stays alive until stop.
        """
        # Heartbeat must run regardless of session crashes — its own task
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        backoff = INITIAL_BACKOFF_SEC
        try:
            while not self._stop_event.is_set():
                try:
                    await self._run_session()
                    backoff = INITIAL_BACKOFF_SEC  # session ended normally → reset
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception(
                        "CMELiveStreamer: session crashed (%s) — reconnect in %.1fs",
                        type(e).__name__, backoff,
                    )
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass  # normal — backoff elapsed, attempt reconnect
                    backoff = min(backoff * 2.0, MAX_BACKOFF_SEC)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._flush_all_buffers(reason="shutdown")

    def stop(self) -> None:
        """Graceful shutdown signal — `run()` exits on the next cycle."""
        self._stop_event.set()

    def health_status(self) -> dict[str, Any]:
        """For monitoring — called from daemon /snapshot or a cron health check."""
        now = datetime.now(timezone.utc)
        last_age_sec = (
            (now - self._last_record_ts).total_seconds()
            if self._last_record_ts else None
        )
        return {
            "records_received": self._records_received,
            "records_dropped": self._records_dropped,
            "files_uploaded": self._files_uploaded,
            "last_record_age_sec": last_age_sec,
            "last_upload_ts_utc": (
                self._last_upload_ts.isoformat() if self._last_upload_ts else None
            ),
            "buffered_rows": {r: len(b.rows) for r, b in self._buffers.items()},
            "instrument_map_size": len(self._inst_to_root),
        }

    # ─────────────────────────────────────────────────────────────────
    # Internal — 1 session = 1 WebSocket connection
    # ─────────────────────────────────────────────────────────────────
    async def _run_session(self) -> None:
        """Lifecycle of one session: subscribe → iterate records → on stop, flush.

        Databento Live is a sync blocking iterator, so we run it in a separate
        thread and ship records to the main loop via asyncio.Queue.
        """
        symbols_continuous = [CME_TO_CONTINUOUS[r] for r in self._roots]
        logger.info(
            "CMELiveStreamer: opening session — dataset=%s schema=%s symbols=%s",
            self._dataset, self._schema, symbols_continuous,
        )

        loop = asyncio.get_running_loop()
        record_queue: asyncio.Queue = asyncio.Queue(maxsize=50_000)
        thread_done = asyncio.Event()
        thread_error: dict[str, BaseException] = {}

        def _producer_thread() -> None:
            """Separate thread — consumes the Databento blocking iterator."""
            try:
                client = db.Live(key=self._api_key)
                client.subscribe(
                    dataset=self._dataset,
                    schema=self._schema,
                    symbols=symbols_continuous,
                    stype_in="continuous",
                )
                # We do not use snapshot subscriptions — we only need a fresh stream.
                for record in client:
                    if self._stop_event.is_set():
                        break
                    try:
                        loop.call_soon_threadsafe(record_queue.put_nowait, record)
                    except asyncio.QueueFull:
                        # main loop cannot keep up — drop
                        self._records_dropped += 1
            except BaseException as e:
                thread_error["err"] = e
            finally:
                loop.call_soon_threadsafe(thread_done.set)

        thread = threading.Thread(target=_producer_thread, daemon=True, name="db-live")
        thread.start()

        flush_task = asyncio.create_task(self._periodic_flush_loop())

        try:
            while not self._stop_event.is_set() and not thread_done.is_set():
                try:
                    record = await asyncio.wait_for(record_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue  # check stop on each tick
                self._handle_record(record)
        finally:
            flush_task.cancel()
            try:
                await flush_task
            except (asyncio.CancelledError, Exception):
                pass

            # If the producer thread is still running, signal stop
            self._stop_event.set() if thread_error else None

            # Flush buffers at session end — if the next session lands in the same bucket, records append.
            await self._flush_all_buffers(reason="session-end")

            if thread_error:
                raise RuntimeError(
                    f"producer thread died: {type(thread_error['err']).__name__}: "
                    f"{thread_error['err']}"
                )

    # ─────────────────────────────────────────────────────────────────
    # Record handling
    # ─────────────────────────────────────────────────────────────────
    def _handle_record(self, record: Any) -> None:
        """Handle a single record — symbology refresh or buffer append."""
        # SymbolMappingMsg → update instrument_id → raw_symbol → root mapping
        if isinstance(record, db.SymbolMappingMsg):
            raw = getattr(record, "stype_out_symbol", None)
            inst_id = getattr(record, "instrument_id", None)
            root = _raw_symbol_to_root(raw) if raw else None
            if inst_id is not None and root:
                self._inst_to_root[inst_id] = root
                logger.debug(
                    "CMELiveStreamer: symbol map updated inst_id=%s raw=%s root=%s",
                    inst_id, raw, root,
                )
            return

        # We only care about TradeMsg. Ignore other message types.
        if not isinstance(record, db.TradeMsg):
            return

        inst_id = record.instrument_id
        root = self._inst_to_root.get(inst_id)
        if root is None:
            return  # mapping not received yet, or symbol outside our watchlist

        # ts_event is ns-since-epoch (int) → UTC datetime
        ts_ns = int(record.ts_event)
        ts = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)

        # price is fixed-point int (1e-9 USD). size is contracts as-is.
        # Some SDK versions expose `record.pretty_*`; others do not — convert manually.
        price = float(record.price) / 1e9
        size = int(record.size)
        # side: 'A' (Ask, aggressor=buyer), 'B' (Bid, aggressor=seller), 'N' (none)
        side = record.side if isinstance(record.side, str) else chr(record.side)

        row = {
            "ts_event": ts,
            "instrument_id": inst_id,
            "price": price,
            "size": size,
            "side": side,
        }

        self._enqueue_row(root, ts, row)

        self._records_received += 1
        self._last_record_ts = datetime.now(timezone.utc)

    def _enqueue_row(self, root: str, ts: datetime, row: dict[str, Any]) -> None:
        """Push a row to the current-bucket buffer for the given root. Flush on bucket rollover."""
        buf = self._buffers[root]
        bucket_start = _bucket_start_utc(ts, self._bucket_seconds)

        # Crossed a bucket boundary — flush the previous bucket then start a new one
        if buf.current_bucket is not None and bucket_start > buf.current_bucket:
            asyncio.create_task(self._flush_buffer(root, reason="bucket-rollover"))
            # Don't wait for the task to drain the buffer asynchronously — to
            # prevent races, swap in a fresh deque immediately.
            buf.rows = deque()

        if buf.current_bucket is None:
            buf.current_bucket = bucket_start

        buf.rows.append(row)

        # overflow guard
        if len(buf.rows) >= MAX_BUFFER_ROWS_PER_SYMBOL:
            logger.warning(
                "CMELiveStreamer: buffer overflow root=%s rows=%d — forced flush",
                root, len(buf.rows),
            )
            asyncio.create_task(self._flush_buffer(root, reason="overflow"))
            buf.rows = deque()

    # ─────────────────────────────────────────────────────────────────
    # Periodic flush loop — flush once shortly after each bucket boundary
    # ─────────────────────────────────────────────────────────────────
    async def _periodic_flush_loop(self) -> None:
        """Flush all buffers 5 seconds after every 5-minute bucket boundary.

        +5s grace allows late records a brief moment to arrive.
        """
        try:
            while not self._stop_event.is_set():
                now = datetime.now(timezone.utc)
                next_bucket = _bucket_start_utc(now, self._bucket_seconds)
                # If next_bucket == current bucket start, advance to the next bucket (+5 min)
                next_flush = (
                    next_bucket
                    + pd.Timedelta(seconds=self._bucket_seconds + 5)
                )
                wait_sec = max(1.0, (next_flush - now).total_seconds())
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait_sec)
                    return  # stop signal — finally handles the flush
                except asyncio.TimeoutError:
                    pass
                # Flush all roots
                await self._flush_all_buffers(reason="periodic")
        except asyncio.CancelledError:
            raise

    async def _flush_all_buffers(self, *, reason: str) -> None:
        """Flush each root's current buffer (in parallel)."""
        await asyncio.gather(
            *(self._flush_buffer(r, reason=reason) for r in self._roots),
            return_exceptions=True,
        )

    async def _flush_buffer(self, root: str, *, reason: str) -> None:
        """Single root buffer → Parquet → GCS upload. No-op if empty."""
        buf = self._buffers[root]
        if not buf.rows or buf.current_bucket is None:
            return

        # Swap the current bucket data (subsequent records go to a new bucket)
        rows_snapshot = list(buf.rows)
        bucket_start = buf.current_bucket
        buf.rows = deque()
        # current_bucket gets set by the next record (leaving it None lets the first record refresh it)
        buf.current_bucket = None

        try:
            await asyncio.to_thread(
                self._write_and_upload, root, bucket_start, rows_snapshot,
            )
            self._files_uploaded += 1
            self._last_upload_ts = datetime.now(timezone.utc)
            logger.info(
                "CMELiveStreamer: flushed root=%s bucket=%s rows=%d reason=%s",
                root, bucket_start.isoformat(), len(rows_snapshot), reason,
            )
        except Exception as e:
            logger.exception(
                "CMELiveStreamer: flush failed root=%s bucket=%s rows=%d: %s "
                "— rows lost",
                root, bucket_start.isoformat(), len(rows_snapshot), e,
            )

    # ─────────────────────────────────────────────────────────────────
    # Heartbeat — every 60 seconds, overwrite the status snapshot on GCS
    # ─────────────────────────────────────────────────────────────────
    async def _heartbeat_loop(self) -> None:
        """Periodically write `_health/heartbeat.json` to GCS.

        The Daemon's StreamerHealthMonitor checks `ts` in this file to determine
        whether the streamer is alive. Write failures are only logged (retried
        on the next tick).
        """
        try:
            # First heartbeat after a brief delay so the session has time (~2-5s) to come up.
            await asyncio.sleep(2.0)
            while not self._stop_event.is_set():
                try:
                    await asyncio.to_thread(self._write_heartbeat)
                except Exception as e:
                    logger.warning(
                        "CMELiveStreamer: heartbeat write failed (%s) — next tick retry",
                        type(e).__name__,
                    )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=HEARTBEAT_INTERVAL_SEC,
                    )
                    return  # stop signal
                except asyncio.TimeoutError:
                    pass  # normal — next heartbeat
        except asyncio.CancelledError:
            raise

    def _write_heartbeat(self) -> None:
        """Sync helper — write one health JSON and overwrite the GCS blob."""
        now = datetime.now(timezone.utc)
        last_age_sec = (
            (now - self._last_record_ts).total_seconds()
            if self._last_record_ts else None
        )
        payload = {
            "ts": now.isoformat(),
            "records_received": self._records_received,
            "records_dropped": self._records_dropped,
            "files_uploaded": self._files_uploaded,
            "last_record_age_sec": last_age_sec,
            "buffered_rows": {r: len(b.rows) for r, b in self._buffers.items()},
            "instrument_map_size": len(self._inst_to_root),
            "roots": list(self._roots),
        }
        blob = self._bucket.blob(HEARTBEAT_BLOB)
        # application/json — inline preview-able in the GCS console
        blob.upload_from_string(
            json.dumps(payload, indent=2),
            content_type="application/json",
        )

    def _write_and_upload(
        self, root: str, bucket_start: datetime, rows: list[dict[str, Any]],
    ) -> None:
        """Sync helper — write parquet with pyarrow, upload to GCS."""
        # 1) DataFrame → pyarrow Table → Parquet file (snappy compression)
        df = pd.DataFrame(rows)
        df = df.sort_values("ts_event").reset_index(drop=True)
        table = pa.Table.from_pandas(df, preserve_index=False)

        date_str = bucket_start.strftime("%Y-%m-%d")
        time_str = bucket_start.strftime("%H%M")
        local_path = self._local_dir / f"{root}_{date_str}_{time_str}.parquet"
        pq.write_table(table, str(local_path), compression="snappy")

        # 2) GCS upload
        blob_name = _gcs_blob_name(root, bucket_start)
        blob = self._bucket.blob(blob_name)
        blob.upload_from_filename(str(local_path), content_type="application/octet-stream")

        # 3) clean up local temp file
        try:
            local_path.unlink()
        except OSError:
            pass


__all__ = [
    "CMELiveStreamer",
    "DEFAULT_BUCKET",
    "DEFAULT_LOCAL_BUFFER",
    "BUCKET_SECONDS",
]
