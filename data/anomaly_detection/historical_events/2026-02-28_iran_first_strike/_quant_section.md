## 9. Quantitative replay data

_Official announcement: 2026-02-27 22:15 PT (= 06:15 UTC)._

_Per-section `t` is measured from the **section's own** T=0._  
_• Polymarket: T=0 is either the auto-detected decisive burst minute, or — when manually overridden — the start of the **insider accumulation window** (wallet-funding/wallet-splitting phase that precedes the public announcement)._  
_• CME / Hyperliquid: T=0 is the official announcement._  
_Burst (insider-suspected) rows are bold._  
_Volume columns: **buy** = taker lifted ask (long pressure), **sell** = taker hit bid (short pressure), **net** = buy − sell. Plots show buy volume above the zero line (green) and sell volume below (red), so a downward-dominant burst = short-pressure burst._

### Polymarket

#### `will-the-us-next-strike-iran-on-february-28-2026-et`

_Question_: **Will the US next strike Iran on February 28, 2026 (ET)?**  
_Total market volume_: $5,837,357  _Insider accumulation window (manual override)_: `2026-02-27 14:00 PT` → `2026-02-27 22:15 PT`
  _Lead time vs. announcement_: **-8.2 h** (= -495 min from T=0)

_Window aggregates_: **$107,792** traded across **1,217** taker trades by **207 unique wallets** (USD volume measured in our fetched 10K trade slice — earlier wallet funding outside the API's reach is **not** counted).

![Polymarket will-the-us-next-strike-iran-on-february-28-2026-et 5-min USD volume + Yes %](2026-02-28_iran_first_strike/polymarket_will_the_us_next_strike_iran_on_february_28_2026_et.png)

**1-hour OHLCV** (Yes probability in %, USD volume, taker trades, unique wallets per bar):

| t (min) | open | high | low | close | usd vol | trades | wallets |
|---:|---:|---:|---:|---:|---:|---:|---:|
| **+0** | **14.0%** | **17.0%** | **13.2%** | **14.3%** | **$6,712** | **126** | **37** |
| **+60** | **18.8%** | **24.2%** | **14.0%** | **17.8%** | **$5,047** | **120** | **30** |
| **+120** | **17.9%** | **19.5%** | **15.0%** | **17.3%** | **$4,769** | **102** | **31** |
| **+180** | **15.1%** | **18.7%** | **14.5%** | **15.1%** | **$1,571** | **54** | **16** |
| **+240** | **14.9%** | **15.7%** | **14.8%** | **14.9%** | **$4,508** | **42** | **19** |
| **+300** | **15.8%** | **18.5%** | **13.0%** | **13.7%** | **$20,913** | **185** | **45** |
| **+360** | **13.0%** | **13.8%** | **11.1%** | **12.7%** | **$19,920** | **200** | **50** |
| **+420** | **12.7%** | **22.9%** | **12.1%** | **22.9%** | **$36,342** | **329** | **33** |
| **+480** | **22.1%** | **98.9%** | **19.9%** | **90.3%** | **$117,423** | **475** | **184** |
| +540 | 96.8% | 98.7% | 42.2% | 92.0% | $25,591 | 89 | 63 |


### CME

**CME silent — expected.** The strike landed early Saturday (2/28 06:15 UTC = 1:15 AM ET) when CME oil/equity futures were closed.  No pre-event volume burst could occur on CME.
