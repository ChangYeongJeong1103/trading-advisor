# Historical Insider Trading Events — P10 Validation Dataset

> Input for P10 (detection algorithm validation). Each event is a case the user identified as a
> publicly known insider-style trade.
>
> **The purpose of this document is not "prediction" but "simulation scenarios"** —
> we directly run the P10.3 replay framework against this point's actual historical data to verify
> whether our detector fires, and if so at which tier/timing it emits. The tier timelines / z-score
> ranges in this file are **target scenarios** (the standard our detector should hit), not
> post-hoc estimates.
>
> **Every event is analyzed across all 4 channels.** Why not only the primary channel:
> the insider may have used other channels in our watchlist as well (option exercises, etc.),
> and if our detector catches a signal the user missed manually, that itself is a major finding.
> In particular, **CME ES (E-mini S&P)** mirrors option hedging flow in nearly every
> macro/geopolitical event, so ES analysis is included for all events.

────────────────────────────────────────────────────────────────────────────
## Time notation rule (user decision locked 2026-05-14)

All human-readable timestamps are notated as **Pacific Time (PT) first**, with UTC in parentheses.

- Format: `01:21 AM PT (09:21 UTC)`.
- All event `.md` bodies + plot axes/annotations + this README's Summary Table use PT.
- Only the frontmatter's `announcement_ts:` retains ISO 8601 UTC (machine-readable).
- Auto-plot generation script (`scripts/generate_historical_event_data.py`) uses
  `ZoneInfo("America/Los_Angeles")` for auto PT/PDT (DST aware).

────────────────────────────────────────────────────────────────────────────
## Source

`data/historical event list.pdf` — 7 cases (#1–#7) the user compiled directly.
This compilation expands the PDF's fragmented rows and restructures them from a per-channel
detector-signature perspective.

────────────────────────────────────────────────────────────────────────────
## File structure

| File                                    | Description                                                      |
| :-------------------------------------- | :-------------------------------------------------------- |
| `README.md`                             | (this file) Index + summary table + replay usage            |
| `2025-04-09_liberation_day.md`          | Trump tariff 90 day pause — SPY call options              |
| `2025-10-10_china_tariff_100.md`        | China 100% tariff — Hyperliquid BTC/ETH short             |
| `2026-01-03_maduro_arrest.md`           | Venezuela Maduro arrest — Polymarket Yes bet                |
| `2026-02-28_iran_first_strike.md`       | Iran first strike — Polymarket wallet splitting (38 accounts) |
| `2026-03-23_iran_strike_pause.md`       | Iran strike pause — CME oil short + S&P long              |
| `2026-04-17_hormuz_open.md`             | Hormuz strait open — CME Brent short, 1-min burst             |
| `2026-04-21_trump_iran_fractured.md`    | Trump "Iran fractured" — CME Brent short, 2-min burst       |

Plot PNGs live alongside each event in `<event_id>/`:

| Folder                                              | Attached plots                                                    |
| :------------------------------------------------ | :------------------------------------------------------------ |
| `2025-04-09_liberation_day/`                      | `cme_BZ.png`, `cme_ES.png`                                    |
| `2025-10-10_china_tariff_100/`                    | `cme_BZ.png`, `cme_CL.png`, `cme_ES.png`, `hyperliquid_BTC.png` |
| `2026-01-03_maduro_arrest/`                       | `polymarket_maduro_out_*.png`, `polymarket_maduro_in_us_custody_*.png` |
| `2026-02-28_iran_first_strike/`                   | `polymarket_will_the_us_next_strike_iran_*.png`               |
| `2026-03-23_iran_strike_pause/`                   | `cme_BZ.png`, `cme_CL.png`, `cme_ES.png`                       |
| `2026-04-17_hormuz_open/`                         | `cme_BZ.png`                                                  |
| `2026-04-21_trump_iran_fractured/`                | `cme_BZ.png`, `cme_ES.png`                                    |

Schema for each event file (after P10.4 + 2026-05 quantitative augmentation):

1. **Event summary** — Announcement time (PT first, UTC parenthetical), price impact, INSIDER likelihood
2. **Pre-event suspicious activity** — When (T-N), platform, bet direction, size, account pattern
3. **Per-channel expected detector behavior** — Each of the 4 channels:
   - Channel 1 (Polymarket): which market, expected vol_burst z-score, yes_share change
   - Channel 2 (Hyperliquid): expected OI z-score, fresh-wallet detect, panic_filter
   - Channel 3 (CME): expected vol_z (which symbol), price_jump
   - Channel 4 (X): Stage1 score, expected LLM tier, status ID if a real X post existed
4. **Expected system_state timeline** — Tier progression from pre-event T-X to announcement T0
5. **P10 detection target** — When the alert should have emitted + evaluation of median latency ≤ 60s
6. **Sources** — News links, X status IDs, on-chain txs, etc.
7. **Quantitative data** *(added 2026-05)* — 1-min / 2-min / 5-min OHLCV tables
   - `open / high / low / close` + `buy_vol / sell_vol / neutral_vol / net_vol` columns
   - Burst-window aggregate (e.g., "395 contracts — 113 buy / 118 sell → SHORT (sell-side)")
   - All times in PT (UTC not duplicated — PT first)
8. **Embedded plots** *(added 2026-05)* — PNGs in the above folder referenced inline in the body
   - Top panel: price (close) line + right-side last-price label
   - Bottom panel: diverging volume bars — buy (green, above) / sell (red, below) / neutral (gray)
   - Yellow axvspan = burst window (suspected insider zone)
   - X-axis: PT time (`mdates.DateFormatter` + `ZoneInfo("America/Los_Angeles")`)
   - T=0 announcement vline + burst-start vline

────────────────────────────────────────────────────────────────────────────
## Summary Table

| # | Date       | Event                            | Announcement time (PT / UTC)         | Pre-event window | Insider position                                                                    | Primary channel   | Secondary channels             |
|---|-----------|----------------------------------|--------------------------------------|------------------|--------------------------------------------------------------------------------------|-------------------|--------------------------------|
| 1 | 2025-04-09 | Trump tariff 90d pause           | 10:18 PT / 17:18 UTC                 | T-18min (SPY) / 04-04~04-09 (Brent hypothesis) | SPY bullish call options ($2.14M → +$18.86M) + Brent oil burst hypothesis (1st of 3 repetitions)         | CME (ES, BZ)      | Polymarket?                    |
| 2 | 2025-10-10 | China 100% tariff                | 13:50 PT / 20:50 UTC                 | T-1d (10/9 09:39 PT ~ T-1min) | Hyperliquid BTC/ETH short ($1.1B notional, +$160-200M)                              | Hyperliquid (BTC, ETH) | X (Lookonchain pre-event)      |
| 3 | 2026-01-03 | Venezuela Maduro arrest          | 01:21 PT / 09:21 UTC ¹               | 7-day gradual (12/27 ~ 1/2) + T-15min decisive | Polymarket "Maduro out" Yes ($32K → +$400K, ×12), wallet dispersion + intraday run-up           | Polymarket        | X (post-event Lookonchain)     |
| 4 | 2026-02-28 | Iran first strike (US/Israel)    | 22:15 PT 2/27 / 06:15 UTC 2/28 ²    | T-6d ~ T-1d (38-account accumulation) | Polymarket+Kalshi "2/28 Iran strike" Yes (38 accounts, $500M traded → +$2M)         | Polymarket        | X (post-event)                 |
| 5 | 2026-03-23 | Iran strike pause (Truth Social) | 04:04 PT / 11:04 UTC                 | T-16min (1-min burst) | CME WTI/Brent short 6,200 contracts ($580M oil) + S&P long (+$1.5B)                  | CME (CL/BZ + ES)  | X (sometimes)                  |
| 6 | 2026-04-17 | Hormuz strait open (Iran FM)     | 05:45 PT / 12:45 UTC                 | T-20min (1-min burst) | Brent short 7,990 lots ($760M)                                                       | CME (BZ)          | X (post-event UW)              |
| 7 | 2026-04-21 | Trump "Iran fractured" Truth Social | 13:10 PT / 20:10 UTC               | T-16min (2-min burst) | Brent short 4,260 lots + ~2,140 related (~$430M+)                                    | CME (BZ)          | X (post-event Torres CFTC req.) |

¹ Maduro announcement-time correction (2026-05-14): Previous README used `01-04 02:25 UTC` (Lookonchain X post),
   but the actual official T=0 is **Trump's first Truth Social capture announcement** (01-03 04:21 EST
   = 01:21 PT = 09:21 UTC). See `2026-01-03_maduro_arrest.md` for details.
² Iran first strike T=0 correction (2026-05-14): IDF sirens / Trump Truth 8-min video / Polymarket
   price jump are effectively at the same 06:00–06:15 UTC (2/28). Previous README's "TBD" resolved.

────────────────────────────────────────────────────────────────────────────
## Per-channel detector signature pattern (statistics across 7 cases)

| Channel        | Primary signal in N events           | Secondary signal in N events | Silent in N events |
|----------------|-------------------------------------:|-----------------------------:|-------------------:|
| Polymarket     | 2 (#3 Maduro, #4 Iran-1st)           | 1 (#1 — possibly tariff market) | 4                  |
| Hyperliquid    | 1 (#2 China 100%)                    | 0                             | 6                  |
| CME            | 4 (#1 ES, #5 oil+ES, #6 Brent, #7 Brent) | 1 (#2 — BTC/ETH impact)         | 2                  |
| X              | 0 (pre-event)                         | 5 (post-event forensic — #2/3/5/6/7) | 2          |

**Observations** (after quantitative-data analysis — 2026-05 update):

- The primary channels the user identified manually often follow a single-channel pattern, but
  **running detector simulations can pick up additional signals in secondary channels** —
  especially since ES moves in every geopolitical event as macro hedging flow. Until P10.2
  finishes, the "Silent" entries above are provisional.
- **Maduro (#3)** is not a simple "1-hour-ago last bet" but a **2-layer pattern**:
  Phase 1 = 12/27~1/2 7-day cumulative (gradual wallet dispersion), Phase 2 = decisive intraday
  run-up 15 min before announcement. Our detector catches this via vol_burst + odds_cusum in
  Phase 2; in Phase 1, the base score has already drifted up via baseline drift.
- **CME oil-short events (#5/#6/#7)** consistently catch a **sell-side directional burst** in
  the quantitative plots — clearly visualized by the new directional volume plot (green=buy↑ /
  red=sell↓). Sell bars dominate in the 1–2 minutes just before the price drop.
- **2025-10-10 China tariff (#2)** also leaves traces on CME — vol_z is caught as a secondary
  signal in BZ/CL/ES quantitative data (immediately post the Trump Truth post at the time).
  First case where cross-channel corroboration boost is meaningful.
- Cross-channel corroboration boost is clearly meaningful for #2 (HL primary + X
  pre-event from Lookonchain) and #5/#7 (CME oil + ES dual-fire).
  Other events need P10.2 verification.
- Channel 4 (X) is mostly post-fact coverage — pre-event capture confirmed only for #2 hyperliquid
  (1 case).

────────────────────────────────────────────────────────────────────────────
## Usage (how the P10.3 replay framework reads this file)

```python
# Expected API (to be implemented in P10.3)
from anomaly.replay import EventLibrary, ReplayRunner

events = EventLibrary.load("data/anomaly/historical_events/")
for event in events:
    print(event.id, event.announcement_ts, event.primary_channel)

# Single-event replay
runner = ReplayRunner(event=events["2025-10-10_china_tariff_100"])
result = runner.run(window_hours=24)  # T-24h ~ T+1h
print(result.detection_latency_s)  # primary metric
print(result.warning_time_s)       # informational
```

Each event file's YAML frontmatter contains machine-readable critical metadata
(announcement_ts, primary_channel, expected_tier_timeline, etc.)
→ the replay framework parses it directly.

### Regenerating quantitative data / plots

`scripts/generate_historical_event_data.py` (re-)generates the quantitative section +
PNGs for all events in one shot:

```bash
PYTHONPATH=src python scripts/generate_historical_event_data.py
# → updates the quant section inside each event's .md + <event_id>/*.png
```

CME / Hyperliquid uses Databento parquet (GCS cache) + local HL trades CSV;
Polymarket uses direct Gamma API + data-api/trades calls. All times in PT.

────────────────────────────────────────────────────────────────────────────
## TODO

Data collection (P10.2 wrap-up, completed 2026-05):

- [x] **Polymarket historical data** — Gamma API (markets) + data-api/trades.
      `scripts/_polymarket_replay.py` handles chunked price-history + 1000-page
      trades. ✔ Applied to #3 Maduro, #4 Iran-1st.
- [x] **Hyperliquid historical data** — local trades CSV (single wallet).
      `scripts/generate_historical_event_data.py` aggregates OHLCV directly. ✔ Applied to #2.
- [x] **CME historical data** — Databento Parquet (existing P4 infra).
      Read directly from DBN_CACHE directory. ✔ Applied to #1/#2/#5/#6/#7.

Remaining items:

- [ ] **X historical posts** collection — snscrape `--since` flag (in environments where snscrape
      works) or X API recent search (7-day limit makes post-fact retrieval hard)
      → alternative: use the status IDs the user captured (Lookonchain status 1976330420917764535, etc.)
      directly
- [ ] **P10.2 verification** — run the above quantitative data through the real detector and compare
      expected vs actual tier timelines. Fill fp_count / detection_latency_s metrics.
- [ ] **Polymarket trades-API 10,000-row limit** — In 6-day cumulative cases like Iran 2/28,
      the oldest trade is truncated. Consider adopting CLOB `/data/trades` (authenticated).

Each event file's "Sources" section notes concrete data sources where possible.
