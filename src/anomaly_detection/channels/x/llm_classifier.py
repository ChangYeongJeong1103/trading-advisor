"""
x/llm_classifier.py — Stage-2 LLM (GPT-5.4-mini) classifier.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5.4.1, plan P9.4):

  Takes X posts whose Stage-1 (config/x_keywords.yaml) keyword/regex score
  exceeds the threshold, asks GPT-5.4-mini whether they look like an
  insider-trading pattern, and converts the result into a ChannelSignal.

  Pipeline position:
      X collector → parser → stage1_filter ─→ (this module) → ChannelSignal

────────────────────────────────────────────────────────────────────────
Design decisions (Why this design):

  1. YAML-driven prompt (single source of truth)
     - Use system_prompt + examples from config/x_few_shot.yaml verbatim.
     - No prompt embedded in code → modify yaml at runtime to change LLM behavior.

  2. Async + lazy import
     - openai SDK is lazy-imported (works in test envs without it installed).
     - Use AsyncOpenAI (other channels are async too).

  3. JSON mode + Pydantic validate
     - response_format={"type": "json_object"} forces JSON response every time.
     - Validate schema with LLMClassification (Pydantic) — bad responses fall back
       to NORMAL (safe default).

  4. Prompt caching (OpenAI automatic)
     - system_prompt + 9 examples are an identical prefix on every call.
     - 1024+ token prefixes are auto-cached by OpenAI → 90% cost reduction.

  5. In-memory TTL cache (post_id keyed)
     - Same post arriving multiple times triggers only one LLM call.
     - Default 1-hour TTL (configurable).

  6. Vision support
     - When a post has image_urls, send them as vision inputs (mini also supports vision).
     - Additional token cost is negligible (~+$0.001/image).

  7. Error handling = fail-safe NORMAL
     - API timeout / rate limit / parse failure → return NORMAL ChannelSignal.
     - False negatives are safer than false positives (user experience).

────────────────────────────────────────────────────────────────────────
Example usage (in test / channel.py):

    classifier = LLMClassifier(
        few_shot_yaml_path="config/x_few_shot.yaml",
        model="gpt-5.4-mini",
        api_key=os.environ["OPENAI_API_KEY"],
    )
    classification = await classifier.classify(post)

    # Fan out per symbol (emit 2 if the post mentions BTC + ETH)
    for symbol in classification.symbols:
        signal = classifier.to_channel_signal(classification, symbol=symbol)
        ...

────────────────────────────────────────────────────────────────────────
Plan: P9.4 (X channel LLM upgrade)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...core.schemas import (
    CHANNEL_X,
    ChannelSignal,
    Direction,
    Tier,
)

# Lazy-import the OpenAI SDK — wrap in try/except so import does not fail in
# mock-only test environments where the package is missing.
try:
    from openai import AsyncOpenAI       # type: ignore[import-not-found]
    from openai import APIError          # type: ignore[import-not-found]
    from openai import APITimeoutError   # type: ignore[import-not-found]
    from openai import RateLimitError    # type: ignore[import-not-found]

    _OPENAI_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover — environment-dependent
    AsyncOpenAI = None  # type: ignore[assignment, misc]
    APIError = Exception  # type: ignore[assignment, misc]
    APITimeoutError = Exception  # type: ignore[assignment, misc]
    RateLimitError = Exception  # type: ignore[assignment, misc]
    _OPENAI_AVAILABLE = False


logger = logging.getLogger(__name__)


# =====================================================================
# LLM response schema (1:1 with the output_schema in config/x_few_shot.yaml)
# =====================================================================
class LLMClassification(BaseModel):
    """Validated GPT-5.4 JSON response.

    Must match the output_schema in config/x_few_shot.yaml. If this schema
    changes, also update output_schema and examples.expected_output in the yaml.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: Tier = Field(description="4-tier classification")
    confidence: float = Field(ge=0.0, le=1.0, description="Pattern-match confidence")
    direction: str = Field(description="BUY | SELL | NEUTRAL")
    symbols: list[str] = Field(default_factory=list, description="canonical symbol")
    key_signals: list[str] = Field(default_factory=list)
    matched_case: str | None = Field(default=None)
    reasoning: str = Field(default="")
    is_pre_event: bool = Field(default=False)
    recommend_action: str | None = Field(default=None)


# =====================================================================
# Cache entry — prevents reprocessing the same post_id
# =====================================================================
class _CacheEntry:
    """In-memory TTL cache entry. A simple dataclass substitute."""

    __slots__ = ("ts_unix", "classification")

    def __init__(self, ts_unix: float, classification: LLMClassification) -> None:
        self.ts_unix = ts_unix
        self.classification = classification


# =====================================================================
# Direction string → schemas.Direction enum mapping
# =====================================================================
# LLM returns BUY/SELL/NEUTRAL → convert to UP/DOWN/NEUTRAL in core/schemas
# (Direction represents "price-pressure direction", so BUY = upward pressure = UP).
_DIRECTION_MAP: dict[str, Direction] = {
    "BUY": Direction.UP,
    "SELL": Direction.DOWN,
    "NEUTRAL": Direction.NEUTRAL,
    # safety net — in case the LLM returns lowercase
    "buy": Direction.UP,
    "sell": Direction.DOWN,
    "neutral": Direction.NEUTRAL,
    "long": Direction.UP,
    "short": Direction.DOWN,
}


# =====================================================================
# Main class
# =====================================================================
class LLMClassifier:
    """Classify an X post via GPT-5.4-mini → convert to ChannelSignal.

    Args:
        few_shot_yaml_path: path to config/x_few_shot.yaml.
        model: OpenAI model name (default "gpt-5.4-mini" — ~3.3x cheaper than
               frontier. Promote to "gpt-5.4" for more accurate classification).
        api_key: OpenAI API key (typically from the OPENAI_API_KEY env var).
        cache_ttl_seconds: TTL to prevent reprocessing the same post_id (default 3600 = 1h).
        request_timeout_s: API call timeout (default 30s).
        max_image_count: max images per post to send to the LLM (default 4).

    Raises:
        FileNotFoundError: when the yaml file cannot be found.
        RuntimeError: when classify() is called without the openai SDK installed.
    """

    def __init__(
        self,
        *,
        few_shot_yaml_path: str | Path,
        model: str = "gpt-5.4-mini",
        api_key: str | None = None,
        cache_ttl_seconds: float = 3600.0,
        request_timeout_s: float = 30.0,
        max_image_count: int = 4,
    ) -> None:
        # ── Load YAML ────────────────────────────────────────────────
        self._yaml_path = Path(few_shot_yaml_path)
        if not self._yaml_path.exists():
            raise FileNotFoundError(
                f"x_few_shot.yaml not found at {self._yaml_path}"
            )

        with self._yaml_path.open("r", encoding="utf-8") as f:
            self._config: dict[str, Any] = yaml.safe_load(f) or {}

        self._system_prompt: str = str(self._config.get("system_prompt", "")).strip()
        self._examples: list[dict[str, Any]] = list(self._config.get("examples", []))

        if not self._system_prompt:
            raise ValueError("x_few_shot.yaml: system_prompt is empty")
        if not self._examples:
            logger.warning(
                "x_few_shot.yaml: examples is empty — no few-shot learning effect"
            )

        # ── Pre-build the identical messages prefix (caching-friendly) ─
        # OpenAI prompt caching only hits when the prefix is identical, so build
        # the examples portion once and reuse.
        self._cached_prefix: list[dict[str, Any]] = self._build_prefix_messages()

        # ── Settings ────────────────────────────────────────────────
        self._model = model
        self._api_key = api_key
        self._cache_ttl_s = max(0.0, float(cache_ttl_seconds))
        self._timeout_s = max(1.0, float(request_timeout_s))
        self._max_image_count = max(0, int(max_image_count))

        # ── In-memory TTL cache ────────────────────────────────────
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = asyncio.Lock()

        # ── OpenAI client (lazy creation — on first classify call) ──
        self._client: Any = None

        logger.info(
            "LLMClassifier: initialized (model=%s, examples=%d, cache_ttl=%.0fs)",
            self._model, len(self._examples), self._cache_ttl_s,
        )

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────
    async def classify(self, post: dict[str, Any]) -> LLMClassification:
        """One X post → LLMClassification.

        Args:
            post: X post payload. Required fields:
                - id (str): post unique id (cache key)
                - user (str): account handle (e.g. "lookonchain")
                - text (str): post body
                - timestamp (int): unix timestamp
                Optional fields:
                - account_credibility (str): human-written hint
                - image_urls (list[str]): attached image URLs (vision input)

        Returns:
            LLMClassification. Always returns a NORMAL fallback on any error
            (never raises — fail-safe).
        """
        post_id = str(post.get("id", ""))

        # ── Cache hit ────────────────────────────────────────────
        if post_id and self._cache_ttl_s > 0:
            cached = await self._get_cached(post_id)
            if cached is not None:
                logger.debug("LLMClassifier: cache hit for post_id=%s", post_id)
                return cached

        # ── OpenAI call ──────────────────────────────────────────
        try:
            classification = await self._call_llm(post)
        except RuntimeError:
            # Environment problem such as openai SDK not installed — propagate immediately
            raise
        except Exception as e:
            logger.error(
                "LLMClassifier: failed for post_id=%s user=%s: %s",
                post_id, post.get("user"), e,
            )
            classification = self._safe_fallback(reason=f"llm_error:{type(e).__name__}")

        # ── Save to cache ────────────────────────────────────────
        if post_id and self._cache_ttl_s > 0:
            await self._set_cached(post_id, classification)

        return classification

    def to_channel_signal(
        self,
        classification: LLMClassification,
        *,
        symbol: str,
        ts: datetime | None = None,
    ) -> ChannelSignal:
        """LLMClassification → ChannelSignal (for a given symbol).

        Args:
            classification: result of classify().
            symbol: ChannelSignal.symbol (canonical, e.g. "BTC", "CL").
                    Must be one of classification.symbols (warn on violation).
            ts: ChannelSignal.ts (default: now UTC).

        Returns:
            ChannelSignal — can be passed straight to the fusion engine.
        """
        if symbol not in classification.symbols:
            logger.warning(
                "LLMClassifier: symbol %s not in classification.symbols=%s — emitting anyway",
                symbol, classification.symbols,
            )

        direction = _DIRECTION_MAP.get(
            classification.direction, Direction.NEUTRAL
        )

        # score = LLM confidence as-is, with a per-tier sanity-check floor
        # (EMERGENCY: score ≥ 0.80, RISK_OFF: ≥ 0.60, WATCH: ≥ 0.40)
        score = self._floor_score_by_tier(classification.tier, classification.confidence)

        # reason_codes — shorten the LLM's key_signals
        # e.g. ["pre-event timing", "fresh wallet", "$760M short"]
        reason_codes = [str(s)[:80] for s in classification.key_signals[:8]]
        if classification.matched_case:
            reason_codes.append(f"matched:{classification.matched_case}")
        if classification.is_pre_event:
            reason_codes.append("pre_event_alert")

        return ChannelSignal(
            channel=CHANNEL_X,
            symbol=symbol,
            ts=ts or datetime.now(timezone.utc),
            score=score,
            tier=classification.tier,
            direction=direction,
            confidence=classification.confidence,
            fired_detectors=["llm_classifier_v1"],
            reason_codes=reason_codes,
        )

    # ─────────────────────────────────────────────────────────────────
    # OpenAI call — internal
    # ─────────────────────────────────────────────────────────────────
    async def _call_llm(self, post: dict[str, Any]) -> LLMClassification:
        """Actual GPT-5.4 call. _build_user_message → API → JSON parse → validate."""
        if not _OPENAI_AVAILABLE:
            raise RuntimeError(
                "openai SDK not installed. Run 'pip install openai' and retry."
            )

        # Lazy creation — instantiate the client on first call
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                timeout=self._timeout_s,
            )

        # messages = [system, *examples (cached), {analyze this post}]
        user_message = self._build_user_message(post)
        messages = self._cached_prefix + [user_message]

        logger.debug(
            "LLMClassifier: calling %s for post_id=%s user=%s (msgs=%d)",
            self._model, post.get("id"), post.get("user"), len(messages),
        )

        # Force response_format=json_object → always JSON
        response = await asyncio.wait_for(
            self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.2,   # prefer deterministic classification
            ),
            timeout=self._timeout_s + 5.0,   # safety net
        )

        raw_json = response.choices[0].message.content or "{}"
        return self._parse_response(raw_json)

    def _parse_response(self, raw_json: str) -> LLMClassification:
        """LLM JSON response → LLMClassification. NORMAL fallback on failure."""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as e:
            logger.warning("LLMClassifier: JSON parse failed: %s — raw=%s", e, raw_json[:200])
            return self._safe_fallback(reason="json_parse_error")

        try:
            return LLMClassification(**data)
        except ValidationError as e:
            logger.warning("LLMClassifier: validation failed: %s", e)
            return self._safe_fallback(reason="schema_validation_error")

    @staticmethod
    def _safe_fallback(*, reason: str) -> LLMClassification:
        """Return NORMAL on API/parse failure. Fail-safe (false negative > false positive)."""
        return LLMClassification(
            tier=Tier.NORMAL,
            confidence=0.0,
            direction="NEUTRAL",
            symbols=[],
            key_signals=[f"fallback:{reason}"],
            matched_case=None,
            reasoning=f"LLM classification failed; defaulted to NORMAL ({reason})",
            is_pre_event=False,
            recommend_action=None,
        )

    # ─────────────────────────────────────────────────────────────────
    # Prompt build — assemble the message array
    # ─────────────────────────────────────────────────────────────────
    def _build_prefix_messages(self) -> list[dict[str, Any]]:
        """Assemble system + 9 few-shot examples into OpenAI message format.

        Identical prefix on every call → OpenAI prompt caching hits (90% savings).
        """
        msgs: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt}
        ]

        for ex in self._examples:
            input_block = ex.get("input", {})
            expected = ex.get("expected_output", {})
            if not input_block or not expected:
                continue

            # User turn — format example input as human-readable text
            user_text = self._format_example_input(input_block)
            msgs.append({"role": "user", "content": user_text})

            # Assistant turn — serialize expected output as a JSON string
            msgs.append({
                "role": "assistant",
                "content": json.dumps(expected, ensure_ascii=False),
            })

        return msgs

    @staticmethod
    def _format_example_input(input_block: dict[str, Any]) -> str:
        """example.input dict → human-readable text.

        Mirrors the format _build_user_message produces during real analysis.
        """
        parts: list[str] = ["[X Post Analysis Request]"]
        if (acc := input_block.get("account")):
            parts.append(f"Account: {acc}")
        if (cred := input_block.get("account_credibility")):
            parts.append(f"Credibility: {cred}")
        if (ts := input_block.get("timestamp_utc")):
            parts.append(f"Timestamp (UTC): {ts}")
        if (txt := input_block.get("text")):
            parts.append(f"\nText:\n{str(txt).strip()}")
        if (img := input_block.get("image_description")):
            parts.append(f"\nImage description:\n{img}")
        return "\n".join(parts)

    def _build_user_message(self, post: dict[str, Any]) -> dict[str, Any]:
        """Real post to analyze → user message (text + optional vision content).

        OpenAI vision format:
            {"role": "user", "content": [
                {"type": "text", "text": "..."},
                {"type": "image_url", "image_url": {"url": "https://..."}},
            ]}
        """
        # ── Build text portion ───────────────────────────────────
        ts_unix = post.get("timestamp")
        if isinstance(ts_unix, (int, float)):
            ts_str = datetime.fromtimestamp(int(ts_unix), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        else:
            ts_str = "unknown"

        text_lines = [
            "[X Post Analysis Request]",
            f"Account: @{post.get('user', 'unknown')}",
        ]
        if (cred := post.get("account_credibility")):
            text_lines.append(f"Credibility: {cred}")
        text_lines.append(f"Timestamp (UTC): {ts_str}")
        text_lines.append(f"\nText:\n{str(post.get('text', '')).strip()}")

        text_block = "\n".join(text_lines)

        # ── No images → simple string content ─────────────────────
        image_urls: list[str] = list(post.get("image_urls") or [])
        if not image_urls or self._max_image_count == 0:
            return {"role": "user", "content": text_block}

        # ── Vision content (text + image_url parts) ───────────────
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": text_block}]
        for url in image_urls[: self._max_image_count]:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": str(url)},
            })

        return {"role": "user", "content": content_parts}

    # ─────────────────────────────────────────────────────────────────
    # Tier-aware score floor — 3-stage sanity check
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _floor_score_by_tier(tier: Tier, confidence: float) -> float:
        """Enforce consistency when LLM confidence disagrees with the tier.

        Example: if the LLM picks EMERGENCY but confidence=0.5, that is contradictory.
        → raise it to the tier minimum floor (consistency on audit).
        """
        floors: dict[Tier, float] = {
            Tier.NORMAL: 0.0,
            Tier.WATCH: 0.40,
            Tier.RISK_OFF: 0.60,
            Tier.EMERGENCY: 0.80,
        }
        return max(floors.get(tier, 0.0), float(confidence))

    # ─────────────────────────────────────────────────────────────────
    # Cache helpers — TTL-based in-memory dict
    # ─────────────────────────────────────────────────────────────────
    async def _get_cached(self, post_id: str) -> LLMClassification | None:
        async with self._cache_lock:
            entry = self._cache.get(post_id)
            if entry is None:
                return None
            age = time.time() - entry.ts_unix
            if age > self._cache_ttl_s:
                # expired — drop
                self._cache.pop(post_id, None)
                return None
            return entry.classification

    async def _set_cached(self, post_id: str, classification: LLMClassification) -> None:
        async with self._cache_lock:
            self._cache[post_id] = _CacheEntry(time.time(), classification)
            # If it grows too large (>10k), evict from the oldest — simple budget guard
            if len(self._cache) > 10_000:
                self._evict_oldest_locked(target_size=8_000)

    def _evict_oldest_locked(self, *, target_size: int) -> None:
        """Evict the oldest entries when the cache budget is exceeded. Call under _cache_lock."""
        sorted_items = sorted(self._cache.items(), key=lambda kv: kv[1].ts_unix)
        for pid, _ in sorted_items[: max(0, len(self._cache) - target_size)]:
            self._cache.pop(pid, None)
