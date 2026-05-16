"""
hyperliquid/normalizer.py — Hyperliquid Info payload → unified schema conversion.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §6.4 "Right format" sanity check):
  Convert one asset context from the Hyperliquid `metaAndAssetCtxs` response
  into our schema (RawEvent / NormalizedEvent). Reject invalid payloads
  immediately — fail-fast (architecture §6.4).

  Major difference from Polymarket:
    - Polymarket trade payload is "1 trade" → sided + size_usd naturally.
    - Hyperliquid's assetCtx payload is a **snapshot** (24h cumulative stats + current price).
      i.e. it represents "cumulative over 24h" rather than "size traded in this cycle".
      → features compute deltas by subtracting two cumulatives (5-min buckets).
      → normalizer simply stamps the cumulative into NormalizedEvent.meta as-is.
      → size_usd = 0 (the snapshot itself is not a single trade), price uses markPx,
        side = None (not a sided trade).

  One conversion variant:
    asset context dict + coin name → 1 RawEvent + 1 NormalizedEvent

────────────────────────────────────────────────────────────────────────
Hyperliquid asset context payload spec (from collector.py response):

  {
    "dayNtlVlm":   "1234567.89",   # 24h cumulative notional volume in USD
    "openInterest":"688.11",       # OI in base coin (BTC count, ETH count, ...)
    "funding":     "0.0000125",    # current funding rate (1h or 8h depending coin)
    "markPx":      "63000.0",      # mark price (USD)
    "midPx":       "63010.5",      # mid price (USD; nullable)
    "prevDayPx":   "60000.0",      # 24h ago price (USD)
    "oraclePx":    "63005.0",
    "premium":     "0.00031774",   # nullable (low-liquidity coin)
    "impactPxs":   ["62950", "63050"]  # nullable
  }

  Required (raise if missing): dayNtlVlm, markPx
  Optional (None if missing):  midPx, openInterest, funding, prevDayPx, oraclePx

────────────────────────────────────────────────────────────────────────
Symbol convention:
  Watchlist contains coin names like "BTC", "ETH" (the perp names from the
  Hyperliquid universe as-is). NormalizedEvent.symbol uses that coin name.
  Cross-channel collisions are distinguished by the `channel` field — symbol stays simple.

────────────────────────────────────────────────────────────────────────
Architecture: §6.4 "Right format" sanity, §4.1 NormalizedEvent
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ...core.schemas import (
    CHANNEL_HYPERLIQUID,
    NormalizedEvent,
    RawEvent,
    Source,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Asset context normalizer (main — called every cycle)
# =====================================================================
def normalize_asset_ctx(
    coin: str,
    ctx: dict[str, Any],
    *,
    universe_meta: dict[str, Any] | None = None,
    ts_source: datetime | None = None,
) -> tuple[RawEvent, NormalizedEvent]:
    """Hyperliquid asset context dict → (RawEvent, NormalizedEvent).

    Args:
        coin: perp coin name (e.g. "BTC", "ETH"). As written in the watchlist.
        ctx: assetCtxs[i] dict returned by collector.fetch_meta_and_asset_ctxs().
        universe_meta: universe item at the same index (optional, attached to meta).
        ts_source: response time (UTC). None → use call time (Hyperliquid response
                   does not include a per-asset timestamp).

    Returns:
        (RawEvent, NormalizedEvent): both are passed by caller to store/feature.

    Raises:
        ValueError: missing required field / wrong type — fail-fast.
    """
    # Required field extraction — fail-fast (architecture §6.4)
    try:
        day_ntl_vlm = float(ctx["dayNtlVlm"])
        mark_px = float(ctx["markPx"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(
            f"hyperliquid asset ctx missing/invalid required field for "
            f"coin={coin!r}: {e} (keys present: {list(ctx.keys())[:10]})"
        ) from e

    # validation — reject negatives (cumulative volume should be monotonic non-decreasing)
    if day_ntl_vlm < 0:
        raise ValueError(
            f"hyperliquid asset ctx dayNtlVlm < 0 for coin={coin!r}: {day_ntl_vlm}"
        )
    if mark_px < 0:
        raise ValueError(
            f"hyperliquid asset ctx markPx < 0 for coin={coin!r}: {mark_px}"
        )

    # Optional fields — None when missing or null
    mid_px = _safe_float(ctx.get("midPx"))
    open_interest = _safe_float(ctx.get("openInterest"))
    funding = _safe_float(ctx.get("funding"))
    prev_day_px = _safe_float(ctx.get("prevDayPx"))
    oracle_px = _safe_float(ctx.get("oraclePx"))

    if ts_source is None:
        ts_source = datetime.now(timezone.utc)

    # RawEvent — original payload preserved for audit. universe meta is bundled together to stay reproducible.
    raw_payload: dict[str, Any] = {"coin": coin, "ctx": ctx}
    if universe_meta is not None:
        raw_payload["universe_meta"] = universe_meta

    raw = RawEvent(
        channel=CHANNEL_HYPERLIQUID,
        source=Source.REST,
        symbol=coin,
        ts_source=ts_source,
        payload=raw_payload,
    )

    # NormalizedEvent — snapshot, so size_usd=0, side=None.
    # cumulative day_ntl_vlm etc. live in meta so features handles them via a history buffer.
    normalized = NormalizedEvent(
        channel=CHANNEL_HYPERLIQUID,
        symbol=coin,
        ts_source=ts_source,
        ts_ingest=raw.ts_ingest,
        side=None,            # snapshot — not a sided trade
        size_usd=0.0,         # this single snapshot itself has 0 trade volume
        price=mark_px,        # use markPx as the representative price
        meta={
            "day_ntl_vlm_usd": day_ntl_vlm,
            "open_interest_coins": open_interest,
            "funding_rate": funding,
            "mark_px": mark_px,
            "mid_px": mid_px,
            "prev_day_px": prev_day_px,
            "oracle_px": oracle_px,
        },
        raw_ref=raw.id,
    )

    return raw, normalized


# =====================================================================
# Backward-compatible entry (skeleton's normalize() signature compat)
# =====================================================================
def normalize(raw: dict[str, Any]) -> NormalizedEvent:
    """Skeleton-compat. Real use should prefer normalize_asset_ctx().

    Assumes raw dict in the shape {"coin": str, "ctx": dict, "universe_meta"?: dict}.
    """
    coin = str(raw["coin"])
    ctx = raw["ctx"]
    universe_meta = raw.get("universe_meta")
    _, normalized = normalize_asset_ctx(coin, ctx, universe_meta=universe_meta)
    return normalized


# =====================================================================
# Internal helper
# =====================================================================
def _safe_float(v: Any) -> float | None:
    """None / "null" / empty string → None. Otherwise try float(); return None on failure."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
