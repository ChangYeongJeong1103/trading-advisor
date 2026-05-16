"""
channels/x/ — X (Twitter) account scraping channel.

────────────────────────────────────────────────────────────────────────
P9.4 (CURRENT — LLM pipeline):

  · XCollector       — X API v2 Bearer single path (real, PAYG)
  · MockXCollector   — synthetic posts (test / walking-skeleton)
  · Stage1Filter     — keyword + regex score (cost 0)
  · LLMClassifier    — GPT-5.4 analysis + few-shot learning (cost per call)
  · XChannel         — collector → stage1 → llm → ChannelSignal wiring

  Pipeline: collector → stage1_filter → llm_classifier → ChannelSignal

  Config (yaml driven):
    - config/x_keywords.yaml  (Stage1 weights + keywords + irrelevant filters)
    - config/x_few_shot.yaml  (LLM system prompt + 9 few-shot examples)

────────────────────────────────────────────────────────────────────────
DEPRECATED (P5 walking-skeleton — kept for backward-compat / git history):

  · parser.py        — regex extraction of symbol/direction/magnitude (unused by XChannel)
  · features.py      — XFeatures (15min rolling mention count)
  · detector.py      — XDetector (count + credibility weight rule)
  · credibility.py   — hardcoded account weight dict

  Intentionally removed in P9.4: a "multiple accounts mentioning simultaneously"
  signal cannot detect single-post insider trading early. The LLM must be able
  to judge EMERGENCY from a single post. These modules will be deleted in the
  next cleanup pass.
"""

# ─────────────────────────────────────────────────────────────────────
# P9.4 — current pipeline
# ─────────────────────────────────────────────────────────────────────
from .channel import XChannel  # noqa: F401
from .collector import XCollector  # noqa: F401
from .mock_collector import MockXCollector  # noqa: F401
from .stage1_filter import Stage1Filter, Stage1Result  # noqa: F401
from .llm_classifier import LLMClassifier, LLMClassification  # noqa: F401

# ─────────────────────────────────────────────────────────────────────
# DEPRECATED — P5 walking-skeleton (no longer used by XChannel).
# Still importable for external code that depends on it, but slated for the next cleanup.
# ─────────────────────────────────────────────────────────────────────
from .parser import parse_post  # noqa: F401  # DEPRECATED
from .credibility import account_weight, known_accounts  # noqa: F401  # DEPRECATED
from .features import XFeatures  # noqa: F401  # DEPRECATED
from .detector import XDetector, XDetectorConfig  # noqa: F401  # DEPRECATED
