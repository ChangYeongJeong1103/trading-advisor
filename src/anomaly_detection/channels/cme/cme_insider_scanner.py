"""
cme/cme_insider_scanner.py — 24/7 GCS-polling scanner for cme_insider_v1.

────────────────────────────────────────────────────────────────────────
Responsibilities (P12-B Production wiring):

  Background task that polls the raw trade Parquet files written to GCS by
  CMELiveStreamer, and runs CMEFeatures + CMEInsiderV1Detector 24/7.

  Fires alerts independently of TV webhooks. When tier > NORMAL, push to
  CMEChannel._latest_signal → orchestrator pulls it on the next 5s cycle →
  ChannelAlertDispatcher sends email/telegram if cooldown passes.

────────────────────────────────────────────────────────────────────────
Data flow:

  ┌─ Databento WebSocket  ─→ CMELiveStreamer (VM)  ─→ GCS Parquet
  │                                                       │
  │              (here)                       60s polling │
  │  CMEInsiderScanner (Cloud Run) ←──────────────────────┘
  │           │
  │           ▼  features.add_events + compute_snapshot
  │      CMEFeatures
  │           │
  │           ▼  detector.evaluate
  │   CMEInsiderV1Detector
  │           │
  │           ▼  push (max-tier merge)
  └─→ CMEChannel._latest_signal[symbol]

────────────────────────────────────────────────────────────────────────
Design decisions:

  · **Incremental polling** — every 60s, fetch only trades "after the last
    read ts" from GCS and append to the features buffer. CMEFeatures buffer
    keeps ~60 min of history itself → no re-fetch needed. Backfill 60 min
    only once on cold start (process restart).

  · **Polling interval = 60s** — there is an average ~2.5 min latency
    between the streamer's 5min bucket flush and the actual trade arrival.
    With the additional 60s poll, worst-case alert latency is ~6 min.
    Sufficient for P12-B's "catch bursts that TV misses" goal (meaningful
    within the 24h cooldown).

  · **Per-symbol independent processing** — a single symbol's read/eval
    failure does not block other symbols' polling (per-symbol try/except).

  · **In-process with CMEChannel** (inside the Cloud Run daemon)
    → state injection via direct callback. No IPC / HTTP required.

  · **Trade reader is injectable** — GCS in production, in-memory list etc.
    swappable for tests/replay.

────────────────────────────────────────────────────────────────────────
Failure isolation:

  - GCS read timeout / exception → log + auto retry on the next cycle
  - 0-row data (streamer dead or pre-flush) → silent skip (normal)
  - features baseline not ready → detector returns NORMAL (handled naturally)

  Scanner's own crashes are caught by try/except in the task — the daemon itself does not die.

────────────────────────────────────────────────────────────────────────
Architecture: §3 Process View, §5.2 Channel pipeline, §5.4.1 channel tier
"""

from __future__ import annotations

# ── stdlib ─────────────────────────────────────────────────────────────
import asyncio
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

# ── 3rd-party (lazy import — module loads even in VM-less environments) ──
import pandas as pd

# ── local ─────────────────────────────────────────────────────────────
from ...alerts.alert_ohlc_buffer import AlertOhlcBuffer
from ...core.schemas import (
    CHANNEL_CME,
    ChannelSignal,
    NormalizedEvent,
    Side,
    Tier,
)
from .cme_insider_v1 import (
    CMEInsiderV1Detector,
    DEFAULT_INSIDER_THRESHOLDS,
    InsiderSymbolThresholds,
)
from .features import CMEFeatures
from .live_streamer import CME_TO_CONTINUOUS

logger = logging.getLogger(__name__)


# =====================================================================
# Trade reader — abstraction (for injection)
# =====================================================================
# (symbol, start_dt, end_dt) → list of NormalizedEvent (CMEFeatures input format)
TradeReader = Callable[
    [str, datetime, datetime],
    "Awaitable[list[NormalizedEvent]]",
]


# =====================================================================
# Config
# =====================================================================
@dataclass(frozen=True)
class InsiderScannerConfig:
    """CMEInsiderScanner runtime parameters.

    Attributes:
        gcs_bucket:           name of the bucket CMELiveStreamer writes to.
        scan_interval_s:      polling interval (seconds). default 60s.
        warmup_lookback_min:  minutes to backfill on cold start. default 70min
                              (CMEFeatures baseline 35min + slack).
        gcs_read_timeout_s:   GCS read timeout.
        thresholds:           per-symbol detector threshold override.
                              Defaults to DEFAULT_INSIDER_THRESHOLDS.
    """
    gcs_bucket: str = "anomaly-cme-trades"
    scan_interval_s: float = 60.0
    warmup_lookback_min: int = 70
    gcs_read_timeout_s: float = 10.0
    thresholds: dict[str, InsiderSymbolThresholds] = field(
        default_factory=lambda: dict(DEFAULT_INSIDER_THRESHOLDS),
    )


# =====================================================================
# CMEInsiderScanner — main class
# =====================================================================
class CMEInsiderScanner:
    """Background scanner: GCS poll → features → detector → channel push.

    Lifecycle:
        await scanner.start()   # warmup backfill + start
        ... (background loop auto-runs)
        await scanner.stop()    # graceful shutdown

    Thread-safety: only the single background task mutates buffer/state.
    The channel callback is sync — safe inside the event loop because it
    only mutates a dict in an async environment like Cloud Run.
    """

    def __init__(
        self,
        *,
        symbols: list[str],
        signal_callback: Callable[[ChannelSignal], None],
        config: Optional[InsiderScannerConfig] = None,
        trade_reader: Optional[TradeReader] = None,
        gcs_client=None,            # google.cloud.storage.Client (lazy)
        now_provider: Optional[Callable[[], datetime]] = None,
        ohlc_buffer: AlertOhlcBuffer | None = None,
    ) -> None:
        """
        Args:
            symbols: CME root symbols to scan (e.g. ["BZ","CL","ES","GC"]).
                Must be in CME_TO_CONTINUOUS (silently skipped otherwise).
            signal_callback: fn that pushes tier > NORMAL signals to the channel.
                Wired to CMEChannel._inject_insider_signal.
            config: runtime parameters — default if unset.
            trade_reader: GCS read abstraction. Production GCS reader is
                lazy-initialised when unset.
            gcs_client: google.cloud.storage.Client. Lazy-initialised with
                default credentials when unset.
        """
        self._cfg = config or InsiderScannerConfig()

        # symbols must be roots the streamer knows.
        self._symbols = [s for s in symbols if s in CME_TO_CONTINUOUS]
        skipped = [s for s in symbols if s not in CME_TO_CONTINUOUS]
        if skipped:
            logger.warning(
                "CMEInsiderScanner: symbols not in streamer roots, skipped: %s",
                skipped,
            )

        self._signal_callback = signal_callback

        # Per-symbol features — 1 buffer per symbol.
        # CMEFeatures internally keeps _SymbolState as a dict, but a single
        # instance can handle all symbols. Use one for clarity.
        self._features = CMEFeatures()
        self._detector = CMEInsiderV1Detector(thresholds=self._cfg.thresholds)

        # Per-symbol last-successfully-read trade ts (for incremental polling).
        self._last_read_ts: dict[str, datetime] = {}

        # Trade reader injection — GCS in production.
        self._trade_reader = trade_reader  # None → lazy GCS reader
        self._gcs_client = gcs_client

        # Clock injection — test/replay can inject simulated time.
        # Production uses wall-clock.
        self._now_provider: Callable[[], datetime] = (
            now_provider or (lambda: datetime.now(timezone.utc))
        )

        # Lifecycle.
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._cycles_run = 0
        self._signals_emitted = 0

        # P12-D — data for alert PNG price/volume panel.
        # _poll_symbol pushes each NormalizedEvent after _read_trades.
        # None → skip push (mock test / legacy behavior).
        self._ohlc_buffer = ohlc_buffer

    # P12-D — provide a setter so the buffer can be wired later when the
    # channel injects the scanner externally (used by CMEChannel.__init__).
    def set_ohlc_buffer(self, buffer: AlertOhlcBuffer | None) -> None:
        """Inject / replace the alert OHLC buffer after construction."""
        self._ohlc_buffer = buffer

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Warmup backfill + spawn background loop.

        Idempotent. No-op if already running.
        """
        if self._task is not None and not self._task.done():
            logger.warning("CMEInsiderScanner.start: already running, ignoring")
            return

        self._stop_event = asyncio.Event()

        # ── Warmup backfill ─────────────────────────────────────────
        # Pre-fetch 70 min of data so the first cycle starts with baseline_ready=True.
        # Loop still starts on failure — natural accumulation in subsequent cycles.
        try:
            await self._warmup_backfill()
        except Exception as e:
            logger.exception(
                "CMEInsiderScanner: warmup backfill failed (continuing): %s", e,
            )

        self._task = asyncio.create_task(
            self._scan_loop(), name="cme-insider-scan-loop",
        )
        logger.info(
            "CMEInsiderScanner: started (symbols=%s, interval=%.0fs, warmup=%dmin)",
            self._symbols, self._cfg.scan_interval_s, self._cfg.warmup_lookback_min,
        )

    async def stop(self) -> None:
        """Stop the background loop safely. Idempotent."""
        if self._stop_event is None:
            return
        if self._stop_event.is_set():
            return

        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(
                    self._task, timeout=self._cfg.scan_interval_s + 2.0,
                )
            except asyncio.TimeoutError:
                logger.warning("CMEInsiderScanner.stop: forcing cancel")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, BaseException):
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

        logger.info(
            "CMEInsiderScanner: stopped (cycles=%d, signals=%d)",
            self._cycles_run, self._signals_emitted,
        )

    # ─────────────────────────────────────────────────────────────────
    # Warmup
    # ─────────────────────────────────────────────────────────────────
    async def _warmup_backfill(self) -> None:
        """Fetch 70 min of data on cold start → fill features buffer.

        Ensures baseline_ready becomes True immediately. Does not run the
        detector (real detection starts from the first polling cycle).
        """
        now = self._now_provider()
        start = now - timedelta(minutes=self._cfg.warmup_lookback_min)

        for sym in self._symbols:
            try:
                events = await self._read_trades(sym, start=start, end=now)
                if events:
                    self._features.add_events(events)
                    # initialize last_read_ts — most recent trade ts.
                    self._last_read_ts[sym] = max(e.ts_source for e in events)
                logger.info(
                    "CMEInsiderScanner.warmup: %s → %d events backfilled",
                    sym, len(events),
                )
            except Exception as e:
                logger.warning(
                    "CMEInsiderScanner.warmup: %s failed: %s — will retry next cycle",
                    sym, e,
                )

    # ─────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────
    async def _scan_loop(self) -> None:
        """Poll + evaluate every symbol once per scan_interval_s."""
        assert self._stop_event is not None

        # Small jitter — avoids synchronized bursts with other polling tasks.
        await asyncio.sleep(2.0)

        while not self._stop_event.is_set():
            cycle_start = self._now_provider()
            try:
                await self._poll_once(now=cycle_start)
                self._cycles_run += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(
                    "CMEInsiderScanner: cycle failed (continuing): %s", e,
                )

            # Next interval — exit immediately on stop signal.
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._cfg.scan_interval_s,
                )
            except asyncio.TimeoutError:
                continue
            else:
                break

    async def _poll_once(self, *, now: datetime) -> None:
        """One cycle — incremental fetch + detector evaluate for all symbols."""
        for sym in self._symbols:
            try:
                await self._poll_symbol(sym, now=now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(
                    "CMEInsiderScanner: poll failed for %s: %s", sym, e,
                )

    async def _poll_symbol(self, sym: str, *, now: datetime) -> None:
        """One symbol per cycle — incremental read → features → detector → push."""
        # ── 1) Incremental fetch window ─────────────────────────────
        # Only trades after last_read_ts. Full lookback on first run (warmup failed).
        last = self._last_read_ts.get(sym)
        if last is None:
            start = now - timedelta(minutes=self._cfg.warmup_lookback_min)
        else:
            # Slight overlap to avoid boundary drops (1 second).
            start = last - timedelta(seconds=1)

        events = await self._read_trades(sym, start=start, end=now)

        # Avoid duplicate trades — push only strictly later than last_read_ts.
        if last is not None:
            events = [e for e in events if e.ts_source > last]

        if events:
            self._features.add_events(events)
            self._last_read_ts[sym] = max(e.ts_source for e in events)

            # P12-D — push data for the alert PNG price/volume panel.
            # NormalizedEvent.size_usd is price × contracts × multiplier, so
            # the unit is USD notional. The Email/Telegram/X volume axis is
            # labelled in USD as well. side: BUY → "buy", SELL → "sell", None → "neutral".
            if self._ohlc_buffer is not None:
                for ev in events:
                    try:
                        if ev.side == Side.BUY:
                            ohlc_side = "buy"
                        elif ev.side == Side.SELL:
                            ohlc_side = "sell"
                        else:
                            ohlc_side = "neutral"
                        self._ohlc_buffer.push_trade(
                            channel=CHANNEL_CME,
                            symbol=sym,
                            ts=ev.ts_source,
                            price=float(ev.price or 0.0),
                            size=float(ev.size_usd or 0.0),
                            side=ohlc_side,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "CMEInsiderScanner: ohlc_buffer.push_trade failed: %s",
                            e,
                        )

        # ── 2) Snapshot + evaluate ──────────────────────────────────
        snap = self._features.compute_snapshot(sym, now=now)
        if snap is None:
            return
        signal = self._detector.evaluate(snap)

        # ── 3) Push only when alert-worthy ──────────────────────────
        # Skip NORMAL to avoid polluting channel state.
        if signal.tier == Tier.NORMAL:
            return

        try:
            self._signal_callback(signal)
            self._signals_emitted += 1
            logger.info(
                "CMEInsiderScanner: emit %s tier=%s reasons=%s",
                sym, signal.tier.value, signal.reason_codes,
            )
        except Exception as e:
            logger.error(
                "CMEInsiderScanner: signal_callback failed for %s: %s", sym, e,
            )

    # ─────────────────────────────────────────────────────────────────
    # Trade reader — production GCS or injected
    # ─────────────────────────────────────────────────────────────────
    async def _read_trades(
        self, sym: str, *, start: datetime, end: datetime,
    ) -> list[NormalizedEvent]:
        """Trade reader call wrapper (with timeout)."""
        reader = self._trade_reader or self._read_trades_from_gcs
        try:
            return await asyncio.wait_for(
                reader(sym, start, end),
                timeout=self._cfg.gcs_read_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "CMEInsiderScanner: read timeout for %s [%s, %s]",
                sym, start.isoformat(), end.isoformat(),
            )
            return []

    async def _read_trades_from_gcs(
        self, sym: str, start: datetime, end: datetime,
    ) -> list[NormalizedEvent]:
        """Default production reader — GCS Parquet → NormalizedEvent.

        Hive-partitioned path:
            gs://<bucket>/trades/symbol={ROOT}/date=YYYY-MM-DD/HHMM.parquet
        Streamer flushes in 5min buckets — read all bucket files covering
        start ~ end and convert row by row.

        Sync GCS read is offloaded to a thread pool → does not block the event loop.
        """
        df = await asyncio.to_thread(
            self._read_trades_df_sync, sym, start, end,
        )
        if df.empty:
            return []
        return _df_to_normalized_events(df, sym)

    def _read_trades_df_sync(
        self, sym: str, start: datetime, end: datetime,
    ) -> pd.DataFrame:
        """Sync GCS read — same pattern as the enricher."""
        client = self._get_gcs_client()
        bucket = client.bucket(self._cfg.gcs_bucket)

        # Cover 5min bucket boundaries.
        bucket_seconds = 300
        cursor = _floor_to_bucket(start, bucket_seconds)
        last = _floor_to_bucket(end, bucket_seconds)

        # One list_blobs per date prefix (small efficiency).
        prefixes_by_date: dict[str, str] = {}
        cur = cursor
        while cur <= last:
            date_str = cur.strftime("%Y-%m-%d")
            prefixes_by_date.setdefault(
                date_str, f"trades/symbol={sym}/date={date_str}/",
            )
            cur += timedelta(seconds=bucket_seconds)

        frames: list[pd.DataFrame] = []
        for date_str, prefix in prefixes_by_date.items():
            for blob in bucket.list_blobs(prefix=prefix):
                if not blob.name.endswith(".parquet"):
                    continue
                fname = blob.name.rsplit("/", 1)[-1]    # e.g. "2030.parquet"
                hhmm = fname.split(".", 1)[0]
                if len(hhmm) != 4 or not hhmm.isdigit():
                    continue
                bucket_dt = datetime(
                    year=int(date_str[:4]), month=int(date_str[5:7]),
                    day=int(date_str[8:10]),
                    hour=int(hhmm[:2]), minute=int(hhmm[2:]),
                    tzinfo=timezone.utc,
                )
                bucket_end = bucket_dt + timedelta(seconds=bucket_seconds)
                # Skip if it doesn't overlap with the window.
                if bucket_end <= start or bucket_dt >= end + timedelta(seconds=1):
                    continue
                try:
                    raw = blob.download_as_bytes()
                    frames.append(pd.read_parquet(io.BytesIO(raw)))
                except Exception as e:
                    logger.warning(
                        "CMEInsiderScanner: skip unreadable blob %s: %s",
                        blob.name, e,
                    )

        if not frames:
            return pd.DataFrame()

        df = pd.concat(frames, ignore_index=True)
        if "ts_event" in df.columns:
            df["ts_event"] = pd.to_datetime(df["ts_event"], utc=True)
            df = df.sort_values("ts_event")
            # Window trim — inclusive of start ~ inclusive of end.
            df = df[(df["ts_event"] >= start) & (df["ts_event"] <= end)]
        return df

    def _get_gcs_client(self):
        """Lazy init google.cloud.storage.Client."""
        if self._gcs_client is None:
            from google.cloud import storage     # noqa: import-outside-toplevel
            self._gcs_client = storage.Client()
        return self._gcs_client

    # ─────────────────────────────────────────────────────────────────
    # Health / debug
    # ─────────────────────────────────────────────────────────────────
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def cycles_run(self) -> int:
        return self._cycles_run

    @property
    def signals_emitted(self) -> int:
        return self._signals_emitted


# =====================================================================
# Helpers
# =====================================================================
# CME contract notional multiplier (price × size × multiplier = USD).
# Must match the live_streamer root keys.
_NOTIONAL_MULTIPLIER: dict[str, float] = {
    "CL": 1000.0,    # WTI: 1000 barrels
    "BZ": 1000.0,    # Brent: 1000 barrels
    "ES": 50.0,      # E-mini S&P: $50 / point
    "GC": 100.0,     # Gold: 100 troy oz
}


def _df_to_normalized_events(df: pd.DataFrame, sym: str) -> list[NormalizedEvent]:
    """Streamer parquet rows → NormalizedEvent list (CMEFeatures input).

    Streamer schema: ts_event, instrument_id, price, size, side.
    """
    mult = _NOTIONAL_MULTIPLIER.get(sym, 1.0)
    if mult == 1.0:
        logger.warning(
            "_df_to_normalized_events: unknown multiplier for %s, using 1.0", sym,
        )

    out: list[NormalizedEvent] = []
    for row in df.itertuples(index=False):
        ts = row.ts_event
        # pandas Timestamp → python datetime (UTC).
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        price = float(row.price)
        size = int(row.size)
        size_usd = price * size * mult
        # P12-D — preserve aggressor side from streamer schema (A/B/N).
        # 'A' = Ask hit  → aggressor=buyer  → Side.BUY
        # 'B' = Bid hit  → aggressor=seller → Side.SELL
        # 'N' or other   → side=None (unknown / non-trade record)
        raw_side = getattr(row, "side", None)
        if raw_side == "A":
            side = Side.BUY
        elif raw_side == "B":
            side = Side.SELL
        else:
            side = None
        out.append(NormalizedEvent(
            channel=CHANNEL_CME,
            symbol=sym,
            ts_source=ts,
            ts_ingest=ts,
            side=side,
            size_usd=size_usd,
            price=price,
            meta={},
            raw_ref="cme-insider-scanner",
        ))
    return out


def _floor_to_bucket(dt: datetime, bucket_seconds: int) -> datetime:
    """Floor to an arbitrary bucket boundary (same logic as the streamer)."""
    epoch = int(dt.timestamp())
    floored = epoch - (epoch % bucket_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


__all__ = [
    "CMEInsiderScanner",
    "InsiderScannerConfig",
    "TradeReader",
]
