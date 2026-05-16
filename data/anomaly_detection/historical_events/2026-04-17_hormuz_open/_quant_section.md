## 9. Quantitative replay data

_Official announcement: 2026-04-17 05:45 PT (= 12:45 UTC)._

_Per-section `t` is measured from the **section's own** T=0._  
_• Polymarket: T=0 is either the auto-detected decisive burst minute, or — when manually overridden — the start of the **insider accumulation window** (wallet-funding/wallet-splitting phase that precedes the public announcement)._  
_• CME / Hyperliquid: T=0 is the official announcement._  
_Burst (insider-suspected) rows are bold._  
_Volume columns: **buy** = taker lifted ask (long pressure), **sell** = taker hit bid (short pressure), **net** = buy − sell. Plots show buy volume above the zero line (green) and sell volume below (red), so a downward-dominant burst = short-pressure burst._

### CME BZ

![CME BZ volume + price](2026-04-17_hormuz_open/cme_BZ.png)

**1-min OHLCV** (5 bars before burst → burst → 3 bars after):

| t (min) | open | high | low | close | buy | sell | net | trades |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| -26 | 95.70 | 95.71 | 95.66 | 95.69 | 18 | 4 | +14 | 26 |
| -25 | 95.72 | 95.75 | 95.71 | 95.71 | 5 | 5 | +0 | 9 |
| -24 | 95.63 | 95.65 | 95.60 | 95.60 | 5 | 6 | -1 | 11 |
| -23 | 95.79 | 95.82 | 95.79 | 95.82 | 2 | 3 | -1 | 5 |
| -22 | 95.83 | 95.83 | 95.59 | 95.60 | 8 | 4 | +4 | 13 |
| **-21** | **95.65** | **95.65** | **94.89** | **94.95** | **170** | **56** | **+114** | **209** |
| -20 | 94.95 | 95.03 | 94.28 | 94.47 | 218 | 106 | +112 | 268 |
| -19 | 94.49 | 94.95 | 94.49 | 94.94 | 26 | 40 | -14 | 74 |
| -18 | 94.95 | 95.15 | 94.76 | 94.81 | 26 | 32 | -6 | 62 |
| -17 | 94.83 | 94.84 | 94.65 | 94.75 | 24 | 34 | -10 | 40 |


**2-min burst aggregate** (max volume bar in burst window): `726 contracts` — **388 buy / 162 sell** → LONG (buy-side)


**5-min burst aggregate** (max volume bar in burst window): `648 contracts` — **300 buy / 224 sell** → LONG (buy-side)

