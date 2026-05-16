"""
cme/databento_client.py — Databento Historical API client + cost guard.

────────────────────────────────────────────────────────────────────────
Responsibilities (P9.3.P0.A2):

  After a TradingView webhook fires as the primary trigger (5~15s latency, ~free),
  we fetch raw trade data from Databento for a ±10~15 minute window around the
  event for precise verification:

    · D2 (VPIN) — aggressor side distribution → informed trading probability
    · D4 (cross-asset) — is there a spike on another root at the same time?
    · After-the-fact verification of "did the price actually move?"

  This fetch is PAYG (pay-as-you-go), so wrong calls can blow up the monthly bill.
  → Two layers of protection:

    Layer 1: CostTracker  — tracks monthly cumulative spend; blocks fetches at the $40 cap.
    Layer 2: Local cache  — hits disk for repeat (root, start, end) calls (option C).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Cache location: `data/databento/cache/<dataset>/<root>__<start>__<end>__<schema>.parquet`
    P9.3 decision option C: local disk (works in Cloud Run's /tmp as well).
    Cache is lost on Cloud Run container restart — consider GCS migration after measurement.

  · Estimate before fetch: get_cost() pre-computes a USD estimate → raise BEFORE
    fetching when projected to exceed the cap. This prevents accidents like
    "fetched anyway and got billed past the cap".

  · All fetches use the sync API → wrapped in asyncio.to_thread() so the daemon
    event loop is not blocked.

  · CostTracker is persisted to a JSON file (`data/databento/cost_tracker.json`).
    Cumulative spend for the current month survives process restart / Cloud Run redeploy.
    (Caveat: /tmp would lose it like the cache → we keep it under ANOMALY_DATA_PATH —
    the daemon already places ANOMALY_DATA_PATH alongside the persistent stores.)

  · Symbol format: we only carry the root ("CL"). For Databento we translate
    to continuous front-month → "CL.c.0". stype_in="continuous".

  · The mapping between our `symbols` (CME root) ↔ Databento continuous symbol
    lives in one place (CME_TO_CONTINUOUS). When adding a new root, update here
    + watchlist.yaml + tradingview_adapter simultaneously.

────────────────────────────────────────────────────────────────────────
Env vars:
  DATABENTO_API_KEY        — Databento Historical API key (already in .env)
  ANOMALY_DATA_PATH        — base path for cache and cost_tracker.json

D7 (LOCKED): historical-only. EVT-1 may enable stream_live (kill-switch required).
"""

# ── stdlib ─────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import json
import logging
import threading                              # file-write lock for CostTracker
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── 3rd-party ──────────────────────────────────────────────────────────
import databento as db                        # Historical API + DBNStore
import pandas as pd                           # to_df result type

logger = logging.getLogger(__name__)


# =====================================================================
# Constants — root symbol → Databento continuous symbol mapping
# =====================================================================
# Databento's continuous symbology: "{root}.c.{rank}"
#   rank=0  → front month (closest expiry)
#   rank=1  → next month
# In P9.3 we only fetch front-month (D7 cost saving).
CME_TO_CONTINUOUS: dict[str, str] = {
    "CL": "CL.c.0",       # WTI front month
    "BZ": "BZ.c.0",       # Brent (CME) front month
    "ES": "ES.c.0",       # E-mini S&P front month
    "GC": "GC.c.0",       # Gold front month
}

# Dataset we use. CME Globex MDP3 = market data feed.
# The trades schema includes the 'side' (aggressor) column — core to D2 (VPIN).
DEFAULT_DATASET: str = "GLBX.MDP3"
DEFAULT_SCHEMA: str = "trades"

# Conservative estimate (per fetch) used when the cost-estimate API fails.
# Measured: 1 hour, 1 symbol trades ~ $0.10~$0.50, so $1.00 is a generous safety margin.
CONSERVATIVE_ESTIMATE_USD: float = 1.00


# =====================================================================
# Exceptions — catchable so callers can degrade gracefully
# =====================================================================
class DatabentoCostCapExceeded(RuntimeError):
    """Monthly cost cap reached — raised when the requested fetch is denied."""


class DatabentoUnknownSymbol(ValueError):
    """root not in CME_TO_CONTINUOUS — need to add to the watchlist / mapping."""


# =====================================================================
# CostTracker — cumulative monthly spend (persistent)
# =====================================================================
@dataclass
class _MonthState:
    """One month of cumulative state — dataclass for easy JSON serialize/restore."""
    month_key: str           # "YYYY-MM" (UTC based)
    total_usd: float         # cumulative spend
    n_fetches: int           # cumulative fetch count
    n_cache_hits: int        # cache hit count (cost 0)


class CostTracker:
    """Tracks cumulative monthly spend. Persisted to a JSON file.

    Thread-safe (locks on file write).
    """

    def __init__(self, *, state_file: Path, monthly_cap_usd: float = 40.0) -> None:
        """
        Args:
            state_file: JSON path to persist cumulative state
                        (e.g. data/databento/cost_tracker.json).
            monthly_cap_usd: monthly cap. ValueError if ≤ 0.
        """
        if monthly_cap_usd <= 0:
            raise ValueError(f"monthly_cap_usd must be > 0, got {monthly_cap_usd}")

        self._state_file = state_file
        self._cap = float(monthly_cap_usd)
        self._lock = threading.Lock()         # prevent file-write races

        # Create parent dir if missing
        state_file.parent.mkdir(parents=True, exist_ok=True)

        # Load existing state — reset to fresh if missing or corrupt
        self._state = self._load_or_init()

    # ─────────────────────────────────────────────────────────────────
    # Public properties
    # ─────────────────────────────────────────────────────────────────
    @property
    def monthly_cap_usd(self) -> float:
        """Monthly cap (USD) — immutable."""
        return self._cap

    @property
    def current_month_spent_usd(self) -> float:
        """Cumulative spend this month. Automatically shows 0 on month rollover."""
        with self._lock:
            self._maybe_rollover()
            return self._state.total_usd

    @property
    def current_month_remaining_usd(self) -> float:
        """Budget remaining this month (cap - spent); 0 if negative."""
        return max(0.0, self._cap - self.current_month_spent_usd)

    def snapshot(self) -> dict[str, Any]:
        """For monitoring — exposed via the daemon's /snapshot."""
        with self._lock:
            self._maybe_rollover()
            return {
                "month_key": self._state.month_key,
                "total_usd": round(self._state.total_usd, 4),
                "cap_usd": self._cap,
                "remaining_usd": round(max(0.0, self._cap - self._state.total_usd), 4),
                "pct_used": round(self._state.total_usd / self._cap * 100.0, 2),
                "n_fetches": self._state.n_fetches,
                "n_cache_hits": self._state.n_cache_hits,
            }

    # ─────────────────────────────────────────────────────────────────
    # Public actions
    # ─────────────────────────────────────────────────────────────────
    def would_exceed_cap(self, estimate_usd: float) -> bool:
        """Predict whether the sum of estimate + current cumulative exceeds the cap."""
        return (self.current_month_spent_usd + max(0.0, estimate_usd)) > self._cap

    def record_fetch(self, cost_usd: float) -> None:
        """Called after a real fetch — increments total + count + flushes to disk."""
        with self._lock:
            self._maybe_rollover()
            self._state.total_usd += max(0.0, float(cost_usd))
            self._state.n_fetches += 1
            self._save()

    def record_cache_hit(self) -> None:
        """One cache hit — cost 0, count only."""
        with self._lock:
            self._maybe_rollover()
            self._state.n_cache_hits += 1
            self._save()

    # ─────────────────────────────────────────────────────────────────
    # Internal — month rollover / file IO
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _now_month_key() -> str:
        """Current UTC 'YYYY-MM' string."""
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _maybe_rollover(self) -> None:
        """Reset cumulative to 0 when the month changes (caller assumed to hold the lock)."""
        cur = self._now_month_key()
        if self._state.month_key != cur:
            logger.info(
                "CostTracker: month rollover %s -> %s "
                "(prev_spent=$%.4f, prev_fetches=%d)",
                self._state.month_key, cur,
                self._state.total_usd, self._state.n_fetches,
            )
            self._state = _MonthState(
                month_key=cur, total_usd=0.0, n_fetches=0, n_cache_hits=0,
            )
            self._save()

    def _load_or_init(self) -> _MonthState:
        """Load JSON — start from 0 if missing or invalid."""
        cur = self._now_month_key()
        if not self._state_file.exists():
            return _MonthState(month_key=cur, total_usd=0.0, n_fetches=0, n_cache_hits=0)
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            loaded = _MonthState(
                month_key=str(data.get("month_key", cur)),
                total_usd=float(data.get("total_usd", 0.0)),
                n_fetches=int(data.get("n_fetches", 0)),
                n_cache_hits=int(data.get("n_cache_hits", 0)),
            )
            # If the stored month is in the past, rollover triggers immediately (handled by _maybe_rollover on next record call)
            return loaded
        except (json.JSONDecodeError, ValueError, OSError) as e:
            logger.warning(
                "CostTracker: failed to load %s, starting fresh: %s",
                self._state_file, e,
            )
            return _MonthState(month_key=cur, total_usd=0.0, n_fetches=0, n_cache_hits=0)

    def _save(self) -> None:
        """Atomic JSON save (caller assumed to hold the lock).

        atomic = write to a temp file then rename — prevents corruption from partial writes.
        """
        tmp = self._state_file.with_suffix(".json.tmp")
        payload = {
            "month_key": self._state.month_key,
            "total_usd": self._state.total_usd,
            "n_fetches": self._state.n_fetches,
            "n_cache_hits": self._state.n_cache_hits,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._state_file)


# =====================================================================
# DatabentoClient — fetch + cache + cost guard
# =====================================================================
class DatabentoClient:
    """Wrapper around the Databento Historical API.

    Public API (all async — daemon event-loop friendly):
      · fetch_historical_range(root, start, end) -> pd.DataFrame
      · estimate_cost_usd(root, start, end) -> float
    """

    def __init__(
        self,
        *,
        api_key: str,
        cost_tracker: CostTracker,
        cache_dir: Path,
        dataset: str = DEFAULT_DATASET,
        schema: str = DEFAULT_SCHEMA,
    ) -> None:
        """
        Args:
            api_key: Databento API key (typically starts with 'db-').
            cost_tracker: monthly-spend cap guard — every fetch must pass through it.
            cache_dir: parquet cache base folder.
                       Actual files are saved at cache_dir / dataset / "{key}.parquet".
            dataset:  default is "GLBX.MDP3" (CME Globex MDP3).
            schema:   "trades" (includes the 'side' aggressor column needed for D2 VPIN).
        """
        if not api_key or not api_key.startswith("db-"):
            raise ValueError(
                "DatabentoClient: api_key must start with 'db-' "
                "(set DATABENTO_API_KEY env var)"
            )
        self._api_key = api_key
        self._cost_tracker = cost_tracker
        self._dataset = dataset
        self._schema = schema

        # Databento Historical SDK client — sync API
        self._hist = db.Historical(key=api_key)

        # Cache folder — sub-dir per dataset
        self._cache_dir = cache_dir / dataset
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────
    async def estimate_cost_usd(
        self, root: str, start: datetime, end: datetime,
    ) -> float:
        """Pre-fetch USD estimate. Returns a conservative estimate on failure.

        Args:
            root: our root symbol (e.g. "CL").
            start, end: UTC datetimes — assumed UTC if naive.

        Returns:
            float: expected USD cost. Returns CONSERVATIVE_ESTIMATE_USD on failure.
        """
        symbol = self._root_to_continuous(root)
        try:
            # The Databento SDK is sync — wrap in to_thread to avoid blocking the event loop
            cost = await asyncio.to_thread(
                self._hist.metadata.get_cost,
                dataset=self._dataset,
                start=start,
                end=end,
                symbols=symbol,
                schema=self._schema,
                stype_in="continuous",
            )
            return float(cost)
        except Exception as e:
            # Estimate failure is not fetch failure — be conservative and proceed
            logger.warning(
                "DatabentoClient.estimate_cost_usd failed (root=%s): %s. "
                "Using conservative $%.2f.",
                root, e, CONSERVATIVE_ESTIMATE_USD,
            )
            return CONSERVATIVE_ESTIMATE_USD

    async def fetch_historical_range(
        self, root: str, start: datetime, end: datetime,
    ) -> pd.DataFrame:
        """Trade ticks in the given window as a DataFrame.

        Flow:
            1) Cache hit → return immediately (cost 0)
            2) Miss → estimate_cost_usd → cap check
            3) Cap OK → get_range fetch → DataFrame → save parquet
            4) cost_tracker.record_fetch()

        Args:
            root: one of "CL" / "BZ" / "ES" / "GC".
            start, end: UTC datetimes.

        Returns:
            pd.DataFrame: trades schema columns (price, size, side, symbol, ...).
                          index is ts_recv (UTC).

        Raises:
            DatabentoUnknownSymbol: root not in CME_TO_CONTINUOUS.
            DatabentoCostCapExceeded: estimate exceeds monthly cap → fetch denied.
            ValueError: argument errors such as start >= end.
        """
        if start >= end:
            raise ValueError(f"start ({start}) must be < end ({end})")

        symbol = self._root_to_continuous(root)

        # ─────────────────────────────────────────────────────────
        # 1) Cache lookup
        # ─────────────────────────────────────────────────────────
        cache_path = self._cache_path(root, start, end)
        if cache_path.exists():
            try:
                df = pd.read_parquet(cache_path)
                self._cost_tracker.record_cache_hit()
                logger.info(
                    "DatabentoClient: CACHE HIT root=%s window=[%s, %s] "
                    "rows=%d path=%s",
                    root, start.isoformat(), end.isoformat(),
                    len(df), cache_path.name,
                )
                return df
            except Exception as e:
                # If the cache file is corrupt — delete and fresh-fetch
                logger.warning(
                    "DatabentoClient: cache file corrupt (%s), refetching: %s",
                    cache_path, e,
                )
                try:
                    cache_path.unlink()
                except OSError:
                    pass

        # ─────────────────────────────────────────────────────────
        # 2) Cost estimate + cap check (BEFORE fetch — accident prevention)
        # ─────────────────────────────────────────────────────────
        estimate = await self.estimate_cost_usd(root, start, end)
        if self._cost_tracker.would_exceed_cap(estimate):
            current = self._cost_tracker.current_month_spent_usd
            cap = self._cost_tracker.monthly_cap_usd
            msg = (
                f"Databento cost cap exceeded — "
                f"estimate=${estimate:.4f}, this_month=${current:.4f}, cap=${cap:.2f}"
            )
            logger.error("DatabentoClient: %s (root=%s)", msg, root)
            raise DatabentoCostCapExceeded(msg)

        # ─────────────────────────────────────────────────────────
        # 3) Fetch — wrap the sync SDK in to_thread to make it async
        # ─────────────────────────────────────────────────────────
        logger.info(
            "DatabentoClient: fetching root=%s symbol=%s window=[%s, %s] "
            "estimate=$%.4f",
            root, symbol, start.isoformat(), end.isoformat(), estimate,
        )

        store = await asyncio.to_thread(
            self._hist.timeseries.get_range,
            dataset=self._dataset,
            start=start,
            end=end,
            symbols=symbol,
            schema=self._schema,
            stype_in="continuous",
        )

        # DBNStore → DataFrame (decompress + parse)
        df = await asyncio.to_thread(store.to_df)

        # ─────────────────────────────────────────────────────────
        # 4) Cache save + cost record
        # ─────────────────────────────────────────────────────────
        # Cache even empty results — so a repeat request for the same empty window does not get billed again.
        try:
            await asyncio.to_thread(df.to_parquet, cache_path, index=True)
        except Exception as e:
            logger.warning(
                "DatabentoClient: cache save failed (%s): %s — "
                "fetch succeeded; next call will re-fetch.",
                cache_path, e,
            )

        # The actual cost may differ from the estimate (Databento doesn't return cost in the response body).
        # → Use the estimate (conservative). Exact measurement is available on the Databento portal.
        self._cost_tracker.record_fetch(estimate)

        logger.info(
            "DatabentoClient: FETCHED root=%s rows=%d cost~$%.4f "
            "month_total=$%.4f / $%.2f",
            root, len(df), estimate,
            self._cost_tracker.current_month_spent_usd,
            self._cost_tracker.monthly_cap_usd,
        )

        return df

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    def _root_to_continuous(self, root: str) -> str:
        """Our root → Databento continuous symbol. Raises on unknown."""
        if root not in CME_TO_CONTINUOUS:
            raise DatabentoUnknownSymbol(
                f"unknown root {root!r} — "
                f"add to CME_TO_CONTINUOUS (allowed: {sorted(CME_TO_CONTINUOUS.keys())})"
            )
        return CME_TO_CONTINUOUS[root]

    def _cache_path(self, root: str, start: datetime, end: datetime) -> Path:
        """Stable cache file name. Strips OS-unfriendly characters like ':'.

        Format: {root}__{startZ}__{endZ}__{schema}.parquet
                e.g.: CL__20260417T120000Z__20260417T130000Z__trades.parquet
        """
        # Drop ':' and use 'YYYYMMDDTHHMMSSZ' format
        s = start.strftime("%Y%m%dT%H%M%SZ")
        e = end.strftime("%Y%m%dT%H%M%SZ")
        return self._cache_dir / f"{root}__{s}__{e}__{self._schema}.parquet"
