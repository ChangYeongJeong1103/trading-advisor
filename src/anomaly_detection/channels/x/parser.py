"""
x/parser.py — X post body → NormalizedEvent.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5 pipeline step 2):

  Convert an X post payload (output of mock_collector.MockXCollector.generate_posts()
  or the EVT-1 snscrape adapter) into our schema.

  Core extraction (P5 baseline — regex + keyword dict):

    1) symbols        : tokens like "$BTC", "$WTI", "BTC", "ETH"
                        (matched against ticker whitelist — reduces false positives)
    2) direction      : "long"/"buy"/"pump"/"sweep call" → BUY
                        "short"/"sell"/"dump"/"sweep put" → SELL
                        (NEUTRAL if missing)
    3) magnitude_flag : True if body contains magnitude phrases like "massive",
                        "$XYZ M", "N,NNN contracts", "150 accounts"
    4) urls           : all https:// links in the body

  parse_post() returns a list of 0~N (RawEvent, NormalizedEvent) tuples.
  When the same post mentions both BTC and ETH, 2 NormalizedEvents are emitted
  (per-symbol fan-out). When no symbols match, an empty list — features auto-skips.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · regex + keyword dict (simple + deterministic). NER / LLM arrive in P9.

  · Symbol whitelist (`_KNOWN_SYMBOLS`) blocks false positives:
    posts like "I LOVE TACOS" are noise, breaking fusion corroboration.

  · Direction is simple keyword matching. NEUTRAL if missing (most anomaly posts
    use direction-ambiguous wording like "ALERT", "massive", "moving").

  · magnitude flag is used by detector when escalating to EMERGENCY (plan §8 P5).

  · ts_source is the same for one post — even on symbol fan-out.

────────────────────────────────────────────────────────────────────────
Plan: §8 P5 (v0 detector = keyword whitelist + credibility)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ...core.schemas import (
    CHANNEL_X,
    NormalizedEvent,
    RawEvent,
    Side,
    Source,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Symbol whitelist — aligned with the fusion engine's other-channel symbols
# =====================================================================
# (Map X's free text → symbols used by other channels)
# Polymarket: market slug — hard to match by name (long) → P9.
# Hyperliquid: BTC/ETH/SOL/...
# CME: CL(=WTI)/GC(=GOLD)/ES(=SPX)/NQ/ZB/6E
_KNOWN_SYMBOLS: dict[str, str] = {
    # ticker → canonical symbol (aligned with other channels)
    "BTC": "BTC",
    "BITCOIN": "BTC",
    "ETH": "ETH",
    "ETHEREUM": "ETH",
    "SOL": "SOL",
    "SOLANA": "SOL",
    "WTI": "CL",       # CME's WTI = "CL"
    "CRUDE": "CL",
    "OIL": "CL",
    "GOLD": "GC",      # CME's Gold = "GC"
    "XAU": "GC",
    "ES": "ES",
    "SPX": "ES",
    "SPY": "ES",       # SPY ETF correlates strongly with ES futures
    "NQ": "NQ",
    "NDX": "NQ",
    "QQQ": "NQ",
}

# Symbol token regex: $TICKER (e.g. $BTC, $WTI) or word-boundary ticker (e.g. " BTC ")
_SYMBOL_PATTERN = re.compile(
    r"(?:\$([A-Z]{2,6})\b|(?<![A-Za-z0-9])([A-Z]{3,8})(?![A-Za-z0-9]))"
)

# Direction keywords
_BUY_KEYWORDS = (
    "long", "buy", "buys", "bought", "pump", "pumping", "bull", "bullish",
    "rally", "moon", "calls", "call sweep", "sweep call",
)
_SELL_KEYWORDS = (
    "short", "sell", "sells", "sold", "dump", "dumping", "bear", "bearish",
    "crash", "puts", "put sweep", "sweep put",
)

# Magnitude phrases — part of the EMERGENCY trigger
_MAGNITUDE_KEYWORDS = (
    "massive", "huge", "big", "alert", "🚨", "unusual",
)
_MAGNITUDE_NUMBER_RE = re.compile(
    r"(?:"
    r"\$\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*[MmBbKk]"     # $580M / $1.2B / $20K
    r"|\d{1,3}(?:,\d{3})+\s*(?:contracts|accounts|positions|tweets|posts)"  # 6,200 contracts
    r"|\d{1,2}[Kk]\s*(?:contracts|positions)"        # 12k contracts
    r")"
)
_URL_RE = re.compile(r"https?://\S+")


# =====================================================================
# Public — parse_post (mock or snscrape results both)
# =====================================================================
def parse_post(post: dict[str, Any]) -> list[tuple[RawEvent, NormalizedEvent]]:
    """X post payload → 0~N (RawEvent, NormalizedEvent) tuples.

    Args:
        post: dict output of mock_collector / snscrape adapter.
              Required: id, user, text, timestamp.
              Optional: url, is_mock_spike, source.

    Returns:
        list[tuple[RawEvent, NormalizedEvent]].
        Empty list if no symbols matched (features auto-skips).

    Raises:
        ValueError: required field missing (fail-fast).
    """
    try:
        post_id = str(post["id"])
        user = str(post["user"])
        text = str(post["text"])
        unix_ts = int(post["timestamp"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(
            f"x post payload missing/invalid required field: {e} "
            f"(payload keys: {list(post.keys())[:10]})"
        ) from e

    url = post.get("url")
    is_mock_spike = bool(post.get("is_mock_spike", False))
    source_label = str(post.get("source", "unknown"))

    ts_source = datetime.fromtimestamp(unix_ts, tz=timezone.utc)

    # 1) Symbols — whitelist matching only
    symbols = _extract_symbols(text)
    if not symbols:
        return []

    # 2) Direction
    direction = _extract_direction(text)

    # 3) Magnitude
    has_magnitude = _has_magnitude(text)

    # 4) URLs
    urls = _URL_RE.findall(text)

    # 1 post payload → 1 RawEvent + N NormalizedEvents (one per symbol)
    raw = RawEvent(
        channel=CHANNEL_X,
        source=Source.SCRAPE,      # classify snscrape / mock both as scraping
        symbol=symbols[0],         # representative symbol (raw is single)
        ts_source=ts_source,
        payload=post,
    )

    out: list[tuple[RawEvent, NormalizedEvent]] = []
    for sym in symbols:
        meta = {
            "user": user,
            "text": text,
            "post_id": post_id,
            "url": url,
            "urls_in_text": urls,
            "has_magnitude": has_magnitude,
            "all_symbols": symbols,
            "is_mock_spike": is_mock_spike,
            "source_label": source_label,
        }
        normalized = NormalizedEvent(
            channel=CHANNEL_X,
            symbol=sym,
            ts_source=ts_source,
            ts_ingest=raw.ts_ingest,
            side=direction,
            size_usd=0.0,           # X is not a trade — size 0
            price=None,
            meta=meta,
            raw_ref=raw.id,
        )
        out.append((raw, normalized))

    return out


# =====================================================================
# Helpers
# =====================================================================
def _extract_symbols(text: str) -> list[str]:
    """Extract whitelisted tickers as canonical symbols (dedupe, preserve order)."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _SYMBOL_PATTERN.finditer(text.upper()):
        tok = m.group(1) or m.group(2)
        if tok is None:
            continue
        canonical = _KNOWN_SYMBOLS.get(tok)
        if canonical is None:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def _extract_direction(text: str) -> Side | None:
    """Match direction keywords. None if both or neither are present."""
    low = text.lower()
    has_buy = any(kw in low for kw in _BUY_KEYWORDS)
    has_sell = any(kw in low for kw in _SELL_KEYWORDS)
    if has_buy and not has_sell:
        return Side.BUY
    if has_sell and not has_buy:
        return Side.SELL
    return None


def _has_magnitude(text: str) -> bool:
    """Whether the body contains a magnitude phrase / numeric expression."""
    low = text.lower()
    if any(kw in low for kw in _MAGNITUDE_KEYWORDS):
        return True
    if _MAGNITUDE_NUMBER_RE.search(text) is not None:
        return True
    return False
