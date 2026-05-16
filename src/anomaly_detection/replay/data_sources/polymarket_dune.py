"""
data_sources/polymarket_dune.py — Polymarket historical trades source (Dune wrap).

────────────────────────────────────────────────────────────────────────
Role:
  Historical trade fetcher for the Polymarket channel in Replay.
  - slug (event.primary_symbols) → conditionId mapping: Polymarket Gamma API
  - Fetch all trades for conditionId + window: Dune Analytics (saved query)
  - parquet cache (disk-first design, same pattern as CME)

────────────────────────────────────────────────────────────────────────
Flow:

  1) warmup(event):
       · event.primary_channel != polymarket → no activation (not supported).
       · For each slug in event.primary_symbols (slug list):
            a) Gamma API → market metadata → extract conditionId.
            b) On parquet cache hit, load immediately.
            c) On miss, execute Dune saved query (env DUNE_QUERY_ID_POLYMARKET_TRADES).
            d) Store result DataFrame in self._trades_by_symbol[slug] (after caching).

  2) get_bar(symbol=slug, sim_clock):
       · Return a BarTick sliced to [sim_clock, sim_clock+60s) from the DataFrame.
       · 0 trades → None (= "no data" — detector stays NORMAL).

────────────────────────────────────────────────────────────────────────
Design decisions (why this way):

  · Unlike CME, "secondary spillover" monitoring is meaningless for Polymarket.
    Each slug is a specific market, so "monitor every slug" doesn't fit. Therefore:
      - If primary, fetch every explicitly listed slug.
      - If secondary, do not activate (if an event md author wants Polymarket
        monitored, they must list the slug as primary or in a separate list).
        v0 simplification.

  · Dune saved query (free plan):
      · Save the SQL once on dune.com (params: condition_id, start_ts, end_ts).
      · Our code only executes by query_id → swaps in params.
      · Free plan: 2,500 credits/month. 1 execution ≈ 10 credits (small) → ~250 runs.
      · Re-execution for the same condition_id+window is avoided via parquet cache.

  · Converting trade rows → NormalizedEvent is the responsibility of ChannelReplay
    (the data source only preserves raw column names). Same pattern as CME.

  · If DUNE_API_KEY / DUNE_QUERY_ID_POLYMARKET_TRADES are missing, warmup raises —
    fail-fast (do not silently skip on missing env).

────────────────────────────────────────────────────────────────────────
References:
  · src/anomaly/channels/polymarket/collector.py — Gamma fetch_market reuse
  · docs/p10-polymarket-replay.md                — SQL template + ops guide
  · docs/p10-replay-framework.md §3.2            — HistoricalDataSource Protocol
"""

from __future__ import annotations

# --- standard library ---
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

# --- third-party ---
import pandas as pd

# --- local: production polymarket layer (Gamma) ---
from ...channels.polymarket.collector import PolymarketCollector
from ...core.schemas import CHANNEL_POLYMARKET

# --- local: replay schemas ---
from ..schemas import BarTick, HistoricalEvent

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Module constants — default cache location when env is not set
# ─────────────────────────────────────────────────────────────────────
# Estimate the repo root — this file is at: src/anomaly/replay/data_sources/polymarket_dune.py
# repo root: 4 levels up (data_sources → replay → anomaly → src → repo)
_REPO_ROOT: Path = Path(__file__).resolve().parents[4]
_DEFAULT_CACHE_DIR: Path = _REPO_ROOT / "data" / "anomaly" / "replay_cache" / "polymarket"

# Dune performance tier:
#   "small" : free/Plus plan — ~10 credits per execute, low memory
#   "medium": Plus or higher — ~20 credits, OK for larger datasets
#   "large" : Premium       — ~40 credits
# Our query is single-market trades for ~1 week, so "small" is enough (free plan compatible).
_DUNE_PERFORMANCE: str = "small"

# Maximum wait time (seconds) when Dune execute is polled asynchronously. 1 minute is usually enough.
_DUNE_WAIT_TIMEOUT_S: int = 120

# Dune query parameter names — the saved query must define the same names.
_PARAM_CONDITION_ID: str = "condition_id"
_PARAM_START_TS: str = "start_ts"
_PARAM_END_TS: str = "end_ts"


# ─────────────────────────────────────────────────────────────────────
# Helper — build DuneClient from env (lazy import to keep module light)
# ─────────────────────────────────────────────────────────────────────
def _build_default_dune_client():
    """Create a dune-client DuneClient instance from env (DUNE_API_KEY).

    Lazy import — for runs where replay doesn't actually touch Polymarket,
    we don't want an import error if dune-client isn't installed.

    Raises:
        RuntimeError: when DUNE_API_KEY env is missing.
        ImportError: when the dune-client package is not installed (with guidance).
    """
    api_key = os.environ.get("DUNE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DUNE_API_KEY env var is not set. "
            "Set it in .env or the shell environment before running Polymarket replay. "
            "(Get one at https://dune.com → Settings → API.)"
        )

    try:
        from dune_client.client import DuneClient
    except ImportError as exc:
        raise ImportError(
            "dune-client is not installed. Run: pip install dune-client"
        ) from exc

    return DuneClient(api_key=api_key)


# ─────────────────────────────────────────────────────────────────────
# PolymarketDuneSource — HistoricalDataSource impl for Polymarket.
# ─────────────────────────────────────────────────────────────────────
class PolymarketDuneSource:
    """Polymarket trades 1-min bar fetcher (Dune backed).

    Usage:
        source = PolymarketDuneSource()           # uses env-based clients
        await source.warmup(event)                # fetch entire window upfront
        bar = source.get_bar("market-slug", sim_clock)
        await source.close()

    Args:
        dune_client: Direct injection (for tests). If None, built from env.
        gamma_collector: Direct injection (for tests). If None, a new one is created.
        query_id: Dune saved query ID. If None, env DUNE_QUERY_ID_POLYMARKET_TRADES.
        cache_dir: parquet cache dir. If None, _DEFAULT_CACHE_DIR.
    """

    channel: ClassVar[str] = CHANNEL_POLYMARKET

    def __init__(
        self,
        *,
        dune_client: Any = None,
        gamma_collector: PolymarketCollector | None = None,
        query_id: int | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        # Lazy resolution — even without env, the import itself must succeed.
        self._dune = dune_client
        self._gamma = gamma_collector or PolymarketCollector()
        self._query_id = query_id  # lazy resolve in warmup().

        # parquet cache directory — mkdir if missing.
        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # symbol(slug) → DataFrame mapping. Populated by warmup().
        # DataFrame index is ts (UTC, tz-aware); columns: condition_id, price, size_usd, side.
        self._trades_by_symbol: dict[str, pd.DataFrame] = {}

        # Which symbols (slugs) actually have data after warmup.
        self._warmed_symbols: set[str] = set()

        # slug → conditionId mapping (Gamma result cache).
        self._condition_id_by_slug: dict[str, str] = {}

    # ─────────────────────────────────────────────────────────────────
    # supports — pre-check by the runner that this source can handle the event.
    # ─────────────────────────────────────────────────────────────────
    def supports(self, event: HistoricalEvent) -> bool:
        """True if this event has at least one Polymarket slug to fetch.

        - Polymarket is the primary channel: primary_symbols must have ≥1 entry.
        - Polymarket as secondary is not supported in v0 (False).
        """
        return len(self._slugs_for_event(event)) > 0

    # ─────────────────────────────────────────────────────────────────
    # warmup — fetch all trades for the full event window in one go.
    # ─────────────────────────────────────────────────────────────────
    async def warmup(self, event: HistoricalEvent) -> None:
        """Fetch trades for the range event.window_start ~ window_end, once per slug.

        Cache hit → 0 cost / immediate return.
        Miss → Dune execute (medium tier).

        Failures (slug mapping miss, Dune errors, etc.) log a warning and skip that
        slug — other slugs keep going. If self._trades_by_symbol ends up empty,
        ChannelReplay returns None from step().
        """
        slugs = self._slugs_for_event(event)
        if not slugs:
            logger.info(
                "PolymarketDuneSource: no Polymarket slugs in event=%s "
                "(primary_channel=%s primary_symbols=%s) — skipping warmup",
                event.event_id, event.primary_channel, event.primary_symbols,
            )
            return

        # Lazy build of clients/query_id.
        if self._dune is None:
            self._dune = _build_default_dune_client()
        if self._query_id is None:
            qid_str = os.environ.get("DUNE_QUERY_ID_POLYMARKET_TRADES")
            if not qid_str:
                raise RuntimeError(
                    "DUNE_QUERY_ID_POLYMARKET_TRADES env var is not set. "
                    "Save the SQL template (docs/p10-polymarket-replay.md) on dune.com, "
                    "then put the query_id (number from the URL) into .env."
                )
            try:
                self._query_id = int(qid_str)
            except ValueError as exc:
                raise RuntimeError(
                    f"DUNE_QUERY_ID_POLYMARKET_TRADES must be an integer, got '{qid_str}'"
                ) from exc

        # Buffer on both sides of the window — if Polymarket trades don't end
        # exactly on the minute boundary, +1 minute ensures the last bar is included,
        # and a small front pad helps baseline stability.
        fetch_start = event.window_start
        fetch_end = event.window_end + timedelta(minutes=1)

        # Open Gamma session (used to resolve slug → conditionId).
        await self._gamma.open()

        try:
            for slug in slugs:
                try:
                    await self._warmup_one_slug(
                        slug=slug,
                        event_id=event.event_id,
                        fetch_start=fetch_start,
                        fetch_end=fetch_end,
                    )
                except Exception as exc:  # noqa: BLE001
                    # One slug failing doesn't stop the rest.
                    logger.warning(
                        "PolymarketDuneSource: slug=%s warmup failed (event=%s): %s",
                        slug, event.event_id, exc,
                    )
        finally:
            # We're done with Gamma — close. Dune client is stateless (httpx internally).
            await self._gamma.close()

    async def _warmup_one_slug(
        self,
        *,
        slug: str,
        event_id: str,
        fetch_start: datetime,
        fetch_end: datetime,
    ) -> None:
        """For a single slug: slug→conditionId mapping + cache check + (if needed) Dune fetch."""
        # 1) slug → conditionId.
        condition_id = await self._resolve_condition_id(slug)
        if condition_id is None:
            logger.warning(
                "PolymarketDuneSource: slug=%s → conditionId not found via Gamma "
                "(market may not exist or be closed). event=%s",
                slug, event_id,
            )
            return

        # 2) cache lookup.
        cache_path = self._cache_path(condition_id, fetch_start, fetch_end)
        if cache_path.exists():
            logger.info(
                "PolymarketDuneSource: slug=%s cache HIT %s",
                slug, cache_path.name,
            )
            df = pd.read_parquet(cache_path)
        else:
            # 3) Dune execute.
            logger.info(
                "PolymarketDuneSource: slug=%s cache MISS — Dune execute "
                "query_id=%s condition_id=%s window=[%s, %s) for event=%s",
                slug, self._query_id, condition_id,
                fetch_start.isoformat(), fetch_end.isoformat(), event_id,
            )
            df = await self._fetch_trades_from_dune(
                condition_id=condition_id,
                start_ts=fetch_start,
                end_ts=fetch_end,
            )
            # Cache to parquet (next call for the same condition_id+window costs 0).
            self._save_cache(df, cache_path)
            logger.info(
                "PolymarketDuneSource: slug=%s saved cache %s (rows=%d)",
                slug, cache_path.name, len(df),
            )

        # 4) Normalize DataFrame → in-memory store.
        df_norm = self._normalize_dune_df(df)
        self._trades_by_symbol[slug] = df_norm
        if len(df_norm) > 0:
            self._warmed_symbols.add(slug)
        logger.info(
            "PolymarketDuneSource: slug=%s loaded %d trade rows (active=%s)",
            slug, len(df_norm), slug in self._warmed_symbols,
        )

    # ─────────────────────────────────────────────────────────────────
    # get_bar — 1-minute in-memory slice → BarTick.
    # ─────────────────────────────────────────────────────────────────
    def get_bar(self, symbol: str, sim_clock: datetime) -> BarTick | None:
        """Bundle trades from [sim_clock, sim_clock+60s) into a BarTick and return it.

        Args:
            symbol: Polymarket slug.
            sim_clock: Start of the 1-min bar (UTC).

        Returns:
            BarTick: when ≥1 trade exists in that minute.
            None: 0 trades (warmup not run / no trades).
        """
        df = self._trades_by_symbol.get(symbol)
        if df is None or len(df) == 0:
            return None

        bar_end = sim_clock + timedelta(minutes=1)

        # The DataFrame index is sorted by ts (UTC tz-aware) — slice is O(log n).
        try:
            slice_df = df.loc[sim_clock:bar_end]  # type: ignore[misc]
        except (KeyError, TypeError):
            mask = (df.index >= sim_clock) & (df.index < bar_end)
            slice_df = df[mask]
        else:
            # df.loc[a:b] is inclusive of b — we want b exclusive, so filter again.
            slice_df = slice_df[slice_df.index < bar_end]

        if len(slice_df) == 0:
            return None

        # Trade rows → list[dict] (ChannelReplay converts to NormalizedEvent).
        trades: list[dict[str, Any]] = []
        for ts, row in slice_df.iterrows():
            trades.append({
                "ts": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                "price": float(row["price"]),
                "size_usd": float(row["size_usd"]),
                "side": str(row.get("side", "buy")),  # 'buy' or 'sell'
                "outcome": str(row.get("outcome", "")),
                "trader": str(row.get("trader", "")),
                "tx_hash": str(row.get("tx_hash", "")),
            })

        return BarTick(
            channel=CHANNEL_POLYMARKET,
            symbol=symbol,
            ts=sim_clock,
            bar_seconds=60,
            payload={"trades": trades},
        )

    # ─────────────────────────────────────────────────────────────────
    # close — clear memory. Gamma was already closed at the end of warmup.
    # ─────────────────────────────────────────────────────────────────
    async def close(self) -> None:
        """Release memory. Idempotent."""
        self._trades_by_symbol.clear()
        self._warmed_symbols.clear()
        self._condition_id_by_slug.clear()
        # Gamma may have been re-opened inside warmup — close defensively.
        if self._gamma.is_open:
            await self._gamma.close()

    # ─────────────────────────────────────────────────────────────────
    # Public read-only — channel_replays uses this to check active symbols.
    # ─────────────────────────────────────────────────────────────────
    @property
    def warmed_symbols(self) -> frozenset[str]:
        """Slugs that actually have data after warmup()."""
        return frozenset(self._warmed_symbols)

    # ─────────────────────────────────────────────────────────────────
    # Internal — slug selection (primary only, secondary not supported)
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _slugs_for_event(event: HistoricalEvent) -> list[str]:
        """List of slugs this event should fetch from Polymarket.

        Rules:
          1) Polymarket is the primary channel → use all primary_symbols (assumed slugs).
          2) Polymarket as secondary is not supported in v0 (empty list).
             (Each Polymarket slug is a specific market, so "monitor everything"
             is meaningless.)

        Returns:
            slug list. Unlike CME, we do not auto-include every root.
        """
        if event.primary_channel == CHANNEL_POLYMARKET:
            return list(event.primary_symbols)
        return []

    # ─────────────────────────────────────────────────────────────────
    # Internal — slug → conditionId via Gamma.
    # ─────────────────────────────────────────────────────────────────
    async def _resolve_condition_id(self, slug: str) -> str | None:
        """Look up the conditionId for one slug via the Gamma API (with cache).

        Returns:
            str: conditionId (including 0x prefix).
            None: when the market does not exist or has no conditionId field.
        """
        if slug in self._condition_id_by_slug:
            return self._condition_id_by_slug[slug]

        # Replay always targets past events → the market is likely already closed.
        # Search both active+closed with include_closed=True (no impact on live code).
        try:
            market = await self._gamma.fetch_market(slug, include_closed=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PolymarketDuneSource: Gamma fetch_market(slug=%s) failed: %s",
                slug, exc,
            )
            return None

        if market is None:
            return None

        cid = market.get("conditionId")
        if not cid:
            logger.warning(
                "PolymarketDuneSource: slug=%s market exists but no conditionId field",
                slug,
            )
            return None

        # conditionId is typically 0x lowercase. The SQL uses
        # WHERE LOWER(condition_id)=LOWER(?) for safe comparison, but we also
        # cache it as lowercase.
        cid_l = str(cid).lower()
        self._condition_id_by_slug[slug] = cid_l
        logger.info(
            "PolymarketDuneSource: slug=%s → conditionId=%s",
            slug, cid_l,
        )
        return cid_l

    # ─────────────────────────────────────────────────────────────────
    # Internal — execute the Dune saved query.
    # ─────────────────────────────────────────────────────────────────
    async def _fetch_trades_from_dune(
        self,
        *,
        condition_id: str,
        start_ts: datetime,
        end_ts: datetime,
    ) -> pd.DataFrame:
        """Execute the Dune saved query and return the result as a DataFrame.

        Note:
            dune-client exposes a sync API (run_query does internal polling).
            We wrap it with asyncio.to_thread so the event loop isn't blocked.
        """
        # Lazy import (so module import works even when dune-client is missing).
        from dune_client.query import QueryBase
        from dune_client.types import QueryParameter

        # Dune datetime parameters use the 'YYYY-MM-DD HH:MM:SS' format (UTC).
        # If tz-aware, use strftime safely.
        params = [
            QueryParameter.text_type(name=_PARAM_CONDITION_ID, value=condition_id),
            QueryParameter.text_type(
                name=_PARAM_START_TS,
                value=start_ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            ),
            QueryParameter.text_type(
                name=_PARAM_END_TS,
                value=end_ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        ]

        query = QueryBase(
            name=f"polymarket-trades-{condition_id[:8]}",
            query_id=int(self._query_id),  # type: ignore[arg-type]
            params=params,
        )

        # Call the sync run_query_dataframe via to_thread (avoid blocking the event loop).
        import asyncio

        def _run_sync() -> pd.DataFrame:
            return self._dune.run_query_dataframe(
                query=query,
                performance=_DUNE_PERFORMANCE,
                ping_frequency=2,  # status check every 2 seconds
            )

        df = await asyncio.to_thread(_run_sync)
        if df is None or len(df) == 0:
            return pd.DataFrame()
        return df

    # ─────────────────────────────────────────────────────────────────
    # Internal — Dune raw DataFrame → normalized schema.
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _normalize_dune_df(df: pd.DataFrame) -> pd.DataFrame:
        """Tidy Dune SQL results into the column names we expect + index by ts.

        Expected (possible) input column candidates — these can vary depending on
        how the user wrote the SQL, so we allow several variants:
            - ts candidates:        block_time | ts | timestamp
            - price:                price (0..1)
            - size_usd candidates:  usd_amount | usd_volume | size_usd | trade_amount_usd
            - side candidates:      trade_type | side | side_raw  (BUY/SELL → buy/sell)
            - outcome candidates:   outcome | outcome_label | outcome_index
            - trader candidates:    trader | wallet | maker
            - tx_hash:              tx_hash

        Returns:
            DataFrame: index=ts (UTC tz-aware), columns=
              [condition_id (optional), price, size_usd, side, outcome, trader, tx_hash]
        """
        if len(df) == 0:
            return pd.DataFrame(
                columns=["price", "size_usd", "side", "outcome", "trader", "tx_hash"],
            )

        # ─ Find the ts column ─
        ts_col = _first_present(df, ["block_time", "ts", "timestamp"])
        if ts_col is None:
            raise ValueError(
                f"Dune result missing ts column (tried block_time/ts/timestamp). "
                f"Got columns: {list(df.columns)}"
            )

        # Convert ts to a UTC tz-aware datetime.
        ts_series = pd.to_datetime(df[ts_col], utc=True)

        # ─ price ─
        if "price" not in df.columns:
            raise ValueError(
                f"Dune result missing 'price' column. Got: {list(df.columns)}"
            )
        price = df["price"].astype(float)

        # ─ size_usd ─
        size_col = _first_present(
            df, ["usd_amount", "usd_volume", "size_usd", "trade_amount_usd"],
        )
        if size_col is not None:
            size_usd = df[size_col].astype(float)
        else:
            # Fallback: shares × price (Polymarket trade size is in shares).
            shares_col = _first_present(df, ["shares", "size", "amount"])
            if shares_col is None:
                raise ValueError(
                    f"Dune result missing both usd amount and shares column. "
                    f"Got: {list(df.columns)}"
                )
            size_usd = df[shares_col].astype(float) * price

        # ─ side: BUY/SELL from trade_type/side → buy/sell ─
        side_col = _first_present(df, ["trade_type", "side", "side_raw"])
        if side_col is not None:
            side = df[side_col].astype(str).str.lower().map(
                lambda s: "buy" if s.startswith("b") else (
                    "sell" if s.startswith("s") else "buy"  # default buy
                )
            )
        else:
            # Use outcome_index as a side proxy (1=YES → buy, 0=NO → sell).
            oi_col = _first_present(df, ["outcome_index"])
            if oi_col is not None:
                side = df[oi_col].astype(int).map({1: "buy", 0: "sell"})
            else:
                side = pd.Series(["buy"] * len(df), index=df.index)

        # ─ outcome label ─
        outcome_col = _first_present(df, ["outcome", "outcome_label"])
        if outcome_col is not None:
            outcome = df[outcome_col].astype(str)
        else:
            outcome = pd.Series([""] * len(df), index=df.index)

        # ─ trader / tx_hash (optional) ─
        trader_col = _first_present(df, ["trader", "wallet", "maker"])
        trader = (
            df[trader_col].astype(str) if trader_col else pd.Series([""] * len(df))
        )
        tx_col = _first_present(df, ["tx_hash"])
        tx_hash = df[tx_col].astype(str) if tx_col else pd.Series([""] * len(df))

        out = pd.DataFrame({
            "price": price.values,
            "size_usd": size_usd.values,
            "side": side.values,
            "outcome": outcome.values,
            "trader": trader.values,
            "tx_hash": tx_hash.values,
        }, index=ts_series.values)

        out.index.name = "ts"
        # The index must be sorted for .loc[a:b] slicing to be fast (safe even if
        # the Dune SQL already ORDER BYs).
        out = out.sort_index()
        return out

    # ─────────────────────────────────────────────────────────────────
    # Internal — parquet cache helpers.
    # ─────────────────────────────────────────────────────────────────
    def _cache_path(
        self,
        condition_id: str,
        start_ts: datetime,
        end_ts: datetime,
    ) -> Path:
        """Cache file path. Unique by condition_id+window."""
        # condition_id is usually 0x... ~64 chars — fine as a filename (well under FS limits).
        s = start_ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
        e = end_ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return self._cache_dir / f"{condition_id}_{s}_{e}.parquet"

    @staticmethod
    def _save_cache(df: pd.DataFrame, path: Path) -> None:
        """Save as parquet. Save even if df is empty (column header only) — prevents re-fetch."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # pyarrow handles datetime64[ns, UTC] naturally.
        df.to_parquet(path, engine="pyarrow", index=False)

    def __repr__(self) -> str:
        return (
            f"<PolymarketDuneSource warmed={sorted(self._warmed_symbols)} "
            f"rows_total={sum(len(df) for df in self._trades_by_symbol.values())}>"
        )


# ─────────────────────────────────────────────────────────────────────
# Module helper
# ─────────────────────────────────────────────────────────────────────
def _first_present(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """Return the first column name from `candidates` that exists in df. None if none match."""
    for name in candidates:
        if name in df.columns:
            return name
    return None


__all__ = ["PolymarketDuneSource", "_build_default_dune_client"]
