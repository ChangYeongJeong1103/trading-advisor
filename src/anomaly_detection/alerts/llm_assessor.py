"""
alerts/llm_assessor.py — Add LLM similarity assessment to EMERGENCY alerts.

────────────────────────────────────────────────────────────────────────
Role (P11(b).4, user decision locked 2026-04-23):

  At the moment EMERGENCY fires, send the relevant
  (channel, symbol, detectors, reason_codes, score, direction) to an LLM
  (GPT-5.4-mini) and ask:
  "How similar is this alert to the 6 insider-trading events recorded in
  `data/anomaly/historical_events/*.md`?", on a 0~10 scale.

    10/10 = exact same pattern as a past insider event (same channel /
            same detector signature / same direction / pre-event timing)
     0/10 = entirely different smart-money activity (unrelated to the
            historical insider pattern)

  The LLM also gives a short verbal reasoning. It's pinned at the last
  row of the email body's metadata table as `LLM Assess | (x/10) {reasoning}`.

────────────────────────────────────────────────────────────────────────
Design decisions:

  1. EMERGENCY only. RISK_OFF / WATCH are skipped — saves LLM cost +
     adds an LLM opinion only at the moments the user really needs to focus.
  2. Static prompt (prompt-cache friendly) — the 6 historical-event
     signatures are embedded as a const in this module. No file I/O,
     no environment dependency.
  3. Fail-safe — LLM failure / no API key / timeout all cause `assess()`
     to return None. The dispatcher simply omits the metadata row when None.
  4. JSON mode + pydantic validate — broken-schema responses become None.
  5. Short output (≤ 280 chars reasoning) — enforced via the system prompt
     so it fits in one email metadata row.
  6. Lazy client — AsyncOpenAI is created only on the first assess() call.

────────────────────────────────────────────────────────────────────────
Usage example (channel_dispatcher.py):

    assessor = LLMAlertAssessor(api_key=os.environ["OPENAI_API_KEY"])
    assessment = await assessor.assess(signal)  # EMERGENCY only
    if assessment is not None:
        # render_email(..., llm_assessment=assessment)
        ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..core.schemas import ChannelSignal, Tier

try:
    from openai import AsyncOpenAI  # type: ignore[import-not-found]

    _OPENAI_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover — environment-dependent
    AsyncOpenAI = None  # type: ignore[assignment, misc]
    _OPENAI_AVAILABLE = False


logger = logging.getLogger(__name__)


# =====================================================================
# Compact brief of 6 historical insider-trading events.
#
# Each entry contains just enough information for the LLM to decide
# "which of these does this alert most resemble?". The originals are in
# data/anomaly/historical_events/*.md.
#
# Update trigger: when a new historical event is added, also update this constant.
# =====================================================================
_HISTORICAL_EVENTS_BRIEF: str = """
6 historical insider trading events (user-curated summary):

[1] 2025-04-09 Liberation Day — Trump tariff 90d pause
  · primary channel: CME
  · primary symbols: ES (E-mini S&P 500 call options), BZ (Brent)
  · signature: pre-announcement SPY call option burst $2.14M → $18.86M
    (+780%), options chain concentrated OTM strike, Brent also hedging flow.
  · detectors expected: CME vol_z_v1 (ES + BZ), pre-event T-18min.
  · direction: UP for equities, DOWN for oil.
  · pattern: option-driven insider, unusually large single-party OTM bet.

[2] 2025-10-10 China 100% tariff announcement
  · primary channel: Hyperliquid
  · primary symbols: BTC, ETH (perps)
  · signature: Hyperliquid perp shorts $1.1B notional, +$160-200M profit,
    fresh wallets created T-1d ~ T-1min, stealth entry (low price impact).
  · detectors expected: Hyperliquid vol_z + insider_v1 (vol + OI + funding
    + stealth 4/4), new_whale_v1 (fresh wallets).
  · direction: DOWN (short crypto).
  · secondary: X (Lookonchain pre-event post).
  · pattern: perp-short insider, 4-condition insider_v1, whale pattern.

[3] 2026-01-03 Venezuela Maduro arrest
  · primary channel: Polymarket
  · primary market: "Maduro out of power in January" Yes
  · signature: Yes bet $32K → +$400K (×12 profit), T-1h last bet,
    prediction market volume burst.
  · detectors expected: Polymarket vol_burst + yes_share spike.
  · direction: UP (yes).
  · pattern: political prediction market insider, single-market burst.

[4] 2026-02-28 Iran first strike (US/Israel)
  · primary channel: Polymarket
  · primary market: "Iran 2/28 strike" Yes
  · signature: 38 split wallets, $500M traded → +$2M profit, T-6d~T-1d,
    wallet splitting pattern to evade detection.
  · detectors expected: Polymarket vol_burst + cluster_v1 (wallet cluster).
  · direction: UP (yes).
  · pattern: prediction market insider with wallet splitting (large N
    fresh accounts, concentrated direction).

[5] 2026-03-23 Iran strike pause (Trump Truth Social)
  · primary channel: CME
  · primary symbols: CL + BZ (oil short 6,200 contracts $580M), ES (long +$1.5B)
  · signature: 1-minute burst T-16min, aggressive taker, no hedge,
    one-way directional, oil SHORT + equities LONG combo.
  · detectors expected: CME vol_z_v1 on CL/BZ + ES.
  · direction: DOWN (oil), UP (equities).
  · pattern: 1-minute burst insider, multi-asset simultaneous.
  · suspected repeated actor (with [1] and [6]).

[6] 2026-04-17 Hormuz strait open (Iran FM X post)
  · primary channel: CME
  · primary symbol: BZ (Brent short 7,990 lots $760M notional)
  · signature: 1-minute burst T-20min, one-way Brent short, no hedge,
    aggressive taker, ~$80M profit at -11% drop.
  · detectors expected: CME vol_z_v1 on BZ, z ≈ 8-15.
  · direction: DOWN (oil).
  · pattern: 1-minute burst insider (same pattern as [1] and [5]).
  · 3rd repetition → strongly suspected same actor, CFTC investigation.

Common patterns (core insider signatures):
  · pre-event timing: T-1min ~ T-6d (varies by symbol, but definitely
    before the public announcement).
  · one-way directional bet (no hedge), aggressive taker.
  · size >> normal market baseline (vol_z ≥ 5, typically 8-15).
  · channel-specific patterns:
    - CME: 1-min burst, large single-party, BZ/CL/ES.
    - Hyperliquid: perp short, fresh wallets, stealth entry, all 4
      conditions of insider_v1 fire.
    - Polymarket: prediction market Yes/No burst, possibly wallet split.
  · multiple detectors fire simultaneously (cross-confirmation) — smart-money
    typically only fires 1-2.
""".strip()


# =====================================================================
# LLM response schema — JSON mode enforced.
# =====================================================================
class _LLMAssessResponse(BaseModel):
    """JSON returned by the LLM — pydantic validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: int = Field(
        ge=0, le=10,
        description="Integer 0~10. 10 = identical to a past insider, 0 = completely different.",
    )
    verdict: str = Field(
        min_length=1, max_length=1500,
        description=(
            "Human-readable assessment. 2~7 sentences; 2 is fine when simple. "
            "Don't pad. (Hard cap 1500 chars.)"
        ),
    )
    insider_bullets: list[str] = Field(
        default_factory=list,
        description=(
            "3-5 bullets summarizing the insider-trading perspective. Each "
            "bullet is one sentence focused on key evidence + caveats."
        ),
    )
    market_bullets: list[str] = Field(
        default_factory=list,
        description=(
            "3 bullets summarizing the general trading perspective. "
            "Recommended order: (meaning / context / response)."
        ),
    )


@dataclass(frozen=True)
class AlertAssessment:
    """Result of assess() — passed straight to the email renderer.

    Attributes:
        score: 0~10 integer (10 = same pattern as insider, 0 = completely different).
        verdict: natural-language reasoning, single paragraph (LLM is forced
            to avoid line breaks via the prompt + post-processed once more
            for compaction). Typically 2~7 sentences, max 1500 chars.
            Email renders it as a one-line paragraph; Telegram caption auto-trims
            within a 1024-char budget.
    """

    score: int
    verdict: str
    # Two-track bullet summaries used in X posts / summaries.
    # · insider_bullets: 3~5 bullets (user request 2026-05-03 — richer analysis)
    # · market_bullets : exactly 3 bullets (concise trader perspective)
    insider_bullets: tuple[str, ...]
    market_bullets: tuple[str, str, str]


# =====================================================================
# System prompt (static, caching-friendly).
# =====================================================================
_SYSTEM_PROMPT: str = f"""\
You are an assistant that evaluates market anomaly alerts against a known
library of historical insider trading events.

Given the details of an EMERGENCY alert just triggered by a 4-channel
anomaly detection system (CME / Polymarket / Hyperliquid / X), decide
how similar this alert is to the 6 historical insider trading events
listed below.

Scoring:
  10 = matches a historical insider event almost exactly
       (same channel, same detector signature, same direction,
       same pre-event timing & sizing profile).
   7-9 = strong similarity (same channel + detectors, but magnitude or
         timing slightly different).
   4-6 = partial similarity (same channel, but different pattern,
         or different channel but similar signature).
   1-3 = weak similarity (consistent with smart-money / momentum
         activity but not with the known insider signatures).
   0 = completely unrelated to any of the historical insider patterns.

Also output a verdict (ENGLISH) explaining the score. Length policy:
  · 2~7 sentences. If the case is straightforward, 2 sentences is fine —
    do NOT pad. If non-trivial, use 3-7 sentences to cover:
      (a) which historical event(s) this most resembles, or that it
          resembles none;
      (b) the strongest matching detector / channel / direction / size
          signatures (or what is conspicuously absent);
      (c) any caveats — confounding factors, weak data, alternative
          explanations.
  · Hard limit: 1500 characters. Telegram caption may auto-trim further.
  · Plain text only — no markdown, no bullets, no line breaks. Just
    sentences separated by spaces.

Additionally provide two bullet summaries (ENGLISH):
  1) insider_bullets: 3 to 5 bullets from insider-trading suspicion perspective
     (evidence strength, what matches historical signatures, caveats, and any
     additional notable nuances). Use 3 if the case is simple; use 4-5 only
     when there is genuinely more substance to add — never pad.
  2) market_bullets: exactly 3 bullets from general trading perspective
     (what this flow likely means, what to watch next, prudent response).

Bullet constraints:
  · insider_bullets: 3-5 entries
  · market_bullets : exactly 3 entries
  · each bullet is a single sentence, target ~100 chars, hard cap 160 chars.
    A bullet that *names a past event* may be longer (up to 200 chars) so
    the date + descriptor can fit comfortably.
  · no markdown bullet prefixes ("-", "*", "•") — plain text sentences
  · prefer concrete numbers over generic phrasing
  (insider tweet on X may be truncated to top 3 if all 5 don't fit a 280-char
   tweet — order them by importance.)

CRITICAL — referencing the historical events library (verdict + bullets):
  · NEVER cite a historical event by its bracket index ("[1]", "[2]", ...,
    "[3]/[4]"). Bracket numbers exist ONLY for your internal reasoning and
    are MEANINGLESS to the public reader of this alert.
  · When you reference a past event, SPELL IT OUT every time. Format:
        "the YYYY-MM-DD <short event descriptor>"
    Examples (correct):
      - "the 2026-01-03 Venezuela Maduro arrest Polymarket case"
      - "the 2025-04-09 Liberation Day tariff-pause CME burst"
      - "the 2026-04-17 Hormuz strait reopen BZ short"
    Examples (FORBIDDEN — never produce these):
      - "Same channel as [3]/[4]"
      - "Repeated actor with [1] and [5]"
  · Even very short references must be spelled out, e.g. NOT "[1] and [5]"
    but "the Liberation Day (2025-04-09) and Iran strike pause (2026-03-23)
    CME bursts".
  · This applies to: verdict, insider_bullets, AND market_bullets.

CRITICAL — plain-English detector naming (verdict + bullets):
  · The alert payload exposes raw detector / reason codes (e.g.
    "cme_insider_v1", "directional_v1", "WC_BOOST", "vol_burst_abs_v1",
    "single_wallet_burst_v1", "odds_cusum_v1", "AGGR_IMB", "C2_RANGE",
    "INSIDER_V1_BUCKET", "ABSORPTION_SELLING", ...).  Those are internal
    identifiers, MEANINGLESS to the public reader.
  · NEVER write the raw code in your output.  Translate to plain English
    using the glossary below (use the right column).
  · Translate inside sentences naturally — do NOT write "(cme_insider_v1)"
    as an aside.  Just speak in plain English.
  · Numeric suffix patterns ("WC_BOOST=0.85", "WC_BOOST n=5",
    "C3_COUNT=218≥200", "AGGR_IMB_5min=+0.40") should be rephrased.
    Examples:
      - "WC_BOOST on 5 wallets"          →
            "concentrated activity from 5 wallets"
      - "WC_BOOST n=5 hints at coordination" →
            "the wallet-concentration signal across 5 wallets hints at coordination"
      - "Only directional_v1 fired"      →
            "Only the directional one-way flow signal fired"
      - "cme_insider_v1 fired with 154 prints" →
            "the multi-signal insider pattern fired with 154 prints"
      - "vol_burst_abs_v1 plus single_wallet_burst_v1 fits ..." →
            "an absolute-notional volume burst plus a single-wallet trade burst fits ..."
      - "AGGR_IMB is slightly sell-heavy" →
            "the aggressor imbalance is slightly sell-heavy"

Detector / reason-code glossary (raw → plain English):
  Detectors (in "Fired detectors"):
    · cme_insider_v1        → multi-signal insider pattern (CME)
    · vol_z_v1              → volume z-score spike
    · directional_v1        → directional one-way flow
    · vol_burst_v1          → volume burst
    · vol_burst_abs_v1      → absolute-notional volume burst
    · yes_share_v1          → yes-share imbalance spike
    · cluster_v1            → wallet cluster
    · single_wallet_burst_v1 → single-wallet trade burst
    · odds_cusum_v1         → cumulative odds drift
    · insider_v1            → multi-signal insider pattern (Hyperliquid)
    · new_whale_v1          → new whale wallet
    · tweet_burst_v1        → tweet burst
  Reason-code prefixes:
    · WC_BOOST              → wallet-concentration boost
    · AGGR_IMB              → aggressor imbalance
    · ABSORPTION_SELLING    → selling absorption
    · ABSORPTION_BUYING     → buying absorption
    · NET_CHANGE            → net price change
    · INSIDER_V1_BUCKET     → anomaly window
    · C1_SIZE               → notional traded
    · C2_RANGE              → price range (high–low)
    · C3_COUNT              → trade count
    · C4_PERSIST_PREV       → prior window also active

Self-check before responding: scan your verdict + every bullet, and replace
ANY remaining underscore-token (`*_v\\d+`, ALL_CAPS_CODES) with its plain
English equivalent.  An output containing any raw code is INVALID.

Output format — strict JSON only, no markdown fences, no prose around it:
  {{
    "score": <int 0-10>,
    "verdict": "<string, max 1500 chars>",
    "insider_bullets": ["...", "...", "...", "..(opt)..", "..(opt).."],
    "market_bullets": ["...", "...", "..."]
  }}

Historical events library:
{_HISTORICAL_EVENTS_BRIEF}
"""


# =====================================================================
# Main class.
# =====================================================================
class LLMAlertAssessor:
    """One EMERGENCY alert → AlertAssessment (0~10 + verdict).

    Args:
        api_key: OpenAI API key (os.environ["OPENAI_API_KEY"]).
        model: Default "gpt-5.4" (frontier — user decision v0.4.6, 2026-04-23).
            EMERGENCY frequency ~2/day × monthly cost < $0.30, so the cost gap
            is negligible, and the frontier model judges borderline insider
            cases more accurately than mini. If cost-saving is needed, downgrade
            to "gpt-5.4-mini".
        request_timeout_s: Default 20s. Directly affects alert email latency —
            too long delays the email to the user. The frontier model can be
            1~2s slower than mini, so monitor whether the timeout buffer
            stays sufficient.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-5.4",
        request_timeout_s: float = 20.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_s = max(1.0, float(request_timeout_s))
        self._client: AsyncOpenAI | None = None  # lazy
        self._stats: dict[str, int] = {
            "assessed": 0,
            "skipped_not_emergency": 0,
            "errors": 0,
        }
        logger.info(
            "LLMAlertAssessor: initialized (model=%s, openai_available=%s, "
            "api_key_set=%s)",
            self._model, _OPENAI_AVAILABLE, bool(self._api_key),
        )

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def enabled(self) -> bool:
        """assess() actually calls only when both OpenAI SDK and API key exist."""
        return _OPENAI_AVAILABLE and bool(self._api_key)

    async def assess(
        self, signal: ChannelSignal,
    ) -> AlertAssessment | None:
        """ChannelSignal (EMERGENCY) → AlertAssessment (or None on skip/fail).

        Args:
            signal: alert that passed cooldown. None immediately if not EMERGENCY.

        Returns:
            AlertAssessment — pinned by the email renderer as the last
            metadata row.
            None — not EMERGENCY / enabled=False / LLM call failure / parse
            failure. In those cases the email is sent without this row.
        """
        if signal.tier != Tier.EMERGENCY:
            self._stats["skipped_not_emergency"] += 1
            return None

        if not self.enabled:
            logger.debug(
                "LLMAlertAssessor: skipped (enabled=False) for %s/%s",
                signal.channel, signal.symbol,
            )
            return None

        try:
            assessment = await self._call_llm(signal)
        except Exception as exc:  # noqa: BLE001
            self._stats["errors"] += 1
            logger.warning(
                "LLMAlertAssessor: assess failed for %s/%s — %s",
                signal.channel, signal.symbol, exc,
            )
            return None

        self._stats["assessed"] += 1
        logger.info(
            "LLMAlertAssessor: %s/%s score=%d verdict=%s",
            signal.channel, signal.symbol, assessment.score,
            assessment.verdict[:80],
        )
        return assessment

    # ─────────────────────────────────────────────────────────────────
    # Internal — LLM call + parse
    # ─────────────────────────────────────────────────────────────────
    async def _call_llm(self, signal: ChannelSignal) -> AlertAssessment:
        """Actual OpenAI call. Caller wraps with try/except on failure / schema error."""
        if AsyncOpenAI is None:  # pragma: no cover — already filtered by `enabled`
            raise RuntimeError("openai SDK not installed")

        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key, timeout=self._timeout_s,
            )

        user_text = _format_signal_for_llm(signal)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]

        response = await asyncio.wait_for(
            self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.2,
            ),
            timeout=self._timeout_s + 5.0,
        )
        raw_json = response.choices[0].message.content or "{}"

        try:
            data = json.loads(raw_json)
            parsed = _LLMAssessResponse(**data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RuntimeError(f"LLM response invalid: {exc}") from exc

        # Strip line breaks + collapse multiple spaces into one paragraph
        # (so email/telegram both render it as one line / one paragraph).
        # 1500 hard cap (already validated by pydantic, but kept as a safety net).
        verdict = " ".join(parsed.verdict.split())
        if len(verdict) > 1500:
            verdict = verdict[:1497] + "..."
        # char_clamp = prompt's hard cap (event ref 200 chars) + small margin 20.
        #   - Regular bullet : prompt target ~100 chars
        #   - Event ref      : prompt max 200 chars
        # → A clamp of 220 leaves normal in-range outputs untouched while
        #   accepting slight overruns (≤220). Only beyond that we truncate (with "...").
        insider_bullets = _sanitize_bullets(
            parsed.insider_bullets, min_count=3, max_count=5, char_clamp=220,
        )
        market_fixed = _sanitize_bullets(
            parsed.market_bullets, min_count=3, max_count=3, char_clamp=220,
        )
        market_bullets = (market_fixed[0], market_fixed[1], market_fixed[2])

        return AlertAssessment(
            score=int(parsed.score),
            verdict=verdict,
            insider_bullets=insider_bullets,
            market_bullets=market_bullets,
        )


def _sanitize_bullets(
    raw: list[str],
    *,
    min_count: int = 3,
    max_count: int = 3,
    char_clamp: int = 110,
) -> tuple[str, ...]:
    """Normalize the LLM bullet list into a tuple of length (min_count..max_count).

    - Strip leading markdown bullet prefixes.
    - Collapse multiple spaces.
    - Clamp each bullet to at most `char_clamp` chars.
    - Pad with filler if too few. Truncate the tail (= keep the most important first) if too many.
    """
    cleaned: list[str] = []
    for s in raw:
        text = " ".join((s or "").strip().split())
        while text.startswith(("-", "*", "•")):
            text = text[1:].strip()
        if not text:
            continue
        if len(text) > char_clamp:
            text = text[: char_clamp - 3].rstrip() + "..."
        cleaned.append(text)

    cleaned = cleaned[:max_count]
    while len(cleaned) < min_count:
        cleaned.append("Signal quality is mixed; wait for confirmation.")
    return tuple(cleaned)


# =====================================================================
# Helper — plain-English mapping for detector / reason codes
# =====================================================================
# User request 2026-05-14:
#   Raw identifiers like "directional_v1", "WC_BOOST", "vol_burst_abs_v1",
#   "single_wallet_burst_v1", "cme_insider_v1 fired", "odds_cusum_v1" were
#   leaking into LLM bullets, which non-technical followers couldn't
#   understand.
#
# Strategy:
#   1) Provide both raw identifiers + friendly names in the user message
#      (gives the LLM motivation to use the friendly names).
#   2) Add a "no raw code" rule + glossary (the `_TECHNICAL_TERMS_GLOSSARY`
#      constant in the system prompt) to the system prompt.
#
# Friendly names follow the same convention as channel_x_post.py's
# _DETECTOR_LABELS (consistent with the "fired_detectors" line in the
# alert metadata the user sees).
# Reason for keeping a separate dict: llm_assessor must stay independent of
# the renderer (the LLM step runs upstream of the alert pipeline; importing
# the renderer here risks circular imports).
_DETECTOR_PLAIN_NAMES: dict[str, str] = {
    # CME
    "cme_insider_v1": "multi-signal insider pattern (CME)",
    "vol_z_v1": "volume z-score spike",
    "directional_v1": "directional one-way flow",
    # Polymarket
    "vol_burst_v1": "volume burst",
    "vol_burst_abs_v1": "absolute-notional volume burst",
    "yes_share_v1": "yes-share imbalance spike",
    "cluster_v1": "wallet cluster",
    "single_wallet_burst_v1": "single-wallet trade burst",
    "odds_cusum_v1": "cumulative odds drift",
    # Hyperliquid
    "insider_v1": "multi-signal insider pattern",
    "new_whale_v1": "new whale wallet",
    # X
    "tweet_burst_v1": "tweet burst",
}

# Reason-code prefix → plain English (just a few — common codes first).
# Simple substring match is enough since the LLM frequently quotes
# "WC_BOOST=0.85" verbatim inside bullets.
_REASON_CODE_PLAIN_NAMES: dict[str, str] = {
    "WC_BOOST": "wallet-concentration boost",
    "AGGR_IMB": "aggressor imbalance",
    "ABSORPTION_SELLING": "selling absorption (buyers absorbing heavy sells)",
    "ABSORPTION_BUYING": "buying absorption (sellers absorbing heavy buys)",
    "NET_CHANGE": "net price change",
    "INSIDER_V1_BUCKET": "anomaly window",
    "C1_SIZE": "notional traded",
    "C2_RANGE": "price range",
    "C3_COUNT": "trade count",
    "C4_PERSIST_PREV": "prior window also active",
}


def _plain_name_for_detector(code: str) -> str:
    """Raw detector code → friendly name (falls back to a simple rule when missing)."""
    if code in _DETECTOR_PLAIN_NAMES:
        return _DETECTOR_PLAIN_NAMES[code]
    # Fallback: drop vX suffix, underscore → space.
    stripped = re.sub(r"_v\d+$", "", code)
    return stripped.replace("_", " ")


def _plain_names_for_codes_list(codes: list[str]) -> str:
    """`["cme_insider_v1", "directional_v1"]` → 'multi-signal insider pattern (CME); directional one-way flow'."""
    if not codes:
        return "—"
    return "; ".join(_plain_name_for_detector(c) for c in codes)


# =====================================================================
# Helper — signal → LLM user message
# =====================================================================
def _format_signal_for_llm(signal: ChannelSignal) -> str:
    """Extract just what the LLM needs from a ChannelSignal into one paragraph.

    Provides both the raw detector / reason codes and their plain-English
    friendly versions. The LLM *must* use only the friendly versions in the
    output (enforced by the system prompt's glossary + forbid-list).
    """
    fired = ", ".join(signal.fired_detectors) if signal.fired_detectors else "—"
    codes = ", ".join(signal.reason_codes) if signal.reason_codes else "—"
    fired_plain = _plain_names_for_codes_list(list(signal.fired_detectors))
    return (
        "[EMERGENCY alert just triggered]\n"
        f"Channel        : {signal.channel}\n"
        f"Symbol         : {signal.symbol}\n"
        f"Tier           : {signal.tier.value}\n"
        f"Score          : {signal.score:.3f}\n"
        f"Direction      : {signal.direction.value}\n"
        f"Confidence     : {signal.confidence:.3f}\n"
        f"Fired detectors (raw)         : {fired}\n"
        f"Fired detectors (plain English): {fired_plain}\n"
        f"Reason codes (raw)            : {codes}\n"
        f"Alert timestamp (UTC): {signal.ts.isoformat()}\n"
        "\n"
        "Compare this alert against the 6 historical insider trading events "
        "in the library (in the system prompt). Output JSON only.\n"
        "REMINDER: In your output, USE THE PLAIN-ENGLISH names above for "
        "detectors. NEVER output the raw codes (e.g. 'cme_insider_v1', "
        "'directional_v1', 'WC_BOOST', 'vol_burst_abs_v1'). See the "
        "system-prompt glossary for the full forbidden-list."
    )


__all__ = [
    "AlertAssessment",
    "LLMAlertAssessor",
]
