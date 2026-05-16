# Operations Monitoring — Anomaly Daemon

> **Purpose**: Operational monitoring for the production daemon (the
> B + C setup).
>
> - **B (active review)** — `scripts/weekly_review.sh` — you run it
>   periodically by hand for a holistic review.
> - **C (passive alerts)** — Cloud Monitoring + Databento billing —
>   only auto-pushes to you when something goes wrong.
>
> For the architecture / detection math narrative, see
> [`anomaly-architecture.md`](anomaly-architecture.md) and
> [`anomaly-detection-math.md`](anomaly-detection-math.md).

---

## 0. One-line operations cycle

```
Mon–Thu : C stays quiet = healthy.  Just normal life.  (If something breaks, C emails you immediately.)
Friday  : Run B once, paste the output into chat → review together.
```

---

## 1. B — Weekly Review Script

### 1.1 Usage

```bash
# Default — review the last 3 days
scripts/weekly_review.sh

# Last 7 days
scripts/weekly_review.sh 7

# Capture the output to a file
scripts/weekly_review.sh > review_$(date +%F).md
```

The output is a markdown report (6 auto-generated sections + qualitative
review template + Databento cost table).

### 1.2 What it measures automatically (6 sections)

| § | Metric | Data source | Healthy range |
|---|---|---|---|
| 1 | Cloud Run revisions | `gcloud run revisions list` | Only revisions you deployed (auto-added = OOM / restart) |
| 2 | ERROR log count | Cloud Logging | < 5 over 3 days (repeated identical errors → needs a fix) |
| 3 | Fusion-state counts (per tier) | Cloud Logging | WATCH 5–30/day, RISK_OFF 0–5/day, EMERGENCY 0–1/day |
| 4 | GCS audit-bucket alert directories | `gsutil ls` | Roughly matches RISK_OFF + EMERGENCY totals |
| 5 | Per-channel activity | Cloud Logging | All four channels non-zero (X is currently mocked) |
| 6 | `/health` endpoint live ping | Cloud Run service URL | Responds with `{"ok": true}` |

### 1.3 What you fill in by hand (qualitative review)

The script ends by printing an empty template. Fill it in yourself when
you paste the output into chat:

1. **Precision (FP rate)** — among the RISK_OFF / EMERGENCY alerts you received, real vs. noise.
2. **Recall (FN candidates)** — cases where "something big happened in the market but no alert fired".
3. **Databento cost** — open the [console](https://databento.com/portal/billing) in your browser and fill in the table.

### 1.4 Environment overrides (works on other projects / regions too)

```bash
PROJECT_ID="other-project" \
REGION="us-east1" \
SERVICE_NAME="other-daemon" \
GCS_AUDIT_BUCKET="other-audit-bucket" \
  scripts/weekly_review.sh 7
```

Defaults:
- `PROJECT_ID = trading-advisor-478909`
- `REGION = us-west2`
- `SERVICE_NAME = anomaly-daemon`
- `GCS_AUDIT_BUCKET = anomaly-alerts-audit-trading-advisor-478909`

---

## 2. C — Passive alerts (Cloud Monitoring + Databento)

Three alerts are active. Each catches a different class of failure.

### 2.1 Alert 1 — Daemon DOWN (Cloud Monitoring)

| Field | Value |
|---|---|
| **Trigger** | Zero log entries from anomaly-daemon for 10 minutes |
| **Why this signal** | The daemon polls Hyperliquid / Polymarket every second, so it produces many logs continuously. A sustained gap of zero = daemon dead |
| **Notification** | email → `cyjeong@umich.edu` |
| **Auto-close** | 30 minutes (clears automatically when healthy again) |
| **Implementation** | log-based metric `anomaly_daemon_log_volume` + alert policy `anomaly-daemon — DOWN (no logs 10min)` |

**What to do when it fires**:
1. `gcloud run services describe anomaly-daemon --region=us-west2` → confirm READY.
2. `gcloud logging read 'severity>=ERROR' --freshness=30m` → check the most recent error.
3. Redeploy if needed (`scripts/deploy_anomaly.sh v0.x.x`).

### 2.2 Alert 2 — ERROR-log spike (Cloud Monitoring)

| Field | Value |
|---|---|
| **Trigger** | anomaly-daemon emits ≥ 10 `severity >= ERROR` logs within 5 minutes |
| **Why this signal** | In normal operation ERROR is rare. 10 in 5 min clearly indicates a recurring bug or external API outage |
| **Notification** | email → `cyjeong@umich.edu` |
| **Auto-close** | 30 minutes |
| **Implementation** | log-based metric `anomaly_daemon_error_count` + alert policy `anomaly-daemon — ERROR spike (≥10 in 5min)` |

**What to do when it fires**:
1. `gcloud logging read 'severity>=ERROR' --freshness=10m --format='value(textPayload)' | head -20` → look for a recurring stack trace.
2. Same pattern → fix code → redeploy. All different → external API outage is likely (Hyperliquid / Polymarket).

### 2.3 Alert 3 — Databento cost cap (Databento itself)

Configured inside **Databento console**, not GCP Cloud Monitoring (GCP
cannot see Databento spend).

**Setup (one-time)**:

1. Sign in at [Databento Portal](https://databento.com/portal).
2. Left menu → **Billing → Spending limits**.
3. Configure a **Hard cap** (a soft alert alone will keep billing past the limit!):
   - **Monthly hard cap**: `$50` (PAYG ceiling on top of the $179 subscription. Typical usage is ~$5/month, so plenty of headroom.)
   - **Daily soft alert**: `$20` (you get an email when this is breached — early detection for runaway costs).
4. Save.

**Our daemon also enforces its own cost cap** (`DatabentoClient.monthly_cap_usd`).
- env: `ANOMALY_CME_DATABENTO_CAP_USD` (default is defined in `core/config.py`)
- Keep both caps in sync for safety (Databento console = $50, code = $50).

**What to do when it fires**:
- If the daily $20 alert pops, recall whether you ran a historical replay this cycle.
- If you didn't, the daemon's enricher is over-calling a fallback API. Inspect the code.

---

## 3. One-page summary of active items

| Kind | Name | Trigger | Notification |
|---|---|---|---|
| Notification channel | `anomaly-alerts-email` | (the channel itself) | email → `cyjeong@umich.edu` |
| Log-based metric | `anomaly_daemon_log_volume` | (the metric itself) | — |
| Log-based metric | `anomaly_daemon_error_count` | (the metric itself) | — |
| Alert policy | `anomaly-daemon — DOWN (no logs 10min)` | 0 logs / 10 min | email |
| Alert policy | `anomaly-daemon — ERROR spike (≥10 in 5min)` | ≥ 10 ERROR / 5 min | email |
| Databento | Spending limit (hard cap) | $50 monthly | Databento email |
| Databento | Spending alert (soft) | $20 daily | Databento email |
| Script | `scripts/weekly_review.sh` | manual, run by you | stdout markdown |

---

## 4. Change / operations cheat-sheet

### 4.1 Pause / resume an alert policy

```bash
# Pause (e.g. intentional maintenance window)
gcloud alpha monitoring policies update POLICY_ID --no-enabled

# Resume
gcloud alpha monitoring policies update POLICY_ID --enabled

# List policy IDs
gcloud alpha monitoring policies list --format="value(name,displayName)"
```

### 4.2 Add a notification channel (e.g. a second email)

```bash
gcloud beta monitoring channels create \
  --display-name="anomaly-alerts-secondary" \
  --type=email \
  --channel-labels=email_address=NEW_EMAIL@example.com
```

To attach the new channel to an existing alert policy, use the console
or `policies update`.

### 4.3 Change an alert threshold (e.g. ERROR spike 10 → 20)

```bash
# Update from a JSON file
gcloud alpha monitoring policies update POLICY_ID \
  --policy-from-file=/path/to/updated.json
```

Or edit directly in the GCP Console UI (easier).

### 4.4 Test fire — verify the alerts actually work

**Liveness alert**: temporarily scale the Cloud Run service to 0 instances.
```bash
gcloud run services update anomaly-daemon --region=us-west2 --min-instances=0 --max-instances=0
# Wait 10 minutes → you should receive an email → restore to 1/1
gcloud run services update anomaly-daemon --region=us-west2 --min-instances=1 --max-instances=1
```

**ERROR spike**: it is not feasible to manually inject ERROR logs into
the production daemon, so you cannot test-fire this. It is validated
naturally the first time a real bug surfaces.

---

## 5. Change history

| Date | Change | Trigger |
|---|---|---|
| 2026-04-22 | Initial setup — 2 alert policies + 1 notification channel + 2 log metrics | Start of B + C operations |
