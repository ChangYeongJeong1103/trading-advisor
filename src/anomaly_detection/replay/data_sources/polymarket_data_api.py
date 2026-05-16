"""
data_sources/polymarket_data_api.py — Polymarket historical trades via public Data API.

────────────────────────────────────────────────────────────────────────
Role:
  Historical trade fetcher for Replay's Polymarket channel.
  Backend = **Polymarket public Data API** (https://data-api.polymarket.com).

────────────────────────────────────────────────────────────────────────
Why use this instead of Dune:

  · Dune's Free plan **blocks API execute** (only available from Plus $399/month).
    Concretely: /api/v1/query/{id}/execute returns 400 for every performance tier.
    "Invalid performance tier" — small is for manual UI only, medium/large are paid.

  · The Polymarket native Data API:
      - Public (no API key required, generous rate limit)
      - Paginates 1000 trades at a time (offset-based)
      - Sorted newest-first — we paginate until each page's oldest ts goes
        below fetch_start
      - Server-side time filter is not supported → filter client-side

  · Result: zero cost, zero extra env vars, simpler implementation than Dune.

────────────────────────────────────────────────────────────────────────
Flow (same interface as PolymarketDuneSource):

  1) warmup(event):
       · primary_channel != polymarket → no-op.
       · For each slug:
           a) Gamma → look up conditionId (include_closed=True — past events'
              markets are already resolved).
           b) On cache parquet hit, load immediately.
           c) On miss, paginate the Data API by condition_id.
           d) Normalize DataFrame → self._trades_by_symbol[slug].

  2) get_bar(symbol, sim_clock):
       · Return a BarTick sliced to [sim_clock, sim_clock+60s).

────────────────────────────────────────────────────────────────────────
References:
  · src/anomaly/channels/polymarket/collector.py — Gamma fetch_market reuse.
  · docs/p10-replay-framework.md §3.2            — HistoricalDataSource Protocol.
  · The earlier attempt (Dune backend) is preserved in polymarket_dune.py for
    paid-plan users. v0.1 routes the CLI through this file.
"""

from __future__ import annotations

# --- standard library ---
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

# --- third-party ---
import httpx
import pandas as pd

# --- local: production polymarket layer (Gamma) ---
from ...channels.polymarket.collector import PolymarketCollector
from ...core.schemas import CHANNEL_POLYMARKET

# --- local: replay schemas ---
from ..schemas import BarTick, HistoricalEvent

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Module constants
# ─────────────────────────────────────────────────────────────────────
# repo root: 4 levels up (data_sources → replay → anomaly → src → repo)
_REPO_ROOT: Path = Path(__file__).resolve().parents[4]
_DEFAULT_CACHE_DIR: Path = _REPO_ROOT / "data" / "anomaly" / "replay_cache" / "polymarket"

# Polymarket public Data API.
_DATA_API_BASE: str = "https://data-api.polymarket.com"

# Max page size (server cap confirmed).
_PAGE_SIZE: int = 1000

# httpx timeout — the Polymarket data API is typically fast, but keep a safety buffer.
_HTTP_TIMEOUT_S: float = 30.0

# Pagination safety: cap so we don't fetch too many pages.
# If 1M trades exist over 1 week → 1000 pages = too many. Even a market like
# Maduro has ~50K over 7 days, so 50 pages is plenty. We use 200
# (~200ms per page ≈ 40s worst case).
_MAX_PAGES: int = 200


# ─────────────────────────────────────────────────────────────────────
# PolymarketDataApiSource — HistoricalDataSource impl (public Data API).
# ─────────────────────────────────────────────────────────────────────
class PolymarketDataApiSource:
    """Polymarket trades 1-min bar fetcher (Polymarket Data API).

    Usage:
        source = PolymarketDataApiSource()
        await source.warmup(event)
        bar = source.get_bar("market-slug", sim_clock)
        await source.close()

    Args:
        gamma_collector: Direct injection (for tests). If None, a new one is created.
        cache_dir: parquet cache dir. If None, _DEFAULT_CACHE_DIR.
    """

    channel: ClassVar[str] = CHANNEL_POLYMARKET

    def __init__(
        self,
        *,
        gamma_collector: PolymarketCollector | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._gamma = gamma_collector or PolymarketCollector()

        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # symbol(slug) → DataFrame mapping. Populated by warmup().
        # DataFrame index = ts (UTC tz-aware), columns: condition_id, price,
        # size_usd, side, outcome, trader.
        self._trades_by_symbol: dict[str, pd.DataFrame] = {}
        self._warmed_symbols: set[str] = set()
        self._condition_id_by_slug: dict[str, str] = {}

        # Data API HTTP client — keep-alive across pagination during warmup.
        self._http: httpx.AsyncClient | None = None

    # ─────────────────────────────────────────────────────────────────
    # supports
    # ─────────────────────────────────────────────────────────────────
    def supports(self, event: HistoricalEvent) -> bool:
        """True if this event has at least one Polymarket slug to fetch."""
        return len(self._slugs_for_event(event)) > 0

    # ─────────────────────────────────────────────────────────────────
    # warmup
    # ─────────────────────────────────────────────────────────────────
    async def warmup(self, event: HistoricalEvent) -> None:
        """Fetch the event window's trades once per slug."""
        slugs = self._slugs_for_event(event)
        if not slugs:
            logger.info(
                "PolymarketDataApiSource: no Polymarket slugs in event=%s "
                "(primary_channel=%s) — skipping warmup",
                event.event_id, event.primary_channel,
            )
            return

        # Buffer both ends of the window (include the trailing minute).
        fetch_start = event.window_start
        fetch_end = event.window_end + timedelta(minutes=1)

        # Open the Gamma session and the Data API HTTP client.
        await self._gamma.open()
        self._http = httpx.AsyncClient(
            base_url=_DATA_API_BASE,
            timeout=_HTTP_TIMEOUT_S,
        )

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
                    # One slug failing doesn't stop other slugs.
                    logger.warning(
                        "PolymarketDataApiSource: slug=%s warmup failed "
                        "(event=%s): %s",
                        slug, event.event_id, exc,
                    )
        finally:
            await self._gamma.close()
            await self._http.aclose()
            self._http = None

    async def _warmup_one_slug(
        self,
        *,
        slug: str,
        event_id: str,
        fetch_start: datetime,
        fetch_end: datetime,
    ) -> None:
        """For one slug: resolve conditionId + check cache + (if needed) Data API fetch."""
        # 1) slug → conditionId.
        condition_id = await self._resolve_condition_id(slug)
        if condition_id is None:
            logger.warning(
                "PolymarketDataApiSource: slug=%s → conditionId not found "
                "via Gamma (market may not exist). event=%s",
                slug, event_id,
            )
            return

        # 2) cache lookup.
        cache_path = self._cache_path(condition_id, fetch_start, fetch_end)
        if cache_path.exists():
            logger.info(
                "PolymarketDataApiSource: slug=%s cache HIT %s",
                slug, cache_path.name,
            )
            df = pd.read_parquet(cache_path)
        else:
            # 3) Data API fetch (paginated).
            logger.info(
                "PolymarketDataApiSource: slug=%s cache MISS — Data API fetch "
                "condition_id=%s window=[%s, %s) for event=%s",
                slug, condition_id,
                fetch_start.isoformat(), fetch_end.isoformat(), event_id,
            )
            df = await self._fetch_trades_paginated(
                condition_id=condition_id,
                fetch_start=fetch_start,
                fetch_end=fetch_end,
            )
            self._save_cache(df, cache_path)
            logger.info(
                "PolymarketDataApiSource: slug=%s saved cache %s (rows=%d)",
                slug, cache_path.name, len(df),
            )

        # 4) normalize → in-memory store.
        df_norm = self._normalize_df(df)
        self._trades_by_symbol[slug] = df_norm
        if len(df_norm) > 0:
            self._warmed_symbols.add(slug)
        logger.info(
            "PolymarketDataApiSource: slug=%s loaded %d trade rows (active=%s)",
            slug, len(df_norm), slug in self._warmed_symbols,
        )

    # ─────────────────────────────────────────────────────────────────
    # get_bar
    # ─────────────────────────────────────────────────────────────────
    def get_bar(self, symbol: str, sim_clock: datetime) -> BarTick | None:
        """Return a BarTick of trades in [sim_clock, sim_clock+60s)."""
        df = self._trades_by_symbol.get(symbol)
        if df is None or len(df) == 0:
            return None

        bar_end = sim_clock + timedelta(minutes=1)

        try:
            slice_df = df.loc[sim_clock:bar_end]  # type: ignore[misc]
        except (KeyError, TypeError):
            mask = (df.index >= sim_clock) & (df.index < bar_end)
            slice_df = df[mask]
        else:
            # df.loc[a:b] is inclusive of b — we need exclusive, so filter again.
            slice_df = slice_df[slice_df.index < bar_end]

        if len(slice_df) == 0:
            return None

        trades: list[dict[str, Any]] = []
        for ts, row in slice_df.iterrows():
            trades.append({
                "ts": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                "price": float(row["price"]),
                "size_usd": float(row["size_usd"]),
                "side": str(row.get("side", "buy")),
                "outcome": str(row.get("outcome", "")),
                "trader": str(row.get("trader", "")),
            })

        return BarTick(
            channel=CHANNEL_POLYMARKET,
            symbol=symbol,
            ts=sim_clock,
            bar_seconds=60,
            payload={"trades": trades},
        )

    # ─────────────────────────────────────────────────────────────────
    # close
    # ─────────────────────────────────────────────────────────────────
    async def close(self) -> None:
        self._trades_by_symbol.clear()
        self._warmed_symbols.clear()
        self._condition_id_by_slug.clear()
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._gamma.is_open:
            await self._gamma.close()

    @property
    def warmed_symbols(self) -> frozenset[str]:
        return frozenset(self._warmed_symbols)

    # ─────────────────────────────────────────────────────────────────
    # Internal — slug selection
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _slugs_for_event(event: HistoricalEvent) -> list[str]:
        """Only use primary_symbols when primary_channel == polymarket."""
        if event.primary_channel == CHANNEL_POLYMARKET:
            return list(event.primary_symbols)
        return []

    # ─────────────────────────────────────────────────────────────────
    # Internal — slug → conditionId via Gamma
    # ─────────────────────────────────────────────────────────────────
    async def _resolve_condition_id(self, slug: str) -> str | None:
        """One slug → conditionId. Closed markets are allowed (replay is for past events)."""
        if slug in self._condition_id_by_slug:
            return self._condition_id_by_slug[slug]

        try:
            market = await self._gamma.fetch_market(slug, include_closed=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PolymarketDataApiSource: Gamma fetch_market(slug=%s) failed: %s",
                slug, exc,
            )
            return None

        if market is None:
            return None

        cid = market.get("conditionId")
        if not cid:
            logger.warning(
                "PolymarketDataApiSource: slug=%s market exists but no "
                "conditionId field",
                slug,
            )
            return None

        cid_l = str(cid).lower()
        self._condition_id_by_slug[slug] = cid_l
        logger.info(
            "PolymarketDataApiSource: slug=%s → conditionId=%s",
            slug, cid_l,
        )
        return cid_l

    # ─────────────────────────────────────────────────────────────────
    # Internal — Polymarket Data API pagination
    # ─────────────────────────────────────────────────────────────────
    async def _fetch_trades_paginated(
        self,
        *,
        condition_id: str,
        fetch_start: datetime,
        fetch_end: datetime,
    ) -> pd.DataFrame:
        """Paginate the Data API's GET /trades by offset.

        API response:
            list[dict] — newest first.
            Each trade: timestamp(int sec), price(float 0..1), size(shares),
            side("BUY"/"SELL"), proxyWallet, conditionId, outcome, etc.

        Strategy:
            Start from page 0 → stop once the oldest trade ts in a page is
            **before** fetch_start. After that filter client-side to
            [fetch_start, fetch_end).

            Edge case: market started trading before fetch_start → the last
            page's oldest_ts is before fetch_start → break.
            Conversely, market remained active after fetch_end → the first
            trade of a page is after fetch_end → those trades are dropped by
            the client filter.
        """
        assert self._http is not None  # only called from inside warmup.

        start_unix = int(fetch_start.timestamp())
        end_unix = int(fetch_end.timestamp())

        rows: list[dict[str, Any]] = []
        # Known Polymarket Data API constraint: offsets beyond a certain value
        # (~4000 observed) return 400 Bad Request. The exact cap is undocumented.
        # On 400 we do not throw — we break and proceed with what we have so far
        # (i.e. the most recent N trades).
        # P10.4: needed to handle the high-volume Iran market ($89M).
        truncated_by_api = False
        for page in range(_MAX_PAGES):
            offset = page * _PAGE_SIZE
            try:
                r = await self._http.get(
                    "/trades",
                    params={
                        "market": condition_id,
                        "limit": _PAGE_SIZE,
                        "offset": offset,
                    },
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 400:
                    # API rejected the offset — proceed with partial data.
                    logger.warning(
                        "PolymarketDataApiSource: API offset cap reached "
                        "at page=%d offset=%d (cid=%s) — proceeding with "
                        "%d trades collected so far.",
                        page, offset, condition_id, len(rows),
                    )
                    truncated_by_api = True
                    break
                raise

            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break

            rows.extend(batch)

            # newest-first → the last row of a batch is the oldest.
            oldest_ts = int(batch[-1].get("timestamp", 0))
            logger.debug(
                "PolymarketDataApiSource: page=%d offset=%d batch_rows=%d "
                "oldest_ts=%d (need >= %d)",
                page, offset, len(batch), oldest_ts, start_unix,
            )

            if oldest_ts <= start_unix:
                # Reached before the window start → stop.
                break

            if len(batch) < _PAGE_SIZE:
                break

            await asyncio.sleep(0.2)
        else:
            logger.warning(
                "PolymarketDataApiSource: _MAX_PAGES (%d) hit for condition_id=%s "
                "— window may be incomplete (start-ts coverage may be insufficient).",
                _MAX_PAGES, condition_id,
            )

        if truncated_by_api and rows:
            # If the oldest ts in the partial data is before fetch_start, OK;
            # otherwise the oldest trade is still inside our window → the front
            # of the window is missing (= warmup baseline may be insufficient).
            # Warn only.
            oldest_collected = int(rows[-1].get("timestamp", 0))
            if oldest_collected > start_unix:
                hours_short = (oldest_collected - start_unix) / 3600.0
                logger.warning(
                    "PolymarketDataApiSource: API truncation — oldest trade "
                    "in collected data is %.1f h after window start. "
                    "Pre-event baseline may be incomplete.",
                    hours_short,
                )

        if not rows:
            return pd.DataFrame()

        # client-side window filter.
        df = pd.DataFrame(rows)
        df["timestamp"] = df["timestamp"].astype(int)
        df = df[(df["timestamp"] >= start_unix) & (df["timestamp"] < end_unix)]
        # Sort newest-first → oldest-first (downstream assumption).
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    # ─────────────────────────────────────────────────────────────────
    # Internal — DataFrame normalize (data API columns → our standard)
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
        """Convert the Data API response into the columns/index the detector expects.

        Input columns (raw data API): timestamp(int sec), price(float),
            size(shares), side("BUY"/"SELL"), proxyWallet, outcome,
            conditionId, ...

        Output columns:
            index    : ts (UTC tz-aware datetime)
            price    : float 0..1
            size_usd : float (= shares × price, prediction-market definition)
            side     : "buy" or "sell" (lowercase)
            outcome  : str (e.g. "Yes")
            trader   : str (proxy wallet address)
            condition_id : str
        """
        if len(df) == 0:
            return pd.DataFrame()

        # ts → UTC datetime index.
        ts = pd.to_datetime(df["timestamp"], unit="s", utc=True)

        out = pd.DataFrame({
            "price": df["price"].astype(float),
            "size_usd": (df["size"].astype(float) * df["price"].astype(float)),
            "side": df["side"].astype(str).str.lower(),
            "outcome": df.get("outcome", pd.Series(dtype=str)).astype(str),
            "trader": df.get("proxyWallet", pd.Series(dtype=str)).astype(str),
            "condition_id": df.get("conditionId", pd.Series(dtype=str)).astype(str),
        })
        out.index = ts
        out = out.sort_index()
        return out

    # ─────────────────────────────────────────────────────────────────
    # Internal — parquet cache I/O
    # ─────────────────────────────────────────────────────────────────
    def _cache_path(
        self,
        condition_id: str,
        fetch_start: datetime,
        fetch_end: datetime,
    ) -> Path:
        """Cache file path. Unique key by condition_id + window."""
        cid_short = condition_id.lstrip("0x")[:16]  # short.
        s = fetch_start.strftime("%Y%m%dT%H%M%SZ")
        e = fetch_end.strftime("%Y%m%dT%H%M%SZ")
        return self._cache_dir / f"{cid_short}__{s}__{e}__trades.parquet"

    @staticmethod
    def _save_cache(df: pd.DataFrame, path: Path) -> None:
        """parquet write — atomic (.tmp → rename)."""
        if len(df) == 0:
            # Cache the 0-row case too, so the next call doesn't re-fetch (placeholder schema).
            df = pd.DataFrame(columns=[
                "timestamp", "price", "size", "side",
                "proxyWallet", "outcome", "conditionId",
            ])
        tmp = path.with_suffix(path.suffix + ".tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(path)
