"""
data_sources/hyperliquid_trades_csv.py — Hyperliquid pre-processed CSV trades source.

────────────────────────────────────────────────────────────────────────
Role (P10.4 — "C-free" path, free strategy):
  Instead of the Hyperliquid S3 archive (requester-pays, requires AWS), read
  the "per-user full trade history CSV" distributed by hypedexer.com and feed
  it into replay.

  This CSV's columns (per the hypedexer export):
    time       : "YYYY-MM-DD HH:MM:SS"  (UTC, no tz suffix)
    coin       : "BTC", "ETH", ...
    dir        : "Open Short" / "Close Short" / "Open Long" / "Close Long"
                 / "Auto-Deleveraging" / "Liquidation" etc.
    px         : fill price (USD)
    sz         : fill size (base coin units)
    ntl        : notional USD (= px * sz)
    fee        : fee (USDC)
    feeToken   : "USDC"
    closedPnl  : realized PnL on close (USDC)
    hash       : block/tx hash (identical across split fills of the same order)

  This CSV contains **only one wallet's fills** (the user exported with a wallet
  filter). Therefore:
    · The vol baseline is "this wallet's historical vol", not "whole-market vol"
      → vol_z_v1 may overreact even on the first trade with a large z-score.
    · cluster_v1 requires multiple wallets so it **cannot be verified** — we'll inject 0.
    · new_whale_v1 is the key verification detector.

  ---------------------------------------------------------------
  Source lifecycle:
    · warmup(event): decompress the CSV once + load into memory, slice to event window.
      Group into list[trade dict] by 1-minute bucket. O(1) lookup afterwards.
    · get_bar(symbol, sim_clock): return a BarTick with the trades list for
      that minute. None if 0 trades.
    · close(): clear memory.

────────────────────────────────────────────────────────────────────────
References:
  · docs/p10-replay-framework.md §3.2 — HistoricalDataSource Protocol
  · src/anomaly/replay/data_sources/base.py
  · src/anomaly/replay/data_sources/cme_databento.py — structural mirror
"""

from __future__ import annotations

import csv
import gzip
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

from ...core.schemas import CHANNEL_HYPERLIQUID
from ..schemas import BarTick, HistoricalEvent

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Module constants
# ─────────────────────────────────────────────────────────────────────
# repo root: this file = <repo>/src/anomaly/replay/data_sources/hyperliquid_trades_csv.py
# parents[4] = <repo>
_REPO_ROOT: Path = Path(__file__).resolve().parents[4]

# Default CSV path (the file the user got from hypedexer). Existence is checked at warmup.
_DEFAULT_CSV_PATH: Path = (
    _REPO_ROOT / "data" / "hyperliquid-trades-2025-01-01-to-2026-04-21.csv.gz"
)


# ─────────────────────────────────────────────────────────────────────
# HyperliquidTradesCsvSource
# ─────────────────────────────────────────────────────────────────────
class HyperliquidTradesCsvSource:
    """Load a Hyperliquid single-wallet trades CSV into minute buckets.

    Usage:
        source = HyperliquidTradesCsvSource()
        await source.warmup(event)
        bar = source.get_bar("BTC", sim_clock)   # minute bucket
        await source.close()
    """

    channel: ClassVar[str] = CHANNEL_HYPERLIQUID

    def __init__(
        self,
        *,
        csv_path: Path | None = None,
    ) -> None:
        """
        Args:
            csv_path: CSV (gzipped or plain) absolute path. None → _DEFAULT_CSV_PATH.
        """
        self._csv_path: Path = csv_path or _DEFAULT_CSV_PATH

        # In-memory bucket: (coin, minute_start_utc) → list[trade_dict]
        # minute_start_utc is tz-aware datetime with second=0.
        self._trades_by_minute: dict[
            tuple[str, datetime], list[dict[str, Any]]
        ] = {}

        # Set of coins for which warmup succeeded — referenced by channel_replay
        # when checking e.g. "ETH is not supported".
        self._warmed_symbols: set[str] = set()

        # For logging
        self._event_id: str | None = None
        self._total_loaded: int = 0

    # ─────────────────────────────────────────────────────────────────
    # supports — can this source handle the event?
    # ─────────────────────────────────────────────────────────────────
    def supports(self, event: HistoricalEvent) -> bool:
        """True if primary_channel=hyperliquid and the CSV file exists."""
        if event.primary_channel != CHANNEL_HYPERLIQUID:
            return False
        return self._csv_path.exists()

    # ─────────────────────────────────────────────────────────────────
    # warmup — decompress CSV once, slice to event window, bucket by minute.
    # ─────────────────────────────────────────────────────────────────
    async def warmup(self, event: HistoricalEvent) -> None:
        """Read the CSV into memory and bucket by minute. Drop rows outside the event window.

        If Hyperliquid is not the event's primary channel, return immediately —
        avoid scanning 170k rows unnecessarily when running other events.
        """
        self._event_id = event.event_id

        # Skip the scan when not the primary channel — avoid overhead on
        # Polymarket/CME events.
        if not self.supports(event):
            logger.debug(
                "HyperliquidTradesCsvSource: skip warmup for event=%s "
                "(primary=%s, csv_exists=%s)",
                event.event_id, event.primary_channel, self._csv_path.exists(),
            )
            return

        if not self._csv_path.exists():
            logger.warning(
                "HyperliquidTradesCsvSource: CSV not found at %s — skip warmup",
                self._csv_path,
            )
            return

        window_start = event.window_start  # UTC
        window_end = event.window_end + timedelta(minutes=1)  # inclusive buffer

        logger.info(
            "HyperliquidTradesCsvSource: loading %s for event=%s window=[%s, %s)",
            self._csv_path.name, event.event_id,
            window_start.isoformat(), window_end.isoformat(),
        )

        rows_in_window = 0
        rows_total_scanned = 0
        coins_seen: set[str] = set()

        # Determine gzip vs plain text by suffix. Flexible.
        opener: Any
        if self._csv_path.suffix == ".gz":
            opener = lambda: gzip.open(  # noqa: E731
                self._csv_path, mode="rt", encoding="utf-8", newline=""
            )
        else:
            opener = lambda: open(  # noqa: E731
                self._csv_path, mode="rt", encoding="utf-8", newline=""
            )

        with opener() as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows_total_scanned += 1

                # time column → UTC datetime. CSV has no tz suffix, so assume UTC.
                ts_raw = row.get("time", "").strip()
                if not ts_raw:
                    continue
                try:
                    ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
                    ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    # malformed — silently skip (millions of rows, don't spam log)
                    continue

                # Outside window → if all rows were sorted ascending by ts we could
                # skip before window_start and break after window_end. Order isn't
                # guaranteed, so we only skip.
                if ts < window_start or ts >= window_end:
                    continue

                coin = (row.get("coin") or "").strip()
                if not coin:
                    continue
                coins_seen.add(coin)

                # Parse numeric fields — on failure, skip (fail-soft).
                try:
                    px = float(row.get("px", "0") or 0)
                    sz = float(row.get("sz", "0") or 0)
                    ntl = float(row.get("ntl", "0") or 0)
                except ValueError:
                    continue

                if px <= 0 or sz <= 0 or ntl <= 0:
                    continue

                direction = (row.get("dir") or "").strip()

                # minute bucket key: floor ts to second=0.
                minute_start = ts.replace(second=0, microsecond=0)

                trade: dict[str, Any] = {
                    "ts": ts,
                    "coin": coin,
                    "dir": direction,
                    "px": px,
                    "sz": sz,
                    "ntl": ntl,
                    "hash": (row.get("hash") or "").strip(),
                }

                self._trades_by_minute.setdefault(
                    (coin, minute_start), []
                ).append(trade)
                rows_in_window += 1

        self._total_loaded = rows_in_window
        self._warmed_symbols = set(
            coin for (coin, _), trades in self._trades_by_minute.items() if trades
        )

        logger.info(
            "HyperliquidTradesCsvSource: loaded %d rows (of %d scanned) "
            "for event=%s, coins_in_window=%s, coins_seen_anywhere=%s",
            rows_in_window, rows_total_scanned, event.event_id,
            sorted(self._warmed_symbols), sorted(coins_seen),
        )

        if not self._warmed_symbols:
            logger.warning(
                "HyperliquidTradesCsvSource: 0 rows inside window — "
                "CSV does not cover this event window. "
                "Re-check the CSV date range vs. the event window."
            )

    # ─────────────────────────────────────────────────────────────────
    # get_bar — 1-minute slice.
    # ─────────────────────────────────────────────────────────────────
    def get_bar(self, symbol: str, sim_clock: datetime) -> BarTick | None:
        """Return a BarTick containing the trades in [sim_clock, sim_clock+60s).

        Args:
            symbol: coin name (e.g. "BTC").
            sim_clock: 1-minute bar start (UTC, tz-aware, second=0 expected).

        Returns:
            BarTick: when at least one trade exists in that minute.
            None: 0 trades (or symbol not warmed).
        """
        # Defensive: normalize sim_clock to second=0.
        key_ts = sim_clock.replace(second=0, microsecond=0)
        trades = self._trades_by_minute.get((symbol, key_ts))
        if not trades:
            return None

        return BarTick(
            channel=CHANNEL_HYPERLIQUID,
            symbol=symbol,
            ts=key_ts,
            bar_seconds=60,
            payload={"trades": trades},
        )

    # ─────────────────────────────────────────────────────────────────
    # close — release memory.
    # ─────────────────────────────────────────────────────────────────
    async def close(self) -> None:
        """Clear memory. File handles are already closed at the end of warmup."""
        self._trades_by_minute.clear()
        self._warmed_symbols.clear()

    # ─────────────────────────────────────────────────────────────────
    # Public read-only (used by channel_replay to check active coins).
    # ─────────────────────────────────────────────────────────────────
    @property
    def warmed_symbols(self) -> frozenset[str]:
        """Coins that actually have data inside the event window after warmup."""
        return frozenset(self._warmed_symbols)

    @property
    def total_loaded_rows(self) -> int:
        """Total rows loaded (debug)."""
        return self._total_loaded

    def __repr__(self) -> str:
        return (
            f"<HyperliquidTradesCsvSource event={self._event_id} "
            f"rows={self._total_loaded} coins={sorted(self._warmed_symbols)}>"
        )


__all__ = ["HyperliquidTradesCsvSource"]
