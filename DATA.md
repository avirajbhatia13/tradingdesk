# What data this desk holds

*Generated 2026-08-27 15:26 IST by `make data`, in the private repository that holds the lake. Every number below is read out of the data at the moment that command runs, so it cannot drift the way a hand-maintained table would. Reproduced here unedited.*

> **The lake itself is not in this repository**, and neither is the backfill tooling that fills it. What follows is the *shape* of the data this engine was built against and tested on: which indices, which years, which strikes, which columns are actually populated, and which holes are permanent. It is here because those constraints are most of what separates an honest backtest from a plausible-looking wrong one, and none of them are visible from the code.

The lake is **5,681 MB across 643 files**, local Parquet queried in place by DuckDB. No server, no subscription — and licensed from vendors, which is why it is not mine to redistribute.

## Can I backtest it?

Two kinds of row live here and they name a contract differently. Asking for the wrong one returns **nothing**, which is loud, rather than something wrong, which would not be.

**The full-chain start date is the first session that sees a front expiry**, not the oldest bar. A three-year option inside the retention window carries bars from years before a chain existed around it; NIFTY has bars from 2022-07 where the nearest expiry held is **1,066 days away**. Backtesting over that would price a market that is not there.

Strike widths below are where **99% of bars** sit, not the extreme reached on a gap day. Dhan is requested at ±10 moneyness levels and its derived moneyness still touches ±50 occasionally; quoting that would advertise wings this data cannot test.

| Index | Rolling — `--series` | Full chain — `--expiry` |
|---|---|---|
| **BANKNIFTY** | 2021-08-09 → 2026-08-14, ±10 strikes | **2024-10-03** → 2026-07-28, 450 sessions, 248 strikes<br/>*(bars go back to 2023-03-16, but only far-dated contracts — not a chain)* |
| **NIFTY** | 2021-08-16 → 2026-08-14, ±10 strikes | **2024-08-23** → 2026-08-25, 498 sessions, 179 strikes<br/>*(bars go back to 2022-07-04, but only far-dated contracts — not a chain)* |
| **SENSEX** | 2023-05-30 → 2026-08-14, ±10 strikes | **2024-09-06** → 2026-08-20, 482 sessions, 305 strikes<br/>*(bars go back to 2023-11-10, but only far-dated contracts — not a chain)* |

## Every series, in full

| Index | Source | Bars | Sessions | From | To | Expiries | Strikes | How to reach it |
|---|---|---|---:|---|---|---:|---:|---|
| BANKNIFTY | dhan | 38,769,874 | 1,244 | 2021-08-09 | 2026-08-14 | — | 795 | `--series MONTH` or `--series WEEK` |
| BANKNIFTY | upstox | 65,729,488 | 663 | 2023-03-16 | 2026-07-28 | 27 | 248 | `--expiry front` |
| NIFTY | dhan | 38,897,205 | 1,240 | 2021-08-16 | 2026-08-14 | — | 303 | `--series MONTH` or `--series WEEK` |
| NIFTY | live | 873,194 | 9 | 2026-08-17 | 2026-08-27 | 4 | 121 | `--expiry front` |
| NIFTY | upstox | 141,789,192 | 923 | 2022-07-04 | 2026-08-25 | 100 | 179 | `--expiry front` |
| SENSEX | dhan | 19,373,569 | 798 | 2023-05-30 | 2026-08-14 | — | 403 | `--series MONTH` or `--series WEEK` |
| SENSEX | upstox | 68,104,891 | 492 | 2023-11-10 | 2026-08-20 | 98 | 305 | `--expiry front` |

## Which fields are filled in

A column that is null is a column the engine cannot select on. `moneyness` decides whether a strike can be named at all; `iv` decides whether a delta rule works.

| Index | Source | spot | moneyness | implied vol | open interest | volume |
|---|---|---:|---|---:|---:|---:|
| BANKNIFTY | dhan | 100.0% | -10 … +10  *(99% of bars; extremes reach -52/+52)* | 100.0% | 100.0% | 89.3% |
| BANKNIFTY | upstox | 100.0% | -102 … +128  *(99% of bars; extremes reach -194/+191)* | 93.2% | 82.2% | 42.0% |
| NIFTY | dhan | 100.0% | -10 … +10  *(99% of bars; extremes reach -31/+31)* | 100.0% | 100.0% | 90.2% |
| NIFTY | live | 98.5% | -59 … +59  *(99% of bars; extremes reach -60/+60)* | 76.9% | 82.7% | 36.7% |
| NIFTY | upstox | 100.0% | -90 … +136  *(99% of bars; extremes reach -240/+279)* | 93.0% | 83.4% | 46.0% |
| SENSEX | dhan | 100.0% | -10 … +11  *(99% of bars; extremes reach -73/+73)* | 100.0% | 77.7% | 75.3% |
| SENSEX | upstox | 99.6% | -65 … +159  *(99% of bars; extremes reach -184/+227)* | 93.7% | 91.5% | 48.1% |

- **spot** is the underlying at that minute, stored per bar. Moneyness is derived from it, never from a vendor label.
- **implied vol** is quoted by Dhan and **derived** for Upstox, which serves none — solved from put–call parity against the traded forward. The shortfall is deep in-the-money bars, where the price is intrinsic and says nothing about vol; a null there is the honest answer.
- **volume** disagrees between vendors far more than price does. Treat it as indicative.

## How dense each year is

A year with few sessions is a handful of long-dated contracts that happened to be listed early — real bars, but not a chain. **Do not read a backtest over one as a result.**

| Index | Source | Year | Sessions | Strikes | Bars | |
|---|---|---|---:|---:|---:|---|
| BANKNIFTY | dhan | 2021 | 99 | 177 | 2,989,095 | thin |
| BANKNIFTY | dhan | 2022 | 248 | 268 | 7,738,235 |  |
| BANKNIFTY | dhan | 2023 | 246 | 331 | 7,705,233 |  |
| BANKNIFTY | dhan | 2024 | 249 | 393 | 7,698,829 |  |
| BANKNIFTY | dhan | 2025 | 249 | 216 | 7,818,152 |  |
| BANKNIFTY | dhan | 2026 | 153 | 137 | 4,820,330 |  |
| BANKNIFTY | upstox | 2023 | 63 | 38 | 384,445 | thin |
| BANKNIFTY | upstox | 2024 | 211 | 156 | 11,432,862 |  |
| BANKNIFTY | upstox | 2025 | 249 | 231 | 32,419,571 |  |
| BANKNIFTY | upstox | 2026 | 140 | 224 | 21,492,610 |  |
| NIFTY | dhan | 2021 | 95 | 65 | 2,965,844 | thin |
| NIFTY | dhan | 2022 | 248 | 95 | 7,784,982 |  |
| NIFTY | dhan | 2023 | 246 | 121 | 7,722,661 |  |
| NIFTY | dhan | 2024 | 249 | 173 | 7,790,989 |  |
| NIFTY | dhan | 2025 | 249 | 116 | 7,810,435 |  |
| NIFTY | dhan | 2026 | 153 | 112 | 4,822,294 |  |
| NIFTY | live | 2026 | 9 | 121 | 873,194 | thin |
| NIFTY | upstox | 2022 | 31 | 10 | 8,409 | thin |
| NIFTY | upstox | 2023 | 234 | 43 | 430,274 |  |
| NIFTY | upstox | 2024 | 249 | 161 | 20,632,492 |  |
| NIFTY | upstox | 2025 | 249 | 168 | 71,318,869 |  |
| NIFTY | upstox | 2026 | 160 | 178 | 49,399,148 |  |
| SENSEX | dhan | 2023 | 147 | 190 | 3,070,053 |  |
| SENSEX | dhan | 2024 | 249 | 285 | 5,285,874 |  |
| SENSEX | dhan | 2025 | 249 | 316 | 6,866,187 |  |
| SENSEX | dhan | 2026 | 153 | 204 | 4,151,455 |  |
| SENSEX | upstox | 2023 | 10 | 18 | 39,632 | thin |
| SENSEX | upstox | 2024 | 75 | 167 | 5,188,550 | thin |
| SENSEX | upstox | 2025 | 250 | 277 | 34,289,281 |  |
| SENSEX | upstox | 2026 | 157 | 305 | 28,587,428 |  |

## Contract listings

One row per real contract, exactly as the vendor listed it — the weekly/monthly flag, lot size, tick size and freeze quantity in force when it was listed. Only served while a contract is inside the retention window, so what is not captured is gone.

| Index | Contracts | Expiries | Weekly | Monthly | Futures | From | To |
|---|---:|---:|---:|---:|---:|---|---|
| BANKNIFTY | 8,645 | 28 | 1,350 | 7,273 | 22 | 2024-10-01 | 2026-07-28 |
| NIFTY | 20,848 | 100 | 15,278 | 5,548 | 22 | 2024-10-03 | 2026-08-25 |
| SENSEX | 36,849 | 98 | 27,914 | 8,914 | 21 | 2024-10-04 | 2026-08-20 |

### Lot sizes, as the exchange set them

Read off the contracts themselves rather than inferred. **P&L scales linearly with these**, so a multi-year run at one fixed size is wrong on one side of every revision.

Overlapping ranges are not an error: a contract carries the lot size in force when it was **listed**, so a monthly listed before a revision expires with the old size while the weeklies around it carry the new one.

| Index | Lot size | First expiry | Last expiry | Expiries |
|---|---:|---|---|---:|
| BANKNIFTY | 15 | 2024-10-01 | 2025-01-30 | 10 |
| BANKNIFTY | 30 | 2025-02-27 | 2026-07-28 | 12 |
| BANKNIFTY | 35 | 2025-07-31 | 2025-12-30 | 6 |
| NIFTY | 25 | 2024-10-03 | 2025-01-30 | 14 |
| NIFTY | 75 | 2025-01-02 | 2025-12-30 | 52 |
| NIFTY | 65 | 2026-01-06 | 2026-08-25 | 34 |
| SENSEX | 10 | 2024-10-04 | 2025-01-28 | 14 |
| SENSEX | 20 | 2025-01-07 | 2026-08-20 | 84 |

## Forward price series

The expired futures, one series per monthly expiry. This is the market's own forward, which is what Black-76 wants — deriving one from spot instead means assuming a carry rate, and the tell that the assumption is wrong is the call and put implied vols disagreeing at the same strike.

| Index | Contracts | Bars | From | To |
|---|---:|---:|---|---|
| BANKNIFTY | 20 | 482,052 | 2024-04-09 | 2026-07-28 |
| NIFTY | 21 | 482,918 | 2024-07-26 | 2026-07-28 |
| SENSEX | 21 | 350,873 | 2024-09-04 | 2026-07-30 |

## Index spot

Moneyness is derived from this per bar, so a session with no spot has no moneyness and cannot be selected on.

| Symbol | Source | Bars | From | To |
|---|---|---:|---|---|
| BANKNIFTY | dhan | 389,001 | 2022-06-06 | 2026-08-14 |
| NIFTY | dhan | 398,903 | 2022-05-12 | 2026-08-14 |
| NIFTY | live | 2,046 | 2026-08-17 | 2026-08-27 |
| NIFTY 50 | live | 2,046 | 2026-08-17 | 2026-08-27 |
| SENSEX | dhan | 298,435 | 2023-05-30 | 2026-08-14 |

## What is missing, and why

*Not pulled yet* is a command to run. *Vendor holds nothing* is a permanent hole — state it when reporting a result that spans it.

| Index | Expiry | Cause | Detail |
|---|---|---|---|
| BANKNIFTY | 2024-10-01 | vendor holds nothing | 238 of 238 contracts empty (100.0%) |
| NIFTY | 2024-12-26 | vendor holds nothing | 279 of 281 contracts empty (99.3%) |

## Refreshing this file

`make data` rewrites it, in the repository that has the lake. The command walks every
partition and recomputes every table above from the rows themselves. That is the only
reason a coverage table is worth trusting: the moment one is maintained by hand it
starts describing the data somebody remembers rather than the data that is there, and
a backtest run against the difference fails silently.
