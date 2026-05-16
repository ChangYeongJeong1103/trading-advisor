# Anomaly Detection Math — v1 Reference

> **Purpose**: The page you open **first** when tuning thresholds or
> debugging "why did this alert fire?".
> Every formula and threshold matches the **production code as of
> 2026-04-21**. Code locations are included next to each block.
>
> For the narrative ("why is it designed this way?"), see
> [`anomaly-architecture.md`](anomaly-architecture.md).

---

## 0. End-to-end flow (1 cycle = 5 seconds)

```
RawEvents (per channel)
    │
    ▼
Features  (per channel, FeatureSnapshot)
    │
    ▼
Channel Detectors  (each channel has 1–5 detectors)
    │   each detector → (tier, score)
    │   inside one channel → max tier, max score
    │   + channel-internal modulators (panic_filter / wallet ±1 / credibility floor ...)
    ▼
ChannelSignal  (one per channel)
    │
    ▼
Fusion Engine  (4 channel signals → 1 system state)
    │   1) tier_floor = max(per_channel_tier)
    │   2) corroboration boost (≥2 channels agree on direction → +1 step)
    │   3) noisy-OR fused_score (reference only)
    ▼
FusedAnomalyEvent  (state, fused_score, rationale)
    │
    ▼
AlertRouter → Email / Telegram + RecommendedAction
```

3-layer output shape:

| Layer | Output | Range |
|---|---|---|
| **Channel score** | Signal strength (continuous) | `[0.0, 1.0]` |
| **Channel tier** | Signal grade (discrete) | `NORMAL(0) < WATCH(1) < RISK_OFF(2) < EMERGENCY(3)` |
| **System state** | Overall system grade | Same 4 levels |

> Tier definition: `src/anomaly_detection/core/schemas.py` → `class Tier`

---

## 1. Channel-level: per-channel detectors

### 1.1 CME — `src/anomaly_detection/channels/cme/detector.py`

**Input features**: `vol_zscore_5min`, `price_jump_pct_1min`, `price_jump_pct_5min`, `baseline_ready`

**Detectors (2)** — `final_tier = max(vol_z_v1, price_jump_v1)`

```
detector "vol_z_v1":
    vol_z >= 4                                        → WATCH
    vol_z >= 6                                        → RISK_OFF
    vol_z >= 8  AND  price_jump_pct_5min >= 0.005     → EMERGENCY
    # Using the 5-minute cumulative move catches the burst's aftermath
    # (1-minute move decays immediately after the burst).

detector "price_jump_v1":
    price_jump_pct_1min >= 0.003 (0.3%)               → WATCH
    price_jump_pct_1min >= 0.005 (0.5%)               → RISK_OFF
    # 1-minute as-is — catch a fresh price move (news reflected instantly)
    # separately from the volume detector.
```

**Special rules**
- `baseline_ready = False` (warming up) → always NORMAL, confidence 0.3
- `direction = NEUTRAL` is fixed (v1)
- If `mock_spike_count_5min > 0`, append `MOCK_SPIKE` to reason (visually distinct from production noise)

**Score** (piecewise linear, `max(vol_score, pj_score)`)

```
vol_z piecewise: 0 → 0.0
                 4 → 0.50  (cleared WATCH)
                 6 → 0.70  (RISK_OFF)
                 8 → 0.85  (EMERGENCY)
                 ∞ → 0.95  (asymptote)

pj    piecewise: 0       → 0.0
                 0.3%    → 0.30  (WATCH)
                 0.5%    → 0.55  (RISK_OFF)
                 ∞       → 0.80
```

**Threshold tuning guide**
| Symptom | Fix |
|---|---|
| WATCH fires too often (many FPs) | `vol_z_watch` 4 → 5 |
| EMERGENCY never fires | `vol_z_emergency` 8 → 6 or `pj_emergency` 0.5% → 0.3% |
| Fast spikes get missed | `pj_watch` 0.3% → 0.2% (raise price_jump_v1 sensitivity) |

---

### 1.2 Hyperliquid — `src/anomaly_detection/channels/hyperliquid/detector.py`

**Detectors (4)** — max tier, max score → EMERGENCY guard → panic filter (demote)

```
detector "vol_z_v1":   (alone, capped at RISK_OFF)
    vol_z >= 4    → WATCH
    vol_z >= 6    → RISK_OFF
    vol_z >= 9    → EMERGENCY  ← but capped to RISK_OFF (allow_vol_only_emergency=False)

detector "insider_v1":   (counts 4 conditions)
    cond_vol     : vol_z >= 6
    cond_oi      : oi_delta_usd >= $2M  OR  oi_delta_ratio >= 1%
    cond_funding : |funding_rate| >= 2e-5  OR  |funding_delta_5min| >= 1e-5
    cond_stealth : |price_return_5min| <= 2%  AND  impact_ratio <= 0.90
                   # "big volume but price barely moves" = quiet-accumulation signature

    cond_count >= 3   → EMERGENCY  (score 0.90)
    cond_count >= 2   → RISK_OFF   (score 0.75)
    cond_count == 1   → WATCH      (score 0.55)

detector "new_whale_v1":   (5-min cumulative taker notional from wallets first seen in last 24h)
    cum5m >= $25M    → EMERGENCY  (single-detector EMERGENCY allowed — exempt)
    cum5m >= $10M    → RISK_OFF
    cum5m >= $2M     → WATCH

detector "cluster_v1":   (fresh wallets on same coin × same side × similar price; AND)
    n_wallets >= 8  AND  sum >= $30M  → EMERGENCY  (single-detector allowed — exempt)
    n_wallets >= 5  AND  sum >= $15M  → RISK_OFF
    n_wallets >= 3  AND  sum >= $5M   → WATCH
```

**EMERGENCY guard** (prevents single-detector hypersensitivity)

```
if final == EMERGENCY:
    # If new_whale_v1 or cluster_v1 hit EMERGENCY, it passes alone (exempt).
    if not (new_whale_emergency or cluster_emergency):
        # Otherwise we need ≥ 2 detectors at RISK_OFF+ to keep EMERGENCY.
        if (RISK_OFF+ detector count) < 2:
            final ← RISK_OFF
```

**Panic filter** (modulator, demote) — separates panic sell-off from insider accumulation

```
panic = (vol_z >= 4) AND (oi_delta_usd <= 0)
        AND (price_return_5min <= -1%) AND (impact_ratio >= 1.30)

if panic AND final ∈ {RISK_OFF, EMERGENCY} AND not (new_whale_strong OR cluster_strong):
    final ← WATCH
    score ← min(score, 0.55)
    # Large entries by fresh wallets are NOT demoted even under a panic pattern —
    # they could still be the real insider.
```

**Direction priority**: `cluster_strong → new_whale_strong → price_return → OI+funding → NEUTRAL`

**Special rules**
- `baseline_ready = False` → NORMAL (confidence 0.3)
- `panic AND not wallet_strong` → confidence 0.7

**Threshold tuning guide**
| Symptom | Fix |
|---|---|
| insider_v1 fires too often | `insider_watch_min_conditions` 2 → 3 (each tier 1 step harder) |
| Misses big whale entries | `new_whale_emergency_usd` $25M → $15M |
| EMERGENCY survives during panic | `panic_impact_ratio_min` 1.30 → 1.10 (panic catches more) |

---

### 1.3 Polymarket — `src/anomaly_detection/channels/polymarket/detector.py` (most complex)

**Detectors (5)** + wallet modulator (±1)

> Semantically-equivalent v1/v2 detectors use **conditional fallback**
> (prevents double-firing from inflating the score).

```
detector "vol_burst_v2"   (fallback to vol_z_v1):
    use_tod = (tod_baseline_n >= 5)    # enough time-of-day baseline?
    if use_tod:
        tod_z >= 5     → WATCH
        tod_z >= 8     → RISK_OFF
        tod_z >= 12    → EMERGENCY
    else if baseline_ready:   # v1 vol_z_v1 fallback
        vol_z >= 4                              → WATCH
        vol_z >= 6                              → RISK_OFF
        vol_z >= 10  AND  pj_1min >= 0.20       → EMERGENCY

detector "odds_gap_v2"   (fallback to price_jump_v1):
    if has_mid_price:   # orderbook mid available
        mid_jump >= 0.06    → WATCH
        mid_jump >= 0.12    → RISK_OFF
        mid_jump >= 0.25    → EMERGENCY
    else:   # v1 fallback (last_trade_price)
        pj >= 0.10    → WATCH
        pj >= 0.20    → RISK_OFF

detector "odds_cusum_v1":   (slow cumulative deviation — gradual accumulation)
    strength = max(cusum_pos, cusum_neg)
    strength >= 0.04    → WATCH      (4 pp cumulative)
    strength >= 0.07    → RISK_OFF   (7 pp)
    strength >= 0.12    → EMERGENCY  (12 pp)

detector "directional_v1":   (imbalance × run_length, both must pass)
    n_trades_5min >= 5 required
    |imb| >= 0.55  AND  run >= 6      → WATCH
    |imb| >= 0.75  AND  run >= 12     → RISK_OFF
    |imb| >= 0.92  AND  run >= 30     → EMERGENCY

detector "wallet_concentration_v1":   (P10.3 — HHI + dir_ratio + few-wallet bonus combined)
    # Guard: n_trades >= 5 AND vol_usd >= $5k (filters micro-trade FPs)
    wc_score >= 0.50    → WATCH
    wc_score >= 0.65    → RISK_OFF
    wc_score >= 0.85    → EMERGENCY
    # wc_score examples:
    #   1 wallet alone ($100k)            → 0.90  (EMERGENCY)
    #   38 wallets split (Iran scenario)  → 0.66  (RISK_OFF)
    #   100 retail mixed                  → 0.25  (NORMAL)
```

**EMERGENCY guard (hotfix v0.3.2)** — blocks single-detector EMERGENCY flooding

```
if final == EMERGENCY AND (count of RISK_OFF+ detectors) < 2:
    final ← RISK_OFF
    reason += "HOTFIX_EMERGENCY_GUARD"
```

**Wallet modulator (±1)** — wallet distribution adjusts the other detectors

```
non_wc_max = max(burst, gap, cusum, dir)   # exclude wc itself

# (BOOST ↑) "another detector fired AND wallets are concentrated" = real insider
if wc_score >= 0.65
   AND non_wc_max ∈ {RISK_OFF, WATCH}
   AND final != EMERGENCY
   AND wc_unique >= 5:           # 5+ wallets agreeing in direction (38-wallet split pattern)
    final ← tier_up(final)
    reason += "WC_BOOST"

# (DAMP ↓) "EMERGENCY but wallets are scattered" = likely retail panic
elif wc_score <= 0.30
     AND wc_unique >= 30
     AND final == EMERGENCY
     AND n_trades >= 5:
    final ← RISK_OFF
    reason += "WC_DAMP_RETAIL"
```

**Direction priority**: `directional_v1 fires → stronger cusum side → imbalance sign → NEUTRAL`

**Special rules**
- `baseline_ready = False AND tod_n < 5` → confidence 0.5 (burst detector is meaningless)

**Threshold tuning guide**
| Symptom | Fix |
|---|---|
| EMERGENCY fires too often (FP) | `min_riskoff_detectors_for_emergency` 2 → 3 |
| Misses spread-out accumulation (38-wallet split) | `wc_riskoff` 0.65 → 0.55 (raise wallet detector sensitivity) |
| Retail panic still trips EMERGENCY | `wc_low_threshold` 0.30 → 0.40 (DAMP applies more often) |

---

### 1.4 X (Twitter) — `src/anomaly_detection/channels/x/detector.py`

**Input**: `n_unique_accounts_15min`, `sum_account_weight_15min`, `magnitude_count_15min`, `direction_buy/sell_count_15min`

```
n_acc >= 1                                    → WATCH
n_acc >= 2                                    → RISK_OFF
n_acc >= 3  AND  magnitude_count >= 1         → EMERGENCY

# Credibility floor (1 step ↓ — demote when only low-trust accounts are noisy)
if tier == EMERGENCY AND sum_weight < 2.4:    tier ← RISK_OFF
if tier == RISK_OFF  AND sum_weight < 1.5:    tier ← WATCH

# Direction
if buy > sell + 1:    UP
if sell > buy + 1:    DOWN
else:                 NEUTRAL
```

**Score** (piecewise sum)

```
base         = {1: 0.30,  2: 0.50,  >=3: 0.65 + 0.05 × saturate}
weight_bonus = 0.20 × min(1, sum_w / 3)
mag_bonus    = 0.10 if magnitude_count > 0 else 0
score        = clip(base + weight_bonus + mag_bonus, 0, 1)
```

**Special rules**
- No baseline warmup (X always has `baseline_ready=1.0`)
- On credibility demote, confidence 0.7
- If `mock_spike`, append `MOCK_SPIKE` to reason

> ⚠️ **As of 2026-04-21, production = MockXCollector**. Zero real X posts.
> So the formulas above currently operate over only the 5 mock samples.
> Turn on EVT-1.3 (X API Basic) for real data.

**Threshold tuning guide (once real data is on)**
| Symptom | Fix |
|---|---|
| 1-account chatter trips WATCH (FP) | `accounts_watch` 1 → 2 |
| Strong post from a single major account (e.g. WhaleAlert) is NORMAL | Lower the weight floor or add a single-account override |

---

## 2. System-level: Fusion engine

Location: `src/anomaly_detection/core/fusion_engine.py` → `def fuse(...)`

Every cycle, four `ChannelSignal`s become **one `FusedAnomalyEvent`**.
**Pure function** — no state, same input always produces the same output.

### Step 1: `tier_floor` (the strongest channel is the base)

```
tier_floor = max(per_channel_tiers)
```

> If one channel reaches EMERGENCY but the others are quiet, we still do
> **not** reject it. A single strong signal is preserved — see
> architecture §5.4.2.

### Step 2: Corroboration boost (+1 step)

If ≥2 channels agree on direction, step up one tier.

```
agree_count = | { v ∈ channels : v.tier >= WATCH AND v.direction == direction* } |
direction*  = whichever of UP / DOWN has more votes (NEUTRAL excluded; tied = no consensus)

if agree_count >= 2 AND tier_floor != NORMAL:
    state ← tier_up(tier_floor)         # NORMAL→WATCH→RISK_OFF→EMERGENCY (cap)
else:
    state ← tier_floor
```

> NEUTRAL directions are excluded — without direction, corroboration is meaningless.
> Already at EMERGENCY → cap (cannot rise further).

### Step 3: `fused_score` (secondary, NOT used to decide state)

**Noisy-OR**:

```
eff_weight_i = config.weight_i × confidence_i × health_i        ∈ [0,1]
p_i          = score_i × eff_weight_i                           ∈ [0,1]
fused_score  = 1 - Π_i (1 - p_i)                                ∈ [0,1]
```

> Any single strong channel pulls the fused score up. NORMAL / None channels have
> score=0 → multiplied as `(1-0)=1` (identity) so they vanish automatically.
> **Current weights are 1.0 for all 4 channels (default).**
> P9 will tune per-channel weights (e.g. real-data channels 1.0, mock 0.5).

### Step 4: state → action

| state | DeliveryTier | Email | Telegram | RecommendedAction |
|---|---|---|---|---|
| NORMAL | none | — | — | NO_ACTION |
| WATCH | digest | 06:00 daily | — | MONITOR |
| RISK_OFF | realtime | immediate | — | REDUCE_RISK |
| EMERGENCY | urgent | immediate | immediate | EXIT_OR_HEDGE |

> Definitions live in `core/schemas.py` → `class DeliveryTier`, `class RecommendedAction`.

---

## 3. Cheat-sheet (one-page summary)

| Channel | # Detectors | Single-detector EMERGENCY allowed? | Special modulator | Direction |
|---|---|---|---|---|
| **CME** | 2 (vol_z_v1, price_jump_v1) | ✅ vol_z 8+ AND pj_5m 0.5%+ | none | NEUTRAL fixed |
| **Hyperliquid** | 4 (vol_z, insider, new_whale, cluster) | ❌ generally needs ≥2 RISK_OFF+ / ✅ new_whale and cluster solo allowed | panic_filter (demote) | cluster > new_whale > price > OI/funding |
| **Polymarket** | 5 (vol_burst, odds_gap, cusum, directional, wallet_conc) | ❌ needs ≥2 RISK_OFF+ (hotfix v0.3.2) | wallet ±1 (BOOST/DAMP) | directional > cusum > imbalance |
| **X** | 1 rule (account/magnitude AND) | ✅ n≥3 AND magnitude≥1 | credibility floor (demote) | buy_count vs sell_count |

---

## 4. Five facts worth memorising

1. **Channel tier** = OR(detectors). Polymarket / HL gate single-detector hypersensitivity with the EMERGENCY guard.
2. **System state** = `tier_floor` (max) + corroboration boost (≥2 channels agree → +1).
3. **`fused_score`** = noisy-OR. **For audit / UI only — never used to decide state.**
4. **Boost cap = EMERGENCY.** Already EMERGENCY → cannot go higher.
5. **NEUTRAL direction** is excluded from corroboration counts — no direction, no meaning.

---

## 5. Debugging quick-checklist

Order of checks for "why didn't this alert fire?" / "why did this alert fire?":

1. **Did raw events arrive per channel?** → check `channels.running` in the `/snapshot` endpoint.
2. **Were features computed?** → look at each channel's features dict (especially `baseline_ready`).
3. **What tier did the channel detector reach?** → ChannelSignal's `fired_detectors` + `reason_codes`.
4. **Did a modulator demote / boost?** → look for `PANIC_FILTER_DOWNGRADED`, `WC_BOOST`, `EMERGENCY_GUARD_DOWNGRADED`, `LOW_CREDIBILITY_DEMOTED` markers.
5. **Did fusion boost?** → `FusedAnomalyEvent.boost_applied`.
6. **Did AlertRouter block on cooldown?** → logs from `alerts/cooldown.py`.

---

## 6. Change history

| Date | Version | Change |
|---|---|---|
| 2026-04-21 | v1.0 | Initial version (after the 4-channel detector + fusion formulas were locked) |

> Update this doc whenever you change a threshold. During PR review, confirm the doc was updated alongside the code.
