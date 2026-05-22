"""
channels/truth_social/collector.py — Trump Truth Social timeline collector.

────────────────────────────────────────────────────────────────────────
Responsibilities:
  GET directly against the Mastodon-compatible public API endpoint via
  ``curl_cffi.AsyncSession`` and fetch only Trump's new posts. No token /
  login needed (since the 2025-08-27 policy, Truth Social exposes
  prominent figures' statuses unauthenticated).

  Two call modes:

  1) live polling — invoked each cycle by the anomaly daemon.
        await collector.fetch_recent(since_id=...)
     => Returns posts from the most recent down to just after since_id.
        On the first call, only up to `limit` items (default 5) to avoid backlog bursts.

  2) backfill — invoked by scripts/truth_social_backfill.py.
        await collector.fetch_range(since=ts1, until=ts2)
     => Paginates with the Mastodon ``max_id`` cursor and collects every
        post in the time range. For reference DB curation.

────────────────────────────────────────────────────────────────────────
Cloudflare evasion (2026-05-22 update — P12-E):

  Truth Social sits behind Cloudflare, so a plain httpx request gets
  flagged by JA3/TLS fingerprinting → 403 "Just a moment..." (Cloudflare
  JS challenge page).

  Mitigation: use ``curl_cffi.requests.AsyncSession(impersonate="chrome116")``
  which mimics a real Chrome ClientHello + HTTP/2 SETTINGS frame, bypassing
  Cloudflare bot detection.
  (Measured 2026-05-22: chrome119+ is blocked. chrome116/110 and safari17_0
  pass. Truth Social's web client appears to expect a ~Chrome 116 fingerprint.) Other layers stay the same:
    · Standard browser User-Agent (overridable via env)
    · Accept-Language: en-US,en;q=0.9
    · 5min + ±60s jitter (handled at the channel layer)
    · 429/5xx → tenacity exponential backoff (max 3 attempts)

────────────────────────────────────────────────────────────────────────
Env vars:

  TRUTH_SOCIAL_TIMEOUT_S    int (default 30)  — per-request timeout
  TRUTH_SOCIAL_USER_AGENT   str               — UA override (default usually fine)
  TRUTH_SOCIAL_HOST         str (default "https://truthsocial.com")
  TRUTH_SOCIAL_IMPERSONATE  str (default "chrome116") — curl_cffi profile key.
      Measured (2026-05-22): chrome116, chrome110, safari17_0, safari15_5
      all pass. chrome119+ is blocked. Recommended fallback: "safari17_0".

──────────────────────────────────────────────────────────────────────
Data sources — "direct" vs "gcs" (P12-E.2, 2026-05-22):

  Measurements show Cloudflare blocks GCP-datacenter IPs regardless of
  fingerprint (Cloud Run cannot pass with any impersonate profile).
  Two source modes are supported:

    source="direct"  (default, for local/dev)
        Fetches Truth Social directly via curl_cffi. Works from
        residential IPs, Azure (GitHub Actions), and local Docker.
        Blocked on Cloud Run.

    source="gcs"  (production on Cloud Run)
        Reads gs://<bucket>/<prefix>/latest.json every cycle. A separate
        process (e.g. tools/truthsocial_publisher/ running on GitHub
        Actions) fetches the data via curl_cffi and uploads the snapshot.

──────────────────────────────────────────────────────────────────────
Additional env vars:

  TRUTH_SOCIAL_SOURCE      str (default "direct") — "direct" or "gcs".
  TRUTH_SOCIAL_GCS_BUCKET  str (default "anomaly-truthsocial") — bucket name.
  TRUTH_SOCIAL_GCS_PREFIX  str (default "realDonaldTrump")   — object prefix.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .normalize import TruthPost, normalize_status

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────
# realDonaldTrump's Mastodon-internal account ID. Documented in snscrape #493
# and the truthbrush README. A permanent identifier, safe to hardcode.
TRUMP_ACCOUNT_ID: str = "107780257626128497"

_DEFAULT_HOST: str = "https://truthsocial.com"
_STATUSES_PATH: str = "/api/v1/accounts/{account_id}/statuses"

# Cloudflare lets through common browser fingerprints. Chrome on macOS has the
# closest trace to Truth Social's official web client, so we use it as default.
_DEFAULT_UA: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


class TruthSocialApiError(RuntimeError):
    """Truth Social API non-2xx response (including Cloudflare block)."""


# ────────────────────────────────────────────────────────────────────
# Collector
# ────────────────────────────────────────────────────────────────────
class TrumpCollector:
    """Collector that polls only Trump's Truth Social timeline.

    In-memory state:
      · ``_last_seen_id`` — dedupe key populated by fetch_recent. Reset on daemon
        restart, so the fetch_recent call right after restart only returns the
        most recent `limit` items (same strategy as XCollector).

    Multi-threading safety: the AsyncClient internal connection pool is
    thread-safe. Standard usage is a single instance per process.
    """

    def __init__(
        self,
        *,
        account_id: str = TRUMP_ACCOUNT_ID,
        host: str | None = None,
        user_agent: str | None = None,
        timeout_s: float | None = None,
        client: AsyncSession | None = None,
        impersonate: str | None = None,
        source: Literal["direct", "gcs"] | None = None,
        gcs_bucket: str | None = None,
        gcs_prefix: str | None = None,
    ) -> None:
        self.account_id = account_id
        self.host = (
            host or os.environ.get("TRUTH_SOCIAL_HOST", _DEFAULT_HOST)
        ).rstrip("/")
        self.user_agent = (
            user_agent
            or os.environ.get("TRUTH_SOCIAL_USER_AGENT", _DEFAULT_UA)
        )
        self.timeout_s = (
            timeout_s
            if timeout_s is not None
            else float(os.environ.get("TRUTH_SOCIAL_TIMEOUT_S", "30"))
        )
        # curl_cffi impersonate profile — measured (2026-05-22):
        #   ✅ chrome116, chrome110, safari17_0, safari15_5 all pass
        #   ❌ chrome119+ blocked (Truth Social's web client appears to expect
        #      a ~Chrome 116 fingerprint).
        # Default to chrome116; env override allows safari17_0 fallback.
        self.impersonate = (
            impersonate
            or os.environ.get("TRUTH_SOCIAL_IMPERSONATE", "chrome116")
        )

        # ── Source mode (P12-E.2) ─────────────────────────────────────
        # "direct" = call Truth Social directly via curl_cffi (works on
        #            residential / Azure IPs only).
        # "gcs"    = read the latest.json snapshot that an external
        #            publisher (GitHub Actions, etc.) has written. Required
        #            on Cloud Run since Cloudflare blocks GCP IPs.
        env_source = os.environ.get("TRUTH_SOCIAL_SOURCE", "direct").lower()
        chosen: Literal["direct", "gcs"] = (
            source if source is not None else ("gcs" if env_source == "gcs" else "direct")
        )
        self.source: Literal["direct", "gcs"] = chosen
        self.gcs_bucket = (
            gcs_bucket
            or os.environ.get("TRUTH_SOCIAL_GCS_BUCKET", "anomaly-truthsocial")
        )
        self.gcs_prefix = (
            gcs_prefix
            or os.environ.get("TRUTH_SOCIAL_GCS_PREFIX", "realDonaldTrump")
        ).strip("/")

        # Allow tests to inject a mock client — external injection takes precedence.
        self._client = client
        self._owns_client = client is None

        # gcs mode reuses a single storage.Client per process (lazy init).
        self._gcs_client: Any = None

        self._last_seen_id: str | None = None

        logger.info(
            "TrumpCollector init: source=%s, impersonate=%s, "
            "gcs=gs://%s/%s/latest.json",
            self.source, self.impersonate, self.gcs_bucket, self.gcs_prefix,
        )

    # ── Lifecycle ──────────────────────────────────────────────────
    async def aclose(self) -> None:
        """Close only externally-owned clients. Injected ones are the caller's responsibility.

        curl_cffi.AsyncSession exposes async ``close()``. We also fall back
        to ``aclose()`` so that httpx-style mock clients keep working.
        """
        if self._owns_client and self._client is not None:
            close_fn = getattr(self._client, "close", None)
            try:
                if close_fn is not None:
                    result = close_fn()
                    if asyncio.iscoroutine(result):
                        await result
                else:
                    aclose_fn = getattr(self._client, "aclose", None)
                    if aclose_fn is not None:
                        await aclose_fn()
            except Exception as e:  # noqa: BLE001 — best-effort close
                logger.warning("TrumpCollector.aclose: client close failed: %s", e)
            self._client = None

    async def __aenter__(self) -> TrumpCollector:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ── Public — live polling ──────────────────────────────────────
    async def fetch_recent(
        self,
        *,
        limit: int = 20,
        exclude_replies: bool = True,
        exclude_reblogs: bool = False,
    ) -> list[TruthPost]:
        """Fetch one page (limit items) of the most recent posts, then dedupe.

        Args:
            limit: number of statuses per request (Mastodon default max 40). 20 is
                enough for a 5-min daemon cycle — Trump posting 20 within 5 min is highly unlikely.
            exclude_replies: True → filter out replies (Mastodon distinguishes reply
                from retruth).
            exclude_reblogs: False (default) — include retruths. Adjust LLM weight via
                ``TruthPost.is_reblog`` flag during analysis.

        Returns:
            Only posts newer than ``_last_seen_id`` (ascending by time). On first call,
            return all `limit` items and set ``_last_seen_id`` to the most recent
            post — automatic backlog-burst prevention takes effect from the next cycle.
        """
        page = await self._get_page(
            limit=limit,
            exclude_replies=exclude_replies,
            exclude_reblogs=exclude_reblogs,
        )
        if not page:
            return []

        posts = [normalize_status(s) for s in page]
        posts.sort(key=lambda p: p.created_at)

        # First polling — don't emit backlog; just prime cursor at most recent post.
        if self._last_seen_id is None:
            most_recent_id = max(posts, key=lambda p: p.created_at).post_id
            self._last_seen_id = most_recent_id
            logger.info(
                "truth_social initial poll — primed last_seen_id=%s "
                "(skipping %d backlog posts)",
                most_recent_id,
                len(posts),
            )
            return []

        # Emit only posts newer than since_id. ID is snowflake-like (nearly
        # monotonic in time), but to be exact cut on created_at.
        cursor_post = next(
            (p for p in posts if p.post_id == self._last_seen_id),
            None,
        )
        if cursor_post is not None:
            new_posts = [p for p in posts if p.created_at > cursor_post.created_at]
        else:
            # Cursor falls outside this page — treat every post as new.
            # (Practically impossible within 5 min when limit ≥ 20.)
            new_posts = posts

        if new_posts:
            self._last_seen_id = new_posts[-1].post_id
            logger.info(
                "truth_social poll — %d new post(s), advance last_seen_id=%s",
                len(new_posts),
                self._last_seen_id,
            )
        return new_posts

    # ── Public — backfill ──────────────────────────────────────────
    async def fetch_range(
        self,
        *,
        since: datetime,
        until: datetime,
        max_pages: int = 200,
        exclude_replies: bool = False,
        exclude_reblogs: bool = False,
        page_sleep_s: float = 2.0,
    ) -> AsyncIterator[TruthPost]:
        """Async-yield every post in ``since`` ≤ created_at < ``until``.

        Args:
            since: start time (inclusive, UTC aware).
            until: end time (exclusive, UTC aware).
            max_pages: hard cap to prevent infinite loops. 40 per page × 200 = 8K posts.
                Assuming 50 posts/day from Trump, covers ~160 days — enough for backfill.
            exclude_replies/exclude_reblogs: same meaning as in live polling.
            page_sleep_s: sleep between pages (avoids Cloudflare 1015 rate limit).
                Measured: 2 seconds is the safe line. Large ranges like ``--last-hours 14*24``
                avoid 429. With 0.0 it bursts — never set this in production (live polling
                uses only fetch_recent, so this default does not affect it).

        Yields:
            TruthPost — descending by created_at (the order Mastodon API returns).

        Note:
            Mastodon timeline only pages backward via ``max_id`` cursor. We use the
            oldest id in the returned page list as the next request's max_id. If the
            page is empty or goes past ``since``, stop.
        """
        if since.tzinfo is None or until.tzinfo is None:
            raise ValueError("since/until must be timezone-aware UTC datetimes")
        if since >= until:
            return

        max_id: str | None = None
        for page_idx in range(max_pages):
            if page_idx > 0 and page_sleep_s > 0:
                await asyncio.sleep(page_sleep_s)
            page = await self._get_page(
                limit=40,
                max_id=max_id,
                exclude_replies=exclude_replies,
                exclude_reblogs=exclude_reblogs,
            )
            if not page:
                logger.info(
                    "backfill page %d — empty, stop (since=%s, until=%s)",
                    page_idx, since.isoformat(), until.isoformat(),
                )
                return

            oldest_in_page: datetime | None = None
            yielded_in_page = 0
            for raw in page:
                post = normalize_status(raw)
                if oldest_in_page is None or post.created_at < oldest_in_page:
                    oldest_in_page = post.created_at
                if post.created_at >= until:
                    continue  # too recent → skip
                if post.created_at < since:
                    continue  # too old → skip — finish this page then decide to stop
                yield post
                yielded_in_page += 1

            logger.info(
                "backfill page %d — got=%d, yielded=%d, oldest=%s",
                page_idx, len(page), yielded_in_page,
                oldest_in_page.isoformat() if oldest_in_page else "—",
            )

            # Next cursor — this page's oldest status id.
            # (Cursor ordering is by id, but Mastodon snowflake is close to
            #  monotonic in time, so ``min(id, key=int)`` is a safe approximation. Use raw as-is.)
            max_id = min((str(s["id"]) for s in page), key=lambda s: int(s))

            if oldest_in_page is not None and oldest_in_page < since:
                logger.info(
                    "backfill — oldest post in page (%s) is before since (%s), stop",
                    oldest_in_page.isoformat(), since.isoformat(),
                )
                return

    # ── Private — HTTP plumbing ────────────────────────────────────
    async def _get_page(
        self,
        *,
        limit: int = 40,
        max_id: str | None = None,
        exclude_replies: bool = True,
        exclude_reblogs: bool = False,
    ) -> list[dict[str, Any]]:
        """Return one page of raw Mastodon status dicts.

        Two source modes:
          · "direct" — GET /api/v1/.../statuses via curl_cffi.
          · "gcs"    — read gs://<bucket>/<prefix>/latest.json (max_id
                      pagination is not available, so backfill must use
                      "direct").
        """
        if self.source == "gcs":
            if max_id is not None:
                raise NotImplementedError(
                    "gcs source does not support max_id pagination — "
                    "use source='direct' for backfill",
                )
            return await self._get_page_from_gcs(
                limit=limit,
                exclude_replies=exclude_replies,
                exclude_reblogs=exclude_reblogs,
            )

        url = self.host + _STATUSES_PATH.format(account_id=self.account_id)
        params: dict[str, Any] = {
            "limit": int(limit),
            "exclude_replies": "true" if exclude_replies else "false",
            "exclude_reblogs": "true" if exclude_reblogs else "false",
        }
        if max_id is not None:
            params["max_id"] = max_id

        client = await self._ensure_client()

        # Wrap with tenacity — retry only 429/5xx/network errors; 4xx (other) raises immediately.
        # Cloudflare 1015 (429) is usually a 30-60s ban — bump wait_exponential max to 60s
        # so the 2nd/3rd attempt outlives the ban window.
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=2.0, min=5, max=60),
            retry=retry_if_exception_type(
                (CurlRequestException, TruthSocialApiError),
            ),
            reraise=True,
        ):
            with attempt:
                resp = await client.get(url, params=params, timeout=self.timeout_s)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise TruthSocialApiError(
                        f"truth_social api {resp.status_code} on {url}: "
                        f"{resp.text[:200]}",
                    )
                if resp.status_code >= 400:
                    # 4xx (403/404 etc.) — Cloudflare block or endpoint change.
                    # Retrying does not recover, raise immediately (tenacity does not retry).
                    logger.warning(
                        "truth_social api 4xx %d on %s: %s",
                        resp.status_code, url, resp.text[:200],
                    )
                    raise TruthSocialApiError(
                        f"truth_social api {resp.status_code} on {url}",
                    )
                data = resp.json()
                if not isinstance(data, list):
                    raise TruthSocialApiError(
                        f"unexpected response shape (not a list): "
                        f"{type(data).__name__}",
                    )
                return data
        # Unreachable due to tenacity reraise=True — listed for the type checker.
        return []

    async def _ensure_client(self) -> AsyncSession:
        """Lazy create curl_cffi.requests.AsyncSession.

        ``impersonate=`` is what unlocks Cloudflare — it mimics the TLS
        ClientHello + HTTP/2 SETTINGS frame of a real Chrome browser so
        the request is not flagged as a bot. The User-Agent header is
        still set as belt-and-suspenders, but impersonate operates at a
        deeper layer.
        """
        if self._client is None:
            # Headers mirror the actual Truth Social web client. impersonate
            # covers TLS+HTTP/2 layers; these headers align the application
            # layer, lowering Cloudflare's bot score.
            self._client = AsyncSession(
                impersonate=self.impersonate,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Referer": f"{self.host}/",
                    "Origin": self.host,
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                },
                timeout=self.timeout_s,
            )
        return self._client

    # ── Private — GCS source plumbing (P12-E.2) ────────────────────
    async def _get_page_from_gcs(
        self,
        *,
        limit: int,
        exclude_replies: bool,
        exclude_reblogs: bool,
    ) -> list[dict[str, Any]]:
        """Read gs://<bucket>/<prefix>/latest.json and return its `posts`.

        latest.json schema (produced by tools/truthsocial_publisher/main.py):
            {
              "fetched_at": "...",
              "source": "...",
              "account_id": "...",
              "posts": [ <raw Mastodon status>, ... ]   # most-recent first
            }

        ``google-cloud-storage`` is a sync library, so the blocking call is
        wrapped in ``asyncio.to_thread`` to keep the event loop responsive.
        """
        bucket = self.gcs_bucket
        path = f"{self.gcs_prefix}/latest.json"

        def _blocking_read() -> bytes:
            client = self._ensure_gcs_client()
            blob = client.bucket(bucket).blob(path)
            return blob.download_as_bytes()

        try:
            raw_bytes = await asyncio.to_thread(_blocking_read)
        except Exception as e:  # noqa: BLE001 — best-effort GCS read
            logger.warning(
                "truth_social GCS read failed (bucket=%s, path=%s): %s",
                bucket, path, e,
            )
            return []

        try:
            envelope = json.loads(raw_bytes)
        except json.JSONDecodeError as e:
            logger.warning(
                "truth_social GCS latest.json is not valid JSON: %s", e,
            )
            return []

        posts = envelope.get("posts") if isinstance(envelope, dict) else None
        if not isinstance(posts, list):
            logger.warning(
                "truth_social GCS latest.json missing 'posts' list "
                "(envelope keys=%s)",
                list(envelope.keys()) if isinstance(envelope, dict) else "?",
            )
            return []

        # Apply the same filter/limit semantics the Mastodon API would,
        # but on the client side now.
        filtered = []
        for raw in posts:
            if not isinstance(raw, dict):
                continue
            if exclude_replies and raw.get("in_reply_to_id"):
                continue
            if exclude_reblogs and raw.get("reblog"):
                continue
            filtered.append(raw)
            if len(filtered) >= limit:
                break

        fetched_at = envelope.get("fetched_at") if isinstance(envelope, dict) else "?"
        logger.debug(
            "truth_social GCS read ok — posts=%d (filtered=%d), fetched_at=%s",
            len(posts), len(filtered), fetched_at,
        )
        return filtered

    def _ensure_gcs_client(self) -> Any:
        """Lazy ``storage.Client`` — cached per process (heavy to construct)."""
        if self._gcs_client is None:
            # Lazy import so "direct" mode users don't pay the import cost.
            from google.cloud import storage  # type: ignore

            self._gcs_client = storage.Client()
        return self._gcs_client


__all__ = [
    "TRUMP_ACCOUNT_ID",
    "TruthSocialApiError",
    "TrumpCollector",
]


# ────────────────────────────────────────────────────────────────────
# `since_id` helper — for injecting cursor as external state in the production daemon.
# (Unused before daemon registration at v0.7.18; exposed in advance.)
# ────────────────────────────────────────────────────────────────────
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
