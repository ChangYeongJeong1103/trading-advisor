# Anomaly Detection Upgrade Plan (v1)

> Status: **ALIGNED — D1~D13 LOCKED, ready for P1**
> Companion doc: [`anomaly-architecture.md`](anomaly-architecture.md)

---

## 1. Purpose

This document is the single source of truth for the **first-pass upgrade** of the
`trading-advisor` project: adding a **real-time anomaly trading detection** layer
on top of the existing Conditional RAG advisor.

Content:
- Why this upgrade is needed (problem / goals / non-goals)
- What is in v1 scope and what slips to v2
- How to bolt the new code on without breaking the existing repository
- The actual **GitHub file / folder layout** to commit
- Risks, dependencies, and open questions to settle before coding starts

This document and `anomaly-architecture.md` **must always be edited together**.
If architecture changes, the plan changes with it so scope and structure do not drift.

---

## 2. Background and motivation

`trading-advisor` today is a **Conditional RAG + Smart Fallback** financial
assistant that uses uploaded financial documents and an LLM to answer user
questions.

It is great at **retrospective Q&A** (questions about something that already
happened) but **does not watch the live market**. Before big policy / military /
geopolitical events break, **abnormal trading patterns in public data** are
frequently visible in prediction markets, on-chain perp DEXes, futures order
flow, and political social-media posts. Users who do not see these patterns
typically:

- React only after the news headline drops
- Get caught in the post-announcement volatility
- Take losses **that could have been reduced if abnormal flow had been spotted in time**

The goal of this upgrade is **not** to find out "who did the insider trade" or
to copy their bets. The goal is to **detect abnormal trading flow early enough
to defensively adjust positions** — reduce leverage, pause new entries, hedge,
or de-risk fully.

---

## 3. Goals and non-goals

### 3.1 v1 goals

1. **Continuously ingest** live / near-real-time market activity from 5 independent channels:
   - Polymarket (prediction market)
   - Hyperliquid (on-chain perp DEX)
   - CME (oil / index futures + options on futures)
   - X (curated whale / anomaly accounts)
   - Truth Social (@realDonaldTrump — political insider catalyst)
2. Within each channel, use channel-specific logic to detect **abnormal volume / order flow / sentiment shift**.
3. Combine per-channel signals into a single anomaly score via **noisy-OR fusion**, while **always preserving the per-channel scores in full**.
   (To avoid the failure mode of a plain weighted sum killing a single strong signal — see architecture §5.2.)
4. Produce defensive action recommendations via a **two-stage state structure** (`NORMAL → WATCH → RISK_OFF → EMERGENCY`):
   - **(Stage 1) Channel-level tier** — each channel emits its own tier from its detector pool.
   - **(Stage 2) System-level state** — `tier_floor = max(per_channel_tiers)` (max-tier-wins) + corroboration boost (+1 tier when ≥ 2 channels agree at the same tier in the same direction).

   This structure lets single-channel high-conviction signals (e.g. CME-only 6,200 contracts in 1 min) immediately escalate to EMERGENCY — corroboration is a boost, not a hard requirement. Details in architecture §5.4.
5. **User interface = push notification** — users are going about their day and receive an **email + Telegram** when something fires.
   Each email / Telegram body packs system_state + per_channel_tiers (each of the 5 channels' tier) + tier_floor + boost applied? + per-channel score breakdown + reason_codes + fired_detectors + recommended action + external visual links (Polymarket / Hypurrscan / TradingView / X / Truth Social direct links) + fused_score (reference)
   — so the user can act on the message alone, with no dashboard required.
   The existing RAG Streamlit app is **untouched** (separate system).
   The anomaly Streamlit dashboard is **out of v1 scope, deferred to v2** (see §10).
6. Keep each channel **fully modular** so a single channel can be upgraded / replaced / disabled in isolation.

### 3.2 v1 non-goals (deliberately not done)

- **No automated trading / order-routing system.**
- **Do not** attribute trades to specific real-name individuals.
- Financial news, Fed announcements, earnings, Reddit, political RSS feeds — all of these belong to a **v2 upgrade** (see §10).
- **Do not retrain** the existing RAG embeddings.

### 3.3 Success criteria

- Each channel passes its **API / feed sanity check** — i.e., its source produces data the way it is supposed to:
  - **Right data** — only the intended symbols / markets / event types (no unrelated data leaks, no missing fields)
  - **Right timing** — live channels meet the §7 latency targets; batch channels (Dune, etc.) deliver results on schedule
  - **Right format** — every raw payload passes the canonical `NormalizedEvent` schema in one go (zero malformed strings, NaNs, wrong types, or out-of-range numbers)
  - All three are automated in P1 as **per-channel contract tests**, run on every CI run or daemon startup.
- All 5 channels can start / stop independently.
- Each channel emits a `ChannelSignal` **matching the shared schema** ("all 5 channels hand their analysis result to the fusion engine in the same envelope, ChannelSignal").
- The fusion engine produces a `FusedAnomalyEvent` with **reproducible scoring**.
- **Push delivery verified** — on `EMERGENCY` entry, the user's **first receipt** is < 10s P95 (Telegram first — typically < 3s, with email following 5–30s later); on `RISK_OFF` entry, Gmail arrives < 60s P95. `WATCH` is batched into one daily 06:00 PT digest. (Full NFR in architecture §7.)
- **Email / Telegram body is action-ready by itself** — system_state + per_channel_tiers (each of the 5 channels' tier) + tier_floor + boost_applied + per-channel score breakdown + fired_detectors + reason_codes + recommended action + external visual links + fused_score (reference) are all packed in, so the user can decide without opening any additional dashboard. (Same items as architecture §6.5.2 email anatomy.)
- **Throttle verified** — re-entering the same state for the same symbol within 5 minutes does not deliver duplicate alerts.
- The existing RAG endpoint passes its prior behavior unchanged (fully isolated from the anomaly system).

---

## 4. Scope of v1

### 4.1 In scope

| Area                  | v1 inclusion                                                            |
| :-------------------- | :---------------------------------------------------------------------- |
| Channel 1 — Polymarket   | Real-time WebSocket + Dune historical / backtest                     |
| Channel 2 — Hyperliquid  | Real-time WebSocket + Hypurrscan visual check (manual, out-of-band)  |
| Channel 3 — CME          | Databento (primary), TradingView alerts, Unusual Whales (options context) |
| Channel 4 — X            | scrape / API ingestion of 5 named accounts                          |
| Channel 5 — Truth Social | httpx polling of `@realDonaldTrump` + Wayback Machine backfill + GPT-5 LLM market-impact scoring |
| Core                  | Orchestrator, registry, schemas, fusion engine, decision policy, state manager |
| Storage               | Local file / SQLite-based raw / features / signals / decisions stores   |
| **Notification**      | **Email (Gmail SMTP) + Telegram bot. Per-tier delivery policy, throttle, external visual link inside the body. (= v1 PRIMARY UX)** |
| Monitoring            | Per-channel health, latency, error metrics                              |

### 4.2 Out of scope (v2 candidates)

- Financial news / Fed / earnings / 10-K / Reddit / political RSS channels
- Automated order placement / brokerage integration
- Persistent cloud database (v1 starts with local SQLite + flat files)
- Multi-user authentication
- SMS push notification (Telegram bot is the substitute)
- **Anomaly Streamlit dashboard / Anomaly Radar panel** — integrated into the v2 comprehensive trading advisor (see §10)
- Injecting the anomaly state into the RAG advisor (v2)

---

## 5. Conflict check with the existing repository

Before defining the new layout, explicitly verify compatibility with the current code under `src/`:

| Existing file              | Behavior                              | Conflicts with v1? | Note                                                                  |
| :------------------------- | :------------------------------------ | :----------------- | :-------------------------------------------------------------------- |
| `src/market_summary/advisor.py`         | Conditional RAG decision engine       | **No**             | The anomaly module is added under `src/anomaly_detection/`; existing files untouched. |
| `src/market_summary/app_streamlit.py`   | Streamlit UI                          | **No (untouched)** | In v1 the anomaly system never touches Streamlit (push-first). RAG UX is 100% preserved. |
| `src/market_summary/config.py`          | Centralized hyperparameters           | **No**             | New anomaly settings live in `src/anomaly_detection/core/config.py`.   |
| `src/market_summary/document_pipeline.py` | RAG ingestion                       | **No**             | Independent.                                                          |
| `src/market_summary/logging_utils.py`   | Logger + cost / experiment tracker    | **Reused**         | The anomaly module imports the same logger to unify log style.        |
| `src/__init__.py`          | Package marker                        | **No**             | Untouched.                                                            |
| `data/`                    | PDF / DOCX RAG sources                | **No**             | Anomaly artifacts go to `data/anomaly_detection/` (separate subtree).  |
| `chroma_db/`               | RAG vector DB                         | **No**             | Untouched.                                                            |
| `Dockerfile.market_summary` | Streamlit container                   | **Minor change**   | Append dependencies to `requirements_v0-market_summary.txt`. Dockerfile structure stays. |
| `.gitignore`               | Ignores `chroma_db/`, logs            | **Minor change**   | Add `data/anomaly_detection/raw/`, `features/`, `signals/` (raw payloads are large). |

### 5.1 Import-path compatibility

The existing app runs from `src/market_summary/` and uses a **non-relative import** pattern
(`from config import ...`).

To not break that, the anomaly module:
- Lives in its own package at `src/anomaly_detection/`.
- Uses **absolute `anomaly_detection.*` paths only** (e.g. `from anomaly_detection.core.schemas import ChannelSignal`).
- Does **not shadow** top-level names the existing app already uses (`config`, `advisor`, `document_pipeline`, `logging_utils`).

This way, the existing `streamlit run src/market_summary/app_streamlit.py` keeps working without `PYTHONPATH` changes.

---

## 6. Proposed file / folder layout

The structure to be created in this v1 upgrade. Items marked **NEW** are added in this upgrade; everything else is preserved from the existing repo.

```text
trading-advisor/
├── src/
│   ├── __init__.py
│   ├── market_summary/                     # existing — RAG v0
│   │   ├── __init__.py
│   │   ├── advisor.py                      # existing — Conditional RAG
│   │   ├── app_streamlit.py                # existing — UI
│   │   ├── config.py                       # existing — RAG config
│   │   ├── document_pipeline.py            # existing — RAG ingestion
│   │   ├── logging_utils.py                # existing — logger / cost tracker
│   │   └── chroma_db/                      # existing — RAG vector store
│   │
│   └── anomaly_detection/                  # NEW — anomaly detection package
│       ├── __init__.py
│       │
│       ├── core/                           # NEW — channel-agnostic core
│       │   ├── __init__.py
│       │   ├── config.py                   # anomaly-only settings
│       │   ├── schemas.py                  # canonical data contracts
│       │   ├── registry.py                 # channel register / unregister
│       │   ├── orchestrator.py             # async runtime / scheduler
│       │   ├── fusion_engine.py            # cross-channel scoring
│       │   ├── decision_policy.py          # NORMAL/WATCH/RISK_OFF/EMERGENCY mapping
│       │   └── state_manager.py            # state transitions + dwell time
│       │
│       ├── channels/                       # NEW — one folder per source
│       │   ├── __init__.py
│       │   ├── base.py                     # abstract Channel interface
│       │   │
│       │   ├── polymarket/
│       │   │   ├── __init__.py
│       │   │   ├── collector.py            # WebSocket + REST ingest
│       │   │   ├── dune_backfill.py        # history / wallet pattern
│       │   │   ├── normalizer.py
│       │   │   ├── features.py             # vol z-score, prob jump, OB imbalance
│       │   │   └── detector.py             # emit ChannelSignal
│       │   │
│       │   ├── hyperliquid/
│       │   │   ├── __init__.py
│       │   │   ├── collector.py            # official WS / API
│       │   │   ├── normalizer.py
│       │   │   ├── features.py             # OI delta, whale fills, L/S skew
│       │   │   └── detector.py
│       │   │
│       │   ├── cme/
│       │   │   ├── __init__.py
│       │   │   ├── databento_client.py     # primary live + historical
│       │   │   ├── tradingview_adapter.py  # webhook ingest
│       │   │   ├── unusualwhales_adapter.py# options-flow context
│       │   │   ├── normalizer.py
│       │   │   ├── features.py
│       │   │   └── detector.py
│       │   │
│       │   ├── x/
│       │   │   ├── __init__.py
│       │   │   ├── collector.py            # API or scrape (5 accounts)
│       │   │   ├── parser.py               # post → structured event
│       │   │   ├── credibility.py          # per-account weighting
│       │   │   └── detector.py
│       │   │
│       │   └── truth_social/               # NEW — Channel 5
│       │       ├── __init__.py
│       │       ├── collector.py            # httpx polling of @realDonaldTrump
│       │       ├── backfill.py             # Wayback Mastodon API JSON
│       │       ├── reference_db.py         # historical-event reference (15+ posts)
│       │       ├── parser.py               # post + media → structured event
│       │       ├── stage1_filter.py        # keyword / regex gating
│       │       ├── llm_assessor.py         # GPT-5 market-impact + topic_slug
│       │       └── channel.py              # XChannel-like pipeline
│       │
│       ├── storage/                        # NEW — persistence layer
│       │   ├── __init__.py
│       │   ├── raw_store.py                # append-only raw payload
│       │   ├── feature_store.py            # rolling window / time-series
│       │   ├── signal_store.py             # ChannelSignal + FusedAnomalyEvent
│       │   └── decision_store.py           # final decision log (audit trail)
│       │
│       ├── monitoring/                     # NEW — operational visibility
│       │   ├── __init__.py
│       │   ├── health.py                   # per-channel liveness
│       │   ├── metrics.py                  # latency, drop, error counter
│       │   └── cost_tracker.py             # wrapper for every external API/service call (PAYG record + subscription monthly + free daily) + PAYG threshold alert + kill-switch (D13)
│       │
│       └── alerts/                         # NEW — PRIMARY UX (push delivery)
│           ├── __init__.py
│           ├── router.py                   # tier(state) → channel mapping
│           ├── throttle.py                 # cooldown / dedup / WATCH digest queue
│           ├── link_builder.py             # external visual URLs (Polymarket / Hypurrscan / TV / X / Truth Social)
│           └── renderer/
│               ├── __init__.py
│               ├── email.py                # Gmail SMTP send + HTML/plain body
│               └── telegram.py             # Telegram bot send + Markdown body
│
├── data/
│   ├── market_summary/                     # existing RAG PDFs / DOCX
│   └── anomaly_detection/                  # NEW — runtime artifacts (gitignored)
│       ├── raw/
│       ├── features/
│       ├── signals/
│       ├── decisions/
│       ├── historical_events/              # 6 historical insider events (reference)
│       ├── truthsocial/                    # 15+ historical Trump posts (reference)
│       └── cost/                           # D13 — pay-as-you-go cost tracking
│           ├── cost_ledger.csv             # one row per API call (append-only)
│           └── cost_summary_<YYYY-MM>.csv  # daily aggregate per tool
│
├── docs/
│   ├── v0-market_summary/                  # existing
│   │   ├── architecture.md
│   │   ├── conditional-rag.md
│   │   ├── embedding-strategy.md
│   │   ├── hyperparameter-tuning.md
│   │   ├── error_handling.md
│   │   └── api-reference.md
│   └── v1-anomaly_detection/               # NEW
│       ├── anomaly-upgrade-plan.md         # this document
│       ├── anomaly-architecture.md         # system design
│       ├── anomaly-detection-math.md       # detector math reference
│       ├── detection-design.md             # P9 algorithm deep-dive
│       ├── replay-framework.md             # P10 replay framework
│       ├── polymarket-replay.md            # operational guide
│       ├── cloud-run-deploy.md             # Cloud Run deployment
│       └── operations-monitoring.md        # B+C monitoring setup
│
├── notebooks/
│   └── (existing experiment notebooks)     # anomaly prototyping notebooks added as needed
│
├── requirements_v0-market_summary.txt      # v0 — Streamlit + LangChain
├── requirements_v1-anomaly_detection.txt   # v1 — daemon (extended per §7)
├── Dockerfile.market_summary               # v0 — unchanged
├── Dockerfile.anomaly_detection            # NEW — v1 daemon
├── cloudbuild.anomaly_detection.yaml       # NEW — Cloud Build config
├── .dockerignore
├── .gitignore                              # adds data/anomaly_detection/raw, etc.
├── LICENSE
└── README.md                               # rewritten after v1:
                                            #   - covers existing RAG + new anomaly features
                                            #   - architecture overview, usage, structure, setup
                                            #   - a real entry point, not just a docs pointer
```

**Core principle:** each `channels/<name>/` folder is **self-contained**.
The core never imports anything inside a channel; it talks to channels only via the abstract interface in `channels/base.py`.
That is why per-channel upgrades are possible.

**Not built in v1 (for reference):**
- `src/anomaly_detection/ui/` → added in the v2 comprehensive trading advisor (§10)
- `src/anomaly_detection/services/` → added in v2 when a bridge to the RAG advisor is needed

---

## 7. Dependencies (packages to add)

Items to append to `requirements_v1-anomaly_detection.txt`. Exact version pinning is decided at implementation time.

| Package                         | Purpose                                     | Note                                  |
| :------------------------------ | :------------------------------------------ | :------------------------------------ |
| `websockets`                    | Polymarket / Hyperliquid live streams        | Standard async WS client              |
| `httpx`                         | REST calls (Dune, Polymarket REST, UW, Truth Social) | Async-friendly                |
| `databento`                     | Primary CME live + historical               | Official SDK                          |
| `pydantic`                      | Canonical schemas                           | Strict validation at channel boundaries |
| `pandas`                        | Rolling features, baselines                 | Already implied via numpy             |
| `scipy`                         | Statistical baselines (z-score, robust quantile) | Optional                          |
| `apscheduler` *or* `anyio`      | Channel scheduling                          | Pick one in architecture v1           |
| `tweepy` *or* `snscrape` *or* official X API | X account ingestion              | Decided by access type                |
| `sqlalchemy` + `sqlite`         | signal / decision local persistence         | A light default                       |
| `tenacity`                      | Retry policy for unstable sources           | Per-collector + alert-send retry      |
| `python-telegram-bot`           | Telegram bot send (EMERGENCY tier)          | Async-friendly, free, instant push    |
| `jinja2`                        | Email / Telegram body templating            | HTML email + Markdown telegram        |
| `openai`                        | GPT-5 LLM (X Stage-2 + Truth Social market-impact + alert similarity assessor) | PAYG, ~$0.05/month at expected volume |
| `matplotlib` + `pillow`         | 1h / 6h timeline plot generation             | Inline in email + sendMediaGroup for Telegram |
| `beautifulsoup4`                | Truth Social HTML parsing + Wayback scraping | For historical backfill               |
| (stdlib `smtplib`, `email`)     | Gmail SMTP send                             | No extra package needed               |

All of these are a **soft addition** — none of them are imported on the existing RAG path.

---

## 8. Implementation phases

Two kinds of phase:

- **Pn (sequential)** — run in order. P0 → P10. The next phase only begins once the previous one ends.
- **EVT-n (event-triggered)** — fires when a specific condition is met. Not time-ordered, but "when needed".

> Core strategy: **end-to-end plumbing (P1~P8) first → detection-algorithm deep-dive (P9~P10) later**.
> Reason: once external integration (5 channels + Cloud Run + email / Telegram) is stable, debugging algorithm work in isolation is easier.
>      + Detection tuning is only meaningful once P1~P8 has been running and accumulating historical data.

### 8.1 Sequential phases (P0 ~ P10)

| Phase | Deliverable                                                              | Exit criterion                                                |
| :---- | :----------------------------------------------------------------------- | :------------------------------------------------------------ |
| **P0** | **Plan + architecture alignment** (= this very step). Finalize both docs and lock every design decision | Both docs signed off; every OPEN decision LOCKED              |
| **P1** | **Build the full skeleton**: `src/anomaly_detection/core/{schemas,registry,orchestrator}` + `channels/base.py` + `storage/*` interfaces + `alerts/` shell + **`monitoring/cost_tracker.py` scaffold** (D13: all three helpers — `record()` (PAYG) + `record_subscription_monthly()` + `record_free_daily()` + CSV writer (with the `type` column) + the PAYG threshold detector + the kill-switch broadcast interface. The actual PAYG connection starts in P4 with Databento; subscription init starts at P8 Cloud Run deploy time) | A "no-op" stub channel registers and runs, emitting an empty `ChannelSignal`. The whole pipeline travels through (signal → fusion → decision → alert sink). The cost_tracker accepts three dummy calls (PAYG / subscription / free) and writes `cost_ledger.csv` (each row's `type` correct) + `cost_summary_<YYYY-MM>.csv` (columns prefixed with `type_payg_*` / `type_sub_*` / `type_free_*`) cleanly. Injecting an artificial PAYG cumulative fires an alert email once per threshold across all six steps |
| **P2** | Channel 1 (Polymarket) end-to-end. **v0 baseline detector** (5-min rolling volume z-score > 3) | Live WS → normalizer → features → v0 detector → `signal_store`. SQLite shows new signal rows |
| **P3** | Channel 2 (Hyperliquid) end-to-end. **v0 baseline detector** (OI delta > 95th percentile) | Same as P2                                                    |
| **P4** | Channel 3 (CME via Databento, **historical-only**) end-to-end. **v0 baseline detector** (volume z-score > 3). **The Databento client goes through the `cost_tracker` wrapper so every call's estimated cost is recorded in the CSV** (D13 first wiring) | Same as P2. The TV / UW adapter can come later. **Live stream is NOT enabled** (→ EVT-1). Databento usage rows land in cost_ledger; a simulated threshold (artificially inject cost in a test) fires the alert email |
| **P5** | Channel 4 (X via **snscrape**, 10-min polling) end-to-end. **v0 detector** (keyword whitelist match + per-account credibility weight) | Same as P2. **No image vision LLM** (→ EVT-1) |
| **P6** | Fusion engine + decision policy + state manager implemented. **The two-stage state structure (architecture §5.4)**: (1) channel-level tier baseline = simple z-score mapping (`<2 NORMAL / 2~3 WATCH / 3~5 RISK_OFF / ≥5 EMERGENCY`), (2) system-level state = `tier_floor = max()` + corroboration boost. **v0 dwell time / boost threshold = §5.4.3 placeholders as-is** | From stored input, fused_score + per_channel_tiers + system_state reproduce deterministically. max-tier-wins + boost rule behaves exactly as the architecture §5.4.2 scenario table (unit tests pass). Real threshold tuning lives in P10 |
| **P7** | **Alert delivery layer** (`alerts/router`, `throttle`, `link_builder`, `renderer/email`, `renderer/telegram`). The email body is a **5-min batch + last-30-min timeline view** (with per-channel labels like `5s ago / 3min ago / 12min ago`) — clock skew is digestible at human time scale. Implement the **priority-dilution-prevention 4-layer defense** (architecture §6.5.5): (1) Telegram = EMERGENCY only, (2) subject prefix tier hierarchy (`🚨🚨` / `⚠️` / `📊`), (3) EMERGENCY heartbeat reminder (1 h cadence), (4) higher-tier active cross-tag (auto-append the active EMERGENCY / RISK_OFF onto a lower-tier email subject). **The acknowledgement mechanism is deliberately skipped in v1** (last paragraph of architecture §6.5.5) | When a test event is injected, Gmail receives a RISK_OFF email and Telegram receives an EMERGENCY push. Throttle / cooldown + the 4-layer dilution prevention all work. Email shows the 30-min timeline + per_channel_tiers + tier_floor + boost + an AI one-liner. |
| **P8** | **End-to-end plumbing validation + hardening**: health / metrics + sanity check + runbook + perf pass + **Cloud Run deployment** (D12) + **cost_tracker 24h real-data validation + kill-switch dry-run** (D13: after 24 h of real Databento usage, verify cost CSV accuracy; simulate cap breach so the service auto-disables + transitions UNHEALTHY + all six alert-email threshold steps fire) | The 24h uptime test passes (Cloud Run) + a faked anomaly triggers end-to-end push delivery. Every channel's sanity check passes (architecture §6.4). cost_tracker accuracy within ±10%; kill-switch shuts down paid-service calls within one minute of firing. Mac Air can view Cloud Run logs + cost CSV. **At this point = "external connections + internal pipes + cost safety net all run cleanly"** ← only then do we enter P9 |
| **P9** | **Detection algorithm deep-dive** (= the brain). The dedicated §12 explains it in detail. AI/ML, statistical models, possibly differential-equation-based detectors all in scope. Spilt to a separate Cloud Run service when GPU is needed | Per-channel v1 detector + fusion-math algorithm spec is locked in writing. Performance improvement over the v0 baseline is established in historical replay. **Expect multiple back-and-forth sessions between human and AI** |
| **P10** | **Detection algorithm validation.** Replay the P9 detector across 11–15 historical events (Iran strike, Maduro arrest, Trump tariff remarks, etc.) → measure **two metrics** (per the last paragraph of architecture §5.4.3): (1) **Detection latency** (anomaly start → alert) — *primary, our system's performance*. **Goal: median ≤ 60s**. Achieved by jointly optimizing dwell time + threshold. (2) **Warning time** (alert → Trump announcement) — *informational, depends on insider timing*. Reported for user value, not system evaluation. → Replace placeholders in §5.4 (dwell time / boost threshold) with empirical values → bring false-positive risk High → Medium | Alerts emit near the right moment in **N or more** of the 11–15 historical events (precision / recall targets are defined in P9). **Median detection latency ≤ 60 s.** Warning-time distribution per event is laid out in the report. False-positive rate is quantified; threshold tuning is reproducible |

### 8.2 Event-triggered phases (EVT-n)

| EVT   | Trigger                                                  | Work                                                                                                        |
| :---- | :------------------------------------------------------- | :--------------------------------------------------------------------------------------------------------- |
| **EVT-1** | P10 completed (detection validated) **or** external pressure (X snscrape blocked, CME budget approved, etc.) | **Free → Paid migration.** (1) **CME**: Databento historical-only → live stream (config-flag toggle). (2) **X**: snscrape → X API Basic ($200/month) or add a vision LLM (either / both). The only code change is one collector adapter. |
| **EVT-2** | (Reserved slot — future)                                  | E.g. responding to an API change, adding a new channel, secret rotation, etc.                                |

> **Anomaly Streamlit panel is out of v1 scope** — to be integrated into the v2 comprehensive trading advisor (see §10).

### 8.3 Phase entry rules

- Each sequential phase only begins once **every** exit criterion of the previous phase is met.
- **The P8 → P9 transition is especially strict**: do not enter P9 unless all P1~P8 plumbing is stable. Reason: when debugging an algorithm bug in P9, you want to avoid the "is this an infrastructure issue or an algorithm issue?" confusion.
- EVT phases can run **in parallel** with sequential phases (especially EVT-1 will naturally arrive after P10).

---

## 9. Risks and mitigations

| Risk                                                            | Likelihood | Impact | Mitigation                                                                                                |
| :-------------------------------------------------------------- | :--------- | :----- | :-------------------------------------------------------------------------------------------------------- |
| **X** scraping blocked / HTML changes                            | Medium     | Medium | snscrape polling at 10-min intervals (low frequency = near-zero block risk). If blocked, swap in X API Basic ($200/month) or a vision LLM in EVT-1. Polymarket / Hyperliquid are official APIs and have near-zero rate-limit risk under normal use. |
| **Pay-as-you-go API runaway cost** (Databento, OpenAI vision, Cloud Run egress, etc.) | Medium     | **High** | **D13 cost ceiling + kill-switch (architecture §6.7)**: total monthly cap = $1,000 (sum over all paid services). Six cumulative thresholds (10/20/40/60/80/100%) each auto-fire an alert email (body shows total + per-tool breakdown). At 100%, paid services auto-disable (`channel.weight = 0`, fusion graceful degrade). Two CSVs under `data/anomaly_detection/cost/` (`cost_ledger.csv` per-call append + `cost_summary_<YYYY-MM>.csv` daily aggregate) for audit. Extra safety net: Databento symbol subset (CL/BZ/ES/NG) + historical-only (P4). Live is gradually enabled via config flag in EVT-1. |
| **False positive (alert fatigue)** — *currently High*           | **High**   | High   | **Honestly the hardest problem in anomaly detection.** All v1 §5.4 thresholds (channel-level tier mapping + dwell time + boost rule) are **placeholders**, so expect false positives early. Mitigations: (1) **two-stage state** (channel-level tier `NORMAL/WATCH/RISK_OFF/EMERGENCY` + system-level `tier_floor = max()`), (2) **corroboration boost** (need ≥ 2 channels at WATCH+ same direction to get +1 — single false positives do not boost), (3) **tier-asymmetric dwell time** (fast escalation / slow de-escalation → prevent flapping), (4) **state-change-only + same-symbol cooldown + WATCH digest** (§6.5 throttle), (5) **priority-dilution-prevention 4-layer defense** (§6.5.5) — so a real EMERGENCY is not buried by noise. **Real progress comes from P9 deep-dive + P10 historical event replay for threshold tuning** — not in one pass; multiple iterations. After P10, the risk is expected to drop High → Medium → Low gradually. |
| **Clock skew between sources**                                   | Medium     | Medium | Each channel collects independently, but the fusion engine compares times when judging "did multiple channels fire at the same moment?". A ms~s skew at the source server shakes the corroboration call. **Mitigation**: every timestamp is UTC + both `ts_source` (source-reported) and `ts_ingest` (our receive time) are saved + ordering is by `ts_source` (architecture §4.3). |
| Storage growth                                                  | Medium     | Low    | Raw payload is gitignored + rotated daily; only signals / decisions are kept long term.                    |
| X scraping reliability                                          | High       | Low    | X is treated as a **confirmation layer only** — it can never single-handedly drive a `RISK_OFF` decision. |
| Coupling regression on the existing RAG app                     | Low        | High   | Hard rule: no module under `src/anomaly_detection/` may import from `src/market_summary/advisor.py` or `src/market_summary/document_pipeline.py`. Bridges are one-way only. |

---

## 10. v2 preview (for reference)

v1 is a **backend-first + push-notification-first** anomaly detector.
v2's vision is to integrate it into a **comprehensive trading advisor**.

### 10.1 v2 data channels to add (user-provided ideas)

The exact `channels/<name>/` pattern is reused so **`core/` should not need changes**.
Below is the current set of user ideas — use as a starting point when writing v2 spec.

#### Channel A — Financial news
General financial news headlines / analysis / investment-bank price targets.

- Reuters / Bloomberg / Financial Times / CNBC / Yahoo Finance
- Sell-side analyst price targets

#### Channel B — Political news (partially absorbed into v1 as Channel 5)
Political / geopolitical events that directly affect equity prices.

- **Trump statements / Truth Social** — already implemented as v1 Channel 5 (@realDonaldTrump). v2 expands to other politicians + Truth Social broader feed.
- Tariffs, US-China trade conflict
- Oil-price-related policy
- Russia-Ukraine war, Iran conflict, other geopolitical disputes

#### Channel C — Fed stance
Central-bank policy / interest rates / key-official speeches.

- Fed / FOMC official announcements + dot plot
- Fed policy, rate decisions
- Powell speeches, Fed governor statements

#### Channel D — Corporate fundamentals
Per-company financials / insider behavior.

- Earnings / 10-K / 10-Q parsing
- Corporate insider sales (Form 4 / SEC EDGAR insider transactions)

#### Channel E — Macro indicators
Macro-economy data releases.

- Prices / consumption: CPI, PCE
- Labor market:
  - **Nonfarm Payrolls (NFP)** — monthly net new employment (BLS, first Friday of each month)
  - **Initial Jobless Claims** — weekly new jobless claims (leading indicator, every Thursday)
  - **JOLTS** (Job Openings and Labor Turnover Survey) — job openings + quit rate
  - **ADP Employment Report** — private-sector employment estimate (NFP preview, 2 days earlier)
  - **Unemployment Rate**
- Activity: **ISM Manufacturing PMI** + **ISM Services PMI** + **S&P Global PMI**
- Foreign central banks: BOJ rate decisions + ECB rates / guidance (USD impact)

#### Channel F — Institutional flow
Position / price target signals from large asset managers / banks.

Example institutions: BlackRock, The Vanguard Group, Charles Schwab Corporation,
UBS Group, Fidelity Investments, State Street Global Advisors, Allianz,
JPMorgan Chase, Bank of New York Mellon, Capital Group.

What we collect:
- Each institution's **portfolio weighting** by asset (e.g. 13F filings)
- Each institution's published **price target / outlook**
- Trend in weighting changes (`scaling up / down`)

#### Channel G — Tech-company + named-individual investment signals
A leading indicator of "where future capital is flowing".

Example companies: NVIDIA, Google, Microsoft, Apple, Amazon, Meta, Tesla, OpenAI, Anthropic, SpaceX, Palantir.
→ Track what companies / sectors they are aggressively investing in.
   Companies they invest in are likely candidates for mid-to-long-term appreciation.

Example individuals: Jensen Huang, Sam Altman, Elon Musk, Peter Thiel.
→ Their personal investment direction is a leading signal for future investment trends.

---

The fact that v2 can be "dropping in new channels only" is *exactly* why v1 enforces strict modular boundaries.

Every channel emits results matching the same `ChannelSignal` schema and shares the same fusion engine + alert layer.
v1's 5 channels + v2's 7 = 12 total channels running on one core.

### 10.2 v2 Streamlit dashboard (= "comprehensive trading advisor")

The anomaly UI deferred in v1 returns in v2 as:

- A **database** that consolidates v1 backend's **anomaly detection scores + raw news + AI/ML processed data**
- A **chat-style interface on Streamlit** that talks to that DB
- The user asks natural-language questions like "How's oil today?", "Summarize this week's anomaly patterns", "How should I size positions?"
- The user makes **future investment decisions** from that conversation

So, v1's push notification is for **immediate (defensive) action**, and
v2's dashboard is for **deliberate (strategic) decision-making**.
Different UX, both needed.

> **Important**: v2 does not change what the system **does** — the backend still gathers data 24/7, detects anomalies, and sends pushes.
> v2 just adds "a layer to converse with the accumulated data".

---

## 11. Design decisions — D1~D13 all LOCKED ✅

D1~D13 in `anomaly-architecture.md` §8 are all settled. **Once architecture review passes, we can enter P1.**

### ✅ LOCKED (implementation may begin)

| #   | Topic                       | Decision                                                                                  |
| :-- | :-------------------------- | :----------------------------------------------------------------------------------------- |
| D1  | Runtime model               | **A single "market signal" daemon** (5 channels = asyncio tasks). v2 adds more daemons per source domain (financial-news daemon, Fed daemon, etc.) |
| D2  | Async runtime               | **A single asyncio event loop** (inside the daemon)                                        |
| D3  | Persistence                 | **SQLite (signal/decision) + Parquet (raw/feature)**. ChromaDB is vector-search-only, so unsuited for v1. It returns when v2's dashboard needs RAG |
| D4  | Channel restart policy      | **Exponential backoff up to 60 s + 5-min circuit break after 10 failures** (using `tenacity`) |
| D5  | Fusion math                 | **State is decided by `tier_floor = max(per_channel_tiers)` + corroboration boost** (architecture §5.4.2). The `fused_score` from noisy-OR is demoted to a **secondary metric for reference / audit / boost input / email display** — the primary decision variable is channel-level tier. Reason: prevents single-channel high-conviction signals (e.g. CME-only 6,200 contracts) from being diluted by a denominator of 5 in `fused_score`. Algorithm is locked; parameters (channel weights, health decay, boost thresholds) are tuned in P10 replay |
| D6  | State definition (channel tier + system state) | **Two-stage** (architecture §5.4): (1) **Channel-level tier** — each channel emits its own `NORMAL/WATCH/RISK_OFF/EMERGENCY` tier from its detector pool. (2) **System-level state** — `tier_floor = max(per_channel_tiers)` (max-tier-wins) + corroboration boost (+1 tier when ≥ 2 channels are at WATCH+ in the same direction; EMERGENCY does not need a boost). Dwell time is **tier-asymmetric** (fast escalation ≤ 30 s / slow de-escalation 5–10 min). Everything is a v1 baseline placeholder — per-channel detector → tier rules are **defined in P9 deep-dive**, thresholds + dwell time + boost rules are **tuned by P10 historical event replay**. Directly tied to why the false-positive risk is High (see §9 risks) |
| D7  | CME data: free → paid       | **Start historical-only (P4) → enable live stream in EVT-1**                               |
| D8  | X & Truth Social access     | **X: snscrape 10-min polling (P5, free) → swap to X API Basic ($200/month) or add a vision LLM in EVT-1.** **Truth Social: httpx polling of `@realDonaldTrump` every ~5 min + Wayback Mastodon API JSON for historical backfill (already in v1).** Image vision LLM is not used during v1 validation |
| D9  | Does the RAG advisor consume the anomaly state? | **No (v1).** v1 keeps RAG and anomaly fully separate. Integrated in v2's comprehensive trading advisor |
| D10 | v1 symbols universe         | Polymarket: liquidity top-N + keyword watchlist (war / strike / oil / Iran / Hormuz / China / Fed / recession / tariff). Hyperliquid: BTC/ETH/SOL-PERP. CME futures: CL/BZ/ES/NG. CME options (UW): SPY/QQQ/USO/CL. X: 5 named accounts. Truth Social: @realDonaldTrump (Stage-1 keyword filter + Stage-2 LLM topic_slug classification). **All editable any time via watchlist config files.** |
| D11 | Notification & delivery     | **Email (Gmail SMTP) + Telegram bot.** WATCH = daily 06:00 PT (Bay Area) digest, RISK_OFF = immediate email, EMERGENCY = email + Telegram together + **1h heartbeat reminder** (state-change-only's sole exception). State-change-only + same-symbol 5-min cooldown. **Priority-dilution-prevention 4-layer defense** (architecture §6.5.5): Telegram = EMERGENCY only / subject prefix tier hierarchy (`🚨🚨` / `⚠️` / `📊`) / EMERGENCY heartbeat / higher-tier active cross-tag. **The acknowledgement mechanism is deliberately skipped in v1** — even if heartbeats keep coming, the user does not need to send an ack (rationale: a persistent EMERGENCY = persistent risk; YAGNI; revisit in v2 when fatigue is empirically measured). No quiet hours. SMS is v2 |
| D12 | Deployment target           | **The v1 daemon (5 channels + fusion + alerts) = one Google Cloud Run service** (always-on). **Mac Air = development + log / dashboard viewing only** (sleep-irrelevant). If the detection model needs a GPU in P9–P10, **split out as a separate Cloud Run job or Vertex AI** (daemon calls that endpoint). → must be reflected in architecture §3 process view + §6 cross-cutting |
| D13 | **Cost ceiling & kill-switch** (PAYG safety device) | **PAYG monthly cap = $1,000** (sum over Databento + OpenAI API + Cloud Run egress + Cloud Logging + any PAYG tools added in EVT-1 onwards only). Subscriptions (TradingView, Unusual Whales, X API Basic, Cloud Run base, etc.) are **not capped** — flat fee, no runaway risk. **BUT the CSV logs PAYG + subscription + free** (`type` column distinguishes them; purpose: aggregated user view). **6 cumulative threshold alerts (PAYG only)**: 10/20/40/60/80/100% = $100/$200/$400/$600/$800/$1,000; each crossing sends a dedicated email — the body shows (1) PAYG cumulative + per-PAYG-tool breakdown, (2) subscription total reference, (3) grand total. **At 100% the kill-switch fires**: only PAYG services auto-disable (`channel.weight = 0` → fusion graceful degrades, those channels go UNHEALTHY). Subscription channels are unaffected → graceful-degrade effect reinforced. **Two CSVs for audit** (`data/anomaly_detection/cost/`): `cost_ledger.csv` (PAYG per-call append + subscription monthly + free daily, all append-only) + `cost_summary_<YYYY-MM>.csv` (daily aggregate per tool, with `type_payg_*` / `type_sub_*` / `type_free_*` column prefixes). **Implementation**: P1 scaffold (record / record_subscription_monthly / record_free_daily helpers) → P4 Databento first PAYG wiring → P8 24h real validation + kill-switch dry-run. Full spec: architecture §6.7 |

### 📌 Items deferred to P9 / P10 (locked but need tuning)

> **P10 sign-off (2026-04-21)**: items marked ✅ closed via the §13 P10 results. ⏳ are carried forward as P10.5 enhancement candidates.

- ✅ **Channel-level detector → tier mapping rules** — all 5 channels (P9.4 added X + Truth Social on top of the original 4) defined their detector pool + locked thresholds in P9. Validated by 6 events replay (§13).
- ⏳ **Score normalization** (open gap) — after the D6 redesign, tier mapping acts as an implicit normalization and is sufficient for v1 operations, re-confirmed in P10. fused_score is used only for audit / display, so additional calibration is deferred. Revisit in P10.5 / v2. See the last paragraph of architecture §5.2.
- ✅ **D5 parameters** (channel weights, health-decay formula, fused_score → boost usage) — proven sufficient at the current values across the 6 P10 events (fp_count = 0; §13).
- ✅ **D6 threshold + dwell time + boost rule** — frozen after the 04-09 Liberation Day tuning; generalized across the 5 other events (§13).
- ✅ **Detection algorithm spec itself** — per-channel detector + fusion math locked (P9). Validation results (§13).
- ✅ **P10 dual metric** — (1) detection latency: at the 1-min bar-close boundary across all events (≤ 60 s under cycle granularity). (2) warning time: CME primary 13–20 min / Polymarket·Hyperliquid primary 26 h~6 days (see §13 table).
- ⏳ **P10.5 enhancement candidates** — cross-event repeated-actor correlation (the Brent oil pattern repeating 3 times), Hyperliquid full-market vol baseline (S3 asset_ctxs), X channel pre-event capture (expand watchlist).

---

## 12. P9 Detection algorithm deep-dive (placeholder)

> **This section is filled in after P8 is done.** For now it just outlines what will be covered.

During P1~P8 we use only v0 baseline detectors (z-score, percentile, etc.) to validate the plumbing.
In P9 we design the detection brain in earnest.

### 12.1 Topics to cover (expected)

- **Per-channel detector evolution**:
  - Polymarket: rolling z-score → CUSUM / Bayesian online change-point detection?
  - Hyperliquid: OI percentile → orderbook imbalance + whale-wallet clustering?
  - CME: volume z-score → multi-window VPIN, options-surface anomaly?
  - X: keyword match → sentiment + entity NER + cross-account corroboration?
  - Truth Social: Stage-1 keyword filter + Stage-2 GPT market-impact + topic_slug clustering (already P9.4)
- **Fusion math evolution**:
  - Keep noisy-OR or evolve to a Bayesian network?
  - Learn channel weights (logistic regression / gradient boosting on labeled events)?
- **AI/ML candidates**:
  - Time-series anomaly detection (Isolation Forest, autoencoder, TCN)
  - Multi-channel embeddings + similarity (cross-channel context fusion)
  - Possibly: differential / integral-equation dynamics models (price impact, OI flow PDE)
- **Compute infrastructure**:
  - GPU-requiring models cannot run on the Mac Air → **Cloud Run job or Vertex AI training job**
  - Inference is called from the daemon process (REST endpoint or batch job)

### 12.2 How we work

- Expect **multiple back-and-forth sessions** between human and AI (1–2 per feature)
- Each session: small spike code → sanity check against 1–2 historical events → lock the spec
- Finally, §13 will be filled in as the formal algorithm spec doc

### 12.3 P10 validation inputs

- Replay the detectors + fusion math locked in §12 across the 11–15 historical events of §10
- Measure precision / recall / lead-time; replace §5.4 thresholds (placeholder → empirical)
- Quantify false-positive rate + update the risk table (§9: High → Medium → Low)

---

## 13. P10 detection algorithm validation — sign-off (2026-04-21)

> **Replayed the P9-locked detector + fusion math across 6 historical insider-trading events: hit rate 6/6 (100%) + fp_count 0 + warning_time > 0 (every first alert lands before the announcement).** Conclusion: **§5.4 thresholds + dwell time + boost rule are now empirical values (no longer placeholders).** Additional enhancements (P10.5) are listed in §13.5.

### 13.1 Validation set — 6 historical events

| # | event_id | primary | symbol(s) | type | size | source |
| - | -------- | ------- | --------- | ---- | ---- | ------ |
| 1 | 2025-04-09_liberation_day | cme | BZ (Brent) | oil short burst | $XXX M | 04-09 Liberation Day Brent burst (tuning anchor ⭐) |
| 2 | 2025-10-10_china_tariff_100 | hyperliquid | BTC | crypto perp short | $1.1 B notional | "Bitcoin OG" 1 wallet, T-27h ~ T-1min |
| 3 | 2026-01-03_maduro_arrest | polymarket | maduro-in-us-custody-by-january-31 | political prediction | - | Maduro arrest pre-positioning |
| 4 | 2026-02-28_iran_first_strike | polymarket | will-the-us-next-strike-iran-on-february-* (top 4) | political prediction | - | 38-wallet split bet (multi-market YES) |
| 5 | 2026-03-23_iran_strike_pause | cme | CL + BZ + ES | oil short + S&P long | $580M oil + $1.5B ES | Trump Truth Social Iran pause |
| 6 | 2026-04-17_hormuz_open | cme | BZ | oil short burst | $760M, 7,990 lots | Iran FM Araghchi Hormuz open |

> 04-09 / 03-23 / 04-17 = the same oil-burst pattern repeated 3 times — strong suspicion of the same actor (CFTC investigation ongoing).

### 13.2 Results — cross-event matrix

| event | max_tier | first_alert_tier | warning_time | channels_fired | fp_count | note |
| ----- | :------: | :--------------: | -----------: | -------------- | :------: | ---- |
| 2025-04-09 liberation_day | **EMERGENCY** | EMERGENCY | +15 min | cme | **0** | tuning anchor ⭐ |
| 2025-10-10 china_tariff_100 | **EMERGENCY** | RISK_OFF | **+26.6 h** | cme + hyperliquid | **0** | new_whale $133M catch (T-1072min) |
| 2026-01-03 maduro_arrest | **EMERGENCY** | RISK_OFF | **+~6 days** | cme + polymarket | **0** | wallet concentration ('n_wallets=12 top=88%') |
| 2026-02-28 iran_first_strike | **EMERGENCY** | RISK_OFF | **+~6 days** | cme + polymarket | **0** | multi-market YES bet caught |
| 2026-03-23 iran_strike_pause | **EMERGENCY** | EMERGENCY | +13 min | cme | **0** | 3-symbol cross-corroboration (CL+BZ+ES) |
| 2026-04-17 hormuz_open | **EMERGENCY** | EMERGENCY | +20 min | cme | **0** | exactly 1 alert (ideal) |

**Key statistics**:
- max_tier = EMERGENCY: **6/6** ✅
- fp_count = 0: **6/6** ✅
- warning_time > 0 (alert before the announcement): **6/6** ✅
- detector config changes (after the tuning-anchor event): **0** → **no overfit** verified
- detection latency: within a 1-min bar boundary (achievable ≤ 60 s in real operation)

### 13.3 Channel-level detector validation

| channel | active detectors (P9 lock) | events validated | key finding |
| ------- | -------------------------- | ---------------- | ----------- |
| **CME** | `vol_z_v1` + `price_jump_v1` + `EMERGENCY_AND_RULE_5M` ruleset | 6/6 (fires as secondary or primary across every event) | A 5-min window of vol_z + price_jump is extremely stable. Cross-symbol corroboration (BZ+CL+ES simultaneously) emerges naturally |
| **Polymarket** | `vol_burst_v2` + `wallet_concentration_v1` + `WC_BOOST` modulator | 2/6 (01-03, 02-28) | Wallet concentration is the decisive separator between dispersed retail flow (38-wallet split) and a single insider actor. WC_BOOST + the `wc_unique≥5` guard keeps alert spam under control |
| **Hyperliquid** | `vol_z_v1` + `insider_v1` + `new_whale_v1` (`cluster_v1` / `panic_filter_v1` defined too, used when data is available) | 1/6 (10-10) | `new_whale_v1` catches the large entry of a fresh wallet exactly via 5-min cumulative notional thresholds ($2M / $10M / $25M) |
| **X** | `Stage1Filter` + `LLMClassifier` (P9.4 production) | 0/6 (announcement-side X accounts of the validation set are out of the watchlist) | Only forensic capture after the fact is possible. P10.5 candidate is watchlist expansion |
| **Truth Social** | `Stage1Filter` + `LLMAssessor` (P9.5 production, post-validation) | n/a (added after the validation set was frozen) | Channel 5 (added 2026-05) — Stage-1 keyword filter + Stage-2 GPT-5 market-impact + topic_slug clustering. To be validated against new historical Trump-post events |

### 13.4 Operational-load validation (with channel_alerts 60-min cooldown)

> raw cycle-level high-tier emit counts vs. user-facing alerts after a per-channel + per-tier 60min cooldown.

| event | total cycles | raw high-tier cycles | post-cooldown alerts | user load |
| ----- | -----------: | -------------------: | --------------------: | --------- |
| 2025-04-09 liberation_day | 91 | ~30 | 3 | low |
| 2025-10-10 china_tariff_100 | 1,662 | 237 | 34 | moderate (24h+ window) |
| 2026-01-03 maduro_arrest | 10,111 | ~600 | ~30 | low (~6 days / ~30 alerts ≈ 1 alert / 5h) |
| 2026-02-28 iran_first_strike | 8,671 | 1,061 | 26 | low |
| 2026-03-23 iran_strike_pause | 91 | ~25 | 3 | very low |
| 2026-04-17 hormuz_open | 91 | ~26 | **1** | ideal |

→ The "channel-level 60-min cooldown + tier-escalation pass" rule yields ~10–50× dedup vs. raw bursts. Inbox load is manageable.

### 13.5 P10.5 enhancement candidates (deferred)

- **Hyperliquid full-market vol baseline** — replay today runs off a CSV-based single-wallet record, so `vol_z_v1`'s baseline is self-referential (the wallet's own past volume). In production this is handled naturally by the daemon's `metaAndAssetCtxs` poll. If improved replay accuracy is needed, evaluate adopting the S3 asset_ctxs (requester-pays AWS).
- **Cross-event repeated-actor correlation** — 04-09 / 03-23 / 04-17 Brent oil repeated 3 times → very likely the same actor. Add an `actor signature` detector (1-min burst + unhedged one-way + same time slot) to raise confidence on a future 4th burst of the same pattern.
- **X channel pre-event capture** — add announcement-side accounts (Iran government accounts like Araghchi) to the watchlist so X catches the announcement side too. Requires the LLM to reason about the market impact of political statements accurately.
- **Ground-truth narrative match score** — `target_match_score` is a field in ReplayMetrics but is uncomputed in v1. Adding an automated comparison between the event-md expected timeline and the actual tier transitions would let us regression-test future detector changes automatically.

### 13.6 Risk re-assessment (input to §9 refresh)

- **False positive (alert fatigue)** — fp_count = 0 across the 6 events. Risk grade: **recommend downgrading High → Medium**. But the validation set is only 6 — hold off on Low until 1–2 months of real operation.
- **Source clock skew** — `ts_source`-based ordering works naturally in replay. No issues.
- Other risks in §9 remain valid as-is.

### 13.7 Sign-off

- **§5.4 thresholds + dwell time + boost rule = locked to empirical values**.
- **P10 exit criterion met** — alerts emit near the right moment in N (= 6) or more events + median detection latency ≤ 60 s + warning-time distribution organized + false-positive rate quantified.
- **P11 may begin** (real-operation deployment / EVT-1 paid migration review).

### 13.8 Post-sign-off P10.5 — Alert cooldown v2 + subject template lock (2026-04-21 PM)

Additional lock for the alert-fatigue issue discovered during the **6-event timeline-plot visual sanity check** right after P10 sign-off.

#### Problem (sanity check finding)

Re-measured the actual row counts of `channel_alerts.csv` (after the 60-min cooldown v1):

| event | post-cooldown alerts (v1, **re-measured**) | user load (v1) |
| ----- | -----------------------------------------: | -------------- |
| 2025-04-09 liberation_day | 1 | very low |
| 2025-10-10 china_tariff_100 | 34 | moderate |
| 2026-01-03 maduro_arrest | 128 | **high** (~6 days / 128 alerts ≈ 1/68min) |
| 2026-02-28 iran_first_strike | **278** | **fatigue** (~6 days / 278 alerts ≈ 1/31min) |
| 2026-03-23 iran_strike_pause | 3 | very low |
| 2026-04-17 hormuz_open | 1 | ideal |

→ The "26 / 30" estimates in §13.4 disagreed with the real cooldown-CSV rows. The **iran_first_strike Polymarket symbol "will-the-us-next-strike-iran-feb22" oscillated between EMERGENCY ↔ RISK_OFF for 6 days**, triggering the "tier escalation = pass immediately" rule every time → ~31 min apart. With one Polymarket event filling the inbox, real signals on other channels (CME / Hyperliquid / X) risk being buried.

#### Decisions locked by the user

1. **Cooldown key refined**: per `channel` → per `(channel, symbol, tier)`.
   → A CME/BZ EMERGENCY locked at the same time keeps a CME/CL EMERGENCY alive (cross-symbol corroboration preserved).
2. **Cooldown duration**: 60min → **24h (1440 min)**.
   → The same asset's EMERGENCY ceiling lock holds for 24 h.
3. **Demote silent**: a downward tier change (EMERGENCY → RISK_OFF / WATCH) is not notified.
   → Explicit user note: "no need to notify the easing".
4. **Escalation rule strengthened**: per (channel, symbol), only emit when the new tier is **higher than the max tier the user has seen in the last 24 h** (repeating the same tier inside the cooldown stays silent).
5. **Subject emoji + format lock** (alerts/subject_template.py):
   - channel emoji: cme=📊, polymarket=🔷, hyperliquid=🟡, x=𝕏, truth_social=𝕋
   - tier prefix: EMERGENCY=🚨🚨, RISK_OFF=⚠️, WATCH=📋 (swapped v1's `📊` → `📋` to avoid the CME-emoji clash)
   - format: `{tier_prefix} {channel_emoji} {CHANNEL_SHORT} · {symbol} → {TIER}`
     e.g. `🚨🚨 📊 CME · BZ → EMERGENCY` / `⚠️ 🔷 POLY · maduro → RISK_OFF`
6. **Brand-logo inline assets** (for P11 email body wiring): `assets/anomaly/channel_logos/{cme,polymarket,hyperliquid,x,truth_social}.png`. (CME ships without an official logo; revisit during P11 wiring.)

#### Result — v1 vs v2 comparison (production CLI re-run ✅)

| event | v1 alerts (60min, per-ch) | v2 alerts (24h, per-(ch,sym,tier)) | delta |
| ----- | -----------------------: | ---------------------------------: | ----- |
| 2025-04-09 liberation_day | 1 | **3** | +2 (cross-symbol preserved: BZ EMG → ES RISK_OFF → ES EMG) |
| 2025-10-10 china_tariff_100 | 34 | **12** | −22 (−65%) |
| 2026-01-03 maduro_arrest | 128 | **27** | −101 (−79%) |
| 2026-02-28 iran_first_strike | 278 | **46** | **−232 (−83%)** ← fatigue resolved |
| 2026-03-23 iran_strike_pause | 3 | **6** | +3 (cross-symbol preserved: BZ → ES → CL) |
| 2026-04-17 hormuz_open | 1 | **1** | 0 |
| **TOTAL** | **445** | **95** | **−79%** |

- `first_alert` timestamp / `max_tier` / `warning_time` / `channels_fired` all preserved → zero impact on detection latency.
- A standalone comparison script (`scripts/compare_alert_cooldown_v1_v2.py`) and the production CLI result match exactly (3, 12, 27, 46, 6, 1) → v2 rule operates correctly on the production code path.

#### Code change summary

- **`src/anomaly_detection/replay/reporters/channel_alerts.py`** — rewritten with the v2 5-tenet rule. cooldown default 60 → 1440, key tuple `(channel, symbol, tier)`, 24h sliding window tracking max-tier-seen.
- **`src/anomaly_detection/replay/cli.py`** — `--alert-cooldown-minutes` default 60 → 1440 + help text updated.
- **`src/anomaly_detection/alerts/subject_template.py`** — new. channel emoji + tier prefix + subject builder. Both the production renderer and the replay prototype import this.
- **`assets/anomaly/channel_logos/`** — new. 3 PNGs (polymarket, hyperliquid, x) (for P11 email body), CME / Truth Social added later.
- **`scripts/replay_six_events.sh`** — new. Helper for batch replay of the 6 events.
- **`scripts/compare_alert_cooldown_v1_v2.py`** — new. Kept around as a quick comparison tool for future cooldown-policy changes.

→ **P10.5 lock complete. P11 may begin.**

---

### 13.9 P11(a) — Production email renderer wiring (2026-04-21 evening)

All P11(a) sub-steps complete. **System-level fusion email path off + per-channel email dispatcher on.**

#### User decisions locked (P11(a))

1. **Email plot window**: 1h + 6h **stacked** (Q1.c).
2. **CME logo**: user-provided PNG → `assets/anomaly/channel_logos/cme.png`.
3. **Plot data source**: in-memory rolling buffer inside the orchestrator (Q3.a) — no separate storage.
4. **Dry-run safety**: `EMAIL_DRY_RUN` env (default true) — explicit toggle required for real send (Q4.a).
5. **System-level fusion email**: **OFF** (user decision — channel-level alerts only). Disabled via `AlertRouter.email_enabled=False`. The Telegram path remains for EMERGENCY heartbeat / URGENT (channel-level Telegram push + EMERGENCY-only filter come in P11(b)+).

#### Sub-steps complete

| step | scope | result |
| :--- | :---- | :----- |
| a.1  | New `email_plot.py` (production-grade plot generator, one image per window, X-axis shifted to alert_clock) | ✅ |
| a.2  | New `channel_email.py` (per-channel renderer: subject + HTML body + 1h/6h inline plot ×2 + brand logo cid) | ✅ |
| a.3  | New `inline_assets.py` (logo cid mapping + MIMEImage builder) + 4 user PNGs (added CME) | ✅ |
| a.4.1 | New `alerts/cooldown.py` — v2 cooldown shared module (both the replay reporter and the production daemon import it) | ✅ |
| a.4.2 | New `alerts/live_timeline.py` — 24h rolling `LiveTimelineBuffer` (production-side ReplayResult synthesizer) | ✅ |
| a.4.3 | New `alerts/channel_dispatcher.py` — `ChannelEmailDispatcher` (cooldown + plot + SMTP combined) + dry-run audit `.eml` auto-dump | ✅ |
| a.4.4 | Added `timeline_buffer` + `channel_dispatcher` parameters to `core/orchestrator.py` → push to buffer and call the dispatcher every cycle | ✅ |
| a.4.5 | In `entrypoints/anomaly_daemon.py`, wire up the buffer + cooldown + ChannelSmtpConfig + dispatcher; lock `AlertRouter(email_enabled=False)` | ✅ |
| a.5  | `scripts/preview_channel_alert_emails.py` + `scripts/preview_channel_logos.py` (`.eml` preview helpers) | ✅ |
| a.6  | Real SMTP test-send | **next step (user toggle)** — verify once with `EMAIL_DRY_RUN=false` + Gmail App Password |

#### Validation

- **Replay regression**: all 6 events produced exactly the v2 cooldown lock result (3 + 12 + 27 + 46 + 6 + 1 = **95 alerts**) — behavior identical after the refactor ✅.
- **End-to-end smoke** (synthetic ChannelSignals):
  - 4 channels firing at once → only the 2 that passed cooldown emit (CME BZ EMERGENCY, Polymarket trump-iran RISK_OFF) ✅
  - Re-fire the same signal 5 minutes later → `suppressed_cooldown` (0 emit) ✅
  - Demote the same (ch, sym) tier (BZ EMERGENCY → RISK_OFF) → `demote_silent` (0 emit) ✅
  - Same channel, different symbol (CME CL) EMERGENCY → cross-symbol corroboration preserved (1 emit) ✅
  - Subject lock prints exactly: `🚨🚨 📊 CME · BZ → EMERGENCY` / `⚠️ 🔷 POLY · trump-iran → RISK_OFF` ✅
  - 3 `.eml` + `plot_60m.png` / `plot_360m.png` per alert auto-dumped ✅
- **Daemon integration**: `AnomalyDaemon(load_config())` instantiates with no errors, both `router.email_enabled=False` and `orch.timeline_buffer/dispatcher` are wired ✅.
- **Snapshot endpoint**: `/snapshot` response gains a `channel_dispatcher` block (`stats`, `buffer_size`, `cooldown_minutes`, `active_cooldown_keys`, `smtp_dry_run`, `smtp_recipients`) — enables ops-health monitoring.

#### Next (P11(b)+)

- **Telegram channel-level push (EMERGENCY only)** — the Telegram path is currently system-level (inside router). Plans to split it with the same per-channel cooldown + EMERGENCY filter. A new `ChannelTelegramDispatcher` can follow the `ChannelEmailDispatcher` pattern.
- **a.6 real SMTP test-send** — pop a Gmail App Password into `.env`, toggle `EMAIL_DRY_RUN=false` once, do a visual sanity check on the received email.
- **Cloud Run deploy** — rebuild + deploy the daemon image, confirm dispatcher stats count correctly via `/snapshot`.

#### Code change summary (P11(a))

- `src/anomaly_detection/alerts/cooldown.py` — new (v2 cooldown shared module + `ChannelAlertCooldown` stateful wrapper).
- `src/anomaly_detection/alerts/live_timeline.py` — new (`LiveTimelineBuffer` 24h rolling, `to_replay_result_at()` synthesis).
- `src/anomaly_detection/alerts/channel_dispatcher.py` — new (per-channel cooldown + email dispatcher, dry-run `.eml` audit dump).
- `src/anomaly_detection/alerts/renderer/channel_email.py` — new (P11(a).2, the core renderer of this phase; render_email + send_email + dump_eml).
- `src/anomaly_detection/alerts/renderer/email_plot.py` — new (P11(a).1, alert-window plot generator).
- `src/anomaly_detection/alerts/renderer/inline_assets.py` — new (P11(a).3, logo cid + MIMEImage builder).
- `src/anomaly_detection/alerts/router.py` — add `email_enabled` flag (default True; the daemon sets False). `flush_digest_now` also no-ops under the same flag.
- `src/anomaly_detection/core/orchestrator.py` — add optional `timeline_buffer` + `channel_dispatcher` params, plus the new §6.5/6.6 step inside `_run_one_cycle`.
- `src/anomaly_detection/entrypoints/anomaly_daemon.py` — wire buffer + cooldown + `ChannelSmtpConfig` + `ChannelEmailDispatcher`, set `AlertRouter(email_enabled=False)`, expose dispatcher info via `/snapshot`.
- `src/anomaly_detection/replay/reporters/channel_alerts.py` — drop inline cooldown code; reuse the pure function from `alerts/cooldown.py` (behavior identical).
- `assets/anomaly/channel_logos/cme.png` — user-provided CME logo added (polymarket/hyperliquid/x.png were added in §13.8; during P11(a).3 we swapped the polymarket↔hyperliquid filenames to correct the original mismatch).
- `scripts/preview_channel_alert_emails.py` + `scripts/preview_channel_logos.py` — new (`.eml` dry-run preview tools).

→ **P11(a) lock complete. a.6 SMTP toggle validation + Cloud Run deploy (P11(b)) may begin.**

### 13.10 P11(d) — Channel-level Telegram dispatcher (EMERGENCY only)

User decision right after P11(a) complete: **system-level Telegram path also OFF, channel-level + EMERGENCY only**. A Telegram dispatcher is added with the same shape as the P11(a) email work; the system-level URGENT push + 1h heartbeat reminder are both disabled. **The 24h cooldown_expired itself plays the role of a natural reminder** (the per-channel dispatcher re-sends the same alert → email + Telegram both come again).

#### Decisions (user lock 2026-04-21)

| Question                                                   | Lock |
| :--------------------------------------------------------- | :--- |
| System-level Telegram (URGENT push + heartbeat reminder)   | **Both OFF.** Channel-level only. Heartbeat is naturally handled by the channel-level 24h cooldown_expired. |
| Email / Telegram cooldown sharing                          | **Shared.** When one `(channel, symbol, tier)` passes within 24h, both email and Telegram fire, then both go silent. `cooldown.decide` is called once per cycle. |
| Telegram body                                              | **Same as email — subject + key metadata + 1h/6h plot ×2** (sendMediaGroup). Plot PNGs are reused from the email stage (no second render). |
| Tier filter                                                | **EMERGENCY only.** RISK_OFF / WATCH go email-only (`telegram_emergency_only=True`). |

#### Sub-steps complete

| step | scope | result |
| :--- | :---- | :----- |
| d.1  | New `alerts/renderer/channel_telegram.py` — `render_channel_telegram()` (caption HTML ≤ 1024 chars, reusing subject_template) + `send_channel_telegram()` (sendMediaGroup, dry-run capture) + `dump_telegram_capture()` (.txt audit) | ✅ |
| d.2  | Refactor `alerts/channel_dispatcher.py` — `ChannelEmailDispatcher` → `ChannelAlertDispatcher` (unified email + Telegram, single cooldown call, single plot reused, tier filter, separated stats: `email_errors` / `telegram_*`). `ChannelEmailDispatcher` retained as backward-compat alias. New `DispatchResult` (emails + telegrams). | ✅ |
| d.3  | Add `telegram_enabled` flag to `alerts/router.py` (default True; backward-compat) — URGENT dispatch + `emit_heartbeat_if_due()` both no-op behind the flag. | ✅ |
| d.4  | Wire up `entrypoints/anomaly_daemon.py` — inject `channel_telegram_config` (separate TelegramConfig instance; dry_run policy same as email), lock `telegram_emergency_only=True`, lock `AlertRouter(telegram_enabled=False)`, comment out the `_heartbeat_loop` task creation. Expose `telegram_enabled` / `telegram_dry_run` / `telegram_emergency_only` via `/snapshot`. | ✅ |
| d.5  | End-to-end synthetic smoke (5 cases) + 6-event replay regression (95 alerts identical) | ✅ |
| d.6  | docs/anomaly-upgrade-plan.md §13.10 + changelog | ✅ |

#### Validation

- **Replay regression**: all 6 events match the baseline (3 + 12 + 27 + 46 + 6 + 1 = **95 alerts, delta=0**) — the refactor does not affect cooldown results ✅.
- **End-to-end smoke** (synthetic ChannelSignals, 5 cases):
  - case 1: initial CME/BZ EMERGENCY → both email + telegram fire ✅
  - case 2: same (CME, BZ, EMERGENCY) 1h later → cooldown shared → both silent ✅
  - case 3: CME/CL RISK_OFF (different symbol, lower tier) → email only, `telegram_skipped_tier` +1 ✅
  - case 4: escalation on CME/CL RISK_OFF → EMERGENCY → both fire ✅
  - case 5: cooldown_expired after 25h → both fire, Telegram caption auto-tagged "🔔 24h elapsed reminder (risk still in progress)" ✅
  - Final stats: `considered=5 emitted=4 suppressed=1 telegram_emitted=3 telegram_skipped_tier=1 telegram_errors=0`
  - Dry-run audit: 4 `.eml` + 3 `channel_telegram.txt` per alert dir.
- **Telegram caption format** (excerpt from actual .txt): `<b>🚨🚨 📊 CME · CL → EMERGENCY</b>\n🕐 PT  (UTC)\n🎯 fired: vol_z_v1\n📊 score=0.90 · direction=down\n📝 reason: ...\n⏱ 🔔 24h elapsed reminder ...· 24h lock\n<i>id: ...</i>` — matches the email subject exactly, includes the cross-link audit ID.
- **Daemon integration**: import + `AnomalyDaemon` instantiate clean. Heartbeat task creation commented out — the `_heartbeat_loop()` method itself stays in code (in case per-channel heartbeats become useful later).

#### Code change summary (P11(d))

- `src/anomaly_detection/alerts/renderer/channel_telegram.py` — **new** (per-channel Telegram renderer + send + .txt audit dump).
- `src/anomaly_detection/alerts/channel_dispatcher.py` — `ChannelEmailDispatcher` → `ChannelAlertDispatcher` rename + Telegram path + `DispatchResult` new. `ChannelEmailDispatcher` alias retained (backward-compat).
- `src/anomaly_detection/alerts/router.py` — `telegram_enabled` flag added (default True). URGENT dispatch + `emit_heartbeat_if_due()` both check the flag.
- `src/anomaly_detection/core/orchestrator.py` — type hint `ChannelEmailDispatcher` → `ChannelAlertDispatcher` updated; docstring `email + (optional) telegram`.
- `src/anomaly_detection/entrypoints/anomaly_daemon.py` — import rename; inject `channel_telegram_config`; lock `telegram_emergency_only=True`; lock `AlertRouter(telegram_enabled=False)`; comment out `_heartbeat_loop` task creation; expose Telegram block in `/snapshot`.

→ **P11(d) lock complete. System-level email + telegram + heartbeat all OFF; channel-level email (all tiers) + telegram (EMERGENCY only) is the operating model. Cloud Run deploy (P11(b)) may begin.**

---

### 13.11 P11(b) — Cloud Run production deploy (v0.4.0 = P11(a/d) lock applied)

Production receives the P11(a/d) lock right after P11(d) lock. Proceeded with user confirmation — the previous revision (`anomaly-daemon-00018`, image `:v0.3.9`, deployed 2026-04-20 16:21 UTC) only had up to P10.5 cooldown v2; channel-level dispatcher (P11(a)) and channel-level Telegram (P11(d)) were not in production. v0.4.0 syncs both at once.

#### Decisions (user lock 2026-04-21)

| Question                            | Lock |
| :---------------------------------- | :--- |
| Build path                          | **Cloud Build** (`cloudbuild.anomaly_detection.yaml`) — safer than local docker build on M1 Mac (arm64), automatic cross-platform amd64, 5 min vs. 10–15 min, same path used for all v0.1.1–v0.3.9 builds. |
| Version bump                        | **minor (v0.3.9 → v0.4.0)** — adding channel-level telegram is roughly a minor feature. |
| Deploy strategy                     | **100% traffic shift immediately** (Cloud Run default). Cut over to the new revision without a canary split — already validated locally + smoke test 5 cases + 6-event replay regression. |
| Service config                      | **Kept as-is** (`--min-instances=1`, `--max-instances=1`, `--no-cpu-throttling`, `--cpu=1`, `--memory=512Mi`, no secret-mount changes). |

#### Sub-steps complete

| step | scope | result |
| :--- | :---- | :----- |
| c.1  | Add `matplotlib` + `pillow` to `requirements_v1-anomaly_detection.txt` — needed for plot generation by channel_email + channel_telegram inside the production image. | ✅ |
| c.2  | Add `COPY assets/anomaly/ ./assets/anomaly/` to `Dockerfile.anomaly_detection` — the 4 channel brand-logo PNGs (CME/POLY/HL/X) need to ship inside the image so production email's inline `cid:logo_<channel>` does not break. `inline_assets._REPO_ROOT` is `Path(__file__).parents[4]` = `/app` — same layout. | ✅ |
| c.3  | Add the P11(d) section to `docs/cloud-run-deploy.md` — explain the `telegram_emergency_only=True` behavior, validation via `/snapshot.channel_dispatcher`, and the dry-run audit-dump directory path (`${ANOMALY_DATA_PATH}/alerts_live/{ch}_{sym}_{tier}_{ts}/`). | ✅ |
| c.4  | New `scripts/deploy_anomaly.sh` (chmod +x) — one-shot script with 3 modes (`v0.x.y` / `--build-only` / `--env-only`) for the local docker-build path (using `gcloud builds submit` + `gcloud run deploy` directly is faster when using Cloud Build). | ✅ |
| c.5  | Local docker build sanity check (M1 arm64) — `anomaly-daemon:local` 1.18 GB, `/health` ok, `/snapshot.channel_dispatcher` shows `cooldown_minutes=1440`, `buffer_max_age_h=24`, `telegram_emergency_only=true`, automatic dry_run (no creds) — all normal. | ✅ |
| c.6  | **Production deploy** — `cloudbuild.anomaly_detection.yaml` v0.3.9 → v0.4.0 bump → `gcloud builds submit --config cloudbuild.anomaly_detection.yaml .` (build `90a40afe-...` SUCCESS, 1m 55s) → `gcloud run deploy anomaly-daemon --image=...:v0.4.0 --region=us-west2` (revision `anomaly-daemon-00019-76h`, 100% traffic). | ✅ |

#### Validation (live production `/snapshot`)

```json
{
  "alert_modes": {
    "email_dry_run": false,         // ← real send (SMTP_PASSWORD secret mount OK)
    "telegram_dry_run": false       // ← real send (TELEGRAM_BOT_TOKEN secret mount OK)
  },
  "channel_dispatcher": {
    "stats": {"considered": 0, "emitted": 0, "suppressed": 0,
              "email_errors": 0, "telegram_emitted": 0,
              "telegram_skipped_tier": 0, "telegram_errors": 0},
    "buffer_size": 7,                // ← LiveTimelineBuffer collects snapshots every cycle (5 s)
    "buffer_max_age_h": 24,          // ← P11(a) 24h rolling lock
    "cooldown_minutes": 1440,        // ← P10.5 v2 24h lock
    "active_cooldown_keys": 0,
    "smtp_dry_run": false,
    "smtp_recipients": ["cyjeong@umich.edu"],
    "telegram_enabled": true,
    "telegram_dry_run": false,
    "telegram_emergency_only": true  // ← P11(d) hard-lock
  }
}
```

→ Every P11(a/d) lock is active in production. The next channel signal that passes `cooldown.decide` will auto-deliver to the inbox + Telegram (no manual trigger needed).

#### Code change summary (P11(b))

- `cloudbuild.anomaly_detection.yaml` — image tag v0.3.9 → v0.4.0.
- `requirements_v1-anomaly_detection.txt` — add `matplotlib` + `pillow`.
- `Dockerfile.anomaly_detection` — `COPY assets/anomaly/ ./assets/anomaly/`.
- `docs/v1-anomaly_detection/cloud-run-deploy.md` — P11(d) telegram emergency-only section + dry-run audit dump location + `/snapshot.channel_dispatcher` validation method.
- `scripts/deploy_anomaly.sh` — **new** (one-shot deploy script for the local docker-build path).

→ **P11(b) lock complete. v0.4.0 active in production (anomaly-daemon-00019). Next steps: operational monitoring — confirm the first production alert lands in inbox + Telegram + verify the stats counter (especially `telegram_emergency_only` behavior) after 24h+ of uptime.**

---

## 14. Change log

> Note: entries below pre-date the 2026-05 directory reorganization. Path references such as
> `src/anomaly/` and `data/anomaly/` map to the new `src/anomaly_detection/` and
> `data/anomaly_detection/` respectively.

| Date    | Author                | Change                                                                                              |
| :------ | :-------------------- | :-------------------------------------------------------------------------------------------------- |
| _today_ | ChangYeong + AI pair  | Initial draft for alignment.                                                                        |
| _today_ | ChangYeong + AI pair  | Rewrote the 100% English doc as English-first.                                                       |
| _today_ | ChangYeong + AI pair  | Updated Goal #3 / #5 / success criteria to explicitly require noisy-OR fusion + per-channel visibility as hard requirements. |
| _today_ | ChangYeong + AI pair  | Added the channel API/feed sanity check (right data / right timing / right format) item to success criteria. Pairs with architecture §6.4. |
| _today_ | ChangYeong + AI pair  | **Switched to push-first delivery**: v1 PRIMARY UX = email (Gmail SMTP) + Telegram bot. The Streamlit anomaly dashboard is removed from v1 scope → absorbed by the v2 comprehensive trading advisor. Goal #5, success criteria, §4 in/out scope, §5 conflict check, §6 folder layout (alerts/ promoted to a sub-package + ui/ removed), §7 dependencies (added telegram/jinja2), §8 phases (P7 = alert delivery, ~~P7 Streamlit~~ → v2), §10 v2 preview expanded (10.1 channels + 10.2 dashboard vision), §11 D9/D11 marked LOCKED. |
| _today_ | ChangYeong + AI pair  | **D1~D11 all LOCKED**. Cleaned up §6 folder layout (removed v1's `services/` and `notebooks/anomaly_prototyping/`, README rewritten end-to-end at project finish), restructured §8 phases (P0=alignment, P1=skeleton, P4 CME historical-only / P5 X snscrape / **P9 free→paid migration** added), updated §9 risks (X-only rate-limit explicit + false-positive High explanation + clock skew detail), reorganized §10.1 v2 channels by user idea (Channels A~G: financial news / political / Fed stance / corporate fundamentals + insider sales / macro + BOJ / institutional flow + 13F / big tech + named individuals investment). Swapped the §11 OPEN questions table for a LOCKED design-decisions table. |
| _today_ | ChangYeong + AI pair  | Organized §10.1 Channel E (macro) by official indicator names (NFP / Initial Jobless Claims / JOLTS / ADP / Unemployment Rate / ISM Manufacturing PMI + ISM Services PMI / BOJ + ECB). Locked the "5-min batch + 30-min timeline email" UX spec into §8 P7 deliverable (clock skew resolved on human time scale via the timeline view). Fixed D8 polling interval 5 min → 10 min. |
| _today_ | ChangYeong + AI pair  | **Restructured §8 phases (walking-skeleton strategy)**: end-to-end plumbing (P1~P8) first → detection deep-dive (P9~P10) later. Moved the old P9 (free→paid) to **EVT-1** (event-triggered). Made the **v0 baseline detector** explicit across P2~P5 (not "no-op"). Added **P9 = Detection algorithm deep-dive** + **P10 = Detection validation**. Added Cloud Run deployment to the P8 exit criterion. Split §8 into §8.1 (sequential) + §8.2 (event-triggered) + §8.3 (phase entry rules). **Added §12**: P9 deep-dive placeholder (topics, workflow, P10 validation inputs). Promoted the old §12 change log → §13. **Added D12 OPEN to §11**: deployment target (default = Cloud Run). Updated §9 risks "P9" references to EVT-1. Updated §11 false-positive mitigation reference to P9/P10. |
| _today_ | ChangYeong + AI pair  | **D12 LOCKED**: the v1 daemon = one Google Cloud Run service (always-on); Mac Air = dev + log viewing only; split off to a separate Cloud Run job or Vertex AI if a GPU is needed in P9. Updated the §11 heading to "D1~D12 all LOCKED ✅". → Reflecting D12 into architecture §3 process view + §6 cross-cutting is part of the architecture review still pending. |
| _today_ | ChangYeong + AI pair  | **Added D13 LOCKED: Cost ceiling & kill-switch.** A pay-as-you-go runaway-billing safety device — PAYG monthly cap = $1,000 (sum across Databento + OpenAI + Cloud Run egress + Cloud Logging; flat subscriptions excluded). Six cumulative thresholds (10/20/40/60/80/100%) each send an alert email (body shows total + per-tool breakdown). At 100%, PAYG services auto-disable → channel UNHEALTHY → fusion graceful degrade. Two CSVs (`cost_ledger.csv` per-call append + `cost_summary_<YYYY-MM>.csv` daily aggregate, in `data/anomaly_detection/cost/`) for audit, openable by hand. Impact: §6 folder layout (`monitoring/cost_tracker.py` + `data/anomaly_detection/cost/` added), §8 P1 (scaffold) / P4 (Databento first wiring) / P8 (24h real measurement + kill-switch dry-run), §9 risks (renamed the Databento cost row to "Pay-as-you-go API runaway cost" + kill-switch reference), §11 D13 LOCKED + heading D1~D12 → D1~D13. Architecture §6.7 added + §7 NFR + §8 D13 + §9 Glossary. |
| _today_ | ChangYeong + AI pair  | **§11 D13 expanded: aggregate cost view (CSV logs PAYG + subscription + free).** User request — alerts/kill-switch apply only to PAYG, but the two CSVs record every tool so total cost is in one place. The `type` column (`payg`/`subscription`/`free`) distinguishes them, ledger shows `payg_cum_month` + `total_cum_month` together. Made the subscription tool list (TradingView/Unusual Whales/X API Basic/Cloud Run base) and the free tool list (Telegram/Gmail/Polymarket/Hyperliquid/snscrape) explicit. Documented that the P1 cost_tracker scaffold includes `record_subscription_monthly()` + `record_free_daily()`. Reinforced graceful-degrade: the kill-switch only marks PAYG channels UNHEALTHY → subscription channels keep operating. Full spec: architecture §6.7. |
| _today_ | ChangYeong + AI pair  | **Documented the score-normalization gap (P9 deferred item).** Added the item to §11 "Items deferred to P9 / P10". Also recorded the reason it is low priority in v1 — after the D6 redesign, channel-level tier is the primary input to system_state, so the tier mapping itself plays an implicit normalization role. fused_score is used for audit / boost aux / display, so the step-wise mapping (current v0) is workable. Detail in the last paragraph of architecture §5.2. |
| _today_ | ChangYeong + AI pair  | **Plan minor consistency fix.** (1) Reinforced the §3.3 success-criteria email-body anatomy to mirror architecture §6.5.2 (system_state + per_channel_tiers + tier_floor + boost_applied + fired_detectors + reason_codes + fused_score, etc.). (2) Fixed a §13 change-log section reference (`§11.1` → `§11 "P9/P10 deferred" subsection`). |
| _today_ | ChangYeong + AI pair  | **D13 minor alignment fix.** (1) Updated the `cost_tracker.py` comment in the §6 folder layout from "paid API call wrapper" → "wrapper for every external API/service call (PAYG record + subscription monthly + free daily) + PAYG threshold alert + kill-switch" (reflecting the post-D13 expansion). (2) Documented the cost_tracker triple-helper (`record` / `record_subscription_monthly` / `record_free_daily`) + the CSV `type` column + the summary CSV `type_payg_*` / `type_sub_*` / `type_free_*` prefix validation + 6-step threshold dummy send validation in the §8 P1 deliverable + exit criterion. → Consistent with architecture §6.7. |
| _today_ | ChangYeong + AI pair  | **Architecture-review-driven plan back-sync (alignment sweep).** (1) **§3.1 Goal #4 / #5 + §3.3 success criteria**: switched the corroboration guard → **two-stage state (channel tier + system-level max-tier-wins + boost)** model, made per_channel_tiers / tier_floor / boost / fired_detectors mandatory push payload items, quantified push latency targets at EMERGENCY < 10s P95 / RISK_OFF < 60s P95. (2) **§8 P6**: documented the channel-level tier baseline mapping (z-score → tier) + the two-stage state structure. (3) **§8 P7**: **priority-dilution-prevention 4-layer defense** (Telegram EMERGENCY only / subject prefix `🚨🚨` `⚠️` `📊` / heartbeat / cross-tag) + acknowledgement v1 skip explicit. (4) **§8 P10**: dual metric — detection latency (primary, ≤ 60s) + warning time (informational). (5) **§9 risks false positive**: refreshed mitigation in 5 layers (max-tier-wins + boost + tier-asymmetric dwell + dilution prevention). (6) **§11 D5**: demoted fused_score to a secondary metric (reference / audit / boost input). (7) **§11 D6**: rewrote completely as **State definition (channel tier + system state)** — two-stage + tier-asymmetric dwell + P9/P10 tune. (8) **§11 D11**: heartbeat (1h, the sole exception to state-change-only) + 4-layer dilution prevention + acknowledgement v1 deliberate skip + Bay Area time digest documented. (9) **§11 "P9/P10 deferred" subsection**: channel tier mapping + dual metric documented. → **plan ↔ architecture in full alignment. P1 may begin.** |
| 2026-05-16 | ChangYeong + AI pair | **Added Channel 5 = Truth Social.** Restructured from 4 channels to 5 channels throughout this document: §3.1 Goal #1 (Truth Social bullet), §3.3 success criteria (5 channels), §4.1 in-scope (new "Channel 5 — Truth Social" row), §6 folder layout (`channels/truth_social/`, `data/anomaly_detection/truthsocial/` reference store), §7 dependencies (added `openai`, `matplotlib + pillow`, `beautifulsoup4`), §10.1 Channel B (partially absorbed into v1 as Channel 5), §11 D1 (5 channels), D8 (Truth Social access policy), D10 (Truth Social symbol = @realDonaldTrump, Stage-1 keyword + Stage-2 LLM), D12 (5-channel daemon), §13.3 channel-validation table (Truth Social row). Translated the entire document to 100% English in the same pass. Updated all paths `src/anomaly/` → `src/anomaly_detection/`, `data/anomaly/` → `data/anomaly_detection/`, `Dockerfile.anomaly` → `Dockerfile.anomaly_detection`, `requirements-anomaly.txt` → `requirements_v1-anomaly_detection.txt`, `cloudbuild.anomaly.yaml` → `cloudbuild.anomaly_detection.yaml`. |
