"""
polymarket/channel.py — PolymarketChannel: collector → normalizer → features → detector wiring.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §2 Component, §3 Process View, §6.6 Failure Mode):

  Implements the Channel base lifecycle. On the 5-second polling loop:

    for slug in market_slugs:
        market   = collector.fetch_market(slug)              # 1) Gamma API
        trades   = collector.fetch_trades(market.conditionId) # 2) Data API
        new_trs  = filter unseen trades by trade_key          # 3) dedupe
        for tr in new_trs:
            raw, normalized = normalize_trade(tr)             # 4) → schema
            raw_store.append(raw)                             # 5) audit
            features.add_events([normalized])                 # 6) buffer
        snap = features.compute_snapshot(slug, now)           # 7) z-score
        feature_store.append(snap)                            # 8) audit
        signal = detector.evaluate(snap)                      # 9) tier
        record signal per-symbol with ts                      # 10) sticky map

  The fusion engine polls get_current_signal() in a separate task every 5s.
  Returns one max-tier signal across all symbols (architecture §5.4.1 channel tier).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Per-cycle sequential calls for every watchlist slug. The v1 watchlist is in
    the 5~10 range, which is fast enough within httpx's 10 concurrent connection
    limit. When it grows, parallelize via asyncio.gather (P9). Sequential is
    easier to debug for now.

  · Trade dedupe: keep (timestamp + proxyWallet + size + price) tuples in a set.
    The same Polymarket trade can appear in multiple cycles (especially with
    limit=50 polling overlapping the previous cycle). The set holds only the
    last 5 minutes per symbol → memory is small.

  · Cycle exceptions are caught + logged + the next cycle proceeds. Only
    asyncio.CancelledError is re-raised (graceful shutdown signal).

  · raw_store / feature_store may be None (in tests / dry-run). Both use a
    simple None branch without hasattr checks.

────────────────────────────────────────────────────────────────────────
Architecture: §2.1 Component, §3 Process View, §5.4.1 Channel Tier, §6.6
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import ClassVar, Deque

from ...alerts.alert_ohlc_buffer import (
    AlertOhlcBuffer,
    polymarket_side_from_api,
)
from ...core.schemas import (
    CHANNEL_POLYMARKET,
    ChannelSignal,
    Tier,
)
from ...storage.feature_store import FeatureStore
from ...storage.polymarket_baseline_store import PolymarketBaselineStore
from ...storage.raw_store import RawStore
from ..base import Channel
from .collector import PolymarketCollector
from .detector import PolymarketDetector, PolymarketDetectorConfig
from .features import PolymarketFeatures
from .normalizer import normalize_trade

logger = logging.getLogger(__name__)


# Max number of trade keys retained in a symbol's dedupe set.
# 5s polling × 50 trades/cycle × 12 cycles/min × 5 min = 15000.
# In practice per-market trade frequency is much lower — 1500 is a safe ceiling.
_DEDUPE_SET_MAX = 1500


# Sticky window — freshness for tier > NORMAL signals returned by get_current_signal.
# v1: 60s. Comfortably longer than the 5s detector cycle so fusion does not miss it.
_DEFAULT_STICKY_WINDOW_S: float = 60.0

# Market metadata refresh interval (P9.1 M2 — refresh bestBid/bestAsk for mid-price).
# Calling market for every trade poll doubles API calls. 30s is enough (ratio of
# best-of-book changes vs trade frequency).
_MARKET_REFRESH_S: float = 30.0

# P12-C: logging / skip tuning.
#
# WARN_THRESHOLD : consecutive failures ≤ this count log as WARNING (one-liner, no stack trace).
#                  Polymarket Gamma API occasionally hangs 5~10s → 1~2 timeouts is normal.
# ERROR_EVERY    : after exceeding WARN_THRESHOLD, log ERROR + traceback once every N attempts.
#                  5s polling × 60 = once every 5 minutes → keep the debug signal without spam.
# MISSING_SKIP   : if a market returns None N times in a row (404-ish — presumed closed/resolved
#                  slug), drop that slug from rotation. Skip the polling cycle itself.
_POLL_WARN_THRESHOLD: int = 2
_POLL_ERROR_EVERY: int = 60
_MISSING_MARKET_SKIP_AFTER: int = 12  # 5s × 12 = 60s — treat as expired if not recovered in 1 min


class PolymarketChannel(Channel):
    """Polymarket detection pipeline (collector + features + detector wiring)."""

    name: ClassVar[str] = CHANNEL_POLYMARKET

    def __init__(
        self,
        *,
        market_slugs: list[str],
        raw_store: RawStore | None = None,
        feature_store: FeatureStore | None = None,
        baseline_store: PolymarketBaselineStore | None = None,
        detector_config: PolymarketDetectorConfig | None = None,
        poll_interval_s: float = 5.0,
        sticky_window_s: float = _DEFAULT_STICKY_WINDOW_S,
        http_timeout_s: float = 10.0,
        trades_per_fetch: int = 50,
        ohlc_buffer: AlertOhlcBuffer | None = None,
    ) -> None:
        """
        Args:
            market_slugs: polymarket slugs from the watchlist.
            raw_store: append-only store for RawEvent (audit). None → skip raw saving.
            feature_store: FeatureSnapshot store. None → skip features saving.
            baseline_store: P9.1 M1 — time-of-day SQLite store. None → tod_* features
                emit 0 (uses in-memory baseline only).
            detector_config: threshold override. None → v1 defaults.
            poll_interval_s: REST polling interval. Default 5s.
            sticky_window_s: threshold above which get_current_signal drops stale signals.
            http_timeout_s: per-HTTP-call timeout.
            trades_per_fetch: max trades fetched per polling call.
        """
        if not market_slugs:
            logger.warning("PolymarketChannel: empty market_slugs — channel will idle")

        self._slugs: list[str] = list(market_slugs)
        self._raw_store = raw_store
        self._feature_store = feature_store
        self._baseline_store = baseline_store
        # P12-D — data source for alert PNG price/volume panel. None → skip push.
        self._ohlc_buffer = ohlc_buffer

        self._collector = PolymarketCollector(http_timeout_s=http_timeout_s)
        self._features = PolymarketFeatures(baseline_store=baseline_store)
        self._detector = PolymarketDetector(detector_config)

        self._poll_interval_s = max(1.0, float(poll_interval_s))
        self._sticky_window_s = max(5.0, float(sticky_window_s))
        self._trades_per_fetch = max(1, int(trades_per_fetch))

        # State
        # symbol → (signal_ts, ChannelSignal). get_current_signal applies the sticky window.
        self._latest_signal: dict[str, tuple[datetime, ChannelSignal]] = {}
        # symbol → conditionId (cached on first fetch).
        self._condition_id_cache: dict[str, str] = {}
        # symbol → recent trade keys (dedupe).
        self._seen_trade_keys: dict[str, Deque[tuple]] = {}
        # symbol → (refreshed_at, market_meta) — bestBid/bestAsk/spread cache (P9.1 M2).
        # Periodically refreshed every _MARKET_REFRESH_S. Initially populated alongside conditionId fetch.
        self._market_meta_cache: dict[str, tuple[datetime, dict]] = {}
        # P12-C: consecutive failure counter (per slug). Reset to 0 on success.
        # Used by _poll_loop to throttle ERROR log frequency — prevents stack
        # traces from piling up every cycle when the same slug keeps timing out.
        self._consec_failures: dict[str, int] = {}
        # P12-C: skiplist for expired (closed/resolved) slugs — if fetch_market
        # returns None N times in a row, automatically enter skip rotation.
        # Prevents ERROR floods when watchlist.yaml cleanup is forgotten.
        self._missing_market_strikes: dict[str, int] = {}
        # health
        self._last_event_ts: datetime | None = None
        # lifecycle
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Lifecycle
    # ─────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Spawn the polling task. Returns immediately (loop runs in the background)."""
        if self._task is not None and not self._task.done():
            logger.warning("PolymarketChannel.start: already running, ignoring")
            return

        await self._collector.open()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._poll_loop(), name="polymarket-poll-loop"
        )
        logger.info(
            "PolymarketChannel: started (slugs=%d, interval=%.1fs)",
            len(self._slugs), self._poll_interval_s,
        )

    async def stop(self) -> None:
        """Stop polling + close collector. Idempotent."""
        if self._stop_event is None:
            return
        if self._stop_event.is_set():
            return

        self._stop_event.set()
        if self._task is not None:
            try:
                # Exits naturally when the next sleep wakes. Force-cancel if not finished in time.
                await asyncio.wait_for(
                    self._task, timeout=self._poll_interval_s + 2.0
                )
            except asyncio.TimeoutError:
                logger.warning("PolymarketChannel.stop: forcing cancel")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, BaseException):
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

        await self._collector.close()
        logger.info("PolymarketChannel: stopped")

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Signal output (fusion engine polls)
    # ─────────────────────────────────────────────────────────────────
    def get_current_signal(self) -> ChannelSignal | None:
        """Return the highest-tier signal among fresh signals (architecture §5.4.1).

        Returns:
            The highest-tier ChannelSignal (inside the sticky window).
            None if all are NORMAL or none exist (fusion treats as NORMAL).
        """
        if not self._latest_signal:
            return None

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self._sticky_window_s)

        # Only fresh & non-NORMAL are candidates
        candidates: list[ChannelSignal] = []
        for ts, sig in self._latest_signal.values():
            if ts < cutoff:
                continue
            if sig.tier == Tier.NORMAL:
                continue
            candidates.append(sig)

        if not candidates:
            return None
        # Highest tier — break ties on score
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
    # Polling loop — main background task
    # ─────────────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        """Process all slugs once per interval. Exits on stop_event."""
        assert self._stop_event is not None

        # Small jitter — prevents bursting in sync with other channels that start simultaneously
        await asyncio.sleep(0.5)

        while not self._stop_event.is_set():
            cycle_start = asyncio.get_event_loop().time()
            # P12-F: per-slug success/fail aggregated into channel fetch_health.
            # 1 slug ok → 'ok'; all-fail cycle → 'fail'; skip-only cycle leaves
            # the previous status untouched (so weekly_digest does not lock).
            cycle_ok = 0
            cycle_fail = 0
            last_error: BaseException | None = None

            for slug in self._slugs:
                if self._stop_event.is_set():
                    break

                # P12-C: skip slugs marked as expired automatically — avoids sending
                # the call itself each cycle, blocking ERROR/timeout floods.
                if self._missing_market_strikes.get(slug, 0) >= _MISSING_MARKET_SKIP_AFTER:
                    continue

                try:
                    await self._poll_one(slug)
                    cycle_ok += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    cycle_fail += 1
                    last_error = e
                    # A single slug's failure must not block other slugs.
                    # Log throttling — so a slug timing out every cycle (5s) does
                    # not cause stack-trace floods:
                    #   · first N attempts : WARNING (no traceback)
                    #   · then periodic    : ERROR + traceback (for debugging)
                    #   · otherwise        : DEBUG (silently)
                    self._consec_failures[slug] = self._consec_failures.get(slug, 0) + 1
                    n = self._consec_failures[slug]
                    if n <= _POLL_WARN_THRESHOLD:
                        logger.warning(
                            "PolymarketChannel: poll failed for slug=%s (n=%d, %s)",
                            slug, n, type(e).__name__,
                        )
                    elif n % _POLL_ERROR_EVERY == 0:
                        logger.exception(
                            "PolymarketChannel: poll failing repeatedly slug=%s (n=%d)",
                            slug, n,
                        )
                    else:
                        logger.debug(
                            "PolymarketChannel: poll failed (suppressed) slug=%s n=%d",
                            slug, n,
                        )

            # P12-F: 1+ slug ok → fetch_health='ok'; all-fail → 'fail'.
            # Skip-only cycles leave fetch_health unchanged.
            if cycle_ok > 0:
                self._record_fetch_ok()
            elif cycle_fail > 0 and last_error is not None:
                self._record_fetch_fail(last_error)

            # Wait until the next cycle — wakes immediately on stop signal
            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_s = max(0.1, self._poll_interval_s - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                continue
            else:
                break  # stop_event set

    async def _poll_one(self, slug: str) -> None:
        """One cycle for a slug: market metadata → trades → features → detector → signal."""
        now = datetime.now(timezone.utc)

        # 1) Fetch market metadata.
        #    - Force-fetch when conditionId is uncached (first call).
        #    - Otherwise refresh every _MARKET_REFRESH_S → bestBid/bestAsk stay fresh (P9.1 M2).
        condition_id = self._condition_id_cache.get(slug)
        market_cache_entry = self._market_meta_cache.get(slug)

        needs_market_fetch = (
            condition_id is None
            or market_cache_entry is None
            or (now - market_cache_entry[0]).total_seconds() >= _MARKET_REFRESH_S
        )

        if needs_market_fetch:
            market = await self._collector.fetch_market(slug)
            if market is None:
                # First fetch failure → return if no conditionId (cannot fetch trades).
                # If we have a cached one (refresh failure), reuse the existing cache.
                if condition_id is None:
                    # P12-C: presumed closed/resolved slug → increment strike counter.
                    # After _MISSING_MARKET_SKIP_AFTER consecutive strikes, _poll_loop auto-skips.
                    strikes = self._missing_market_strikes.get(slug, 0) + 1
                    self._missing_market_strikes[slug] = strikes
                    if strikes == _MISSING_MARKET_SKIP_AFTER:
                        logger.warning(
                            "PolymarketChannel: slug=%s consistently missing (%d strikes) "
                            "→ removing from rotation. Recommend cleaning up watchlist.yaml.",
                            slug, strikes,
                        )
                    else:
                        logger.debug(
                            "PolymarketChannel: slug=%s not found (strike %d/%d)",
                            slug, strikes, _MISSING_MARKET_SKIP_AFTER,
                        )
                    return
            else:
                # Extract conditionId (only meaningful on first run — stable after)
                if condition_id is None:
                    try:
                        condition_id = str(market["conditionId"])
                    except (KeyError, TypeError):
                        logger.warning(
                            "PolymarketChannel: market payload missing conditionId for %s",
                            slug,
                        )
                        return
                    self._condition_id_cache[slug] = condition_id

                # Cache only bestBid/bestAsk/spread — keep memory small for every trade normalize call.
                meta_keys = ("bestBid", "bestAsk", "spread")
                slim_meta = {k: market.get(k) for k in meta_keys if k in market}
                self._market_meta_cache[slug] = (now, slim_meta)
                # Successful fetch — reset strike / failure counters.
                if self._missing_market_strikes.get(slug):
                    self._missing_market_strikes[slug] = 0
                if self._consec_failures.get(slug):
                    self._consec_failures[slug] = 0

        # market_meta variable — most recent cache (None → normalize_trade ignores)
        market_meta_for_normalize: dict | None = None
        if slug in self._market_meta_cache:
            market_meta_for_normalize = self._market_meta_cache[slug][1]

        # 2) Fetch recent trades
        trades_raw = await self._collector.fetch_trades(
            condition_id, limit=self._trades_per_fetch,
        )
        if not trades_raw:
            # No trades — could still run the detector with no features update, but
            # an empty buffer returns None anyway → skip.
            return

        # 3) Dedupe + normalize + raw_store append
        new_normalized = []
        seen = self._seen_trade_keys.setdefault(slug, deque(maxlen=_DEDUPE_SET_MAX))
        seen_set = set(seen)

        for tr in trades_raw:
            try:
                key = self._trade_key(tr)
            except Exception as e:
                logger.warning("PolymarketChannel: bad trade payload: %s", e)
                continue
            if key in seen_set:
                continue

            try:
                raw, normalized = normalize_trade(
                    tr, market_meta=market_meta_for_normalize,
                )
            except ValueError as e:
                logger.warning("PolymarketChannel: normalize failed (%s): %s", slug, e)
                continue

            seen.append(key)
            seen_set.add(key)

            if self._raw_store is not None:
                try:
                    self._raw_store.append(raw)
                except Exception as e:
                    logger.error("PolymarketChannel: raw_store.append failed: %s", e)

            # P12-D — push to alert OHLC buffer (1-min bar aggregation).
            # PM trade's 'size' is USD notional (converted in collector.normalize).
            # side: API's BUY/SELL string → buy/sell aggressor.
            if self._ohlc_buffer is not None:
                try:
                    self._ohlc_buffer.push_trade(
                        channel=CHANNEL_POLYMARKET,
                        symbol=slug,
                        ts=normalized.ts_source,
                        price=float(normalized.price or 0.0),
                        size=float(normalized.size_usd or 0.0),
                        side=polymarket_side_from_api(
                            str(tr.get("side", "")) if isinstance(tr, dict) else "",
                        ),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "PolymarketChannel: ohlc_buffer.push_trade failed: %s", e,
                    )

            new_normalized.append(normalized)
            self._last_event_ts = max(
                self._last_event_ts or normalized.ts_source,
                normalized.ts_source,
            )

        # 4) Update feature buffer — only when new data exists (duplicate adds are meaningless)
        if new_normalized:
            self._features.add_events(new_normalized)

        # 5) Feature snapshot — keyed by slug (reuse cycle-start `now`)
        snapshot = self._features.compute_snapshot(slug, now)
        if snapshot is None:
            return

        if self._feature_store is not None:
            try:
                self._feature_store.append(snapshot)
            except Exception as e:
                logger.error("PolymarketChannel: feature_store.append failed: %s", e)

        # 6) Detector → channel signal
        signal = self._detector.evaluate(snapshot)
        # Store every signal (even NORMAL goes into the sticky map; get_current_signal filters)
        self._latest_signal[slug] = (now, signal)

        if signal.tier != Tier.NORMAL:
            logger.info(
                "polymarket signal: slug=%s tier=%s score=%.3f reasons=%s",
                slug, signal.tier.value, signal.score, signal.reason_codes,
            )

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _trade_key(tr: dict) -> tuple:
        """Polymarket trade dedupe key.

        Polymarket REST returns the same trade again, identical down to
        transactionHash. The 5-tuple ts + wallet + size + price + side is
        effectively unique.
        """
        return (
            int(tr["timestamp"]),
            str(tr["proxyWallet"]),
            float(tr["size"]),
            float(tr["price"]),
            str(tr.get("side", "")),
        )
