"""
alerts/renderer/channel_x_post.py — EMERGENCY alert -> X thread text renderer.

Role:
  Convert ChannelSignal plus an optional LLM assessment into a human-readable
  X thread body of up to three tweets.

  tweet #1: signal summary plus plain-language detector/reason-code explanation
  tweet #2: three insider-perspective bullets (LLM)
  tweet #3: three general trading-perspective bullets (LLM)

Notes:
  - This module only handles text rendering. Actual X API calls live in
    x_publisher.py.
  - Raw reason_codes are formatted for internal operators, so external users
    see interpreted meaning instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...core.schemas import CHANNEL_TRUTH_SOCIAL, ChannelSignal
from ..llm_assessor import AlertAssessment

# Single long-post limit after X Premium Basic (user decision 2026-05-04).
# A typical EMERGENCY post (~800 chars) fits in full, so we merge the former
# 3-tweet thread into one tweet for better readability.
_X_MAX_CHARS = 25_000

# Legacy standard tweet limit, kept as a reference in case a future free-tier
# fallback is needed. Currently unused.
_X_STANDARD_MAX_CHARS = 280


@dataclass(frozen=True)
class RenderedXThread:
    """Tweet-thread text for X uploads."""

    tweets: tuple[str, ...]


_DETECTOR_LABELS: dict[str, str] = {
    "vol_z_v1": "volume anomaly",
    "vol_burst_v2": "time-of-day volume burst",
    "price_jump_v1": "price jump",
    "odds_gap_v2": "odds gap",
    "odds_cusum_v1": "cumulative direction shift",
    "directional_v1": "one-way order flow",
    "wallet_concentration_v1": "wallet concentration",
    "insider_v1": "multi-signal insider pattern",
    "cme_insider_v1": "multi-signal insider pattern (CME)",
    "new_whale_v1": "new whale entry",
    "cluster_v1": "distributed whale cluster",
    "panic_filter_v1": "panic-flow filter",
    "tradingview_webhook": "TradingView trigger",
    "llm_classifier_v1": "LLM narrative classifier",
    "x_corroboration_v1": "X cross-account corroboration",
    "x_magnitude_v1": "X aggregated magnitude",
    "truth_social_llm_v1": "LLM Trump-post market-impact scorer",
}


def friendly_detector_name(raw: str) -> str:
    """Convert a raw fired_detectors code into a user-friendly label.

    For unmapped codes, fall back by removing version suffixes such as _v1 and
    _v2. The result may still contain underscore-style visual jargon, but at
    least version numbers are hidden when a mapping is missing.
    """
    if raw in _DETECTOR_LABELS:
        return _DETECTOR_LABELS[raw]
    # Remove trailing numeric version suffixes such as "_v1" and "_v2".
    stripped = re.sub(r"_v\d+$", "", raw)
    return stripped.replace("_", " ")


def friendly_detector_list(raw_list: tuple[str, ...] | list[str]) -> str:
    """Convert a fired_detectors list into a comma-separated friendly string."""
    if not raw_list:
        return "—"
    return ", ".join(friendly_detector_name(r) for r in raw_list)

# CME ticker root → external-user-friendly name.
# Match the root after removing the final two chars: month code + year digit.
# Do not add a "(CME)" suffix to avoid duplicating the channel marker in header.
_SYMBOL_FRIENDLY: dict[str, str] = {
    "BZ": "Brent Crude Oil",
    "CL": "WTI Crude Oil",
    "ES": "S&P 500 Futures",
    "NQ": "Nasdaq-100 Futures",
    "GC": "Gold Futures",
    "SI": "Silver Futures",
    "ZN": "10-Year T-Note",
    "ZB": "30-Year T-Bond",
}

# CME futures root → standard FinTwit cashtag, used with a leading "$".
# Goal: when users search $BZ / $CL on X, our post is discoverable too.
_CASHTAG_BY_CME_ROOT: dict[str, str] = {
    "BZ": "BZ",
    "CL": "CL",
    "ES": "ES",
    "NQ": "NQ",
    "GC": "GC",
    "SI": "SI",
    "ZN": "ZN",
    "ZB": "ZB",
}

# Channel + instrument category → hashtag appended to the body.
# X spam filters may downrank posts with 3+ hashtags, so allow at most one per
# channel/instrument.
# `#Insider` is added separately when LLM score≥7, keeping the total ≤2.
_HASHTAG_PER_CHANNEL: dict[str, str] = {
    "polymarket":   "#PredictionMarkets",
    "hyperliquid":  "#Crypto",
    "x":            "#FinTwit",  # X channel = generic insider-flow scraper
    "truth_social": "#TrumpTracker",
}

# CME hashtags vary by instrument root (oil/gold/equity).
_HASHTAG_PER_CME_ROOT: dict[str, str] = {
    "BZ": "#OilTrading",
    "CL": "#OilTrading",
    "GC": "#GoldTrading",
    "SI": "#GoldTrading",  # Silver is also in the metals category.
    "ES": "#Macro",
    "NQ": "#Macro",
    "ZN": "#Macro",
    "ZB": "#Macro",
}

# LLM suspicion threshold; add #Insider at the end when score is at or above it.
# User decision: 2026-05-04.
_INSIDER_HASHTAG_THRESHOLD: int = 7

# Channel labels dedicated to X headers, separate from email-subject SHORT aliases.
# User decision 2026-05-04: POLY/HL are unclear to external readers, so use full
# names. CME is already standard among traders, so keep it as-is.
_X_CHANNEL_DISPLAY: dict[str, str] = {
    "cme":          "CME",
    "polymarket":   "Polymarket",
    "hyperliquid":  "Hyperliquid",
    "x":            "X",
    "truth_social": "Truthsocial",
}

# Direction labels for Polymarket headers: use prediction-market-native terms
# instead of UP/DOWN. CME/Hyperliquid keep UP/DOWN as futures/crypto convention.
_POLYMARKET_DIRECTION_LABEL: dict[str, str] = {
    "UP":   "YES",
    "DOWN": "NO",
}

# Stop words for converting Polymarket slugs to Title Case. Keep lowercase;
# always capitalize the first word.
_TITLE_CASE_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in",
    "of", "on", "or", "the", "to", "vs", "via", "with",
})

# Common routing/geo prefixes at the start of Polymarket slugs. These are
# classification labels, not topic content, so drop them during prettifying.
_POLYMARKET_DROP_PREFIXES: frozenset[str] = frozenset({
    "us", "x", "world", "global", "crypto", "politics",
})

# Hyperliquid-specific ─────────────────────────────────────────────────
_RE_HL_INSIDER = re.compile(
    r"^INSIDER cond=(\d+)/(\d+) OIΔ=([+-]?[0-9.]+) "
    r"fund=([+-]?[0-9.]+) impact_ratio=([0-9.]+)$"
)
_RE_HL_NEW_WHALE = re.compile(
    r"^NEW_WHALE n24h=(\d+) max_cum5m=\$([0-9.]+)M$"
)
_RE_HL_CLUSTER = re.compile(
    r"^CLUSTER n_wallets=(\d+) sum=\$([0-9.]+)M$"
)
# X-channel-specific ───────────────────────────────────────────────────
_RE_X_ACCOUNTS = re.compile(r"^X_ACCOUNTS=(\d+)$")
_RE_X_MAGNITUDE = re.compile(r"^X_MAGNITUDE=(\d+)$")
_RE_X_WEIGHT = re.compile(r"^X_WEIGHT=([0-9.]+)$")
_RE_X_MATCHED = re.compile(r"^matched:(.+)$")

# Shared by CME / Polymarket ────────────────────────────────────────────
_RE_DIR = re.compile(r"^DIR_IMB=([+-]?\d+(?:\.\d+)?) RUN=(\d+)$")
_RE_WC = re.compile(
    r"^WC=([0-9.]+) \(n_wallets=(\d+) top=(\d+)% dir_ratio=(\d+)%\)$",
)
_RE_WC_BOOST = re.compile(r"^WC_BOOST\(score=([0-9.]+) n=(\d+)\)$")
_RE_VOL_Z = re.compile(r"^VOL_Z=([+-]?\d+(?:\.\d+)?)$")
_RE_VOL_TOD_Z = re.compile(r"^VOL_TOD_Z=([+-]?\d+(?:\.\d+)?) \(n=(\d+)\)$")
# P12-D Polymarket — vol_burst_abs_v1 / single_wallet_burst_v1 (absolute USD).
# format: "VOL_USD=$49.5K (n=2)" / "SW_BURST=$49.5K (n=2)" — K = thousand.
_RE_VOL_USD_ABS = re.compile(r"^VOL_USD=\$([0-9.]+)K \(n=(\d+)\)$")
_RE_SW_BURST = re.compile(r"^SW_BURST=\$([0-9.]+)K \(n=(\d+)\)$")
_RE_PRICE_JUMP_PCT = re.compile(r"^PRICE_JUMP_PCT_([15]M)=([+-]?\d+(?:\.\d+)?)%$")
_RE_PRICE_JUMP_RAW = re.compile(r"^PRICE_JUMP_1M=([+-]?\d+(?:\.\d+)?)$")
_RE_MID_JUMP = re.compile(r"^MID_JUMP_1M=([+-]?\d+(?:\.\d+)?)$")
_RE_CUSUM = re.compile(r"^CUSUM_(POS|NEG)=([+-]?\d+(?:\.\d+)?)$")
# P12-D CME — direction audit / multi-bucket fallback.
# format: "NET_CHANGE_2min=+0.11%" or "NET_CHANGE_5min=+0.44% (multi-bucket)".
_RE_NET_CHANGE = re.compile(
    r"^NET_CHANGE_(\d+min)=([+-]?\d+(?:\.\d+)?)%(?:\s*\((multi-bucket)\))?$"
)
# P12-D CME — aggressor breakdown.
# format: "AGGR_IMB_2min=-0.31 (buy=$6.9M sell=$13.2M)"
_RE_AGGR_IMB = re.compile(
    r"^AGGR_IMB_(\d+min)=([+-]?\d+(?:\.\d+)?)\s*"
    r"\(buy=\$([0-9.]+)M\s+sell=\$([0-9.]+)M\)$"
)

# v0.7.12 (user request) — user-facing conversion for the 4-condition codes in
# CME insider_v1. Keep internal codes unchanged for audit/log/test stability;
# translate to plain language only at render time.
_RE_CME_INSIDER_BUCKET = re.compile(r"^INSIDER_V1_BUCKET=(\d+min)$")
# C1_SIZE has two modes:
#   absolute_musd (BZ/CL): unit $M  → "C1_SIZE=43.4$M"
#   vol_z_5min   (ES/GC): unit σ   → "C1_SIZE=7.0σ"
_RE_CME_INSIDER_SIZE = re.compile(r"^C1_SIZE=([0-9.]+)(\$M|σ)$")
_RE_CME_INSIDER_RANGE = re.compile(
    r"^C2_RANGE=([0-9.]+)%\s*\((\d+min) high-low\)$"
)
# Note: ≥ is U+2265. cme_insider_v1.py emits this exact char in reason_code.
_RE_CME_INSIDER_COUNT = re.compile(r"^C3_COUNT=(\d+)\u2265(\d+)$")
_RE_CME_INSIDER_PERSIST = re.compile(r"^C4_PERSIST_PREV=([0-9.]+)$")


def render_channel_x_thread(
    *,
    signal: ChannelSignal,
    cooldown_reason: str,
    llm_assessment: AlertAssessment | None = None,
) -> RenderedXThread:
    """ChannelSignal -> X long-post (single tweet, X Premium 25k-char budget).

    User decision 2026-05-04 (after Premium Basic upgrade):
      Previously this was a 3-tweet thread (Header / Insider / Trader), but
      threads looked too messy on X, so it was consolidated into a single
      long-post. All analysis goes into one tweet.

    Layout:
        <CHANNEL_SHORT> | <pretty symbol> | <DIR> <score>
        Detectors:
        - <plain English label> = <formula value>
        - ...
        Analysis: <one-sentence summary>
        <Imbalance|Concentration>: <legend>          (conditional)

        Insider-trading suspicion: <score>/10
        - <bullet 1>
        - <bullet 2>
        - <bullet 3>
        [- <bullet 4>]                                (optional)
        [- <bullet 5>]                                (optional)

    `RenderedXThread.tweets` is always a length-1 tuple, preserving compatibility
    with the existing publisher thread-posting loop. Reply chaining is not needed
    because this is a single tweet.

    NOTE (2026-05-14 change): "Trader's view" used to be posted as well, but it
    was removed across all channels (X / Email / Telegram) because the
    insider-suspicion analysis was already sufficient and followers were unlikely
    to read it. The LLM still generates market_bullets for optionality, but they
    are not displayed.
    """
    _ = cooldown_reason  # Cooldown info has little meaning for external users.

    # ── Channel display name (X header only; separate from email subject) ──
    channel_label = _x_channel_display(signal.channel)

    # ── Instrument display name ─────────────────────────────────────────
    # Polymarket: slug → Title Case (`Iran Permanent Peace Deal by April
    # 22 2026`).
    # Truth Social: topic_slug → Title Case (`Rare Earth Export Ban`).
    # Other channels keep the existing friendly mapping (`BZ` → `Brent Crude Oil`).
    if signal.channel == "polymarket":
        pretty = _pretty_polymarket_slug(signal.symbol)
    elif signal.channel == CHANNEL_TRUTH_SOCIAL:
        pretty = _pretty_topic_slug(signal.symbol)
    else:
        pretty = _pretty_symbol(signal.symbol)

    # ── Direction label ─────────────────────────────────────────────────
    # Polymarket: UP→YES / DOWN→NO (prediction-market native term).
    # CME / Hyperliquid: keep UP / DOWN (futures/crypto convention).
    direction_str = _x_direction_label(signal.channel, signal.direction.value)

    # ── Build header (insert cashtag when available) ────────────────────
    cashtag = _cashtag_for_signal(signal.channel, signal.symbol)
    if cashtag:
        header = (
            f"{channel_label} | {cashtag} {pretty} "
            f"| {direction_str} {signal.score:.2f}"
        )
    else:
        header = (
            f"{channel_label} | {pretty} "
            f"| {direction_str} {signal.score:.2f}"
        )

    detector_lines = _render_detector_lines(signal.reason_codes)
    analysis = _render_analysis(signal.reason_codes)
    footnote = _render_footnote(signal.reason_codes)

    sections: list[str] = []
    sections.append(_build_header_section(
        header=header,
        detector_lines=detector_lines,
        analysis=analysis,
        footnote=footnote,
    ))

    if llm_assessment is not None:
        # Show only Insider-trading suspicion (3-5 bullets).
        # Per user decision 2026-05-14, market_bullets (Trader's view) are hidden
        # on all channels. See the docstring.
        insider_block = _format_bullet_section(
            title=f"Insider-trading suspicion: {llm_assessment.score}/10",
            bullets=llm_assessment.insider_bullets,
        )
        sections.append(insider_block)

    # Hashtag line for FinTwit reach, as its own one-line section.
    hashtag_line = _hashtag_line_for_signal(
        channel=signal.channel,
        symbol=signal.symbol,
        llm_assessment=llm_assessment,
    )
    if hashtag_line:
        sections.append(hashtag_line)

    # Separate sections with blank lines.
    body = "\n\n".join(sections)

    # Safety clamp for the 25k-char limit. It should practically never be hit.
    if len(body) > _X_MAX_CHARS:
        body = body[: _X_MAX_CHARS - 3].rstrip() + "..."

    return RenderedXThread(tweets=(body,))


def _build_header_section(
    *,
    header: str,
    detector_lines: list[str],
    analysis: str,
    footnote: str,
) -> str:
    """First section: Header + Detectors + Analysis + Imbalance/Concentration."""
    parts = [header, "Detectors:"]
    parts.extend(f"- {line}" for line in detector_lines)
    if analysis:
        parts.append(f"Analysis: {analysis}")
    if footnote:
        parts.append(footnote)
    return "\n".join(parts)


def _format_bullet_section(*, title: str, bullets: tuple[str, ...]) -> str:
    """Render a title plus bullet list as a plain string."""
    if not bullets:
        return title
    return "\n".join([title, *(f"- {b}" for b in bullets)])


# NOTE: Cascading degradation helpers from the old 280-char tweet era
# (_build_first_tweet, _render_bullet_tweet, _fit_tweet) were removed after the
# switch to X Premium long-posts (25k chars). See git history if needed.


# ─────────────────────────────────────────────────────────────────────
# FinTwit reach: builders for cashtags ($BZ) and hashtags (#OilTrading, etc.)
# ─────────────────────────────────────────────────────────────────────
def _x_channel_display(channel: str) -> str:
    """Channel display name for X headers. Unknown values fall back to upper(channel).

    Separate from `channel_short` (SHORT alias for email subjects): external X
    readers may not know what `POLY` / `HL` mean, so use full names.
    """
    return _X_CHANNEL_DISPLAY.get(channel, channel.upper())


def _x_direction_label(channel: str, direction_value: str) -> str:
    """Direction label: Polymarket uses YES/NO; all others use UP/DOWN."""
    upper = direction_value.upper()
    if channel == "polymarket":
        return _POLYMARKET_DIRECTION_LABEL.get(upper, upper)
    return upper


def _pretty_polymarket_slug(slug: str, max_len: int = 60) -> str:
    """Convert a Polymarket slug into human-readable Title Case.

    Example:
        "us-x-iran-permanent-peace-deal-by-april-22-2026"
            → "Iran Permanent Peace Deal by April 22 2026"

    Process:
      1) Split on `-`
      2) Drop leading routing prefixes (`us`, `x`, `world`, `crypto`, ...)
      3) Apply Title Case, keeping stop words like `by`/`of`/`to` lowercase
         except for the first word
      4) Keep numeric tokens (`22`, `2026`) unchanged
      5) Truncate and append `...` if over `max_len`

    Return the raw value when matching fails or input is empty.
    """
    s = (slug or "").strip().lower()
    if not s:
        return slug

    parts = [p for p in s.split("-") if p]
    # Remove leading routing prefixes. If all parts are prefixes, keep original.
    while parts and parts[0] in _POLYMARKET_DROP_PREFIXES:
        parts.pop(0)
    if not parts:
        return slug

    out: list[str] = []
    for i, token in enumerate(parts):
        # Keep purely numeric tokens unchanged (e.g. "22", "2026").
        if token.isdigit():
            out.append(token)
            continue
        # Keep stop words lowercase, except the first word is capitalized.
        if i > 0 and token in _TITLE_CASE_STOP_WORDS:
            out.append(token)
            continue
        out.append(token.capitalize())

    pretty = " ".join(out)
    if len(pretty) > max_len:
        pretty = pretty[: max_len - 3].rstrip() + "..."
    return pretty


def _cashtag_for_signal(channel: str, symbol: str) -> str:
    """Channel + instrument → FinTwit cashtag (e.g. `$BZ`), or empty string.

    Args:
        channel: signal.channel ("cme" / "hyperliquid" / "polymarket" / "x").
        symbol:  raw symbol (e.g. `BZQ5`, `BTC-USD`, `us-x-iran-...`).

    Returns:
        e.g. `$BZ`, `$BTC`. Return an empty string when no mapping exists, which
        omits the cashtag. Channels without tickers, such as Polymarket slugs,
        always return an empty string.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return ""

    if channel == "cme":
        # `BZQ5` → root=`BZ` → `$BZ`
        for root_len in (2, 1):
            if len(s) > root_len:
                root = s[:root_len]
                if root in _CASHTAG_BY_CME_ROOT:
                    return f"${_CASHTAG_BY_CME_ROOT[root]}"
        return ""

    if channel == "hyperliquid":
        # `BTC-USD` / `BTC-USDC` / `BTC` → `$BTC`
        # Remove any -USD/-USDC/-USDT suffix.
        base = s.split("-", 1)[0] if "-" in s else s
        # Safety guard: if base is too long, do not create a cashtag (likely slug).
        if 2 <= len(base) <= 6 and base.isalpha():
            return f"${base}"
        return ""

    # Polymarket slugs, X channel, etc.: no cashtag.
    return ""


def _hashtag_line_for_signal(
    *,
    channel: str,
    symbol: str,
    llm_assessment: AlertAssessment | None,
) -> str:
    """Build the one-line hashtag suffix for the body, or an empty string.

    Principles (user decision 2026-05-04):
      · At most one hashtag per channel/instrument to avoid X algorithm spam filters.
      · Add `#Insider` when LLM suspicion score ≥ 7 → maximum of 2 tags.
      · If there are 0 tags, omit the hashtag line entirely.

    Args:
        channel: signal.channel.
        symbol: signal.symbol.
        llm_assessment: If present, inspect score. If None, skip #Insider check.
    """
    tags: list[str] = []

    if channel == "cme":
        s = (symbol or "").strip().upper()
        for root_len in (2, 1):
            if len(s) > root_len:
                root = s[:root_len]
                tag = _HASHTAG_PER_CME_ROOT.get(root)
                if tag:
                    tags.append(tag)
                    break
    else:
        tag = _HASHTAG_PER_CHANNEL.get(channel)
        if tag:
            tags.append(tag)

    # Add #Insider when LLM suspicion score is at or above the threshold.
    if llm_assessment is not None and llm_assessment.score >= _INSIDER_HASHTAG_THRESHOLD:
        tags.append("#Insider")

    return " ".join(tags) if tags else ""


# ─────────────────────────────────────────────────────────────────────
# Symbol prettifier — CME contract code → friendly name
# ─────────────────────────────────────────────────────────────────────
# Convert a Truth Social topic slug to external-user-friendly Title Case.
# Example: "rare_earth_export_ban"  →  "Rare Earth Export Ban"
#     "iran_temp_ceasefire"    →  "Iran Temp Ceasefire"
#     "liberation_day"         →  "Liberation Day"
#
# Capitalize the first letter only (Title Case); no Polymarket-style stop-word
# handling. Trump post topics are usually short (1-4 words), so capitalizing
# every word reads naturally.
def _pretty_topic_slug(slug: str) -> str:
    s = (slug or "").strip()
    if not s:
        return slug
    words = [w for w in s.replace("-", "_").split("_") if w]
    if not words:
        return slug
    return " ".join(w[0].upper() + w[1:].lower() for w in words)


# Convert key_tickers from the Truth Social LLM ("SPY,QQQ,NVDA,MP") into X
# cashtag form ("$SPY, $QQQ, $NVDA, $MP") so X auto-hyperlinks them.
def _format_truth_social_tickers(raw: str) -> str:
    parts = [t.strip().upper() for t in raw.split(",") if t.strip()]
    return ", ".join(f"${t}" for t in parts)


def _pretty_symbol(symbol: str) -> str:
    """`BZQ5` → `Brent Crude Oil`. Unknown values remain raw.

    This targets external X users, so ticker codes (BZQ5) are not exposed.
    Internal operators / Email / Telegram use a separate layout with raw symbols.

    CME futures roots are usually the first 1-2 letters (BZ, CL, ES, NQ, GC...).
    """
    s = (symbol or "").strip().upper()
    if not s:
        return symbol
    for root_len in (2, 1):
        if len(s) > root_len:
            root = s[:root_len]
            label = _SYMBOL_FRIENDLY.get(root)
            if label:
                return label
    return _short_symbol(s, max_len=44)


def _symbol_with_friendly(symbol: str) -> str:
    """`BZQ5` → `BZQ5 (Brent Crude Oil)`, only when _SYMBOL_FRIENDLY maps it.

    For Email / Telegram operators: on channels where exposing tickers is OK,
    enrich with the friendly name in parentheses. Keep raw when there is no
    mapping (Polymarket slug, HL coin, etc.).

    Unlike `_pretty_symbol`, do not fallback on mapping misses; this avoids
    incorrect enrichment such as `us-x-iran-... (US-X-IRAN-...)`.
    """
    s = (symbol or "").strip()
    if not s:
        return symbol
    upper = s.upper()
    for root_len in (2, 1):
        if len(upper) > root_len:
            root = upper[:root_len]
            label = _SYMBOL_FRIENDLY.get(root)
            if label:
                return f"{s} ({label})"
    return s


# ─────────────────────────────────────────────────────────────────────
# Detector line renderer: show plain language and formula values together
# ─────────────────────────────────────────────────────────────────────
def _render_detector_lines(reasons: list[str]) -> list[str]:
    """reason_codes → ['<plain English label> = <formula value>', ...].

    This used to show only the first 3 items (legacy 280-char tweet behavior),
    but X Premium long-post / Email / Telegram now have enough budget to show
    every fired condition plus audit metrics (NET_CHANGE, AGGR_IMB).
    For NEUTRAL direction, NET_CHANGE must be visible to explain "why NEUTRAL";
    that is essential for auditability.

    Exclude codes already handled in the Analysis / Insider rows to avoid
    duplication:
      · ABSORPTION_* (Analysis row)
      · ANALYSIS=… / INSIDER_NOTE=… / INSIDER_SUSPICION=… / POST_URL=…
        (truth_social: each is displayed in a separate row)
    """
    if not reasons:
        return ["thresholds were exceeded"]
    out: list[str] = []
    for code in reasons:
        if code in _DETECTOR_LINE_SKIP:
            # Already handled in plain language in the Analysis row; avoid duplicate display.
            continue
        # Truth Social-only prefixes handled in separate rows.
        if code.startswith((
            "ANALYSIS=", "INSIDER_NOTE=", "INSIDER_SUSPICION=",
            "POST_URL=",
        )):
            continue
        line = _render_one_detector_line(code)
        if line:
            out.append(line)
    return out


# Markers handled in plain language in the Analysis row; avoid duplicate exposure
# in the Detectors row.
_DETECTOR_LINE_SKIP: frozenset[str] = frozenset({
    "ABSORPTION_BUYING",
    "ABSORPTION_SELLING",
})


def _render_one_detector_line(code: str) -> str:
    text = code.strip()

    m = _RE_VOL_TOD_Z.match(text)
    if m:
        z = float(m.group(1))
        n = int(m.group(2))
        return f"Volume vs same-minute baseline = {z:.1f}σ (n={n})"

    m = _RE_VOL_Z.match(text)
    if m:
        z = float(m.group(1))
        return f"Volume vs recent baseline = {z:.1f}σ"

    m = _RE_VOL_USD_ABS.match(text)
    if m:
        usd_k = float(m.group(1))
        n = int(m.group(2))
        return f"5-min volume = ${usd_k:.1f}K ({n} trades)"

    m = _RE_SW_BURST.match(text)
    if m:
        usd_k = float(m.group(1))
        n = int(m.group(2))
        return f"Single-wallet burst = ${usd_k:.1f}K ({n} trades)"

    m = _RE_PRICE_JUMP_PCT.match(text)
    if m:
        window = m.group(1).lower()
        pct = float(m.group(2))
        return f"{window} price change = {pct:+.2f}%"

    m = _RE_PRICE_JUMP_RAW.match(text)
    if m:
        jump = float(m.group(1))
        return f"1m price jump = {jump:+.3f}"

    m = _RE_MID_JUMP.match(text)
    if m:
        jump = float(m.group(1))
        return f"1m mid-price move = {jump:+.3f}"

    m = _RE_DIR.match(text)
    if m:
        imb = float(m.group(1))
        run = int(m.group(2))
        return f"Trade flow imbalance = {imb:+.2f} (run = {run})"

    m = _RE_WC.match(text)
    if m:
        score = float(m.group(1))
        n_w = int(m.group(2))
        top = int(m.group(3))
        dir_r = int(m.group(4))
        return (
            f"Wallet concentration = {score:.2f} "
            f"({n_w} wallets, top {top}%, same dir {dir_r}%)"
        )

    m = _RE_WC_BOOST.match(text)
    if m:
        score = float(m.group(1))
        n_w = int(m.group(2))
        return f"Concentration boost = {score:.2f} ({n_w} wallets)"

    m = _RE_CUSUM.match(text)
    if m:
        side = "Upside" if m.group(1) == "POS" else "Downside"
        val = float(m.group(2))
        return f"{side} cumulative drift = {val:+.3f}"

    # ── CME P12-D direction audit / aggressor breakdown ───────────
    m = _RE_NET_CHANGE.match(text)
    if m:
        window = m.group(1)
        pct = float(m.group(2))
        suffix = " (multi-bucket)" if m.group(3) else ""
        return f"{window} net price change = {pct:+.2f}%{suffix}"

    m = _RE_AGGR_IMB.match(text)
    if m:
        window = m.group(1)
        imb = float(m.group(2))
        buy_m = float(m.group(3))
        sell_m = float(m.group(4))
        return (
            f"{window} aggressor balance = {imb:+.2f} "
            f"(buy ${buy_m:.1f}M, sell ${sell_m:.1f}M)"
        )

    if text == "ABSORPTION_BUYING":
        return "Iceberg buying — price up but sell-side aggressors dominant"
    if text == "ABSORPTION_SELLING":
        return "Iceberg selling — price down but buy-side aggressors dominant"

    # ── CME insider_v1 4-condition codes (v0.7.12 user-facing conversion) ──
    m = _RE_CME_INSIDER_BUCKET.match(text)
    if m:
        return f"Anomaly window = {m.group(1)}"

    m = _RE_CME_INSIDER_SIZE.match(text)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        if unit == "$M":
            return f"Notional traded = ${val:.1f}M"
        # σ: ES/GC vol_z mode.
        return f"Volume z-score = {val:.1f}σ"

    m = _RE_CME_INSIDER_RANGE.match(text)
    if m:
        pct = float(m.group(1))
        win = m.group(2)
        return f"Price range = {pct:.2f}% ({win} high-low)"

    m = _RE_CME_INSIDER_COUNT.match(text)
    if m:
        actual = int(m.group(1))
        thr = int(m.group(2))
        return f"Trade count = {actual} (\u2265 {thr} threshold)"

    m = _RE_CME_INSIDER_PERSIST.match(text)
    if m:
        prev = float(m.group(1))
        return f"Prior window also active = ${prev:.1f}M"

    # ── Hyperliquid ────────────────────────────────────────────────
    m = _RE_HL_INSIDER.match(text)
    if m:
        cond_hit = int(m.group(1))
        cond_tot = int(m.group(2))
        oi_delta = float(m.group(3))
        funding = float(m.group(4))
        impact = float(m.group(5))
        oi_str = (
            f"{oi_delta/1_000_000:+.1f}M" if abs(oi_delta) >= 1_000_000
            else f"{oi_delta/1_000:+.0f}k"
        )
        return (
            f"Insider score = {cond_hit}/{cond_tot} "
            f"(OI Δ = {oi_str}, funding = {funding:+.4%}, "
            f"impact ratio = {impact:.2f})"
        )

    m = _RE_HL_NEW_WHALE.match(text)
    if m:
        n24 = int(m.group(1))
        cum = float(m.group(2))
        return (
            f"Fresh wallet flow = ${cum:.2f}M in 5min "
            f"({n24} new wallets in 24h)"
        )

    m = _RE_HL_CLUSTER.match(text)
    if m:
        n_w = int(m.group(1))
        s = float(m.group(2))
        return f"Wallet cluster = ${s:.2f}M across {n_w} wallets"

    # ── X channel ──────────────────────────────────────────────────
    m = _RE_X_ACCOUNTS.match(text)
    if m:
        return f"Distinct X accounts repeating signal = {int(m.group(1))}"

    m = _RE_X_MAGNITUDE.match(text)
    if m:
        return f"Aggregated post magnitude = {int(m.group(1))}"

    m = _RE_X_WEIGHT.match(text)
    if m:
        return f"Trust-weighted magnitude = {float(m.group(1)):.2f}"

    m = _RE_X_MATCHED.match(text)
    if m:
        return f"Pre-event narrative match: {m.group(1)}"

    if text == "pre_event_alert":
        return "Pre-event narrative alert (LLM matched historical case)"

    # ── Truth Social (Channel 5) ───────────────────────────────────
    if text.startswith("TOPIC="):
        # Convert snake_case to plain-language Title Case to match header pretty topic.
        return f"Topic = {_pretty_topic_slug(text.split('=', 1)[1])}"
    if text.startswith("CATEGORY="):
        # Category is a single word (tariff/iran/china/...), so capitalize it.
        return f"Category = {text.split('=', 1)[1].capitalize()}"
    if text.startswith("IMPACT_SCORE="):
        return f"Market impact score = {text.split('=', 1)[1]}"
    if text.startswith("SIMILAR="):
        sim = text.split("=", 1)[1]
        if sim and sim != "none":
            return f"Most similar past event = {sim}"
        return "Most similar past event = (none — novel topic)"
    if text.startswith("TICKERS="):
        # `$` cashtag prefix + space separator; X auto-hyperlinks these.
        return f"Key tickers = {_format_truth_social_tickers(text.split('=', 1)[1])}"

    # ── Shared / operational labels ─────────────────────────────────
    if text == "HOTFIX_EMERGENCY_GUARD":
        return "Single-detector emergency guard"
    if text == "EMERGENCY_AND_RULE_5M":
        return "5m flow + price confirmation rule"
    if text == "EMERGENCY_GUARD_DOWNGRADED":
        return "Emergency guard: tier downgraded (single-detector lacked corroboration)"
    if text == "PANIC_FILTER_DOWNGRADED":
        return "Panic-flow filter: tier downgraded (likely noise from panic)"
    if text.startswith("TV_TRIGGER:"):
        return text.replace("TV_TRIGGER:", "TradingView trigger: ")
    if text.startswith("SCHEDULED_"):
        return f"Scheduled macro window: {text.replace('SCHEDULED_', '')}"

    return text if len(text) <= 80 else text[:77] + "..."


# ─────────────────────────────────────────────────────────────────────
# One-line Analysis: plain-language one-line summary, separate from Detectors row
# ─────────────────────────────────────────────────────────────────────
def _render_analysis(reasons: list[str]) -> str:
    """Plain-language one-liner explaining the combined detector picture.

    Map wording by detector group for each channel, with set-style deduping so
    duplicate words do not appear when the same signal fires across channels.

    Truth Social (Channel 5) has no detector keyword and the LLM creates the
    rationale directly, so use the ANALYSIS= code value as-is.
    """
    if not reasons:
        return ""

    # ── Truth Social fast path: use the LLM rationale when ANALYSIS= exists.
    for r in reasons:
        if r.startswith("ANALYSIS="):
            txt = r.split("=", 1)[1].strip()
            if txt:
                return txt
            break

    # ── Shared (CME / Polymarket)
    has_vol = any(
        _RE_VOL_TOD_Z.match(r) or _RE_VOL_Z.match(r) or _RE_VOL_USD_ABS.match(r)
        for r in reasons
    )
    has_jump = any(
        _RE_PRICE_JUMP_PCT.match(r)
        or _RE_PRICE_JUMP_RAW.match(r)
        or _RE_MID_JUMP.match(r)
        for r in reasons
    )
    has_dir = any(_RE_DIR.match(r) for r in reasons)
    has_wc = any(_RE_WC.match(r) or _RE_WC_BOOST.match(r) for r in reasons)
    # P12-D Polymarket: one-shot single-wallet bet (n_trades<=3 + large USD).
    has_sw_burst = any(_RE_SW_BURST.match(r) for r in reasons)
    # P12-D CME: iceberg / absorption accumulation signature (direction and
    # aggressor signs mismatch).
    has_absorb_buy = "ABSORPTION_BUYING" in reasons
    has_absorb_sell = "ABSORPTION_SELLING" in reasons
    # ── Hyperliquid
    has_hl_insider = any(_RE_HL_INSIDER.match(r) for r in reasons)
    has_hl_new_whale = any(_RE_HL_NEW_WHALE.match(r) for r in reasons)
    has_hl_cluster = any(_RE_HL_CLUSTER.match(r) for r in reasons)
    # ── X channel
    has_x_repetition = any(_RE_X_ACCOUNTS.match(r) for r in reasons)
    has_x_match = any(
        _RE_X_MATCHED.match(r) or r == "pre_event_alert" for r in reasons
    )

    pieces: list[str] = []
    if has_vol:
        pieces.append("volume burst")
    if has_jump:
        pieces.append("price jump")
    if has_dir:
        pieces.append("one-way flow")
    if has_wc:
        pieces.append("wallet concentration")
    if has_sw_burst:
        pieces.append("single-wallet bet")
    if has_absorb_buy:
        pieces.append(
            "iceberg buying — price up while sell-side dominant "
            "(hidden bid absorbing)"
        )
    elif has_absorb_sell:
        pieces.append(
            "iceberg selling — price down while buy-side dominant "
            "(hidden ask absorbing)"
        )
    if has_hl_insider:
        pieces.append("insider-pattern accumulation (vol+OI+funding+stealth)")
    if has_hl_new_whale:
        pieces.append("fresh-wallet whale entry")
    if has_hl_cluster:
        pieces.append("fresh-wallet cluster betting")
    if has_x_match:
        pieces.append("pre-event narrative match")
    elif has_x_repetition:
        pieces.append("multi-account narrative repetition")

    if not pieces:
        return ""
    return " + ".join(pieces) + "."


# ─────────────────────────────────────────────────────────────────────
# Footnote: one-line interpretation guide for a selected metric
# ─────────────────────────────────────────────────────────────────────
def _render_footnote(reasons: list[str]) -> str:
    """For X posts: return one line as `<label>: <legend>`. (legacy interface)

    Internally builds (label, body) and combines them. Email/Telegram can call
    `_render_footnote_parts` directly and use label as the row label.
    """
    label, body = _render_footnote_parts(reasons)
    if not body:
        return ""
    return f"{label}: {body}"


def _render_footnote_parts(reasons: list[str]) -> tuple[str, str]:
    """One-line scale guide for the metric most likely to confuse external users.

    Returns:
        (label, body). If body is empty, omit the footnote entirely.

    Priority: DIR_IMB ("Imbalance") > WC ("Concentration") > none.
    """
    has_dir = any(_RE_DIR.match(r) for r in reasons)
    has_wc = any(_RE_WC.match(r) or _RE_WC_BOOST.match(r) for r in reasons)
    if has_dir:
        # Flow imbalance scale legend.
        # Semantics: −1 = all sells, 0 = balanced, +1 = all buys.
        return ("Imbalance", "+1=all buys, 0=balanced, −1=all sells.")
    if has_wc:
        return ("Concentration", "0=even, 1=single wallet.")
    return ("", "")


def _translate_reason_code(code: str) -> str:
    text = code.strip()

    m = _RE_DIR.match(text)
    if m:
        imb = float(m.group(1))
        run = int(m.group(2))
        return (
            f"one-way flow imbalance {imb:+.2f} with {run} same-side trades in a row"
        )

    m = _RE_WC.match(text)
    if m:
        score = float(m.group(1))
        n_wallets = int(m.group(2))
        top = int(m.group(3))
        dir_ratio = int(m.group(4))
        return (
            "wallet concentration "
            f"{score:.2f} (n={n_wallets}, top wallet {top}%, same direction {dir_ratio}%)"
        )

    m = _RE_WC_BOOST.match(text)
    if m:
        score = float(m.group(1))
        n_wallets = int(m.group(2))
        return f"concentration boost applied (score {score:.2f}, wallets {n_wallets})"

    m = _RE_VOL_TOD_Z.match(text)
    if m:
        z = float(m.group(1))
        n = int(m.group(2))
        return f"time-of-day normalized volume spike {z:.1f}σ (baseline n={n})"

    m = _RE_VOL_Z.match(text)
    if m:
        z = float(m.group(1))
        return f"volume is {z:.1f}σ above recent baseline"

    m = _RE_VOL_USD_ABS.match(text)
    if m:
        usd_k = float(m.group(1))
        n = int(m.group(2))
        return f"5-min volume reached ${usd_k:.1f}K across {n} trades"

    m = _RE_SW_BURST.match(text)
    if m:
        usd_k = float(m.group(1))
        n = int(m.group(2))
        return (
            f"single-wallet burst of ${usd_k:.1f}K in just {n} trade"
            f"{'s' if n != 1 else ''}"
        )

    m = _RE_PRICE_JUMP_PCT.match(text)
    if m:
        window = m.group(1)
        pct = float(m.group(2))
        return f"price moved {pct:.2f}% in {window.lower()}"

    m = _RE_PRICE_JUMP_RAW.match(text)
    if m:
        jump = float(m.group(1))
        return f"1m price jump {jump:.3f} in market odds"

    m = _RE_MID_JUMP.match(text)
    if m:
        jump = float(m.group(1))
        return f"mid-price moved {jump:.3f} in 1m"

    m = _RE_CUSUM.match(text)
    if m:
        side = "upside" if m.group(1) == "POS" else "downside"
        val = float(m.group(2))
        return f"cumulative drift on {side} reached {val:.3f}"

    # P12-D CME: plain-language conversion for direction audit / aggressor breakdown.
    m = _RE_NET_CHANGE.match(text)
    if m:
        window = m.group(1)
        pct = float(m.group(2))
        suffix = " (multi-bucket fallback)" if m.group(3) else ""
        return f"net price change over {window} reached {pct:+.2f}%{suffix}"

    m = _RE_AGGR_IMB.match(text)
    if m:
        window = m.group(1)
        imb = float(m.group(2))
        buy_m = float(m.group(3))
        sell_m = float(m.group(4))
        return (
            f"aggressor balance over {window} = {imb:+.2f} "
            f"(buy ${buy_m:.1f}M, sell ${sell_m:.1f}M)"
        )

    if text == "ABSORPTION_BUYING":
        return (
            "iceberg buying detected — price moved up despite sell-side "
            "aggressors dominating (likely hidden bid absorbing flow)"
        )
    if text == "ABSORPTION_SELLING":
        return (
            "iceberg selling detected — price moved down despite buy-side "
            "aggressors dominating (likely hidden ask distributing supply)"
        )

    # Soften internal-only markers into plain English.
    if text == "HOTFIX_EMERGENCY_GUARD":
        return "single-detector emergency guard was applied"
    if text == "EMERGENCY_AND_RULE_5M":
        return "emergency required both flow and 5m price confirmation"
    if text.startswith("TV_TRIGGER:"):
        return text.replace("TV_TRIGGER:", "TradingView trigger: ")
    if text.startswith("SCHEDULED_"):
        return f"scheduled macro window tag ({text.replace('SCHEDULED_', '')})"

    # Fallback: expose the raw code while keeping it from becoming too long.
    return text if len(text) <= 100 else text[:97] + "..."


def _cooldown_to_text(cooldown_reason: str) -> str:
    if cooldown_reason == "initial":
        return "first alert in cooldown window"
    if cooldown_reason.startswith("escalation"):
        return f"tier escalation ({cooldown_reason})"
    if cooldown_reason == "cooldown_expired":
        return "cooldown expired reminder"
    return cooldown_reason


def _short_symbol(symbol: str, max_len: int) -> str:
    if len(symbol) <= max_len:
        return symbol
    return symbol[: max_len - 3] + "..."


def _shrink_piece(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _first_reason_only(reasons: list[str]) -> str:
    if not reasons:
        return "thresholds exceeded"
    return _shrink_piece(_translate_reason_code(reasons[0]), 90)


# Public exports: expose helper aliases so channel_email / channel_telegram use
# the same detector wording. This is the single source of truth.
render_detector_lines = _render_detector_lines
render_analysis = _render_analysis
render_footnote = _render_footnote
render_footnote_parts = _render_footnote_parts
pretty_symbol = _pretty_symbol
symbol_with_friendly = _symbol_with_friendly

__all__ = [
    "RenderedXThread",
    "render_channel_x_thread",
    "render_detector_lines",
    "render_analysis",
    "render_footnote",
    "render_footnote_parts",
    "pretty_symbol",
    "symbol_with_friendly",
    "friendly_detector_name",
    "friendly_detector_list",
]

