# A backtest engine for NSE index options

This is the research half of a trading desk I built for my own option selling —
the engine, its tests, the document it is operated from, and six worked runs.
It is here to be read rather than adopted.

---

## The problem it exists to solve

A backtest that is wrong does not raise an exception. It returns a number, the
number is plausible, and you act on it.

Three real examples from this engine's own history, all found after they had
been producing results for a while:

**Legs that quietly followed the money.** Strike selection used to re-evaluate
every minute, so a position re-selected the at-the-money strike as spot moved.
That is not a straddle — it is a strategy that *cannot lose to a directional
move*. It reported a Sharpe of 14.7 and a 92% win rate. On 4 June 2024 it
recorded +₹11,459 for a trade that actually lost ₹24,019. The same short
straddle reads **+₹36.2 L with re-striking on, against −₹36,792 with legs
pinned to the strike they entered.** Nothing errored. Legs are now pinned by
default and `restrike` is opt-in for strategies that genuinely roll.

**A safety rule that was a no-op.** `--dte-min 4 --dte-max 4 --hold 2` returned
P&L byte-identical to `--hold 0`. The days-to-expiry band was filtering the
session list rather than the entry days, so the sessions a position needed to
be *carried through* were removed before the schedule was built. Every position
truncated to a single day, and the report labelled it "cut short by a contract
roll" — a silent no-op wearing the label of a deliberate rule.

**A held position spliced across two contracts.** Expiry was resolved per
session instead of per position, so when the front expired mid-hold the ladder
shifted underneath the trade and later sessions were marked against a contract
it had never traded. That reported **−₹94,168 for a strategy that made
+₹78,284.**

Every one of these produced a plausible number and no error. That is the
failure mode this codebase is organised against, and it is why the tests read
the way they do: many of them pin a specific wrong answer that was once
returned, and their docstrings say which. The section of `BACKTESTING.md`
called *"Conventions that will bite you"* is the running list.

---

## What the engine does about it

**Rules are written the way they are spoken.** The leg grammar is the interface,
so the gap between the rule in your head and the rule that ran is small enough
to see:

```
SELL CE 0          the at-the-money call
SELL CE @120       the call trading nearest ₹120
BUY CE @atm/3 x3   three lots of the call nearest a third of the ATM call's premium
SELL CE 20d        the 20-delta call
SELL PE 1%         the put 1% out of the money
```

Repairs are written the same way — `--adjust "gap>=40%: roll-cheap-to-expensive"`,
`--wings breakeven`. The grammar is wider than it looks; it is specified in
`BACKTESTING.md`.

**Costs are date-scoped, and usually larger than the edge.** STT on options rose
twice inside the test window — 0.0625% to 0.10% in Oct 2024 to 0.15% in Apr 2026
— and exchange fees moved with it. Each trade is charged at the rates in force
on its own day. The baseline short straddle is **+₹1.17 L gross and −₹36,792
net**: a strategy that works and a strategy that pays for itself are different
questions, so every report gives both.

**Lot sizes are read off the contracts, not assumed.** NIFTY went 25 to 75 to 65
inside this window. P&L scales linearly with lot size, so a multi-year run at
one fixed size is wrong on one side of every revision.

**Margin is modelled, and it is the denominator.** SPAN scenario loss plus
exposure on genuinely uncovered shorts, capped at max loss when bounded, never
below the net premium paid. Reports separate *peak margin* from *capital you
would actually need* — for the baseline straddle, ₹2.05 L against ₹3.77 L,
because an account sized to the margin alone would have been wiped out by the
strategy's own worst stretch.

**The reports argue against their own results.** A sweep prints how many
variations you have now looked at, because running enough of them makes one look
significant on noise alone. A walk-forward prints how much of the in-sample edge
actually survived:

> **Efficiency 36%.** Only 36% of the in-sample edge carried into the periods it
> was not chosen on. Below about 50% the optimisation is mostly fitting noise,
> and the honest expectation for this strategy is the out-of-sample row, not the
> sweep's headline.

That is run 010, where a sweep promised +₹264 a trade with hindsight, and
choosing blind and trading forward returned +₹66.

**Every run is numbered, saved and reproducible.** One command runs the strategy,
computes the report, allocates the next id and writes `db/backtests/NNN-slug/` —
the report, the result, and the exact spec that produced it. Results are never
assembled by hand from library calls.

---

## The six runs in this repository

Read `report.md` in any of them; they are written to be read on their own.

| | Run | Why it is here |
|---|---|---|
| 001 | Short straddle 9:20 | The baseline, and it **loses** — +₹1.17 L gross, −₹36,792 net. A cost problem, not a signal problem |
| 002 | Iron condor 3/8 | Same window, profitable, defined risk — and the return-on-margin caveat that makes it look better than it is |
| 008 | Is 25% the lucky stop? | A parameter sweep: 16 of 18 settings profitable, with the multiple-comparisons warning printed alongside |
| 010 | Does the 25% stop survive? | Walk-forward. The honest version of run 008, and the reason to distrust it |
| 022 | Book with DN weekly strangle | A portfolio backtest — several strategies sharing one capital base |
| 029 | Monthly DN strangle, with gap repair | Mid-trade adjustment: a repair rule expressed in the grammar, and tested |

They were produced against roughly 5.7 GB of 1-minute vendor data. **That data is
not in this repository** — it is licensed, and not mine to redistribute — so the
runs can be read but not re-run here. `DATA.md` describes exactly what it covers:
which years, which strikes, and which columns are actually populated.

---

## Running it

Python 3.12. Four dependencies: duckdb, pyarrow, numpy, pytest.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest            # 428 tests, ~30s
```

**The test suite is the part you can actually run.** It needs no data and no
credentials: the tests that exercise the engine build small synthetic lakes in a
temporary directory and point the loader at them, which is what makes the whole
thing testable without shipping 5.7 GB.

The CLI runs, and reads the six saved runs:

```bash
cd backend
../.venv/bin/python -m tools.backtest list
../.venv/bin/python -m tools.backtest show 010
```

`tools.backtest run` will start and then tell you it found no data, which is the
intended behaviour rather than a crash:

```
no trades: no trades — is the lake backfilled for this range?
```

---

## Layout

```
BACKTESTING.md              the contract: grammar, coverage, limits, how to report a result
DATA.md                     what the data covers — generated from the lake, reproduced verbatim
backend/app/backtest/       the engine: strike selection, costs, sweeps, walk-forward,
                            Monte Carlo, portfolios, mid-trade adjustment, the run registry
backend/app/quant/          Black-Scholes and Black-76 greeks, payoff curves, risk, strategy specs
backend/app/data/           the Parquet lake loader and its schema
backend/tools/backtest.py   the CLI every run goes through
backend/tests/              428 tests
db/backtests/               six worked runs
```

**Reading order.** `BACKTESTING.md` first — it is the contract, and the two
sections worth reading even if you never run anything are *"Conventions that
will bite you"* and *"What the data cannot support"*. Then a report, `010` for
preference. Then `backend/app/backtest/engine.py`.

Two things to expect in `BACKTESTING.md`: it is reproduced verbatim from the
private repository, so it opens with instructions addressed to an AI assistant
working in that repo, and its shell examples carry that machine's paths. Both
are left alone, because the document is worth more unedited than tidied.

---

## What is not here

The live half of the desk: a three-account dashboard with portfolio greeks,
margin, payoff curves and VaR under shock, running against a failover chain of
market-data feeds. Along with the lake, the backfill and recording tooling, and
every credential. Two modules that are here — `quant/risk.py` and
`quant/forward.py` — belong to that dashboard rather than to the engine, and
`risk.py` is missing one test that asserted it reaches the dashboard's totals,
because the dashboard is not here to assert it against.

There is no execution layer anywhere in the project. Orders are placed by hand.
