"""
hyperliquid/collector.py — Hyperliquid Info API thin client (REST POST /info).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §2.1, §6.6):
  Only responsible for talking to the public Hyperliquid Info endpoint.

    POST https://api.hyperliquid.xyz/info
    body : {"type": "metaAndAssetCtxs"}   (no auth required, public)

  Response: 2-element list.
    [0] = universe meta — {"universe": [{"name": "BTC", "szDecimals": 5,
                                          "maxLeverage": 50}, ...]}
    [1] = assetCtxs — per-asset market stat dicts in the same order as universe:
            {
              "dayNtlVlm":   "1234567.89",   # 24h notional volume in USD
              "openInterest":"688.11",       # OI (base coin)
              "funding":     "0.0000125",    # current funding rate
              "markPx":      "63000.0",      # mark price (USD)
              "midPx":       "63010.5",      # mid price
              "prevDayPx":   "60000.0",      # 24h ago price
              "oraclePx":    "63005.0",
              "premium":     "0.00031774",
              "impactPxs":   ["62950", "63050"]
            }

  v1 P3 is REST polling first (architecture §3 walking-skeleton).
  Upgrade to WebSocket arrives in P9 deep-dive (when we need trade-by-trade
  microstructure). With just 5s REST polling, the v1 latency goal
  (RISK_OFF P95 < 60s) is comfortably met — features only look at 5-min
  cumulative deltas.

  Manages only lifecycle (open / close). Polling loop / signal emit etc. are channel.py's job.

────────────────────────────────────────────────────────────────────────
Dependencies:
  - httpx               : async HTTP client (already in requirements-anomaly.txt)
  - tenacity            : exponential backoff + retry (D4)
  - No external API auth required (public read-only)

────────────────────────────────────────────────────────────────────────
Architecture: §2.1 Component, §6.6 Failure Mode (retry/backoff)
Plan: §3.1 Goal #2 (Hyperliquid channel)
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


# Hyperliquid public REST endpoint (no auth)
INFO_BASE_URL: str = "https://api.hyperliquid.xyz"
INFO_PATH: str = "/info"

# tenacity retry targets — network / timeout only. HTTP 4xx (request-format errors) fail immediately.
_RETRIABLE_EXC = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class HyperliquidCollector:
    """Hyperliquid Info API thin client.

    One instance is shared inside the channel (single httpx.AsyncClient).
    On channel startup call open() once, on shutdown call close() once.
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
                # Cap concurrent connections — protect Hyperliquid rate limits
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                headers={
                    "User-Agent": "anomaly-daemon/0.1 (+hyperliquid-channel)",
                    "Content-Type": "application/json",
                },
            )
            logger.info("HyperliquidCollector: opened HTTP client (timeout=%.1fs)",
                        self._timeout)

    async def close(self) -> None:
        """Close httpx client. Idempotent."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("HyperliquidCollector: closed HTTP client")

    @property
    def is_open(self) -> bool:
        return self._client is not None

    # ─────────────────────────────────────────────────────────────────
    # Public API — metaAndAssetCtxs (universe + per-asset stats)
    # ─────────────────────────────────────────────────────────────────
    async def fetch_meta_and_asset_ctxs(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """One-shot query for every perp asset's meta + current market stats.

        Returns:
            (meta, asset_ctxs):
              - meta:        {"universe": [{"name": "BTC", "szDecimals": ..., ...}, ...]}
              - asset_ctxs:  same order as universe. dict per asset.

            Raises ValueError if the payload format is unexpected (fail-fast — schema changes caught immediately).

        Raises:
            ValueError: payload format mismatch.
            httpx.HTTPStatusError: 4xx/5xx response.
            RetryError: all retries exhausted.
        """
        body = {"type": "metaAndAssetCtxs"}
        data = await self._post_json(INFO_PATH, json_body=body)

        # Response must be a 2-element list: [meta_obj, asset_ctxs_list]
        if not isinstance(data, list) or len(data) != 2:
            raise ValueError(
                f"hyperliquid metaAndAssetCtxs unexpected payload type/len: "
                f"type={type(data).__name__}, "
                f"len={len(data) if isinstance(data, list) else 'n/a'}"
            )

        meta, asset_ctxs = data[0], data[1]
        if not isinstance(meta, dict) or "universe" not in meta:
            raise ValueError(
                "hyperliquid metaAndAssetCtxs: meta missing 'universe' field"
            )
        if not isinstance(asset_ctxs, list):
            raise ValueError(
                f"hyperliquid metaAndAssetCtxs: assetCtxs not a list "
                f"(got {type(asset_ctxs).__name__})"
            )

        return meta, asset_ctxs

    # ─────────────────────────────────────────────────────────────────
    # Public API — recentTrades (per-coin, includes wallet addresses)
    # ─────────────────────────────────────────────────────────────────
    async def fetch_recent_trades(self, coin: str) -> list[dict[str, Any]]:
        """One-shot query for a specific coin's recent trades. Includes wallet addresses.

        Args:
            coin: perp coin name (e.g. "BTC", "ETH"). Same as universe.name.

        Returns:
            trades: list of trade dicts. Empty list when no response.
                Key fields per trade (Hyperliquid Info /info recentTrades schema):
                  - "coin"  (str)        — coin traded
                  - "side"  (str)        — "B" (taker buy) or "A" (taker sell)
                  - "px"    (str)        — execution price (str for decimal precision)
                  - "sz"    (str)        — execution size (str)
                  - "time"  (int, ms)    — execution time (UTC ms epoch)
                  - "hash"  (str)        — onchain tx hash
                  - "tid"   (int)        — unique trade id (dedupe key)
                  - "users" (list[str])  — [taker_address, maker_address]
                                           we only look at taker (=users[0]).

            Raises ValueError on payload format mismatch (fail-fast).

        Raises:
            ValueError: payload format mismatch.
            httpx.HTTPStatusError: 4xx/5xx response.
            RetryError: all retries exhausted.
        """
        if not coin or not isinstance(coin, str):
            raise ValueError(f"coin must be non-empty str, got {coin!r}")

        body = {"type": "recentTrades", "coin": coin}
        data = await self._post_json(INFO_PATH, json_body=body)

        # Response must be a list of dicts (empty list when absent)
        if data is None:
            return []
        if not isinstance(data, list):
            raise ValueError(
                f"hyperliquid recentTrades unexpected payload type for coin={coin}: "
                f"type={type(data).__name__}"
            )

        # Only verify each element is a dict. Skip malformed items.
        clean: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict):
                clean.append(item)
        return clean

    # ─────────────────────────────────────────────────────────────────
    # Internal — HTTP POST with retry (tenacity exponential backoff)
    # ─────────────────────────────────────────────────────────────────
    async def _post_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
    ) -> Any:
        """POST call + retry. Auto-opens if open() was not called."""
        if self._client is None:
            await self.open()
        assert self._client is not None  # mypy

        url = f"{INFO_BASE_URL}{path}"

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            retry=retry_if_exception_type(_RETRIABLE_EXC),
            reraise=True,
        )

        try:
            async for attempt in retrying:
                with attempt:
                    resp = await self._client.post(url, json=json_body)
                    # 4xx/5xx → raise. 5xx is not in _RETRIABLE_EXC, so no retry.
                    # v1 simplification: 5xx retries on the next polling cycle.
                    resp.raise_for_status()
                    return resp.json()
        except RetryError as e:
            logger.warning("HyperliquidCollector: %s exhausted retries: %s", url, e)
            raise
        # AsyncRetrying always exits via the return/raise above — unreachable.
        return None  # pragma: no cover
