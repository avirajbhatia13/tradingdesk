# Trading Desk

**A trading desk for NSE index options, built from scratch for my own option
selling.** A 583-million-bar options data lake, a vectorised backtester that
answers a what-if in about 100 milliseconds, and a live risk desk across three
broker accounts: one definition of a strategy, three runtimes (backtest, paper,
live monitoring), and the apparatus that says whether the thing is doing what I
thought.

![The Portfolio page: where the money is](docs/screenshots/03-portfolio.png)

## At a glance

| | |
|---|---|
| **Data** | **583 million** 1-minute option bars for NIFTY, BANKNIFTY and SENSEX, **2019 → 2026**, 9 GB of Parquet queried in place by DuckDB. No server, no subscription, no cloud |
| **Speed** | First run loads the contracts in ~4 s; every variation after that runs in **~100 ms**. A 64-cell grid takes 18 s, a 4-fold walk-forward 2 s |
| **Honesty** | Sweeps report the *share* of the grid that made money, walk-forwards report how much edge survived out of sample, and every attempt is logged, kept or not (**kept 14 of 46**) |
| **Reproducibility** | **65 numbered runs**, each saved with the exact spec that produced it; `verify` re-runs them all and fails on a one-rupee drift |
| **Realism** | Brokerage and STT at the rates of each trade's own day (two tax-regime changes), lot sizes read off each contract rather than assumed, SPAN and exposure margin, capital sized to the worst drawdown |
| **Live desk** | Three Zerodha accounts in one view, greeks solved from traded prices (the broker sends neither IV nor greeks), Kite's own basket margin, VaR under shocks, four market-data feeds with automatic failover |
| **Tests** | **1,312** in the private repository; the **428** engine tests here run on a clean clone with no data and no credentials |

> **The live platform in these screenshots is not available, and will not be.**
> It logs into real brokerage accounts, mine and my family's, and every number
> on it is somebody's actual money, margin and holdings. What ships in this
> repository is the research half: the backtest engine, its tests, the document
> it is operated from, and six worked runs. The live desk, the market-data lake,
> the backfill tooling and every credential stay private.
>
> Every screenshot is from my own account. The capital in it is small, but it
> is real money and the desk manages it every session, which is the point of
> showing it rather than a demo book.

## New in November 2026: the redesign

The whole interface was rebuilt to a professional standard without losing a
single function: every element id and script hook was kept, and the old design
is archived beside it.

- **A new design system.** Graphite ground, one accent colour, KPI plates laid
  out as a mosaic, a left rail with the research pages nested under it, and a
  glass top bar that keeps the indices and India VIX in view.
- **Jump anywhere with ⌘K.** A command palette for every page, tab and action,
  plus `G`-then-key shortcuts (`G D` dashboard, `G P` portfolio, `G C` console).
- **A Portfolio page.** Where the money is, as a map: every holding sized and
  grouped by the job it does (core funds, the momentum engine, lines to
  convert, liquid), actual against plan, the path from holdings to pledge to
  margin, and a three-step plan to move money into the systematic sleeve,
  ending in this week's orders for the ETF momentum strategy.
- **A faster desk.** Four data-health routes were scanning the lake for ten
  seconds on the event loop, freezing every other request while they ran. They
  now run in a thread pool, so the rest of the desk stays at milliseconds.

---

## The data, and why the backtests run in milliseconds

Historical 1-minute bars for **expired** Indian index options are the hard part.
No single vendor has them all, so the lake was assembled from four sources over
**2019 → 2026** and reconciled where they overlap:

| Source | What it gave | Reach |
|---|---|---|
| **ICICI Breeze** | the full NIFTY chain, every strike, 1-minute | **2019-01-01 → 2026-09**, 209.5M bars |
| **Upstox** | full chain, every strike, with contract identity | 2022-07 → 2026-08, 141.8M bars (NIFTY) + BANKNIFTY + SENSEX |
| **Dhan** | rolling series indexed by moneyness, ±10 strikes | 2021-08 → 2026-08, 38.9M bars per index |
| **My own recorder** | the live feed sampled every 5 s into 1-minute bars, ±60 strikes | since 17 Aug 2026, forever |

Measured out of the lake, not estimated (`DATA.md` in this repo is generated
from it and reproduced verbatim):

- **583 million option bars** across NIFTY, BANKNIFTY and SENSEX, **9.0 GB** of
  zstd Parquet in 739 files, queried in place by DuckDB. No server, no
  subscription, no cloud.
- NIFTY full chain: **1,674 sessions, 454 strikes, 385 expiries**, 2019-01-01
  → 2026-09-02, with **68,709** contract listings carrying the lot size, tick
  size and weekly/monthly flag in force when each was listed.
- Per-bar spot, derived moneyness, implied vol on 90–100% of rows (solved from
  put–call parity where the vendor serves none), open interest and volume.
- Two STT regime changes, three NIFTY lot-size revisions (25 → 75 → 65) and the
  Thursday→Tuesday expiry move all inside the window — every trade is charged
  and sized at the rule in force on its own day.

### Vectorised, so a variation costs 100 ms instead of 30 seconds

The hosted backtesters I used before this treat every run as a fresh job: change
one stop-loss and wait **10–30 seconds** for the answer, per variation. This
engine does the expensive part **once** — contract selection in SQL over the
Parquet, one scan — and everything after that is **numpy over the loaded
matrix**. Nothing iterates minute by minute in Python.

Measured on this machine, on the lake above, in September 2026:

| Run | First run (loads the contracts) | Every variation after |
|---|---|---|
| ATM short straddle, 2 legs, 1,232 sessions (2021–2026), 463k bars | **3.9 s** | **96 ms** median (93–120 ms over 30 variations) |
| Iron condor, 4 legs, same window | 3.6 s | **110 ms** |
| ATM straddle on the full chain, **1,790 sessions, 2019 → 2026** | 35 s | ~100 ms |
| 64-cell grid (stop × target × slippage) from the CLI, straddle | — | **18 s wall** for all 64, load included |
| 20-cell grid from the browser, a delta-selected positional strangle on dated expiries | — | 29 s for all 20 |
| 4-fold walk-forward over 7 settings, same strangle | — | **2.1 s** |

That is roughly **100–300× per variation** once the contracts are in memory,
and it changes what a backtest *is*. A single number is one draw; at 100 ms a
draw you can afford the whole neighbourhood:

- **Sweep** the stop, the target, the slippage and the entry in one grid and
  read the *share of the grid that made money* — a plateau is an edge, a single
  bright cell surrounded by losses is a curve fit. The 64-cell straddle grid
  above reported **3 of 64 settings profitable (5%)**, which is the answer
  "there is no stop that saves this strategy", in eighteen seconds.
- **Walk it forward**: pick the best setting on the past only, trade it blind
  through the next fold, and report what share of the in-sample edge survived.
  Below 50% the tuning was fitting noise. Run 010 in this repo is the honest
  version of run 008: a sweep promised +₹264 a trade with hindsight, choosing
  blind returned +₹66. Efficiency 36%.
- **Count the attempts.** Every run and every grid cell is a chance for
  something to look good by accident, so every attempt is logged, kept or
  discarded — "kept 3 of 28" is on the page, and it is the denominator that
  makes the survivors mean anything.

The engine's own timing line is printed at the foot of every report
(`Run over 463,011 bars in 5860 ms`), so the claim is checkable on every run.

---

## A tour of the desk

Captured on 11 November 2026, after the redesign, with the market closed and on
my account only. The other two accounts on the desk are hidden with the desk's
own per-browser hide, which recomputes every total over what is visible. The
one panel built from desk-wide totals, the P&L since the desk started
recording, is blanked in these images for the same reason.

A few pages only come alive while the market is open: open positions, the live
option chain, the forward-test book and strategies running on live prices.
Those keep their screenshots from **September 2026**, taken during market hours
under the previous design, and are marked as such.

### 1. The dashboard

One screen for the book. Options P&L split into booked, open and today; margin
used and free; the odds of spot touching the nearest breakeven (priced on the
book's own implied vol); greeks in rupees; max loss at expiry against capital;
concentration; the holdings that fund the margin; and each account as a card
that can be hidden from the screen. Everything recomputes over the accounts
that are visible: here, one of three.

![Dashboard](docs/screenshots/01-dashboard.png)

Every page, tab and action is one keystroke away: **⌘K** opens a jump palette,
and `G` then a letter goes straight to a page.

![Jump palette](docs/screenshots/01b-jump-palette.png)

With positions open (September 2026, previous design), they are grouped by
underlying and then by account, so three accounts short the same strike read as
one concentrated risk. Each group carries its payoff at expiry, in 7 days and
now; breakevens and the distance to the nearest one; margin from Kite's own
basket calculator, with the hedge benefit named (**₹2.33 L deployed against
₹2.48 L leg by leg: ₹15,800 saved by hedging**); and legs you can link into a
named structure with its own max profit, max loss and profit left.

![Positions (September 2026)](docs/screenshots/sept-2026/02-positions.png)

### 2. Portfolio: where the money is

New with the redesign. Every holding across the accounts, the pledge under the
options book, and the plan the money is moving towards. The top line answers
the questions that matter before a trade: what is held, what is pledged, how
much margin that pledge buys after the haircut, how much of it the options book
is using, and whether the cash leg of the pledge is large enough to avoid
interest on the shortfall.

![Portfolio](docs/screenshots/03-portfolio.png)

The money as a map. Each block is a holding, sized by value and grouped by the
job it does: core funds, the momentum engine, lines to convert, liquid. The
colour can switch to pledged share, P&L or account, any sleeve can be zoomed,
and the side panel shows each sleeve against its target.

![Money map](docs/screenshots/03b-portfolio-map.png)

From holdings to margin: what is pledged, what the haircut takes, what is left
as collateral, and how much of it the options book has in use, account by
account.

![Holdings to pledge to margin](docs/screenshots/03c-portfolio-flow.png)

And the plan as three steps. **Convert** the lines that are not on a rule, with
what each sale actually frees after charges and an estimate of tax for the
holding period. **Put it to work** across the sleeves. **Every week**, the ETF
momentum strategy's orders for the week against what the account already holds:
rank, the order, the stop (the higher of 12% under entry and 15% under the
highest close since), and an order list to copy. The desk places no orders.

![Convert and the weekly sheet](docs/screenshots/03d-portfolio-convert-v3.png)

### 3. Holdings and collateral

Holdings merged by symbol, with what is pledged, the margin it releases after
the haircut, and how much of the pledge is cash equivalent, which is the number
Zerodha charges interest on. Folded underneath: allocation and concentration,
what is carrying the P&L, how much sits unpledged and what pledging it would
release, and an averaging calculator that shows the P&L *after* a buy beside
the P&L now, because buying at the market lowers the average and does not
reduce the loss. The whole tab exports to Markdown or a print-ready page.

![Holdings](docs/screenshots/02-holdings.png)

### 4. The Console: how the book has actually done

Reconstructed from Zerodha's own exports rather than the live feed, so it
reaches back before the desk existed. Three lines indexed to 100: an equity NAV
(unit-NAV, so buying more stock does not move it), the **options return on the
whole portfolio** (net F&O P&L over the collateral base, compounded daily), and
the two combined. Every figure can be narrowed to any set of accounts and any
range.

![Console](docs/screenshots/04-console.png)

Under it, F&O P&L by month with the portfolio on the month's first trading day
as the base, and every charge head with where the figure came from: a
statement, a dated ledger line, or an estimate, and it says which. Here
margin-shortfall interest is 42.6% of all charges, the kind of leak
that only shows up once every head is itemised.

![Console: by month, and every charge](docs/screenshots/04b-console-months.png)

### 5. Option chain and strategy builder

*September 2026, previous design, market open.* The chain for the three indices
and every F&O stock, priced off the bid/ask mid and the **synthetic forward from
put-call parity** rather than spot, which is why the desk's IVs agree with
Sensibull's. Click a bid to sell it, an ask to buy it, or load a structure. The
builder gives net premium, max profit and loss, breakevens, probability of
profit, combined greeks, a margin estimate, and the payoff at expiry and at T+n,
and can load the live book as its starting basket to ask "what does adding this
do to what I already have".

![Option chain (September 2026)](docs/screenshots/sept-2026/05-option-chain.png)

![Strategy builder (September 2026)](docs/screenshots/sept-2026/06-strategy-builder.png)

### 6. Forward test, and strategies running on live prices

*September 2026, previous design, market open.* A paper book with its own
capital, marked against the same live feed. Entries stamp at the price that was
really on screen, expired legs settle at intrinsic, and the equity curve is
recorded through the session.

![Forward test (September 2026)](docs/screenshots/sept-2026/07-forward-test.png)

Set a saved strategy's status to *paper* and the runner takes it from there:
every session it enters at the strategy's own entry time against live prices,
applies the same exits, and books the result, using the **engine's own
functions**, not a second implementation. That closes the whole "my backtest
worked but live lost money" genre at the source. The page leads with the gap
between what the strategy is making per trade and what the backtest said to
expect. It places no orders.

![Running (September 2026)](docs/screenshots/sept-2026/08-running.png)

### 7. Research: strategies, and the evidence ladder

Every strategy is a named idea that *claims* definitions; the numbered runs are
the evidence and are never edited. What a strategy can say about itself is
derived, never typed: **backtested** (a full run exists), **settings swept**
(the neighbourhood was tested, and what share of it worked), **tested without
hindsight** (a walk-forward exists *and its efficiency held up*). A walk-forward
that ran and failed shows as a warning, because it is worse evidence than never
having run one.

![Saved strategies](docs/screenshots/09-strategies.png)

The research app opens each one as a tree of variants. This weekly
delta-neutral strangle has all three rungs: backtested (run 021, ₹1.22 L), swept
(83% of the grid worked) and walked forward (169% of the in-sample edge
survived).

![A strategy and its evidence](docs/screenshots/09b-research-strategy.png)

### 8. Opening a run: the analysis dashboard

Everything open, nothing collapsed, all of it derived from the stored report, so
every run on disk gets the whole dashboard without re-running. This is run 021,
the strangle above: 20-delta, entered Friday, held two sessions, ₹6,000 target,
no stop.

The headline: net after charges (**₹1,21,945**), the capital you would actually
have needed (**₹1,93,733**: peak margin **plus** the worst drawdown, because an
account sized to the margin alone would have been wiped out by the strategy's
own worst stretch), return on that capital (28.2% a year), Sharpe 3.80, and the
equity curve with how far underwater it went.

![Run: headline and equity](docs/screenshots/10-run-headline.png)

Every session as one square, coloured by what it made: hover for that day's
entry, exit, strikes, MFE/MAE and the itemised charges it paid. A year × month
grid. And the distribution of days, because a profit factor is produced both by
a strategy that wins a little most days and by one carried by three outliers,
and only the shape separates them: here the best five sessions are **13.8% of
gross profit**.

![Run: sessions, months, distribution](docs/screenshots/11-run-sessions.png)

Thirty-odd metrics including Calmar, ulcer index and expectancy; **every**
drawdown episode rather than the worst, with how long each took to recover.

![Run: metrics and drawdowns](docs/screenshots/12-run-metrics.png)

How far each trade ran before it was closed, and the same trades cut by year,
month, weekday, exit reason and days held.

![Run: cuts by year, month, weekday, exit](docs/screenshots/13-run-cuts.png)

The same trades in a different order. The realised drawdown (₹30,731) is one
draw; 2,000 reshuffles give the typical one (₹33,923) and the unlucky one
(₹53,533 at the 5th percentile), and the capital to size off the latter. Then
correlation against whichever other runs you tick: two strategies near +1 are
one bet at double size.

![Run: Monte Carlo and correlation](docs/screenshots/14-run-montecarlo.png)

And what was assumed, said out loud: the legs, lot size, slippage, brokerage,
stop and target, and which sessions were flagged and why.

![Run: what was assumed](docs/screenshots/14b-run-assumptions.png)

### 9. Vary it: a grid in seconds, from the page

*September 2026, previous design.* Any number of parameters, each a list or a
range, full product. This is the 20-cell grid from the timing table, target ×
stop on the weekly delta-neutral strangle, run live from the browser and back
in 29 seconds: **55% of 20 made money**, sorted by net, with the drawdown,
capital and Sharpe of every cell beside it. Tick the ones worth keeping and each
is re-run in full as a numbered child strategy under its parent.

![Sweep (September 2026)](docs/screenshots/sept-2026/15-sweep.png)

### 10. Walk it forward: the same grid without hindsight

*September 2026, previous design.* Chosen on the past, traded on the future it
could not see, 2.1 seconds for four folds. The page says out loud when the
chosen setting *changed* between folds, because that is itself the finding:
there was never a stable optimum.

![Walk-forward (September 2026)](docs/screenshots/sept-2026/16-walkforward.png)

### 11. The lab, and every attempt

Sweep or walk-forward any saved run. Under it, the append-only attempts log:
recording a run costs an id, exploring costs nothing, but every exploration is
still counted, because a denominator made only of the runs somebody chose to
keep is not a denominator. **Kept 14 of 46.**

![Research lab](docs/screenshots/17-research-lab.png)

### 12. Replay any session, minute by minute

The session simulator loads a stored day once and replays it in the browser,
so autoplay and scrubbing cost nothing. This is NIFTY on 4 June 2024, the day
the election result moved the index 6%: 63 strikes, with every premium, IV and
open-interest figure a real print from the lake. What is modelled (the fill,
the payoff, greeks and margin) is labelled as modelled, with how.

![Simulate](docs/screenshots/18-simulate.png)

### 13. Every run, numbered, whether it worked or not

The registry: 65 runs. Returns are on the capital an account would actually
have needed, not on margin. Stars are a shortlist; tick boxes feed the
correlation matrix.

![Saved runs](docs/screenshots/21-saved-runs.png)

### 14. Stealth mode

*September 2026, previous design.* Because the desk aggregates several people's
accounts, showing it to anyone means showing them balances that are not mine.
One keystroke masks every rupee figure, position size and account name **on the
server, at the wire** (the browser never receives them) while market data,
ratios and the payoff curve's shape survive. Turning it off needs a PIN, checked
on the server, because by then the other person is the one at the keyboard.

![Stealth (September 2026)](docs/screenshots/sept-2026/20-stealth.png)

---

## The problem the engine exists to solve

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
assembled by hand from library calls. `verify` re-runs every stored run from its
own spec and fails if a single net figure has drifted by a rupee, which is what
stands between a refactor and silently changing what every stored backtest
means.

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

They were produced against the lake described above. **That data is not in
this repository** — it is licensed, and not mine to redistribute — so the runs
can be read but not re-run here. `DATA.md` describes exactly what it covers:
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
thing testable without shipping 9 GB. The private repository carries 1,312
tests; the 428 here are the engine's.

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
docs/screenshots/           the live desk after the November 2026 redesign, from my own account
docs/screenshots/sept-2026/ pages captured in September with the market open, previous design
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

The live half of the desk — everything in the screenshots above: the
three-account dashboard, the Portfolio page, the Console, the chain and builder, the forward test
and runner, the research pages, stealth mode, the failover chain of market-data
feeds it runs on. Along with the lake, the backfill and recording tooling, and
every credential. Two modules that are here — `quant/risk.py` and
`quant/forward.py` — belong to that dashboard rather than to the engine, and
`risk.py` is missing one test that asserted it reaches the dashboard's totals,
because the dashboard is not here to assert it against.

There is no execution layer anywhere in the project. Orders are placed by hand.
