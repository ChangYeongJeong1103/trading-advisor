"""
channels/truth_social/normalize.py — Status JSON → TruthPost dataclass.

────────────────────────────────────────────────────────────────────────
Responsibilities:
  Normalize one Truth Social (Mastodon-compatible) status JSON into a
  frozen dataclass (`TruthPost`) used internally. Convert HTML content to
  plain text (so LLM scoring / keyword filter can consume it as-is).

  The raw JSON is preserved in the ``raw`` field — original access remains
  available for backfill / debug.

────────────────────────────────────────────────────────────────────────
Key Mastodon status fields (Truth Social uses the same schema):

  · id            : str   — status id (snowflake-like). Key for sorting/dedupe.
  · created_at    : str   — ISO 8601 UTC ("2025-04-02T17:18:34.000Z").
  · content       : str   — HTML body ("<p>Today is liberation day...</p>").
  · url           : str   — https://truthsocial.com/@{user}/posts/{id}
  · account.acct  : str   — "realDonaldTrump"
  · media_attachments: list — list of image/video attachments.
  · reblog        : dict|None — None for own posts, the original status for retruths.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Mastodon content is always HTML — strip tags/entities to extract plain text.
# 1) Replace block tags like <br>/<p> with newlines
# 2) Remove remaining inline tags
# 3) Decode common HTML entities
_BLOCK_TAG_RE = re.compile(
    r"</p>\s*<p>|<br\s*/?>|</p>|<p[^>]*>",
    flags=re.IGNORECASE,
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITIES: dict[str, str] = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&nbsp;": " ",
    "&hellip;": "…",
}


def _html_to_text(html: str | None) -> str:
    """Mastodon content (HTML) → plain text.

    Replace block-level tags with newlines, remove inline tags, decode common HTML entities.
    """
    if not html:
        return ""
    text = _BLOCK_TAG_RE.sub("\n", html)
    text = _ANY_TAG_RE.sub("", text)
    for entity, ch in _HTML_ENTITIES.items():
        text = text.replace(entity, ch)
    return text.strip()


@dataclass(frozen=True)
class TruthPost:
    """A normalized Truth Social post.

    Attributes:
        post_id: Mastodon-style status id (dedupe key). No guaranteed monotonicity in time;
            comparisons must use ``created_at`` to be safe.
        created_at: post publish time (UTC, aware datetime).
        author: account handle (e.g. "realDonaldTrump"). Not lowercased — original casing.
        text: HTML-stripped plain text body. Format consumed by LLM / keyword filter.
        url: link to the original post.
        media_count: number of attachments (image/video). 0 means text-only.
        is_reblog: True when this is a retruth of someone else's post (not own).
            Analysis can weight these lower.
        raw: original status JSON. Dumped as-is to backfill files → source-of-truth
            during reference DB curation.
    """

    post_id: str
    created_at: datetime
    author: str
    text: str
    url: str
    media_count: int = 0
    is_reblog: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def char_count(self) -> int:
        """Body length (for LLM cost estimate / log noise judgement)."""
        return len(self.text)


def _parse_created_at(value: str | None) -> datetime:
    """ISO 8601 string → aware UTC datetime. Fallback to epoch on failure."""
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    s = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        logger.warning("Unparseable created_at=%r — fallback to epoch", value)
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_status(status: dict[str, Any]) -> TruthPost:
    """Truth Social (Mastodon) status JSON → TruthPost.

    Args:
        status: single status dict from the API response. Missing fields are
            filled with safe defaults (post_id and url are assumed to always exist).

    Returns:
        TruthPost — normalized object used internally.

    Raises:
        KeyError: when status lacks the ``id`` field entirely. Indicates a broken response schema.
    """
    post_id: str = str(status["id"])  # KeyError if missing — schema broken
    text = _html_to_text(status.get("content"))
    media = status.get("media_attachments") or []
    account = status.get("account") or {}
    return TruthPost(
        post_id=post_id,
        created_at=_parse_created_at(status.get("created_at")),
        author=str(account.get("acct") or account.get("username") or ""),
        text=text,
        url=str(status.get("url") or ""),
        media_count=len(media),
        is_reblog=status.get("reblog") is not None,
        raw=status,
    )
