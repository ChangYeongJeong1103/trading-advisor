"""
alerts/x_publisher.py — X(Twitter) auto-post publisher for EMERGENCY alerts.

Role:
  Sequentially uploads rendered tweet thread text to X API v2 `/2/tweets`.
  - Upload the first tweet
  - Subsequent tweets are linked as a reply chain (thread)
  - (Optional) Attach one image to the first tweet — pre-upload via v1.1
    media/upload, then pass the resulting media_id to the v2 POST.

Supported auth:
  1) OAuth 1.0a user context (recommended — supports both posting and media upload)
  2) Bearer token (fallback when a user-context bearer token is available — no media)

Safety:
  - If enabled=False, the call itself is skipped
  - If dry_run=True, no API call — only log/capture
  - On failure, the rest of the dispatcher flow (email/telegram) continues (best-effort)
  - If image upload fails, retry posting text-only (without the image).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    from requests_oauthlib import OAuth1  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover
    OAuth1 = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_X_POST_URL = "https://api.twitter.com/2/tweets"
# v1.1 media upload still works fine with OAuth1 (including X Premium Basic).
# Simple (non-chunked) upload is sufficient for PNGs under 5 MB.
_X_MEDIA_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"


@dataclass(frozen=True)
class XPostConfig:
    enabled: bool = False
    dry_run: bool = True
    timeout_s: float = 20.0


@dataclass(frozen=True)
class XCredentials:
    # OAuth1 (recommended for posting)
    api_key: str = ""
    api_key_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""
    # Optional bearer fallback (must be user-context token to post)
    bearer_token: str = ""

    @property
    def has_oauth1(self) -> bool:
        return all([
            bool(self.api_key),
            bool(self.api_key_secret),
            bool(self.access_token),
            bool(self.access_token_secret),
        ])

    @property
    def has_bearer(self) -> bool:
        return bool(self.bearer_token)


@dataclass
class SentXPost:
    tweets: tuple[str, ...]
    tweet_ids: tuple[str, ...]
    dry_run: bool
    sent_at_iso: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


async def send_x_thread(
    *,
    tweets: tuple[str, ...],
    config: XPostConfig,
    creds: XCredentials,
    media_path: Path | None = None,
) -> SentXPost:
    """Sequentially upload thread text via the X API.

    Args:
        tweets: Tweet bodies to upload. (Currently single-tweet mode, so length 1.)
        config: enabled/dry_run/timeout.
        creds: OAuth1 / bearer.
        media_path: (Optional) PNG image to attach to the first tweet. If the file
                    does not exist or upload fails, post text only (still send,
                    even without the image). If OAuth1 auth is missing
                    (bearer-only), skip the image attachment.

    Raises:
        RuntimeError: missing credentials / API failure (text post itself fails).
    """
    if not tweets:
        raise RuntimeError("send_x_thread: empty tweet list")

    if config.dry_run:
        logger.info(
            "x_publisher: DRY_RUN thread_size=%d first_tweet=%r media=%s",
            len(tweets), tweets[0][:120], str(media_path) if media_path else "none",
        )
        return SentXPost(tweets=tweets, tweet_ids=tuple(), dry_run=True)

    if requests is None:
        raise RuntimeError("x_publisher: requests package not installed")

    if not (creds.has_oauth1 or creds.has_bearer):
        raise RuntimeError("x_publisher: missing credentials (OAuth1 or bearer token)")

    # ── Pre-upload image (best-effort) ─────────────────────────────
    media_id: str | None = None
    if media_path is not None:
        media_id = await _upload_media_best_effort(
            media_path=media_path, timeout_s=config.timeout_s, creds=creds,
        )

    tweet_ids: list[str] = []
    reply_to_id: str | None = None
    for idx, text in enumerate(tweets):
        # Media is attached only to the first tweet (X policy: attach to head of thread).
        attached_media_id = media_id if idx == 0 else None
        tweet_id = await asyncio.to_thread(
            _post_one_tweet_sync,
            text=text,
            reply_to_id=reply_to_id,
            timeout_s=config.timeout_s,
            creds=creds,
            media_id=attached_media_id,
        )
        tweet_ids.append(tweet_id)
        reply_to_id = tweet_id
        logger.info(
            "x_publisher: posted %d/%d tweet_id=%s media=%s",
            idx + 1, len(tweets), tweet_id,
            attached_media_id or "none",
        )

    return SentXPost(tweets=tweets, tweet_ids=tuple(tweet_ids), dry_run=False)


async def _upload_media_best_effort(
    *,
    media_path: Path,
    timeout_s: float,
    creds: XCredentials,
) -> str | None:
    """Upload an image via X v1.1 media/upload → return media_id_string.

    Best-effort: returns None on any failure (caller proceeds with image-less
    posting). Skipped entirely when OAuth1 is missing (v1.1 upload is not
    possible with bearer-only auth).
    """
    if not creds.has_oauth1:
        logger.warning(
            "x_publisher: media upload skipped — OAuth1 credentials required "
            "(media upload is not possible with a bearer token).",
        )
        return None
    if not media_path.exists():
        logger.warning(
            "x_publisher: media upload skipped — file not found: %s", media_path,
        )
        return None

    try:
        return await asyncio.to_thread(
            _upload_media_sync,
            media_path=media_path,
            timeout_s=timeout_s,
            creds=creds,
        )
    except Exception as exc:  # noqa: BLE001
        # The body must still be posted even without the image, so don't raise — return None.
        logger.warning(
            "x_publisher: media upload FAILED (continuing without image): %s", exc,
        )
        return None


def _upload_media_sync(
    *,
    media_path: Path,
    timeout_s: float,
    creds: XCredentials,
) -> str:
    """Synchronous v1.1 simple media upload (≤ 5 MB). Returns media_id_string."""
    assert requests is not None  # guarded by caller
    if OAuth1 is None:
        raise RuntimeError("x_publisher: requests-oauthlib not installed")

    auth = OAuth1(
        creds.api_key,
        creds.api_key_secret,
        creds.access_token,
        creds.access_token_secret,
    )
    with media_path.open("rb") as f:
        files = {"media": (media_path.name, f, "image/png")}
        response = requests.post(
            _X_MEDIA_UPLOAD_URL,
            files=files,
            auth=auth,
            timeout=max(3.0, float(timeout_s)),
        )
    if response.status_code >= 300:
        raise RuntimeError(
            "media upload API error "
            f"status={response.status_code} body={response.text[:500]}"
        )
    body = response.json()
    media_id = str(body.get("media_id_string", "")).strip()
    if not media_id:
        raise RuntimeError(f"media upload missing media_id_string: {body}")
    return media_id


def _post_one_tweet_sync(
    *,
    text: str,
    reply_to_id: str | None,
    timeout_s: float,
    creds: XCredentials,
    media_id: str | None = None,
) -> str:
    """One synchronous HTTP call. Run via to_thread from the async wrapper."""
    assert requests is not None  # guarded by caller
    payload: dict[str, object] = {"text": text}
    if reply_to_id:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to_id}
    if media_id:
        payload["media"] = {"media_ids": [media_id]}

    headers = {"Content-Type": "application/json"}
    request_kwargs: dict[str, object] = {
        "url": _X_POST_URL,
        "json": payload,
        "headers": headers,
        "timeout": max(3.0, float(timeout_s)),
    }

    # Prefer OAuth1.
    if creds.has_oauth1:
        if OAuth1 is None:
            raise RuntimeError("x_publisher: requests-oauthlib not installed")
        request_kwargs["auth"] = OAuth1(
            creds.api_key,
            creds.api_key_secret,
            creds.access_token,
            creds.access_token_secret,
        )
    else:
        # Bearer fallback (must be a user-context token to post).
        headers["Authorization"] = f"Bearer {creds.bearer_token}"

    response = requests.post(**request_kwargs)
    if response.status_code >= 300:
        raise RuntimeError(
            "x_publisher: API error "
            f"status={response.status_code} body={response.text[:500]}"
        )
    body = response.json()
    tweet_id = str(body.get("data", {}).get("id", "")).strip()
    if not tweet_id:
        raise RuntimeError(f"x_publisher: missing tweet id in response: {body}")
    return tweet_id


__all__ = [
    "XCredentials",
    "XPostConfig",
    "SentXPost",
    "send_x_thread",
]

