# Cloud Run Deployment Guide — anomaly daemon

Step-by-step guide for deploying the 24/7 anomaly-detection daemon to Google Cloud Run.

> **Assumes**: `gcloud` CLI is already installed and you have run `gcloud auth login`.
> If the existing `trading-advisor` runs in the same GCP project, prefer the same project / region.

---

## 0. Set environment variables (one-time)

```bash
export PROJECT_ID="your-gcp-project-id"        # your project
export REGION="us-west1"                        # close to the Bay Area
export SERVICE_NAME="anomaly-daemon"
export REPO="anomaly-images"                   # Artifact Registry repo name
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE_NAME}"

gcloud config set project "${PROJECT_ID}"
```

---

## 1. Create the Artifact Registry repo (one-time)

```bash
gcloud artifacts repositories create "${REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="Anomaly daemon images"

# Authenticate Docker so it can push to Artifact Registry
gcloud auth configure-docker "${REGION}-docker.pkg.dev"
```

---

## 2. Register secrets in Secret Manager (one-time)

> Same pattern as `trading-advisor`. If a secret already exists, reuse it.

```bash
# Enable the required APIs (one-time)
gcloud services enable secretmanager.googleapis.com run.googleapis.com

# Email (Gmail SMTP App Password)
echo -n "your-gmail-app-password" | \
  gcloud secrets create SMTP_PASSWORD --data-file=-

# Telegram bot
echo -n "1234567890:ABC..." | \
  gcloud secrets create TELEGRAM_BOT_TOKEN --data-file=-

# (P4+) Databento — CME data
echo -n "db-..." | \
  gcloud secrets create DATABENTO_API_KEY --data-file=-

# (Optional) X official API — when EVT-1 is enabled
echo -n "..." | \
  gcloud secrets create X_API_BEARER --data-file=-
```

> To update / rotate a secret:
> ```bash
> echo -n "new-password" | gcloud secrets versions add SMTP_PASSWORD --data-file=-
> ```

### Grant the Cloud Run service account access to secrets

```bash
# Default compute service account (or, recommended, a dedicated SA you create)
PROJECT_NUM=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
SA_EMAIL="${PROJECT_NUM}-compute@developer.gserviceaccount.com"

for SECRET in SMTP_PASSWORD TELEGRAM_BOT_TOKEN DATABENTO_API_KEY; do
  gcloud secrets add-iam-policy-binding "${SECRET}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor"
done
```

---

## 3. Build and test the Docker image locally (required before deploy)

```bash
cd /path/to/trading-advisor

# Build (takes 5–10 minutes — pyarrow and similar wheels compile)
docker build -f Dockerfile.anomaly_detection -t anomaly-daemon:local .

# Run locally (auto dry-run mode — when no secrets are present)
docker run --rm -p 8080:8080 \
  -e ANOMALY_ENV=local \
  anomaly-daemon:local

# In another terminal, check health
curl http://localhost:8080/health
# → {"ok": true, "uptime_s": 12.3, "cycles_run": 2}

curl http://localhost:8080/snapshot
# → debug info: router state, channels, alert_modes, ...
```

> Before channels are registered (walking-skeleton state), only `cycles_run` increments.
> Validate graceful shutdown with `Ctrl-C` (the `daemon_stopped` log should appear).

---

## 4. Push the image (Artifact Registry)

```bash
# Build (production tag)
docker build -f Dockerfile.anomaly_detection -t "${IMAGE}:v0.1.0" -t "${IMAGE}:latest" .

# Push
docker push "${IMAGE}:v0.1.0"
docker push "${IMAGE}:latest"
```

---

## 5. Deploy to Cloud Run (the key step)

```bash
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}:v0.1.0" \
  --region="${REGION}" \
  --platform=managed \
  \
  `# Core 24/7 daemon options ──────────────────────────` \
  --min-instances=1 \
  --max-instances=1 \
  --no-cpu-throttling \
  --cpu=1 \
  --memory=512Mi \
  --timeout=3600 \
  \
  `# External exposure ─────────────────────────────────` \
  --port=8080 \
  --no-allow-unauthenticated \
  `# (The option above means only IAM-authenticated callers can view health/metrics — recommended.)` \
  \
  `# Environment variables ─────────────────────────────` \
  --set-env-vars="ANOMALY_ENV=cloud_run" \
  --set-env-vars="ANOMALY_DATA_PATH=/tmp/anomaly-data" \
  --set-env-vars="SMTP_HOST=smtp.gmail.com,SMTP_PORT=587" \
  --set-env-vars="SMTP_USER=me@gmail.com,SMTP_FROM=me@gmail.com,SMTP_TO=alerts@me.com" \
  --set-env-vars="TELEGRAM_CHAT_ID=123456789" \
  \
  `# Secrets (mounted from Secret Manager) ──────────────` \
  --set-secrets="SMTP_PASSWORD=SMTP_PASSWORD:latest" \
  --set-secrets="TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest"
```

### Why each option

| Option | Reason |
|---|---|
| `--min-instances=1` | Prevents scale-to-zero → keeps the 24/7 daemon running |
| `--max-instances=1` | Only one concurrent instance (avoids state fan-out — D12) |
| `--no-cpu-throttling` | Provides 100% CPU even with no requests (background loop must keep moving) |
| `--cpu=1 --memory=512Mi` | Starting spec. Once all channels are added, you may need to raise to 1Gi |
| `--timeout=3600` | Request timeout. We are a background daemon, so this is mostly irrelevant |
| `--no-allow-unauthenticated` | Hides health / metrics from the public (IAM only) |

---

## 6. (Optional) Persistent storage — GCS FUSE mount

Defaults to `/tmp/anomaly-data` (volatile). SQLite / parquet vanish on restart.
**If you need audit logs**, mount GCS via FUSE:

```bash
# 1) Create a GCS bucket (one-time)
gsutil mb -l "${REGION}" "gs://${PROJECT_ID}-anomaly-data"

# 2) Mount into the Cloud Run service (needs the gen2 environment)
gcloud run services update "${SERVICE_NAME}" \
  --region="${REGION}" \
  --execution-environment=gen2 \
  --add-volume=name=anomaly-data,type=cloud-storage,bucket="${PROJECT_ID}-anomaly-data" \
  --add-volume-mount=volume=anomaly-data,mount-path=/var/anomaly-data \
  --update-env-vars="ANOMALY_DATA_PATH=/var/anomaly-data"
```

> Caveat: GCS FUSE has slightly imperfect POSIX semantics and may not play nicely
> with SQLite WAL mode. If you see frequent SQLite I/O errors in production, split the
> layout so SQLite stays on `/tmp` and only parquet syncs to GCS.

---

## 7. Verify the deployment

```bash
# Fetch the service URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" --format="value(status.url)")

echo "Service: ${SERVICE_URL}"

# IAM-authenticated call (because of --no-allow-unauthenticated)
TOKEN=$(gcloud auth print-identity-token)

curl -H "Authorization: Bearer ${TOKEN}" "${SERVICE_URL}/health"
curl -H "Authorization: Bearer ${TOKEN}" "${SERVICE_URL}/livez"
curl -H "Authorization: Bearer ${TOKEN}" "${SERVICE_URL}/snapshot"
```

> Note: on Cloud Run `*.run.app` URLs, the exact path `/healthz` may be 404'd at the
> edge. Use `/health` or `/livez` for operational checks.

### Verify alert delivery (P7)

After deployment, confirm the daemon reads its secrets correctly:

```bash
# Look at alert_modes in the startup log — if both are false, real delivery is active
gcloud run services logs read "${SERVICE_NAME}" --region="${REGION}" --limit=200 \
  | grep -E "daemon_starting|alert_modes"

# Expected (when every secret is registered and ANOMALY_DRY_RUN is unset):
#   alert_modes={'email_dry_run': False, 'telegram_dry_run': False}
```

Rules:
- **Auto-detection**: email = real when both `SMTP_USER` and `SMTP_PASSWORD` exist; either missing → dry_run.
  Telegram needs both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to be real.
- **Force dry**: `--set-env-vars ANOMALY_DRY_RUN=true` (use right after deploy to avoid an accidental push).
- **Force real**: `--set-env-vars ANOMALY_DRY_RUN=false` (only meaningful when creds exist).

### P11(d) — Telegram behavior (locked)

- **EMERGENCY only**: `channel_dispatcher` runs with `telegram_emergency_only=True` (daemon hard-lock), so only EMERGENCY-tier alerts go to Telegram. WATCH / RISK_OFF goes to email only.
- **System-level URGENT notifications and the 1-hour heartbeat are both OFF** (`AlertRouter(telegram_enabled=False)` + no heartbeat task). The only Telegram a user receives is one channel-level EMERGENCY per event.
- **Cooldown is shared**: email and Telegram share the same `(channel, symbol, tier)` 24h cooldown. There is no partial silence (either both deliver or both are silent).
- **Verification**: the `channel_dispatcher` block in `/snapshot` exposes `telegram_enabled`, `telegram_dry_run`, `telegram_emergency_only`, `cooldown_minutes`, `buffer_max_age_h`, and `stats`.

```bash
curl -sS -H "Authorization: Bearer ${TOKEN}" "${URL}/snapshot" \
  | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['channel_dispatcher'], indent=2))"
# Expected (production):
#   {
#     "stats": {"considered":..., "emitted":..., "telegram_emitted":..., ...},
#     "buffer_size": ...,
#     "buffer_max_age_h": 24,
#     "cooldown_minutes": 1440,           ← 24h lock (anything less than 1440 will alert)
#     "smtp_dry_run": false,
#     "telegram_enabled": true,
#     "telegram_dry_run": false,
#     "telegram_emergency_only": true     ← always true (daemon hard-lock)
#   }
```

### Where dry-run audit dumps land (P11(a/d))

When `ANOMALY_DRY_RUN=true` (or auto-dry kicks in), each alert auto-dumps audit
artifacts into the following directory:

```
${ANOMALY_DATA_PATH}/alerts_live/{channel}_{symbol}_{tier}_{YYYYMMDDTHHMM}/
  ├── channel_alert.eml      ← Gmail "Show original" compatible — double-click to preview the email
  ├── channel_telegram.txt   ← caption + plot path (only for EMERGENCY)
  ├── plot_60m.png           ← 1h timeline (email + Telegram reuse the same PNG)
  └── plot_360m.png          ← 6h timeline
```

In production mode (real send), only the plot PNGs land in the same directory; the
.eml / .txt files are NOT generated (the inbox is the source of truth).

> Safest first alert: inject a single mock spike, then verify it lands in your inbox + Telegram.
> CME mock spike: `--set-env-vars CME_MOCK_SPIKE=true` (P4 walking-skeleton).

### Reading logs

```bash
gcloud run services logs read "${SERVICE_NAME}" \
  --region="${REGION}" --limit=50

# Or in the Cloud Console:
# https://console.cloud.google.com/run/detail/${REGION}/${SERVICE_NAME}/logs
```

---

## 8. Update / rollback

```bash
# Deploy a new version
docker build -f Dockerfile.anomaly_detection -t "${IMAGE}:v0.2.0" .
docker push "${IMAGE}:v0.2.0"
gcloud run services update "${SERVICE_NAME}" \
  --region="${REGION}" --image="${IMAGE}:v0.2.0"

# Rollback
gcloud run services update "${SERVICE_NAME}" \
  --region="${REGION}" --image="${IMAGE}:v0.1.0"

# Traffic split (canary)
gcloud run services update-traffic "${SERVICE_NAME}" \
  --region="${REGION}" \
  --to-tags=v0-2-0=10,v0-1-0=90
```

---

## 9. Cost estimate (based on D13)

| Item | Monthly cost (approx.) |
|---|---|
| Cloud Run (1 instance, 1 CPU, 512Mi, always-on) | $30–50 |
| Artifact Registry (1 GB image storage) | $0.10 |
| Secret Manager (5–10 secrets) | $0.30 |
| Cloud Logging (moderate usage) | $0–5 |
| **Total (PAYG)** | **~$35–55** |

> Comfortably under D13's $1,000 cap. Once Databento (CME) / OpenAI etc. are added,
> track them on the dashboard.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Container exits right after deploy | Failed to listen on `$PORT` — if you don't see `http_server_listening` in the logs, aiohttp may not be installed. Check requirements.txt |
| `secret access denied` | Service account lacks `secretmanager.secretAccessor`. Re-run the IAM commands in section 2 |
| Container stays alive after `daemon_stopping` | Cloud Run's SIGTERM grace period is 10 s. If shutdown doesn't finish in time, it is force-SIGKILLed |
| WebSocket drops (after P2–P3 lands) | Check `min-instances=1` and `--no-cpu-throttling`. If restarts are frequent, raise `--cpu=2` |
| Memory OOM | Raise `--memory=1Gi`, or reduce `raw_store` retention (`raw_retention_days`) |

---

## 11. Operations Runbook (P8)

> A grab-bag of commands you reach for during 24/7 operation. Written without env vars so you can paste them into a fresh terminal as-is.

### 11.1 One-shot status check

```bash
# 1) Mint an auth token (used as ${TOKEN} below)
TOKEN=$(gcloud auth print-identity-token)
URL=$(gcloud run services describe anomaly-daemon --region=us-west2 --format='value(status.url)')

# 2) Liveness + uptime
curl -sS -H "Authorization: Bearer ${TOKEN}" "${URL}/health"
# → {"ok": true, "uptime_s": 1234.5, "cycles_run": 247}

# 3) Full snapshot (router state / channels / alert_modes)
curl -sS -H "Authorization: Bearer ${TOKEN}" "${URL}/snapshot" | python3 -m json.tool

# 4) Currently deployed revision + env
gcloud run services describe anomaly-daemon --region=us-west2 \
  --format='value(spec.template.metadata.name, spec.template.spec.containers[0].env)'
```

### 11.2 Kill-switch (stop alerts immediately)

> Use it when: alerts are flooding or wrong-signal pushes are going out.
> The daemon keeps running and still collects / stores signals. **Only outbound delivery is stopped.**

```bash
# Off — stop every alert (a new revision applies in ~10 s)
gcloud run services update anomaly-daemon --region=us-west2 \
  --update-env-vars=ANOMALY_DRY_RUN=true

# Verify
curl -sS -H "Authorization: Bearer ${TOKEN}" "${URL}/snapshot" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['alert_modes'])"
# → {'email_dry_run': True, 'telegram_dry_run': True}

# On — re-enable alerts
gcloud run services update anomaly-daemon --region=us-west2 \
  --remove-env-vars=ANOMALY_DRY_RUN
```

### 11.3 Mock-spike toggle (P8 validation / demo)

```bash
# Turn on the CME mock spike (every 2 minutes, 80× volume)
gcloud run services update anomaly-daemon --region=us-west2 \
  --update-env-vars=CME_MOCK_SPIKE=true,CME_MOCK_SPIKE_EVERY_S=120,CME_MOCK_SPIKE_MULT=80

# Turn it off (back to production mode)
gcloud run services update anomaly-daemon --region=us-west2 \
  --remove-env-vars=CME_MOCK_SPIKE,CME_MOCK_SPIKE_EVERY_S,CME_MOCK_SPIKE_MULT
```

> **Caveat**: for a spike to reach the alert stage, 30 minutes of baseline must accumulate first (CMEFeatures default).
> A fresh deploy resets the baseline, so you must wait ~30 minutes before alerts start firing.

### 11.4 Secret rotation

```bash
# Register a new password (version auto-increments)
echo -n "new-app-password" | gcloud secrets versions add SMTP_PASSWORD --data-file=-

# Cloud Run references :latest, so the next container start picks it up automatically.
# To apply immediately, force a new revision with a no-op update:
gcloud run services update anomaly-daemon --region=us-west2 \
  --update-secrets=SMTP_PASSWORD=SMTP_PASSWORD:latest
```

### 11.5 Quick log searches

```bash
# Last 5 minutes — alert-related only
gcloud logging read 'resource.type="cloud_run_revision"
  AND resource.labels.service_name="anomaly-daemon"
  AND (textPayload=~"SPIKE|alert|EMERGENCY|RISK_OFF|email_sent|telegram_sent")' \
  --freshness=5m --limit=50 \
  --format='value(timestamp,textPayload)'

# Errors only (last 1 hour)
gcloud logging read 'resource.type="cloud_run_revision"
  AND resource.labels.service_name="anomaly-daemon"
  AND severity>=ERROR' \
  --freshness=1h --limit=30 --format='value(timestamp,textPayload)'

# Cloud Console (browser)
# https://console.cloud.google.com/run/detail/us-west2/anomaly-daemon/logs
```

### 11.6 24h monitoring checklist

> A quick once-over every 24 hours. If anything looks off, run the 11.1 commands for precise diagnosis.

| Check | Command / location | Healthy value |
|------|--------------------|----------------|
| Daemon liveness | `curl ... /health` | `ok: true` |
| Cumulative uptime | `uptime_s` in `curl ... /health` | ~86400 ± a small delta over 24h (no restarts) |
| Cycles progressing | `cycles_run` | uptime_s / 5 ± a little (5-second cycles) |
| Alert mode stable | `alert_modes` in `/snapshot` | `{email_dry_run: false, telegram_dry_run: false}` |
| All 4 channels running | `channels.running` in `/snapshot` | `["polymarket","hyperliquid","cme","x"]` (mock included) |
| Memory / CPU | Cloud Console → Metrics | mem < 70%, cpu < 50% (idle < 10%) |
| Cost accumulating | Cloud Billing dashboard | Under $50/month (D13 cap = $1000) |

### 11.7 Emergency stop (full halt)

```bash
# Drop the instance count to 0 → immediate stop (scale-to-zero means next request will cold-start)
gcloud run services update anomaly-daemon --region=us-west2 \
  --min-instances=0 --max-instances=0

# Restore
gcloud run services update anomaly-daemon --region=us-west2 \
  --min-instances=1 --max-instances=1
```
