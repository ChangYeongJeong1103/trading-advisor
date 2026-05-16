## 9. Quantitative replay data

_Official announcement: 2026-03-23 04:04 PT (= 11:04 UTC)._

_Per-section `t` is measured from the **section's own** T=0._  
_• Polymarket: T=0 is either the auto-detected decisive burst minute, or — when manually overridden — the start of the **insider accumulation window** (wallet-funding/wallet-splitting phase that precedes the public announcement)._  
_• CME / Hyperliquid: T=0 is the official announcement._  
_Burst (insider-suspected) rows are bold._  
_Volume columns: **buy** = taker lifted ask (long pressure), **sell** = taker hit bid (short pressure), **net** = buy − sell. Plots show buy volume above the zero line (green) and sell volume below (red), so a downward-dominant burst = short-pressure burst._

### CME CL

![CME CL volume + price](2026-03-23_iran_strike_pause/cme_CL.png)

**1-min OHLCV** (5 bars before burst → burst → 3 bars after):

| t (min) | open | high | low | close | buy | sell | net | trades |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| -21 | 98.65 | 98.80 | 98.65 | 98.73 | 53 | 63 | -10 | 84 |
| -20 | 98.70 | 98.91 | 98.44 | 98.55 | 159 | 146 | +13 | 233 |
| -19 | 98.53 | 98.58 | 98.44 | 98.54 | 33 | 38 | -5 | 62 |
| -18 | 98.55 | 98.65 | 98.50 | 98.60 | 17 | 35 | -18 | 43 |
| -17 | 98.59 | 98.66 | 98.47 | 98.56 | 92 | 38 | +54 | 89 |
| **-16** | **98.55** | **98.67** | **98.51** | **98.52** | **24** | **32** | **-8** | **42** |
| **-15** | **98.52** | **98.54** | **98.03** | **98.21** | **354** | **258** | **+96** | **492** |
| **-14** | **98.20** | **98.37** | **97.40** | **97.95** | **1,226** | **752** | **+474** | **1,168** |
| -13 | 97.96 | 98.35 | 97.91 | 98.27 | 175 | 331 | -156 | 314 |
| -12 | 98.28 | 98.46 | 98.24 | 98.33 | 156 | 251 | -95 | 249 |
| -11 | 98.35 | 98.57 | 98.29 | 98.37 | 67 | 75 | -8 | 104 |
| -10 | 98.34 | 98.46 | 98.32 | 98.40 | 47 | 52 | -5 | 86 |


**2-min burst aggregate** (max volume bar in burst window): `2,683 contracts` — **1,401 buy / 1,083 sell** → LONG (buy-side)


**5-min burst aggregate** (max volume bar in burst window): `3,382 contracts` — **1,671 buy / 1,461 sell** → LONG (buy-side)


### CME BZ

![CME BZ volume + price](2026-03-23_iran_strike_pause/cme_BZ.png)

**1-min OHLCV** (5 bars before burst → burst → 3 bars after):

| t (min) | open | high | low | close | buy | sell | net | trades |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| -21 | 113.03 | 113.10 | 113.03 | 113.07 | 4 | 5 | -1 | 8 |
| -20 | 113.10 | 113.23 | 112.79 | 112.87 | 15 | 20 | -5 | 39 |
| -19 | 112.82 | 112.91 | 112.80 | 112.91 | 5 | 5 | +0 | 8 |
| -18 | 112.85 | 112.96 | 112.85 | 112.89 | 7 | 2 | +5 | 8 |
| -17 | 112.95 | 112.95 | 112.78 | 112.88 | 9 | 8 | +1 | 17 |
| **-16** | **112.92** | **112.93** | **112.82** | **112.82** | **3** | **0** | **+3** | **4** |
| **-15** | **112.82** | **112.82** | **112.14** | **112.20** | **46** | **47** | **-1** | **119** |
| **-14** | **112.22** | **112.44** | **111.77** | **112.15** | **96** | **93** | **+3** | **248** |
| -13 | 112.22 | 112.73 | 112.19 | 112.60 | 39 | 30 | +9 | 59 |
| -12 | 112.65 | 112.79 | 112.61 | 112.76 | 16 | 10 | +6 | 33 |
| -11 | 112.68 | 112.76 | 112.60 | 112.62 | 8 | 10 | -2 | 16 |
| -10 | 112.57 | 112.69 | 112.57 | 112.63 | 2 | 5 | -3 | 7 |


**2-min burst aggregate** (max volume bar in burst window): `377 contracts` — **135 buy / 123 sell** → LONG (buy-side)


**5-min burst aggregate** (max volume bar in burst window): `439 contracts` — **161 buy / 148 sell** → LONG (buy-side)


### CME ES

![CME ES volume + price](2026-03-23_iran_strike_pause/cme_ES.png)

**1-min OHLCV** (5 bars before burst → burst → 3 bars after):

| t (min) | open | high | low | close | buy | sell | net | trades |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| -21 | 6499.00 | 6499.00 | 6497.00 | 6497.25 | 290 | 198 | +92 | 229 |
| -20 | 6497.00 | 6500.25 | 6495.75 | 6500.00 | 266 | 260 | +6 | 245 |
| -19 | 6499.75 | 6499.75 | 6495.50 | 6497.00 | 243 | 202 | +41 | 190 |
| -18 | 6497.25 | 6498.50 | 6495.50 | 6497.50 | 218 | 199 | +19 | 188 |
| -17 | 6497.25 | 6498.25 | 6495.25 | 6496.25 | 178 | 184 | -6 | 162 |
| **-16** | **6496.50** | **6497.00** | **6492.75** | **6494.00** | **312** | **393** | **-81** | **284** |
| **-15** | **6494.25** | **6494.75** | **6490.00** | **6494.50** | **787** | **753** | **+34** | **529** |
| **-14** | **6494.75** | **6518.00** | **6493.50** | **6509.50** | **2,034** | **2,471** | **-437** | **2,091** |
| -13 | 6509.75 | 6510.00 | 6502.25 | 6503.75 | 827 | 631 | +196 | 637 |
| -12 | 6504.00 | 6504.25 | 6501.50 | 6503.50 | 569 | 343 | +226 | 350 |
| -11 | 6503.25 | 6505.00 | 6499.75 | 6504.25 | 378 | 360 | +18 | 310 |
| -10 | 6504.50 | 6505.50 | 6502.75 | 6503.00 | 227 | 168 | +59 | 185 |


**2-min burst aggregate** (max volume bar in burst window): `5,963 contracts` — **2,861 buy / 3,102 sell** → SHORT (sell-side)


**5-min burst aggregate** (max volume bar in burst window): `8,008 contracts` — **4,035 buy / 3,973 sell** → LONG (buy-side)

