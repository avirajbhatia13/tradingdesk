# 029 — NIFTY monthly DN strangle: WITH 40pct gap repair

**NIFTY front monthly expiry, dte 5..26** · `SELL CE 20 delta, SELL PE 20 delta · held 20 sessions` · 1 lot per leg (lot size per session) · 2024-10-01 → 2026-07-28 · 21 sessions over 1.82 years

## Verdict

**Lost ₹21,018** over 1.82 years against ₹2.05 L of capital.

> ⚠️ The worst drawdown of **−₹54,100** was **never recovered** — the curve was still below its old high 15 trades later.


## Capital and return

| | |
|---|---|
| Net P&L | **−₹21,018** |
| Gross, before charges | −₹11,344 |
| Total charges | ₹9,674 |
| Peak margin needed | **₹1.51 L** |
| Typical margin (median) | ₹1.31 L |
| Margin per lot (median) | ₹65,275 |
| **Capital you would actually need** | **₹2.05 L** |
| **Return on that capital** | **−10.26%** |
| **Per year, compounded** | **−5.77%** |
| Return on peak margin *(flattering)* | −13.94% |
| Per year on margin, simple | −7.66% |
| Per year on margin, compounded | −7.91% |

*Peak margin is what the account must be able to post on the worst day — the capital actually required. Median is what is typically deployed. Both are SPAN + exposure estimates from the same model the live option-chain page uses.*


> **Capital needed is 1.4x the margin.** Margin is what the exchange blocks per trade; it says nothing about surviving a losing run. An account holding only ₹1.51 L would have been wiped out by this strategy's own worst stretch. The figure above is margin plus the deepest cumulative loss it actually produced — a floor, not a recommendation, since history is not a bound.

> **Read return-on-margin carefully.** It is not return on your account. Nobody trades at 100% margin utilisation — a real account holds a buffer for adverse moves, so the return on the money you actually set aside is materially lower than the figure above. Defined-risk structures look spectacular on this measure precisely because their margin is small; that is a real advantage, but it is not the same as making that percentage on your capital.


## The worst it got

| | |
|---|---|
| Max drawdown | **−₹54,100** |
| As % of peak margin | 35.88% |
| Took this long to fall | 85 days (3 trades) |
| Recovered | **no — still underwater at the end** |
| Return ÷ max drawdown | -0.39 |
| Worst losing streak | 3 trades in a row |
| Best winning streak | 6 trades in a row |

*Recovery time matters more than depth. A drawdown you sit in for a year is the one that makes people abandon a working strategy at the bottom.*


## What else could have happened

The drawdown above is one draw. Re-dealing the same 21 trades in 2,000 different orders — same trades, same total profit, only the sequence changed:

| | Max drawdown |
|---|---|
| Best 5% of orderings | −₹37,177 |
| Typical (median) | −₹50,780 |
| **What actually happened** | **−₹54,100** |
| **Worst 5% of orderings** | **−₹68,331** |
| Capital needed at the deeper of the two | **₹2.19 L** |

Resampling the trades *with replacement* — varying the outcome as well as the order — **64.2%** of paths ended in a loss, and the middle half of them landed between −₹50,104 and +₹12,857.


*Neither figure is a forecast. Both rearrange this strategy's own history, and they assume trades are independent — option selling losses cluster in volatile weeks, so real tail risk is worse than the resampling shows.*


## Trade statistics

| | |
|---|---|
| Trades | 21 |
| Win rate | 71.4% (15 win / 6 lose) |
| Average trade | −₹1,001 |
| Median trade | +₹4,104 |
| Average win | +₹4,036 |
| Average loss | −₹13,592 |
| Best day | +₹4,486 |
| Worst day | −₹37,177 |
| Reward : risk | 0.3 |
| Profit factor | 0.74 |
| Expectancy per trade | −₹1,001 |
| Average month | −₹1,168 |
| Sharpe / Sortino | -1.57 / -1.37 |

*Wins 71.4% of the time but the average loss is bigger than the average win — the classic premium-selling shape. A high win rate alone says nothing; the two must be read together.*


## Where the charges went

| Charge | Amount | Share |
|---|---|---|
| Brokerage | ₹5,880 | 61% |
| STT | ₹1,551 | 16% |
| Exchange transaction | ₹966 | 10% |
| SEBI turnover | ₹3 | 0% |
| Stamp duty | ₹41 | 0% |
| GST | ₹1,233 | 13% |
| **Total** | **₹9,674** | |

## What was assumed

| | |
|---|---|
| Entry / exit | 11:00 → 15:15 |
| Slippage | 0.5 points per leg per side |
| Brokerage | ₹20 per executed order |
| Statutory charges | STT, exchange, SEBI, stamp duty and GST, **at the rates in force on each trade's own date** |
| Stop loss | none |
| Target | ₹4,600 |
| Held for | 20 sessions, averaging 0.0 — **positions are held overnight** |
| Overnight gaps | **realised at the next session's first bar** — a stop cannot fire while the market is shut, so held positions carry gap risk no intraday strategy has |
| Adjusted | when the premium gap between the two sides reaches 40% of the entry credit, close the decayed side and re-sell it at the premium the tested side is now trading; never rolling a leg past the other leg's strike; once they meet the position is a straddle and adjusting stops; buying wings at the straddle's breakevens once it becomes one, which caps the loss |
| Closed by | 7 time, 14 target |

## How the position was repaired

| | |
|---|---|
| Adjustments made | **77** |
| Trades that adjusted at all | 18 of 21 |
| Adjustments per adjusted trade | 4.3 |
| Collapsed into a straddle | 14 |
| Capped with wings | 14 |

*Every repair is a round trip on one leg, so the brokerage line above scales with this count rather than with the number of trades. An adjusted strategy that looks worse than the version that leaves the position alone is usually paying for the repairs rather than being wrong about the market — compare the gross figures before concluding either way.*


## How the strikes were chosen

| Leg | Rule | Landed on | Outside the data |
|---|---|---|---|
| CE | 20 delta | +10 to +26, usually +14 | never |
| PE | 20 delta | +11 to +25, usually +16 | never |

*'Landed on' is where the rule put the leg, in strikes from at-the-money. A rule that ranges widely is doing its job — that variation is the reason it was written as a rule rather than as a fixed strike. 'Outside the data' counts the days the target sat beyond the ±10 strikes the lake holds, where the nearest available contract was substituted.*


## By weekday

| | Trades | P&L | Average | Win rate |
|---|---|---|---|---|
| Monday | 8 | +₹7,896 | +₹987 | 75.0% |
| Tuesday | 1 | −₹37,177 | −₹37,177 | 0.0% |
| Wednesday | 1 | +₹4,261 | +₹4,261 | 100.0% |
| Thursday | 9 | +₹20,305 | +₹2,256 | 88.9% |
| Friday | 1 | −₹12,982 | −₹12,982 | 0.0% |
| Weekend | 1 | −₹3,320 | −₹3,320 | 0.0% |

*A result carried by one weekday is usually an expiry-cycle effect rather than an edge.*


## By year

| | Trades | P&L | Average | Win rate |
|---|---|---|---|---|
| 2024 | 2 | +₹2,649 | +₹1,325 | 50.0% |
| 2025 | 12 | −₹35,488 | −₹2,957 | 66.7% |
| 2026 | 7 | +₹11,821 | +₹1,689 | 85.7% |

*Consistency across years matters more than the total. One exceptional year hiding four flat ones is not a strategy.*


## By month of the year

| | Trades | P&L | Average | Win rate |
|---|---|---|---|---|
| January | 3 | −₹7,715 | −₹2,572 | 66.7% |
| February | 1 | −₹3,320 | −₹3,320 | 0.0% |
| March | 2 | −₹9,342 | −₹4,671 | 50.0% |
| April | 3 | −₹28,919 | −₹9,640 | 66.7% |
| May | 1 | +₹4,486 | +₹4,486 | 100.0% |
| June | 2 | +₹8,555 | +₹4,277 | 100.0% |
| July | 2 | +₹8,366 | +₹4,183 | 100.0% |
| August | 1 | +₹4,461 | +₹4,461 | 100.0% |
| September | 1 | +₹4,374 | +₹4,374 | 100.0% |
| October | 3 | −₹10,048 | −₹3,349 | 33.3% |
| November | 1 | +₹4,137 | +₹4,137 | 100.0% |
| December | 1 | +₹3,948 | +₹3,948 | 100.0% |

*Every January together, every February together. This separates 'March 2023 was bad' from 'March is bad' — the second is a seasonality claim and needs more than five samples to make.*


## By month

12 of 18 months were profitable (67%).

| Month | Trades | P&L |
|---|---|---|
| 2024-10 | 1 | −₹1,488 |
| 2024-11 | 1 | +₹4,137 |
| 2025-01 | 1 | +₹1,022 |
| 2025-02 | 1 | −₹3,320 |
| 2025-03 | 1 | −₹13,603 |
| 2025-04 | 1 | −₹37,177 |
| 2025-05 | 1 | +₹4,486 |
| 2025-06 | 1 | +₹4,450 |
| 2025-07 | 1 | +₹4,431 |
| 2025-08 | 1 | +₹4,461 |
| 2025-09 | 1 | +₹4,374 |
| 2025-10 | 2 | −₹8,559 |
| 2025-12 | 1 | +₹3,948 |
| 2026-01 | 2 | −₹8,737 |
| 2026-03 | 1 | +₹4,261 |
| 2026-04 | 2 | +₹8,258 |
| 2026-06 | 1 | +₹4,104 |
| 2026-07 | 1 | +₹3,935 |

---

*Run over 142,173 bars in 270880 ms. Margin, charges and slippage are modelled estimates — see `BACKTESTING.md` for what the data can and cannot support.*

