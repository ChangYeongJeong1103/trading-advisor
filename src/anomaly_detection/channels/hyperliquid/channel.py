"""
hyperliquid/channel.py — HyperliquidChannel: collector → normalizer → features → detector wiring.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §2 Component, §3 Process View, §6.6 Failure Mode):

  Implements the Channel base lifecycle. On the 5-second polling loop:

    every cycle:
      meta, all_ctxs = collector.fetch_meta_and_asset_ctxs()  # 1) POST /info
      coin_to_ctx    = zip universe ↔ all_ctxs                # 2) align
      for coin in watchlist:
          ctx = coin_to_ctx.get(coin)                         # 3) filter watchlist
          if ctx is None: continue                            #    (skip missing coin)
          raw, normalized = normalize_asset_ctx(coin, ctx)    # 4) → schema
          raw_store.append(raw)                               # 5) audit
          features.add_events([normalized])                   # 6) buffer
          snap = features.compute_snapshot(coin, now)         # 7) z-score (delta-based)
          feature_store.append(snap)                          # 8) audit
          signal = detector.evaluate(snap)                    # 9) tier
          record signal per-coin with ts                      # 10) sticky map

  The fusion engine polls get_current_signal() in a separate task every 5s.
  Returns one max-tier signal across all coins (architecture §5.4.1 channel tier).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · One metaAndAssetCtxs call per cycle pulls all coin data at once.
    (No need to split calls per slug like Polymarket — 1 call = every perp.)
    → Minimal rate-limit pressure.

  · No dedupe needed (snapshot-shaped — even repeat cumulative values are handled
    by features. Still, to save buffer memory, skip when "previous snapshot's
    cumulative value + mark_px are identical").

  · Cycle exceptions caught + logged + the next cycle proceeds. Only
    asyncio.CancelledError is re-raised (graceful shutdown signal).

  · raw_store / feature_store may be None (in tests / dry-run).

────────────────────────────────────────────────────────────────────────
Architecture: §2.1 Component, §3 Process View, §5.4.1 Channel Tier, §6.6
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from ...core.schemas import (
    CHANNEL_HYPERLIQUID,
    ChannelSignal,
    Tier,
)
from ...alerts.alert_ohlc_buffer import (
    AlertOhlcBuffer,
    hyperliquid_side_from_taker,
)
from ...storage.feature_store import FeatureStore
from ...storage.hl_wallet_store import HLWalletStore
from ...storage.raw_store import RawStore
from ..base import Channel
from .collector import HyperliquidCollector
from .detector import HyperliquidDetector, HyperliquidDetectorConfig
from .features import HyperliquidFeatures
from .normalizer import normalize_asset_ctx

logger = logging.getLogger(__name__)


# Sticky window — freshness for tier > NORMAL signals returned by get_current_signal.
# v1: 60s. Comfortably longer than the 5s detector cycle so fusion does not miss it.
_DEFAULT_STICKY_WINDOW_S: float = 60.0


class HyperliquidChannel(Channel):
    """Hyperliquid detection pipeline (collector + features + detector wiring)."""

    name: ClassVar[str] = CHANNEL_HYPERLIQUID

    def __init__(
        self,
        *,
        coins: list[str],
        raw_store: RawStore | None = None,
        feature_store: FeatureStore | None = None,
        wallet_store: HLWalletStore | None = None,
        detector_config: HyperliquidDetectorConfig | None = None,
        poll_interval_s: float = 5.0,
        sticky_window_s: float = _DEFAULT_STICKY_WINDOW_S,
        http_timeout_s: float = 10.0,
        new_whale_window_min: int = 5,
        new_whale_fresh_within_h: int = 24,
        new_whale_warmup_after_h: float = 24.0,
        wallet_trade_prune_interval_s: float = 300.0,
        cluster_window_min: int = 10,
        cluster_price_band_pct: float = 0.005,
        ohlc_buffer: AlertOhlcBuffer | None = None,
    ) -> None:
        """
        Args:
            coins: hyperliquid perp coins in the watchlist (e.g. ["BTC", "ETH"]).
            raw_store: append-only store for RawEvent (audit). None → skip raw saving.
            feature_store: FeatureSnapshot store. None → skip features saving.
            wallet_store: P9.2.P2 — SQLite store tracking wallet aggregates from recentTrades.
                          None → new_whale_v1 detector disabled (all features 0).
            detector_config: threshold override. None → v3 defaults.
            poll_interval_s: REST polling interval. Default 5s.
            sticky_window_s: threshold above which get_current_signal drops stale signals.
            http_timeout_s: per-HTTP-call timeout.
            new_whale_window_min: window (minutes) for the cumulative notional in new_whale_v1.
            new_whale_fresh_within_h: "fresh" threshold — first_seen within N hours.
            new_whale_warmup_after_h: store must be alive N hours before new_whale_v1 activates.
                                      Cold-start protection: shortly after boot, every wallet
                                      looks "new", so block the detector from firing.
            wallet_trade_prune_interval_s: how often to call hl_wallet_trade rolling prune (s).
            cluster_window_min: window (minutes) for cluster grouping in P9.2.P3 cluster_v1.
                                Fresh wallets that traded within the last N minutes are cluster candidates.
            cluster_price_band_pct: same-price-band width for cluster_v1 (ratio of anchor_px).
                                    0.005 = ±0.5% (1% total width).
        """
        if not coins:
            logger.warning("HyperliquidChannel: empty coins list — channel will idle")

        self._coins: list[str] = list(coins)
        self._raw_store = raw_store
        self._feature_store = feature_store
        self._wallet_store = wallet_store

        self._collector = HyperliquidCollector(http_timeout_s=http_timeout_s)
        self._features = HyperliquidFeatures()
        self._detector = HyperliquidDetector(detector_config)

        self._poll_interval_s = max(1.0, float(poll_interval_s))
        self._sticky_window_s = max(5.0, float(sticky_window_s))
        # P12-D — data source for alert PNG price/volume panel. None → skip push.
        self._ohlc_buffer = ohlc_buffer

        # P9.2.P2 settings
        self._new_whale_window_min = max(1, int(new_whale_window_min))
        self._new_whale_fresh_within_h = max(1, int(new_whale_fresh_within_h))
        self._new_whale_warmup_after_h = max(0.0, float(new_whale_warmup_after_h))
        self._wallet_trade_prune_interval_s = max(60.0, float(wallet_trade_prune_interval_s))
        self._last_wallet_prune_ts: datetime | None = None

        # P9.2.P3 settings (cluster_v1)
        self._cluster_window_min = max(1, int(cluster_window_min))
        self._cluster_price_band_pct = max(0.0001, float(cluster_price_band_pct))

        # State
        # coin → (signal_ts, ChannelSignal). get_current_signal applies the sticky window.
        self._latest_signal: dict[str, tuple[datetime, ChannelSignal]] = {}
        # coin → previous snapshot's (day_ntl_vlm, mark_px). Dedupe to save buffer memory.
        self._last_snapshot_kv: dict[str, tuple[float, float]] = {}
        # coin → largest tid seen on the previous cycle. Aids polling dedup (real dedup is the SQLite PK).
        self._last_seen_tid: dict[str, int] = {}
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
            logger.warning("HyperliquidChannel.start: already running, ignoring")
            return

        await self._collector.open()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._poll_loop(), name="hyperliquid-poll-loop"
        )
        logger.info(
            "HyperliquidChannel: started (coins=%d, interval=%.1fs)",
            len(self._coins), self._poll_interval_s,
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
                logger.warning("HyperliquidChannel.stop: forcing cancel")
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
        logger.info("HyperliquidChannel: stopped")

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Signal output (fusion engine polls)
    # ─────────────────────────────────────────────────────────────────
    def get_current_signal(self) -> ChannelSignal | None:
        """Return the highest-tier signal among fresh signals (architecture §5.4.1)."""
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
    # Polling loop — main background task
    # ─────────────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        """Each interval: one metaAndAssetCtxs call + process watchlist coins."""
        assert self._stop_event is not None

        # Small jitter — avoids bursting in sync with other channels
        await asyncio.sleep(0.7)

        while not self._stop_event.is_set():
            cycle_start = asyncio.get_event_loop().time()
            try:
                await self._poll_once()
                # P12-F: if the whole cycle finished without raising, the core
                # fetch succeeded. Best-effort sub-steps (recentTrades etc.)
                # are wrapped in their own try/except inside _poll_once.
                self._record_fetch_ok()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A single cycle failure must not block the next cycle
                logger.exception("HyperliquidChannel: poll_once failed: %s", e)
                self._record_fetch_fail(e)

            # Wait until the next cycle — wakes immediately on stop signal
            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_s = max(0.1, self._poll_interval_s - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                continue
            else:
                break  # stop_event set

    async def _poll_once(self) -> None:
        """One cycle: call metaAndAssetCtxs → process watchlist coins."""
        # 1) Single call returns all perp data
        meta, asset_ctxs = await self._collector.fetch_meta_and_asset_ctxs()

        # 2) universe ↔ assetCtxs index alignment
        universe = meta.get("universe", [])
        if not isinstance(universe, list):
            logger.warning("HyperliquidChannel: universe not a list, skip cycle")
            return
        if len(universe) != len(asset_ctxs):
            logger.warning(
                "HyperliquidChannel: universe (%d) ↔ assetCtxs (%d) length mismatch",
                len(universe), len(asset_ctxs),
            )
            # Proceed on the shorter side (best effort)
        n = min(len(universe), len(asset_ctxs))

        # coin name → (ctx dict, universe item)
        coin_map: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for i in range(n):
            uni_item = universe[i] if isinstance(universe[i], dict) else {}
            ctx_item = asset_ctxs[i] if isinstance(asset_ctxs[i], dict) else None
            if ctx_item is None:
                continue
            name = uni_item.get("name")
            if not isinstance(name, str):
                continue
            coin_map[name] = (ctx_item, uni_item)

        # 3) Process watchlist coins
        now = datetime.now(timezone.utc)
        for coin in self._coins:
            if self._stop_event is not None and self._stop_event.is_set():
                break

            entry = coin_map.get(coin)
            if entry is None:
                logger.debug(
                    "HyperliquidChannel: coin=%s not found in universe (skip)", coin,
                )
                continue
            ctx, uni_item = entry

            # 3a) Fetch recentTrades + update wallet_store (P9.2.P2).
            #     Still run process_coin on failure (detector partially works from assetCtxs alone).
            # Call recentTrades when either wallet_store (insider detector) or
            # ohlc_buffer (alert plot) is set.
            if self._wallet_store is not None or self._ohlc_buffer is not None:
                try:
                    await self._poll_recent_trades(coin)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(
                        "HyperliquidChannel: recentTrades failed coin=%s: %s", coin, e,
                    )

            try:
                await self._process_coin(coin, ctx, uni_item, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(
                    "HyperliquidChannel: process_coin failed coin=%s: %s", coin, e,
                )

        # 4) Periodic wallet_trade rolling prune (about once every 5 min)
        if self._wallet_store is not None:
            self._maybe_prune_wallet_trades(now)

    async def _poll_recent_trades(self, coin: str) -> None:
        """One recentTrades call → push into wallet_store + ohlc_buffer.

        Invoked when either wallet_store or ohlc_buffer is set (caller decides).
        """
        trades = await self._collector.fetch_recent_trades(coin)
        if not trades:
            return

        # ── P9.2.P2 — wallet_store (taker-only) ──
        if self._wallet_store is not None:
            inserted = self._wallet_store.record_trades(trades, taker_only=True)
            if inserted > 0:
                try:
                    max_tid = max(
                        int(t.get("tid", 0))
                        for t in trades
                        if t.get("tid") is not None
                    )
                    self._last_seen_tid[coin] = max_tid
                except Exception:
                    pass
                logger.debug(
                    "HyperliquidChannel: recentTrades coin=%s fetched=%d "
                    "inserted=%d",
                    coin, len(trades), inserted,
                )

        # ── P12-D — push to alert OHLC buffer (1-min bar aggregation). ──
        # HL recentTrades fields: side(B/A) / px / sz / time(ms) / coin.
        # sz is in base-coin units (BTC count if BTC). price is USD.
        if self._ohlc_buffer is not None:
            for t in trades:
                try:
                    ts_ms = int(t.get("time", 0))
                    if ts_ms <= 0:
                        continue
                    ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
                    price = float(t.get("px", 0))
                    size = float(t.get("sz", 0))
                    side = hyperliquid_side_from_taker(str(t.get("side", "")))
                    self._ohlc_buffer.push_trade(
                        channel=CHANNEL_HYPERLIQUID,
                        symbol=coin,
                        ts=ts, price=price, size=size, side=side,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "HyperliquidChannel: ohlc_buffer.push_trade failed: %s", e,
                    )

    def _maybe_prune_wallet_trades(self, now: datetime) -> None:
        """Rolling-window prune for wallet_trade. Called at most once every 5 min."""
        if self._wallet_store is None:
            return
        if (
            self._last_wallet_prune_ts is not None
            and (now - self._last_wallet_prune_ts).total_seconds()
            < self._wallet_trade_prune_interval_s
        ):
            return
        try:
            # Drop trades older than 1 hour (matches store's trade_retention_hours)
            cutoff = now - timedelta(hours=1)
            n = self._wallet_store.prune_trades_older_than(cutoff)
            if n:
                logger.debug("HyperliquidChannel: pruned %d old wallet trades", n)
        except Exception as e:
            logger.warning("HyperliquidChannel: wallet trade prune failed: %s", e)
        finally:
            self._last_wallet_prune_ts = now

    async def _process_coin(
        self,
        coin: str,
        ctx: dict[str, Any],
        uni_item: dict[str, Any],
        now: datetime,
    ) -> None:
        """One coin per cycle: snapshot → normalize → features → detector → signal."""
        # 1) normalize
        try:
            raw, normalized = normalize_asset_ctx(
                coin, ctx,
                universe_meta=uni_item,
                ts_source=now,
            )
        except ValueError as e:
            logger.warning("HyperliquidChannel: normalize failed coin=%s: %s", coin, e)
            return

        # 2) Buffer-saving dedupe — skip when previous snapshot's cumulative + price both match
        cur_kv = (
            float(normalized.meta.get("day_ntl_vlm_usd", 0.0)),
            float(normalized.meta.get("mark_px", 0.0)),
        )
        last_kv = self._last_snapshot_kv.get(coin)
        if last_kv is not None and last_kv == cur_kv:
            # Completely identical → buffer append is meaningless (no impact on delta computation).
            # Raw is also low audit value. Still run the detector to update the sticky map.
            snapshot = self._features.compute_snapshot(coin, now)
            if snapshot is not None:
                self._inject_wallet_features(snapshot, coin, now)
                signal = self._detector.evaluate(snapshot)
                self._latest_signal[coin] = (now, signal)
            return
        self._last_snapshot_kv[coin] = cur_kv

        # 3) raw_store append (audit)
        if self._raw_store is not None:
            try:
                self._raw_store.append(raw)
            except Exception as e:
                logger.error("HyperliquidChannel: raw_store.append failed: %s", e)

        # 4) Update feature buffer
        self._features.add_events([normalized])
        self._last_event_ts = max(
            self._last_event_ts or normalized.ts_source,
            normalized.ts_source,
        )

        # 5) Feature snapshot
        snapshot = self._features.compute_snapshot(coin, now)
        if snapshot is None:
            return

        if self._feature_store is not None:
            try:
                self._feature_store.append(snapshot)
            except Exception as e:
                logger.error("HyperliquidChannel: feature_store.append failed: %s", e)

        # 5b) Query wallet_store → inject new_whale_* keys into snapshot.features
        self._inject_wallet_features(snapshot, coin, now)

        # 6) detector → channel signal
        signal = self._detector.evaluate(snapshot)
        self._latest_signal[coin] = (now, signal)

        if signal.tier != Tier.NORMAL:
            logger.info(
                "hyperliquid signal: coin=%s tier=%s score=%.3f reasons=%s",
                coin, signal.tier.value, signal.score, signal.reason_codes,
            )

    # ─────────────────────────────────────────────────────────────────
    # P9.2.P2 — wallet_store → injection into features dict
    # ─────────────────────────────────────────────────────────────────
    def _inject_wallet_features(
        self,
        snapshot: Any,
        coin: str,
        now: datetime,
    ) -> None:
        """Fill snapshot.features dict from wallet_store query results so the detector can see them.

        Keys injected (P9.2.P2 + P9.2.P3):
          new_whale_*  (P2):
            - new_whale_max_cum5m_usd  : max 5-min cumulative notional among fresh wallets
            - new_whale_count_24h      : count of fresh-wallet candidates (with trades in window)
            - new_whale_warmup_ready   : whether cold-start guard passed (1.0/0.0)
            - new_whale_last_side_code : +1.0=B(buy), -1.0=A(sell), 0.0=unknown
          cluster_*   (P3):
            - cluster_top_sum_notional_usd : sum notional of top cluster (USD)
            - cluster_top_n_wallets         : distinct wallet count of top cluster
            - cluster_warmup_ready          : cold-start guard (same as new_whale)
            - cluster_top_side_code         : +1.0=B / -1.0=A / 0.0=unknown

        Injects only zeros when wallet_store is None (detector treats as NORMAL).
        """
        feats = snapshot.features  # dict[str, float] — mutable
        if self._wallet_store is None:
            feats["new_whale_max_cum5m_usd"] = 0.0
            feats["new_whale_count_24h"] = 0.0
            feats["new_whale_warmup_ready"] = 0.0
            feats["new_whale_last_side_code"] = 0.0
            feats["cluster_top_sum_notional_usd"] = 0.0
            feats["cluster_top_n_wallets"] = 0.0
            feats["cluster_warmup_ready"] = 0.0
            feats["cluster_top_side_code"] = 0.0
            return

        # cold-start guard: detector only activates after warmup_after_h since store boot
        now_ms = int(now.timestamp() * 1000)
        warmup_ms = int(self._new_whale_warmup_after_h * 3600 * 1000)
        warmup_ready = (now_ms - self._wallet_store.started_at_ms) >= warmup_ms

        # ── new_whale_v1 query ──
        try:
            candidates = self._wallet_store.get_fresh_whale_candidates(
                now=now,
                coin=coin,
                window_min=self._new_whale_window_min,
                fresh_within_h=self._new_whale_fresh_within_h,
                min_cum_notional_usd=self._detector.config.new_whale_watch_usd,
            )
        except Exception as e:
            logger.warning(
                "HyperliquidChannel: wallet_store new_whale query failed coin=%s: %s",
                coin, e,
            )
            candidates = []

        if candidates:
            top = candidates[0]  # sorted: cum_notional_usd DESC
            feats["new_whale_max_cum5m_usd"] = float(top.cum_notional_usd)
            feats["new_whale_count_24h"] = float(len(candidates))
            feats["new_whale_last_side_code"] = (
                1.0 if top.last_side == "B" else (-1.0 if top.last_side == "A" else 0.0)
            )
        else:
            feats["new_whale_max_cum5m_usd"] = 0.0
            feats["new_whale_count_24h"] = 0.0
            feats["new_whale_last_side_code"] = 0.0
        feats["new_whale_warmup_ready"] = 1.0 if warmup_ready else 0.0

        # ── cluster_v1 query (P9.2.P3) ──
        # anchor_px is the current mark_px — fall back to normalized.meta.mark_px if missing from features.
        anchor_px = float(feats.get("mark_px", 0.0))
        if anchor_px <= 0.0:
            # When snapshot.features lacks mark_px, cluster query is meaningless → inject 0
            feats["cluster_top_sum_notional_usd"] = 0.0
            feats["cluster_top_n_wallets"] = 0.0
            feats["cluster_warmup_ready"] = 1.0 if warmup_ready else 0.0
            feats["cluster_top_side_code"] = 0.0
            return

        try:
            clusters = self._wallet_store.get_fresh_wallet_cluster(
                now=now,
                coin=coin,
                anchor_px=anchor_px,
                fresh_within_h=self._new_whale_fresh_within_h,
                cluster_window_min=self._cluster_window_min,
                price_band_pct=self._cluster_price_band_pct,
                # Match detector's watch threshold (cluster-candidate cutoff)
                min_n_wallets=self._detector.config.cluster_watch_n_wallets,
                min_sum_notional_usd=self._detector.config.cluster_watch_sum_usd,
            )
        except Exception as e:
            logger.warning(
                "HyperliquidChannel: wallet_store cluster query failed coin=%s: %s",
                coin, e,
            )
            clusters = []

        if clusters:
            ctop = clusters[0]  # sum_notional_usd DESC
            feats["cluster_top_sum_notional_usd"] = float(ctop.sum_notional_usd)
            feats["cluster_top_n_wallets"] = float(ctop.n_wallets)
            feats["cluster_top_side_code"] = (
                1.0 if ctop.side == "B" else (-1.0 if ctop.side == "A" else 0.0)
            )
        else:
            feats["cluster_top_sum_notional_usd"] = 0.0
            feats["cluster_top_n_wallets"] = 0.0
            feats["cluster_top_side_code"] = 0.0
        feats["cluster_warmup_ready"] = 1.0 if warmup_ready else 0.0
