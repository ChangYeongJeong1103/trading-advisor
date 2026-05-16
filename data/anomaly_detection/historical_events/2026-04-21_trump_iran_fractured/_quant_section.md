## 9. Quantitative replay data

_Official announcement: 2026-04-21 13:10 PT (= 20:10 UTC)._

_Per-section `t` is measured from the **section's own** T=0._  
_• Polymarket: T=0 is either the auto-detected decisive burst minute, or — when manually overridden — the start of the **insider accumulation window** (wallet-funding/wallet-splitting phase that precedes the public announcement)._  
_• CME / Hyperliquid: T=0 is the official announcement._  
_Burst (insider-suspected) rows are bold._  
_Volume columns: **buy** = taker lifted ask (long pressure), **sell** = taker hit bid (short pressure), **net** = buy − sell. Plots show buy volume above the zero line (green) and sell volume below (red), so a downward-dominant burst = short-pressure burst._

### CME BZ

![CME BZ volume + price](2026-04-21_trump_iran_fractured/cme_BZ.png)

**1-min OHLCV** (5 bars before burst → burst → 3 bars after):

| t (min) | open | high | low | close | buy | sell | net | trades |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| -21 | 100.14 | 100.30 | 99.91 | 100.30 | 18 | 47 | -29 | 77 |
| -20 | 100.26 | 100.43 | 100.24 | 100.38 | 4 | 9 | -5 | 17 |
| -19 | 100.47 | 100.69 | 100.47 | 100.68 | 11 | 31 | -20 | 62 |
| -18 | 100.70 | 101.03 | 100.69 | 100.87 | 60 | 98 | -38 | 110 |
| -17 | 100.88 | 100.98 | 100.78 | 100.90 | 23 | 26 | -3 | 79 |
| **-16** | **100.89** | **100.91** | **100.76** | **100.91** | **70** | **88** | **-18** | **204** |
| **-15** | **100.90** | **101.04** | **100.60** | **100.73** | **43** | **30** | **+13** | **88** |
| -14 | 100.74 | 101.15 | 100.68 | 100.68 | 35 | 40 | -5 | 65 |
| -13 | 100.66 | 100.71 | 100.57 | 100.60 | 6 | 12 | -6 | 21 |
| -12 | 100.59 | 100.70 | 100.56 | 100.56 | 5 | 8 | -3 | 21 |
| -11 | 100.70 | 100.75 | 100.64 | 100.67 | 6 | 7 | -1 | 21 |


**2-min burst aggregate** (max volume bar in burst window): `395 contracts` — **113 buy / 118 sell** → SHORT (sell-side)


**5-min burst aggregate** (max volume bar in burst window): `282 contracts` — **95 buy / 97 sell** → SHORT (sell-side)


### CME ES

![CME ES volume + price](2026-04-21_trump_iran_fractured/cme_ES.png)

**1-min OHLCV** (5 bars before burst → burst → 3 bars after):

| t (min) | open | high | low | close | buy | sell | net | trades |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| -21 | 7098.00 | 7100.50 | 7095.00 | 7099.25 | 2,095 | 2,171 | -76 | 1,518 |
| -20 | 7099.00 | 7107.00 | 7099.00 | 7099.75 | 3,990 | 4,257 | -267 | 3,034 |
| -19 | 7099.75 | 7100.00 | 7095.75 | 7097.50 | 3,052 | 2,728 | +324 | 2,014 |
| -18 | 7097.50 | 7097.50 | 7092.50 | 7094.75 | 2,759 | 2,247 | +512 | 1,643 |
| -17 | 7094.75 | 7097.50 | 7093.50 | 7096.75 | 1,837 | 2,329 | -492 | 1,280 |
| **-16** | **7096.75** | **7099.50** | **7095.25** | **7096.00** | **2,623** | **2,716** | **-93** | **1,738** |
| **-15** | **7096.25** | **7102.00** | **7093.50** | **7100.25** | **3,752** | **4,333** | **-581** | **2,669** |
| -14 | 7100.50 | 7101.25 | 7098.25 | 7099.50 | 3,261 | 3,744 | -483 | 1,876 |
| -13 | 7099.75 | 7100.00 | 7095.75 | 7099.25 | 3,467 | 3,931 | -464 | 2,174 |
| -12 | 7099.00 | 7101.00 | 7097.75 | 7100.25 | 5,072 | 6,136 | -1,064 | 2,677 |
| -11 | 7100.25 | 7103.75 | 7097.50 | 7100.50 | 26,213 | 27,074 | -861 | 8,886 |


**2-min burst aggregate** (max volume bar in burst window): `64,495 contracts` — **31,285 buy / 33,210 sell** → SHORT (sell-side)


**5-min burst aggregate** (max volume bar in burst window): `86,983 contracts` — **41,765 buy / 45,218 sell** → SHORT (sell-side)

