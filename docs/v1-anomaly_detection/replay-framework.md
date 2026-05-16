# P10 Replay Framework Design

> Status: **DESIGN — to be locked after smoke test**
> Companion docs: [`anomaly-architecture.md`](anomaly-architecture.md), [`anomaly-upgrade-plan.md`](anomaly-upgrade-plan.md), [`detection-design.md`](detection-design.md)
> Event corpus: [`../../data/anomaly_detection/historical_events/README.md`](../../data/anomaly_detection/historical_events/README.md)

---

## 1. Why this doc exists

P10 = "Detection algorithm validation". P9 locked the detector and fusion
math; P10 measures how well it actually works on historical events and
replaces placeholder thresholds with empirical values.

This document is the single source of truth for **replay framework v0**,
the system that makes those validations automatic and reproducible.

P10 sub-phases (plan §8.1 P10):

| Sub | Title | Status |
| :-- | :--- | :--- |
| P10.1 | Historical event list lock | DONE — 6 events in `data/anomaly_detection/historical_events/` |
| **P10.2** | **Per-channel historical data collection** | **NEXT after P10.3 framework v0** |
| **P10.3** | **Replay framework** | **THIS DOC** |
| P10.4 | Metric calculation (latency, warning, FP) | depends on P10.3 |
| P10.5 | Tier mapping + threshold tune iteration | depends on P10.4 |
| P10.6 | Final report | last |

> P10.3 is scheduled before P10.2 because the framework needs to lock the
> input schema (1-min OHLCV / event stream) before data collection can
> start without wasted effort.

---

## 2. Design principles

### 2.1 Replay clock = **1-minute bar discrete step**

> Per-bar discrete step; the bar itself is a function of time.

The unit a human looks at on a chart (1-minute bar) is the simulator's
tick. We do NOT use the asyncio event loop or wall-clock sleep.

```
sim_clock = event.window_start
while sim_clock <= event.window_end:
    for ch in active_channels:
        bar = data_source.get_bar(ch, sim_clock)            # 1-min OHLCV / signal bucket
        features = ch.feature_engine.update(bar, sim_clock) # rolling window including prior bars
        signal = ch.detector.evaluate(features, sim_clock)  # may fire
    fused = fuse(all_signals, weights, health, sim_clock)   # multi-channel + boost
    decision = state_manager.observe(fused, sim_clock)      # tier transition
    result.record(sim_clock, signals, fused, decision)
    sim_clock += 1 minute
```

### 2.2 Multi-channel + fusion + boost

P9 = per-channel detector tuning, P10 = system-level integration. All four
channels always run for every event (if a channel has no data, it
gracefully skips and emits NORMAL).

→ Cross-channel boost scenarios such as Event #2 (X tweet + HL OI z-score
simultaneously) are validated end-to-end.

### 2.3 Each event is independent

Events do not share detector state (e.g. rolling baselines). ReplayRunner
creates fresh detector instances per event. The only exception is the
baseline-warmup window (`event.ts - 14d ~ event.window_start`), which is
fetched separately so detectors do not cold-start.

### 2.4 In-memory storage

Production stores (`signal_store.db`, `decision_store.db`) are NEVER
touched. ReplayRunner accumulates everything in in-memory lists and
exports them as CSV / YAML / PNG at the end.

Reason: replay is re-run dozens of times. Polluting the production DBs
each time would break the production daemon's audit trail.

### 2.5 Real data first, fixture fallback only when source is essentially unavailable

> **Why do we need fixtures?**
> Most historical data is still present at the source (the same data you
> watched on the chart at the time). It is only that our system was not
> running back then, so the raw data was not written into our stores.
> Replay re-fetches that data after the fact and feeds it to the detectors.
>
> One exception: **HL info API depth** — Hyperliquid's official info API
> typically retains only the last ~6 months to 1 year on its free tier.
> Older events are unrecoverable from the source. Only then is a manual
> fixture capture required. Every other channel can be replayed from the
> real source.

| Channel | Primary source | Coverage | Fallback |
| :-- | :-- | :-- | :-- |
| CME | Databento Historical (`databento_client.py` already implemented, with disk cache + cost cap) | Multiple years | — |
| Polymarket | GraphQL `/data-api` + on-chain (Dune `dune_backfill.py`) | Multiple years | — |
| Hyperliquid | info API `/info` (candleSnapshot, recentTrades) | **Last ~6 months to 1 year only** | Manual fixture JSON (older events only) |
| X | snscrape (search by keyword + timeframe) | Multiple years | Manual fixture (when snscrape is blocked) |

Fixture format = parquet or JSON, stored at
`data/anomaly_detection/replay_fixtures/<event_id>/<channel>.parquet`.

### 2.6 Output = CSV + per-event YAML + PNG plot

Three artifact types — humans look at and judge them; machines aggregate
them.

```
data/anomaly_detection/replay_results/
├── summary.csv                              # cross-event aggregate
├── 2025-04-09_liberation_day/
│   ├── result.yaml                          # tier timeline + fired_detectors per minute
│   ├── timeline.png                         # tier escalation + signal stem plot
│   └── per_channel/
│       ├── cme.csv                          # 1-min OHLCV + features + detector verdict
│       ├── polymarket.csv
│       └── ...
└── 2025-10-10_china_tariff_100/
    └── ...
```

One-line example from `summary.csv`:

```
event_id, primary_channel, event_ts, max_tier_reached, first_alert_ts, detection_latency_s, warning_time_s, fp_count, channels_fired
2025-04-09_liberation_day, cme, 2025-04-09T17:18:00Z, EMERGENCY, 2025-04-09T17:02:30Z, 87, 930, 0, "cme,x"
```

(`detection_latency_s` = first_alert_ts − first_anomaly_observed_ts,
`warning_time_s` = announcement_ts − first_alert_ts.)

---

## 3. Module layout

```
src/anomaly_detection/replay/
├── __init__.py
├── event_library.py        # YAML frontmatter parser → HistoricalEvent
├── schemas.py              # ReplaySession, ReplayResult, BarTick, etc.
├── data_sources/
│   ├── base.py             # HistoricalDataSource Protocol
│   ├── null.py             # placeholder — emits no bars (channel NORMAL). Fills the v0 gap for unimplemented channels.
│   ├── cme_databento.py    # wraps existing databento_client.fetch_historical_range
│   ├── polymarket_graphql.py
│   ├── hyperliquid_info.py
│   ├── x_snscrape.py
│   └── fixture.py          # load from data/anomaly_detection/replay_fixtures/
├── runner.py               # ReplayRunner — main loop (§2.1 pseudocode)
├── metrics.py              # latency / warning / FP calculation
├── reporters/
│   ├── csv.py              # summary.csv + per_channel/*.csv
│   ├── yaml_report.py      # result.yaml
│   └── plot.py             # matplotlib timeline.png
└── cli.py                  # python -m anomaly_detection.replay <event_id>
```

Key point: replay **reuses detector / feature_engine / fusion /
state_manager as-is**. The new code is only (a) the layer that normalizes
data into 1-min bars, (b) the result collector, and (c) the reporters.

### 3.1 New Pydantic schemas

```python
class HistoricalEvent(BaseModel):
    event_id: str               # "2025-04-09_liberation_day"
    event_ts: datetime          # announcement timestamp (UTC)
    primary_channel: str
    secondary_channels: list[str]
    primary_symbols: list[str]
    position_type: str          # "options_call" / "futures_short" / ...
    window_before_min: int = 60 # how far before event_ts to start replay
    window_after_min: int = 30  # how far after event_ts to end replay
    warmup_days: int = 14       # baseline data fetch window
    target_tier_timeline: dict[str, str]  # {"T-30min": "WATCH", ...}
    related_events: list[str] = []
    # Parsed from frontmatter; narrative is kept as raw markdown for the report.

class BarTick(BaseModel):
    channel: str
    symbol: str
    ts: datetime                # bar open time (UTC)
    bar_seconds: int = 60
    payload: dict[str, Any]     # OHLCV / aggregated Polymarket trades / X tweets / HL OI snapshot

class ReplayMinute(BaseModel):
    sim_clock: datetime
    per_channel_signals: dict[str, ChannelSignal | None]
    fused_event: FusedAnomalyEvent | None
    decision: DecisionRecord | None

class ReplayResult(BaseModel):
    event: HistoricalEvent
    started_at: datetime
    finished_at: datetime
    minutes: list[ReplayMinute]
    metrics: ReplayMetrics      # latency, warning, FP, max_tier
```

### 3.2 HistoricalDataSource Protocol

```python
class HistoricalDataSource(Protocol):
    channel: ClassVar[str]

    def supports(self, event: HistoricalEvent) -> bool: ...
    async def warmup(self, event: HistoricalEvent) -> None:
        """Fetch the baseline window (event.warmup_days) and seed feature_engine."""
    async def get_bar(self, symbol: str, sim_clock: datetime) -> BarTick | None: ...
    async def close(self) -> None: ...
```

Each source is responsible for normalizing its channel data into 1-min
BarTick objects.

---

## 4. CLI / usage

```bash
# Replay 1 event (uses real sources)
python -m anomaly_detection.replay 2025-04-09_liberation_day

# Replay all 6 events and write summary.csv
python -m anomaly_detection.replay --all

# Force fixture mode (skip live source fetch)
python -m anomaly_detection.replay 2025-04-09_liberation_day --fixture-only

# Override config inline (threshold tuning)
python -m anomaly_detection.replay --all --override polymarket.detector.vol_burst_v2_tod.threshold=4.0
```

Each run writes artifacts to
`data/anomaly_detection/replay_results/<event_id>/`. `--all` additionally
updates `summary.csv`.

---

## 5. Metric definitions (P10 dual metric)

The framework auto-computes the dual metric from plan §8.1 P10 + §12.3.

| Metric | Definition | Where it lives |
| :-- | :-- | :-- |
| **detection_latency_s** | first_alert_ts − first_anomaly_observed_ts | primary metric, goal median ≤ 60s |
| **warning_time_s** | event.event_ts − first_alert_ts | informational; depends on the insider's timing |
| **fp_count** | RISK_OFF-or-higher alerts before event.event_ts (excluding the warmup window) that the scenario does not list as legitimate | per-event FP estimate |
| **max_tier_reached** | highest system_state reached inside the event window | hit/miss judgement (compared with the last tier in target_tier_timeline) |
| **target_match_score** | overlap between target_tier_timeline and the actual tier escalation (0–1) | per-event scenario fidelity |

`first_anomaly_observed_ts` is the timestamp of the first "pre-event
suspicious activity" trade in each event's narrative (manual ground
truth from the event .md).

---

## 6. Plot specification (matplotlib)

`timeline.png` per event:

```
┌─────────────────────────────────────────────────────────────┐
│  CME ES — vol_z_v1                          [stem plot]     │
│  Polymarket — vol_burst_v2_tod              [stem plot]     │
│  Hyperliquid — oi_z_v1                      [stem plot]     │
│  X — Stage1 score                           [stem plot]     │
│─────────────────────────────────────────────────────────────│
│  per_channel_tier (4 lanes, color-coded)    [step plot]     │
│  system_state                               [step plot]     │
│─────────────────────────────────────────────────────────────│
│  vertical line at event.event_ts            "ANNOUNCE"      │
│  vertical lines at first_alert_ts per tier  "WATCH/RISK_OFF/EMERGENCY"  │
└─────────────────────────────────────────────────────────────┘
```

X-axis = sim_clock (±N minutes around event.event_ts as zero).
Y-axis = each detector's raw score / tier rank.

→ One PNG is enough for a human to see "this is where it spiked, this is
when our detector caught it".

---

## 7. v0 build order (next steps)

> Principle (confirmed by the user): **the runner is multi-channel +
> fusion + boost from day 1.** Only data sources are added incrementally.
> When a source is not yet implemented, that channel gracefully emits
> NORMAL.

1. **Schemas + EventLibrary** — `replay/schemas.py`, `replay/event_library.py`; parse the 6 event .md frontmatters.
2. **Runner skeleton (multi-channel from day 1)** — `runner.py` + `data_sources/base.py` + `data_sources/null.py` (a placeholder where every channel emits NORMAL). Sanity-check that the Liberation Day 1-min loop reaches fusion + state_manager.
3. **CME data source (real)** — `data_sources/cme_databento.py` wrapping the existing `databento_client.fetch_historical_range`. Start from the most mature source.
4. **CSV + YAML reporter** — `reporters/csv.py`, `reporters/yaml_report.py`.
5. **Plot reporter** — `reporters/plot.py` (matplotlib).
6. **Smoke test #1** — Liberation Day end-to-end (CME real + the others NORMAL) → review whether PNG / CSV / YAML look reasonable.
7. (After confirmation) Add the Polymarket data source → Smoke test #2 (validate with the Maduro arrest event).
8. (After confirmation) Add the X data source → Smoke test #3 (validate with the China tariff event).
9. (After confirmation) Add the HL data source → run all 6 events → auto-generate `summary.csv`.

---

## 8. Open / deferred decisions

| Topic | Status |
| :-- | :-- |
| Threshold tuning loop (P10.5) | Separate doc, once framework v0 is stable |
| Channels whose bar size is not 1 minute (e.g. HL OI 5-second snapshots) | v0 = uniform 1-min buckets. Add per-channel overrides if finer buckets are needed |
| Cross-event correlation detector (Event #1 ↔ #6 Brent burst pattern) | Revisit in P10.5 after looking at replay results |
| Streaming replay (live-tail paper trading) | Deferred. After P10 validation, planned for P11 (or EVT if no P11) |

---

## Appendix A — File locations cheat sheet

- Event corpus: `data/anomaly_detection/historical_events/<event_id>.md`
- Replay code: `src/anomaly_detection/replay/`
- Replay results: `data/anomaly_detection/replay_results/<event_id>/`
- Replay fixtures (only if source unavailable): `data/anomaly_detection/replay_fixtures/<event_id>/<channel>.parquet`
- Databento cache (reused): `data/databento/cache/`
- Cost ledger (reused): `data/anomaly_detection/cost/`
