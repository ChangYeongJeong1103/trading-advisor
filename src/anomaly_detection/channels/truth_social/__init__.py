"""
channels/truth_social/ — Trump Truth Social posts collector (Channel 5).

────────────────────────────────────────────────────────────────────────
Responsibilities (v0.7.18 — first cut, DRY_RUN only):

  Poll Trump's (realDonaldTrump) Truth Social timeline every 5 min ± 60s
  jitter and emit only new posts. Call the Mastodon-compatible public API
  endpoint directly via raw httpx — no token / login required.

  Pipeline (from Step 3 — v0.7.20+):
    TrumpCollector → TruthPost normalize → ReferenceDB hybrid retrieval
    (keyword + embedding) → TruthSocialLLMScorer (GPT-5.4 JSON mode) →
    ChannelSignal (tier=NORMAL/WATCH/RISK_OFF/EMERGENCY)

  Score → Tier mapping:
    · 9-10 → EMERGENCY  (Email + Telegram + X)
    · 7-8  → RISK_OFF   (Email only)
    · 5-6  → WATCH      (fusion contribution only, no alert)
    · 0-4  → NORMAL     (no signal emit)

────────────────────────────────────────────────────────────────────────
Endpoint (Mastodon compatible, public):

  · GET https://truthsocial.com/api/v1/accounts/{account_id}/statuses
      ?limit=40&exclude_replies=true&exclude_reblogs=false
  · cursor: use the last status_id in the response list as ``max_id`` for the next page.

  realDonaldTrump account_id = "107780257626128497" (Mastodon internal ID,
  stable). No auth required (Truth Social policy since 2025-08-27 allows
  prominent figures).

────────────────────────────────────────────────────────────────────────
Cloudflare evasion safeguards:

  · Standard browser User-Agent + Accept-Language headers.
  · 5min baseline + ±60s jitter — avoids on-the-hour polling pattern.
  · Polls only one user (Trump) — load / suspicious behavior is near 0.

────────────────────────────────────────────────────────────────────────
Plan: P13.1 (Truth Social channel, follow-up)
"""

from .channel import TruthSocialChannel  # noqa: F401
from .collector import (  # noqa: F401
    TRUMP_ACCOUNT_ID,
    TruthSocialApiError,
    TrumpCollector,
)
from .llm_scorer import MarketImpactScore, TruthSocialLLMScorer  # noqa: F401
from .normalize import TruthPost, normalize_status  # noqa: F401
from .reference_db import (  # noqa: F401
    ReferenceEvent,
    ReferencePost,
    RetrievedReference,
    TruthSocialReferenceDB,
)
