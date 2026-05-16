"""
alerts/alert_ohlc_buffer.py — Per-channel rolling 1-minute OHLCV + buy/sell-split buffer
for alert-time price/volume plots.

────────────────────────────────────────────────────────────────────────
Role:
  Data source for the "Price vs time" + "Volume vs time" plot the user
  sees when an Email/Telegram/X alert fires.

  Mirrors the style of plot_event_symbol() in
  scripts/generate_historical_event_data.py (price line on top, diverging
  buy-sell volume on the bottom) and reuses it directly in the production
  alert flow.

  Each channel's (cme/polymarket/hyperliquid) live collector calls
  `push_trade()` per trade → internally accumulates into 1-minute buckets
  (open/high/low/close/buy_vol/sell_vol/neutral_vol).

  When an alert fires, the plot writer slices the last N minutes via
  `bars(channel, symbol, since, until)` and renders.

────────────────────────────────────────────────────────────────────────
Why 1-min bar accumulation? (no per-tick storage):
  · Active CME symbols like ES/BZ get 7h * 100tps = 2.5M ticks. Memory pressure.
  · Aggregating to 1-min bars yields 7h * 60 = 420 bars/symbol. ~80B/bar → about 34KB.
  · 1-min resolution matches the historical-event plot (consistent with user expectation).
  · 2-min / 5-min detectors can resample directly from 1-min bars.

────────────────────────────────────────────────────────────────────────
Thread safety:
  · push_trade / bars both mutate / copy inside a short critical section.
  · The daemon is a single asyncio loop, so true thread contention is rare,
    but CMELiveStreamer can use a producer thread, so the lock acts as a
    safety net.
"""

from __future__ import annotations

# ── Standard library ────────────────────────────────────────────────
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Iterable, Literal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Default retention — covers the 6h plot with some headroom.
#   Longest email/telegram window = 360 min (6h).
#   Add 60 min headroom → 420 min (7h) so edge bars are not clipped.
# ─────────────────────────────────────────────────────────────────────
DEFAULT_RETENTION_MINUTES: int = 420
BUCKET_SECONDS: int = 60  # 1-min bar.

# Aggressor side label — "buy" / "sell" / "neutral".
Side = Literal["buy", "sell", "neutral"]


# ─────────────────────────────────────────────────────────────────────
# Single 1-min bar (open/high/low/close + per-direction accumulated volume).
# ─────────────────────────────────────────────────────────────────────
@dataclass
class OhlcBar:
    """OHLCV + buy/sell-split per 1-minute (or BUCKET_SECONDS) bucket.

    Attributes:
        ts: bucket start time (UTC, floored to seconds).
        open: price of the first trade in the bucket.
        high: highest price in the bucket.
        low:  lowest price in the bucket.
        close: price of the last trade in the bucket.
        buy_vol: accumulated size of trades with aggressor=buy.
        sell_vol: accumulated size of trades with aggressor=sell.
        neutral_vol: accumulated size of trades with unknown aggressor.
        trade_count: number of trades in the bar (for debug).
    """
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    neutral_vol: float = 0.0
    trade_count: int = 0

    @property
    def total_vol(self) -> float:
        """Sum across all directions. Simple total used before matplotlib display."""
        return self.buy_vol + self.sell_vol + self.neutral_vol


# ─────────────────────────────────────────────────────────────────────
# Public API — per-channel × per-symbol rolling buffer.
# ─────────────────────────────────────────────────────────────────────
@dataclass
class AlertOhlcBuffer:
    """Per-(channel, symbol) 1-min OHLCV rolling tape.

    Args:
        retention_minutes: how long to keep bars (in minutes). Default 420 (= 7h).
            The oldest bar is lazily pruned during push_trade / bars calls.
        bucket_seconds: bar bucket size (seconds). Default 60 (1-min).

    Storage:
        key = (channel_lower, symbol) — channel is normalized to lowercase
            ("cme" / "polymarket" / "hyperliquid"). symbol kept as-is.
        value = deque[OhlcBar] (ascending time; left=oldest, right=newest).

    NOTE:
        Thread-safe (in case CME VM streamer in the codebase uses a producer
        thread). The lock is short — it only protects bar mutation.
    """
    retention_minutes: int = DEFAULT_RETENTION_MINUTES
    bucket_seconds: int = BUCKET_SECONDS
    _bars: dict[tuple[str, str], Deque[OhlcBar]] = field(
        default_factory=dict,
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def push_trade(
        self,
        *,
        channel: str,
        symbol: str,
        ts: datetime,
        price: float,
        size: float,
        side: Side = "neutral",
    ) -> None:
        """Record one trade into the buffer.

        Args:
            channel: "cme" / "polymarket" / "hyperliquid" (case-insensitive).
            symbol: same key as ChannelSignal.symbol (CME=root, PM=slug, HL=coin).
            ts: trade timestamp (UTC; tz-aware recommended).
            price: trade price.
            size: trade size — CME=contracts, PM=USD notional, HL=base coin sz.
                (Per-channel units are spelled out in plot labels — the buffer just sums.)
            side: aggressor direction — "buy" / "sell" / "neutral".

        Note:
            naive ts is treated as UTC. price/size that are 0 or negative are
            ignored (not a real trade). An empty symbol is also ignored.
        """
        if not symbol:
            return
        if price <= 0.0 or size <= 0.0:
            return

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        # 1-min bucket start time (floored to seconds).
        bucket_ts = _bucket_floor(ts, self.bucket_seconds)
        key = (channel.lower(), symbol)

        with self._lock:
            buf = self._bars.setdefault(key, deque())

            # If same bucket, mutate the last bar; otherwise append a new bar.
            if buf and buf[-1].ts == bucket_ts:
                bar = buf[-1]
                bar.high = max(bar.high, price)
                bar.low = min(bar.low, price)
                bar.close = price
                bar.trade_count += 1
            else:
                # If ts is older than the last bar (out-of-order)
                # — rare, but a safety net for dedupe / replay. Just ignore.
                if buf and bucket_ts < buf[-1].ts:
                    return
                bar = OhlcBar(
                    ts=bucket_ts,
                    open=price, high=price, low=price, close=price,
                    trade_count=1,
                )
                buf.append(bar)

            # Accumulate by side.
            if side == "buy":
                bar.buy_vol += size
            elif side == "sell":
                bar.sell_vol += size
            else:
                bar.neutral_vol += size

            # If too long, prune (memory protection).
            self._prune_locked(buf, now=ts)

    def bars(
        self,
        *,
        channel: str,
        symbol: str,
        since: datetime,
        until: datetime,
    ) -> list[OhlcBar]:
        """Return bars within [since, until] (ascending time, copied).

        Args:
            channel: same key as push_trade.
            symbol: same key as push_trade.
            since: start time (UTC, inclusive).
            until: end time (UTC, inclusive).

        Returns:
            list[OhlcBar] — empty list if no bars exist.
            Returned OhlcBars are not frozen copies but the same objects, so
            the caller must not mutate them (read-only).
        """
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)

        key = (channel.lower(), symbol)
        with self._lock:
            buf = self._bars.get(key)
            if not buf:
                return []
            return [b for b in buf if since <= b.ts <= until]

    def first_bar_ts(
        self, *, channel: str, symbol: str,
    ) -> datetime | None:
        """Time of the oldest bar currently retained.

        Used by the plot to label "data starts: HH:MM" — when the buffer
        starts later than the window, it lets the user know the data is partial.
        """
        key = (channel.lower(), symbol)
        with self._lock:
            buf = self._bars.get(key)
            if not buf:
                return None
            return buf[0].ts

    # ─────────────────────────────────────────────────────────────
    # Internal — retention prune.
    # ─────────────────────────────────────────────────────────────
    def _prune_locked(
        self, buf: Deque[OhlcBar], *, now: datetime,
    ) -> None:
        """Drop bars older than retention. Assumes the lock is held."""
        cutoff = now.timestamp() - self.retention_minutes * 60
        while buf and buf[0].ts.timestamp() < cutoff:
            buf.popleft()

    # ─────────────────────────────────────────────────────────────
    # Debug helpers.
    # ─────────────────────────────────────────────────────────────
    def snapshot_keys(self) -> list[tuple[str, str]]:
        """Which (channel, symbol) pairs are currently in the buffer (for tests/health)."""
        with self._lock:
            return list(self._bars.keys())

    def bar_count(self, *, channel: str, symbol: str) -> int:
        """Bar count for a single (channel, symbol)."""
        key = (channel.lower(), symbol)
        with self._lock:
            return len(self._bars.get(key, ()))


# ─────────────────────────────────────────────────────────────────────
# Normalization helper — sticky 1-min bucket start time.
# ─────────────────────────────────────────────────────────────────────
def _bucket_floor(ts: datetime, bucket_seconds: int) -> datetime:
    """Floor ts to a multiple of bucket_seconds."""
    epoch_s = int(ts.timestamp())
    floored = epoch_s - (epoch_s % bucket_seconds)
    return datetime.fromtimestamp(floored, tz=ts.tzinfo or timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Side inference helpers — channel-specific raw value → "buy" / "sell" / "neutral".
# (Collected here so each channel's collector can simply import them.)
# ─────────────────────────────────────────────────────────────────────
def cme_side_from_aggressor(side: str | None) -> Side:
    """CME Databento side code → buy/sell/neutral.

    Convention (Databento + comments in live_streamer):
      · 'A' = Ask-taker (= buyer aggressor)  → "buy"
      · 'B' = Bid-taker (= seller aggressor) → "sell"
      · Anything else / None / 'N' → "neutral"
    """
    if side == "A":
        return "buy"
    if side == "B":
        return "sell"
    return "neutral"


def polymarket_side_from_api(side: str | None) -> Side:
    """Polymarket data-api/trades 'side' field → buy/sell/neutral.

    Convention (BUY = buying YES = "buy" pressure, SELL = "sell" pressure).
    """
    if side is None:
        return "neutral"
    s = side.strip().upper()
    if s == "BUY":
        return "buy"
    if s == "SELL":
        return "sell"
    return "neutral"


def hyperliquid_side_from_taker(side: str | None) -> Side:
    """Hyperliquid recentTrades 'side' field → buy/sell/neutral.

    Convention (per comments in collector.py):
      · "B" = taker buy  → "buy"
      · "A" = taker sell → "sell"
    """
    if side == "B":
        return "buy"
    if side == "A":
        return "sell"
    return "neutral"


__all__ = [
    "AlertOhlcBuffer",
    "OhlcBar",
    "Side",
    "DEFAULT_RETENTION_MINUTES",
    "BUCKET_SECONDS",
    "cme_side_from_aggressor",
    "polymarket_side_from_api",
    "hyperliquid_side_from_taker",
]
