# Truth Social Market-Moving Events — Reference v1

> **Source**: User-curated (`TruthSocial_events.md`) + verbatim validation planned during Step 2 backfill
> **Last updated**: 2026-05-15 (PT)
> **Scope**: Cases between 2025-01-20 and 2026-05-15 where a Truth Social post was explicitly a market-moving catalyst
> **Used by**: Few-shot reference DB for Channel 5 (Truth Social) LLM scoring. When new posts arrive, they are
>   compared semantically against these references to compute a market-impact score (0–10).

---

## 0. Purpose & Conventions

### Why we maintain a separate v1
- The user draft (`TruthSocial_events.md`) is narrative-rich but the schema varies per event, which makes it
  hard to inject directly into an LLM prompt.
- v1 aligns every event under the same schema (YAML frontmatter + unified table) so that the LLM receives a
  consistent few-shot signal when scoring new posts.
- The original post text is cross-linked via `post_ids` to `TruthSocial_events_raw.md` (Step 2 backfill output).

### Schema (shared by every event)

```yaml
event_id:               # YYYY-MM-DD_slug — unique within this file
posted_at_pt:           # User timezone (Pacific)
posted_at_et:           # US-market reference
posted_at_utc:          # Collector key
post_ids:               # Matched from raw.jsonl after Step 2 backfill — initially [TBD]
author:                 # realDonaldTrump (effectively the only author at this stage)
truth_social_isolated:  # true = Truth Social was the sole catalyst; false = simultaneous with Oval Office/AF1, etc.
news_dump_pattern:      # Posted Fri 16:00+ ET or weekend pre-dawn?
topic_tags:             # [tariff, china, iran, powell, ...]
market_impact_score:    # 0–10, human-curated
horizon:                # "intraday" / "1-3d" / "5d+"
confidence:             # high/medium/low — how clear the causal link is
insider_concern:        # Discussed in congressional hearings / SEC mentions?
```

### Score rubric (0–10)

| Score | Criteria (approximate) |
|---|---|
| 9–10 | Single post moves S&P 500 ±2%+ or a sector index ±5%+. Referenced across all channels |
| 7–8 | S&P 500 ±1–2% or single name ±5%+. Broad media coverage |
| 5–6 | S&P 500 ±0.5–1% or sector-limited impact. Catalyst can be isolated |
| 3–4 | Minor impact (~0.5%) — market reaction is dampened after TACO-trade learning |
| 1–2 | Almost no impact — already discounted by the market as noise |
| 0 | No impact / not measurable |

### Confidence guidance
- **high**: Major outlets (CNBC/Reuters/Bloomberg) directly cite the Truth Social post + accurate timing
- **medium**: Market reaction is measurable but combined with another catalyst (Oval Office remarks, etc.)
- **low**: Anecdotal, intraday timestamp unclear, or backfill required

---

## 1. Top 20 Priority Index (Step 2 backfill order)

Sorted from highest impact down — Step 2's `truth_social_backfill.py` raw-dumps in this order.
`P` column = priority (1 = highest).

| P  | Event ID | Date (PT) | Topic | S&P | NASDAQ | SOX | Score | Conf |
|----|----------|-----------|-------|-----|--------|-----|-------|------|
| 1  | 2025-04-09_buy_then_pause           | 2025-04-09 06:37 PDT     | tariff/china/pause     | +9.52%  | +12.16% | +18.8% | **10** | high |
| 2  | 2025-04-02_liberation_day           | 2025-04-02 ~12:50 PDT    | reciprocal-tariff      | -4.84%  | -5.97%  | -9.9%  | **10** | high |
| 3  | 2025-10-10_china_rare_earth         | 2025-10-10 (intraday)    | china/rare-earth       | -2.71%  | -3.56%  | -6.3%  | **9**  | high |
| 4  | 2025-05-12_china_total_reset        | 2025-05-11 (Sun PT)      | china/deal             | +3.26%  | +4.35%  | +7.0%  | **9**  | high |
| 5  | 2025-06-21_iran_fordow_strike       | 2025-06-21 ~16:50 PDT    | iran/military          | -0.4%   | -0.7%   | -0.5%  | **8**  | high |
| 6  | 2025-06-23_iran_ceasefire           | 2025-06-23 ~15:02 PDT    | iran/ceasefire         | +1.11%  | +1.43%  | +1.8%  | **8**  | high |
| 7  | 2025-05-23_apple_eu_twin            | 2025-05-23 05:47 PDT     | tariff/apple/eu        | -0.67%  | -1.00%  | -2.0%  | **8**  | high |
| 8  | 2025-04-21_powell_too_late          | 2025-04-21 (intraday)    | powell/fed             | -2.36%  | -2.55%  | -2.8%  | **8**  | high |
| 9  | 2025-12-08_nvda_h200_china          | 2025-12-08 (intraday)    | nvda/china/export      | +0.50%  | +0.95%  | +1.9%  | **7**  | high |
| 10 | 2026-02-28_iran_second_strike       | 2026-02-28 ~23:30 PT 2/27| iran/military          | -1.4%   | -1.8%   | -2.3%  | **8**  | high |
| 11 | 2026-03-23_iran_5day_pause          | 2026-03-23 (morning PT)  | iran/pause             | +1.1%   | +1.4%   | +1.6%  | **7**  | high |
| 12 | 2025-06-05_musk_crazy               | 2025-06-05 (intraday)    | tesla/musk             | -0.3%   | -0.5%   | n/a    | **7**  | high (TSLA only) |
| 13 | 2025-07-30_korea_deal               | 2025-07-30 (intraday)    | korea/deal             | +0.04%  | -0.27%  | +0.5%  | **6**  | high |
| 14 | 2026-02-20_scotus_ieepa_section122  | 2026-02-20 (afternoon)   | court/tariff           | +2.1%   | +2.8%   | +3.5%  | **8**  | medium |
| 15 | 2026-04-21_trump_iran_fractured     | 2026-04-21 (PT)          | iran                   | varies  | varies  | varies | **7**  | high |
| 16 | 2026-04-17_hormuz_open              | 2026-04-17 (PT)          | iran/oil               | varies  | varies  | varies | **7**  | high |
| 17 | 2026-01-21_greenland_tonedown       | 2026-01-21 06:10 PT      | greenland/eu           | +1.0%   | +1.2%   | +1.4%  | **6**  | medium |
| 18 | 2025-09-27_powell_youre_fired       | 2025-09-27 (Sat)         | powell/fed             | -0.2%   | -0.3%   | n/a    | **4**  | medium |
| 19 | 2026-01-03_maduro_arrest_signal     | 2026-01-03 (PT)          | venezuela/oil          | +1.2%   | +0.9%   | +0.4%  | **6**  | low (post unconfirmed) |
| 20 | 2025-02-01_mexico_canada_tariff     | 2025-02-01 (Sat)         | tariff/canada/mexico   | -0.8%   | -1.2%   | -2.4%  | **7**  | high |

> **Step 2 guide**: Backfill with a ±1-day window around the `Date (PT)` above. The event_id should match the directory name of the `truth_social_backfill.py --out` path (e.g., `--out data/anomaly/truthsocial/raw/2025-04-09_buy_then_pause/`).

---

## 2. Phase 1 — Early Tariff Threats (2025-01-20 ~ 2025-03-31)

### 2.1 Mexico/Canada/China IEEPA Tariff Confirm (2025-02-01, Sat)

```yaml
event_id: 2025-02-01_mexico_canada_tariff
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-02-01 (Sat, intraday)"
posted_at_et: "2025-02-01 (Sat, intraday)"
posted_at_utc: TBD
post_ids: ["113934520197790682", "113934450227067577", "113931044424714413"]
author: realDonaldTrump
truth_social_isolated: false   # Simultaneous with the White House factsheet + EO
news_dump_pattern: true        # Saturday announcement
topic_tags: [tariff, mexico, canada, china, ieepa, fentanyl]
market_impact_score: 7
horizon: "1-3d"
confidence: high
insider_concern: false
```

**Post gist**: Invocation of IEEPA → 25% on Mexico/Canada, 10% on China. Fentanyl-based justification.

**Trump Truth Social Posts (verbatim)**:
- **Feb 1, 5:42 PM ET** (official announcement): *"Today, I have implemented a 25% Tariff on Imports from Mexico and Canada (10% on Canadian Energy), and a 10% additional Tariff on China. This was done through the International Emergency Economic Powers Act (IEEPA) because of the major threat of illegal aliens and deadly drugs killing our Citizens, including fentanyl. We need to protect Americans, and it is my duty as President to ensure the safety of all."*
- **Feb 1, 6:21 PM ET**: *"TARIFFS!"*
- **Feb 2, 8:26 AM ET** (Canada follow-up): *"We pay hundreds of Billions of Dollars to SUBSIDIZE Canada. Why? There is no reason. We don't need anything they have. We have unlimited Energy, should make our own Cars, and have more Lumber than we can ever use. Without this massive subsidy, Canada ceases to exist as a viable Country."*
- **Feb 2, 8:09 AM ET** (defensive): *"The 'Tariff Lobby,' headed by the Globalist, and always wrong, Wall Street Journal, is working hard to justify Countries like Canada, Mexico, China, and too many others to name, continue the decades long RIPOFF OF AMERICA..."*

**Market reaction (Mon 2/3)**:

| Asset    | Dir | Mag    | Window |
|----------|-----|--------|--------|
| S&P 500  | ↓   | -0.8%  | 1d (intraday -2% recovered) |
| NASDAQ   | ↓   | -1.2%  | 1d |
| NVDA     | ↓   | -2.8%  | 1d |
| AVGO     | ↓   | -2.4%  | 1d |

**Notes**:
- Saturday announcement = first instance of the "news dump" pattern. Template for all subsequent tariff announcements.
- Intraday recovery on Mexico deferral announcement.

---

### 2.2 Colombia 25% / 50% Emergency Tariff (2025-01-26, Sun)

```yaml
event_id: 2025-01-26_colombia_emergency
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-01-26 (Sun, intraday)"
posted_at_utc: TBD
post_ids: ["113896070273857964"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: true
topic_tags: [tariff, colombia, immigration]
market_impact_score: 3
horizon: "intraday"
confidence: medium
insider_concern: false
```

**Post gist**: Colombia refused US military deportation flights → 25% emergency tariff (rising to 50% within a week), visa sanctions, financial penalties. *"These measures are just the beginning."*

**Market reaction (Mon 1/27)**: S&P -0.3% (risk-off cleared quickly after Petro capitulated).

**Notes**: Start of the pattern of using Truth Social as a policy-announcement channel.

---

### 2.3 March 4 Tariff Confirm (2025-02-27)

```yaml
event_id: 2025-02-27_march4_confirm
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-02-27 (Thu morning PT)"
posted_at_utc: TBD
post_ids: ["114076153524132682"]
author: realDonaldTrump
truth_social_isolated: false   # Combo with NVIDIA earnings
news_dump_pattern: false
topic_tags: [tariff, china, mexico, canada]
market_impact_score: 7
horizon: "1-3d"
confidence: medium             # Hard to isolate from NVDA earnings
insider_concern: false
```

**Post gist**: *"...the proposed TARIFFS scheduled to go into effect on MARCH FOURTH will, indeed, go into effect, as scheduled."* + additional 10% on China.

**Market reaction (same day 2/27)**:

| Asset    | Dir | Mag    | Notes |
|----------|-----|--------|-------|
| S&P 500  | ↓   | -1.6%  | |
| NASDAQ   | ↓   | -2.8%  | |
| NVDA     | ↓   | **-8.5%** | NVIDIA earnings + tariff fears combo |

**Notes**: Hard to isolate Truth Social — the earnings catalyst is concurrent.

---

### 2.4 Steel/Aluminum 25% (2025-03-12)

```yaml
event_id: 2025-03-12_steel_aluminum_25
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-03-12"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: false   # Simultaneous Proclamation
news_dump_pattern: false
topic_tags: [tariff, steel, aluminum]
market_impact_score: 6
horizon: "1d"
confidence: medium
insider_concern: false
```

**Market reaction**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| S&P 500  | ↓   | -1.8%  |
| NASDAQ   | ↓   | -2.5%  |
| SOX      | ↓   | -3.2%  |
| CLF/X/NUE | ↑   | +5–10% (steel names higher) |

---

## 3. Phase 2 — Liberation Day Cluster (2025-04-02 ~ 04-09)

### 3.1 ★ Liberation Day Reciprocal Tariff (2025-04-02, Wed ~12:50 PDT)

```yaml
event_id: 2025-04-02_liberation_day
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-04-02 ~12:50 PDT"
posted_at_et: "2025-04-02 ~15:50 EDT"
posted_at_utc: "2025-04-02T19:50:00Z"
post_ids: ["114275641914968162", "114269118325764698", "114268708154085693", "114268044617916583"]                # "LIBERATION DAY IN AMERICA!" + country-by-country chart citations
author: realDonaldTrump
truth_social_isolated: false   # Concurrent with Rose Garden chart unveil
news_dump_pattern: false       # Right after market close (timing was deliberate)
topic_tags: [tariff, reciprocal, china, eu, vietnam, korea, japan, taiwan, india]
market_impact_score: 10
horizon: "5d+"
confidence: high
insider_concern: false
```

**Post gist**: *"LIBERATION DAY IN AMERICA!"* + country-by-country tariff rates: China 34%, EU 20%, Vietnam 46%, Taiwan 32%, Japan 24%, **Korea 25%**, India 26%, Cambodia 49%.

**Market reaction**:

| Date  | Asset    | Dir | Mag       | Notes |
|-------|----------|-----|-----------|-------|
| 4/2 AH| SPY      | ↓   | -2.2%     | after-hours |
| 4/2 AH| QQQ      | ↓   | -3.0%     | after-hours |
| 4/2 AH| AAPL     | ↓   | -7%       | after-hours |
| 4/3   | S&P 500  | ↓   | **-4.84%**| regular session |
| 4/3   | NASDAQ   | ↓   | **-5.97%**| regular session |
| 4/3   | SOX      | ↓   | **-9.9%** | worst single day |
| 4/3   | TSM      | ↓   | -7.2%     | |
| 4/3   | AVGO     | ↓   | -10.5%    | |
| 4/3   | NVDA     | ↓   | -7.8%     | |
| 4/3   | AMD      | ↓   | -8.9%     | |
| 4/3   | MU       | ↓   | -16%      | DRAM/HBM sell-off |

**Notes**:
- #1 in the user historical event series — cross-reference with `data/anomaly/historical_events/2025-04-09_liberation_day.md`.
- Truth Social cannot be isolated (concurrent with the Rose Garden chart reveal) — but the market explicitly cited the Truth Social post as the catalyst.

---

### 3.2 ★★★ "BE COOL / GREAT TIME TO BUY" + 90-day Pause (2025-04-09, Wed)

```yaml
event_id: 2025-04-09_buy_then_pause
posted_at_pt: "2025-04-09 06:37 PDT"
posted_at_et: "2025-04-09 09:37 EDT"
posted_at_utc: "2025-04-09T13:37:00Z"
post_ids: ["114308272725981913", "114308258545250117", "114309144289505174"]      # 9:33 / 9:37 / 13:18 ET — three posts
author: realDonaldTrump
truth_social_isolated: true    # Truth Social was the primary catalyst for the market reaction
news_dump_pattern: false       # Right after the regular session open
topic_tags: [tariff, china, 90-day-pause, market-moving, insider-concern]
market_impact_score: 10
horizon: "5d+"
confidence: high
insider_concern: true          # Congressional hearings, Schiff formal investigation request
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
```

**Post excerpts** (chronological):
1. **09:33 ET**: *"BE COOL! Everything is going to work out well. The USA will be bigger and better than ever before!"*
2. **09:37 ET**: *"THIS IS A GREAT TIME TO BUY!!! DJT"* ← the most famous post
3. **~13:18 ET**: 90-day reciprocal-tariff pause announced (10% baseline maintained; China raised to 145%)

**Market reaction**:

| Asset       | Dir | Mag       | Window | Notes |
|-------------|-----|-----------|--------|-------|
| S&P 500     | ↑   | **+9.52%**| 1d     | Largest one-day gain since October 2008 |
| NASDAQ      | ↑   | **+12.16%**| 1d    | Largest one-day gain since January 2001 |
| Dow         | ↑   | +7.87% (+2,962 pts)| 1d | |
| SOX         | ↑   | **+18.8%**| 1d     | Largest one-day gain in the index's history |
| NVDA        | ↑   | +18.7%    | 1d     | |
| AMD         | ↑   | +24%      | 1d     | |
| AVGO        | ↑   | +15%      | 1d     | |
| TSM         | ↑   | +14%      | 1d     | |
| MU          | ↑   | +18%      | 1d     | |
| DJT (Trump Media) | ↑ | +22.7%  | 1d     | Dual meaning of the "DJT" sign |

**Why this scored 10 / Insider concern**:
- 9:37 "BUY" → 90-day pause announced ~4 hours later → large same-day profits for buyers
- AOC: *"Any member of Congress who purchased stocks in the last 48 hours should probably disclose..."*
- Sen. Adam Schiff: Formal investigation request
- Lutnick (CNBC): *"Bessent and I sat with the President while he wrote one of the most extraordinary Truth posts"* — strong implication that the BUY post was written when the pause decision had already been made
- Richard Painter (former White House ethics counsel): *"The people who bought when they saw that post made a lot of money."*

**Cross-event**:
- Absolute counter-catalyst to the April 2–4 selloff (5-day S&P -12%)
- Core reference for user historical event #1 (`2025-04-09_liberation_day.md`)

---

### 3.3 Liberation Day continued selloff + China retaliation (2025-04-03 ~ 04)

```yaml
event_id: 2025-04-04_china_retaliation
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-04-04 (intraday)"
posted_at_utc: TBD
post_ids: ["114279756371714617"]
author: realDonaldTrump
truth_social_isolated: false   # Combo with China announcement
news_dump_pattern: false
topic_tags: [tariff, china, retaliation, panic-rally-attempt]
market_impact_score: 9
horizon: "1-3d"
confidence: high
insider_concern: false
```

**Trump posts**:
- *"MY POLICIES WILL NEVER CHANGE!"*
- *"This is a great time to get rich!"* ← prelude to April 9 "BUY"

**Market reaction (4/4)**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| S&P 500  | ↓   | -5.97% |
| NASDAQ   | ↓   | -5.82% |

**Notes**: 2-day decline (4/3 + 4/4) was the 5th worst since WWII.

---

### 3.4 Fake News "90-day pause" Denial (2025-04-07, Mon)

```yaml
event_id: 2025-04-07_fake_news_denial
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-04-07 (intraday)"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [tariff, fake-news, intraday-volatility]
market_impact_score: 7
horizon: "intraday"
confidence: high
insider_concern: false
```

**Context**: Fake *"Hassett confirms 90-day pause"* headline → S&P +8% intraday → Trump Truth Social denial → sharp drop.

**Market impact**: ~$2.4T market cap swing (intraday). Truth Social denial was the catalyst.

---

## 4. Phase 3 — Tariff Reprieve & Powell (2025-04-10 ~ 2025-05-31)

### 4.1 China 145% Tariff Confirm (2025-04-10)

```yaml
event_id: 2025-04-10_china_145
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-04-10 (intraday)"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [tariff, china, hangover]
market_impact_score: 7
horizon: "1d"
confidence: high
insider_concern: false
```

**Post gist**: Clarified the China cumulative tariff at 145% ("125% reciprocal + 20% fentanyl").

**Market reaction**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| S&P 500  | ↓   | -3.46% |
| NASDAQ   | ↓   | -4.31% |
| SOX      | ↓   | -8%    |
| NVDA     | ↓   | -6%    |

---

### 4.2 ★ Powell "Mr Too Late" Attack (2025-04-21, Easter Mon)

```yaml
event_id: 2025-04-21_powell_too_late
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-04-21 (intraday)"
posted_at_utc: TBD
post_ids: ["114352766082542122", "114376239725335883"]           # 4/17 + 4/21 two posts
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [powell, fed, independence-risk, dollar]
market_impact_score: 8
horizon: "1-3d"
confidence: high
insider_concern: false
```

**Posts**:
- 4/17: *"Powell's termination cannot come fast enough."*
- 4/21: *"There can almost be no inflation, but there can be a SLOWING of the economy unless Mr Too Late, a major loser, lowers interest rates, NOW."*

**Market reaction (4/21)**:

| Asset       | Dir | Mag    | Notes |
|-------------|-----|--------|-------|
| S&P 500     | ↓   | -2.36% | Powell-firing risk + Fed independence |
| NASDAQ      | ↓   | -2.55% | |
| USD index   | ↓   | -1.0%  | Safe-asset avoidance |
| Gold        | ↑   | +2.9%  | |
| SOX         | ↓   | -2.8%  | |
| NVDA        | ↓   | -4.5%  | |

---

### 4.3 UK "Economic Prosperity Deal" (2025-05-08)

```yaml
event_id: 2025-05-08_uk_deal
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-05-08 (intraday)"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: false   # Combo with White House Rose Garden announcement
news_dump_pattern: false
topic_tags: [trade-deal, uk]
market_impact_score: 5
horizon: "1d"
confidence: high
insider_concern: false
```

**Market reaction**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| S&P 500  | ↑   | +0.6%  |
| NASDAQ   | ↑   | +1.1%  |

---

### 4.4 ★ China "TOTAL RESET" Deal (2025-05-11, Sun night PT)

```yaml
event_id: 2025-05-12_china_total_reset
posted_at_pt: "2025-05-11 (Sun evening PT)"
posted_at_et: "2025-05-11 (Sun evening ET)"
posted_at_utc: TBD
post_ids: ["114490604608562822", "114491534347862682"]                    # Truth Social verbatim unverified — channel was the Geneva joint statement
author: realDonaldTrump
truth_social_isolated: false   # Geneva joint statement (Bessent/Greer) was the primary catalyst
news_dump_pattern: true        # Sunday night
topic_tags: [china, deal, tariff-cut, geneva]
market_impact_score: 9
horizon: "5d+"
confidence: medium             # Truth Social is not isolated (Geneva joint statement + EO)
insider_concern: true          # Consecutive timing with the May 13–16 Saudi/Qatar/UAE trip
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
```

**Attribution note**: The user's *"TOTAL RESET... BIG PROGRESS!!!"* Truth Social citation could not be verbatim cross-checked. The 5/11–5/12 scraped posts do not contain a direct verbatim mention of the China deal (prescription drugs, Edan hostage release, Qatar 747, etc.). The actual market catalyst is presumed to be the **Geneva joint statement (US/China, Bessent/Greer announcement)** + **EO 14298 (effective 5/12)**. Trump's Truth Social posts are at the level of follow-up broader-market optimism (e.g., *"IN JUST THREE MONTHS, TRILLIONS OF DOLLARS..."*, 5/11 2:26 PM ET).

**Market reaction (Mon 5/12)**:

| Asset    | Dir | Mag    | Notes |
|----------|-----|--------|-------|
| S&P 500  | ↑   | +3.26% | 2nd-largest one-day gain of the year |
| NASDAQ   | ↑   | +4.35% | |
| SOX      | ↑   | +7.0%  | NVDA +5.4%, AMD +5.2%, AVGO +6.3%, TSM +4.4% |

**Insider concern**: Repeat of the 4/9 "BUY" pattern — 5/12 RESET → 5/13–16 Middle East trip. Suspected deliberate timing.

---

### 4.5 ★ Apple 25% + EU 50% Twin Posts (2025-05-23, Fri pre-market)

```yaml
event_id: 2025-05-23_apple_eu_twin
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-05-23 05:47 PDT (Apple) / 06:23 PDT (EU)"
posted_at_et: "2025-05-23 08:47 EDT / 09:23 EDT"
posted_at_utc: "2025-05-23T12:47:00Z / 13:23:00Z"
post_ids: ["114556968834547173", "114556874484491575"]
author: realDonaldTrump
truth_social_isolated: true    # Sole pre-market catalyst
news_dump_pattern: false       # Friday pre-market
topic_tags: [tariff, apple, eu, twin-post]
market_impact_score: 8
horizon: "intraday + 1-3d"
confidence: high
insider_concern: false
```

**Post excerpts**:
1. **08:47 ET (Apple)**: *"I have long ago informed Tim Cook of Apple that I expect their iPhone's that will be sold in the United States of America will be manufactured and built in the United States, not India, or anyplace else. If that is not the case, a Tariff of at least 25% must be paid by Apple to the U.S."*
2. **09:23 ET (EU)**: *"The European Union, which was formed for the primary purpose of taking advantage of the United States on TRADE... Our discussions with them are going nowhere! Therefore, I am recommending a straight 50% Tariff on the European Union, starting on June 1, 2025."*

**Market reaction**:

| Asset    | Dir | Mag    | Notes |
|----------|-----|--------|-------|
| Dow futures | ↓ | -600 pts | pre-market |
| S&P 500  | ↓   | -0.67% | close, weekly -1.7% |
| NASDAQ   | ↓   | -1.00% | |
| AAPL     | ↓   | -3.0%  | Single name dragged the Dow |
| ASML     | ↓   | -2.5%  | |
| NXPI     | ↓   | -2.2%  | |
| NVDA     | ↓   | -1.2%  | |
| VIX      | ↑   | +23% (intraday) → +8% close | |
| STOXX 600| ↓   | -1.7%  | Europe |
| DAX      | ↓   | -2.4%  | Europe |
| CAC      | ↓   | -2.2%  | Europe |

---

### 4.6 EU Tariff "TACO" Reversal (2025-05-25, Sun)

```yaml
event_id: 2025-05-25_eu_extension
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-05-25 (Sun)"
posted_at_utc: TBD
post_ids: ["114570775887793036"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: true
topic_tags: [tariff, eu, taco-trade]
market_impact_score: 6
horizon: "1-3d"
confidence: high
insider_concern: false
```

**Post gist**: *"I have agreed to the extension — July 9, 2025"* (after a call with Ursula von der Leyen).

**Market reaction (Tue 5/27)**: S&P +2.05%, NASDAQ +2.5%, SOX +3.5% — **start of the "TACO trade" meme**.

---

## 5. Phase 4 — Iran/Israel 12-Day War (2025-06)

### 5.1 Steel/Aluminum 25→50% (2025-06-04)

```yaml
event_id: 2025-06-04_steel_50
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-06-04"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: false
news_dump_pattern: false
topic_tags: [tariff, steel, aluminum]
market_impact_score: 5
horizon: "1d"
confidence: high
insider_concern: false
```

**Market reaction**: S&P -0.6%, NASDAQ -0.5%. CLF/X/STLD/NUE +20–25%. Whirlpool/GM/F weak.

---

### 5.2 ★ "Elon has gone CRAZY!" (2025-06-05)

```yaml
event_id: 2025-06-05_musk_crazy
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-06-05 (intraday)"
posted_at_utc: TBD
post_ids: ["114632206992330264"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [tesla, musk, feud, single-stock]
market_impact_score: 7         # Broad impact small, but TSLA single -14.3%
horizon: "intraday + 1d"
confidence: high
insider_concern: false
```

**Posts**: *"Elon has gone CRAZY!"*, *"easiest money ever saved... terminate Elon's Governmental Subsidies and Contracts."*

**Market reaction**:

| Asset    | Dir | Mag      | Notes |
|----------|-----|----------|-------|
| TSLA     | ↓   | **-14.3%**| Market-cap loss ~$152B — among the largest single-day losses in company history |
| S&P 500  | ↓   | -0.3%    | Limited broad impact |
| NASDAQ   | ↓   | -0.5%    | |

---

### 5.3 ★ Iran Fordow/Natanz/Esfahan Strike (2025-06-21, Sat ~16:50 PDT)

```yaml
event_id: 2025-06-21_iran_fordow_strike
verification_level: double_verified  # trumpstruth.org verbatim + narrative cross-check passed. Cannot escalate to triple due to absence of a Mastodon API JSON snapshot in the Wayback Machine (checked 2026-05-15)
posted_at_pt: "2025-06-21 ~16:50 PDT"
posted_at_et: "2025-06-21 ~19:50 EDT"
posted_at_utc: "2025-06-21T23:50:00Z"
post_ids: ["114724016661152553", "114724035571020048"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: true        # Saturday evening
topic_tags: [iran, military, news-dump, geopolitics]
market_impact_score: 8
horizon: "1-3d"
confidence: high
insider_concern: true          # 6/17 "Iran has 2 weeks" → strike; reports of unusual options volume
```

**Post gist**: *"We have completed our very successful attack on the three Nuclear sites in Iran, including Fordow, Natanz, and Esfahan..."*

**Market reaction (Mon 6/23)**:

| Asset    | Dir | Mag    | Notes |
|----------|-----|--------|-------|
| S&P futures | ↓ | -1.2%  | early Mon open |
| S&P 500  | ↓   | -0.4%  | Close (settled once Hormuz was not blockaded) |
| NASDAQ   | ↓   | -0.7%  | |
| Brent    | ↑   | +5.7%  | early Mon |
| LMT/RTX/NOC | ↑ | +1–2% | Defense |
| XOM/CVX  | ↑   | +2%    | Energy |

**Pattern**: Saturday-evening announcement → news-dump effect limits Monday gap.

---

### 5.4 ★ Iran/Israel Ceasefire (2025-06-23, Mon ~15:02 PDT)

```yaml
event_id: 2025-06-23_iran_ceasefire
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-06-23 ~15:02 PDT"
posted_at_et: "2025-06-23 ~18:02 EDT"
posted_at_utc: "2025-06-23T22:02:00Z"
post_ids: ["114735830758023638", "114734934153569653"]           # 18:02 ET + midnight follow-up
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false       # Right after the regular session close
topic_tags: [iran, ceasefire, oil-reverse, risk-on]
market_impact_score: 8
horizon: "1-3d"
confidence: high
insider_concern: true          # Unusual WTI put / S&P call volume (CFTC)
```

**Post gist**: *"Israel and Iran have agreed to a COMPLETE CEASEFIRE."* Post-midnight follow-up: *"It's a big day for World Peace!..."*

**Market reaction (Tue 6/24)**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| S&P 500  | ↑   | +1.11% (intraday +1.5%) |
| NASDAQ   | ↑   | +1.43% |
| Brent    | ↓   | -13% (over 2 days) |
| SOX      | ↑   | +1.8%  |
| NVDA     | ↑   | +2.6%  |
| Airlines  | ↑   | +5–7%  |

---

## 6. Phase 5 — Trade Deals & Court Rulings (2025-07 ~ 12)

### 6.1 Vietnam Deal (2025-07-02)

```yaml
event_id: 2025-07-02_vietnam_deal
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-07-02 (intraday)"
posted_at_utc: TBD
post_ids: ["114784170652465525", "114784098546698642"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [trade-deal, vietnam, supply-chain]
market_impact_score: 5
horizon: "1d"
confidence: high
insider_concern: false
```

**Post gist**: *"I have just made a Trade Deal with Vietnam"* — 20% tariff (cut from 46%).

**Market reaction**: S&P +0.5%, SOX +1.2%. NXPI/MCHP +2% (Vietnam packaging exposure).

---

### 6.2 Korea 25% "Tariff Letter" (2025-07-07~08)

```yaml
event_id: 2025-07-08_korea_letter
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-07-07~08"
posted_at_utc: TBD
post_ids: ["114820873191783674", "114820563898758883", "114820908713657769", "114819199532902807", "114812490799920916"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [tariff, korea, letter-format]
market_impact_score: 5
horizon: "1d"
confidence: high
insider_concern: false
```

**Post gist**: *"Dear Mr. President"* — 25% Korea tariff threat.

**Market reaction**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| KOSPI    | ↓   | -1.5%  |
| SK Hynix ADR | ↓ | -3%   |
| Samsung ADR  | ↓ | -2.2% |
| US broad | ~   | minimal |

---

### 6.3 NVDA H20 China Export Reopen (2025-07-15)

```yaml
event_id: 2025-07-15_h20_china_reopen
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-07-15 (intraday)"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [nvda, china, export, semiconductor]
market_impact_score: 6
horizon: "1d"
confidence: high
insider_concern: false
```

**Post gist**: *"We are letting Nvidia sell its H20 chip to China"*.

**Market reaction**: NVDA +4.0%, AMD +6.4%, SOX +1.7%, S&P +0.4%. **Sole catalyst for the semiconductor sector.**

---

### 6.4 Japan Deal $550B (2025-07-23)

```yaml
event_id: 2025-07-23_japan_deal
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-07-23"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [trade-deal, japan, auto]
market_impact_score: 6
horizon: "1d"
confidence: high
insider_concern: false
```

**Post gist**: *"We just completed a massive Deal with Japan..."*

**Market reaction**: S&P +0.78% (new high), NASDAQ +0.61%. **TM +13.9%, HMC +11%** (single-day double-digit moves in Japanese auto ADRs).

---

### 6.5 ★ Korea Deal (2025-07-30, core user interest)

```yaml
event_id: 2025-07-30_korea_deal
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-07-30 (intraday)"
posted_at_utc: TBD
post_ids: ["114944494894008041"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [trade-deal, korea, semiconductor, memory, display]
market_impact_score: 6
horizon: "1-3d"
confidence: high
insider_concern: false
```

**Post gist**: *"...Full and Complete Trade Deal with the Republic of Korea... $350 Billion Dollars for Investments... [semis & pharma no less favorable than any other country]..."*

**Deal terms**:
- 15% auto tariff (down from 25%)
- **Semiconductors & pharma "no less favorable than any other country"** ← core protection for Korean memory
- Steel/aluminum/copper held at 50%
- $100B LNG purchase commitment
- $150B shipbuilding investment

**Market reaction**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| KOSPI    | ↑   | +0.7%  |
| Samsung Electronics | ↑ | +2.1%  |
| SK Hynix | ↑   | +3.5%  |
| S&P 500  | ~   | +0.04% |
| NASDAQ   | ↓   | -0.27% |
| SMH      | ↑   | +0.5%  |
| AVGO     | ↑   | +0.8%  |
| AMAT     | ↑   | +1.2%  |

**Notes**: The user's direct KR memory/display exposure is ring-fenced. Renegotiation risk under a future Section 122 framework remains.

---

### 6.6 BLS Jobs Data + Powell Pressure (2025-08-01)

```yaml
event_id: 2025-08-01_bls_rigged
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-08-01"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: false   # Composite market catalyst
news_dump_pattern: false
topic_tags: [bls, jobs, powell]
market_impact_score: 5
horizon: "1d"
confidence: low                # Hard to isolate
insider_concern: false
```

**Post gist**: BLS-revised data *"rigged"* + dismissal of the BLS Commissioner.

**Market reaction**: S&P -1.6%, NASDAQ -2.2% (combo catalyst; Truth Social cannot be isolated).

---

### 6.7 NVDA/AMD 15% Revenue Share (2025-08-08)

```yaml
event_id: 2025-08-08_15pct_revenue_share
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-08-08"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [nvda, amd, revenue-share, china]
market_impact_score: 4
horizon: "1d"
confidence: medium
insider_concern: false
```

**Post gist**: *"Nvidia and AMD will pay 15% of their China chip revenue to U.S. Treasury."*

**Market reaction**: NVDA -1.5%, AMD -1.8% (short-term), recovered the following week.

---

### 6.8 Powell "YOU'RE FIRED" AI Cartoon (2025-09-27, Sat)

```yaml
event_id: 2025-09-27_powell_youre_fired
posted_at_pt: "2025-09-27 (Sat)"
posted_at_et: "2025-09-27 (Sat ~22:14 ET, image series)"
posted_at_utc: TBD
post_ids: ["115279506720009564", "115279525606703136", "115279550586740869", "115279601478749277"]   # image-only candidates, Sept 27 22:14~22:38 ET
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: true
topic_tags: [powell, fed, ai-cartoon, image-only, low-impact]
market_impact_score: 4
horizon: "1d"
confidence: medium
insider_concern: false
verification_level: media_only_verified  # Image-only post — the fact that Trump posted an image series that day is confirmable, but trumpstruth.org has not yet processed the image descriptions
```

**Post gist**: AI-generated image series — Trump shouting *"YOU'RE FIRED!"* at Powell + Powell carrying a box of belongings. **Image-only post with no text body** (Truth Social 9/27 22:14–22:38 ET window, 4 candidates among a 17-post image-only series).

**Verbatim status**: Because these are image posts with no text, our text-based matcher cannot cross-check them. trumpstruth.org's detail page also shows "The site periodically processes attachment images and videos, so more information may be available soon" (image descriptions not yet indexed). A Wayback spot check in Phase D is needed.

**Market reaction (Mon 9/29)**: S&P -0.2%, USD -0.3% — market dismissed it as a cartoon.

---

### 6.9 ★ China Rare-Earth Retaliation "massive increase" (2025-10-10, Fri)

```yaml
event_id: 2025-10-10_china_rare_earth
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-10-10 (intraday)"
posted_at_et: "2025-10-10 (intraday)"
posted_at_utc: TBD
post_ids: ["115350455734003647"]           # Main + Xi-not-meeting follow-up
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [tariff, china, rare-earth, retaliation, semiconductor]
market_impact_score: 9
horizon: "1-3d"
confidence: high
insider_concern: false
```

**Posts**:
- *"We have been contacted by other Countries who are extremely angry at this great Trade hostility... massive increase of tariffs"*
- *"Now there seems to be no reason"* to meet Xi Jinping (APEC South Korea)

**Market reaction**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| S&P 500  | ↓   | **-2.71%** |
| NASDAQ   | ↓   | **-3.56%** |
| Dow      | ↓   | -1.90% (-878 pts) |
| SOX      | ↓   | **-6.3%** |
| NVDA     | ↓   | -4.9%  |
| AMD      | ↓   | -7.8%  |
| AVGO     | ↓   | -3.5%  |
| TSM      | ↓   | -5.8%  |
| MU       | ↓   | -7.0%  |
| VIX      | ↑   | +28%   |
| Brent    | ↓   | -4.2%  |

**Notes**: Worst day since April 10. Cross-reference with user historical event #2 (`2025-10-10_china_tariff_100.md`).

---

### 6.10 China TACO Reversal (2025-10-13, Mon)

```yaml
event_id: 2025-10-13_china_tonedown
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-10-11~13 weekend"
posted_at_utc: TBD
post_ids: ["115362196088273474"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: true
topic_tags: [tariff, china, taco-reversal]
market_impact_score: 7
horizon: "1d"
confidence: high
insider_concern: false
```

**Posts**: Weekend *"Don't worry about China"* — additional 100% tariff deferred until November 1.

**Market reaction (10/13)**: S&P +1.56%, NASDAQ +2.21%, SOX +3.6%.

---

### 6.11 APEC Xi/Trump Deal (2025-11-01)

```yaml
event_id: 2025-11-01_apec_deal
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2025-11-01 (Sat)"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: true
topic_tags: [china, apec, deal, rare-earth]
market_impact_score: 5
horizon: "1d"
confidence: high
insider_concern: false
```

**Post gist**: *"We have a deal"* — 10% tariff cut if China cooperates on fentanyl; rare-earths normalized for 1 year.

**Market reaction (Mon 11/3)**: S&P +0.7%, SOX +1.4% (NVDA +1.2%, AMD +2.1%).

---

### 6.12 ★ NVDA H200 China Export Approval (2025-12-08, Mon)

```yaml
event_id: 2025-12-08_nvda_h200_china
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2025-12-08 (intraday)"
posted_at_utc: TBD
post_ids: ["115686072737425841"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [nvda, china, export, semiconductor, h200]
market_impact_score: 7
horizon: "1-3d"
confidence: high
insider_concern: false
```

**Post gist**: *"...allow NVIDIA to sell their H200 product to approved customers in China. President Xi has responded positively. The United States will receive 25% of all sales..."*

**Market reaction**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| NVDA     | ↑   | +2.8% (intraday +4.5%) |
| AMD      | ↑   | +3.1%  |
| AVGO     | ↑   | +1.5%  |
| SOX      | ↑   | +1.9%  |
| NASDAQ   | ↑   | +0.95% |
| S&P 500  | ↑   | +0.5%  |

**Reversal**: Reports mid-December of China customs blocking → Lutnick: *"Nvidia has not sold a single H200 AI GPU to China"*.

---

## 7. Phase 6 — SCOTUS IEEPA, Iran II, 2026 (2026-01 ~ 05-15)

### 7.1 Maduro Arrest Signal (2026-01-03 ~ 01-05) — Open Question

```yaml
event_id: 2026-01-03_maduro_arrest_signal
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2026-01-03 (PT)"
posted_at_et: "2026-01-03 ~04:21 ET" # See user historical event #3 reference
posted_at_utc: "2026-01-03T09:21:00Z"
post_ids: ["115830428767897167", "115832088990838303", "115833880793462028"]
author: realDonaldTrump
truth_social_isolated: false   # Combo with the Maduro-capture announcement
news_dump_pattern: false
topic_tags: [venezuela, maduro, oil, geopolitics]
market_impact_score: 6
horizon: "1d"
confidence: low                # Hard to identify the specific market-moving Truth Social post
insider_concern: true          # Reference for insider analysis in the historical event
```

**Context**: T=0 in user historical event #3 (`2026-01-03_maduro_arrest.md`) = Trump's first Truth Social capture announcement (04:21 EST). Our scrape captured the exact verbatim text.

**Trump Truth Social Posts (verbatim, sequence)**:
- **Jan 3, 4:21 AM ET** (✨ official announcement, matches historical event T=0 exactly): *"The United States of America has successfully carried out a large scale strike against Venezuela and its leader, President Nicolas Maduro, who has been, along with his wife, captured and flown out of the Country. This operation was done in conjunction with U.S. Law Enforcement. Details to follow. There will be a News Conference today at 11 A.M., at Mar-a-Lago. Thank you for your attention to this matter! President DONALD J. TRUMP"*
- **Jan 3, 11:23 AM ET** (image caption): *"Nicolas Maduro on board the USS Iwo Jima."*
- **Jan 3, 6:59 PM ET** (follow-up, news citation): *"Democrat lawmaker Wasserman Schultz comes out in support of Trump's operation in Venezuela..."*

**Market reaction**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| S&P 500  | ↑   | +1.2%  |
| NASDAQ   | ↑   | +0.9%  |
| SOX      | ↑   | +0.4%  |
| Chevron  | ↑   | +5.1%  |
| Exxon    | ↑   | +2.2%  |
| Halliburton | ↑| +7.8%  |

**Verify in Step 2**: Exact first capture-post ID and body. Confirm the historical-event 04:21 ET timestamp.

---

### 7.2 Greenland Tone-Down (2026-01-21, 20 min before open)

```yaml
event_id: 2026-01-21_greenland_tonedown
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2026-01-21 11:27 PT"     # Per scrape verbatim (2:27 PM ET)
posted_at_et: "2026-01-21 14:27 ET"
posted_at_utc: "2026-01-21T19:27:00Z"
post_ids: ["115934734335579278"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false       # Mid-day market — re-evaluate intentional timing
topic_tags: [greenland, nato, rutte, tariff-defer]
market_impact_score: 6
horizon: "1d"
confidence: high
insider_concern: false          # 14:27 ET is mid-day — "20 minutes before open" attribution is wrong
```

**Trump Truth Social Post (verbatim, 2:27 PM ET)**: *"Based upon a very productive meeting that I have had with the Secretary General of NATO, Mark Rutte, we have formed the framework of a future deal with respect to Greenland and, in fact, the entire Arctic Region. This solution, if consummated, will be a great one for the United States of America, and all NATO Nations. Based upon this understanding, I will not be imposing the Tariffs that were scheduled to go into effect on February 1st. Additional discussions are being held concerning The Golden Dome as it pertains to Greenland. Further information will be made available as discussions progres..."*

**Attribution note**: The original note's CNN attribution "06:10 PT / 09:10 ET, 20 minutes before markets opened" does not match the scrape verbatim — the actual framework-deal post is the mid-day 14:27 ET one. Rather than moving the market directly "20 min before the open," it was a mid-day announcement following the NATO meeting.

**Market reaction**: S&P +1.0% (recovery after the previous trading day's worst day since October).

---

### 7.3 ★ SCOTUS IEEPA Ruling + Section 122 (2026-02-20, Fri)

```yaml
event_id: 2026-02-20_scotus_ieepa_section122
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2026-02-20 (afternoon PT)"
posted_at_et: "2026-02-20 (afternoon ET)"
posted_at_utc: TBD
post_ids: ["116105691693335080", "116104410806971686", "116104407604484915"]           # Heated reaction + Section 122 follow-up
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [court, ieepa, section-122, scotus]
market_impact_score: 8
horizon: "1-3d"
confidence: medium             # Market reaction split (Friday +, Monday -)
insider_concern: false
```

**Context**: SCOTUS 6-3 IEEPA tariffs illegal → $166B refund. After Trump's heated reaction he announced a 10% global tariff (150-day) under Section 122 (Trade Act 1974).

**Trump Truth Social Posts (verbatim, sequence)**:
- **Feb 20, 1:37 PM ET** (anger): *"To show you how ridiculous the opinion is, the Court said that I'm not allowed to charge even $1 DOLLAR to any Country under IEEPA, I assume to protect other Countries, not the United States which they should be interested in protecting — But I am allowed to cut off any and all Trade or Business with that same Country, even imposing a Foreign Country destroying embargo..."*
- **Feb 20, 1:38 PM ET** (main): *"The Supreme Court's Ruling on TARIFFS is deeply disappointing! I am ashamed of certain Members of the Court for not having the Courage to do what is right for our Country. I would like to thank and congratulate Justices Thomas, Alito, and Kavanaugh for your Strength, Wisdom, and Love of our Country... Foreign Countries that have been ripping us off for years are ecstatic, and dancing in the streets — But they won't be dancing for long!"*
- **Feb 20, 7:04 PM ET** (heated follow-up): *"Those members of the Supreme Court who voted against our very acceptable and proper method of TARIFFS should be ashamed of themselves. Their decision was ridiculous but, now the adjustment process begins, and we will do everything possible to take in even more money than we were taking in before!"*

**Market reaction**:

| Date       | Asset    | Dir | Mag    |
|------------|----------|-----|--------|
| 2/20 PM    | S&P 500  | ↑   | +2.1%  |
| 2/20 PM    | NASDAQ   | ↑   | +2.8%  |
| 2/20 PM    | SOX      | ↑   | +3.5%  |
| 2/20 PM    | NVDA     | ↑   | +2.8%  |
| 2/20 PM    | AVGO     | ↑   | +4.1%  |
| 2/23 Mon   | S&P 500  | ↓   | -0.6% (give-back) |
| 2/23 Mon   | SOX      | ↓   | -1.2%  |

---

### 7.4 Section 122 Global 10% Effective (2026-02-24)

```yaml
event_id: 2026-02-24_section122_effective
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2026-02-24"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: false   # Concurrent with the EO
news_dump_pattern: false
topic_tags: [tariff, section-122, global]
market_impact_score: 7
horizon: "1-3d"
confidence: high
insider_concern: false
```

**Market reaction**: S&P -1.5%, NASDAQ -2.1%, SOX -2.8%.

---

### 7.5 ★ Iran 2nd Strike (2026-02-28, Sat ~02:30 ET)

```yaml
event_id: 2026-02-28_iran_second_strike
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2026-02-27 ~23:30 PST"
posted_at_et: "2026-02-28 ~02:30 EST"
posted_at_utc: "2026-02-28T07:30:00Z"
post_ids: ["116147572522796874", "116150413051904167"]                # Video-message posts
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: true        # Saturday pre-dawn — deliberate news dump
topic_tags: [iran, military, news-dump, video]
market_impact_score: 8
horizon: "1-3d"
confidence: high
insider_concern: true          # Historical event #4 (`2026-02-28_iran_first_strike.md`)
```

**Post gist**: A Saturday-dawn video message — confirming additional strikes on Iranian nuclear + missile facilities. A Khamenei-death announcement was posted that same afternoon.

**Trump Truth Social Posts (verbatim, sequence)**:
- **Feb 28, 4:35 AM ET** (news citation, lead-up): *"Iran tried to interfere in 2020, 2024 elections to stop Trump, and now faces renewed war with United States: [link to justthenews.com]..."*
- **Feb 28, 4:37 PM ET** (✨ main announcement): *"Khamenei, one of the most evil people in History, is dead. This is not only Justice for the people of Iran, but for all Great Americans, and those people from many Countries throughout the World, that have been killed or mutilated by Khamenei and his gang of bloodthirsty THUGS. He was unable to avoid our Intelligence and Highly Sophisticated Tracking Systems and, working closely with Israel, there was not a thing he, or the other leaders that have been killed along with him, could do. This is the single greatest chance for the Iranian people to take back their Country..."*

**Verbatim note**: The user's original note about "a Saturday pre-dawn ~02:30 ET video message" could not be found verbatim in our scrape (the post at that time is the 4:35 AM news-citation). The primary catalyst of the market reaction (Mon 3/2) is more likely the Khamenei-dead announcement that afternoon.

**Market reaction (Mon 3/2)**:

| Asset    | Dir | Mag    |
|----------|-----|--------|
| S&P 500  | ↓   | -1.4%  |
| NASDAQ   | ↓   | -1.8%  |
| Brent    | ↑   | +6%    |
| Gold     | ↑   | +2.5%  |

**Reference**: User historical event #4 (`2026-02-28_iran_first_strike.md`).

---

### 7.6 ★ Iran 5-Day Pause (2026-03-23, Mon)

```yaml
event_id: 2026-03-23_iran_5day_pause
verification_level: double_verified  # 2 of 3 posts pass Wayback; 1 (116278963291413739) cannot escalate to triple due to missing Wayback archive (checked 2026-05-15). trumpstruth.org verbatim + narrative cross-check all pass.
posted_at_pt: "2026-03-23 (morning PT)"
posted_at_utc: TBD
post_ids: ["116278963291413739", "116278232362967212", "116278159912794855"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [iran, pause, oil]
market_impact_score: 7
horizon: "1-3d"
confidence: high
insider_concern: true          # Historical event #5 (`2026-03-23_iran_strike_pause.md`)
```

**Post gist** (all caps): *"...I have instructed the Department of War to postpone any and all military strikes against Iranian power plants and energy infrastructure for a five day period."*

**Market reaction**: S&P +1.1%, NASDAQ +1.4%, Brent -3.5%. Gasoline at $3.96 → stabilized.

---

### 7.7 Middle East Crisis (2026-03-30)

```yaml
event_id: 2026-03-30_me_crisis
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2026-03-30"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: false
news_dump_pattern: false
topic_tags: [iran, geopolitics]
market_impact_score: 7
horizon: "1d"
confidence: low                # Needs identification of the specific post
insider_concern: false
```

**Market reaction**: S&P -2.5%, NASDAQ -3.4%, SOX -4.2%.

**Verify in Step 2**: Which specific Truth Social post was the catalyst.

---

### 7.8 Liberation Day 1st Anniversary (2026-04-02)

```yaml
event_id: 2026-04-02_liberation_anniversary
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2026-04-02"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [tariff, anniversary, pharma, steel]
market_impact_score: 5
horizon: "1d"
confidence: high
insider_concern: false
```

**Post gist**: *"Liberation Day Anniversary"* — 100% premium pharma; tiered steel/aluminum/copper.

**Market reaction**: PFE -4%, LLY -2.5%, MRK -3.1%, S&P -0.5%, NASDAQ -0.7%.

---

### 7.9 Iran Temporary Ceasefire (2026-04-07~08)

```yaml
event_id: 2026-04-07_iran_temp_ceasefire
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2026-04-07"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [iran, ceasefire, risk-on]
market_impact_score: 6
horizon: "1-3d"
confidence: high
insider_concern: false
```

**Market reaction**: S&P +0.7%, NASDAQ +1.0%, SOX +1.1%. The next day a 2-week ceasefire deal — record close + SOX rally.

---

### 7.10 ★ Hormuz Open (2026-04-17)

```yaml
event_id: 2026-04-17_hormuz_open
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2026-04-17 06:27 PT"     # First post (Hormuz open announcement)
posted_at_et: "2026-04-17 09:27 ET"
posted_at_utc: "2026-04-17T13:27:00Z"
post_ids: ["116420275523158052", "116420456436213944", "116420562510387829"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [iran, hormuz, oil]
market_impact_score: 7
horizon: "1-3d"
confidence: high
insider_concern: true          # Historical event #6 (`2026-04-17_hormuz_open.md`)
```

**Trump Truth Social Posts (verbatim, sequence)**:
- **Apr 17, 9:27 AM ET** (✨ main announcement): *"THE STRAIT OF HORMUZ IS COMPLETELY OPEN AND READY FOR BUSINESS AND FULL PASSAGE, BUT THE NAVAL BLOCKADE WILL REMAIN IN FULL FORCE AND EFFECT AS IT PERTAINS TO IRAN, ONLY, UNTIL SUCH TIME AS OUR TRANSACTION WITH IRAN IS 100% COMPLETE. THIS PROCESS SHOULD GO VERY QUICKLY IN THAT MOST OF THE POINTS ARE ALREADY NEGOTIATED. THANK YOU FOR YOUR ATTENTION TO THIS MATTER! PRESIDENT DONALD J.TRUMP"*
- **Apr 17, 10:13 AM ET** (heated, NATO): *"Now that the Hormuz Strait situation is over, I received a call from NATO asking if we would need some help. I TOLD THEM TO STAY AWAY, UNLESS THEY JUST WANT TO LOAD UP THEIR SHIPS WITH OIL. They were useless when needed, a Paper Tiger! President DJT"*
- **Apr 17, 10:40 AM ET**: *"Iran has agreed to never close the Strait of Hormuz again. It will no longer be used as a weapon against the World! President DONALD J. TRUMP"*

**Note**: If the historical-event BZ short burst was just before the 9:27 AM ET announcement, an insider concern is implicated — recommend cross-reference via Phase D Wayback.

---

### 7.11 ★ Trump Iran Fractured (2026-04-21)

```yaml
event_id: 2026-04-21_trump_iran_fractured
verification_level: triple_verified  # Wayback Mastodon API JSON automated verification passed (2026-05-15)
posted_at_pt: "2026-04-21 13:09 PT"     # Main STATEMENT post
posted_at_et: "2026-04-21 16:09 ET"
posted_at_utc: "2026-04-21T20:09:00Z"
post_ids: ["116444507618729432", "116445317344745621", "116445555373723862"]
author: realDonaldTrump
truth_social_isolated: true
news_dump_pattern: false
topic_tags: [iran, geopolitics, hormuz]
market_impact_score: 7
horizon: "1-3d"
confidence: high
insider_concern: true          # Historical event #7 (`2026-04-21_trump_iran_fractured.md`)
```

**Trump Truth Social Posts (verbatim, sequence)**:
- **Apr 21, 4:09 PM ET** (✨ main STATEMENT, source of the event_id): *"STATEMENT OF PRESIDENT DONALD J. TRUMP: Based on the fact that the Government of Iran is seriously fractured, not unexpectedly so and, upon the request of Field Marshal Asim Munir, and Prime Minister Shehbaz Sharif, of Pakistan, we have been asked to hold our Attack on the Country of Iran until such time as their leaders and representatives can come up with a unified proposal. I have therefore directed our Military to continue the Blockade and, in all other respects, remain ready and able, and will therefore extend the Ceasefire until such time as their proposal is submitted, and discussions a..."*
- **Apr 21, 7:35 PM ET** (heated, WSJ): *"THE WALL STREET JOURNAL HAS LOST ITS WAY! An IDIOT on The Wall Street Journal's Editorial Board, named Elliot Kaufman, just wrote an Op Ed entitled, 'The Iranians Take Trump for a Sucker.' Really? For 47 years, they have killed our people, and many others, and taken advantage of every President, except me — And what did I give to them, a Country in tatters! Their entire Navy is at the bottom of the Sea, their Air Force is gone..."*
- **Apr 21, 8:36 PM ET** (Hormuz context): *"Iran doesn't want the Strait of Hormuz closed, they want it open so they can make $500 Million Dollars a day (which is, therefore, what they are losing if it is closed!). They only say they want it closed because I have it totally BLOCKADED (CLOSED!), so they merely want to 'save face.'..."*

---

### 7.12 China Visit (Jensen excluded) (2026-05-12)

```yaml
event_id: 2026-05-12_china_visit_no_jensen
verification_level: single_source  # Narrative quote validator failed to auto-match (raw.jsonl exists but needs manual review)
posted_at_pt: "2026-05-12"
posted_at_utc: TBD
post_ids: [TBD]
author: realDonaldTrump
truth_social_isolated: false
news_dump_pattern: false
topic_tags: [china, nvda, jensen, semiconductor]
market_impact_score: 4
horizon: "1d"
confidence: low                # Administration decision — Trump's direct Truth Social citation unclear
insider_concern: false
```

**Market reaction**: NVDA -1.5%, SOX -0.6%.

---

## 8. Cross-Event Patterns (learned canonical patterns)

These patterns act as secondary signals for LLM scoring.

### 8.1 Timing patterns

| Pattern | Examples | Why important |
|---------|----------|---------------|
| **Pre-market (06:00–09:30 ET)** | 2025-05-23 (Apple/EU twin), 2026-01-21 (Greenland) | Maximum impact at the regular-session open — directly catalyzes intraday volatility |
| **Market open ±10 min** | 2025-04-09 09:33/09:37 ET | Peak-volume window — market absorbs within 1 second |
| **Friday close ~ weekend dawn** | 2025-06-21 (Iran strike), 2026-02-28 (Iran II), 2025-02-01 (Mexico/Canada) | "News dump" — minimizes Mon-open gap |
| **Sunday evening** | 2025-05-11 (China RESET), 2025-05-25 (EU extension) | Maximizes the Monday gap |

### 8.2 TACO Pattern (Trump Always Chickens Out)

Extreme threat → market crash → retreat within days → market rebound. Same playbook:

| Setup | Reversal | Magnitude |
|-------|----------|-----------|
| 2025-04-02 Liberation Day (-12%) | 2025-04-09 90-day pause (+9.52%) | full retracement |
| 2025-05-23 EU 50% (-0.67%) | 2025-05-25 extension (+2.05%) | overshoot |
| 2025-07-11 Canada 35% (-0.33%) | de facto delay | dampened |
| 2025-10-10 China rare-earth (-2.71%) | 2025-10-13 tonedown (+1.56%) | partial |

**Market learning**: From H2 2025 the market progressively prices in TACO trades → for posts of equivalent magnitude, market reaction decreases.

### 8.3 Insider-concern timestamps (congressional/media coverage)

| Date | Concern | Evidence |
|------|---------|----------|
| 2025-04-09 09:37 ET "BUY" | Schiff formal investigation request, Lutnick quote | Circumstantial evidence that the pause was decided at the time of writing |
| 2025-05-11 RESET → 5/13-16 ME trip | Consecutive timing | Repeat of the 4/9 pattern |
| 2025-06-21 strike, 2025-06-23 ceasefire | Unusual options volume | WTI put / S&P call (CFTC) |
| 2026-01-21 Greenland (20-min pre-open) | CNN: *"20 minutes before markets opened"* | Deliberate timing |
| 2026-02-28 ~02:30 ET Sat | News-dump pattern | "Trump did wait until after the markets closed on a Friday" |

### 8.4 Sector beta to Truth Social posts

Ratio versus the S&P 500:

| Sector / Index | Beta to China-related TS posts |
|----------------|--------------------------------|
| SOX            | 2.5–3.0x (S&P -2.71% → SOX -6.3%, S&P +9.52% → SOX +18.8%) |
| NVDA (single)  | ~1.8x |
| AMD (single)   | ~2.1x |
| MU (memory)    | ~3.0x (greatest HBM/AI sensitivity) |
| TSM ADR        | ~2.2x |
| Korea (KOSPI)  | ~1.0x (ring-fenced after the Korea deal) |
| TSLA (Musk feud) | Unrelated to broad market; single-event max -14.3% |

---

## 9. Open Questions & Verification Needed

Outstanding items to address after Step 2 backfill.

### 9.1 User draft's stated limitations
1. **Verification of specific market-moving Truth Social posts related to Maduro** — User draft note: *"Not verified in this survey."* Confirm after Step 2 backfill around 2026-01-03 ±1 day.
2. **2026-03-30 Middle East crisis** — Unclear which specific post served as the catalyst.
3. **2026-04-17 (Hormuz) / 2026-04-21 (Iran fractured)** — Cross-reference with historical events. Acquire precise timestamps / post IDs in Step 2.

### 9.2 Cases where isolating Truth Social is hard
The following events are bundled with Oval Office / signing ceremony / AF1 remarks, so market impact cannot be isolated. Marked confidence: medium/low.

- 2025-02-27 March 4 confirm (combo with NVDA earnings)
- 2025-08-01 BLS jobs (composite market catalyst)
- 2026-02-24 Section 122 effective (simultaneous EO)

### 9.3 Additional verification for V2
- Market-reaction analysis between April 9 09:33 ET "BE COOL" and 09:37 "BUY" — 4-minute intraday tick data would offer a more precise LLM reference.
- Quantitative measurement of the "TACO trade" learning curve — How much the market reaction has decreased for posts of equivalent magnitude at the 2025-04, 2025-10, and 2026-02 points.
- Additional curation of events post-Q2 2026 (as they occur after May 15).

---

## 10. Index — by post_id (populated after Step 2 backfill)

This table will be matched against the `id` field in the raw.jsonl output of Step 2's `truth_social_backfill.py`. Empty at v1, cross-linked to `TruthSocial_events_raw.md` after Step 2.

| post_id | event_id | posted_at_utc | excerpt (first 100 chars) |
|---------|----------|---------------|---------------------------|
| (TBD by Step 2) | ... | ... | ... |

---

## 11. Schema Usage Guide (for LLM-prompt authors)

When injecting this reference into an LLM scoring prompt:

1. **Few-shot selection**: Pick 3–5 references whose `topic_tags` overlap most with the new post.
2. **Compact format**: Don't inject the full markdown — use frontmatter YAML + post excerpt + market-reaction table only (~500 tokens / event).
3. **Score reasoning**: When computing a score, have the LLM include a one-line reasoning of "why this post is similar to reference X."
4. **TACO awareness**: For extreme threats, factor in the TACO learning curve when assigning a score — discount to 9–10 at the 2025-04 timeframe, to 7–8 at the 2026-04 timeframe.
