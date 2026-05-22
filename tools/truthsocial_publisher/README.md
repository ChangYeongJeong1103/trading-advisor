# Truth Social → GCS Publisher

A tiny GitHub-Actions-driven service that fetches Donald Trump's latest
Truth Social posts every 5 minutes and uploads them to a Google Cloud
Storage bucket. The anomaly-detection daemon (running on Cloud Run)
then reads `latest.json` from that bucket as its data source.

## Why this exists

Truth Social sits behind Cloudflare, and Cloudflare blocks requests
coming from GCP datacenter IP ranges with HTTP 403 ("Just a moment..."
JS challenge page) — even when the TLS fingerprint and headers perfectly
mimic a real browser. We verified this on 2026-05-22 with `curl_cffi`
impersonating `chrome116`, `chrome120`, and `safari17_0`: each profile
succeeded from a residential IP and from a local Docker container, but
all failed from Cloud Run (`us-west2`).

GitHub Actions runs on Microsoft Azure IPs, which Cloudflare currently
treats more leniently for Truth Social. By moving the fetch step to
GitHub Actions and persisting the result to GCS, the daemon stays put
on Cloud Run while still receiving fresh data.

```
GitHub Actions (Azure IP, /5 * * * *)
        │
        ▼
   curl_cffi (chrome116 TLS fingerprint)
        │
        ▼
   Truth Social /api/v1/accounts/107780257626128497/statuses
        │
        ▼
   gs://anomaly-truthsocial/realDonaldTrump/latest.json
        │
        ▼
   Anomaly daemon (Cloud Run) — TruthSocialChannel
```

## Repo layout

```
.github/workflows/truthsocial_fetch.yml   # 5-minute cron trigger
tools/truthsocial_publisher/
├── main.py            # fetch + upload (entrypoint)
├── requirements.txt   # curl_cffi, google-cloud-storage
└── README.md          # this file
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `GCS_BUCKET` | _(required)_ | Target bucket, e.g. `anomaly-truthsocial`. |
| `GCS_OBJECT_PREFIX` | `realDonaldTrump` | Folder prefix inside the bucket. |
| `TRUTH_SOCIAL_LIMIT` | `40` | Number of statuses to fetch per run. |
| `TRUTH_SOCIAL_IMPERSONATE` | `chrome116` | `curl_cffi` browser profile. |
| `GOOGLE_APPLICATION_CREDENTIALS` | _(workflow-managed)_ | Path to the SA key JSON the workflow writes to disk from a secret. |

## Output schema

```json
{
  "fetched_at": "2026-05-22T12:55:00+00:00",
  "source": "github-actions:owner/repo@run_id=...",
  "account_id": "107780257626128497",
  "posts": [ /* most-recent first, raw Mastodon status objects */ ]
}
```

Written to **two** locations per run:

- `gs://<bucket>/<prefix>/latest.json` — overwritten each run; what the daemon polls.
- `gs://<bucket>/<prefix>/history/YYYY/MM/DD/HH-MM-SSZ.json` — immutable snapshot for debugging / replay.

## Running locally (smoke test)

```bash
cd tools/truthsocial_publisher
pip install -r requirements.txt
export GCS_BUCKET=anomaly-truthsocial
export GOOGLE_APPLICATION_CREDENTIALS=$HOME/.gcp-keys/truthsocial-publisher-key.json
python main.py
```

If Cloudflare blocks your IP, try `TRUTH_SOCIAL_IMPERSONATE=safari17_0`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — `latest.json` updated. |
| `2` | Fetch failed (Cloudflare 403, network error, etc.). The workflow will alert via GitHub's normal failure notifications. |
| `3` | GCS upload failed (auth, quota, etc.). |
