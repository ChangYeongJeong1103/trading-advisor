"""
x/stage1_filter.py — Stage-1 keyword/regex filter (gate before the LLM call).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5.4.1, plan P9.4):

  Cheap, fast first-pass filtering of X posts before sending them to the
  LLM (GPT-5.4). Critical for cost / latency reduction.

  Pipeline position:
      X collector → parser → (this module) → if passed → llm_classifier
                                           → else      → drop

────────────────────────────────────────────────────────────────────────
Scoring (1:1 with stage1_filter_config in config/x_keywords.yaml):

  score = (ticker_score + case_score + common_score + regex_score)
          × account_multiplier
          × (0 if irrelevant else 1)

  passed = score >= llm_threshold (default 0.80)

  Each category is "credited once" even on multiple matches — prevents long
  posts from always passing. (E.g. 5 case keyword hits still only add 0.40.)

  Score range:
    - Minimum 0.00 (irrelevant match or nothing)
    - Maximum sum 1.20 (every category matches)
    - Maximum final 1.44 (× max account_multiplier 1.20)

────────────────────────────────────────────────────────────────────────
Keyword matching rules:

  - Case-insensitive (lowercase comparison).
  - **Best-effort word-boundary** (Python `\b...\b`):
      · If keyword start/end is alphanumeric, apply \b:
        "short" → matches "is short", does not match "shortage".
      · If keyword start/end is special ($, 0x...), use substring matching:
        "$32k" → matches as-is (\b would be meaningless).
  - Phrase keywords ("opened short", "before announcement") work the same way.
  - regex_patterns use Python `re` syntax; IGNORECASE is auto-applied.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Compile every keyword once at init time → evaluate() is fast (~ms).
  · evaluate() returns a Pydantic Stage1Result instead of a dict → rich audit breakdown.
  · Preserve matched keyword lists → traceable "why it passed/dropped".
  · YAML changes require a daemon restart (Cloud Run = image rebuild).

────────────────────────────────────────────────────────────────────────
Plan: P9.4 (X channel LLM upgrade)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# =====================================================================
# Result schema — output of evaluate()
# =====================================================================
class Stage1Result(BaseModel):
    """Stage-1 filter evaluation result.

    Carries a rich breakdown for audit / debug. channel.py only inspects `passed`
    to decide whether to call the LLM. Remaining fields are for logging /
    structured event store.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool = Field(description="Whether to call the LLM (score >= threshold)")
    score: float = Field(ge=0.0, description="final score (after applying account_mult)")
    base_score: float = Field(ge=0.0, description="sum before applying account_mult")
    account_multiplier: float = Field(ge=0.0, description="per-account multiplier")

    # Per-category match flags (4)
    ticker_matched: bool = False
    case_matched: bool = False
    common_matched: bool = False
    regex_matched: bool = False

    # Actually matched keywords / patterns (for audit)
    matched_tickers: list[str] = Field(default_factory=list)
    matched_case_keywords: list[str] = Field(default_factory=list)
    matched_common_signals: list[str] = Field(default_factory=list)
    matched_regex_patterns: list[str] = Field(default_factory=list)

    # Irrelevant filter — when matched, immediately reject with score=0
    irrelevant_matched: list[str] = Field(default_factory=list)
    rejected_by_irrelevant: bool = False


# =====================================================================
# Main class
# =====================================================================
class Stage1Filter:
    """Score an X post by keyword/regex → decide whether to call the LLM.

    Args:
        keywords_yaml_path: path to config/x_keywords.yaml.

    Raises:
        FileNotFoundError: yaml file not found.
        ValueError: when yaml's stage1_filter_config is missing or invalid.
    """

    def __init__(self, *, keywords_yaml_path: str | Path) -> None:
        # ── Load YAML ─────────────────────────────────────────────
        self._yaml_path = Path(keywords_yaml_path)
        if not self._yaml_path.exists():
            raise FileNotFoundError(f"x_keywords.yaml not found at {self._yaml_path}")

        with self._yaml_path.open("r", encoding="utf-8") as f:
            self._config: dict[str, Any] = yaml.safe_load(f) or {}

        # ── stage1_filter_config — weights + threshold ──────────
        cfg = self._config.get("stage1_filter_config")
        if not isinstance(cfg, dict):
            raise ValueError("x_keywords.yaml: missing 'stage1_filter_config'")

        weights = cfg.get("weights", {})
        self._w_ticker = float(weights.get("ticker_match", 0.20))
        self._w_case = float(weights.get("case_keyword_match", 0.40))
        self._w_common = float(weights.get("common_signal_match", 0.30))
        self._w_regex = float(weights.get("regex_match", 0.30))
        self._llm_threshold = float(cfg.get("llm_threshold", 0.80))

        # ── Compile per-category keywords (once at init) ────────
        # tickers_and_assets — flat list
        tickers = self._config.get("tickers_and_assets", []) or []
        self._ticker_patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, _compile_keyword(kw)) for kw in tickers if isinstance(kw, str)
        ]

        # case_keywords — nested dict {case_name: {keywords: [...]}}
        case_kw_dict = self._config.get("case_keywords", {}) or {}
        self._case_patterns: list[tuple[str, re.Pattern[str]]] = []
        for case_name, case_block in case_kw_dict.items():
            if not isinstance(case_block, dict):
                continue
            for kw in case_block.get("keywords", []) or []:
                if isinstance(kw, str):
                    self._case_patterns.append(
                        (f"{case_name}:{kw}", _compile_keyword(kw))
                    )

        # common_signal_keywords — nested dict (timing/wallet/size/...)
        common_dict = self._config.get("common_signal_keywords", {}) or {}
        self._common_patterns: list[tuple[str, re.Pattern[str]]] = []
        for section_name, kw_list in common_dict.items():
            if not isinstance(kw_list, list):
                continue
            for kw in kw_list:
                if isinstance(kw, str):
                    self._common_patterns.append(
                        (f"{section_name}:{kw}", _compile_keyword(kw))
                    )

        # regex_patterns — flat dict {name: pattern_str}
        regex_dict = self._config.get("regex_patterns", {}) or {}
        self._regex_patterns: list[tuple[str, re.Pattern[str]]] = []
        for name, pattern_str in regex_dict.items():
            if not isinstance(pattern_str, str):
                continue
            try:
                self._regex_patterns.append(
                    (name, re.compile(pattern_str, re.IGNORECASE))
                )
            except re.error as e:
                logger.warning(
                    "Stage1Filter: invalid regex %r=%r — skipping (%s)",
                    name, pattern_str, e,
                )

        # account_priority — {account_handle_lowercase: multiplier}
        account_priority = self._config.get("account_priority", {}) or {}
        self._account_multipliers: dict[str, float] = {
            str(handle).lower(): float(mult)
            for handle, mult in account_priority.items()
        }

        # irrelevant_filters — nested dict (sports/spam/retail_meme)
        irrelevant_dict = self._config.get("irrelevant_filters", {}) or {}
        self._irrelevant_patterns: list[tuple[str, re.Pattern[str]]] = []
        for section_name, kw_list in irrelevant_dict.items():
            if not isinstance(kw_list, list):
                continue
            for kw in kw_list:
                if isinstance(kw, str):
                    self._irrelevant_patterns.append(
                        (f"{section_name}:{kw}", _compile_keyword(kw))
                    )

        logger.info(
            "Stage1Filter: loaded "
            "(tickers=%d, case_kws=%d, common_kws=%d, regex=%d, "
            "irrelevant=%d, accounts=%d, threshold=%.2f)",
            len(self._ticker_patterns), len(self._case_patterns),
            len(self._common_patterns), len(self._regex_patterns),
            len(self._irrelevant_patterns), len(self._account_multipliers),
            self._llm_threshold,
        )

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────
    @property
    def llm_threshold(self) -> float:
        """Currently configured LLM-call threshold."""
        return self._llm_threshold

    def evaluate(self, post: dict[str, Any]) -> Stage1Result:
        """One X post → Stage1Result via keyword/regex matching.

        Args:
            post: X post payload. Required keys:
                - text (str): post body.
                - user (str, optional): account handle (e.g. "lookonchain")
                                        — used for account_priority lookup.

        Returns:
            Stage1Result. passed=True → forward to llm_classifier.
        """
        text = str(post.get("text", ""))
        user = str(post.get("user", "")).lower().lstrip("@")

        # ── 0) Irrelevant filter — when matched, immediately reject (score=0) ─
        irrelevant_matched = self._match_pattern_list(text, self._irrelevant_patterns)
        if irrelevant_matched:
            return Stage1Result(
                passed=False,
                score=0.0,
                base_score=0.0,
                account_multiplier=self._lookup_account_multiplier(user),
                irrelevant_matched=irrelevant_matched,
                rejected_by_irrelevant=True,
            )

        # ── 1) Per-category match ──────────────────────────────────
        matched_tickers = self._match_pattern_list(text, self._ticker_patterns)
        matched_cases = self._match_pattern_list(text, self._case_patterns)
        matched_common = self._match_pattern_list(text, self._common_patterns)
        matched_regex = self._match_pattern_list(text, self._regex_patterns)

        ticker_hit = bool(matched_tickers)
        case_hit = bool(matched_cases)
        common_hit = bool(matched_common)
        regex_hit = bool(matched_regex)

        # ── 2) base score (each category credited once) ───────────
        base = (
            (self._w_ticker if ticker_hit else 0.0)
            + (self._w_case if case_hit else 0.0)
            + (self._w_common if common_hit else 0.0)
            + (self._w_regex if regex_hit else 0.0)
        )

        # ── 3) account multiplier ─────────────────────────────────
        mult = self._lookup_account_multiplier(user)
        final = base * mult

        passed = final >= self._llm_threshold

        return Stage1Result(
            passed=passed,
            score=round(final, 4),
            base_score=round(base, 4),
            account_multiplier=mult,
            ticker_matched=ticker_hit,
            case_matched=case_hit,
            common_matched=common_hit,
            regex_matched=regex_hit,
            matched_tickers=matched_tickers,
            matched_case_keywords=matched_cases,
            matched_common_signals=matched_common,
            matched_regex_patterns=matched_regex,
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _match_pattern_list(
        text: str, patterns: list[tuple[str, re.Pattern[str]]]
    ) -> list[str]:
        """Match all (label, pattern) entries against text → return matched labels.

        Collects all matched labels within the same category (for audit).
        """
        out: list[str] = []
        for label, pat in patterns:
            if pat.search(text) is not None:
                out.append(label)
        return out

    def _lookup_account_multiplier(self, user_lower: str) -> float:
        """Lookup multiplier by account handle. Unknown → 1.0."""
        if not user_lower:
            return 1.0
        return self._account_multipliers.get(user_lower, 1.0)


# =====================================================================
# Module-level helper — keyword compilation
# =====================================================================
def _compile_keyword(keyword: str) -> re.Pattern[str]:
    """A single keyword (word or phrase) → compiled case-insensitive regex.

    Word-boundary application rules (best effort):
      - If keyword's start/end character is alphanumeric, add \\b on that side
      - For special characters (e.g. $24m, 0xabc, 🚨), \\b has no effect, so omit it
        → substring matching applies

    Examples:
      "short"        → \\bshort\\b   (does not match "shorts", "shortage")
      "opened short" → \\bopened short\\b
      "$32k"         → \\$32k        (substring match — $ is not a word char)
      "0x31a56e"     → 0x31a56e\\b   (substring on the left, boundary on the right)
    """
    escaped = re.escape(keyword)
    prefix = r"\b" if keyword[0].isalnum() else ""
    suffix = r"\b" if keyword[-1].isalnum() else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)
