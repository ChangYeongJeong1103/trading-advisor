"""
truth_social/llm_scorer.py — GPT-based market-impact scorer (Step 3).

────────────────────────────────────────────────────────────────────────
Responsibilities:

  Take one new Trump Truth Social post and ask OpenAI GPT-5.4:

    "How immediately/impactfully will this post affect US equity markets
     (S&P/NASDAQ) compared to historical events in the reference DB?"

  Result:
    MarketImpactScore {
        score: int 0-10,
        confidence: low|medium|high,
        category: tariff|iran|china|fed|...
        direction: bull|bear|neutral|mixed,
        rationale: str (1-3 sentences),
        key_tickers: list[str] (e.g. ["SPY", "QQQ", "NVDA"]),
        most_similar_event_id: str (which reference DB event is most similar),
    }

────────────────────────────────────────────────────────────────────────
Design decisions (mirroring the X channel's LLMClassifier pattern):

  1. **JSON mode + Pydantic validate**:
     response_format={"type": "json_object"} → forced schema → safe parsing.
     On failure, score=0 NORMAL fallback (false negative safer than false positive).

  2. **Few-shot in-prompt**:
     Include (post, score, category, market reaction summary) for top-K references
     retrieved by ReferenceDB.retrieve() in the prompt.
     OpenAI's prefix cache reduces the system_prompt portion cost by 90%.

  3. **Async + lazy openai import**:
     Module import succeeds even without the openai package (test env compatible).

  4. **Per-post result cache (in-memory TTL 1h)**:
     The LLM is called only once even if the same post_id arrives twice.

────────────────────────────────────────────────────────────────────────
Score → Tier mapping (user decision):

  · 9-10 → Tier.EMERGENCY  (Email + Telegram + X)
  · 7-8  → Tier.RISK_OFF   (Email only)
  · 5-6  → Tier.WATCH      (emit signal, no alert — dispatcher email_min_tier=RISK_OFF)
  · 0-4  → Tier.NORMAL     (no signal emit)

  ChannelSignal.score [0,1] = llm_score / 10.0.

────────────────────────────────────────────────────────────────────────
Env vars / config:

  · OPENAI_API_KEY (required; missing disables the channel entirely)
  · Model: default "gpt-5.4" (one notch above X channel — Trump posts are
    longer in context with more policy nuance than X. Cost diff is negligible).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...core.schemas import (
    CHANNEL_TRUTH_SOCIAL,
    ChannelSignal,
    Direction,
    Tier,
)
from .normalize import TruthPost
from .reference_db import RetrievedReference, TruthSocialReferenceDB

# Lazy-import the OpenAI SDK — module import works even without the package installed.
try:
    from openai import APIError       # type: ignore[import-not-found]
    from openai import APITimeoutError  # type: ignore[import-not-found]
    from openai import AsyncOpenAI     # type: ignore[import-not-found]
    from openai import RateLimitError  # type: ignore[import-not-found]

    _OPENAI_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore[assignment, misc]
    APIError = Exception  # type: ignore[assignment, misc]
    APITimeoutError = Exception  # type: ignore[assignment, misc]
    RateLimitError = Exception  # type: ignore[assignment, misc]
    _OPENAI_AVAILABLE = False


logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-5.4"
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_CACHE_TTL_S = 3600.0  # post_id cache: 1h


# ====================================================================
# LLM response schema (forced JSON mode)
# ====================================================================
class MarketImpactScore(BaseModel):
    """Validated form of the JSON returned by GPT.

    1:1 with the output_schema embedded in the LLM prompt. Update prompt together when adding fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: int = Field(ge=0, le=10, description="0=no impact, 10=S&P ±5% or more")
    confidence: str = Field(description="low | medium | high")
    category: str = Field(description="tariff/iran/china/fed/ukraine/economy/political/other")
    direction: str = Field(description="bull | bear | neutral | mixed")
    rationale: str = Field(
        min_length=10, max_length=600,
        description="2-3 sentence Analysis. Surfaced in the email body `Analysis` row.",
    )
    key_tickers: list[str] = Field(default_factory=list, max_length=10)
    most_similar_event_id: str = Field(default="", max_length=80)

    # ── New fields (user decision 2026-05-15) ──
    topic_slug: str = Field(
        default="other", max_length=60,
        description=(
            "snake_case slug for the email subject (e.g. liberation_day, "
            "mexico_canada_tariff, iran_ceasefire, rare_earth_export_ban). "
            "must NOT include date prefix — only the topic part."
        ),
    )
    insider_concern_score: int = Field(
        default=0, ge=0, le=10,
        description=(
            "Separate from market_impact_score. How suspicious that this post "
            "may give an unfair informational advantage to people who saw it "
            "early / through insider channels. 0=routine public statement, "
            "10=strong evidence of insider trading risk (e.g. policy that "
            "moves a single ticker with no prior leak)."
        ),
    )
    insider_analysis: str = Field(
        default="", max_length=400,
        description=(
            "1-2 sentences explaining the insider_concern_score reasoning. "
            "Surfaced in the email body `Insider-trading suspicion` row."
        ),
    )

    # ── Tier mapping helper ──
    def to_tier(self) -> Tier:
        if self.score >= 9:
            return Tier.EMERGENCY
        if self.score >= 7:
            return Tier.RISK_OFF
        if self.score >= 5:
            return Tier.WATCH
        return Tier.NORMAL

    def to_direction(self) -> Direction:
        d = self.direction.lower()
        if d == "bull":
            return Direction.UP
        if d == "bear":
            return Direction.DOWN
        return Direction.NEUTRAL


# ====================================================================
# Prompt template
# ====================================================================
_SYSTEM_PROMPT = """You are a financial markets analyst evaluating posts from
Donald Trump's Truth Social account for their immediate market impact on US
equities (S&P 500, NASDAQ) and select tickers.

Your job: given a NEW Trump post and a few HISTORICAL reference events (each
with verbatim posts and observed market reaction), output a JSON object
predicting how the NEW post is likely to move markets within the next session.

Scoring scale (market_impact_score, integer 0-10):
  10 — S&P moves ±5% or more (rare: Liberation Day Apr 2 2025, BUY-then-pause Apr 9 2025)
  9  — S&P ±3-5% (major tariff escalation, war strike, Fed shock)
  7-8 — S&P ±1-3% (significant tariff hike, geopolitical escalation, sector-wide news)
  5-6 — S&P ±0.5-1% (minor policy noise, single-sector hit)
  3-4 — single-ticker move only, no broad index impact (political attack on a CEO)
  0-2 — meme / personal grievance / sports / no economic content

Direction:
  bull = expected up move
  bear = expected down move
  neutral = unclear or balanced
  mixed = different sectors react opposite ways

Categories: tariff | iran | china | fed | ukraine | economy | political | crypto | other

Output rules:
  · ALWAYS return a single JSON object matching the schema below — no prose, no markdown.
  · Be conservative: most posts (>80%) are score ≤ 4. Reserve 7+ for posts that quote
    SPECIFIC tariff rates, country names, military actions, or Fed personnel decisions.
  · Use the historical references to calibrate — if the new post is similar to a
    score-9 historical event, it deserves a similar score; if dissimilar to all,
    default toward lower scores.
  · `most_similar_event_id`: pick the SINGLE closest historical event_id from the
    references shown. Empty string "" if none reasonably similar.
  · `key_tickers`: only specific tickers strongly implied by the post (e.g. NVDA for
    chip-export news, TSLA for Musk-related, XOP/CL for oil, SPY/QQQ for broad).
    Empty list [] if not specific.
  · `topic_slug`: a short snake_case slug describing the topic only (no date prefix).
    PREFER to reuse the slug suffix of `most_similar_event_id` when applicable.
    Examples: liberation_day | mexico_canada_tariff | iran_ceasefire |
    iran_temp_ceasefire | rare_earth_export_ban | powell_fired | china_total_reset.
    If the post is general / non-market, use "other".
  · `insider_concern_score` (0-10) is SEPARATE from market_impact_score. It measures
    how suspicious the post is from an insider-trading perspective — i.e. the risk
    that someone who saw this content early (a few minutes before public release)
    could have placed a directional bet with high asymmetry.
      10 — surprise policy on a single ticker / commodity with no prior leak.
       7 — broad-impact post but specific direction (tariff rate, military action).
       4 — repeat of known stance (rhetorical reinforcement).
       1 — opinion / personal grievance / domestic political with no asset link.
       0 — meme / unrelated.
  · `insider_analysis`: 1-2 sentences explaining the insider_concern_score reasoning,
    e.g. "Names a specific country and rate not previously announced" or
    "Reiteration of stance already priced in over the past week".

JSON schema:
{
  "score": int 0-10,
  "confidence": "low" | "medium" | "high",
  "category": one of above,
  "direction": one of above,
  "rationale": short string (1-3 sentences explaining why),
  "key_tickers": list of strings,
  "most_similar_event_id": string (one event_id from references) or "",
  "topic_slug": short snake_case slug (no date),
  "insider_concern_score": int 0-10,
  "insider_analysis": 1-2 sentence string
}
"""


def _format_reference(ref: RetrievedReference, idx: int) -> str:
    """Render one historical reference as LLM-friendly markdown."""
    ev = ref.event
    posts_block = "\n".join(
        f"  · [{p.posted_at_utc.strftime('%Y-%m-%d %H:%M UTC') if p.posted_at_utc else '?'}]"
        f' "{p.text[:400]}{"…" if len(p.text) > 400 else ""}"'
        for p in ev.posts[:5]
    )
    if not posts_block:
        posts_block = "  · (no verbatim posts captured — image-only or single_source event)"
    # Try to extract just the "Market reaction" table from narrative. Fallback: first 200 chars.
    narrative_snippet = ev.narrative
    # When narrative is too long, prefer the "Market reaction" section
    if len(narrative_snippet) > 800:
        mr_idx = narrative_snippet.lower().find("market reaction")
        if mr_idx >= 0:
            narrative_snippet = narrative_snippet[mr_idx : mr_idx + 800]
        else:
            narrative_snippet = narrative_snippet[:800]

    return (
        f"### Reference {idx}: `{ev.event_id}`\n"
        f"- category: {ev.category or 'unknown'}\n"
        f"- market_impact_score: {ev.market_impact_score}/10\n"
        f"- verification: {ev.verification_level or 'unknown'}\n"
        f"- retrieval_match_score: keyword={ref.score_keyword:.2f} embedding={ref.score_embedding:.2f}\n"
        f"\n**Verbatim posts**:\n{posts_block}\n"
        f"\n**Context (v1.md narrative)**:\n{narrative_snippet.strip()}\n"
    )


def _format_new_post(post: TruthPost) -> str:
    """Render the new post for the LLM."""
    return (
        f"### NEW Trump Truth Social post (to score)\n"
        f"- posted_at_utc: {post.created_at.isoformat()}\n"
        f"- post_id: {post.post_id}\n"
        f"- is_reblog: {post.is_reblog}\n"
        f"- media_count: {post.media_count}\n"
        f"- char_count: {post.char_count}\n"
        f"\n**Verbatim text**:\n\"{post.text}\"\n"
    )


# ====================================================================
# Main class
# ====================================================================
class TruthSocialLLMScorer:
    """New Trump Truth Social post → market-impact score (LLM).

    Args:
        reference_db: pre-loaded TruthSocialReferenceDB instance.
        api_key: OpenAI API key. None → env OPENAI_API_KEY.
        model: GPT model name (default "gpt-5.4").
        top_k_references: number of references to embed in the prompt (default 5).
        timeout_s: API timeout.
        cache_ttl_s: post_id result cache TTL.
    """

    def __init__(
        self,
        *,
        reference_db: TruthSocialReferenceDB,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        top_k_references: int = 5,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
    ) -> None:
        if not _OPENAI_AVAILABLE:
            raise RuntimeError(
                "TruthSocialLLMScorer: openai package not installed. "
                "Install with `pip install openai`.",
            )
        self._ref_db = reference_db
        self._api_key = api_key
        self._model = model
        self._top_k = max(1, int(top_k_references))
        self._timeout_s = float(timeout_s)
        self._cache_ttl_s = float(cache_ttl_s)

        # post_id → (cached_ts_unix, MarketImpactScore)
        self._cache: dict[str, tuple[float, MarketImpactScore]] = {}

        # lazy AsyncOpenAI
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise RuntimeError(
                "TruthSocialLLMScorer: OPENAI_API_KEY not provided.",
            )
        assert AsyncOpenAI is not None  # after _OPENAI_AVAILABLE check
        self._client = AsyncOpenAI(api_key=self._api_key, timeout=self._timeout_s)
        return self._client

    # ────────────────────────────────────────────────────────────────
    # Score API
    # ────────────────────────────────────────────────────────────────
    async def score(self, post: TruthPost) -> MarketImpactScore:
        """Single post → MarketImpactScore. Cache hit first.

        Falls back to NORMAL (score=0) on failure (safe).
        """
        # ── cache hit ──
        cached = self._cache.get(post.post_id)
        if cached is not None:
            cached_ts, cached_score = cached
            if (time.time() - cached_ts) < self._cache_ttl_s:
                logger.debug(
                    "TruthSocialLLMScorer: cache hit post_id=%s score=%d",
                    post.post_id, cached_score.score,
                )
                return cached_score

        # ── Reference DB retrieval ──
        try:
            references = await self._ref_db.retrieve(
                post.text or post.url,
                top_k=self._top_k,
            )
        except Exception as e:  # noqa: BLE001 — best-effort retrieval
            logger.warning(
                "TruthSocialLLMScorer: retrieve failed post_id=%s: %s",
                post.post_id, e,
            )
            references = []

        # ── LLM call ──
        try:
            result = await self._call_llm(post, references)
        except (APIError, APITimeoutError, RateLimitError, asyncio.TimeoutError) as e:
            logger.error(
                "TruthSocialLLMScorer: LLM API error post_id=%s: %s",
                post.post_id, e,
            )
            result = self._fallback_score()
        except Exception as e:  # noqa: BLE001 — final safety net
            logger.exception(
                "TruthSocialLLMScorer: unexpected error post_id=%s: %s",
                post.post_id, e,
            )
            result = self._fallback_score()

        # ── cache + return ──
        self._cache[post.post_id] = (time.time(), result)
        return result

    async def _call_llm(
        self,
        post: TruthPost,
        references: list[RetrievedReference],
    ) -> MarketImpactScore:
        """Actual OpenAI API call + JSON response validation."""
        client = self._ensure_client()

        ref_block = "\n\n".join(
            _format_reference(r, i + 1) for i, r in enumerate(references)
        )
        if not ref_block:
            ref_block = "(no similar historical references retrieved)"

        user_prompt = (
            f"## HISTORICAL REFERENCES (top-{len(references)} most similar)\n\n"
            f"{ref_block}\n\n"
            f"---\n\n"
            f"{_format_new_post(post)}\n\n"
            f"---\n\n"
            "Based on the historical references, output a JSON object scoring the NEW post. "
            "Be conservative; default to lower scores when unclear. "
            "Pick `most_similar_event_id` only from the references shown above."
        )

        logger.debug(
            "TruthSocialLLMScorer: calling LLM post_id=%s model=%s refs=%d",
            post.post_id, self._model, len(references),
        )

        resp = await client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=600,
        )

        raw = resp.choices[0].message.content or ""
        try:
            data = json.loads(raw)
            return MarketImpactScore.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.error(
                "TruthSocialLLMScorer: invalid JSON response post_id=%s: %s "
                "raw=%s",
                post.post_id, e, raw[:200],
            )
            return self._fallback_score()

    @staticmethod
    def _fallback_score() -> MarketImpactScore:
        """Safe default on API/parse failure — does not produce a NORMAL signal."""
        return MarketImpactScore(
            score=0,
            confidence="low",
            category="other",
            direction="neutral",
            rationale="(LLM call failed — fallback to NORMAL)",
            key_tickers=[],
            most_similar_event_id="",
        )

    # ────────────────────────────────────────────────────────────────
    # ChannelSignal conversion
    # ────────────────────────────────────────────────────────────────
    def to_channel_signal(
        self,
        *,
        post: TruthPost,
        score: MarketImpactScore,
        symbol: str = "SPY",
        ts: datetime | None = None,
    ) -> ChannelSignal:
        """MarketImpactScore → ChannelSignal.

        Args:
            post: original post (for post_id tracing).
            score: LLM scoring result.
            symbol: symbol for alert routing. Default "SPY" (broad market).
                When key_tickers is non-empty, use the first ticker
                (when the channel fans out).
            ts: signal time (UTC). None → now.
        """
        # reason_codes: email/X renderer pastes these directly into the metadata table.
        # User-requested fields (2026-05-15): topic, similar event, key_tickers,
        # analysis(=rationale), insider_concern_score, insider_analysis.
        reason_codes: list[str] = [
            f"TOPIC={score.topic_slug or 'other'}",
            f"CATEGORY={score.category}",
            f"IMPACT_SCORE={score.score}/10",
            f"SIMILAR={score.most_similar_event_id or 'none'}",
        ]
        if score.key_tickers:
            reason_codes.append(f"TICKERS={','.join(score.key_tickers[:5])}")
        reason_codes.append(
            f"INSIDER_SUSPICION={score.insider_concern_score}/10"
        )
        if score.insider_analysis:
            reason_codes.append(f"INSIDER_NOTE={score.insider_analysis}")
        if score.rationale:
            reason_codes.append(f"ANALYSIS={score.rationale}")
        if post.url:
            reason_codes.append(f"POST_URL={post.url}")

        return ChannelSignal(
            channel=CHANNEL_TRUTH_SOCIAL,
            symbol=symbol,
            ts=ts or datetime.now(timezone.utc),
            score=score.score / 10.0,
            tier=score.to_tier(),
            direction=score.to_direction(),
            confidence={"low": 0.4, "medium": 0.7, "high": 0.95}.get(
                score.confidence.lower(), 0.5,
            ),
            fired_detectors=["truth_social_llm_v1"],
            reason_codes=reason_codes,
        )
