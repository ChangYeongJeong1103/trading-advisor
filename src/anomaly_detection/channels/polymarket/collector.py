"""
polymarket/collector.py — Polymarket REST API thin client (Gamma + Data).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §2.1, §6.6):
  Only responsible for talking to Polymarket's two public REST endpoints.
  - Gamma API   (https://gamma-api.polymarket.com/)
      market metadata: slug → {conditionId, lastTradePrice, bestBid/Ask,
      spread, volume24hr, active, closed, outcomes, ...}
  - Data API    (https://data-api.polymarket.com/)
      recent trades: conditionId → [{wallet, side, size, price, timestamp, ...}]

  v1 P2 is REST polling first (architecture §3 walking-skeleton). WebSocket
  upgrade arrives in P9 deep-dive. REST alone with 5~10s polling comfortably
  meets the v1 latency goal (RISK_OFF P95 < 60s).

  Manages only lifecycle (open / close). Polling loop / signal emit etc. are channel.py's job.

────────────────────────────────────────────────────────────────────────
Dependencies:
  - httpx               : async HTTP client (already in requirements-anomaly.txt)
  - tenacity            : exponential backoff + retry (D4)
  - No external API auth required (public read-only)

────────────────────────────────────────────────────────────────────────
Architecture: §2.1 Component Responsibility, §6.6 Failure Mode (retry/backoff)
Plan: §3.1 Goal #1 (Polymarket channel)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


# Polymarket public endpoints (no auth)
GAMMA_BASE_URL: str = "https://gamma-api.polymarket.com"
DATA_BASE_URL: str = "https://data-api.polymarket.com"

# Exceptions tenacity retries on — network / 5xx / timeout only. 4xx (bad slug etc.) fails immediately.
_RETRIABLE_EXC = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class PolymarketCollector:
    """Polymarket REST API thin client.

    One instance is shared inside a channel (single httpx.AsyncClient).
    Channel calls open() once on start, close() once on shutdown.
    """

    def __init__(
        self,
        *,
        http_timeout_s: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        """
        Args:
            http_timeout_s: per-HTTP-call timeout (connect+read).
            max_retries: retry attempts on transient errors (exponential backoff).
        """
        self._timeout = http_timeout_s
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    async def open(self) -> None:
        """Open httpx client. Idempotent."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                # Cap concurrent connections — protect Polymarket rate limits
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                headers={"User-Agent": "anomaly-daemon/0.1 (+polymarket-channel)"},
            )
            logger.info("PolymarketCollector: opened HTTP client (timeout=%.1fs)",
                        self._timeout)

    async def close(self) -> None:
        """Close httpx client. Idempotent."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("PolymarketCollector: closed HTTP client")

    @property
    def is_open(self) -> bool:
        return self._client is not None

    # ─────────────────────────────────────────────────────────────────
    # Public API — Gamma (market metadata)
    # ─────────────────────────────────────────────────────────────────
    async def fetch_market(
        self,
        slug: str,
        *,
        include_closed: bool = False,
    ) -> dict[str, Any] | None:
        """Fetch metadata for one market by slug.

        Args:
            slug: Polymarket market slug (e.g. "iran-strike-by-feb28").
            include_closed: True → also search closed/resolved markets.
                Live polling uses False (default) — closed markets are meaningless.
                Replay (P10) uses True — past events refer to already-resolved markets.

        Returns:
            dict: market object (volume24hr, lastTradePrice, conditionId, etc.).
            None: slug does not exist or market expired (returned as empty array, not 404).

        Raises:
            httpx.HTTPStatusError: 4xx/5xx response (after retries fail).
            RetryError: all retries exhausted.
        """
        params: dict[str, str] = {"slug": slug}
        # Gamma /markets default = active markets only.
        # Add closed=true to include closed markets (P10.4 re-verification).
        # NOTE: closed=any once worked but as of 2026-04 it is rejected with a 422
        # validation error — boolean only. So we only use 'true'/'false'.
        if include_closed:
            params["closed"] = "true"

        data = await self._get_json("/markets", base=GAMMA_BASE_URL, params=params)

        if not isinstance(data, list) or not data:
            logger.debug("PolymarketCollector: market not found for slug=%s", slug)
            return None
        return data[0]

    # ─────────────────────────────────────────────────────────────────
    # Public API — Data (recent trades)
    # ─────────────────────────────────────────────────────────────────
    async def fetch_trades(
        self,
        condition_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch recent trades by conditionId.

        Args:
            condition_id: market's conditionId (0x...).
            limit: max trades to fetch. v1 recommends 50 (enough for 5s polling).

        Returns:
            list[dict]: newest trade first. Empty list when none.
        """
        params = {"market": condition_id, "limit": str(int(limit))}
        data = await self._get_json("/trades", base=DATA_BASE_URL, params=params)

        if not isinstance(data, list):
            logger.warning("PolymarketCollector: unexpected trades payload type=%s",
                           type(data).__name__)
            return []
        return data

    # ─────────────────────────────────────────────────────────────────
    # Internal — HTTP GET with retry (tenacity exponential backoff)
    # ─────────────────────────────────────────────────────────────────
    async def _get_json(
        self,
        path: str,
        *,
        base: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        """GET call + retry. Auto-opens if open() was not called."""
        if self._client is None:
            await self.open()
        assert self._client is not None  # mypy

        url = f"{base}{path}"

        # AsyncRetrying — tenacity's async-aware retry. Up to 3 attempts, backoff 0.5s → 1s → 2s.
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            retry=retry_if_exception_type(_RETRIABLE_EXC),
            reraise=True,
        )

        try:
            async for attempt in retrying:
                with attempt:
                    resp = await self._client.get(url, params=params)
                    # raise_for_status on 4xx/5xx — 5xx is not in _RETRIABLE_EXC so no retry.
                    # For 5xx retries we'd need a separate branch — v1 keeps it simple (retry on next polling cycle).
                    resp.raise_for_status()
                    return resp.json()
        except RetryError as e:
            logger.warning("PolymarketCollector: %s exhausted retries: %s", url, e)
            raise
        # AsyncRetrying always exits via the return/raise above — unreachable.
        return None  # pragma: no cover
