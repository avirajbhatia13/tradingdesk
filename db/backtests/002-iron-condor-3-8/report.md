# 002 — Iron condor 3/8

**NIFTY week** · `SELL CE 3 strikes OTM, SELL PE 3 strikes OTM, BUY CE 8 strikes OTM, BUY PE 8 strikes OTM` · 1 lot per leg (lot size 75) · 2021-08-16 → 2026-08-14 · 1,234 sessions over 4.99 years

## Verdict

Made **+₹92,813** over 4.99 years. Against the **₹1.00 L** an account would have needed to survive this strategy's own worst run, that is **+92.42%** in total — **+14.00% a year compounded**.

> Charges consumed 73% of the gross profit (₹2.57 L of ₹3.50 L).


## Capital and return

| | |
|---|---|
| Net P&L | **+₹92,813** |
| Gross, before charges | +₹3.50 L |
| Total charges | ₹2.57 L |
| Peak margin needed | **₹18,578** |
| Typical margin (median) | ₹11,816 |
| Margin per lot (median) | ₹2,954 |
| **Capital you would actually need** | **₹1.00 L** |
| **Return on that capital** | **+92.42%** |
| **Per year, compounded** | **+14.00%** |
| Return on peak margin *(flattering)* | +499.60% |
| Per year on margin, simple | +100.04% |
| Per year on margin, compounded | +43.14% |

*Peak margin is what the account must be able to post on the worst day — the capital actually required. Median is what is typically deployed. Both are SPAN + exposure estimates from the same model the live option-chain page uses.*


> **Capital needed is 5.4x the margin.** Margin is what the exchange blocks per trade; it says nothing about surviving a losing run. An account holding only ₹18,578 would have been wiped out by this strategy's own worst stretch. The figure above is margin plus the deepest cumulative loss it actually produced — a floor, not a recommendation, since history is not a bound.

> **Read return-on-margin carefully.** It is not return on your account. Nobody trades at 100% margin utilisation — a real account holds a buffer for adverse moves, so the return on the money you actually set aside is materially lower than the figure above. Defined-risk structures look spectacular on this measure precisely because their margin is small; that is a real advantage, but it is not the same as making that percentage on your capital.


## The worst it got

| | |
|---|---|
| Max drawdown | **−₹81,850** |
| As % of peak margin | 440.59% |
| Took this long to fall | 572 days (386 trades) |
| Recovered after | 386 days (262 trades) |
| Return ÷ max drawdown | 1.13 |
| Worst losing streak | 10 trades in a row |
| Best winning streak | 18 trades in a row |

*Recovery time matters more than depth. A drawdown you sit in for a year is the one that makes people abandon a working strategy at the bottom.*


## What else could have happened

The drawdown above is one draw. Re-dealing the same 1,234 trades in 2,000 different orders — same trades, same total profit, only the sequence changed:

| | Max drawdown |
|---|---|
| Best 5% of orderings | −₹16,477 |
| Typical (median) | −₹24,091 |
| **What actually happened** | **−₹81,850** |
| **Worst 5% of orderings** | **−₹38,073** |
| Capital needed at the deeper of the two | **₹1.00 L** |

> ⚠️ **The losses arrived in clusters.** The drawdown that actually happened is **3.4x deeper than a typical reshuffle** and 2.1x deeper than the worst of 2,000 of them — reshuffling cannot produce it at all. That is what a losing streak concentrated in a few volatile weeks looks like, and it means the resampled figures below are a floor on the risk rather than a bound on it.


Resampling the trades *with replacement* — varying the outcome as well as the order — **1.5%** of paths ended in a loss, and the middle half of them landed between +₹65,597 and +₹1.21 L.


*Neither figure is a forecast. Both rearrange this strategy's own history, and they assume trades are independent — option selling losses cluster in volatile weeks, so real tail risk is worse than the resampling shows.*


## Trade statistics

| | |
|---|---|
| Trades | 1,234 |
| Win rate | 52.3% (645 win / 589 lose) |
| Average trade | +₹75 |
| Median trade | +₹38 |
| Average win | +₹866 |
| Average loss | −₹790 |
| Best day | +₹7,810 |
| Worst day | −₹4,121 |
| Reward : risk | 1.1 |
| Profit factor | 1.2 |
| Expectancy per trade | +₹75 |
| Average month | +₹1,522 |
| Sharpe / Sortino | 1.01 / 1.8 |

## Where the charges went

| Charge | Amount | Share |
|---|---|---|
| Brokerage | ₹1.97 L | 77% |
| STT | ₹10,825 | 4% |
| Exchange transaction | ₹10,661 | 4% |
| SEBI turnover | ₹25 | 0% |
| Stamp duty | ₹367 | 0% |
| GST | ₹37,463 | 15% |
| **Total** | **₹2.57 L** | |

## What was assumed

| | |
|---|---|
| Entry / exit | 09:20 → 15:15 |
| Slippage | 0.5 points per leg per side |
| Brokerage | ₹20 per executed order |
| Statutory charges | STT, exchange, SEBI, stamp duty and GST, **at the rates in force on each trade's own date** |
| Stop loss | none |
| Target | none |
| Closed by | 1234 time |

## By weekday

| | Trades | P&L | Average | Win rate |
|---|---|---|---|---|
| Monday | 248 | −₹27,423 | −₹111 | 46.4% |
| Tuesday | 247 | +₹54,379 | +₹220 | 59.9% |
| Wednesday | 247 | −₹1,379 | −₹6 | 50.6% |
| Thursday | 246 | +₹1.16 L | +₹473 | 61.8% |
| Friday | 242 | −₹51,927 | −₹215 | 42.6% |
| Weekend | 4 | +₹2,694 | +₹673 | 50.0% |

*A result carried by one weekday is usually an expiry-cycle effect rather than an edge.*


## By year

| | Trades | P&L | Average | Win rate |
|---|---|---|---|---|
| 2021 | 94 | +₹3,981 | +₹42 | 58.5% |
| 2022 | 247 | −₹7,022 | −₹28 | 48.2% |
| 2023 | 245 | −₹49,170 | −₹201 | 43.3% |
| 2024 | 247 | +₹62,022 | +₹251 | 59.9% |
| 2025 | 248 | +₹51,775 | +₹209 | 57.3% |
| 2026 | 153 | +₹31,227 | +₹204 | 49.0% |

*Consistency across years matters more than the total. One exceptional year hiding four flat ones is not a strategy.*


## By month of the year

| | Trades | P&L | Average | Win rate |
|---|---|---|---|---|
| January | 106 | +₹6,641 | +₹63 | 53.8% |
| February | 102 | +₹48,986 | +₹480 | 62.7% |
| March | 98 | +₹17,429 | +₹178 | 52.0% |
| April | 95 | +₹3,751 | +₹39 | 46.3% |
| May | 105 | +₹23,588 | +₹225 | 57.1% |
| June | 104 | +₹9,231 | +₹89 | 51.0% |
| July | 110 | −₹34 | −₹0 | 50.9% |
| August | 103 | +₹5,824 | +₹57 | 55.3% |
| September | 106 | −₹9,536 | −₹90 | 48.1% |
| October | 100 | −₹9,889 | −₹99 | 44.0% |
| November | 97 | −₹8,595 | −₹89 | 48.5% |
| December | 108 | +₹5,418 | +₹50 | 56.5% |

*Every January together, every February together. This separates 'March 2023 was bad' from 'March is bad' — the second is a seasonality claim and needs more than five samples to make.*


## By month

35 of 61 months were profitable (57%).

| Month | Trades | P&L |
|---|---|---|
| 2021-08 | 11 | −₹3,346 |
| 2021-09 | 21 | +₹749 |
| 2021-10 | 20 | +₹881 |
| 2021-11 | 19 | −₹1,892 |
| 2021-12 | 23 | +₹7,589 |
| 2022-01 | 20 | −₹206 |
| 2022-02 | 20 | +₹9,915 |
| 2022-03 | 21 | +₹2,169 |
| 2022-04 | 19 | +₹1,496 |
| 2022-05 | 21 | +₹5,816 |
| 2022-06 | 22 | +₹2,683 |
| 2022-07 | 21 | −₹4,701 |
| 2022-08 | 20 | −₹5,611 |
| 2022-09 | 22 | −₹2,238 |
| 2022-10 | 18 | −₹4,489 |
| 2022-11 | 21 | −₹4,764 |
| 2022-12 | 22 | −₹7,092 |
| 2023-01 | 21 | −₹2,546 |
| 2023-02 | 20 | +₹1,858 |
| 2023-03 | 21 | −₹3,145 |
| 2023-04 | 17 | −₹1,375 |
| 2023-05 | 22 | −₹5,957 |
| 2023-06 | 21 | −₹4,439 |
| 2023-07 | 21 | −₹2,972 |
| 2023-08 | 22 | −₹2,260 |
| 2023-09 | 20 | −₹5,095 |
| 2023-10 | 20 | −₹9,476 |
| 2023-11 | 20 | −₹7,556 |
| 2023-12 | 20 | −₹6,207 |
| 2024-01 | 22 | −₹3,186 |
| 2024-02 | 21 | +₹8,212 |
| 2024-03 | 18 | +₹2,790 |
| 2024-04 | 20 | +₹967 |
| 2024-05 | 22 | +₹8,500 |
| 2024-06 | 19 | +₹7,959 |
| 2024-07 | 22 | +₹2,556 |
| 2024-08 | 21 | +₹9,927 |
| 2024-09 | 21 | −₹2,233 |
| 2024-10 | 22 | +₹9,771 |
| 2024-11 | 18 | +₹5,004 |
| 2024-12 | 21 | +₹11,756 |
| 2025-01 | 23 | +₹10,735 |
| 2025-02 | 20 | +₹18,784 |
| 2025-03 | 19 | +₹2,694 |
| 2025-04 | 19 | +₹6,104 |
| 2025-05 | 21 | +₹10,859 |
| 2025-06 | 21 | +₹1,718 |
| 2025-07 | 23 | +₹3,863 |
| 2025-08 | 19 | +₹4,329 |
| 2025-09 | 22 | −₹719 |
| 2025-10 | 20 | −₹6,576 |
| 2025-11 | 19 | +₹614 |
| 2025-12 | 22 | −₹628 |
| 2026-01 | 20 | +₹1,843 |
| 2026-02 | 21 | +₹10,216 |
| 2026-03 | 19 | +₹12,921 |
| 2026-04 | 20 | −₹3,440 |
| 2026-05 | 19 | +₹4,371 |
| 2026-06 | 21 | +₹1,310 |
| 2026-07 | 23 | +₹1,220 |
| 2026-08 | 10 | +₹2,785 |

---

*Run over 463,360 bars in 1812 ms. Margin, charges and slippage are modelled estimates — see `BACKTESTING.md` for what the data can and cannot support.*

