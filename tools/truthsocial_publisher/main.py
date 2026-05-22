"""
tools/truthsocial_publisher/main.py — Trump Truth Social → GCS publisher.

──────────────────────────────────────────────────────────────────────
What this does
──────────────────────────────────────────────────────────────────────
1. Runs from GitHub Actions (Azure datacenter IP) every 5 minutes.
2. Fetches Trump's latest statuses from Truth Social's Mastodon-compatible
   public API using curl_cffi with a Chrome 116 TLS fingerprint
   (passes Cloudflare bot detection).
3. Uploads the result to GCS as
     gs://<BUCKET>/realDonaldTrump/latest.json
   (and a timestamped snapshot for history/debugging).
4. The Cloud Run anomaly daemon reads `latest.json` and treats it
   as if it had fetched Truth Social directly.

──────────────────────────────────────────────────────────────────────
Why GitHub Actions and not Cloud Run?
──────────────────────────────────────────────────────────────────────
Cloudflare aggressively blocks GCP-datacenter IPs even with a perfect
TLS fingerprint (verified 2026-05-22 with chrome116/chrome120/safari17_0
— all return HTTP 403 from Cloud Run while the *same* Docker image
succeeds from a residential IP). GitHub Actions runs on Azure IPs,
which Cloudflare currently treats more leniently for Truth Social.

──────────────────────────────────────────────────────────────────────
Inputs (env vars set in the GitHub workflow)
──────────────────────────────────────────────────────────────────────
  GCS_BUCKET                target bucket name (e.g. "anomaly-truthsocial")
  GCS_OBJECT_PREFIX         optional GCS prefix (default "realDonaldTrump")
  TRUTH_SOCIAL_LIMIT        how many recent statuses to keep (default 40)
  TRUTH_SOCIAL_IMPERSONATE  curl_cffi browser profile (default "chrome116")
  GOOGLE_APPLICATION_CREDENTIALS
                            path to the service-account JSON key

──────────────────────────────────────────────────────────────────────
Outputs (in GCS)
──────────────────────────────────────────────────────────────────────
  gs://<BUCKET>/<PREFIX>/latest.json
      {
        "fetched_at": "2026-05-22T12:55:00Z",
        "source": "github-actions:run_id=...",
        "account_id": "107780257626128497",
        "posts": [ <raw Mastodon status>, ... ]    # most-recent first
      }

  gs://<BUCKET>/<PREFIX>/history/2026/05/22/12-55-00Z.json
      Same payload — useful for debugging / replay.

──────────────────────────────────────────────────────────────────────
Exit codes
──────────────────────────────────────────────────────────────────────
  0  success
  2  fetch failed (Cloudflare 403, network, etc.)
  3  upload failed (GCS auth / quota / etc.)
"""

from __future__ import annotations

# ── 표준 라이브러리 ─────────────────────────────────────────────────
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

# ── 서드파티 ─────────────────────────────────────────────────────────
from curl_cffi.requests import Session
from google.cloud import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("truthsocial_publisher")


# ── 상수 ────────────────────────────────────────────────────────────
# Trump 의 Mastodon 내부 account ID — 영구 식별자, 안전하게 hardcode.
TRUMP_ACCOUNT_ID: str = "107780257626128497"
TS_HOST: str = "https://truthsocial.com"
STATUSES_PATH: str = f"/api/v1/accounts/{TRUMP_ACCOUNT_ID}/statuses"

# 일반 브라우저 User-Agent — Truth Social 공식 web client 와 비슷한 trace.
DEFAULT_UA: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/116.0.0.0 Safari/537.36"
)


def fetch_truth_social(*, limit: int, impersonate: str) -> list[dict[str, Any]]:
    """curl_cffi 로 Trump 의 latest statuses N개를 가져온다.

    Cloudflare 통과를 위해 ``impersonate`` 가 핵심 — TLS ClientHello +
    HTTP/2 SETTINGS frame 까지 실제 Chrome 116 을 모방. 측정 결과
    chrome119+ 는 Truth Social Cloudflare 에 막힘. chrome116 / 110 /
    safari17_0 통과.

    Raises:
        RuntimeError: 비-2xx 응답 또는 list 가 아닌 JSON.
    """
    url = TS_HOST + STATUSES_PATH
    params = {
        "limit": int(limit),
        "exclude_replies": "true",
        "exclude_reblogs": "false",
    }
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": f"{TS_HOST}/",
        "Origin": TS_HOST,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    # curl_cffi.Session 은 sync — GitHub Actions 단발 실행이라 async 불필요.
    with Session(impersonate=impersonate, headers=headers, timeout=30) as s:
        resp = s.get(url, params=params)
        if resp.status_code != 200:
            body = resp.text[:200] if resp.text else ""
            raise RuntimeError(
                f"truth_social returned status {resp.status_code}: {body}",
            )
        data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError(
                f"unexpected response shape: {type(data).__name__}",
            )
        logger.info("fetched %d statuses from truth_social", len(data))
        return data


def build_payload(posts: list[dict[str, Any]]) -> dict[str, Any]:
    """GCS 에 저장할 envelope payload."""
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "fetched_at": fetched_at,
        "source": _github_actions_source_tag(),
        "account_id": TRUMP_ACCOUNT_ID,
        "posts": posts,
    }


def _github_actions_source_tag() -> str:
    """GitHub Actions 의 run metadata 를 source 로 박는다 (디버깅용)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "unknown")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    return f"github-actions:{repo}@run_id={run_id}"


def upload_to_gcs(
    *,
    bucket_name: str,
    prefix: str,
    payload: dict[str, Any],
) -> None:
    """latest.json + history/yyyy/mm/dd/HH-MM-SSZ.json 두 위치에 upload.

    latest.json — daemon 이 polling 하는 경로 (overwrite).
    history/... — 디버깅 / replay 용 (immutable).
    """
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    now = datetime.now(timezone.utc)
    ts_path = now.strftime("history/%Y/%m/%d/%H-%M-%SZ.json")

    # 1) latest.json — daemon polling target.
    latest_blob = bucket.blob(f"{prefix}/latest.json")
    latest_blob.cache_control = "no-cache, max-age=0"
    latest_blob.upload_from_string(body, content_type="application/json")
    logger.info("uploaded gs://%s/%s/latest.json (%d bytes)",
                bucket_name, prefix, len(body))

    # 2) history snapshot — 절대 overwrite 안 되도록 if_generation_match=0.
    history_blob = bucket.blob(f"{prefix}/{ts_path}")
    history_blob.upload_from_string(
        body, content_type="application/json", if_generation_match=0,
    )
    logger.info("uploaded gs://%s/%s/%s (%d bytes)",
                bucket_name, prefix, ts_path, len(body))


def main() -> int:
    bucket_name = os.environ.get("GCS_BUCKET")
    if not bucket_name:
        logger.error("GCS_BUCKET env var required")
        return 2

    prefix = os.environ.get("GCS_OBJECT_PREFIX", "realDonaldTrump").strip("/")
    limit = int(os.environ.get("TRUTH_SOCIAL_LIMIT", "40"))
    impersonate = os.environ.get("TRUTH_SOCIAL_IMPERSONATE", "chrome116")

    logger.info(
        "config: bucket=%s, prefix=%s, limit=%d, impersonate=%s",
        bucket_name, prefix, limit, impersonate,
    )

    # ── Fetch (with one short retry — Cloudflare occasional 5xx) ──
    posts: list[dict[str, Any]] = []
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            posts = fetch_truth_social(limit=limit, impersonate=impersonate)
            break
        except Exception as e:  # noqa: BLE001 — try a fallback profile
            last_err = e
            logger.warning("fetch attempt %d failed: %s", attempt + 1, e)
            if attempt == 0:
                # 첫 retry — 같은 profile 로 3초 wait.
                time.sleep(3)
            else:
                # 두 번째 retry — safari17_0 fallback.
                impersonate = "safari17_0"
                logger.info("retry with fallback impersonate=%s", impersonate)
                time.sleep(5)
    else:
        logger.error("all fetch attempts failed: %s", last_err)
        return 2

    if not posts:
        logger.warning("got 0 posts — still uploading empty payload "
                       "(daemon will see no new statuses)")

    payload = build_payload(posts)
    try:
        upload_to_gcs(bucket_name=bucket_name, prefix=prefix, payload=payload)
    except Exception as e:  # noqa: BLE001 — upload failure
        logger.exception("GCS upload failed: %s", e)
        return 3

    logger.info("done — fetched=%d, uploaded to gs://%s/%s/latest.json",
                len(posts), bucket_name, prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
