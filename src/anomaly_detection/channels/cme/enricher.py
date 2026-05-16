"""
channels/cme/enricher.py — CME alert enrichment (GCS Live Parquet + post-analysis).

────────────────────────────────────────────────────────────────────────
Responsibilities (P9.3.P3 Step D — Live Streamer architecture):

  Called by AlertRouter right before dispatch on RISK_OFF / EMERGENCY tiers.
  Takes the ChannelSignal emitted by the TradingView primary trigger (CMEChannel) and:

    1. Reads the raw trade Parquet files written to GCS by CMELiveStreamer
       (gs://<bucket>/trades/symbol={ROOT}/date=YYYY-MM-DD/HHMM.parquet)
    2. Computes precise metrics via post_analysis.run_post_analysis()
    3. Appends triggered metrics to ChannelSignal.reason_codes
       → email/telegram renderer automatically surfaces them in the alert body

  Design changes (vs. the previous Historical API version):
    · No Databento Historical API call — removes both latency and cost issues
    · Direct GCS read (us-west1 region → ms-level for Cloud Run in same region)
    · If streamer is stopped → empty dataframe → `POST_ANALYSIS_NO_DATA`
      (StreamerHealthMonitor emits a separate alert to drive root-cause action)

  Failure isolation:
    - GCS read timeout / exception → `POST_ANALYSIS_FAILED: gcs_read ...`
    - 0 rows (before buffer flush, or streamer down) → `POST_ANALYSIS_NO_DATA`
    - cap / root not mapped  → `POST_ANALYSIS_SKIPPED: ...`
    - post-analysis exception → `POST_ANALYSIS_FAILED: post_analysis ...`

  In any case the alert itself still dispatches normally (enrichment does not block alerts).

────────────────────────────────────────────────────────────────────────
Window policy:
  Read minute-granular Parquet files covering [event_ts - 15min, event_ts + 1min].
  The streamer flushes every BUCKET_SECONDS (300s = 5 min), so in the worst case
  the bucket right before event_ts may not yet be uploaded to GCS. In that case
  "previous 4-min bucket + current in-progress bucket (partial)" may be empty,
  yielding NO_DATA. → Rather than re-analyzing 5 minutes later, operationally we
  treat "real-time enrichment as best-effort" and surface it to the user as-is.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from ...core.schemas import CHANNEL_CME, ChannelSignal, Tier
from .live_streamer import CME_TO_CONTINUOUS
from .post_analysis import (
    PostAnalysisThresholds,
    ROOT_THRESHOLDS,
    run_post_analysis,
)
from .tradingview_adapter import _parse_root_symbol

logger = logging.getLogger(__name__)


# =====================================================================
# Config
# =====================================================================
@dataclass(frozen=True)
class EnrichmentConfig:
    """CMEEnricher runtime parameters.

    Attributes:
        gcs_bucket:
            Name of the bucket CMELiveStreamer writes to (e.g. "anomaly-cme-trades").
        fetch_window_minutes_before:
            Fetch start offset (minutes) before event_ts.
            post_analysis looks at [event_ts-5m, event_ts), this adds boundary slack.
        fetch_window_minutes_after:
            Fetch end offset (minutes) after event_ts.
            +1 = covers the situation just after the final minute ended (may be pre-flush).
        fetch_timeout_s:
            Total timeout for the GCS read.
            5s is enough in the same region. If not finished, append PENDING reason.
        enable_for_tiers:
            Set of tiers to call enrichment on. RISK_OFF + EMERGENCY by default.
    """

    gcs_bucket: str = "anomaly-cme-trades"
    fetch_window_minutes_before: int = 15
    fetch_window_minutes_after: int = 1
    fetch_timeout_s: float = 5.0
    enable_for_tiers: frozenset[Tier] = field(
        default_factory=lambda: frozenset({Tier.RISK_OFF, Tier.EMERGENCY})
    )


# reason_codes prefixes — email/telegram renderer surfaces these verbatim
_PREFIX_TRIGGER = "POST_ANOMALY"
_PREFIX_NODATA  = "POST_ANALYSIS_NO_DATA"
_PREFIX_PENDING = "POST_ANALYSIS_PENDING"
_PREFIX_SKIPPED = "POST_ANALYSIS_SKIPPED"
_PREFIX_FAILED  = "POST_ANALYSIS_FAILED"


# =====================================================================
# CMEEnricher — public API
# =====================================================================
class CMEEnricher:
    """Attach GCS-live-parquet-based precise metrics to a CME ChannelSignal.

    Lifecycle: stateless (GCS client is thread-safe, reused internally).
    Thread-safety: safe for concurrent multi-task calls.
    """

    def __init__(
        self,
        *,
        config: Optional[EnrichmentConfig] = None,
        post_thresholds: Optional[PostAnalysisThresholds] = None,
        gcs_client=None,  # tests may inject a fake (None → lazy import)
    ) -> None:
        """
        Args:
            config: EnrichmentConfig — default when unset.
            post_thresholds: PostAnalysisThresholds — defaults to ROOT_THRESHOLDS when unset.
            gcs_client: google.cloud.storage.Client instance. When unset, created
                with default credentials on first use (using the Cloud Run SA).
        """
        self._cfg = config or EnrichmentConfig()
        self._thresholds = post_thresholds
        self._gcs_client = gcs_client  # lazy init — daemon still boots even on import failure

    # ─────────────────────────────────────────────────────────────────
    # 1) Decision-level entry — called by AlertRouter
    # ─────────────────────────────────────────────────────────────────
    async def enrich_decision(
        self,
        *,
        decision_tier: Tier,
        contributing: list[ChannelSignal],
    ) -> list[ChannelSignal]:
        """Enrich only CME signals in `contributing`. Pass through other channels."""
        # 1) tier gate — skip if not RISK_OFF / EMERGENCY
        if decision_tier not in self._cfg.enable_for_tiers:
            return contributing

        cme_signals = [s for s in contributing if s.channel == CHANNEL_CME]
        if not cme_signals:
            return contributing

        # 2) Parallel enrich, with safe wrappers so one failure does not block others
        enriched_results = await asyncio.gather(
            *(self._safe_enrich_signal(s) for s in cme_signals),
            return_exceptions=False,
        )
        enriched_by_id = {orig.id: new for orig, new in zip(cme_signals, enriched_results)}
        return [enriched_by_id.get(s.id, s) for s in contributing]

    # ─────────────────────────────────────────────────────────────────
    # 2) Single-signal enrich (exception-absorbing wrapper)
    # ─────────────────────────────────────────────────────────────────
    async def _safe_enrich_signal(self, signal: ChannelSignal) -> ChannelSignal:
        try:
            return await self._enrich_signal(signal)
        except Exception as e:
            logger.exception(
                "CMEEnricher: unexpected error enriching signal %s (symbol=%s): %s",
                signal.id, signal.symbol, e,
            )
            return _append_reason(
                signal,
                f"{_PREFIX_FAILED}: unexpected ({type(e).__name__})",
            )

    async def _enrich_signal(self, signal: ChannelSignal) -> ChannelSignal:
        """Body of a single CME signal enrich — GCS read + post-analysis."""
        # 1) symbol → root parsing
        try:
            root = _parse_root_symbol(signal.symbol)
        except ValueError as e:
            logger.info(
                "CMEEnricher: symbol %r → failed to parse root, skip: %s",
                signal.symbol, e,
            )
            return _append_reason(
                signal, f"{_PREFIX_SKIPPED}: unknown root for {signal.symbol!r}",
            )

        # 2) root gate
        if root not in CME_TO_CONTINUOUS:
            return _append_reason(
                signal, f"{_PREFIX_SKIPPED}: root {root!r} not streamed",
            )
        if root not in ROOT_THRESHOLDS:
            return _append_reason(
                signal, f"{_PREFIX_SKIPPED}: root {root!r} has no calibrated thresholds",
            )

        # 3) fetch window
        event_ts = signal.ts.astimezone(timezone.utc)
        start = _floor_to_minute(
            event_ts - timedelta(minutes=self._cfg.fetch_window_minutes_before)
        )
        end = _floor_to_minute(
            event_ts + timedelta(minutes=self._cfg.fetch_window_minutes_after)
        )

        # 4) GCS read with timeout
        try:
            trades_df = await asyncio.wait_for(
                asyncio.to_thread(self._read_trades_from_gcs, root, start, end),
                timeout=self._cfg.fetch_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "CMEEnricher: GCS read timeout (>%ss) root=%s window=[%s, %s]",
                self._cfg.fetch_timeout_s, root, start.isoformat(), end.isoformat(),
            )
            return _append_reason(
                signal,
                f"{_PREFIX_PENDING}: GCS read >{self._cfg.fetch_timeout_s:.0f}s timeout",
            )
        except Exception as e:
            logger.exception(
                "CMEEnricher: GCS read failed (root=%s): %s", root, e,
            )
            return _append_reason(
                signal, f"{_PREFIX_FAILED}: gcs_read ({type(e).__name__})",
            )

        # 5) 0 rows — streamer is down or there's a flush timing issue
        if trades_df.empty:
            logger.warning(
                "CMEEnricher: no trades in GCS for root=%s window=[%s, %s] — "
                "check streamer health (StreamerHealthMonitor emits a separate alert)",
                root, start.isoformat(), end.isoformat(),
            )
            return _append_reason(
                signal,
                f"{_PREFIX_NODATA}: no live trades in [{start.strftime('%H:%M')}, "
                f"{end.strftime('%H:%M')} UTC] — streamer/buffer gap",
            )

        # 6) Post-analysis
        try:
            result = run_post_analysis(
                trades_df,
                event_ts=event_ts,
                root=root,
                thresholds=self._thresholds,
            )
        except Exception as e:
            logger.exception(
                "CMEEnricher: post_analysis failed (root=%s, rows=%d): %s",
                root, len(trades_df), e,
            )
            return _append_reason(
                signal, f"{_PREFIX_FAILED}: post_analysis ({type(e).__name__})",
            )

        # 7) Triggered metrics → reason_codes
        if not result.has_trigger:
            logger.info(
                "CMEEnricher: no metric triggered (root=%s, computed=%d) — "
                "dispatch with primary signal only",
                root, len(result.metrics),
            )
            return signal

        new_codes = [f"{_PREFIX_TRIGGER}: {msg}" for msg in result.triggered.values()]
        for w in result.warnings:
            new_codes.append(f"{_PREFIX_TRIGGER}_WARN: {w}")

        logger.info(
            "CMEEnricher: enriched signal %s (root=%s) with %d trigger(s)",
            signal.id, root, len(result.triggered),
        )
        return _append_reasons(signal, new_codes)

    # ─────────────────────────────────────────────────────────────────
    # 3) GCS Parquet read — sync helper (called inside to_thread)
    # ─────────────────────────────────────────────────────────────────
    def _read_trades_from_gcs(
        self,
        root: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Read Hive-partitioned parquet files within the covered range and concat.

        Path format: gs://<bucket>/trades/symbol={ROOT}/date=YYYY-MM-DD/HHMM.parquet
        Streamer flushes per BUCKET_SECONDS (300s=5min) on HHMM boundaries.

        Date-boundary cases are handled naturally (the date varies across enumeration).
        """
        client = self._get_client()
        bucket = client.bucket(self._cfg.gcs_bucket)

        # Build the set of (date_str, hhmm_str) for the 5-min buckets to cover
        prefixes_by_date: dict[str, str] = {}  # date_str → prefix
        cursor = _floor_to_bucket(start, bucket_seconds=300)
        last = _floor_to_bucket(end, bucket_seconds=300)
        while cursor <= last:
            date_str = cursor.strftime("%Y-%m-%d")
            prefixes_by_date.setdefault(
                date_str,
                f"trades/symbol={root}/date={date_str}/",
            )
            cursor += timedelta(seconds=300)

        # Filter blobs whose HHMM lies in our window, under each date prefix
        frames: list[pd.DataFrame] = []
        for date_str, prefix in prefixes_by_date.items():
            blobs = bucket.list_blobs(prefix=prefix)
            for blob in blobs:
                if not blob.name.endswith(".parquet"):
                    continue
                # blob.name = "trades/symbol=CL/date=2026-04-19/1430.parquet"
                fname = blob.name.rsplit("/", 1)[-1]  # "1430.parquet"
                hhmm = fname.split(".", 1)[0]        # "1430"
                if len(hhmm) != 4 or not hhmm.isdigit():
                    continue
                bucket_dt = datetime(
                    year=int(date_str[:4]),
                    month=int(date_str[5:7]),
                    day=int(date_str[8:10]),
                    hour=int(hhmm[:2]),
                    minute=int(hhmm[2:]),
                    tzinfo=timezone.utc,
                )
                # Check if the bucket overlaps the window (assumes 5-min bucket size)
                bucket_end = bucket_dt + timedelta(seconds=300)
                if bucket_end <= start or bucket_dt >= end + timedelta(seconds=1):
                    continue
                try:
                    raw = blob.download_as_bytes()
                    df_part = pd.read_parquet(io.BytesIO(raw))
                    frames.append(df_part)
                except Exception as e:
                    logger.warning(
                        "CMEEnricher: skip unreadable blob %s: %s",
                        blob.name, e,
                    )

        if not frames:
            return pd.DataFrame()

        df = pd.concat(frames, ignore_index=True)
        # post_analysis expects a ts_event index (same schema as databento_client)
        if "ts_event" in df.columns:
            df["ts_event"] = pd.to_datetime(df["ts_event"], utc=True)
            df = df.set_index("ts_event").sort_index()
            # trim to the window
            df = df[(df.index >= start) & (df.index < end + timedelta(seconds=1))]
        return df

    def _get_client(self):
        """Lazy init of google.cloud.storage.Client (Cloud Run SA auto-auth)."""
        if self._gcs_client is None:
            from google.cloud import storage  # noqa: import-outside-toplevel
            self._gcs_client = storage.Client()
        return self._gcs_client


# =====================================================================
# Helpers
# =====================================================================
def _floor_to_minute(dt: datetime) -> datetime:
    """Drop seconds/microseconds and floor to the minute."""
    return dt.replace(second=0, microsecond=0)


def _floor_to_bucket(dt: datetime, *, bucket_seconds: int) -> datetime:
    """Floor to an arbitrary bucket boundary (same logic as the streamer)."""
    epoch = int(dt.timestamp())
    floored = epoch - (epoch % bucket_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _append_reason(signal: ChannelSignal, code: str) -> ChannelSignal:
    return _append_reasons(signal, [code])


def _append_reasons(signal: ChannelSignal, codes: list[str]) -> ChannelSignal:
    if not codes:
        return signal
    return signal.model_copy(
        update={"reason_codes": list(signal.reason_codes) + list(codes)}
    )


__all__ = [
    "CMEEnricher",
    "EnrichmentConfig",
]
