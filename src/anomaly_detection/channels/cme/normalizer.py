"""
cme/normalizer.py — CME mock trade payload → unified schema conversion.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §6.4 "Right format" sanity check):

  Convert a mock CME trade payload (the dict returned by
  mock_collector.MockCMECollector.generate_trades) into our schema
  (RawEvent + NormalizedEvent).

  P4 only handles mock. In P9, add per-source functions like
  normalize_databento_tick / normalize_tradingview_webhook /
  normalize_uw_flow for Databento / TradingView / UnusualWhales payloads —
  all unified into the same NormalizedEvent.

────────────────────────────────────────────────────────────────────────
Mock CME trade payload spec (mock_collector output):

  {
    "symbol":         "CL",
    "timestamp":      <unix_seconds:int>,
    "price":          <float>,
    "size_contracts": <float>,
    "size_usd":       <float>,           # pre-computed USD notional
    "side":           "BUY" | "SELL",
    "is_mock_spike":  bool,              # True when this trade is a spike
    "source":         "mock_v1",
  }

  Required (raise if missing): symbol, timestamp, price, size_contracts, size_usd, side
  Optional (defaults when missing): is_mock_spike (False), source ("mock_v1")

────────────────────────────────────────────────────────────────────────
Architecture: §6.4 "Right format" sanity, §4.1 NormalizedEvent
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ...core.schemas import (
    CHANNEL_CME,
    NormalizedEvent,
    RawEvent,
    Side,
    Source,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Mock trade normalizer (P4 main — called every cycle)
# =====================================================================
def normalize_mock_trade(raw_payload: dict[str, Any]) -> tuple[RawEvent, NormalizedEvent]:
    """CME mock trade payload → (RawEvent, NormalizedEvent).

    Args:
        raw_payload: a single dict from mock_collector.generate_trades().

    Returns:
        (RawEvent, NormalizedEvent): caller (channel) passes them to store/feature.

    Raises:
        ValueError: missing/invalid required field — fail-fast.
    """
    try:
        symbol = str(raw_payload["symbol"])
        unix_ts = int(raw_payload["timestamp"])
        price = float(raw_payload["price"])
        size_contracts = float(raw_payload["size_contracts"])
        size_usd = float(raw_payload["size_usd"])
        side_str = str(raw_payload["side"]).upper()
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(
            f"cme mock trade payload missing/invalid required field: {e} "
            f"(payload keys: {list(raw_payload.keys())[:10]})"
        ) from e

    if size_contracts < 0:
        raise ValueError(f"cme mock trade size_contracts < 0: {size_contracts}")
    if size_usd < 0:
        raise ValueError(f"cme mock trade size_usd < 0: {size_usd}")
    if price < 0:
        raise ValueError(f"cme mock trade price < 0: {price}")

    if side_str == "BUY":
        side: Side = Side.BUY
    elif side_str == "SELL":
        side = Side.SELL
    else:
        raise ValueError(f"cme mock trade unknown side: {side_str!r}")

    is_mock_spike = bool(raw_payload.get("is_mock_spike", False))
    source_label = str(raw_payload.get("source", "mock_v1"))

    ts_source = datetime.fromtimestamp(unix_ts, tz=timezone.utc)

    raw = RawEvent(
        channel=CHANNEL_CME,
        source=Source.REST,           # mock is also "pull-style" so classify as REST
        symbol=symbol,
        ts_source=ts_source,
        payload=raw_payload,
    )

    normalized = NormalizedEvent(
        channel=CHANNEL_CME,
        symbol=symbol,
        ts_source=ts_source,
        ts_ingest=raw.ts_ingest,
        side=side,
        size_usd=size_usd,
        price=price,
        meta={
            "size_contracts": size_contracts,
            "is_mock_spike": is_mock_spike,
            "source_label": source_label,
        },
        raw_ref=raw.id,
    )

    return raw, normalized


# =====================================================================
# Backward-compat — skeleton signature
# =====================================================================
def normalize(raw: dict[str, Any]) -> NormalizedEvent:
    """Skeleton-compat. Accepts only the mock trade.

    Real use should prefer normalize_mock_trade() directly.
    """
    _, normalized = normalize_mock_trade(raw)
    return normalized
