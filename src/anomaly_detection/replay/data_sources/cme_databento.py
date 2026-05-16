"""
data_sources/cme_databento.py — CME historical trades source (Databento wrap).

────────────────────────────────────────────────────────────────────────
Role:
  Historical bar fetcher for the CME channel in Replay.
  Internally reuses the production DatabentoClient (databento_client.py) as-is.
  → Avoids re-implementing cache / cost cap / API key handling.

────────────────────────────────────────────────────────────────────────
Flow:

  1) warmup(event):
       · Pick out the CME roots (ES/BZ/CL/GC) from event.primary_symbols.
       · For each root, call client.fetch_historical_range(root, window_start, window_end)
         → receive one large trades DataFrame (instant on parquet cache hit).
       · Store that DataFrame in the in-memory dict[root → DataFrame].

  2) get_bar(symbol, sim_clock):
       · Slice the stored DataFrame to [sim_clock, sim_clock+60s) and return
         BarTick(payload={"trades": [trade_dict, ...]}).
       · If 0 trades in the minute → return None (= "no data", detector stays NORMAL).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Only valid CME roots are processed — non-CME symbols (e.g. Polymarket slugs)
    cause supports() to return False → the runner falls back to NullDataSource
    (or just ignores them).

  · When converting trades → dict, "side" preserves the raw 'A'/'B'/'N' string.
    Converting NormalizedEvent.side (BUY/SELL) is ChannelReplay's job — the data
    source emits raw, the channel layer assigns meaning (separation of concerns).

  · BarTick.payload["trades"] is a list[dict], each with {"ts", "price", "size",
    "side", "symbol"}. If a minute has 100 trades, there will be 100 entries.
    For v0 this is the simplest.

  · DatabentoClient is sync-context-safe (uses to_thread internally). warmup()
    is only called once, so parquet cache + cost cap alone make this safe enough.

────────────────────────────────────────────────────────────────────────
References:
  · src/anomaly/channels/cme/databento_client.py  — reused client
  · src/anomaly/channels/cme/post_analysis.py     — DataFrame schema reference
  · docs/p10-replay-framework.md §3.2             — HistoricalDataSource Protocol
"""

from __future__ import annotations

# --- standard library ---
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

# --- third-party ---
import pandas as pd

# --- local: production CME client ---
from ...channels.cme.databento_client import (
    CME_TO_CONTINUOUS,
    CostTracker,
    DatabentoClient,
)
from ...core.schemas import CHANNEL_CME

# --- local: replay schemas ---
from ..schemas import BarTick, HistoricalEvent

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Module constants — default paths when env is missing (same as production)
# ─────────────────────────────────────────────────────────────────────
# Estimate the repo root — this file is at: src/anomaly/replay/data_sources/cme_databento.py
# repo root: 4 levels up (data_sources → replay → anomaly → src → repo)
_REPO_ROOT: Path = Path(__file__).resolve().parents[4]
_DEFAULT_DATA_PATH: Path = _REPO_ROOT / "data" / "databento"


# ─────────────────────────────────────────────────────────────────────
# Helper — build default DatabentoClient from env
# ─────────────────────────────────────────────────────────────────────
def build_default_databento_client(
    *,
    monthly_cap_usd: float = 40.0,
) -> DatabentoClient:
    """Build a default DatabentoClient from env (DATABENTO_API_KEY, ANOMALY_DATA_PATH).

    Places cost_tracker.json and the cache folder in the same location as
    production code (data/databento/) — replay shares the same month cap as
    production's spend.

    Raises:
        RuntimeError: when DATABENTO_API_KEY env is missing.
    """
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DATABENTO_API_KEY env var is not set. "
            "Set it in .env or the shell environment before running CME replay. "
            "(Replay requires historical fetch — no fallback.)"
        )

    # If ANOMALY_DATA_PATH is set, use it as base; otherwise repo root/data/databento.
    base_data_path = os.environ.get("ANOMALY_DATA_PATH")
    if base_data_path:
        data_path = Path(base_data_path) / "databento"
    else:
        data_path = _DEFAULT_DATA_PATH

    cost_tracker = CostTracker(
        state_file=data_path / "cost_tracker.json",
        monthly_cap_usd=monthly_cap_usd,
    )
    cache_dir = data_path / "cache"

    return DatabentoClient(
        api_key=api_key,
        cost_tracker=cost_tracker,
        cache_dir=cache_dir,
    )


# ─────────────────────────────────────────────────────────────────────
# CmeDatabentoSource — HistoricalDataSource impl for CME.
# ─────────────────────────────────────────────────────────────────────
class CmeDatabentoSource:
    """CME trades 1-min bar fetcher (Databento backed).

    Usage:
        source = CmeDatabentoSource()                 # uses env-based client
        await source.warmup(event)                    # fetch entire window upfront
        bar = source.get_bar("ES", sim_clock)         # in-memory slice
        await source.close()
    """

    channel: ClassVar[str] = CHANNEL_CME

    def __init__(
        self,
        *,
        client: DatabentoClient | None = None,
    ) -> None:
        """
        Args:
            client: Direct injection (for tests). If None, built from env.
        """
        # If no client is injected, build from env. If env is absent, build_default raises.
        self._client = client  # lazy build in warmup() (import alone must work without env)

        # symbol (root) → DataFrame mapping. Populated by warmup().
        # DataFrame index is ts_recv (UTC); columns: symbol, price, size, side.
        self._trades_by_symbol: dict[str, pd.DataFrame] = {}

        # Symbols that were processed during warmup — used to detect unknown symbols in get_bar.
        self._warmed_symbols: set[str] = set()

    # ─────────────────────────────────────────────────────────────────
    # supports — pre-check by the runner that this source can handle the event.
    # ─────────────────────────────────────────────────────────────────
    def supports(self, event: HistoricalEvent) -> bool:
        """True if this event has any CME root to fetch.

        - CME is the primary channel: at least one CME root must be in primary_symbols.
        - CME is a secondary channel: always True (verify spillover across all CME roots).
        - Otherwise: False.
        """
        return len(self._cme_roots_for_event(event)) > 0

    # ─────────────────────────────────────────────────────────────────
    # warmup — fetch all trades for the entire event window in one pass.
    # ─────────────────────────────────────────────────────────────────
    async def warmup(self, event: HistoricalEvent) -> None:
        """Fetch trades for the event.window_start ~ window_end range once per root.

        If the DatabentoClient has a parquet cache hit, cost is 0 / returns immediately.
        On miss, it runs after passing the cost cap check.
        """
        # 1) Lazy client build — env check happens only when a fetch is actually needed.
        if self._client is None:
            self._client = build_default_databento_client()

        # 2) Pick the CME roots for this event (only the listed ones if primary,
        #    all of them if secondary).
        cme_roots = self._cme_roots_for_event(event)
        if not cme_roots:
            logger.info(
                "CmeDatabentoSource: no CME roots in event=%s "
                "(primary_channel=%s primary_symbols=%s secondary=%s) — "
                "skipping warmup",
                event.event_id, event.primary_channel,
                event.primary_symbols, event.secondary_channels,
            )
            return

        # 3) fetch_historical_range per root. window_end is [start, end) half-open and
        #    Databento is also [start, end), so they match — add +1min so we still get
        #    the trades of the final minute.
        fetch_start = event.window_start
        fetch_end = event.window_end + timedelta(minutes=1)

        for root in cme_roots:
            logger.info(
                "CmeDatabentoSource: warmup fetching root=%s window=[%s, %s) for event=%s",
                root, fetch_start.isoformat(), fetch_end.isoformat(), event.event_id,
            )
            df = await self._client.fetch_historical_range(
                root=root, start=fetch_start, end=fetch_end,
            )
            self._trades_by_symbol[root] = df
            self._warmed_symbols.add(root)
            logger.info(
                "CmeDatabentoSource: root=%s loaded %d trade rows",
                root, len(df),
            )

    # ─────────────────────────────────────────────────────────────────
    # get_bar — 1-minute in-memory slice → BarTick.
    # ─────────────────────────────────────────────────────────────────
    def get_bar(self, symbol: str, sim_clock: datetime) -> BarTick | None:
        """Bundle the trades in [sim_clock, sim_clock+60s) into a BarTick and return it.

        Args:
            symbol: CME root (ES/BZ/CL/GC).
            sim_clock: 1-min bar start (UTC).

        Returns:
            BarTick: when at least 1 trade exists in that minute.
            None: 0 trades (market closed / no trades / unknown symbol).
        """
        df = self._trades_by_symbol.get(symbol)
        if df is None:
            # Symbol not in warmup — silently return None (handled as NORMAL by fusion).
            return None

        bar_end = sim_clock + timedelta(minutes=1)

        # The DataFrame index is ts_recv (UTC, tz-aware). loc[start:end] slice.
        # Databento's to_df() result is sorted, so the slice is O(log n).
        try:
            slice_df = df.loc[sim_clock:bar_end]  # type: ignore[misc]
        except (KeyError, TypeError):
            # If the index is empty or type-mismatched — fall back to a boolean mask.
            mask = (df.index >= sim_clock) & (df.index < bar_end)
            slice_df = df[mask]
        else:
            # df.loc[a:b] is inclusive of b — we want b exclusive, so filter again.
            slice_df = slice_df[slice_df.index < bar_end]

        if len(slice_df) == 0:
            return None

        # Trade rows → list[dict] (ChannelReplay normalizes → NormalizedEvent).
        # ts is an explicit datetime (UTC) — taken straight from the DataFrame index.
        trades: list[dict[str, Any]] = []
        for ts, row in slice_df.iterrows():
            trades.append({
                "ts": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                "price": float(row["price"]),
                "size": int(row["size"]),
                "side": str(row.get("side", "N")),
                "symbol": str(row.get("symbol", symbol)),  # contract code (e.g. "ESM5")
            })

        return BarTick(
            channel=CHANNEL_CME,
            symbol=symbol,
            ts=sim_clock,
            bar_seconds=60,
            payload={"trades": trades},
        )

    # ─────────────────────────────────────────────────────────────────
    # close — DatabentoClient itself doesn't need close. Just clear memory.
    # ─────────────────────────────────────────────────────────────────
    async def close(self) -> None:
        """Release memory. DatabentoClient is stateless, so no separate close."""
        self._trades_by_symbol.clear()
        self._warmed_symbols.clear()

    # ─────────────────────────────────────────────────────────────────
    # Public read-only — channel_replays uses this to check active symbols.
    # ─────────────────────────────────────────────────────────────────
    @property
    def warmed_symbols(self) -> frozenset[str]:
        """Roots that actually have data after warmup(). Empty → supports() False."""
        return frozenset(self._warmed_symbols)

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _cme_roots_in(symbols: list[str]) -> list[str]:
        """Only those of the given symbol list that are registered in CME_TO_CONTINUOUS."""
        return [s for s in symbols if s in CME_TO_CONTINUOUS]

    @classmethod
    def _cme_roots_for_event(cls, event: HistoricalEvent) -> list[str]:
        """List of CME roots this event should fetch.

        Rules:
          1) CME is the primary channel → CME roots from primary_symbols.
             (e.g. liberation_day primary_channel=cme primary_symbols=['BZ'])
          2) CME is a secondary channel → all known CME roots (spillover verification).
             (e.g. china_tariff_100 primary=hyperliquid secondary=[x, cme]
                  → fetch all of ES/CL/BZ/GC to see where the spike happened.)
          3) Otherwise: empty list.
        """
        if event.primary_channel == CHANNEL_CME:
            return cls._cme_roots_in(event.primary_symbols)
        if CHANNEL_CME in event.secondary_channels:
            # All known CME continuous roots.
            return list(CME_TO_CONTINUOUS.keys())
        return []

    def __repr__(self) -> str:
        return (
            f"<CmeDatabentoSource warmed={sorted(self._warmed_symbols)} "
            f"rows_total={sum(len(df) for df in self._trades_by_symbol.values())}>"
        )


__all__ = ["CmeDatabentoSource", "build_default_databento_client"]
