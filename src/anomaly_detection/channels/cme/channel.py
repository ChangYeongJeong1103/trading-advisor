"""
cme/channel.py — CMEChannel: mock_collector → normalizer → features → detector wiring.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §2 Component, §3 Process View, §6.6 Failure Mode):

  Implements the Channel base lifecycle. On the 5-second polling loop:

    every cycle:
      for symbol in watchlist (e.g. CL, GC, ES):
          trades = mock_collector.generate_trades(symbol, now_loop_time)  # 1)
          for tr in trades:
              raw, normalized = normalize_mock_trade(tr)                  # 2)
              raw_store.append(raw)                                       # 3)
              features.add_events([normalized])                           # 4)
          snap = features.compute_snapshot(symbol, now)                   # 5)
          feature_store.append(snap)                                      # 6)
          signal = detector.evaluate(snap)                                # 7)
          record signal per-symbol with ts                                # 8)

  Every 5s, the fusion engine polls get_current_signal() — returns one
  max-tier signal across all symbols (architecture §5.4.1 channel tier).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Mock-only — no external HTTP calls; the cycle itself completes nearly instantly.

  · When the CME_MOCK_SPIKE env var is on, mock_collector periodically injects
    spikes → detector emits an EMERGENCY tier → fusion → router → email/telegram
    dry-run, exercising the full alert pipeline end-to-end.

  · raw_store / feature_store may be None (for tests). Exceptions are caught and
    logged inside the cycle.

  · When the real Databento collector lands in P9, only this channel's _poll_one()
    needs to swap to "real_collector.fetch_*()". Everything else (normalizer,
    features, detector, sticky map) stays — the interface is preserved.

────────────────────────────────────────────────────────────────────────
Architecture: §2.1 Component, §3 Process View, §5.4.1 Channel Tier, §6.6
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from ...core.schemas import (
    CHANNEL_CME,
    ChannelSignal,
    NormalizedEvent,
    RawEvent,
    Tier,
)
from ...alerts.alert_ohlc_buffer import AlertOhlcBuffer
from ...storage.feature_store import FeatureStore
from ...storage.raw_store import RawStore
from ..base import Channel
from .cme_insider_scanner import CMEInsiderScanner, InsiderScannerConfig
from .detector import CMEDetector, CMEDetectorConfig
from .features import CMEFeatures
from .mock_collector import MockCMECollector
from .normalizer import normalize_mock_trade

logger = logging.getLogger(__name__)


# Sticky window — freshness for tier > NORMAL signals returned by get_current_signal.
_DEFAULT_STICKY_WINDOW_S: float = 60.0


class CMEChannel(Channel):
    """CME mock detection pipeline (P4 walking-skeleton).

    Will be replaced in P9 with a real Databento + TradingView + UnusualWhales composite collector.
    """

    name: ClassVar[str] = CHANNEL_CME

    def __init__(
        self,
        *,
        symbols: list[str],
        raw_store: RawStore | None = None,
        feature_store: FeatureStore | None = None,
        detector_config: CMEDetectorConfig | None = None,
        poll_interval_s: float = 5.0,
        sticky_window_s: float = _DEFAULT_STICKY_WINDOW_S,
        mock_seed: int | None = None,
        # ── P12-B: insider_v1 24/7 scanner ─────────────────────────
        # Polls GCS Live Parquet to detect insider bursts directly.
        # Pushes into the same _latest_signal dict as the TV webhook
        # (ingest_external_event) via max-tier merge. None → disabled
        # (mock test / legacy behavior).
        insider_scanner: CMEInsiderScanner | None = None,
        insider_scanner_config: InsiderScannerConfig | None = None,
        enable_insider_scanner: bool = False,
        # ── P12-D: data for alert PNG price/volume panel ──────────
        ohlc_buffer: AlertOhlcBuffer | None = None,
    ) -> None:
        """
        Args:
            symbols: CME symbols from the watchlist (e.g. ["CL", "GC", "ES"]).
            raw_store: store for RawEvent (audit). None → skip.
            feature_store: store for FeatureSnapshot. None → skip.
            detector_config: threshold override.
            poll_interval_s: cycle interval (seconds).
            sticky_window_s: stale threshold for get_current_signal.
            mock_seed: reproducible mock seed. None → env CME_MOCK_SEED.
            insider_scanner: externally-injected scanner instance. Used when
                tests want to inject a fake trade_reader. None + enable_insider_scanner
                =True → auto-create a default CMEInsiderScanner (production).
            insider_scanner_config: config override when default-creating the scanner.
            enable_insider_scanner: True → scanner enabled (turned on by production
                daemon). False (default) → disabled — preserves legacy mock-only behavior.
        """
        if not symbols:
            logger.warning("CMEChannel: empty symbols list — channel will idle")

        self._symbols = list(symbols)
        self._raw_store = raw_store
        self._feature_store = feature_store

        self._collector = MockCMECollector(symbols=self._symbols, seed=mock_seed)
        self._features = CMEFeatures()
        self._detector = CMEDetector(detector_config)

        self._poll_interval_s = max(1.0, float(poll_interval_s))
        self._sticky_window_s = max(5.0, float(sticky_window_s))

        # State
        self._latest_signal: dict[str, tuple[datetime, ChannelSignal]] = {}
        self._last_event_ts: datetime | None = None
        # lifecycle
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

        # P12-D — data for alert PNG price/volume panel.
        self._ohlc_buffer = ohlc_buffer

        # ── P12-B: wire the insider scanner ─────────────────────────
        # Use the externally-injected one if provided; otherwise auto-create when enable=True.
        self._insider_scanner: CMEInsiderScanner | None = None
        if insider_scanner is not None:
            self._insider_scanner = insider_scanner
            # P12-D — also wire the buffer onto an externally-injected scanner if present.
            if self._ohlc_buffer is not None:
                self._insider_scanner.set_ohlc_buffer(self._ohlc_buffer)
        elif enable_insider_scanner:
            self._insider_scanner = CMEInsiderScanner(
                symbols=self._symbols,
                signal_callback=self._inject_insider_signal,
                config=insider_scanner_config,
                ohlc_buffer=self._ohlc_buffer,
            )

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Lifecycle
    # ─────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            logger.warning("CMEChannel.start: already running, ignoring")
            return

        await self._collector.open()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop(), name="cme-poll-loop")

        # P12-B: also start the insider scanner. It manages its own task.
        # Warmup backfill calls GCS — channel still starts if it fails.
        if self._insider_scanner is not None:
            try:
                await self._insider_scanner.start()
            except Exception as e:
                logger.exception(
                    "CMEChannel.start: insider_scanner.start() failed "
                    "(channel running without insider): %s", e,
                )

        logger.info(
            "CMEChannel: started (symbols=%d, interval=%.1fs, mock_spike=%s, insider=%s)",
            len(self._symbols),
            self._poll_interval_s,
            self._collector.spike_enabled,
            "on" if self._insider_scanner is not None else "off",
        )

    async def stop(self) -> None:
        if self._stop_event is None:
            return
        if self._stop_event.is_set():
            return

        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self._poll_interval_s + 2.0)
            except asyncio.TimeoutError:
                logger.warning("CMEChannel.stop: forcing cancel")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, BaseException):
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

        # P12-B: scanner stop — the scanner handles its own timeout / cancel.
        if self._insider_scanner is not None:
            try:
                await self._insider_scanner.stop()
            except Exception as e:
                logger.error("CMEChannel.stop: insider_scanner.stop() failed: %s", e)

        await self._collector.close()
        logger.info("CMEChannel: stopped")

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Signal output
    # ─────────────────────────────────────────────────────────────────
    def get_current_signal(self) -> ChannelSignal | None:
        if not self._latest_signal:
            return None

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self._sticky_window_s)

        candidates: list[ChannelSignal] = []
        for ts, sig in self._latest_signal.values():
            if ts < cutoff:
                continue
            if sig.tier == Tier.NORMAL:
                continue
            candidates.append(sig)

        if not candidates:
            return None
        return max(candidates, key=lambda s: (s.tier.rank(), s.score))

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Health properties
    # ─────────────────────────────────────────────────────────────────
    @property
    def last_event_ts(self) -> datetime | None:
        return self._last_event_ts

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ─────────────────────────────────────────────────────────────────
    # External ingestion (P9.3.P0.C — TradingView webhook → option A)
    # ─────────────────────────────────────────────────────────────────
    def ingest_external_event(
        self,
        raw: RawEvent,
        normalized: NormalizedEvent,
    ) -> None:
        """Receive one event pushed by an external source (TradingView webhook etc.)
        and **immediately** emit a RISK_OFF tier ChannelSignal (P9.3.P2.A — direct emit).

        Design decisions (user-agreed proposal — P9.3.P2.A):
          · TradingView's RVol > 4 itself is already a signal that passed the
            primary anomaly filter.
            → Do NOT re-evaluate via daemon's mock-baseline detector.
            → Emit a RISK_OFF channel signal immediately.
          · Afterwards:
              fusion → router (RISK_OFF dispatch)
                → CMEEnricher (fetch real Databento trade data + post_analysis)
                → enrich reason_codes with metrics
                → telegram/email alert (user judges informed-or-not from metrics)
          · Still add to the features buffer — used by post_analysis for cache/debug
            or for coexistence with detectors in the future.

        Args:
            raw:        RawEvent (for audit / replay — appended to raw_store).
            normalized: NormalizedEvent — extracts metrics like size_usd, meta.

        Raises:
            ValueError: normalized.symbol is not in our watchlist (self._symbols).
        """
        if normalized.symbol not in self._symbols:
            raise ValueError(
                f"CMEChannel.ingest_external_event: symbol {normalized.symbol!r} "
                f"not in watchlist {self._symbols}"
            )

        if self._raw_store is not None:
            try:
                self._raw_store.append(raw)
            except Exception as e:
                logger.error("CMEChannel.ingest_external: raw_store.append failed: %s", e)

        # Push into features buffer — in production the enricher fetches Databento
        # separately, but kept for audit and future-proofing.
        self._features.add_events([normalized])

        # Refresh last_event_ts — health probe uses this to determine "is the channel alive".
        self._last_event_ts = max(
            self._last_event_ts or normalized.ts_source,
            normalized.ts_source,
        )

        # ── Immediately emit a RISK_OFF channel signal (★ core P9.3.P2.A change) ──
        # tier=RISK_OFF: a TV primary trigger always proceeds to enricher (Databento secondary).
        # score=0.75: RISK_OFF threshold (a safe level when combined with other channels in fusion).
        # reason_codes: human-readable summary of the TV trigger's key metrics.
        # fired_detectors: ["tradingview_webhook"] — audit which path fired.
        trigger_name = normalized.meta.get("trigger", "?")
        close_price = normalized.price  # standard price field on NormalizedEvent
        size_usd_m = normalized.size_usd / 1_000_000.0
        close_str = f"{close_price:.2f}" if close_price is not None else "?"
        signal = ChannelSignal(
            channel=CHANNEL_CME,
            symbol=normalized.symbol,
            ts=normalized.ts_source,
            score=0.75,
            tier=Tier.RISK_OFF,
            confidence=0.7,  # incomplete from the primary trigger alone — enricher augments.
            fired_detectors=["tradingview_webhook"],
            reason_codes=[
                f"TV_TRIGGER:{trigger_name} close={close_str} size_usd={size_usd_m:.1f}M",
            ],
        )

        # Guard before saving to _latest_signal:
        # A TV webhook (RISK_OFF) must not overwrite a stronger existing signal
        # (e.g. scanner EMERGENCY) inside the sticky window. Block overwrite if the
        # existing tier is higher.
        now_wall = datetime.now(timezone.utc)
        existing = self._latest_signal.get(normalized.symbol)
        if existing is not None:
            existing_ts, existing_sig = existing
            cutoff = now_wall - timedelta(seconds=self._sticky_window_s)
            if existing_ts >= cutoff and existing_sig.tier.rank() > signal.tier.rank():
                logger.info(
                    "CMEChannel.ingest_external: preserve stronger existing signal "
                    "symbol=%s existing_tier=%s incoming_tier=%s",
                    normalized.symbol,
                    existing_sig.tier.value,
                    signal.tier.value,
                )
                return

        # Use datetime.now(utc): may differ from ts_source (close minute), but the
        # sticky window is wall-clock based so `now` is correct.
        self._latest_signal[normalized.symbol] = (now_wall, signal)

        logger.info(
            "CMEChannel.ingest_external: symbol=%s size_usd=%.0f trigger=%s ts=%s "
            "→ emit RISK_OFF (score=0.75)",
            normalized.symbol,
            normalized.size_usd,
            trigger_name,
            normalized.ts_source.isoformat(),
        )

    # ─────────────────────────────────────────────────────────────────
    # Insider scanner injection (P12-B — 24/7 GCS-polling detector)
    # ─────────────────────────────────────────────────────────────────
    def _inject_insider_signal(self, signal: ChannelSignal) -> None:
        """Push a ChannelSignal emitted by CMEInsiderScanner into channel state.

        Design decisions:
          · Use the same `_latest_signal` dict as the TV webhook
            (`ingest_external_event`) → orchestrator's `get_current_signal()`
            naturally merges by max-tier (logic already uses sticky window +
            max(tier, score)).
          · However, **a signal emitted by the insider scanner in the same cycle
            must not overwrite a stronger existing signal in the slot** — to avoid
            weakening a stronger alarm inside the sticky window.
          · On equal tier, the more recent ts wins (raw values are fresher).

        Args:
            signal: ChannelSignal produced by the scanner's detector
                (tier > NORMAL — already filtered by scanner).

        Thread-safety:
            Assumes asyncio single-thread (Cloud Run daemon). The scanner task and
            the channel's other tasks run on the same event loop, so dict
            modifications are safe.
        """
        sym = signal.symbol
        now = datetime.now(timezone.utc)
        existing = self._latest_signal.get(sym)
        if existing is not None:
            existing_ts, existing_sig = existing
            cutoff = now - timedelta(seconds=self._sticky_window_s)
            # Existing signal within sticky window + higher tier → keep.
            if existing_ts >= cutoff and existing_sig.tier.rank() > signal.tier.rank():
                logger.debug(
                    "CMEChannel.inject_insider: %s tier=%s suppressed by "
                    "stronger existing tier=%s",
                    sym, signal.tier.value, existing_sig.tier.value,
                )
                return

        self._latest_signal[sym] = (now, signal)
        # For health probe — signal that the channel is alive.
        self._last_event_ts = max(self._last_event_ts or signal.ts, signal.ts)

        logger.info(
            "CMEChannel.inject_insider: symbol=%s tier=%s score=%.2f → push",
            sym, signal.tier.value, signal.score,
        )

    # ─────────────────────────────────────────────────────────────────
    # Polling loop
    # ─────────────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        """Process every symbol once per interval."""
        assert self._stop_event is not None

        # Small jitter — avoids bursting in sync with other channels
        await asyncio.sleep(0.9)

        while not self._stop_event.is_set():
            cycle_start = asyncio.get_event_loop().time()

            for symbol in self._symbols:
                if self._stop_event.is_set():
                    break
                try:
                    self._poll_one(symbol, now_loop_time=cycle_start)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception(
                        "CMEChannel: poll failed for symbol=%s: %s", symbol, e,
                    )

            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_s = max(0.1, self._poll_interval_s - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                continue
            else:
                break  # stop_event set

    def _poll_one(self, symbol: str, *, now_loop_time: float) -> None:
        """One symbol per cycle — only sticky cleanup (since P9.3.P2.A).

        Changes (P9.3.P2.A):
          · Removed mock collector call — in production the TV webhook is the only real input.
          · Removed detector.evaluate call — when a webhook arrives, ingest_external_event
            emits RISK_OFF immediately. Mock baseline stats are meaningless.
          · This function's sole responsibility is now stale-signal cleanup:
              remove _latest_signal entries past sticky_window to prevent memory
              leaks / fusion from using stale data.

        Sync function (no IO). Errors caught in poll_loop.
        """
        # _: now_loop_time is passed by the caller for polling cadence. Unused here.
        del now_loop_time

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self._sticky_window_s)

        # Drop signals past the sticky window. Build a list() to avoid mutating during iteration.
        stale_symbols = [
            sym for sym, (ts, _sig) in self._latest_signal.items()
            if sym == symbol and ts < cutoff
        ]
        for sym in stale_symbols:
            del self._latest_signal[sym]
            logger.debug("CMEChannel: dropped stale signal for %s", sym)
