# P10 — Polymarket Replay Operating Guide

This document captures the **one-time setup** procedure and the
**Dune SQL template** required to run the Polymarket channel of the
P10 replay framework.

It mirrors the structure of the CME pattern (`cme_databento.py` + `cme.py`),
but Polymarket pulls its historical trades through a **saved query on Dune
Analytics**.

---

## 1. One-time setup

### 1-1. Issue a Dune API key

1. Sign up at <https://dune.com> (free tier is fine).
2. Settings → API → "Create new API key".
3. Add the issued key to `.env`:
   ```
   DUNE_API_KEY=...
   ```

   - **Free plan**: 2,500 credits per month.
   - One `market_trades` query (medium tier, 1 market × 7 days) ≈ 10–30 credits.
   - Our replay caches every `(condition_id, window)` combination as parquet, so re-running the same event consumes 0 credits.
   - **Recommendation**: fetch each event only once → roughly 70–80 credits per event.

### 1-2. Save the SQL on Dune (create a saved query)

On dune.com, click "+ New query" in the top right → paste the SQL below →
hit "Run" to test once → click "Save". The saved query's URL is
`https://dune.com/queries/12345`, where the numeric part (`12345`) is the
**query_id**.

```sql
-- ─────────────────────────────────────────────────────────────
-- Polymarket historical trades for a single market, time-bounded.
-- Used by trading-advisor / anomaly replay (PolymarketDuneSource).
--
-- Parameters (all "Text" type when defining on dune.com):
--   condition_id : 0x-prefixed hex, e.g. '0x0e01a2...'
--   start_ts     : 'YYYY-MM-DD HH:MM:SS' UTC, e.g. '2026-01-03 00:00:00'
--   end_ts       : 'YYYY-MM-DD HH:MM:SS' UTC
--
-- DuneSQL (Trino) syntax. Polymarket Spellbook table:
--   docs.dune.com/data-catalog/curated/prediction-markets/polymarket/market_trades
-- ─────────────────────────────────────────────────────────────
SELECT
  block_time,
  -- VARBINARY → 0x-prefixed hex string (the client expects str).
  '0x' || LOWER(TO_HEX(condition_id))                       AS condition_id,
  price,                                                     -- 0..1 implied probability
  amount                                                     AS usd_amount,
  shares,
  token_outcome                                              AS outcome,
  -- Our detector only distinguishes buy / sell. YES baking = bullish = buy, NO = sell.
  CASE token_outcome WHEN 'YES' THEN 'buy' ELSE 'sell' END  AS side,
  '0x' || LOWER(TO_HEX(taker))                              AS trader,
  '0x' || LOWER(TO_HEX(tx_hash))                            AS tx_hash
FROM polymarket_polygon.market_trades
WHERE condition_id = from_hex(REPLACE(LOWER('{{condition_id}}'), '0x', ''))
  AND block_time >= CAST('{{start_ts}}' AS TIMESTAMP)
  AND block_time <  CAST('{{end_ts}}'   AS TIMESTAMP)
ORDER BY block_time ASC
LIMIT 50000
```

Before saving, open the **"Parameters"** panel and register all three
parameters as **Text type** (names must match `{{...}}` in the SQL
exactly). Set their defaults to values like the sample below so future
manual test runs only need a click:

| parameter      | type | example default               |
|----------------|------|-------------------------------|
| `condition_id` | Text | `0x0e01a2...`                 |
| `start_ts`     | Text | `2026-01-01 00:00:00`         |
| `end_ts`       | Text | `2026-01-08 00:00:00`         |

Run the saved query once with the small default window. If results come
back, you are good.

### 1-3. Add the query_id to `.env`

```
DUNE_QUERY_ID_POLYMARKET_TRADES=12345    # the numeric part of your query
```

---

## 2. How to run

```bash
# 1) Polymarket primary event (e.g. 2026-01-03 maduro arrest)
PYTHONPATH=src python -m anomaly_detection.replay 2026-01-03_maduro_arrest

# 2) iran_first_strike
PYTHONPATH=src python -m anomaly_detection.replay 2026-02-28_iran_first_strike
```

On the first run:

1. `Gamma API` → slug → conditionId resolution.
2. `Dune execute` → fetch every trade for that conditionId (tens of seconds).
3. Cache to `data/anomaly_detection/replay_cache/polymarket/<conditionId>_<start>_<end>.parquet`.
4. Then the usual 1-min cycle drives the detector → `timeline.png` + `report.yaml`.

On subsequent runs the cache hits → 0 Dune calls → immediate start.

---

## 3. Common problems / debugging

### 3-1. `slug → conditionId not found via Gamma`

The slug in the event .md `primary_symbols` was a placeholder (e.g. Maduro's
`maduro-out-by-jan-2026`) or the market has since been closed/renamed.
Fix:

- Find the real Polymarket market URL and adjust the slug accordingly.
- Example: the last path of <https://polymarket.com/event/maduro-out-of-power-by-...>.
- Update the event .md frontmatter's `primary_symbols` and rerun.

### 3-2. `Dune result 0 rows`

Almost always a wrong conditionId. Verify directly on Dune that the query
returns rows:

```sql
SELECT COUNT(*) FROM polymarket_polygon.market_trades
WHERE condition_id = from_hex(REPLACE(LOWER('0x...'), '0x', ''))
  AND block_time >= TIMESTAMP '2026-01-01 00:00:00';
```

### 3-3. `Dune execute timeout`

`PolymarketDuneSource._DUNE_WAIT_TIMEOUT_S` defaults to 120 seconds. For
larger markets that take longer, raise the constant, reduce the SQL
`LIMIT`, or upgrade the performance tier to `large` (paid plan).

### 3-4. Out of credits

Dune's free plan ships 2,500 credits per month. Check your current usage
at <https://dune.com/settings/billing>. If you hit the cap, either wait
until next month or upgrade to a paid plan (starts at $349/month for 25k
credits).

---

## 4. Cache management

```
data/anomaly_detection/replay_cache/polymarket/
  0x0e01a2..._20260101T000000_20260108T000000.parquet
  0xabcdef..._20260222T000000_20260301T000000.parquet
  ...
```

- Filename = `{conditionId}_{start_iso}_{end_iso}.parquet`.
- Changing an event's window triggers a new fetch (by design). If you do
  not change the pre/post window, the cache hits forever.
- To force a re-fetch: `rm` the parquet file and rerun.

---

## 5. Next steps (P10.6+)

- **Hyperliquid replay**: Hyperliquid's Info API (`candleSnapshot` +
  `recentTrades`) has short retention, which makes historical validation
  hard. Alternatives are under review (Hyperliquid Stats API, archive
  nodes, etc.).
- **X channel replay**: `snscrape` has been discontinued. Alternatives
  (twscrape / Nitter mirror) are being evaluated.
