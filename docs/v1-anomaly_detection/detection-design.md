# P9 Detection Algorithm Design

> Status: **DESIGN LOCKED** — P9.0 in progress, P9.1 next.
> Companion docs: [`anomaly-architecture.md`](anomaly-architecture.md), [`anomaly-upgrade-plan.md`](anomaly-upgrade-plan.md)

---

## 1. Why this doc exists

This is the single source of truth for "P9 = Detection algorithm deep-dive".

The plan doc explicitly flags P9 as the largest phase with "multiple back-and-forth
sessions expected", so design decisions scattered across code are easy to lose. This
document:

- Collects each channel's detection strategy in one place.
- Records the rationale behind detector priorities (P9.1 → P9.2 → P9.3).
- Captures the per-algorithm learning points (why this formula) in a beginner-friendly way.

Whenever you have a question during P9 work, open this doc first.

---

## 2. Core principle (applies to all of P9)

### 2.1 Five universal signatures of insider trading

Reviewing accumulated cases (4/7 ceasefire, Maduro, 2/28 Iran, 3/23 Truth Social,
4/17 Brent, etc.) yields:

1. **Volume burst concentrated in a short time window**
2. **Directional bias** — directional bets without hedges
3. **Sharp departure from the usual baseline** (or slow accumulating drift)
4. **Sudden activity from a newly-created or previously-dormant entity**
5. **Large futures contracts / options fills right before the announcement (minutes to ~1 hour)**

→ Detectors across all 4 channels are designed to catch the **channel-specific
expression** of these 5 signatures.

### 2.2 Why we avoid deep learning (interpretable statistics first)

Two reasons:

1. **To reduce false positives, a human must be able to explain "why this alert fired".**
   Deep models are black-boxes → trust evaporates quickly during operations.
2. **Labeled data is tiny (~10–15 confirmed cases).** Deep learning cannot train on this
   volume → either overfits or fails to learn at all.

→ Statistical / online change-point methods are the right fit.
→ Once P9.4+ has accumulated enough historical replay logs, ML can come back — but only
  for tuning ensemble weights.

### 2.3 Channel differentiation principle

- The four channels are **fundamentally different in data character, so detectors differ too**.
- Polymarket and Hyperliquid may be similar (both are on-chain trade streams).
- CME is very different from those two (regulated futures + options chain + COT data).
- X is very different from #1–3 (NLP / sentiment + posting cadence).

→ We finish one channel before moving to the next (no parallel work).

---

## 3. Polymarket detector strategy

### 3.1 6 Detector families (the full picture)

Detectable Polymarket anomalies group into 6 families. Each family looks at the same
underlying signature from a different angle.

| Family | What it catches | Key feature(s) | v1 status |
|---|---|---|---|
| **F1. Volume burst** | Sudden large trading | 5-min / 30-min z-score | ✅ exists (`vol_z_v1`) |
| **F2a. Odds gap (fast)** | Instantaneous probability jump | 1-min (max−min) odds | ✅ exists (`price_jump_v1`) |
| **F2b. Odds drift (slow)** | Gradual leak (CUSUM-style) | Cumulative deviation | ❌ missing |
| **F3. Directional consistency** | Hedge vs. one-sided informed flow | buy/sell imbalance, run length | ❌ missing |
| **F4. Microstructure (liquidity)** | Impact of a large market order on a thin book | trade/depth ratio, Kyle's λ, orderbook burn | ❌ missing |
| **F5. Wallet identity** | Same entity placing dispersed bets | fresh wallet count, funding-source clustering | ❌ missing |
| **F6. Cross-market correlation** | Related markets reacting in sync | grouped market odds correlation | ❌ missing |

### 3.2 5 Cross-cutting enhancements (M1–M5)

These upgrade detector quality across families. Each has a different implementation cost.

| ID | What | Cost | Phase |
|---|---|---|---|
| **M1** | Time-of-day conditional baseline | Medium (needs 7–14 days of SQLite accumulation) | **P9.1** |
| **M2** | Use mid-price (best_bid+best_ask)/2 | Low (one extra Gamma call) | **P9.1** |
| **M3** | Kyle's λ (price impact / volume) | Medium (needs multi-level orderbook depth) | P9.2 |
| **M4** | Same-side run length (consecutive same-direction trades) | Low (just keep `side`) | **P9.1** |
| **M5** | Pre-spike scout pattern (probe trades before a big order) | Medium (microsecond timing) | P9.3 (speculative) |

### 3.3 Phasing decision

**P9.1** — Statistical detector v2 (no new data source)
- F1 upgrade: `vol_burst_v2` (apply M1 time-of-day baseline)
- F2a upgrade: `odds_gap_v2` (apply M2 mid-price)
- F2b new: `odds_cusum_v1` (CUSUM on mid-price)
- F3 new: `directional_v1` (M4 imbalance + run length)

**P9.2** — Microstructure (new CLOB websocket)
- F4 new: `liquidity_burn_v1`, `trade_to_depth_v1`, `kyle_lambda_v1` (M3)

**Decision branch (after P9.2 + a light P10 replay)**
- Measure backbone recall. Decide whether wallet clustering really adds value.

**P9.3 (conditional)** — Identity layer
- F5 new: `wallet_cluster_v1` (on-chain Polygon RPC or Dune table)
- Re-evaluate M5 (scout pattern) here

**P9.4 (optional)** — Advanced
- BOCPD (Bayesian upgrade of CUSUM)
- F6 cross-market correlation
- ML training of ensemble weights

### 3.4 Explicitly **dropped / deferred** ideas

These came up during brainstorming but were dropped because the cost/ROI is low or
they are subsumed by something else:

| Dropped | Reason |
|---|---|
| Sigmoid weighted score (`score = sigmoid(w1·z + w2·intensity + ...)`) | Requires learning ML weights. With 10–15 labeled cases → overfit. Deferred to P9.4 |
| Tier 1 / Tier 2 composite rule (four conditions simultaneously) | Belongs in the **fusion engine**, not the detector stage. `core/fusion_engine.py`'s corroboration boost already does something similar |
| "Baseline × 10 on non-event days" | M1 (time-of-day baseline) absorbs this naturally |
| Including BOCPD in P9.1 | Does the same job as CUSUM (both are change-point detection). CUSUM is light and robust → enough for P9.1. BOCPD is a learning toy for P9.4 |

---

## 4. P9.1 algorithm study notes (beginner-friendly)

Four statistical concepts to learn while writing P9.1. Each detector's docstring should
include a short 5-line summary.

### 4.1 Limits of z-score and robust statistics

The classic z-score: `(current - mean) / std`. The problem: a single spike of 1–2 events
shakes both mean and std (and if the current sample is included in the baseline, the
detector erodes its own sensitivity).

**Fix 1 — robust statistics**: mean → median, std → MAD (Median Absolute Deviation).
Spikes barely move the median / MAD.

**Fix 2 — temporal separation**: compute the baseline **outside** the current window
(the current code already does this).

**Fix 3 — time-of-day conditional**: instead of "the last 30 minutes", use "the average
of the same time slot over the last N days" → absorbs natural diurnal variation (M1).

→ P9.1 applies **Fix 3 + (Fix 1 as an alternative to M1)**.

### 4.2 CUSUM (Cumulative Sum)

**What**: Sequential change detection borrowed from Industrial Statistical Process Control.
**Why**: Z-score only asks "is this moment big?" → it misses small deviations that
accumulate into slow drift. CUSUM catches them with a running sum.

**Formula**:
```
S_t = max(0, S_{t-1} + (x_t - μ - k))
trigger when S_t >= h
```

- `μ`: baseline mean (normal average)
- `k`: slack — the noise-ignore band. Usually 0.5 × std.
- `h`: threshold — the alarm-firing cumulative value. Usually 5 × std.
- `S_t` returning to 0 means a return to normal.

**Example**: a market normally at odds 0.10 slowly climbs 0.11 → 0.12 → 0.13 → 0.14.
A single z-score reads each step as normal, but CUSUM's cumulative sum exceeds the
threshold → alarm.

### 4.3 Time-of-day baseline

**Idea**: Market trading is cyclical by time of day. Early-morning volume ≠ US-open volume.

**Implementation**:
1. Cut time into 5-minute buckets (`(weekday, hour, bucket_5min)` key).
2. Accumulate the volume distribution for each key over the last 7–14 days in SQLite.
3. Compare the current 5-minute volume against that distribution's z-score.

**Trade-off**: 7-day warmup is required → during the first 7 days the daemon must fall
back to the rolling 30-minute baseline.

### 4.4 Imbalance + run length

**Imbalance ratio**: `|buy_vol - sell_vol| / (buy_vol + sell_vol)`. 0 = perfectly balanced,
1 = fully one-sided.

**Run length**: if the last N trades are all in the same direction, run length = N. At
random, 5 consecutive same-direction trades have P=1/16; 10 in a row → P=1/512 → matters
as binomial significance.

**Why both?** Imbalance is volume-weighted (one big trade = ten small ones); run length
is trade-count-weighted. If a hedge fund spreads buying over 100 small trades, imbalance
catches it weakly while run length catches it strongly.

---

## 5. Evaluation harness (P9.0)

### 5.1 Goal

Make every P9 detector change **deterministically regression-testable**. For each
historical event:

- (input) Replay the raw event stream from that time window.
- (output) Detector / fusion / state / decision results.
- (metric) Precision / recall / lead-time (= alert ts vs. announcement ts).

### 5.2 Folder layout

```
data/historical-events/
  2025-04-07-polymarket-iran-ceasefire/
    manifest.json              # event metadata
    raw/                       # raw event-stream JSONL
      polymarket-trades.jsonl
      polymarket-orderbook.jsonl
    expected/                  # expected output (if any)
      signals.jsonl            # ChannelSignals we want to catch
      decision.json            # expected final decision
    notes.md                   # human-readable case analysis

  2025-10-10-hyperliquid-trump-100pct-tariff/
    manifest.json
    ...

scripts/
  event_replay.py              # CLI: list / replay / print metrics
```

### 5.3 manifest.json schema

```json
{
  "event_id": "2025-04-07-polymarket-iran-ceasefire",
  "title": "Iran ceasefire announcement (4/7)",
  "channels": ["polymarket"],
  "announcement_ts_iso": "2025-04-07T13:00:00Z",
  "window_start_iso": "2025-04-07T11:00:00Z",
  "window_end_iso":   "2025-04-07T14:00:00Z",
  "expected_alert_tier": "EMERGENCY",
  "expected_lead_time_min_min": 30,
  "evidence_url": "https://...",
  "notes": "wallet clustering case — 6+ fresh wallets, all YES, ~$500K"
}
```

### 5.4 Replay script CLI

```bash
# List every registered event
python scripts/event_replay.py --list

# Replay one specific event
python scripts/event_replay.py --event 2025-04-07-polymarket-iran-ceasefire

# Replay everything and print a summary metric
python scripts/event_replay.py --all --report
```

### 5.5 Scope of P9.0 (small code footprint)

P9.0 is only the **harness skeleton**:
- Create the folder layout
- Define the manifest schema + 1 case (4/7 ceasefire) manifest only (raw data filled in later)
- Wire up `--list` and `--event <name>` in the replay script (stub output when raw is empty)
- Actual metric calculation plugs in once P9.1 detectors land

→ Goal: P9.0 is small and provides the validation foundation for P9.1+.

---

## 6. P9 success criteria (check at the end of each phase)

### P9.0 done when:
- `docs/p9-detection-design.md` (this doc) exists.
- `data/historical-events/` folder + 1 manifest exists.
- `scripts/event_replay.py --list` lists that 1 case.

### P9.1 done when:
- 4 detectors (`vol_burst_v2`, `odds_gap_v2`, `odds_cusum_v1`, `directional_v1`) emit ChannelSignals.
- M1 time-of-day baseline SQLite activates after 7 days of accumulation.
- M2 mid-price flows into NormalizedEvent.meta.
- M4 side flows into NormalizedEvent.meta.
- golden_smoke.py gains 4 new scenarios, all PASS.
- After 30 minutes of Cloud Run observation, no abnormal alerts.

### P9.2 done when:
- CLOB websocket subscription added; orderbook depth flows into NormalizedEvent.meta.
- 3 detectors (`liquidity_burn_v1`, `trade_to_depth_v1`, `kyle_lambda_v1`) emit.
- New golden_smoke.py scenarios PASS.

### Decision branch (after P9.2):
- Run a light P10 replay (≥3 historical cases).
- Backbone recall ≥ 70% → P9.3 is a skip candidate.
- Backbone recall < 70% → proceed with P9.3 (wallet clustering).

---

## 7. P9 plan for other channels (placeholder)

Repeat the same procedure (brainstorm → consolidate → pick 4–6 detectors → phasing) per channel.

- **Hyperliquid** (P9-HL): similar to Polymarket (on-chain perp trade stream). OI delta + funding-rate divergence are extra signals.
- **CME** (P9-CME): regulated futures + options chain. Block-trade detection / option-skew shift / unusual contract month are the key signals.
- **X** (P9-X): NLP. Posting-cadence anomaly + sentiment shift + cross-reference against a curated whale list.

Detail design for each channel is appended as a new section right before that channel's work begins.

---

## 8. Open questions (to be answered during P9)

| Q | Phase to answer |
|---|---|
| Time-of-day baseline history length — 7d? 14d? 28d? | P9.1 (decide after looking at data once code is written) |
| Initial `k` and `h` for CUSUM | P9.1 (decide after looking at average Polymarket odds movement in the architecture doc) |
| Weight split between imbalance and run length (0.5/0.5? 0.7/0.3?) | P9.1 (fit using golden test scenarios) |
| Is wallet clustering actually necessary? | Decision branch (after P9.2) |
| When will enough data accumulate to train fusion weights for 4 channels? | Decided post-P10 from production data |

---

> **Edit rule for this doc**: if a design decision changes mid-P9, update this doc in the
> same PR as the code change. Drifting between the design doc and code destroys trust.
