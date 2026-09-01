# 008 — Thursday straddle: is 25% the lucky stop? *(parameter sweep)*

**NIFTY week** · `SELL CE ATM, SELL PE ATM` · 2021-08-16 → 2026-08-14

Varied **stop_loss_pct** and **exit_time** across 18 combinations.

## Is this an edge or a lucky setting?

**16 of 18 settings were profitable (89%)**, with a median of +₹27,928.

> Most of the grid works. That is what an edge looks like — the strategy is not depending on one setting being right.

> The best cell is within 28% of its neighbours — a flat surface, which is the good case.


*You have now looked at 18 variations. Run enough and one of them looks significant on noise alone; that is why this number is printed rather than the best one alone.*


## Net P&L

Rows: **stop_loss_pct** · columns: **exit_time**

| | 14:00 | 14:45 | 15:15 |
|---|---|---|---|
| **0.15** | +₹17,021 | +₹40,737 | +₹60,703 |
| **0.2** | +₹10,750 | +₹39,853 | +₹53,984 |
| **0.25** | +₹23,018 | +₹54,886 | +₹64,898 |
| **0.3** | +₹11,607 | +₹34,729 | +₹42,644 |
| **0.35** | −₹451 | +₹24,604 | +₹31,253 |
| **0.4** | −₹8,686 | +₹9,817 | +₹12,249 |

## Max drawdown

| | 14:00 | 14:45 | 15:15 |
|---|---|---|---|
| **0.15** | −₹42,922 | −₹42,703 | −₹38,086 |
| **0.2** | −₹46,363 | −₹50,425 | −₹45,800 |
| **0.25** | −₹32,382 | −₹40,751 | −₹35,957 |
| **0.3** | −₹33,695 | −₹46,721 | −₹41,222 |
| **0.35** | −₹41,195 | −₹45,877 | −₹39,804 |
| **0.4** | −₹49,197 | −₹51,801 | −₹45,136 |

## The best and the worst of it

| | Setting | Net P&L | Max drawdown | Win rate |
|---|---|---|---|---|
| Best | stop_loss_pct 0.25, exit_time 15:15 | +₹64,898 | −₹35,957 | 42.3% |
| Worst | stop_loss_pct 0.4, exit_time 14:00 | −₹8,686 | −₹49,197 | 52.8% |

*Margin is estimated once from the base strategy and reused across the grid, so the cells share a denominator and are comparable. Run the setting you choose as its own numbered backtest before trading it — that run gets the full report, its own margin estimate and its own Monte Carlo.*

