"""
x/collector.py — Real X (Twitter) post collector (X API v2, OAuth2 Bearer).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5.4.1, plan P9.4 / EVT-1):

  Fetch the most recent posts from the watchlist's X accounts and emit dict payloads.
  Channel's polling loop calls this every cycle (10 min).

  Pipeline position:
      (this module) → parser → stage1_filter → llm_classifier → ChannelSignal

────────────────────────────────────────────────────────────────────────
Data source — X API v2 (PAYG, user decision 2026-05-04):

  · Endpoint: GET /2/users/by/username + GET /2/users/{id}/tweets
  · Auth: Bearer (env X_API_BEARER_TOKEN, GCP Secret Manager mount)
  · Pricing: $0.005/post read, $0.010/user lookup (cached)

  Previously snscrape was primary with X API as fallback, but X started
  aggressively blocking unauthenticated scraping; calls from GCP IPs failed
  almost always, and per-cycle retries flooded the logs. After the user
  switched to X API PAYG we removed the snscrape branch — simplified to a
  single path (X API only).

────────────────────────────────────────────────────────────────────────
Post payload format (compatible with parser.py + stage1_filter + llm_classifier):

  {
    "id":         str,        # tweet id (snowflake)
    "user":       str,        # account handle (no @, lowercase)
    "text":       str,        # post body
    "timestamp":  int,        # unix seconds (UTC)
    "url":        str,        # https://x.com/{user}/status/{id}
    "image_urls": list[str],  # attached images (for vision input)
    "source":     "x_api",
  }

────────────────────────────────────────────────────────────────────────
Dedupe:

  · per-account `last_seen_id` (in-memory) — prevents re-emitting the same post.
  · Only emit posts with id greater than last_seen_id (X snowflake id = chronological).
  · On the first fetch, emit only the most recent 1 post (avoid initial backlog flood).
  · user_id (`/2/users/by/username`) is cached in-memory — one call per account.

────────────────────────────────────────────────────────────────────────
Error handling (per-account):

  · One account's failure does not kill the entire cycle.
  · X API non-2xx → log warn → empty list.
  · network timeout → log warn → empty list.
  · Retried on the next cycle (auto-recovery).

────────────────────────────────────────────────────────────────────────
Env vars:

  X_API_BEARER_TOKEN     X API v2 Bearer (required — collector goes idle without it)
  X_COLLECTOR_TIMEOUT_S  int (default 30) — per-account fetch timeout

────────────────────────────────────────────────────────────────────────
Plan: P9.4 / EVT-1 (X channel real collector)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# =====================================================================
# Main class
# =====================================================================
class XCollector:
    """X account polling collector — X API v2 (Bearer).

    Args:
        accounts: list of X account handles to monitor (e.g. ["lookonchain", "mlmabc"]).
                  '@' is stripped automatically.
        x_api_bearer_token: X API v2 Bearer. None → try env X_API_BEARER_TOKEN.
                            If both are missing the collector stays idle (returns empty list).
        max_per_account: max posts per account per cycle (default 10).
        timeout_s: per-account fetch timeout (default 30s).
    """

    def __init__(
        self,
        *,
        accounts: list[str],
        x_api_bearer_token: str | None = None,
        max_per_account: int = 10,
        timeout_s: float = 30.0,
    ) -> None:
        self._accounts = [a.lstrip("@").lower() for a in accounts if a]
        if not self._accounts:
            logger.warning("XCollector: empty accounts list — collector idle")

        # env fallback
        self._bearer = x_api_bearer_token or os.getenv("X_API_BEARER_TOKEN")
        if not self._bearer:
            logger.warning(
                "XCollector: X_API_BEARER_TOKEN not set — collector will be idle"
            )

        env_timeout = os.getenv("X_COLLECTOR_TIMEOUT_S")
        if env_timeout and env_timeout.isdigit():
            timeout_s = float(env_timeout)

        self._timeout_s = max(5.0, float(timeout_s))
        self._max_per_account = max(1, int(max_per_account))

        # per-account dedupe state — last seen tweet id
        self._last_seen_id: dict[str, int] = {}

        # X API user_id cache (username → user_id, long-lived)
        self._user_id_cache: dict[str, str] = {}

        # httpx client (lazy create)
        self._http_client: httpx.AsyncClient | None = None

        self._is_open = False

        logger.info(
            "XCollector: initialized (accounts=%d, x_api=%s, "
            "max_per_account=%d, timeout=%.0fs)",
            len(self._accounts),
            "on" if self._bearer else "off",
            self._max_per_account,
            self._timeout_s,
        )

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    async def open(self) -> None:
        """Lazy create httpx client. Compatible with mock_collector's open()."""
        if self._is_open:
            return
        self._http_client = httpx.AsyncClient(timeout=self._timeout_s)
        self._is_open = True

    async def close(self) -> None:
        """Close httpx client."""
        if not self._is_open:
            return
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._is_open = False

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def accounts(self) -> list[str]:
        return list(self._accounts)

    # ─────────────────────────────────────────────────────────────────
    # Public API — called every polling cycle
    # ─────────────────────────────────────────────────────────────────
    async def fetch_recent_posts(self) -> list[dict[str, Any]]:
        """Fetch new posts from all watchlist accounts → return as a list.

        Returns:
            list[dict]. Each dict is parser.py's expected format. May be empty.
        """
        if not self._is_open:
            await self.open()

        all_posts: list[dict[str, Any]] = []
        for username in self._accounts:
            try:
                posts = await self._fetch_one_account(username)
                all_posts.extend(posts)
            except Exception as e:
                # Guard so one account's failure does not kill the cycle
                logger.warning(
                    "XCollector: account=%s fetch failed (will retry next cycle): %s",
                    username, e,
                )

        if all_posts:
            logger.info(
                "XCollector: fetched %d new posts across %d accounts",
                len(all_posts), len(self._accounts),
            )
        return all_posts

    # ─────────────────────────────────────────────────────────────────
    # Per-account fetch — X API v2 only
    # ─────────────────────────────────────────────────────────────────
    async def _fetch_one_account(self, username: str) -> list[dict[str, Any]]:
        """Fetch new posts for one account (X API v2)."""
        last_seen = self._last_seen_id.get(username, 0)

        if not self._bearer:
            return []

        try:
            posts = await self._x_api_fetch(username)
        except Exception as e:
            logger.warning(
                "XCollector: X API fetch failed for %s: %s", username, e,
            )
            return []

        # Dedupe — only ids greater than last_seen_id
        new_posts = [p for p in posts if int(p["id"]) > last_seen]
        if new_posts:
            max_id = max(int(p["id"]) for p in new_posts)
            self._last_seen_id[username] = max_id

        # On first call (last_seen=0), if too many, keep only the newest 1 — backlog burst prevention
        if last_seen == 0 and len(new_posts) > 1:
            new_posts.sort(key=lambda p: int(p["id"]), reverse=True)
            new_posts = new_posts[:1]

        return new_posts

    # ─────────────────────────────────────────────────────────────────
    # X API v2 (httpx, async)
    # ─────────────────────────────────────────────────────────────────
    async def _x_api_fetch(self, username: str) -> list[dict[str, Any]]:
        """Fetch latest N tweets for one account via X API v2."""
        if not self._bearer or self._http_client is None:
            return []

        # 1) Lookup user_id (cached)
        user_id = await self._x_api_lookup_user_id(username)
        if not user_id:
            return []

        # 2) Fetch tweets
        url = f"https://api.twitter.com/2/users/{user_id}/tweets"
        headers = {"Authorization": f"Bearer {self._bearer}"}
        params: dict[str, Any] = {
            "max_results": min(max(5, self._max_per_account), 100),
            "tweet.fields": "created_at,attachments,public_metrics",
            "expansions": "attachments.media_keys",
            "media.fields": "url,preview_image_url,type",
        }

        try:
            r = await self._http_client.get(url, headers=headers, params=params)
        except httpx.HTTPError as e:
            raise RuntimeError(f"X API tweets request failed: {e}") from e

        if r.status_code != 200:
            raise RuntimeError(
                f"X API tweets returned {r.status_code}: {r.text[:200]}"
            )

        data = r.json()
        tweets = data.get("data", []) or []
        media_lookup = self._build_media_lookup(data.get("includes", {}))

        out: list[dict[str, Any]] = []
        for t in tweets:
            try:
                tweet_id = str(t["id"])
                text = str(t.get("text", ""))
                ts_iso = t.get("created_at", "")
                ts_unix = self._iso_to_unix(ts_iso)

                # Extract images
                image_urls: list[str] = []
                media_keys = (t.get("attachments", {}) or {}).get("media_keys", [])
                for mk in media_keys:
                    media = media_lookup.get(mk)
                    if media:
                        if media.get("type") == "photo" and media.get("url"):
                            image_urls.append(str(media["url"]))
                        elif media.get("preview_image_url"):
                            image_urls.append(str(media["preview_image_url"]))

                out.append({
                    "id": tweet_id,
                    "user": username.lower(),
                    "text": text,
                    "timestamp": ts_unix,
                    "url": f"https://x.com/{username}/status/{tweet_id}",
                    "image_urls": image_urls,
                    "source": "x_api",
                })
            except Exception as e:
                logger.debug("XCollector: X API tweet parse failed: %s", e)
                continue

        return out

    async def _x_api_lookup_user_id(self, username: str) -> str | None:
        """Resolve username → user_id via X API. Uses cache (permanent)."""
        if username in self._user_id_cache:
            return self._user_id_cache[username]

        if not self._bearer or self._http_client is None:
            return None

        url = f"https://api.twitter.com/2/users/by/username/{username}"
        headers = {"Authorization": f"Bearer {self._bearer}"}

        try:
            r = await self._http_client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("XCollector: X API user lookup HTTP error %s: %s", username, e)
            return None

        if r.status_code != 200:
            logger.warning(
                "XCollector: X API user lookup failed %s: %d %s",
                username, r.status_code, r.text[:200],
            )
            return None

        data = r.json().get("data", {})
        user_id = data.get("id")
        if user_id:
            self._user_id_cache[username] = str(user_id)
        return user_id

    # ─────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _build_media_lookup(includes: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """X API response includes.media → {media_key: media_obj} dict."""
        media_list = includes.get("media", []) or []
        return {str(m["media_key"]): m for m in media_list if "media_key" in m}

    @staticmethod
    def _iso_to_unix(iso_str: str) -> int:
        """X API ISO8601 (e.g. '2026-04-19T12:34:56.000Z') → unix seconds."""
        if not iso_str:
            return 0
        try:
            from datetime import datetime
            # Python 3.11+ fromisoformat handles 'Z'
            iso_str = iso_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
            return int(dt.timestamp())
        except Exception:
            return 0
