"""
polymarket/normalizer.py — Polymarket REST payload → unified schema conversion.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §6.4 "Right format" sanity check):
  Convert Polymarket's two-endpoint payloads into our schema (RawEvent /
  NormalizedEvent). Invalid payloads (None, missing required fields, type
  errors) are rejected immediately — fail-fast principle (architecture §6.4).

  Two kinds of conversion:
    1) trade payload   → 1 RawEvent + 1 NormalizedEvent (sided trade)
    2) market payload  → 1 RawEvent (snapshot only — not a sided trade)
                         (No NormalizedEvent. v1: size_usd is meaningless)

  v1 P2 primarily uses trade conversion. Market snapshot is for baseline / debugging.

────────────────────────────────────────────────────────────────────────
Polymarket payload spec (captured from collector.py endpoint responses):

  trade:
    {
      "proxyWallet": "0x...",
      "side": "BUY" | "SELL",
      "asset": "<token_id>",
      "conditionId": "0x...",
      "size": <float>,             # YES/NO token count (≈ USD)
      "price": <float in [0,1]>,
      "timestamp": <unix_seconds:int>,
      "title": "...",              # market title
      "slug": "...",               # market slug
      ...
    }

  market:
    {
      "slug": "...",
      "conditionId": "0x...",
      "lastTradePrice": <float>,
      "bestBid": <float>,
      "bestAsk": <float>,
      "spread": <float>,
      "volume24hr": <float>,
      "active": bool, "closed": bool,
      ...
    }

────────────────────────────────────────────────────────────────────────
Polymarket's "size" → "size_usd" conversion:
  Polymarket trade `size` is token count (YES or NO).
  USD-equivalent ≈ size × price (1 token's USD value = price).
  v1 uses this approximation. (Strictly, we'd need to look at collateral too — P9.)

────────────────────────────────────────────────────────────────────────
Architecture: §6.4 "Right format" sanity, §4.1 NormalizedEvent
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ...core.schemas import (
    CHANNEL_POLYMARKET,
    NormalizedEvent,
    RawEvent,
    Side,
    Source,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Trade payload normalizer (main — called every cycle)
# =====================================================================
def normalize_trade(
    raw_payload: dict[str, Any],
    market_meta: dict[str, Any] | None = None,
) -> tuple[RawEvent, NormalizedEvent]:
    """Polymarket trade payload → (RawEvent, NormalizedEvent).

    Args:
        raw_payload: a single dict returned by collector.fetch_trades().
        market_meta: optional. Orderbook side info extracted from Gamma API market response.
            If present, these keys flow into NormalizedEvent.meta (P9.1 M2 mid-price):
              - "bestBid"   : float — current YES market highest bid [0,1]
              - "bestAsk"   : float — current YES market lowest ask [0,1]
              - "spread"    : float — bestAsk - bestBid
            mid price (`mid_price = (bestBid + bestAsk) / 2`) is auto-computed.
            If None or keys missing, nothing flows into meta (preserves prior behavior).

    Returns:
        (RawEvent, NormalizedEvent): both are stored by caller + handed to features.

    Raises:
        ValueError: missing required field or wrong type — fail-fast.
                    (Catches collector's external API schema changes immediately.)
    """
    # Extract required fields — fail-fast if missing (architecture §6.4)
    try:
        proxy_wallet = str(raw_payload["proxyWallet"])
        side_str = str(raw_payload["side"]).upper()
        slug = str(raw_payload["slug"])
        condition_id = str(raw_payload["conditionId"])
        size_tokens = float(raw_payload["size"])
        price = float(raw_payload["price"])
        unix_ts = int(raw_payload["timestamp"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(
            f"polymarket trade payload missing/invalid required field: {e} "
            f"(payload keys: {list(raw_payload.keys())[:10]})"
        ) from e

    # validation — reject negative size / out-of-range price
    if size_tokens < 0:
        raise ValueError(f"polymarket trade size < 0: {size_tokens}")
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"polymarket trade price out of [0,1]: {price}")

    # Side conversion: Polymarket BUY/SELL → our Side enum.
    # NOTE: by default BUY is interpreted as a YES outcome buy (most common case).
    # outcome=NO buys are ignored in v1 (outcome-aware mapping arrives in P9).
    if side_str == "BUY":
        side: Side = Side.YES
    elif side_str == "SELL":
        side = Side.NO
    else:
        raise ValueError(f"polymarket trade unknown side: {side_str!r}")

    # USD approximation: with $1-per-token Polymarket collateral, size × price ≈ USD value.
    # YES token market price = price → USD a buyer paid = size × price.
    size_usd = size_tokens * price

    ts_source = datetime.fromtimestamp(unix_ts, tz=timezone.utc)

    # symbol convention: standardize on "polymarket:<slug>" (avoids cross-channel collisions).
    # However, watchlists only carry the slug, so slug itself is also fine.
    # v1 uses the slug as-is (simplest + best for email body readability).
    symbol = slug

    # RawEvent — original payload preserved for audit
    raw = RawEvent(
        channel=CHANNEL_POLYMARKET,
        source=Source.REST,
        symbol=symbol,
        ts_source=ts_source,
        payload=raw_payload,
    )

    # Compose meta — always-included trade-level info + orderbook side info when possible
    meta: dict[str, Any] = {
        "proxy_wallet": proxy_wallet,
        "size_tokens": size_tokens,
        "condition_id": condition_id,
    }

    if market_meta is not None:
        # Pass through Gamma API's bestBid/bestAsk/spread if present (for P9.1 M2 mid-price)
        # Partially populate when only some are present — features stage validates.
        best_bid = _coerce_float(market_meta.get("bestBid"))
        best_ask = _coerce_float(market_meta.get("bestAsk"))
        spread = _coerce_float(market_meta.get("spread"))

        if best_bid is not None:
            meta["best_bid"] = best_bid
        if best_ask is not None:
            meta["best_ask"] = best_ask
        if spread is not None:
            meta["spread"] = spread
        # mid-price only when both sides are present — one side alone is meaningless
        if best_bid is not None and best_ask is not None:
            meta["mid_price"] = (best_bid + best_ask) / 2.0

    # NormalizedEvent — unified schema (input to feature engine)
    normalized = NormalizedEvent(
        channel=CHANNEL_POLYMARKET,
        symbol=symbol,
        ts_source=ts_source,
        ts_ingest=raw.ts_ingest,
        side=side,
        size_usd=size_usd,
        price=price,
        meta=meta,
        raw_ref=raw.id,
    )

    return raw, normalized


def _coerce_float(v: Any) -> float | None:
    """Safely coerce a dict value to float. Return None on None / wrong type.

    Defensive: Polymarket Gamma API sometimes sends numbers as strings or as null.
    """
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# =====================================================================
# Market snapshot normalizer (helper — for baseline / debugging)
# =====================================================================
def normalize_market_snapshot(market_payload: dict[str, Any]) -> RawEvent:
    """Gamma API market metadata → one RawEvent (snapshot).

    Does not produce a NormalizedEvent — it is not a sided trade.
    The feature engine uses this info via a separate path (last_trade_price tracking, etc.).

    Args:
        market_payload: dict returned by collector.fetch_market().

    Raises:
        ValueError: when slug or conditionId is missing.
    """
    try:
        slug = str(market_payload["slug"])
        _ = str(market_payload["conditionId"])
    except KeyError as e:
        raise ValueError(
            f"polymarket market payload missing required field: {e}"
        ) from e

    return RawEvent(
        channel=CHANNEL_POLYMARKET,
        source=Source.REST,
        symbol=slug,
        ts_source=datetime.now(timezone.utc),  # Gamma response has no ts field → use ingest time
        payload=market_payload,
    )


# =====================================================================
# Backward-compatible entry (skeleton's normalize() signature compat)
# =====================================================================
def normalize(raw: dict[str, Any]) -> NormalizedEvent:
    """Skeleton-compat. Accepts only the trade payload.

    Internally calls normalize_trade() and returns only the NormalizedEvent.
    Use normalize_trade() directly if you need the RawEvent for audit.
    """
    _, normalized = normalize_trade(raw)
    return normalized
