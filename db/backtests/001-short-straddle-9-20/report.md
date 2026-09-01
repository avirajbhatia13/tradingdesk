# 001 — Short straddle 9:20

**NIFTY week** · `SELL CE ATM, SELL PE ATM` · 1 lot per leg (lot size 75) · 2021-08-16 → 2026-08-14 · 1,232 sessions over 4.99 years

## Verdict

**Lost ₹36,792** over 4.99 years against ₹3.77 L of capital.

> The strategy itself was profitable — **+₹1.17 L before costs** — but charges of ₹1.54 L took it negative. This is a cost problem, not a signal problem.

> ⚠️ The worst drawdown of **−₹1.72 L** was **never recovered** — the curve was still below its old high 421 trades later.


## Capital and return

| | |
|---|---|
| Net P&L | **−₹36,792** |
| Gross, before charges | +₹1.17 L |
| Total charges | ₹1.54 L |
| Peak margin needed | **₹2.05 L** |
| Typical margin (median) | ₹1.29 L |
| Margin per lot (median) | ₹64,497 |
| **Capital you would actually need** | **₹3.77 L** |
| **Return on that capital** | **−9.76%** |
| **Per year, compounded** | **−2.04%** |
| Return on peak margin *(flattering)* | −17.94% |
| Per year on margin, simple | −3.59% |
| Per year on margin, compounded | −3.88% |

*Peak margin is what the account must be able to post on the worst day — the capital actually required. Median is what is typically deployed. Both are SPAN + exposure estimates from the same model the live option-chain page uses.*


> **Capital needed is 1.8x the margin.** Margin is what the exchange blocks per trade; it says nothing about surviving a losing run. An account holding only ₹2.05 L would have been wiped out by this strategy's own worst stretch. The figure above is margin plus the deepest cumulative loss it actually produced — a floor, not a recommendation, since history is not a bound.

> **Read return-on-margin carefully.** It is not return on your account. Nobody trades at 100% margin utilisation — a real account holds a buffer for adverse moves, so the return on the money you actually set aside is materially lower than the figure above. Defined-risk structures look spectacular on this measure precisely because their margin is small; that is a real advantage, but it is not the same as making that percentage on your capital.


## The worst it got

| | |
|---|---|
| Max drawdown | **−₹1.72 L** |
| As % of peak margin | 83.76% |
| Took this long to fall | 1050 days (706 trades) |
| Recovered | **no — still underwater at the end** |
| Return ÷ max drawdown | -0.21 |
| Worst losing streak | 6 trades in a row |
| Best winning streak | 11 trades in a row |

*Recovery time matters more than depth. A drawdown you sit in for a year is the one that makes people abandon a working strategy at the bottom.*


## What else could have happened

The drawdown above is one draw. Re-dealing the same 1,232 trades in 2,000 different orders — same trades, same total profit, only the sequence changed:

| | Max drawdown |
|---|---|
| Best 5% of orderings | −₹1.26 L |
| Typical (median) | −₹1.85 L |
| **What actually happened** | **−₹1.72 L** |
| **Worst 5% of orderings** | **−₹2.70 L** |
| Capital needed at the deeper of the two | **₹4.75 L** |

> **The realised drawdown was on the lucky side.** A bad ordering of these same trades is 1.6x deeper. Size the account off that figure, not off the one history happened to deal.


Resampling the trades *with replacement* — varying the outcome as well as the order — **57.8%** of paths ended in a loss, and the middle half of them landed between −₹1.32 L and +₹70,908.


*Neither figure is a forecast. Both rearrange this strategy's own history, and they assume trades are independent — option selling losses cluster in volatile weeks, so real tail risk is worse than the resampling shows.*


## Trade statistics

| | |
|---|---|
| Trades | 1,232 |
| Win rate | 60.6% (746 win / 486 lose) |
| Average trade | −₹30 |
| Median trade | +₹720 |
| Average win | +₹2,395 |
| Average loss | −₹3,753 |
| Best day | +₹13,946 |
| Worst day | −₹30,511 |
| Reward : risk | 0.64 |
| Profit factor | 0.98 |
| Expectancy per trade | −₹30 |
| Average month | −₹603 |
| Sharpe / Sortino | -0.11 / -0.1 |

*Wins 60.6% of the time but the average loss is bigger than the average win — the classic premium-selling shape. A high win rate alone says nothing; the two must be read together.*


## Where the charges went

| Charge | Amount | Share |
|---|---|---|
| Brokerage | ₹98,560 | 64% |
| STT | ₹16,563 | 11% |
| Exchange transaction | ₹17,343 | 11% |
| SEBI turnover | ₹40 | 0% |
| Stamp duty | ₹594 | 0% |
| GST | ₹20,870 | 14% |
| **Total** | **₹1.54 L** | |

## What was assumed

| | |
|---|---|
| Entry / exit | 09:20 → 15:15 |
| Slippage | 0.5 points per leg per side |
| Brokerage | ₹20 per executed order |
| Statutory charges | STT, exchange, SEBI, stamp duty and GST, **at the rates in force on each trade's own date** |
| Stop loss | none |
| Target | none |
| Closed by | 1232 time |

## By weekday

| | Trades | P&L | Average | Win rate |
|---|---|---|---|---|
| Monday | 248 | −₹13,935 | −₹56 | 61.3% |
| Tuesday | 246 | +₹85,756 | +₹349 | 65.4% |
| Wednesday | 246 | −₹5,616 | −₹23 | 60.6% |
| Thursday | 246 | −₹84,194 | −₹342 | 57.3% |
| Friday | 242 | −₹16,278 | −₹67 | 58.3% |
| Weekend | 4 | −₹2,525 | −₹631 | 50.0% |

*A result carried by one weekday is usually an expiry-cycle effect rather than an edge.*


## By year

| | Trades | P&L | Average | Win rate |
|---|---|---|---|---|
| 2021 | 94 | +₹10,929 | +₹116 | 61.7% |
| 2022 | 247 | −₹75,378 | −₹305 | 56.7% |
| 2023 | 245 | −₹36,665 | −₹150 | 59.6% |
| 2024 | 245 | −₹17,418 | −₹71 | 62.4% |
| 2025 | 248 | +₹1.03 L | +₹414 | 61.3% |
| 2026 | 153 | −₹20,824 | −₹136 | 63.4% |

*Consistency across years matters more than the total. One exceptional year hiding four flat ones is not a strategy.*


## By month of the year

| | Trades | P&L | Average | Win rate |
|---|---|---|---|---|
| January | 106 | −₹22,364 | −₹211 | 60.4% |
| February | 102 | +₹48,377 | +₹474 | 65.7% |
| March | 98 | −₹25,149 | −₹257 | 51.0% |
| April | 95 | −₹34,550 | −₹364 | 55.8% |
| May | 105 | +₹18,470 | +₹176 | 69.5% |
| June | 102 | −₹6,978 | −₹68 | 60.8% |
| July | 110 | +₹10,723 | +₹97 | 66.4% |
| August | 103 | +₹32,083 | +₹311 | 63.1% |
| September | 106 | −₹24,815 | −₹234 | 63.2% |
| October | 100 | −₹12,424 | −₹124 | 55.0% |
| November | 97 | −₹21,876 | −₹226 | 57.7% |
| December | 108 | +₹1,710 | +₹16 | 56.5% |

*Every January together, every February together. This separates 'March 2023 was bad' from 'March is bad' — the second is a seasonality claim and needs more than five samples to make.*


## By month

28 of 61 months were profitable (46%).

| Month | Trades | P&L |
|---|---|---|
| 2021-08 | 11 | −₹6,375 |
| 2021-09 | 21 | +₹5,426 |
| 2021-10 | 20 | +₹6,702 |
| 2021-11 | 19 | −₹9,835 |
| 2021-12 | 23 | +₹15,011 |
| 2022-01 | 20 | −₹13,046 |
| 2022-02 | 20 | −₹9,752 |
| 2022-03 | 21 | +₹9,035 |
| 2022-04 | 19 | −₹11,094 |
| 2022-05 | 21 | +₹4,536 |
| 2022-06 | 22 | −₹9,958 |
| 2022-07 | 21 | −₹2,311 |
| 2022-08 | 20 | −₹10,582 |
| 2022-09 | 22 | +₹3,082 |
| 2022-10 | 18 | −₹4,721 |
| 2022-11 | 21 | −₹1,228 |
| 2022-12 | 22 | −₹29,338 |
| 2023-01 | 21 | +₹12,894 |
| 2023-02 | 20 | +₹15,219 |
| 2023-03 | 21 | −₹15,512 |
| 2023-04 | 17 | +₹7,190 |
| 2023-05 | 22 | −₹2,270 |
| 2023-06 | 21 | −₹5,334 |
| 2023-07 | 21 | −₹6,091 |
| 2023-08 | 22 | −₹2,161 |
| 2023-09 | 20 | −₹6,537 |
| 2023-10 | 20 | −₹14,014 |
| 2023-11 | 20 | +₹3,021 |
| 2023-12 | 20 | −₹23,069 |
| 2024-01 | 22 | −₹28,579 |
| 2024-02 | 21 | +₹6,884 |
| 2024-03 | 18 | −₹6,367 |
| 2024-04 | 20 | −₹3,364 |
| 2024-05 | 22 | −₹3,434 |
| 2024-06 | 17 | +₹8,648 |
| 2024-07 | 22 | +₹1,611 |
| 2024-08 | 21 | +₹27,374 |
| 2024-09 | 21 | −₹34,670 |
| 2024-10 | 22 | +₹3,010 |
| 2024-11 | 18 | −₹15,427 |
| 2024-12 | 21 | +₹26,895 |
| 2025-01 | 23 | +₹14,333 |
| 2025-02 | 20 | +₹52,053 |
| 2025-03 | 19 | −₹10,245 |
| 2025-04 | 19 | −₹1,854 |
| 2025-05 | 21 | +₹10,244 |
| 2025-06 | 21 | −₹2,158 |
| 2025-07 | 23 | +₹7,496 |
| 2025-08 | 19 | +₹14,406 |
| 2025-09 | 22 | +₹7,883 |
| 2025-10 | 20 | −₹3,400 |
| 2025-11 | 19 | +₹1,593 |
| 2025-12 | 22 | +₹12,212 |
| 2026-01 | 20 | −₹7,966 |
| 2026-02 | 21 | −₹16,026 |
| 2026-03 | 19 | −₹2,059 |
| 2026-04 | 20 | −₹25,428 |
| 2026-05 | 19 | +₹9,393 |
| 2026-06 | 21 | +₹1,824 |
| 2026-07 | 23 | +₹10,019 |
| 2026-08 | 10 | +₹9,420 |

---

*Run over 463,011 bars in 1830 ms. Margin, charges and slippage are modelled estimates — see `BACKTESTING.md` for what the data can and cannot support.*

