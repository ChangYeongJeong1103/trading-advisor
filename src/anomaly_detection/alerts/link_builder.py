"""
alerts/link_builder.py — Build external visual-check links (architecture §6.5.2).

────────────────────────────────────────────────────────────────────────
Role:
  The email body is self-contained (has all necessary information), but
  the user must be able to jump to a visual chart / orderbook / wallet
  in one click for deeper investigation. This module builds those URLs.

  Pure functions only — no config / state dependency. Every argument is
  the caller's responsibility.

────────────────────────────────────────────────────────────────────────
URL templates (architecture §6.5.2):

  Polymarket   → https://polymarket.com/market/<market-slug>
  Hyperliquid  → https://hypurrscan.io/address/<wallet>          (wallet view)
                 https://app.hyperliquid.xyz/trade/<asset>        (asset orderbook)
  CME          → TradingView chart link (preset symbol + interval)
  X            → original post URL (validation only)

  TradingView's CME futures symbol prefix:
    "CL"  → "NYMEX:CL1!"  (front-month continuous)
    "GC"  → "COMEX:GC1!"
    "ES"  → "CME_MINI:ES1!"
    The above prefix mapping is maintained in CME_TV_PREFIX.
"""

from __future__ import annotations

import re
from urllib.parse import quote


# CME symbol → TradingView exchange-prefix mapping.
# Missing symbols fall back to "CME:" (the most common prefix on TradingView).
# Extend this whenever the watchlist grows (in P9).
CME_TV_PREFIX: dict[str, str] = {
    "CL": "NYMEX:CL1!",       # WTI Crude Oil (front-month continuous)
    "BZ": "NYMEX:BZ1!",       # CME Brent Crude Oil (Last Day Financial)
    "GC": "COMEX:GC1!",       # Gold
    "SI": "COMEX:SI1!",       # Silver
    "ES": "CME_MINI:ES1!",    # E-mini S&P 500
    "NQ": "CME_MINI:NQ1!",    # E-mini Nasdaq
    "RTY": "CME_MINI:RTY1!",  # E-mini Russell
    "ZN": "CBOT:ZN1!",        # 10Y T-Note
    "ZB": "CBOT:ZB1!",        # 30Y T-Bond
}


# ─────────────────────────────────────────────────────────────────────
# Polymarket
# ─────────────────────────────────────────────────────────────────────
def polymarket_market(market_slug: str) -> str:
    """Polymarket market page URL.

    Args:
        market_slug: e.g. "iran-strike-by-feb-28-2026". Assumed URL-safe (already a slug).

    Returns:
        "https://polymarket.com/market/<slug>"
    """
    if not market_slug:
        raise ValueError("market_slug must not be empty")
    return f"https://polymarket.com/market/{quote(market_slug, safe='-_')}"


# ─────────────────────────────────────────────────────────────────────
# Hyperliquid
# ─────────────────────────────────────────────────────────────────────
_ETH_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def hyperliquid_wallet(address: str) -> str:
    """Hypurrscan wallet (address) page URL.

    Args:
        address: EVM address (0x + 40 hex chars).

    Returns:
        "https://hypurrscan.io/address/<addr>"

    Raises:
        ValueError: when the address is not in standard EVM format (0x + 40 hex).
    """
    if not _ETH_ADDR_RE.match(address):
        raise ValueError(f"Invalid EVM address: {address!r}")
    return f"https://hypurrscan.io/address/{address}"


def hyperliquid_asset(asset: str) -> str:
    """Hyperliquid asset orderbook URL.

    Args:
        asset: Asset name. e.g. "BTC", "ETH", "SOL" — case preserved.

    Returns:
        "https://app.hyperliquid.xyz/trade/<asset>"
    """
    if not asset or not asset.strip():
        raise ValueError("asset must not be empty")
    return f"https://app.hyperliquid.xyz/trade/{quote(asset.strip(), safe='')}"


# ─────────────────────────────────────────────────────────────────────
# CME → TradingView chart
# ─────────────────────────────────────────────────────────────────────
def tradingview_chart(symbol: str, interval: str = "5") -> str:
    """Direct link to a TradingView chart.

    Args:
        symbol: CME symbol (e.g. "CL", "GC", "ES"). Looked up in CME_TV_PREFIX.
                If unmapped, used as-is (caller may pass a full prefix).
        interval: TradingView interval string. Default "5" (5 minutes).
                  Allowed: "1", "5", "15", "60", "240", "D", "W".

    Returns:
        TradingView chart URL.
    """
    if not symbol or not symbol.strip():
        raise ValueError("symbol must not be empty")

    sym = symbol.strip().upper()
    tv_symbol = CME_TV_PREFIX.get(sym, sym)  # if unmapped, used as-is (may be a full prefix)
    return (
        "https://www.tradingview.com/chart/"
        f"?symbol={quote(tv_symbol, safe=':!')}&interval={quote(interval, safe='')}"
    )


# ─────────────────────────────────────────────────────────────────────
# X (Twitter)
# ─────────────────────────────────────────────────────────────────────
_X_URL_RE = re.compile(r"^https?://(twitter\.com|x\.com)/[^/]+/status/\d+", re.IGNORECASE)


def x_post(post_url: str) -> str:
    """Return the X post URL as-is. Format validation only.

    Args:
        post_url: e.g. "https://x.com/realDonaldTrump/status/1234567890".

    Returns:
        The same URL after validation.

    Raises:
        ValueError: when not in the x.com or twitter.com status URL format.
    """
    if not post_url or not _X_URL_RE.match(post_url):
        raise ValueError(f"Invalid X post URL: {post_url!r}")
    return post_url


# ─────────────────────────────────────────────────────────────────────
# Truth Social (Channel 5)
# ─────────────────────────────────────────────────────────────────────
# Truth Social status URL — Mastodon-compatible path.
#   https://truthsocial.com/@realDonaldTrump/posts/<post_id>
#   https://truthsocial.com/@realDonaldTrump/<post_id>          (older)
_TRUTH_SOCIAL_URL_RE = re.compile(
    r"^https?://truthsocial\.com/@[^/]+/(?:posts/)?\d+",
    re.IGNORECASE,
)


def truth_social_post(post_url: str) -> str:
    """Return the Truth Social post URL as-is. Format validation only.

    Args:
        post_url: e.g. "https://truthsocial.com/@realDonaldTrump/posts/123…".

    Returns:
        The same URL after validation.

    Raises:
        ValueError: when not in the truthsocial.com status URL format.
    """
    if not post_url or not _TRUTH_SOCIAL_URL_RE.match(post_url):
        raise ValueError(f"Invalid Truth Social post URL: {post_url!r}")
    return post_url


__all__ = [
    "CME_TV_PREFIX",
    "polymarket_market",
    "hyperliquid_wallet",
    "hyperliquid_asset",
    "tradingview_chart",
    "x_post",
    "truth_social_post",
]
