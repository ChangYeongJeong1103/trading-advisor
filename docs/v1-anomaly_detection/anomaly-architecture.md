# Anomaly Detection — System Architecture (v1)

> Status: **ALIGNED — D1~D13 LOCKED, ready for P1**
> Companion doc: [`anomaly-upgrade-plan.md`](anomaly-upgrade-plan.md)
> Scope: First-pass upgrade of `trading-advisor`.
> v2 (news / Fed / earnings / Reddit / political feed) is out of scope for this
> document, but the design must be able to accept v2 without changing the core.

---

## 0. How to read this doc

This document follows a layered system-design format:

1. **Context** — what the system is, who uses it, what it talks to.
2. **Logical view** — components and their responsibilities.
3. **Process view** — how it actually runs at runtime (async, process).
4. **Data view** — schemas, stores, retention.
5. **Behavioral view** — the most important flows, step by step.
6. **Cross-cutting concerns** — config, security, observability, failure.
7. **Non-functional requirements** — latency, reliability, cost.
8. **Design decisions (all LOCKED)** — D1~D13 confirmed decisions and their rationale.

Every diagram is drawn in **plain ASCII** so it renders identically across any
editor / GitHub / terminal / code-review tool.

---

## 1. Context

The outermost boundary of the system — user, existing app, the new subsystem, and
every external service we read from.

```
                          ┌─────────────────────────┐
                          │       User / Trader     │
                          └────────────┬────────────┘
                                       │  asks questions / reads radar
                                       ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │                         trading-advisor                           │
   │                                                                   │
   │   ┌──────────────────────────┐    ┌──────────────────────────┐    │
   │   │ Conditional RAG Advisor  │    │ Anomaly Detection        │    │
   │   │ (existing — unchanged)   │    │ Subsystem (NEW v1)       │    │
   │   └──────────────────────────┘    └────────────┬─────────────┘    │
   └────────────────────────────────────────────────┼──────────────────┘
                                                    │
       ┌────────────────────┬─────────────────────┬─┴───────────────────┬─────────────────────┐
       ▼                    ▼                     ▼                     ▼                     ▼
 ┌──────────────┐    ┌──────────────┐     ┌──────────────┐     ┌──────────────────┐    ┌──────────────────────┐
 │  Polymarket  │    │  Hyperliquid │     │  Databento   │     │ X (Twitter)      │    │ Truth Social         │
 │  (WS+REST)   │    │  (WS+REST)   │     │ (CME live +  │     │ 5 named accounts │    │ @realDonaldTrump     │
 │  Dune (SQL)  │    │  Hypurrscan  │     │ historical)  │     │  @Lookonchain    │    │ (httpx scrape +      │
 │              │    │  (manual)    │     │              │     │  @WhaleAlert     │    │ Wayback for backfill)│
 │              │    │              │     │ TradingView  │     │  @PolymarketHist │    │                      │
 │              │    │              │     │ (webhook)    │     │  @UnusualWhales  │    │                      │
 │              │    │              │     │ Unusual      │     │  @Bubblemaps     │    │                      │
 │              │    │              │     │ Whales       │     │                  │    │                      │
 └──────────────┘    └──────────────┘     └──────────────┘     └──────────────────┘    └──────────────────────┘
```

**Boundary**: the existing RAG advisor and the new anomaly subsystem live in the
same repository, but **their runtimes are completely independent subsystems**.

- The existing RAG Streamlit app is an **on-demand process the user launches on a Mac Air**.
- The new anomaly daemon is an **always-on 24/7 service running on Google Cloud Run** (D12).
- In v1 there is no code path between the two. v2's "comprehensive trading advisor"
  will introduce a read-only bridge (see §10 plan).

`Hypurrscan` is deliberately **out-of-band** — used only for manual visual checks
by a human; the system never queries it.

---

## 2. Logical view

The internal structure of the anomaly subsystem. Each box is a code module
(folder) with a single responsibility.

```
                    ┌──────────────────────────────┐
                    │       Config / Secrets       │
                    │  API keys, thresholds, watch │
                    │     list, feature flags      │
                    └──────────────┬───────────────┘
                                   │
┌────────────────────────────────────────────────────────────────────────┐
│                           Channel Modules                              │
│                                                                        │
│  Channel 1  Polymarket Module                                          │
│   - collector       (WS + REST live ingest)                            │
│   - dune_backfill   (historical / wallet pattern / backtest)           │
│   - normalizer      (raw payload  →  NormalizedEvent)                  │
│   - features        (vol z-score, prob jump, OB imbalance, ...)        │
│   - detector        (rules  →  ChannelSignal)                          │
│                                                                        │
│  Channel 2  Hyperliquid Module                                         │
│   - collector       (official WS / API)                                │
│   - normalizer                                                         │
│   - features        (OI delta, whale fills, L/S skew, ...)             │
│   - detector                                                           │
│   (Hypurrscan = manual visual check, not a code path)                  │
│                                                                        │
│  Channel 3  CME / Options Module                                       │
│   - databento_client       (primary live + historical)                 │
│   - tradingview_adapter    (ingest user-defined alert webhooks)        │
│   - unusualwhales_adapter  (options-flow context)                      │
│   - normalizer / features / detector                                   │
│                                                                        │
│  Channel 4  X Signal Module                                            │
│   - collector       (API or scrape, 5 named accounts)                  │
│   - parser          (post text  →  structured event)                   │
│   - credibility     (per-account weight)                               │
│   - detector                                                           │
│                                                                        │
│  Channel 5  Truth Social Module                                        │
│   - collector       (httpx polling of @realDonaldTrump, every ~5 min)  │
│   - backfill        (Wayback Machine for historical posts)             │
│   - parser          (post text + media → structured event)             │
│   - llm_assessor    (Stage-2 GPT-5 market-impact + topic_slug score)   │
│   - detector        (Stage-1 keyword/regex + Stage-2 LLM gating)       │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │   RawEvent  /  NormalizedEvent
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│                  Normalization / Feature Layer                         │
│  - convert each channel's output into a common schema                  │
│  - event time, symbol, source, direction, size, confidence             │
│  - maintain per-channel × per-symbol rolling baselines                 │
│  - deduplicate / cluster related signals                               │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │   ChannelSignal
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│                         Core Engine                                    │
│                                                                        │
│  1) Per-channel anomaly evaluator     (already done in the detector)   │
│  2) Cross-channel confirmation engine (fusion_engine)                  │
│  3) Risk-state engine                 (state_manager + decision_policy)│
│  4) Action recommendation engine      (decision_policy output)         │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │   FusedAnomalyEvent  /  DecisionRecord
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│                  Notification & Delivery (PRIMARY)                     │
│                                                                        │
│  - Email (Gmail SMTP)                                                  │
│      * different delivery policy per tier (see §6.5)                   │
│      * body shows fused score + per-channel scores + reason +          │
│        recommended action                                              │
│      * embeds external visual links (Polymarket / Hypurrscan /         │
│        TradingView / X / Truth Social direct links)                    │
│                                                                        │
│  - Telegram bot                                                        │
│      * EMERGENCY tier → instant mobile push                            │
│      * Same body as email, mobile-friendly formatting                  │
│                                                                        │
│  - Terminal / log (sink for development / debugging, NOT a user UI)    │
└────────────────────────────────────────────────────────────────────────┘
     (Streamlit dashboard is out of v1 scope — to be
          absorbed by v2 as part of the comprehensive trading advisor)
```

### 2.1 Component responsibility (per component)

| Component                       | Must do                                       | Must NOT do                          |
| :------------------------------ | :-------------------------------------------- | :----------------------------------- |
| `channels/<name>/collector`     | Talk to external API / WS, emit raw events     | Score / judge / persist signals      |
| `channels/<name>/normalizer`    | Raw payload → canonical event                  | Compute features                     |
| `channels/<name>/features`      | Maintain rolling stats, compute features       | Decide whether something is anomalous |
| `channels/<name>/detector`      | Apply channel-specific rules → `ChannelSignal` | Talk to other channels               |
| `core/registry`                 | Channel plugin discovery / registration        | Execution                            |
| `core/orchestrator`             | Channel lifecycle / scheduling / restart       | Trading decisions                    |
| `core/fusion_engine`            | Combine `ChannelSignal`s via noisy-OR → `FusedAnomalyEvent`. **Always preserve per-channel detail.** | Compress / discard per-channel info  |
| `core/decision_policy`          | Map fused score + state → recommended action   | Mutate state directly                |
| `core/state_manager`            | Apply hysteresis / dwell time / transitions    | Compute features                     |
| `storage/*`                     | Append-only persistence                        | Modify past records                  |
| `monitoring/*`                  | Observe health, collect metrics                | Trading decisions                    |
| `alerts/router`                 | Route by tier (state) → email / telegram      | Build alert bodies                   |
| `alerts/throttle`               | Cooldown / dedup / WATCH digest queue mgmt    | Decide tier                          |
| `alerts/renderer/*`             | Render `DecisionRecord` → email / telegram body | Decisions or storage mutation       |
| `alerts/link_builder`           | Per-channel signal → external visual URL       | Data transformation                  |
| `monitoring/health.py`          | Per-channel heartbeat / latency / sanity. Mark channels `HEALTHY` / `UNHEALTHY` | Decisions / score computation |

> v1 does **NOT** include a `services/advisory_bridge` (RAG ↔ anomaly) component.
> That arrives in v2's comprehensive trading advisor.

This strict separation is the key enabler for per-channel independent upgrades.

---

## 3. Process view (runtime)

V1's runtime spans **two deployment targets** (D12 LOCKED):

- **Google Cloud Run service** — anomaly daemon (always-on, 24/7)
- **Mac Air (developer laptop)** — writing code / reading logs / running the existing RAG Streamlit app

Reason for the split: the daemon must keep running while the laptop is asleep so
that pushes do not stop, and an on-demand laptop is enough for development.

```
┌──────────────────────────────────────────────────────┐  ┌─────────────────────────────────────┐
│  GOOGLE CLOUD RUN service "anomaly-daemon" (24/7)    │  │  MAC AIR  (dev laptop, on-demand)   │
│                                                      │  │                                     │
│  ┌─────────────────────────────────────────────┐     │  │  ┌─────────────────────────────┐    │
│  │                  orchestrator               │     │  │  │ Existing Streamlit RAG app  │    │
│  │       (asyncio loop, one task per channel)  │     │  │  │ (fully isolated from anomaly)│   │
│  └──┬──────────┬──────────┬──────────┬─────┬───┘     │  │  └─────────────────────────────┘    │
│     │          │          │          │     │         │  │                                     │
│     ▼          ▼          ▼          ▼     ▼         │  │  ┌─────────────────────────────┐    │
│ ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐ ┌────────┐    │  │  │ Cloud Run logs / metrics    │    │
│ │polymkt│ │hyperliq│ │ cme  │ │  x   │ │ truth  │    │  │  │  (gcloud / Cloud Console)   │    │
│ │ task │  │ task  │  │ task │  │ task │ │ social │    │  │  └─────────────────────────────┘    │
│ └───┬──┘  └───┬──┘  └───┬──┘  └───┬──┘ └────┬───┘    │  │                                     │
│     │        │         │         │         │         │  │  ┌─────────────────────────────┐    │
│     └────────┴────┬────┴─────────┴─────────┘         │  │  │ Code editor / git           │    │
│                   ▼                                  │  │  │  → push to repo             │    │
│         ┌─────────────────────┐                      │  │  │  → Cloud Build trigger      │    │
│         │    fusion_engine    │                      │  │  │  → Cloud Run redeploy       │    │
│         └──────────┬──────────┘                      │  │  └─────────────────────────────┘    │
│                    ▼                                 │  │                                     │
│         ┌─────────────────────┐                      │  └─────────────────────────────────────┘
│         │   decision_policy   │                      │
│         └──────────┬──────────┘                      │
│                    ▼                                 │
│         ┌─────────────────────┐                      │
│         │    state_manager    │                      │
│         └──────────┬──────────┘                      │
│                    │                                 │
│       (on state change / RISK_OFF / EMERGENCY)       │
│                    ▼                                 │
│         ┌─────────────────────┐                      │
│         │    alerts/router    │ ─ throttle / dedup   │
│         └──────────┬──────────┘                      │
│             ┌──────┴──────┐                          │
│             ▼             ▼                          │
│       ┌───────────┐ ┌───────────┐                    │
│       │   Email   │ │ Telegram  │                    │
│       │  (SMTP)   │ │    bot    │                    │
│       └─────┬─────┘ └─────┬─────┘                    │
│             │             │                          │
│             ▼             ▼                          │
│       ┌─────────────────────────┐                    │
│       │  Google Cloud Logging   │ ◄ stdout / log     │
│       └─────────────────────────┘                    │
└─────────────────┬─────────────┬──────────────────────┘
                  │             │
                  ▼             ▼
          ┌─────────────────────────────┐
          │  user's phone / mailbox     │  ◄── PRIMARY UX
          └─────────────────────────────┘

(The Cloud Run service additionally writes to persistent storage — audit & future dashboard)

       ┌──────────────────────────────────────────────────────────┐
       │  Cloud Run mounted volume  or  GCS bucket                │
       │   - SQLite (signals.db / decisions.db)                   │
       │   - Parquet (raw / features partitioned by channel/date) │
       └──────────────────────────────────────────────────────────┘
```

### 3.1 Why the daemon lives on Cloud Run (D12 LOCKED rationale)

- Anomaly detection is fundamentally an **always-on push system**.
  The user is going about their day and gets a **push to email / Telegram** when
  a signal fires. They do not need to keep a dashboard open and stare at it.
- That is why v1's user interface is **the push notification itself**, not Streamlit
  (see §6.5). The daemon is also responsible for delivery.
- **Running the daemon on a Mac Air would kill monitoring whenever the laptop sleeps.**
  Always-on is an inherent requirement → cloud-hosted is the natural choice.
- **Why Cloud Run** (vs. a Compute Engine VM or GKE):
  - Only the container needs to be deployed; no OS to manage.
  - 5 WebSockets + light async logic = a small workload (memory 1–2 GB); one Cloud Run instance is enough.
  - Cost ≈ $30–50 / month for always-on 1 vCPU (idle billing must be on — `--min-instances=1`, `--no-cpu-throttling`).
  - Automatic restart, healthcheck, log integration are platform-managed.
- The existing RAG Streamlit app **stays on the Mac Air** — separate system. The
  user's existing query-and-answer UX does not change.
- If P9–P10 detection deep-dive introduces a GPU-requiring model, **split it out as a
  separate Cloud Run job or Vertex AI training/prediction job** and have the daemon
  call that endpoint (D12 LOCKED).

### 3.2 Inside the daemon

- One asyncio event loop, one task per channel.
- Each channel task has its own back-pressure queue: `collector → normalizer → features → detector`.
- Detectors push `ChannelSignal`s onto a shared queue consumed by the fusion engine.
- The fusion engine emits a (debounced) `FusedAnomalyEvent` only at change points worth persisting.
- **`AlertRouter` only performs cross-tag bookkeeping (state-change tracking)** — after the P11(a/d) lock, system-level email / Telegram / heartbeat delivery is fully disabled (`email_enabled=False`, `telegram_enabled=False`, no heartbeat task created).
- **`ChannelAlertDispatcher`** is invoked every fusion cycle (orchestrator §6.5 / §6.6 step). For each channel signal in the registry snapshot, it runs the cooldown decision → on pass, renders two timeline plots (1 h + 6 h) and sends email + (if EMERGENCY) Telegram. Cooldown state is updated once for email/Telegram together — both media see exactly the same set of alerts (§6.5).
- **`LiveTimelineBuffer`** — a 24-hour rolling buffer. The orchestrator pushes a snapshot every cycle; the dispatcher uses it as the data source when an alert is sent.

### 3.3 Inside the daemon — alert path visualization (post P11(a/d))

```
fusion cycle (every 5 s)
  │
  ├─ orchestrator._run_one_cycle()
  │     ├─ registry.snapshot_signals()    → {channel: ChannelSignal | None}
  │     ├─ fusion_engine + decision_policy + state_manager  (state_change tracking)
  │     ├─ timeline_buffer.append(snapshot)                 ← P11(a)
  │     │
  │     ├─ channel_dispatcher.maybe_dispatch(signals)       ← P11(a/d) ★ delivery entry point
  │     │     for each channel signal:
  │     │       cooldown.decide(sig, channel)   → emit / suppress
  │     │       if emit:
  │     │         render_email(sig, replay_result, plot_dir)   → 1h/6h PNG + HTML
  │     │         send_email(rendered, smtp_config)            → Gmail SMTP
  │     │         if dry_run:  dump_eml(rendered, plot_dir/.eml)
  │     │         if tier == EMERGENCY and telegram_config:
  │     │           render_channel_telegram(sig, plot_60m, plot_360m)
  │     │           send_channel_telegram(rendered, telegram_config)  → sendMediaGroup
  │     │           if dry_run:  dump_telegram_capture(.txt)
  │     │
  │     └─ alert_router.dispatch(...)        ← kept alive but the email/telegram send path is a no-op
  │           (cross-tag bookkeeping only — state-change tracking / digest queue accumulation)
  │
  └─ next cycle ...
```

---

## 4. Data view

### 4.1 Canonical types (logical, not yet Python code)

```
RawEvent
  channel:      "polymarket" | "hyperliquid" | "cme" | "x" | "truth_social"
  source:       "ws" | "rest" | "webhook" | "scrape" | "dune"
  symbol:       e.g. "BTC-PERP", "CL", "Yes:Iran-strike-by-Feb28",
                "@Lookonchain", "topic_slug:liberation_day"
  ts_source:    UTC datetime (timestamp the source provided)
  ts_ingest:    UTC datetime (when we received it)
  payload:      JSON blob (as-is from the source)

NormalizedEvent
  channel, symbol, ts_source, ts_ingest
  side:         "buy" | "sell" | "yes" | "no" | "long" | "short" | None
  size_usd:     float
  price:        float | None
  meta:         dict   # e.g. wallet, account_age, leverage

FeatureSnapshot
  channel, symbol, ts
  features:     dict[str, float]   # vol_zscore, prob_jump, ob_imbalance, ...
  baseline_ref: id of the baseline used (for reproducibility)

ChannelSignal
  channel, symbol, ts
  score:        float in [0, 1]
  tier:         "NORMAL" | "WATCH" | "RISK_OFF" | "EMERGENCY"   # ← the channel's own verdict
  direction:    "up" | "down" | "neutral"
  confidence:   float in [0, 1]
  features_ref: id of the FeatureSnapshot used
  fired_detectors: list[str]       # detectors that fired (intra-channel rules)
  reason_codes: list[str]          # e.g. ["VOL_SPIKE_8x", "OB_IMBAL_+0.7"]

FusedAnomalyEvent
  ts
  fused_score:        float in [0, 1]      # noisy-OR result (reference / audit only)
  state:              "NORMAL" | "WATCH" | "RISK_OFF" | "EMERGENCY"
  per_channel_scores: dict[channel -> float]   # always preserves all 5 channels
  per_channel_tiers:  dict[channel -> tier]    # each channel's own tier verdict
  per_channel_signal: dict[channel -> ChannelSignal id | None]
  tier_floor:         tier   # max(per_channel_tiers.values()) — primary input to system_state
  boost_applied:      str | None    # e.g. "WATCH→RISK_OFF (corroboration)"
  contributing:       list[ChannelSignal id]   # tiers > NORMAL
  agreeing_channels:  int                      # corroboration count
  agreeing_direction: "up" | "down" | None     # consensus direction (if any)
  weights:            dict[channel -> applied weight]
  rationale:          str

DecisionRecord
  ts
  fused_event_ref:    FusedAnomalyEvent id
  recommended_action: enum
  policy_version:     str
  notes:              str

  # delivery / notification
  state_change:       (prev_state, new_state)        # only state transitions are push candidates
  delivery_tier:      "none" | "digest" | "realtime" | "urgent"
  delivery_channels:  list["email", "telegram"]      # which media actually delivered
  cooldown_until:     UTC datetime | None            # prevent re-sending on the same symbol
  external_links:     dict[channel -> url]           # visual links embedded in the email/telegram body
```

A separate reference doc (`docs/anomaly-data-contracts.md`) will lock these as Pydantic models in P1.

### 4.2 Storage layout

| Store            | Backend                                                       | Why                                           | Retention             |
| :--------------- | :------------------------------------------------------------ | :-------------------------------------------- | :-------------------- |
| `raw_store`      | Parquet under `data/anomaly_detection/raw/<channel>/<date>/`  | Cheap, high volume, never modified            | 7 days rolling        |
| `feature_store`  | Parquet partitioned by channel / symbol / date                | Time-series friendly, replay-capable          | 30 days rolling       |
| `signal_store`   | SQLite `signals.db`                                           | Small rows; easy audit / replay via CLI / SQL | Indefinite            |
| `decision_store` | SQLite `decisions.db`                                         | Audit trail                                   | Indefinite            |

**Persistence on Cloud Run (D12 LOCKED implications)**:

The local filesystem inside a Cloud Run container is **ephemeral** — wiped on
restart. So the paths above must be mounted onto a persistent backend. Options we
evaluated:

| Option                                               | Pros                                              | Cons                                           |
| :--------------------------------------------------- | :----------------------------------------------- | :--------------------------------------------- |
| **GCS bucket via FUSE mount**                        | Persistence is automatic, cheap, accessible via gsutil | Slight latency increase; possible SQLite write lock issues |
| **Cloud Run mounted volume** (2nd-gen execution env) | Native-filesystem speed, safe for SQLite          | Beta; limited to 1 instance (fine for a single daemon) |
| **Cloud SQL (Postgres)**                             | Managed DB, multi-instance OK                     | Higher cost; schema migration needed (SQLite → Postgres) |

**LOCKED decision** (paired with D12):

- **SQLite (`signal_store`, `decision_store`)** → **Cloud Run 2nd-gen mounted volume**. Small row size + write-lock safety matter most. v1 is a single-instance daemon, so the 1-instance limit does not apply.
- **Parquet (`raw_store`, `feature_store`)** → **GCS bucket via FUSE mount**. Bulky append-only writes; cost matters most. Unlike SQLite there are no lock issues (writes are file-level).
- **Cloud SQL is reconsidered in v2** — when multi-instance / sharing with the RAG advisor becomes necessary. v1 would be over-engineering.

The P8 deploy phase **implements the decisions above as-is rather than validating /
confirming them**. Only revisit if implementation surfaces an unexpected blocker
(for example, SQLite incompatibility with FUSE-mounted files).

### 4.3 Time / clock

- Every timestamp is stored as **UTC** ISO-8601 with microsecond precision.
- Each event keeps **both `ts_source` and `ts_ingest`**.
- Detection logic always orders by `ts_source`; `ts_ingest - ts_source` is reserved for latency monitoring.

---

## 5. Behavioral view

### 5.1 End-to-end happy path

What happens when a single trade arrives from an external source:

```
External source (e.g. Polymarket WS)
        │
        │  trade / book delta
        ▼
   collector
        │
        ├──► raw_store               (append RawEvent)
        │
        ▼
   normalizer
        │
        ▼
   features
        │
        ├──► feature_store           (append FeatureSnapshot)
        │
        ▼
   detector
        │
        ├──► signal_store            (append ChannelSignal)
        │
        ▼
   fusion_engine        (wait until N channels have a current signal,
        │                or until the time window elapses)
        │
        ▼
   decision_policy
        │
        ▼
   state_manager        (apply hysteresis / dwell time)
        │
        ├──► decision_store          (write DecisionRecord only on state change)
        │
        ▼
   alerts/router        (state-change-only + cooldown + WATCH digest queue)
        │
        ├──► alerts/throttle         (apply per-symbol cooldown)
        │
        ▼
   alerts/renderer/email   ──► Gmail SMTP   ──► user mailbox    ◄── PRIMARY UX
   alerts/renderer/telegram ──► Telegram bot ──► user's phone (EMERGENCY only)
```

### 5.2 Fusion math (combining 5 channels into 1 score)

The core principle is to **never kill a single strong signal while still rewarding
corroboration**. A plain weighted average cannot do this.

#### Why a plain weighted average fails

Two hypothetical cases:

```
                    Polymarket   Hyperliquid   CME    X     Truth  Average    Noisy-OR
Case 1 ("scream")     1.0          0.0        0.0    0.0    0.0    0.200      1.000
Case 2 ("hum")        0.3          0.3        0.3    0.3    0.3    0.300      0.832
```

Intuitively, Case 1 (one source screaming at full strength) is **more suspicious**
than Case 2 (everyone mumbling weakly).
A simple average flags Case 2 as more dangerous (0.30 > 0.20). That throws away the
real risk.

#### v1 default: Noisy-OR + corroboration tier guard

Treat each `ChannelSignal.score` as "the probability of independent anomaly evidence"
and OR them probabilistically:

```
fused_score = 1 - Π (1 - score_i)        # i ∈ {polymarket, hyperliquid, cme, x, truth_social}
```

As the table shows:

- **A single strong signal is preserved** (Case 1 → 1.0).
- **Multiple weak signals also accumulate** into a meaningful score (Case 2 → 0.83).
- If all scores are 0, fused is 0 (no spurious signal).

#### Channel weight / health degrade

Each channel's effective score is discounted by health:

```
effective_score_i = score_i * weight_i * health_i

  weight_i  ∈ [0, 1]   # config-defined (e.g. X may be lower)
  health_i  ∈ [0, 1]   # 1.0 = healthy, 0 = WS down etc.
```

The noisy-OR uses `effective_score_i`. A dead channel automatically has weight 0
and does not pull `fused_score` down (= graceful degrade).

#### `fused_score` role change — demoted to reference / audit

`fused_score` from the noisy-OR is still computed every cycle and saved on every
record, but the **primary input to system_state is no longer `fused_score`** — it
is `per_channel_tiers` (see §5.4).

Why we changed this:

- Real insider trading patterns are **most often single-channel** (e.g. CME-only 6,200 contracts; Hyperliquid-only known whale large short).
- Hard-requiring "≥ N channels agree" buries such high-conviction single-channel signals at WATCH → no defensive action.
- So the right model is **each channel emits its own EMERGENCY verdict from its own domain, and the system also goes EMERGENCY** (§5.4 max-tier-wins rule).

What `fused_score` still does:

- **Audit / post-mortem analysis** — measure fused intensity continuously over the same event (states are discrete tiers and have lower resolution).
- **Auxiliary input to boost rules** — when multiple channels simultaneously sit at a medium tier, `fused_score` quantifies the corroboration strength (see §5.4 boost rule).
- **Email body display** — give the user a single-number intuition of overall intensity ("fused 0.94 now").

#### Score normalization (open gap, formally addressed in P9)

Noisy-OR mathematically assumes `score_i` carries "probability of independent
anomaly evidence" ([0, 1] probabilistic semantics). But v0 baseline detectors use
different units per channel — Polymarket / CME use z-scores, Hyperliquid uses OI
percentiles, X uses keyword count × credibility. **A normalization layer that maps
these consistently into [0, 1] is not explicitly specified in v1.** P2–P5 v0
detectors handle it with a step-wise mapping (e.g. `z<2→0.0, 2~3→0.4, 3~5→0.7, ≥5→0.95`) as a placeholder.

**Why is this low priority in v1?** After the D6 redesign, the primary input to
system_state moved to **channel-level tier**, and **the act of each channel
emitting tiers via its own domain rules is itself a normalization into a
cross-channel-comparable discrete scale.** In other words, tier-level
normalization is already implicit. `fused_score` is now for audit + boost
auxiliary + display, so even if its distribution does not carry precise
probabilistic semantics, system behavior is unaffected.

**Options for P9** (consistent [0, 1] mapping per channel):
- Empirical CDF normalization (historical percentile rank)
- Probability calibration (logistic regression on labeled events)
- Sigmoid squashing (`sigmoid(α × (raw − threshold))`, α tuned in P10)
- Or just declare "tier handles normalization" and keep score reference-only.

During P1–P8 walking skeleton work, leave the step-wise mapping in place and just verify plumbing.

#### Per-channel visibility (hard requirement)

`FusedAnomalyEvent` **always** preserves `per_channel_scores` + `per_channel_tiers`
for all 5 channels.
The email / Telegram body must show the per-channel tier + score breakdown **above**
the system state; there is no screen that shows just the aggregate number.
Users gain trust only when they can always see "where the signal is coming from and
at what tier" (§6.5.2 email anatomy).

---

### 5.3 Channel failure path

What happens when one external source dies. The system must **degrade gracefully**, not halt.

```
   collector ── X ──► WebSocket disconnect / 5xx / rate-limit
        │
        ▼
   health module
        │
        ├──► orchestrator            (mark the channel UNHEALTHY, trigger backoff + reconnect)
        │
        ├──► fusion_engine           (drop that channel's weight to 0 and keep producing fused signals with the rest)
        │
        └──► alerts                  ("Channel X degraded" notification)
```

Important: even when one channel dies, **fusion keeps running** — the missing
source is treated as zero-weight, overall confidence drops, but the system as a
whole does not stop.

### 5.4 State transitions

The system has 4 states. State decisions follow a **two-stage structure**:

1. **Channel-level tier (stage 1)** — each channel emits its own tier from its domain rules (§5.4.1).
2. **System-level state (stage 2)** — combine channel tiers into the system state.
   **max-tier-wins** is the default, with an additional **corroboration boost** (§5.4.2).

Entry and exit thresholds differ (hysteresis), and a dwell time prevents
oscillation on 1-minute noise (§5.4.3).

#### 5.4.1 Channel-level tier (stage 1)

Each channel takes its detector-pool result and emits its own `tier`.
Detector types / thresholds differ between channels, but the **tier semantics are identical**:

| Channel tier  | Meaning (intra-channel)                                                            |
| :------------ | :--------------------------------------------------------------------------------- |
| `NORMAL`      | No detector fired, or only weak signals.                                           |
| `WATCH`       | One medium detector fired (e.g. vol z-score > 3).                                  |
| `RISK_OFF`    | Multiple medium detectors fired simultaneously OR one strong detector (e.g. vol z-score > 5 AND ob_imbalance > 0.7). |
| `EMERGENCY`   | High-conviction pattern (e.g. 6,000+ contracts per minute; a known whale wallet taking a large directional position; a 30 pp probability jump on Polymarket). |

Each channel owns its tier rules — i.e. "which detector combination = EMERGENCY?"
is defined by the channel itself.
Detailed detector rules + tier mappings will be defined per channel in **P9
(algorithm deep-dive)** and validated in **P10 (historical event replay)**.

Baseline during P1–P8 walking skeleton:

- v0 detector = just one vol z-score.
- Tier mapping = `z < 2 → NORMAL`, `2 ≤ z < 3 → WATCH`, `3 ≤ z < 5 → RISK_OFF`, `z ≥ 5 → EMERGENCY`.
- P9 expands the detector pool and refines the tier rules at the same time.

#### 5.4.2 System-level state (stage 2)

```
   ┌──────────────────────────────────────────────────┐
   │  Step 1.  tier_floor = max(per_channel_tiers)    │ ← max-tier-wins
   └────────────────────────┬─────────────────────────┘
                            │
                            ▼
   ┌───────────────────────────────────────────────────┐
   │  Step 2.  Boost rule (corroboration → +1 tier)    │
   │                                                   │
   │ - tier_floor = WATCH                              │
   │   AND ≥ 2 channels at WATCH+ (same direction)     │
   │   → boost to RISK_OFF                             │
   │                                                   │
   │ - tier_floor = RISK_OFF                           │
   │     AND ≥ 2 channels at RISK_OFF+ (same direction)│
   │   → boost to EMERGENCY                            │
   │                                                   │
   │ - tier_floor = EMERGENCY → unchanged (already top)│
   └────────────────────────┬──────────────────────────┘
                            │
                            ▼
                  decide candidate_state
                            │
                            ▼
          §5.4.3 hysteresis + dwell time gates passed
                            │
                            ▼
                  update system_state
```

Key points:

- **max-tier-wins**: if any single channel is at EMERGENCY tier, the system is at EMERGENCY. Single-channel patterns are the most common insider signature — they must not be buried.
- **Corroboration only boosts**: agreement from other channels nudges one tier up. EMERGENCY does not need a boost (already at the top).
- **Direction guard**: a boost only applies when the agreeing channels share direction (`up` / `down`). Opposite directions = different narratives, no boost.

Example scenarios:

| Scenario                                                     | per_channel_tiers                                          | tier_floor  | Boost applied?               | system_state | Note                                                         |
| :----------------------------------------------------------- | :--------------------------------------------------------- | :---------- | :--------------------------- | :----------- | :----------------------------------------------------------- |
| 2026.03.23 CME-only (6,200 contracts 16 min before)          | CME=EMERGENCY, others=NORMAL                               | EMERGENCY   | N/A (already top)            | EMERGENCY    | Single-channel high-conviction → straight to EMERGENCY.       |
| 2025.10.10 Hyperliquid-only (known whale large short)        | HL=EMERGENCY, others=NORMAL                                | EMERGENCY   | N/A                          | EMERGENCY    | Same — the single-channel pattern is not buried.              |
| Polymarket probability jump alone                            | PM=RISK_OFF, others=NORMAL                                 | RISK_OFF    | No other RISK_OFF → no boost | RISK_OFF     | Solo RISK_OFF stays RISK_OFF (no boost to EMERGENCY).         |
| Polymarket + Hyperliquid medium signals together (same direction) | PM=WATCH, HL=WATCH, others=NORMAL                          | WATCH       | 2 channels WATCH+ same dir → boost | RISK_OFF     | Corroboration boost lifts the system up by one tier.          |
| 4 channels all weak (each NORMAL or borderline WATCH)        | PM=WATCH, HL=NORMAL, CME=NORMAL, X=NORMAL                  | WATCH       | Corroboration not met        | WATCH        | A single weak signal stays at WATCH.                          |
| Opposite-direction conflict (PM=long whale, HL=short whale)  | PM=WATCH(up), HL=WATCH(down), others=NORMAL                | WATCH       | Direction differs → no boost | WATCH        | Different narratives — no boost.                              |
| Truth Social EMERGENCY (Trump posts tariff threat, others quiet) | TS=EMERGENCY, others=NORMAL                            | EMERGENCY   | N/A                          | EMERGENCY    | Truth Social posts can move markets alone — single-channel preserved. |

#### 5.4.3 Hysteresis + dwell time

We do not change state immediately after a decision is made: **escalate fast (the
stronger the tier, the faster), de-escalate slow.** Dwell times are asymmetric by
tier strength and prior state (tier-asymmetric):

**Escalation** — shorter dwell as the tier gets stronger or as the prior state is already high:

| Transition                            | Dwell time | Rationale                                                                                       |
| :----------------------------------- | :--------- | :----------------------------------------------------------------------------------------------- |
| `NORMAL → WATCH`                     | ≥ 30 s     | WATCH is a mild action (pause new entries). The threshold is weak → needs more noise filtering.   |
| `WATCH → RISK_OFF`                   | ≥ 30 s     | Already past WATCH — extra validation can be short.                                              |
| `RISK_OFF → EMERGENCY`               | ≥ 10 s     | Already at RISK_OFF and now even stronger = nearly certain. Escalate fast.                       |
| **Single-channel jump → EMERGENCY**  | ≥ 15 s     | Jumping from NORMAL or WATCH straight to EMERGENCY. The strong threshold itself is validation + a short dwell enables fast defense. |

**De-escalation** — always cautious; avoid relaxing too soon and re-entering danger:

| Transition                | Dwell time | Rationale                                                |
| :----------------------- | :--------- | :------------------------------------------------------- |
| `WATCH → NORMAL`         | ≥ 5 min    | Demote only after 5 consecutive minutes of candidate_state = NORMAL. |
| `RISK_OFF → WATCH`       | ≥ 5 min    | Do not relax on a transient recovery.                    |
| `EMERGENCY → RISK_OFF`   | ≥ 10 min   | Be most cautious when releasing EMERGENCY.               |

Core principles:

- **Already at a higher tier → shorter dwell** (trust is already accrued).
- **Strong threshold (EMERGENCY) → shorter dwell** (passing a strong threshold = a single sample is already trustworthy).
- **Weaker tier (WATCH) → longer dwell** (harder to distinguish from noise).
- **De-escalation is always cautious**: the cost of a false alert is smaller than the cost of a false sense of safety.

**Sample-count alternative**: dwell can be measured in "N consecutive samples"
instead of seconds (e.g. EMERGENCY entry = 3 consecutive samples). This carries
consistent semantics across channels that have different sample rates (Polymarket
WS is fast, X polling is 10 min). P9 will pick the best fit per channel.

**Extra de-escalation guard**: even during hysteresis-driven de-escalation, also
check `fused_score < threshold` to avoid releasing on a noisy spike (exact value
tuned in P10).

These dwell times / boost thresholds are all **v1 baseline placeholders**. In
**P10 (historical event replay)**, we inject ~10–15 candidate historical events
(2026.03.23 CME 6,200 contracts; 2025.10.10 Hyperliquid known whale large short;
2025.01.03 Polymarket Maduro-arrest betting; etc.) and measure two metrics:

- **Detection latency (direct — our system's performance)**: anomaly trading
  start ts → system alert sent ts. The primary metric under our control.
                                                  **Goal: median detection latency ≤ 60 s** (= alert within 1 minute of the anomaly starting). Achieved via simultaneous optimization of dwell time + threshold.
- **Warning time (informational only)**: alert sent ts → announcement (e.g. Trump
  announcement) ts. Depends on how many minutes before the announcement the insider
  traded — we cannot control this. With a 1-minute prior trade
  (2025.10.10 Hyperliquid) warning is necessarily short; with a 16-minute prior
  trade (2026.03.23 CME) it is long. Reported alongside system metrics for user
  value, not as a system evaluation.

During P1–P8 leave these placeholders in place and only validate plumbing.

### 5.5 Recommended action per state

| State       | Message the system shows the user                                                |
| :---------- | :----------------------------------------------------------------------------- |
| `NORMAL`    | Proceed as usual. Nothing to do.                                                |
| `WATCH`     | Pause new entries. Tighten stops. Wait for confirmation.                        |
| `RISK_OFF`  | Reduce leverage. Shrink directional bet sizing. Consider a partial hedge.        |
| `EMERGENCY` | Switch fully defensive. Close speculative positions or fully hedge.              |

> **Display rule (hard requirement):** every state message is always shown together with:
> (1) **per_channel_tiers** (tier of each of the 5 channels),
> (2) per_channel_scores (raw score of each channel),
> (3) **tier_floor** + **boost_applied** (how system_state was determined — single-channel max or corroboration boost),
> (4) fused_score (for reference),
> (5) each channel's fired_detectors + top reason_codes.
> No screen / email shows the system state number alone — the user must always be
> able to see "which channel pushed the system up to which tier".

---

## 6. Cross-cutting concerns

### 6.1 Configuration

- All anomaly settings live in `src/anomaly_detection/core/config.py` — **never mixed with the existing `src/market_summary/config.py`**. RAG tuning and anomaly tuning must remain independent.
- Per-channel overrides live inside each channel's folder (e.g. `channels/polymarket/config.py`).
- The D10 watchlist (symbols / accounts / keywords) is externalized to a separate YAML config → entries can be added / removed without touching code.

**Secret management** (D12 Cloud Run implications):

| Environment                | How secrets are loaded                                                |
| :------------------------ | :------------------------------------------------------------------- |
| **Local dev** (Mac Air)   | Existing `.env` + `python-dotenv` (same as RAG)                        |
| **Cloud Run** (production)| **Google Secret Manager** + Cloud Run secret-as-env-var binding via `--set-secrets`. Code still calls `os.environ.get(...)`. |

Secrets managed:
- `DATABENTO_API_KEY`, `DUNE_API_KEY`, `UNUSUAL_WHALES_API_KEY`
- `X_API_TOKEN` (added when EVT-1 swaps in the paid plan)
- `GMAIL_APP_PASSWORD`, `GMAIL_FROM_ADDRESS`, `GMAIL_TO_ADDRESS`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `OPENAI_API_KEY` (shared with the existing RAG; reused for X vision LLM if introduced via EVT-1)

Never hard-code these. Never commit `.env` files into the repo (`.gitignore` enforces this).

### 6.2 Logging

- Reuse the existing `src/market_summary/logging_utils.py` so the log format is consistent with the RAG side.
- Every log line carries `channel=...`, `symbol=...`, `ts_source=...`, `latency_ms=...` for easy filtering.

**Cloud Run logging integration** (D12):

- Cloud Run automatically collects everything written to stdout / stderr into **Google Cloud Logging**. No agent install required.
- So you only need Python's `logging.StreamHandler(stdout)` — no separate cloud SDK call.
- **JSON-formatted log lines** are recommended (`python-json-logger`): Cloud Logging auto-parses by field, making search / filter easy.
- From the Mac Air, watch in real time with `gcloud logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=anomaly-daemon"`. → **The Mac Air acts as a thin client for daemon logs.**
- Cloud Logging's default retention is 30 days. For longer, configure GCS export (P8).

### 6.3 Security / compliance

- **Read-only access** to every external source. **v1 has no order routing.**
- Where the platform allows, API keys are read-scope only.
- Never log PII / wallet → real-name mapping attempts. Wallets / addresses are kept as opaque IDs.
- X access is **D8 LOCKED**: v1 uses `snscrape` + 10-min polling + text-only HTML parsing (low frequency means near-zero risk of being blocked; no vision LLM). If blocked or coverage is insufficient, **EVT-1** adds X API Basic ($200 / month) or a vision LLM. ToS is always respected.
- Truth Social access uses **httpx polling of @realDonaldTrump every ~5 min** for live, plus **Wayback Machine archived Mastodon API JSON** for historical backfill. Both are public, anonymous endpoints. Conservative rate-limiting and exponential backoff are mandatory.

### 6.4 Channel sanity check (data-contract test)

Each channel auto-validates that its source is **doing its primary job correctly**.
This is the first line of defense before fusion.

| Dimension     | What it checks                                                                  | Where it runs                                                  |
| :----------- | :----------------------------------------------------------------------------- | :------------------------------------------------------------- |
| Right data   | Only the intended symbols / markets arrive; required fields are present         | Reject + count inside `channels/<name>/normalizer`              |
| Right timing | Live: `ts_ingest - ts_source` < the channel's SLA; batch: last successful ts < expected interval | `monitoring/health.py` per-channel heartbeat                    |
| Right format | Every record passes the `NormalizedEvent` schema (Pydantic validation)          | Strict at the channel boundary; quarantine to raw_store on violation |

On failure:

- **Per-record violation**: drop the record, store the raw payload under `data/anomaly_detection/raw/_quarantine/`, increment the error counter.
- **Threshold exceeded** (e.g. reject ratio > 5% in 1 min): mark the channel `UNHEALTHY`, fusion uses weight 0 — same graceful-degrade path as §6.5.

Additionally, at daemon startup, `channels/<name>/contract_test.py` runs once,
flowing the **last-known-good sample** through normalizer / feature / detector to
sanity-check that no schema has broken. A channel that fails contract test does
not start (fail-fast).

### 6.5 Notification & Delivery model (PRIMARY UX)

> v1's user interface is **the push notification itself**.
> Streamlit dashboard is out of v1 scope (see §10 v2 preview).

> **Model after P11(a/d) lock (locked 2026-04-21):**
>
> - Delivery is **per-channel** (1 signal = 1 email + 1 telegram on EMERGENCY).
> - Old **system-level fusion email / URGENT telegram / 1-hour heartbeat** are all **OFF** (alert fatigue prevention).
>   When the same event triggers in multiple channels, the user receives one alert per channel — they can immediately identify, in a single message, which channel emitted with what signal.
> - Cooldown is keyed on `(channel, symbol, tier)` × 24 h. Only tier escalation passes; demotes are silent.
> - Email + Telegram **share the same cooldown state** — one alert silences both media for 24 h.

#### 6.5.1 Per-tier delivery policy (post P11(a/d))

| Channel signal tier | Email (per-channel)              | Telegram (per-channel) | Subject / Caption prefix      |
| :------------------ | :------------------------------- | :--------------------- | :---------------------------- |
| `NORMAL`            | ❌ (cooldown.decide → normal_skip) | ❌                      | —                             |
| `WATCH`             | ✅ immediate                      | ❌ (`telegram_emergency_only=True`) | `📋` (the CME logo `📊` clashes; we use `📋`) |
| `RISK_OFF`          | ✅ immediate                      | ❌                      | `⚠️`                           |
| `EMERGENCY`         | ✅ immediate                      | ✅ immediate            | `🚨🚨`                         |

**Subject / Caption format (locked)**: `{tier_prefix} {channel_emoji} {CHANNEL_SHORT} · {symbol} → {TIER}`

Per-channel emoji + short name (locked — `alerts/subject_template.py` is the single source of truth):

| Channel        | Emoji  | Short      |
| :------------- | :----- | :--------- |
| `cme`          | 📊     | CME        |
| `polymarket`   | 🔷     | POLY       |
| `hyperliquid`  | 🟡     | HL         |
| `x`            | 𝕏      | (omitted — avoid duplicating with the emoji) |
| `truth_social` | 𝕋      | Truthsocial|

**Examples**:
- `🚨🚨 📊 CME · BZ → EMERGENCY`
- `⚠️ 🔷 POLY · trump-iran → RISK_OFF`
- `📋 𝕏 · @whalealert → WATCH`
- `🚨🚨 𝕋 Truthsocial · liberation_day → EMERGENCY`

> The WATCH digest queue (the old §6.5.3 daily 06:00 PT send) is fully disabled
> once `email_enabled=False` is locked. `flush_digest_now()` is a no-op. After
> P11(a/d), WATCH is delivered immediately too — protected from fatigue by the
> v2 24h cooldown.

#### 6.5.2 Email anatomy (per-channel — P11(a) lock)

**One email = one ChannelSignal.** Subject + brand logo + signal metadata + 1h timeline plot + 6h timeline plot + cooldown footer.

```
Subject:  🚨🚨 📊 CME · BZ → EMERGENCY

┌────────────────────────────────────────────────────────────┐
│  [📊 CME logo PNG inline (cid:logo)]   CME · BZ            │  ← 36x36 logo + 18px header
│                                                            │
│  TIER          EMERGENCY                                    │
│  SCORE         0.97                                         │
│  DIRECTION     ↑ up                                         │
│  TS (PT/UTC)   2026-04-21 12:00 PDT  (2026-04-21 19:00 UTC) │
│                                                            │
│  FIRED         vol_z_v1, price_jump_v1                     │
│  REASON        VOL_Z=4.21, PRICE_JUMP_PCT_1M=+0.85%, ...    │
├────────────────────────────────────────────────────────────┤
│  ── Last 1 hour ──                                          │
│  [inline plot_60m PNG — score / tier / fused state lane]   │
│                                                            │
│  ── Last 6 hours ──                                         │
│  [inline plot_360m PNG — same lanes, wider window]          │
├────────────────────────────────────────────────────────────┤
│  alert_id: a1b2c3d4e5f6  |  cooldown: initial · 24h lock    │
│  sent_at  : 2026-04-21 12:00 PDT                            │
└────────────────────────────────────────────────────────────┘
```

Core principles:

- One **subject line** identifies "which channel / which symbol / which tier" instantly. Visible at a glance from the inbox list.
- Body order = metadata block on top → two plots in the middle → cooldown footer at the bottom (natural mobile scroll).
- The two plots (1h + 6h) make it obvious whether this is "just happening" or "an hour-long progression".
- The cooldown label (`initial` / `escalation(prev=...)` / `cooldown_expired`) lets the user self-perceive fatigue.
- The brand logo is an inline `cid:` attachment (`assets/anomaly/channel_logos/{cme,polymarket,hyperliquid,x,truth_social}.png`).
- For `truth_social`, the metadata block additionally surfaces Trump's avatar + topic_slug + insider concern score parsed from the LLM assessment.

> **Telegram caption** uses the same subject header. The caption body compresses
> PT/UTC time + fired_detectors + score + reason_codes + cooldown label into ≤ 1024
> chars. The same 1h + 6h plots are attached via sendMediaGroup (reusing the email's
> PNGs — no second render).

#### 6.5.3 Cooldown / dedup rules (v2 — P10.5 lock)

Right after P10 validation, an alert-fatigue issue (iran_first_strike v1 = 278
alerts over 6 days) was found and the simple 60-min cooldown was replaced with a
5-tenet v2 rule. Total alerts across 6 events fell 445 → 95 (−79%).

- **Key**: `(channel, symbol, tier)` (was `channel` only in v1)
- **Duration**: **24 h** (locked — production daemon refuses anything below 1440 min)
- **Demote silent**: tier demotes on the same `(channel, symbol)` (`EMERGENCY → RISK_OFF`) are auto-silenced. No "downgrade" alert.
- **Escalation passes**: only tiers **higher than** the max-tier seen in the last 24 h get to emit immediately (e.g. WATCH → EMERGENCY passes within 24 h).
- **Cross-symbol corroboration preserved**: within the same channel, other symbols have independent cooldowns (e.g. CME BZ EMERGENCY does not silence CME ES EMERGENCY).
- **Email + Telegram share state**: `cooldown.decide` is called once per cycle. On pass, both media each fire once, then both are silent for 24 h. There is no partial silence (sending email but blocking telegram, etc.).
- **Natural reminder = `cooldown_expired`**: if the same `(channel, symbol, tier)` re-enters after 24 h, it emits with `reason='cooldown_expired'`. The Telegram caption auto-tags `"🔔 24h elapsed reminder (risk still in progress)"`. This mechanism replaces the pre-P11(d) 1-hour heartbeat reminder.

> Implementation: `alerts/cooldown.py` (`_decide_emit` pure function +
> `ChannelAlertCooldown` stateful wrapper). The production daemon
> (`channel_dispatcher`) and the replay reporter
> (`replay/reporters/channel_alerts.py`) both import the same function — replay
> and production produce identical results (validated in P10.5).

#### 6.5.4 Delivery guarantees

- Email / Telegram delivery failures are **isolated by try/except** — failing on one channel/medium never blocks another. Per-channel error counters (`email_errors`, `telegram_errors`) are exposed via `/snapshot` for ops health.
- **Email retry**: on SMTP send failure, v1 retries with backoff at most once (Gmail SMTP is generally stable). Cloud Run's retry policy will not re-fire the same cooldown.decide on the next turn (state has already been updated). The retry policy can be tightened later.
- **Audit dump (dry-run only)**: when `EMAIL_DRY_RUN=true` or `ANOMALY_DRY_RUN=true`, every alert auto-dumps `channel_alert.eml` (Gmail "Show original" compatible) + `channel_telegram.txt` (caption + plot path) + `plot_60m.png` + `plot_360m.png` into `data/anomaly_detection/alerts_live/{channel}_{symbol}_{tier}_{timestamp}/`. The operator does not need to open this normally; when something goes wrong, the .eml + .txt sit side-by-side and let you compare how each medium rendered the same alert.
- **Credentials** (per §6.1 policy):
  - SMTP: `SMTP_HOST` (default `smtp.gmail.com`) / `SMTP_PORT` (default `587`) / `SMTP_USER` / `SMTP_PASSWORD` (Gmail App Password) / `SMTP_FROM` / `SMTP_TO` (comma-separated list allowed)
  - Telegram: `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
  - Local dev = `.env`, Cloud Run = Secret Manager → env-var binding.
- **Auto dry-run decision**: missing creds → auto dry; present → real. `ANOMALY_DRY_RUN=true|false` overrides explicitly. When deploying to production, unset env vars = `dry_run=False` (auto-send when creds exist).

#### 6.5.5 Priority dilution prevention (post P11(a/d) lock)

**Problem scenario**: once an EMERGENCY has been sent and the 24h cooldown is in
effect, other channels / symbols at RISK_OFF / WATCH can pile up at the top of the
inbox and **visually bury the EMERGENCY that is still in progress**. Also, if the
same channel/symbol/EMERGENCY persists past 24 h, the user may forget about it.

After P11(a/d) we have a **3-layer defense**:

| # | Defense                                          | Behavior                                                                                              | Where it lives                                              |
| :- | :---------------------------------------------- | :---------------------------------------------------------------------------------------------------- | :--------------------------------------------------------- |
| 1 | **Telegram = EMERGENCY-only channel (per-channel)** | Only EMERGENCY tier goes to Telegram. Telegram is physically separated from the email inbox, so it cannot be buried by lower-tier alerts. The system-level URGENT push is also OFF — Telegram contains exactly one true EMERGENCY message. | `channel_dispatcher` (`telegram_emergency_only=True`)    |
| 2 | **Subject / Caption prefix + channel emoji**     | `🚨🚨 📊 CME · BZ → EMERGENCY` packs **tier prefix + channel emoji + symbol** into one line. The inbox lets you identify channel / symbol / severity at a glance. Email and Telegram headers match. | `subject_template.py` (single source of truth) + `renderer/channel_email.py` + `renderer/channel_telegram.py` |
| 3 | **24h cooldown_expired = natural reminder**     | If the same `(channel, symbol, EMERGENCY)` re-enters after 24 h, the same alert auto-fires again. The Telegram caption tags `"🔔 24h elapsed reminder (risk still in progress)"`. This naturally replaces the pre-P11(d) 1-hour heartbeat reminder — no separate task needed; while the channel signal is alive, the reminder is automatic. | `cooldown.py` (`_decide_emit` reason='cooldown_expired') + `channel_telegram._build_caption` |

> **Removed compared with the previous (v1) model:**
> - **Cross-tag** (`[🚨 EMERGENCY (sym) ACTIVE]` × N) — the system-level email itself is OFF, so there is nowhere to attach it. The user receives per-channel alerts where **information for one ChannelSignal is concentrated in one message**, instead of a per-event PER-CHANNEL BREAKDOWN.
> - **1h heartbeat task** (`_heartbeat_loop`) — the daemon no longer creates the task. The 24h `cooldown_expired` mechanism serves as a reminder (longer interval, but fatigue was the larger risk — user-locked decision).
> - **WATCH digest queue (daily 06:00 PT)** — the path is fully disabled with `email_enabled=False`. WATCH is also delivered immediately + protected by the 24h cooldown.

**Delivery timeline example** (BTC EMERGENCY persists 24h+, CL RISK_OFF arrives simultaneously):

```
T=14:00  🚨🚨 📊 CME · BZ → EMERGENCY      (push #1, initial)
                                            email + telegram each 1 message
T=14:30  ⚠️ 📊 CME · CL → RISK_OFF          (push #2, different symbol — independent cooldown)
                                            email only (RISK_OFF skips telegram)
T=15:00  (BZ EMERGENCY signal still alive → silent under 24h cooldown lock)
T=20:00  ⚠️ 🔷 POLY · trump-iran → RISK_OFF  (push #3, different channel / symbol)
                                            email only
T=14:00 (next day) 🚨🚨 📊 CME · BZ → EMERGENCY  (push #4, cooldown_expired = reminder)
                                            telegram caption: "🔔 24h elapsed reminder (risk still in progress)"
                                            email + telegram each 1 message
```

#### Acknowledgement mechanism (deliberately skipped in v1, v2 to consider)

The 24h `cooldown_expired` not stopping itself without an explicit user ack is
**by design, not an omission**. Rationale:

- The same `(channel, symbol, EMERGENCY)` is unlikely to persist 24h+ (when the channel signal's score drops, the channel naturally de-escalates to NORMAL / WATCH → `cooldown.decide` skips with normal_skip).
- If it really does persist 24h+, **continuing to get reminders is correct behavior** (persistent risk = persistent notification).
- The 24h interval is gentle on the user (24× less than the v1 1h heartbeat). After production observation, if fatigue becomes an issue again, we can tighten the cooldown duration (one rotation lever) or add an explicit ack mechanism.

**v2 trigger to review**: if the same EMERGENCY persists 7+ days in production
and daily reminders measurably annoy the user, add a Telegram bot `/ack <channel>
<symbol>` command + ack-state storage (a new table in `signal_store`). Ack
auto-clears on state transition / new EMERGENCY entry.

### 6.6 Failure mode / resilience

| Failure                              | Detection             | Response                                                                                  |
| :----------------------------------- | :-------------------- | :---------------------------------------------------------------------------------------- |
| Single channel WS dies                | Heartbeat miss        | Backoff + reconnect (D4 policy); fusion proceeds with weight-degraded channel.            |
| Source rate-limit                     | HTTP 429              | Exponential backoff; circuit breaker opens for 5 minutes after 10 failures (D4).          |
| Feature store write failure           | Storage error         | Drop oldest from the in-memory buffer; the collector must never block.                    |
| Daemon crash                         | Container exit        | **Cloud Run restarts automatically**; channels resume from the last known good baseline.  |
| Cloud Run instance forced replacement | Container termination | `--min-instances=1` + graceful shutdown handler flushes in-flight signals; new instance restores state from GCS / mounted volume. |
| Email / Telegram send failure         | SMTP / HTTP error     | Exponential backoff retry up to 3 times; every attempt is recorded in `decision_store` (audit). |
| Mac Air laptop sleep                  | n/a                   | **Zero impact** — the daemon lives on Cloud Run; the Mac Air is only an on-demand dev / monitoring environment. |

### 6.7 Cost tracking & kill-switch (D13)

Pay-as-you-go APIs (Databento, OpenAI vision, Cloud Run egress, Cloud Logging,
post-EVT-1 paid tools) carry a **runaway billing risk** — a bug, an API change,
or an infinite loop can spike spend. So **a total monthly cost cap + automatic
kill-switch** is the system's first-line safety device.

#### 6.7.1 Spec summary

> **Core principle**: the CSV records **every tool's cost** (full visibility).
> Alerts + kill-switch apply **only to the pay-as-you-go portion** (flat
> subscriptions are predictable — no runaway risk).

| Item                              | Value / behavior                                                                                          |
| :------------------------------- | :-------------------------------------------------------------------------------------------------------- |
| **Services recorded in the CSV**  | **All tools** — PAYG + flat subscription + free. Purpose: one place to see all costs                       |
| **PAYG (cap applies)**            | Databento, OpenAI API, Cloud Run egress, Cloud Logging — billed by usage                                  |
| **Subscription (cap doesn't apply)** | TradingView, Unusual Whales, Cloud Run base ($30–50/month always-on 1 vCPU), GCS storage, etc. — flat monthly. **X API Basic ($200/month)** is only activated in EVT-1 (D8) — v1 default uses free snscrape |
| **Free**                          | Telegram bot, Gmail SMTP, Polymarket API, Hyperliquid API, snscrape, Truth Social (httpx scrape) — recorded as $0 in the CSV (activity tracking) |
| **PAYG monthly cap**              | **$1,000** (sum across PAYG services only — subscriptions are excluded from the cap)                       |
| **Alert thresholds (PAYG only)**  | 6 cumulative levels: **10% / 20% / 40% / 60% / 80% / 100%** = $100 / $200 / $400 / $600 / $800 / $1,000     |
| **When a threshold fires**        | The first time we cross each threshold, send a **separate alert email** (once per threshold). The body shows (1) PAYG cumulative, (2) PAYG per-tool breakdown, (3) **subscription total + grand total for reference**, (4) remaining PAYG budget |
| **At 100%**                       | **Kill-switch fires** — force every PAYG service flag `enabled=false` → channels go `UNHEALTHY` → fusion sets weight 0 → graceful degrade. Send a RISK_OFF tier alert ("PAYG cap reached. Databento + OpenAI disabled."). **Subscription services are unaffected** (already paid, flat fee, keep running) |
| **Manual override**               | Config flags (`COST_PAYG_CAP_USD`, `COST_KILL_SWITCH_OVERRIDE`) can raise the cap or temporarily unblock. Overrides are recorded in the audit log |
| **Monthly reset**                  | On the 1st of every month at 00:00 UTC, the PAYG cumulative counter resets to 0. Subscriptions append one ledger row on the 1st (flat monthly). Last month's ledger is preserved (audit) |

#### 6.7.2 Implementation (`monitoring/cost_tracker.py`)

Every external API / service call must go through the cost_tracker wrapper —
regardless of PAYG / subscription / free (subscription and free use the
daily / monthly aggregate helpers instead of `record()`). Direct calls are not
allowed (enforced by P1 contract tests).

```
# Pseudocode
class CostTracker:
    def record(
        self,
        tool: str,
        operation: str,
        units: float,
        unit_price: float,
        type: str = "payg",           # "payg" | "subscription" | "free"
    ):
        cost_usd = units * unit_price
        # 1) Append one row to cost_ledger.csv (including the type column)
        # 2) Update monthly cumulative (in-memory + SQLite)
        #    - payg_cum_month: sum only type=='payg' → compared against the cap
        #    - total_cum_month: sum across all types → user's aggregated view
        # 3) [PAYG only] check threshold crossings → send alert email + record the last threshold fired
        # 4) [PAYG only] payg_cum_month >= cap → fire the kill-switch
        #    (set the config flag + broadcast disable to PAYG services; subscriptions are unaffected)
        return cost_usd

    def record_subscription_monthly(self, tool: str, plan: str, monthly_usd: float):
        # Called once on the 1st at 00:00 UTC each month (scheduler or daemon-startup idempotent check)
        self.record(tool, plan, units=1, unit_price=monthly_usd, type="subscription")

    def record_free_daily(self, tool: str, operation: str, daily_count: int):
        # Called once at 23:59:59 UTC each day (activity visibility; cost=0)
        self.record(tool, operation, units=daily_count, unit_price=0.0, type="free")
```

**Call example 1** — PAYG (inside the Databento client):

```
async def fetch_databento_data(symbol, start, end):
    response = await databento_client.range(symbol, start, end)
    cost_tracker.record(
        tool="databento",
        operation=f"historical_range:{symbol}",
        units=response.bytes_received / 1e9,    # GB
        unit_price=DATABENTO_GB_PRICE,
        type="payg",
    )
    return response
```

**Call example 2** — Subscription (monthly init at daemon startup; idempotent):

```
def init_monthly_subscriptions():
    # v1 default — always active
    cost_tracker.record_subscription_monthly("tradingview",     "premium_plan",         60.00)
    cost_tracker.record_subscription_monthly("unusual_whales",  "premium_plan",         80.00)
    cost_tracker.record_subscription_monthly("cloud_run_base",  "always_on_1vcpu_1gb",  40.00)

    # Only when X API Basic is enabled via EVT-1 (D8 — v1 default uses free snscrape and never calls this)
    if config.X_API_BASIC_ENABLED:
        cost_tracker.record_subscription_monthly("x_api_basic", "monthly_basic_plan",  200.00)
```

**Call example 3** — Free (daily activity flush):

```
def flush_free_daily_counters():
    cost_tracker.record_free_daily("telegram_bot",    "send_count",  telegram_send_count_today)
    cost_tracker.record_free_daily("polymarket_api",  "call_count",  polymarket_call_count_today)
    cost_tracker.record_free_daily("hyperliquid_api", "call_count",  hyperliquid_call_count_today)
    cost_tracker.record_free_daily("snscrape",        "post_count",  snscrape_fetch_count_today)
    cost_tracker.record_free_daily("truth_social",    "post_count",  truth_social_fetch_count_today)
```

#### 6.7.3 Two CSVs (audit + browsable by the user)

Location: `data/anomaly_detection/cost/` (on Cloud Run, on top of the GCS FUSE mount — same as §4.2).

**All tools are logged** — pay-as-you-go (`type=payg`), flat subscription
(`type=subscription`, one row on the 1st of each month), free (`type=free`, $0).
The `type` column distinguishes them.

**(1) `cost_ledger.csv`** — append-only. PAYG = one row per call; subscriptions = one row on the 1st of each month; free = one row per day (cost=0) for activity tracking:

```
date,                tool,            type,         operation,                       units,  unit_price, cost_usd, payg_cum_month, total_cum_month
2026-04-01T00:00:00, x_api_basic,     subscription, monthly_basic_plan,              1,      200.00,     200.00,   0.00,           200.00
2026-04-01T00:00:00, tradingview,     subscription, premium_plan,                    1,      60.00,      60.00,    0.00,           260.00
2026-04-01T00:00:00, unusual_whales,  subscription, premium_plan,                    1,      80.00,      80.00,    0.00,           340.00
2026-04-01T00:00:00, cloud_run_base,  subscription, always_on_1vcpu_1gb,             1,      40.00,      40.00,    0.00,           380.00
2026-04-13T08:01:23, databento,       payg,         historical_range:CL,             0.42,   1.20,       0.504,    142.31,         522.31
2026-04-13T08:01:24, openai_api,      payg,         vision_call:gpt-4o:x_post_img,   1500,   0.000010,   0.015,    142.32,         522.33
2026-04-13T08:02:01, cloud_run,       payg,         egress:gcs_read,                 0.08,   0.12,       0.0096,   142.33,         522.34
2026-04-13T23:59:59, telegram_bot,    free,         daily_send_count,                42,     0.0,        0.0,      142.33,         522.34
2026-04-13T23:59:59, polymarket_api,  free,         daily_call_count,                8421,   0.0,        0.0,      142.33,         522.34
...
```

Key columns:
- **`type`** — `payg` / `subscription` / `free`. Only `type='payg'` contributes to the cap.
- **`payg_cum_month`** — PAYG cumulative only (compared with the $1,000 cap).
- **`total_cum_month`** — sum across all types (user's aggregate view).

**(2) `cost_summary_<YYYY-MM>.csv`** — daily aggregate per tool, updated at 00:00 UTC each day:

```
date,         type_payg_databento,  type_payg_openai,  type_payg_cloud_run,  type_payg_cloud_logging,  type_sub_x_api,  type_sub_tradingview,  type_sub_uw,  type_sub_cloud_run_base,  type_free_telegram_calls,  type_free_polymarket_calls,  payg_daily,  sub_daily,  free_daily,  daily_total,  payg_cum_month,  sub_cum_month,  total_cum_month
2026-04-01,   14.76,                 0.42,              0.408,                0.05,                     200.00,          60.00,                 80.00,        40.00,                    52,                        7821,                        15.638,      380.00,     0,           395.638,      15.638,          380.00,         395.638
2026-04-02,   9.72,                  0.38,              0.372,                0.04,                     0,               0,                     0,            0,                        48,                        7912,                        10.512,      0,          0,           10.512,       26.150,          380.00,         406.150
...
2026-04-13,   ...
```

Key columns:
- Each tool is prefixed by type (`type_payg_*`, `type_sub_*`, `type_free_*`).
- **`payg_daily / payg_cum_month`** ← compared with the cap; alert / kill-switch trigger input.
- **`sub_daily / sub_cum_month`** ← flat (most values appear only on day 1).
- **`total_cum_month`** ← aggregate view (real total spend for the month).

Easy to open by hand (Excel / Numbers / `column -s, -t`, etc.).

#### 6.7.4 Alert email body (on threshold cross)

> **Threshold compares**: only PAYG cumulative (subscriptions are flat and
> excluded). However, the body **also shows the aggregate view** so that you
> immediately see what was actually spent this month.

```
Subject:  💰 [COST 60%] PAYG spend reached $600 / $1,000 (April 2026)

────────────────────────────────────────────────────────
PAYG CUMULATIVE     $600.42 / $1,000  (60%, 18 days into month)   ← compared with the cap
PROJECTED END-MONTH $1,034 (extrapolated linearly — over cap!)
NEXT THRESHOLD      80% = $800
KILL-SWITCH AT      $1,000 (PAYG only — subscriptions unaffected)

────────────────────────────────────────────────────────
PAYG PER-TOOL BREAKDOWN
  databento       $480.21   (80% of PAYG)   ← dominant
  openai_api      $108.50   (18%)
  cloud_run        $11.71   (2%)            ← egress
  cloud_logging     $0.00   (0%)

────────────────────────────────────────────────────────
SUBSCRIPTION (flat, cap not applied — reference)
  x_api_basic       $200.00 / month
  unusual_whales     $80.00 / month
  tradingview        $60.00 / month
  cloud_run_base     $40.00 / month
  ─────────────────────────────────
  Subscription total $380.00 / month

────────────────────────────────────────────────────────
GRAND TOTAL THIS MONTH (PAYG + subscription)
  $980.42  (PAYG $600.42 + sub $380.00)

────────────────────────────────────────────────────────
RECOMMENDED ACTION
  - At the current PAYG run rate, $1,000 cap is reached around day 23 → kill-switch fires.
  - Consider shrinking the Databento symbol set or historical range.
  - OR raise COST_PAYG_CAP_USD via config (override is audit-logged).

────────────────────────────────────────────────────────
SEE FULL LEDGER:   data/anomaly_detection/cost/cost_ledger.csv
SEE DAILY SUMMARY: data/anomaly_detection/cost/cost_summary_2026-04.csv
```

#### 6.7.5 System behavior when the kill-switch fires

1. `cost_tracker` detects **PAYG cumulative** ≥ $1,000 → set the global flag `payg_services.enabled = false` (both env and SQLite). Subscription services are unaffected.
2. The next PAYG API call hits the wrapper, which raises `RuntimeError("PAYG kill-switch active")` → the collector catches it via try/except and marks the channel `UNHEALTHY` (only PAYG-dependent channels — e.g. CME (Databento), X EVT-1 vision LLM).
3. `health.py` detects the UNHEALTHY channel → broadcasts weight=0 to `fusion_engine` → that channel no longer contributes to the fused signal. **Subscription / free-based channels** (Polymarket, Hyperliquid, X snscrape, Truth Social, CME options Unusual Whales) keep running → graceful degrade.
4. `alerts/router` sends a RISK_OFF tier alert: "PAYG cap reached. Databento + OpenAI disabled. Subscription channels still active. Manual review required."
5. After the user edits the config (`COST_PAYG_CAP_USD` higher or `COST_KILL_SWITCH_OVERRIDE=true`) and restarts the daemon, the PAYG services come back online.

This entire process is **automatic, no user intervention required** — even in a
runaway-billing event, maximum PAYG loss is capped at $1,000 + a little overhead.
Subscriptions are flat and unaffected.

---

## 7. Non-functional requirements

| NFR                                                                | Target (v1)                                          | Note                                                |
| :----------------------------------------------------------------- | :--------------------------------------------------- | :-------------------------------------------------- |
| End-to-end latency (source event → ChannelSignal persisted)        | WS-based channels < 2 s P95                          | Dune (batch) excluded                               |
| Fusion latency (last ChannelSignal → DecisionRecord)               | < 500 ms P95                                         | Pure arithmetic (noisy-OR product + max + boost rule). GPU / network independent. Even if a detector (P9) uses a GPU, that is outside the fusion measurement scope |
| Push delivery latency (DecisionRecord → user's **first** receipt)   | EMERGENCY < 10 s P95 (Telegram-first), RISK_OFF < 60 s P95 (Gmail SMTP) | EMERGENCY sends Telegram + Email simultaneously → users typically receive Telegram first (usually < 3 s). Gmail SMTP averages 5–30 s with occasional outliers |
| Per-channel uptime (Cloud Run 24h test)                            | ≥ 99 % (≤ 7 h 12 m down/month)                       | Accounts for external source instability (WS disconnect, rate-limit, X blocking); planned restarts excluded; P8 exit criterion |
| Daemon uptime (Cloud Run service)                                  | ≥ 99.5 % (≤ 3 h 36 m down/month)                     | Assumes `--min-instances=1` + healthchecks; allows for Cloud Run instance replacement / deploy time |
| Storage footprint                                                   | < 5 GB / week with the default symbol set            | Tunable via retention                               |
| Cloud Run cost                                                      | < $50 / month (1 vCPU + 1 GB always-on, idle billing) | EVT-1 paid data needs a separate budget             |
| External data cost ceiling (D13 LOCKED)                             | **PAYG monthly cap = $1,000** (sum over pay-as-you-go services only. Subscriptions are not capped) | 6-step threshold alert + auto kill-switch at 100% (PAYG services disable, subscriptions unaffected). The CSV records PAYG + subscription + free (aggregate view). Full spec: §6.7 |
| Reproducibility                                                     | Same RawEvent history → same fused output             | Required for backtesting; precondition for P10 validation |

---

## 8. Core design decisions (all LOCKED ✅)

D1 through D13 are settled. P1 (skeleton) can begin. D5 / D6 algorithms are
locked, but parameters / thresholds will be tuned in **P9 deep-dive + P10
historical replay**.

| #   | Topic                                            | Decision                                                                        | Status |
| :-- | :----------------------------------------------- | :------------------------------------------------------------------------------ | :----- |
| D1  | Runtime model (process)                          | **A single "market signal" daemon** (5 channels = asyncio tasks). v2 adds more daemons per source domain. | LOCKED |
| D2  | Async runtime                                    | **A single asyncio event loop** inside the daemon                                | LOCKED |
| D3  | Persistence                                      | **SQLite (signal / decision) + Parquet (raw / feature)**. ChromaDB shows up in v2 RAG | LOCKED |
| D4  | Channel restart policy                           | **Exponential backoff up to 60 s + 10 failures → 5 min circuit break** (`tenacity`) | LOCKED |
| D5  | Fusion math                                      | **Noisy-OR (`1 − Π(1 − score_i × weight_i × health_i)`)** still computes `fused_score`. But `fused_score` is for **reference / audit / boost-aux**; the primary input to system_state is **per_channel_tiers** (see D6). Details in §5.2. **Algorithm is locked**; parameters (weights, etc.) tuned in P9–P10 | LOCKED (algo) / TUNE in P9~P10 |
| D6  | State definition (channel tier + system state)   | **Two-stage structure (§5.4)**: (1) **Channel-level tier** — each channel emits its own `NORMAL/WATCH/RISK_OFF/EMERGENCY` from its detector pool. (2) **System-level state** — `tier_floor = max(per_channel_tiers)` (max-tier-wins) + corroboration boost (≥ 2 channels at WATCH+ same direction → +1 tier; EMERGENCY needs no boost). Goal: prevent single-channel high-conviction signals (e.g. CME-only 6,200 contracts) from being buried. Per-channel detector → tier rules **defined in P9 deep-dive**, thresholds **tuned by P10 replay** | LOCKED (structure) / TUNE in P9~P10 |
| D7  | CME data: free → paid                            | **P4: start historical-only. EVT-1: enable live stream** (config flag toggle)    | LOCKED |
| D8  | X & Truth Social access method                   | **P5: snscrape + 10-min polling for X (free, text-only HTML parsing)** + **Truth Social httpx polling of @realDonaldTrump every ~5 min + Wayback Mastodon API JSON for backfill**. If EVT-1 finds X stability / coverage insufficient, add X API Basic ($200 / month) or a vision LLM. During v1 validation no image vision LLM is used | LOCKED |
| D9  | Will the RAG advisor consume the anomaly state in v1? | **No.** v1 is push-first; the anomaly state is fully isolated from the RAG. Integrated in v2 | LOCKED |
| D10 | v1 symbols universe                              | Polymarket: top-N by liquidity + keyword watchlist (war / strike / oil / Iran / Hormuz / China / Fed / recession / tariff). Hyperliquid: BTC/ETH/SOL-PERP. CME futures: CL/BZ/ES/NG. CME options (UW): SPY/QQQ/USO/CL. X: 5 named accounts. Truth Social: @realDonaldTrump (Stage-1 keyword filter + Stage-2 LLM topic_slug classification). **All editable any time via watchlist YAML config** | LOCKED |
| D11 | Notification & delivery model                    | **Email (Gmail SMTP) + Telegram bot**. Detailed policy in §6.5. WATCH digest at 06:00 PT, EMERGENCY = email + telegram, other tiers split by tier, state-change-only + 5-min cooldown, no quiet hours | LOCKED |
| D12 | Deployment target                                | **v1 daemon (5 channels + fusion + alerts) = a single Google Cloud Run service** (always-on, `--min-instances=1`, `--no-cpu-throttling`). **Mac Air = development + log / dashboard viewing only** (sleep-irrelevant). If a detection model needs a GPU in P9–P10, **split into a separate Cloud Run job or Vertex AI** (daemon calls that endpoint). Detail impact: §3 process view, §4.2 storage, §6.1 secrets, §6.2 logging, §6.6 failure mode | LOCKED |
| D13 | **Cost ceiling & kill-switch** (PAYG safety device) | **PAYG monthly cap = $1,000** (sum of Databento + OpenAI + Cloud Run egress + Cloud Logging only). Subscriptions (TradingView, Unusual Whales, X API Basic, Cloud Run base, etc.) are cap-excluded — flat fee, no runaway risk. **BUT the CSV logs PAYG + subscription + free** (purpose: aggregated user view, distinguished by the `type` column). 6 cumulative thresholds (10/20/40/60/80/100%, **PAYG only**) each send an alert email (body shows PAYG breakdown + subscription reference + grand total). At 100% PAYG services auto-disable → relevant channels go UNHEALTHY → fusion gracefully degrades. Subscription channels are unaffected. Two CSVs (`cost_ledger.csv` per-call/monthly/daily append + `cost_summary_<YYYY-MM>.csv` daily aggregate, in `data/anomaly_detection/cost/`). Implementation: P1 scaffold → P4 Databento wiring → P8 24h measurement + kill-switch dry-run. Full spec: §6.7 | LOCKED |

---

## 9. Glossary

- **Channel** — a code module owning one external data source.
- **ChannelSignal** — a channel's verdict at a moment in time (score + tier + direction + reason).
- **Channel tier** — the 4-level tier (`NORMAL` / `WATCH` / `RISK_OFF` / `EMERGENCY`) a channel emits from its detector pool. Detectors and thresholds vary per channel, but tier semantics are identical.
- **FusedAnomalyEvent** — the single combined verdict across all channels.
- **fused_score** — the [0, 1] continuous value combining channel scores via noisy-OR (`1 − Π(1 − score_i × weight_i × health_i)`). **NOT the primary input to system_state** (that is the max of per_channel_tiers). Three current roles: (1) audit / post-mortem (states are discrete tiers with low resolution; `fused_score` is continuous and allows fine comparison), (2) auxiliary input to §5.4.2 boost rule, (3) shown as a single overall-intensity number in the email body. Details in §5.2.
- **per_channel_tiers** — dict of each channel's channel tier. **Primary input to system_state.**
- **tier_floor** — `max(per_channel_tiers.values())` — result of the max-tier-wins rule. Any single EMERGENCY channel → tier_floor = EMERGENCY.
- **State / system_state** — the system's current defensive mode (`NORMAL` / `WATCH` / `RISK_OFF` / `EMERGENCY`). The "how defensive should we be right now?" level. Determined after tier_floor + corroboration boost + hysteresis / dwell time (§5.4).
- **Corroboration boost** — when tier_floor is WATCH or RISK_OFF, if another channel agrees with the same direction at the same tier or higher (≥ 2 channels), boost up one tier. EMERGENCY is already maxed.
- **Decision** — the recommended user action attached to a state change.
- **Hysteresis** — using **different** entry and exit thresholds for a state to prevent **oscillation** (state rapidly toggling near the boundary). e.g. enter WATCH at score > 0.40, exit at score < 0.30 → between 0.30 and 0.40, state is held.
- **Dwell time** — a time guard requiring candidate_state to remain stable for N seconds/minutes before updating system_state. Asymmetric: short on the way up (10–30s), long on the way down (5–10min) (§5.4.3).
- **Detection latency** — anomaly trading start ts → system alert sent ts. The **primary metric** for system performance. P10 goal: median ≤ 60 s.
- **Warning time** — alert sent ts → Trump announcement ts. Depends on how many minutes before the announcement the insider traded (we cannot control this). Reported alongside system metrics for user value, not as a system evaluation.
- **Cost ceiling** — monthly spend cap on pay-as-you-go (PAYG) services. v1 = $1,000 / month (PAYG only — subscription flat fees are excluded). Six threshold alerts before hitting it (10/20/40/60/80/100%), kill-switch at 100% (D13, §6.7).
- **Kill-switch** — automatic disable of PAYG services when the cost ceiling hits 100%. Affected channels go UNHEALTHY and are weighted 0 by fusion → graceful degrade. Subscription channels are unaffected (already paid, flat fee). Stops runaway billing without user intervention.
- **PAYG vs subscription** — Cost-tracking classification. **PAYG** (pay-as-you-go): usage-based billing (Databento $/GB, OpenAI $/token, Cloud Run egress $/GB, etc.) → cap-applied + alerted. **Subscription**: flat monthly (X API Basic $200, TradingView, Unusual Whales, Cloud Run base, etc.) → not capped. Both are stored in the CSV (`type` column) for an aggregate view.

---

## 10. Change log

| Date    | Author                | Change                                                                                              |
| :------ | :-------------------- | :-------------------------------------------------------------------------------------------------- |
| _today_ | ChangYeong + AI pair  | Initial v1 draft for alignment.                                                                     |
| _today_ | ChangYeong + AI pair  | Replaced Mermaid diagrams with plain ASCII for raw-text readability.                                |
| _today_ | ChangYeong + AI pair  | Rewrote the 100% English doc as a English-first conversational tone (technical terms stay English).  |
| _today_ | ChangYeong + AI pair  | Switched fusion math from weighted sum → noisy-OR (added §5.2). Per-channel visibility is now a hard requirement (§2 logical view, §2.1 component table, §4.1 schema, §5.5). D5 default updated. |
| _today_ | ChangYeong + AI pair  | Added §6.4: Channel sanity check (data-contract test) — auto-validate right data / right timing / right format + startup contract test. Renumbered the old §6.4 → §6.5. |
| _today_ | ChangYeong + AI pair  | **Switched the delivery model to push-first.** Streamlit dashboard removed from v1 scope, moved to v2. §2 output box, §3 process view, §3.1 rationale, §4.1 `DecisionRecord` schema, new §6.5 (Notification & Delivery model — tier policy, email anatomy, throttle, Gmail SMTP + Telegram bot), §6.5 component table, §8 D9 LOCKED + new D11 LOCKED. Renumbered the old §6.5 → §6.6. |
| _today_ | ChangYeong + AI pair  | **D1–D11 all LOCKED.** §6.3 X access policy aligned with D8 lock (snscrape 5–10 min polling, text-only HTML parsing; vision LLM reviewed in P9). Cleaned up the §8 table: D1/D2/D3/D4/D7/D8/D10 defaults → confirmed decisions; D5/D6 = algorithm lock + parameters tuned in P6. §8 heading changed from "open question" → "all LOCKED". |
| _today_ | ChangYeong + AI pair  | **Consistency sweep — bits not yet reflecting LOCKED decisions.** (1) §0 TOC: "Decisions / open questions" → "Design decisions (all LOCKED)". (2) §4.2 `signal_store` justification: "easy to query from the UI" (old Streamlit-dashboard era wording) → "easy to audit / replay via CLI / SQL" — v1 has no Streamlit. (3) §6.1 secret list: `OPENAI_API_KEY` comment "P9 vision LLM" → "EVT-1 X vision LLM" (matches D8 LOCKED). (4) §6.5.4 delivery guarantee: SMTP / Telegram credential loading mentioned only `.env` → unified to reference §6.1 secret-management policy (Local dev = .env, Cloud Run = Secret Manager). |
| _today_ | ChangYeong + AI pair  | **§4.2 storage backend LOCKED.** Aligned with D12, raising the "default proposal" → confirmed decision. SQLite (signal / decision store) → Cloud Run 2nd-gen mounted volume; Parquet (raw / feature store) → GCS bucket FUSE mount. Cloud SQL explicitly v2-scope. P8 is implementation, not validation / confirmation. |
| _today_ | ChangYeong + AI pair  | **Added §6.5.5: Priority dilution prevention.** Under the state-change-only policy, high-tier alerts can be visually buried by follow-up lower-tier alerts. The 4-layer defense: (1) Telegram = EMERGENCY-only channel, (2) subject prefix tier hierarchy (`🚨🚨` / `⚠️` / `📊`), (3) **EMERGENCY heartbeat reminder** (every 1 h, the sole exception to state-change-only), (4) **Higher-tier active cross-tag** — at send time, every active higher-tier state (`EMERGENCY > RISK_OFF > WATCH`) is auto-appended to the subject of lower-tier emails (e.g. a RISK_OFF email carries the active EMERGENCY; a WATCH digest carries both active EMERGENCY and active RISK_OFF). Added the subject-prefix column + EMERGENCY heartbeat note to §6.5.1, applied emoji prefix to §6.5.2 sample subject, and noted the heartbeat exception in the §6.5.3 throttle rule. |
| _today_ | ChangYeong + AI pair  | **§6.5.5 acknowledgement mechanism in v1 is intentionally skipped.** Documented that an EMERGENCY heartbeat not stopping itself without an explicit user ack is by design, not an omission. Rationale: EMERGENCY naturally de-escalates; if it really persists, a reminder is correct; YAGNI. Made the v2 review trigger explicit (add Telegram `/ack` when something persists 4 h+ N times per month). |
| _today_ | ChangYeong + AI pair  | **§9 Glossary expanded.** After D6 redefinition, dwell-time redesign, and metric clarification, listed all newly meaningful or changed terms: `Channel tier`, `fused_score` (role change emphasized), `per_channel_tiers`, `tier_floor`, `Corroboration boost`, `Dwell time`, `Detection latency`, `Warning time`. Existing `ChannelSignal` / `State` entries were also updated to reflect the new schema / rules. |
| _today_ | ChangYeong + AI pair  | **Redesigned §5.4.3 dwell time as tier-asymmetric.** Old "uniform 60 s on the way up" → stronger tier and higher prior state get shorter dwells (RISK_OFF → EMERGENCY 10 s, single-channel jump → EMERGENCY 15 s, NORMAL → WATCH 30 s, WATCH → RISK_OFF 30 s). De-escalation stays cautious at 5 / 10 min. Rationale: detectors are rolling windows so burst signals persist naturally; a uniform 60 s is too slow for EMERGENCY defense. Also noted the sample-count alternative (matches per-channel sample rates). Tuning approach: P10 injects 10–15 historical events and measures two metrics — (1) **detection latency** (anomaly start → alert, our system's perf, **goal median ≤ 60 s**), (2) **warning time** (alert → announcement, depends on insider timing, informational). |
| _today_ | ChangYeong + AI pair  | **Complete redesign of state definition into channel-level tier + system-level max-wins (+ corroboration boost) two-stage structure (D6 redefined).** The old rule ("≥ 2 channels at WATCH+ → RISK_OFF" hard requirement) was burying single-channel high-conviction insider patterns (e.g. 2026.03.23 CME-only 6,200 contracts, 2025.10.10 Hyperliquid-only known whale large short) at WATCH. Fix: (1) Added `tier` + `fired_detectors` to `ChannelSignal`, added `per_channel_tiers` / `tier_floor` / `boost_applied` to `FusedAnomalyEvent`. (2) Demoted `fused_score` from "primary state input" → "audit / reference / boost-aux". (3) Rewrote §5.4 fully: §5.4.1 channel-level tier definition + v0 baseline mapping, §5.4.2 system-level state (max-tier-wins + boost rule + scenario table), §5.4.3 hysteresis + dwell time separated. (4) Added fused_score role to §8 D5; renamed D6 from "State machine threshold" → "State definition (channel tier + system state)" with updated content. Per-channel detector → tier rules are defined in P9; thresholds tuned by P10 replay. |
| _today_ | ChangYeong + AI pair  | **Added D13 LOCKED + §6.7: Cost ceiling & kill-switch.** A runaway-billing defense for pay-as-you-go services (Databento, OpenAI vision, Cloud Run egress, Cloud Logging) — PAYG monthly cap = sum cap $1,000. Six threshold alerts (10/20/40/60/80/100%, each cross sends a separate email + per-tool breakdown). On 100%, auto-disable PAYG services (`channel.weight=0` → fusion graceful degrade + RISK_OFF tier alert). Two CSVs (`cost_ledger.csv` per-call append + `cost_summary_<YYYY-MM>.csv` daily aggregate, in `data/anomaly_detection/cost/`). Manual override is audit-logged. Impl: P1 scaffold → P4 Databento first wiring → P8 24 h measurement + kill-switch dry-run. Entire §6.7 added (spec summary / impl / CSV format / alert email anatomy / kill-switch behavior). Updated §7 NFR cost ceiling row ($1,000 explicit + §6.7 ref). Added the D13 LOCKED row in §8; updated heading from D1–D12 → D1–D13. Updated §0 TOC. Added Cost ceiling / Kill-switch to §9 Glossary. |
| _today_ | ChangYeong + AI pair  | **§6.7 minor alignment fix.** (1) Removed `X API Basic ($200/month)` from the always-active list in §6.7.1 spec table → "Only when EVT-1 activates (D8) — v1 default uses free snscrape". (2) Added a `if config.X_API_BASIC_ENABLED:` guard to the §6.7.2 `init_monthly_subscriptions()` example (v1 does not call this — expressed in code itself). Consistent with D8 LOCKED. |
| _today_ | ChangYeong + AI pair  | **§6.7 expanded: aggregate cost view (CSV logs PAYG + subscription + free).** User request — alerts only fire on PAYG, but the CSV records every tool so you can see total cost in one place. (1) §6.7.1 spec table separates "CSV scope" (all tools) from "cap scope" (PAYG only); subscription tool list (TradingView / Unusual Whales / X API Basic / Cloud Run base) and free tool list (Telegram / Gmail / Polymarket / Hyperliquid / snscrape) added. (2) §6.7.2 `CostTracker.record()` gains a `type` parameter (`payg` / `subscription` / `free`); added `record_subscription_monthly()` + `record_free_daily()` helpers; three call examples (PAYG / subscription init / free daily flush). (3) §6.7.3 ledger columns expanded with `type` + `payg_cum_month` + `total_cum_month`; summary CSV columns prefixed with `type_payg_*` / `type_sub_*` / `type_free_*`. (4) §6.7.4 alert email body splits into PAYG breakdown / subscription reference / grand total sections. (5) §6.7.5 makes explicit that when the kill-switch fires, only PAYG channels go UNHEALTHY while subscription channels keep running (reinforces the graceful-degrade effect). §7 NFR + §8 D13 + §9 Glossary updated to reflect the PAYG / subscription split; new `PAYG vs subscription` term. |
| _today_ | ChangYeong + AI pair  | **§5.2 score-normalization gap documented explicitly.** Made the gap between noisy-OR's assumed [0, 1] probabilistic semantics and v0 baseline detectors' raw units (z-score / percentile / count) explicit. Reasons it is low priority in v1 (the D6 redesign means tier is now the primary input, and channel-level tier mapping itself acts as implicit normalization) + P9 options (empirical CDF / probability calibration / sigmoid squashing / reference-only). P1–P8 keeps the step-wise mapping and just validates plumbing. |
| 2026-04-21 | ChangYeong + AI pair | **§3 + §6.5 P10.5 + P11(a) + P11(d) sync.** P10.5 cooldown v2 + P11(a) production email wiring + P11(d) channel-level Telegram architecture impacts are now landed in both §3 process view and §6.5 alert subsystem. (1) **§3.2 inside the daemon**: `AlertRouter` now only performs cross-tag bookkeeping (`email_enabled=False`, `telegram_enabled=False`, no heartbeat task created); added the note that `ChannelAlertDispatcher` + `LiveTimelineBuffer` are the new alert entry point. (2) **New §3.3 — daemon alert path visualization**: ASCII flow of the actual code path orchestrator → timeline_buffer.append → channel_dispatcher.maybe_dispatch (cooldown.decide → render_email → send_email → if EMERGENCY: render+send_channel_telegram; .eml/.txt audit dumps in dry-run). (3) **Full rewrite of §6.5.1 per-tier delivery policy**: per-channel, locked subject / caption format `{tier_prefix} {channel_emoji} {SHORT} · {symbol} → {TIER}`, channel emoji table (cme=📊 / poly=🔷 / hl=🟡 / x=𝕏), tier prefix (📋 WATCH / ⚠️ RISK_OFF / 🚨🚨 EMERGENCY — `📊` swapped for `📋` to avoid the CME-logo clash), Telegram = EMERGENCY only. Made the WATCH digest queue disable explicit. (4) **Full rewrite of §6.5.2 email anatomy**: one channel signal = one email model, brand logo (inline cid:) + two timeline plots (1h / 6h) (Telegram also reuses the same PNGs via sendMediaGroup — no second render) + cooldown footer (`alert_id` / `cooldown reason` / `24h lock`). (5) **§6.5.3 cooldown / dedup v2 (P10.5 lock)**: key=`(channel, symbol, tier)` × 24 h, demote silent, escalation only, cross-symbol corroboration preserved, email / Telegram share cooldown (one call per cycle). Validation result: 6 events total alerts 445 → 95 (−79%). cooldown_expired = natural reminder mechanism. (6) **§6.5.4 delivery guarantee**: per-channel error counters (`email_errors` / `telegram_errors`) exposed via `/snapshot`; dry-run audit dump (.eml + .txt + two plot PNGs inside `data/anomaly_detection/alerts_live/{channel}_{symbol}_{tier}_{timestamp}/`); credential env vars renamed `GMAIL_*` → `SMTP_*`; auto dry_run decision rule made explicit. (7) **§6.5.5 priority dilution prevention 4-layer → 3-layer**: removed heartbeat reminder + cross-tag (system-level emails are OFF so there is nowhere to attach + heartbeat task is gone). New model: Telegram = EMERGENCY-only channel (per-channel) + subject / caption prefix + 24 h cooldown_expired = natural reminder. Items removed in the previous v1 (cross-tag / heartbeat / WATCH digest queue) listed in a box. Rewrote the timeline example (BTC EMERGENCY persists 24h+ → cooldown_expired reminder). (8) Acknowledgement mechanism: the 24 h interval is gentle on the user. **plan ↔ architecture P10.5 / P11(a/d) sync complete.** |
| _today_ | ChangYeong + AI pair  | **D12 (deployment target) LOCKED + full plan / architecture sync.** Updated §1 boundary to "Cloud Run daemon + Mac Air dev" (services/advisory_bridge clearly v2-only). Removed services/advisory_bridge from the §2.1 component table; added a monitoring/health row. **Rewrote the §3 process-view diagram**: "Process A / B" → two deployment targets "Cloud Run service (daemon) + Mac Air (dev / observe)". Rewrote §3.1 rationale based on D12 LOCKED (why Cloud Run, cost estimate, RAG stays on Mac Air). Added the Cloud Run persistence option table (GCS FUSE / mounted volume / Cloud SQL) to §4.2. Replaced the final node of the §5.1 happy path "Streamlit Anomaly Radar" → alerts/router → email/telegram. Updated placeholder tuning references in §5.4 P6 → P9 / P10. Added Cloud Run Secret Manager binding + a secrets list to §6.1 configuration. Added Cloud Logging stdout integration + `gcloud logging tail` guide to §6.2 logging. §6.3 X access P9 → EVT-1. Added the Cloud Run instance forced-replacement / Mac Air sleep row to §6.6 failure mode (removed the "Streamlit dies" row). Added push delivery latency / Cloud Run uptime / Cloud Run cost ($50 / month) NFRs to §7, replaced "local 24h" → "Cloud Run 24h". §8 D5 / D6 tune timing P6 → P9 / P10, D7 / D8 P9 → EVT-1; **new D12 row LOCKED**; heading "D1–D11" → "D1–D12". |
| 2026-05-15 | ChangYeong + AI pair | **Added Channel 5 = Truth Social.** Restructured the system from 4 channels to 5 channels. Updated §1 context diagram, §2 logical view (new "Channel 5 Truth Social Module" with collector / backfill / parser / llm_assessor / detector), §3 process view diagram (added `truth_social` task), §4.1 RawEvent channel enum + `FusedAnomalyEvent` `per_channel_*` always 5 entries, §5.2 fusion math (5-channel table + product), §5.4 scenarios (Truth Social EMERGENCY case added), §6.3 security (Truth Social access policy: httpx + Wayback Mastodon API), §6.5.1 channel-emoji table (`truth_social` = 𝕋 / Truthsocial), §6.7 cost-tracking free-tool list (truth_social), §8 D1 (5 channels), D8 (X + Truth Social access policy), D10 (Truth Social symbol = @realDonaldTrump, Stage-1 keyword + Stage-2 LLM), D12 (5-channel daemon), D13 (subscription unaffected list includes Truth Social channel). Translated the entire document to 100% English in the same pass. Updated all paths `src/anomaly/` → `src/anomaly_detection/`, `data/anomaly/` → `data/anomaly_detection/`. |
