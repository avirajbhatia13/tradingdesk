# 010 — Thursday straddle: does the 25% stop survive out-of-sample? *(walk-forward)*

**NIFTY week** · `SELL CE ATM, SELL PE ATM` · 2021-08-16 → 2026-08-14

Chose **stop_loss_pct** from history alone, 4 times across the sample, and traded each choice forward blind.

## What a sweep would have promised, and what you would have got

| | Net P&L | Trades | Per trade | Max drawdown |
|---|---|---|---|---|
| A sweep's best setting, chosen with hindsight | **+₹64,898** | 246 | +₹264 | −₹35,957 |
| **Choosing blind and trading forward** | **+₹12,938** | 197 | +₹66 | −₹43,750 |
| Not optimising at all — the middle of the grid | +₹53,984 | 246 | +₹219 | −₹45,800 |

*The first row covers the whole period and the second only the out-of-sample part of it, so compare the per-trade column rather than the totals.*

> ⚠️ **Efficiency 36%.** Only 36% of the in-sample edge carried into the periods it was not chosen on. Below about 50% the optimisation is mostly fitting noise, and the honest expectation for this strategy is the out-of-sample row, not the sweep's headline.


2 distinct settings won across 4 folds.


## Fold by fold

| Chose from | Chose | Traded on | In-sample per trade | Out-of-sample per trade | Out-of-sample P&L |
|---|---|---|---|---|---|
| 2021-08-26 → 2022-08-11 | stop_loss_pct 0.15 | 2022-08-18 → 2023-08-10 | +₹2 | −₹275 | −₹13,491 |
| 2021-08-26 → 2023-08-10 | stop_loss_pct 0.35 | 2023-08-17 → 2024-07-25 | +₹147 | +₹32 | +₹1,592 |
| 2021-08-26 → 2024-07-25 | stop_loss_pct 0.15 | 2024-08-01 → 2025-07-24 | +₹244 | +₹583 | +₹28,549 |
| 2021-08-26 → 2025-07-24 | stop_loss_pct 0.15 | 2025-07-31 → 2026-08-13 | +₹329 | −₹74 | −₹3,712 |

*Anchored windows: each fold chose its setting using only the data to its left, then traded it forward without looking. Selection metric: net_pnl.*


On the out-of-sample record alone the strategy needed **₹2.49 L** of capital and returned **+5.20%** on it.


---

*6 settings were tried. Walk-forward does not remove the multiple-comparisons problem — it measures it. The out-of-sample row is still one path through one history.*

