# Backtesting — read this first

**If you are an assistant working in this repository, this file is the contract.
Read it before touching anything else. Run backtests only through the commands
below; do not assemble results from library calls.**

Aviraj is an NSE index-option seller. This system holds his own 1-minute market
data and runs backtests on it. Every run is numbered, saved, and reproducible.

---

## Running a backtest

One command. It runs the strategy, computes the full report, allocates the next
id, and writes everything to disk.

```bash
cd ~/Projects/trading/backend
../.venv/bin/python -m tools.backtest run \
    --name "Short straddle 9:20" \
    --legs "SELL CE 0, SELL PE 0"
```

It prints the finished report and saves `db/backtests/NNN-slug/`.

### Legs

`SIDE TYPE <how to pick the strike> [xLOTS] [roll]`, comma separated. The
strike-picking part is written the way the rule is spoken:

| Written | Means |
|---|---|
| `SELL CE 0` | Sell the at-the-money call |
| `BUY PE 5` | Buy the put five strikes out of the money |
| `SELL CE -2` | Sell the call two strikes **in** the money |
| `SELL CE @120` | Sell the call trading nearest **₹120** |
| `BUY CE @atm/3` | Buy the call nearest **a third of the ATM call's premium** |
| `BUY PE @atm/3` | …and the put nearest a third of the ATM **put** — each side reads its own reference |
| `BUY CE @+5/2` | Half the premium of the 5-strikes-OTM call |
| `SELL CE 20d` | The **20-delta** call |
| `SELL PE 1%` | The put **1% out of the money** |
| `SELL CE 200pt` | The call **200 points** out of the money |
| `BUY CE k23000` | The **23000** call, by strike |
| `SELL CE 0 x2` | Two lots |
| `SELL CE 0 roll` | Re-strike as spot moves — **see the warning below** |

**Moneyness is signed and means *strikes out of the money*, on both sides.**
`+5` is an OTM call and an OTM put alike. `-3` is in the money either side.
Available range is **−10 to +10** — see coverage below.

**Delta is computed, not stored.** No vendor here serves greeks (Dhan silently
ignores the field — verified by probing). Delta comes from the IV they do serve,
through the same Black-76 core the live dashboard prices with. The weak input is
time to expiry, which rolling data does not carry; a delta target on expiry day
is indicative rather than exact.

### Options

| Flag | Default | Notes |
|---|---|---|
| `--underlying` | `NIFTY` | also `BANKNIFTY`, `SENSEX`, `BANKEX` |
| `--series` | `WEEK` | or `MONTH`. The **rolling** series — Dhan's data, ±10 strikes |
| `--expiry` | none | trade a real expiry **date** instead: `front`, `next`, or an index. **This is what reaches the full chain** |
| `--expiry-kind` | `any` | `weekly` or `monthly`, from the vendor's own flag |
| `--dte-min` / `--dte-max` | none | days to expiry band. `--dte-max 0` is expiry day only; `--dte-min 1` never holds into the last session. Needs `--expiry` |
| `--start` / `--end` | whole lake | ISO dates |
| `--entry` / `--exit` | `09:20` / `15:15` | 24h, must be inside 09:15–15:30 |
| `--stop` / `--target` | none | rupees on the combined position |
| `--stop-pct` / `--target-pct` | none | fraction of the credit, e.g. `0.3` |
| `--trail` / `--trail-pct` | none | give back at most this from the best the trade has been |
| `--trail-trigger` / `--trail-trigger-pct` | arms immediately | profit before the trail arms |
| `--breakeven` / `--breakeven-pct` | none | profit after which the stop moves to entry |
| `--leg-stop-pct` / `--leg-stop-points` | none | per-leg stop, on that leg's own entry premium |
| `--leg-action` | `all` | `all` closes everything; `leg` closes only the breached leg |
| `--adjust` | none | repair the position while it is open — see **Adjusting a position** below. Repeatable. **Needs `--expiry`** |
| `--adjust-max` | unlimited | cap the repairs per position |
| `--allow-crossover` | off | let a rolled leg pass the other leg's strike |
| `--wings` | none | `breakeven`, or a number of strikes — buy protective wings once the position becomes a straddle |
| `--hold` | `0` | sessions to hold for. `0` is intraday; anything else holds **overnight** |
| `--re-entries` | `0` | extra attempts per day — **re-enters the same strikes** |
| `--re-entry-on` | `stop` | `stop`, `target`, or `both` |
| `--re-entry-gap` | `1` | minutes to wait before going again |
| `--min-iv` / `--max-iv` | none | only enter when ATM IV is inside this, in decimals (`0.12`) |
| `--gap-min` / `--gap-max` | none | only enter when the open gapped this much, in % |
| `--move-min` / `--move-max` | none | only enter when spot has moved this % from the day's open |
| `--weekdays` | all | `0`=Mon, e.g. `0,2,4` |
| `--lot-size` | `75` | one size for the whole run — **see the warning below** |
| `--lot-calendar` | off | size each session by the lot size actually in force |
| `--slippage` | `0.5` | points per leg per side |
| `--brokerage` | `20` | per executed order |
| `--notes` | — | why this run exists; it goes in the record |
| `--dry-run` | — | print without saving or consuming an id |

**Lot sizes change, and P&L scales linearly with them.** Verified against live
Upstox contract listings on 17 Aug 2026:

| | Changes |
|---|---|
| NIFTY | 25 → **75** (Feb 2025) → **65** (Jan 2026) |
| BANKNIFTY | 15 → 30 (Feb 2025) → 35 (Jul 2025) → **30** (Jan 2026) |
| SENSEX | 10 → **20** (Feb 2025) |

So a multi-year run at one fixed size is wrong on one side of every revision —
a NIFTY run pinned at 75 reports three times the rupees that were actually
available in late 2024. Return *on capital* is roughly preserved (margin scales
too), so this bites hardest when sizing an account rather than when ranking
strategies.

`--lot-calendar` fixes it where the calendar reaches, which is **2024-10
onwards only** — Upstox's retention window. Earlier sessions fall back to
`--lot-size` and the report says how many did, at the top, in bold. It is off
by default because turning it on changes the P&L of every run that spans a
revision, and every run already on disk was produced without it.

### Choosing which expiry to trade

There are two ways to say which contract a run is about, because there are two
kinds of row in the lake.

**`--series WEEK`** is the rolling one. Dhan's endpoint never names the
contract it returns, so its rows carry no expiry date at all — `WEEK` means
"whatever the front weekly was on this date". Five years deep, ±10 strikes
wide, and it is what every run already on disk used.

**`--expiry front`** names a real date. Upstox's expired-contract data and our
own recorder both identify the instrument, so their rows carry an expiry and no
series — which is exactly why `--series` could not see them. `front` is the
nearest expiry live on the session, `next` the one after, and an integer goes
further out.

```bash
# a strangle 25 strikes wide — impossible on the rolling data, which stops at 10
../.venv/bin/python -m tools.backtest run \
    --name "Wide strangle" --legs "SELL CE 25, SELL PE 25" --expiry front

# expiry day only
../.venv/bin/python -m tools.backtest run \
    --name "0 DTE straddle" --legs "SELL CE 0, SELL PE 0" \
    --expiry front --dte-max 0

# the monthly, never held into its last session
../.venv/bin/python -m tools.backtest run \
    --name "Monthly straddle" --legs "SELL CE 0, SELL PE 0" \
    --expiry front --expiry-kind monthly --dte-min 1 --hold 5
```

**The two agree where they overlap, which is how this was checked.** Running
the same ATM straddle both ways over June–August 2026: every shared session
picked the identical strike, the entry-premium ratio had a median of **1.000**
— the same contract — and the net P&L differed by a median of **₹1.86**, which
is the two vendors' tick-level price difference and nothing else.

**A session whose contract is missing is skipped, not substituted.** On any day
five or six weeklies are live, all with a strike at the money, all printing in
the same minute. If the expiry the rule names has no bars in the lake, the run
drops that session and says so — it does not quietly promote the next one. This
is not hypothetical: during development the backfill had not reached the
2026-06-09 weekly, and ranking only what was on disk put the 14-day contract in
the front slot. The straddle entered at 576 points instead of 114 — a different
strategy, priced perfectly, with nothing anywhere saying so. Read the skipped
count; it usually means finish the backfill and re-run.

**Time to expiry stops being an approximation.** Rolling data forces the engine
to assume half a cycle. A dated contract knows, so delta selection and the
margin model both use the real remaining life — which on expiry day is the
difference between six hours and three and a half days.

### Sweeping a grid

Same flags, plus `--over` for each axis. Two axes produce a heatmap.

```bash
../.venv/bin/python -m tools.backtest sweep \
    --name "Straddle: stop vs target" \
    --legs "SELL CE 0, SELL PE 0" \
    --over "stop_loss_pct=0.2,0.3,0.4,0.5" \
    --over "target_pct=0.2,0.3,0.4,0.5"
```

A sweep takes its id from the same sequence as a run, deliberately: it is
dozens of backtests, and numbering it separately would let forty attempts hide
behind one entry. Sixteen cells take about four seconds, because every setting
that does not change *which contracts are fetched* reuses the cached leg matrix.

**Read a sweep by its shape, not its peak.** The report leads with what share of
the grid was profitable and whether the best cell's neighbours agree with it. A
plateau is an edge; a single bright cell surrounded by losses is a curve fit,
and it will be somewhere else next year.

### Testing it without hindsight

A sweep picks its best cell knowing the whole period, including the part you are
pretending to trade forward into. `walkforward` picks the setting from earlier
data only, trades it blind through what came next, and reports how much of the
edge survived:

```bash
../.venv/bin/python -m tools.backtest walkforward \
    --name "Straddle: does the 25% stop survive?" \
    --legs "SELL CE 0, SELL PE 0" --weekdays 3 \
    --over "stop_loss_pct=0.15,0.2,0.25,0.3,0.35,0.4" --folds 4
```

**The number to read is the efficiency** — out-of-sample profit per trade as a
share of in-sample. Below about 50% the optimisation is mostly fitting noise and
the honest expectation is the out-of-sample row, not the sweep's headline. Also
watch whether the winning setting *changed* every fold: if it did, there was
never a stable optimum to find.

Measured on the ATM Thursday straddle: the sweep's best cell is **+₹64,898**,
choosing blind and trading forward is **+₹12,938**, and *not optimising at all*
— taking the middle of the grid — is **+₹53,984**. The optimisation actively
hurt.

This is cheap for the same reason sweeps are: each grid cell runs once over the
whole range and its trades are *sliced* by date, which is exact because the
engine carries no state between sessions. A 5-fold walk-forward over 25 settings
costs 25 backtests, not 125.

### Saving a strategy

A run is one attempt; a **strategy** is the definition you would deploy. Promote
one when it is worth keeping:

```bash
../.venv/bin/python -m tools.backtest save 004 \
    --name "ATM straddle, 25% SL, Thursdays" --lots 2
../.venv/bin/python -m tools.backtest strategies
```

It appears on the dashboard's **Strategies** page, and the run detail page has a
**Save as strategy** button that does the same thing.

**A strategy never claims to be validated.** It stores no quality figure at all.
Every saved run is matched to it by a structural fingerprint — legs, underlying,
series, clock, weekdays, but *not* stop or target — so a backtest, a sweep over
six stops, and a walk-forward choosing between them are all filed as evidence
about the same strategy. What it can claim is then derived:

| | |
|---|---|
| Backtested | at least one full run exists |
| Settings swept | the neighbouring settings were tested |
| Tested without hindsight | a walk-forward exists **and its efficiency held up** |

A walk-forward that ran and *failed* is shown as a warning rather than a tick,
because it is worse evidence than never having run one. Status (`draft`,
`paper`, `live`, `retired`) is yours to set and means intent only — **the app
still places no orders.**

The definition itself is not editable once saved. A changed definition is a
different strategy, and editing in place would silently detach it from the runs
that tested it.

### Holding across sessions

`--hold 3` keeps the position open for three sessions instead of closing the
same day. Everything else is unchanged: the same selectors, the same stop,
target, trail and per-leg rules, evaluated over a longer path.

**A hold never crosses a contract roll.** The lake stores a *rolling* series —
`WEEK` means "whatever the front weekly is on this date" — so the same strike a
week later is a different contract, and carrying a position across that boundary
would splice two of them into one price path. The hold is truncated at expiry
instead, and the report says how often that happened.

The expiry boundaries are **recovered from the data**. Black-76 prices an option
from forward, strike, time and vol; the lake has price, strike, spot and the
vendor's vol, which leaves time as the only unknown, so it is solved for by
bisection. Inside a cycle it can only fall, so any increase is a roll. Measured
on NIFTY weeklies: 1,235 of 1,235 sessions solved, the smallest jump was 3.3
days, and it recovers the real Thursday→Tuesday expiry change of September 2025
without being told about it. See `expiries.py`.

Two things a held position has that an intraday one does not:

- **Overnight gap risk.** A stop cannot fire while the market is shut, so a gap
  is realised at the next session's first bar. That is correct, and it is a risk
  no intraday strategy carries.
- **Overnight margin.** The position is funded across sessions, not just within
  one.

Not combinable with `--re-entries` (a within-the-day rule) or a re-striking leg
(across a roll, "the 3rd OTM call" points at a different contract). Both are
refused rather than approximated.

Measured on the baseline: the ATM straddle is **−₹36,792 intraday** and
**+₹8.25 L held three sessions** — mostly because it pays charges 440 times
instead of 1,232, and collects far more decay per entry.

### Adjusting a position

Most strategies people describe carry a repair rule, and until recently this
engine refused all of them. It no longer does. `--adjust` takes the rule the way
it is spoken:

```bash
../.venv/bin/python -m tools.backtest run \
    --name "Delta-neutral strangle, 40% gap repair" \
    --legs "SELL CE 20d, SELL PE 20d" \
    --expiry front --expiry-kind weekly --weekdays 4 --hold 2 \
    --adjust "gap>=40%: roll-cheap-to-expensive" --wings breakeven
```

| Written | Means |
|---|---|
| `gap>=40%: roll-cheap-to-expensive` | when the premium difference between the two short legs reaches 40% of the **entry** credit, close the decayed side and re-sell it at the premium the tested side is now trading |
| `gap>=60pt: close-cheap` | same trigger in points; book the decayed side and leave it off |
| `loss>=50%: roll-cheap-to-expensive` | trigger on the position's loss instead of the gap |

`%` is a fraction of the credit standing **at entry**, `pt` is absolute points.
The two are kept strictly apart, because reading one as the other tests the rule
at 40x or 1/40th of its intended size and nothing says so.

**`--wings breakeven`** buys an OTM call and put at the straddle's own
breakevens once the strangle has collapsed onto one strike, which is the iron-fly
conversion these strategies end with. Both wings are placed the same distance
from the centre, as the rule requires.

**Crossover is refused by default.** A rolled leg never passes the strike of the
leg it is rolling towards; when they meet, the position *is* a straddle and
adjusting stops. `--allow-crossover` turns that off.

**This path is not the vectorised one.** The basket changes while the position
is open, so the run is a stateful walk over minutes — roughly 50x slower per
session, which is affordable because these are weekly entries, not daily ones. A
two-year weekly strategy takes a couple of minutes rather than a couple of
seconds.

That cost lands on `verify` too, which re-runs every stored run: once a few
adjusted runs are on disk it goes from about a minute to fifteen. Budget for it
rather than assuming it hung, and prefer `verify` before and after an engine
change rather than casually.

**Read the brokerage line before judging the rule.** Every repair is a round
trip on one leg. Measured on the NIFTY weekly 20-delta strangle, the 40% rule
fired **171 times across 47 trades** — and each of those is two more orders. The
report's *How the position was repaired* section exists so that cost is visible
next to the result rather than buried in the net.

**What it still cannot do:** `--series`. Re-selecting a strike mid-trade needs
the whole chain at that minute, and the rolling series holds ±10 strikes with no
contract identity. The run is refused rather than approximated.

### Running several together

A book is not the sum of its parts, and the difference runs both ways:

```bash
../.venv/bin/python -m tools.backtest portfolio \
    --name "Condor + Thursday straddle" \
    --hold "iron-condor-3-8:1,atm-straddle-25-sl-thursdays:2"
```

**Margin nets.** The exchange sees one position per underlying, not one per
strategy, so a short call held by one strategy under a long call held by another
is hedged whether or not you thought of them as related. The book is margined on
the union of each session's open legs, through the same SPAN model.

**Drawdowns partly cancel** — unless the members lose on the same days, which is
what the correlation matrix is there to tell you. Netting margin does nothing
about that risk.

The report leads with what combining changed, and separates the two effects,
because they have different causes. Measured on three of the saved strategies:

| | One by one | Together |
|---|---|---|
| Peak margin | ₹4.29 L | ₹4.22 L (−1.7%) |
| Worst drawdown | −₹1.89 L | −₹1.29 L (−31.6%) |
| **Capital needed** | ₹6.17 L | **₹5.51 L (−10.8%)** |

Almost all the saving came from the drawdowns, not the margin — because all
three were live together on only 12.5% of sessions, and netting only helps where
positions actually overlap. The report says that too.

Dates default to the **overlap** of the members' ranges, so the comparison is
like for like. Sizes are per member; the same strategy may appear twice at
different sizes, which is how one idea gets split across accounts.

The dashboard does this interactively: tick strategies on the **Strategies**
page, size them, and hit *Test this book*. Nothing there is saved.

### Running it forward

Set a saved strategy's status to **paper** and the runner takes it from there:
every session it enters at the strategy's own entry time against **live
prices**, tracks it, applies the same exits, and books the result. Watch it
under **Running**. It places no orders.

The point is the comparison. The backtest had to assume 0.5 points of slippage,
fills at the minute's close, and that the last traded price was fair. Forward
running tests all three, and the page leads with the gap between what the
strategy is making per trade and what the backtest said to expect — plus the
spread actually quoted against the spread assumed.

**The runner calls the engine's own functions** — the same `Selector` objects,
the same `_first_exit`, the same `CostModel`. Nothing in it re-implements a rule
the backtest already has, because any rule living in two places will eventually
disagree, and that disagreement is the whole "my backtest worked but live lost
money" genre. `test_the_runner_and_the_engine_agree` asserts it directly: same
strategy, same prices, same answer.

What it cannot do forward: **re-striking legs** (they need the whole chain every
minute) and **gap or intraday-move filters** when the feed is not carrying index
OHLC. Both mark the day `blocked` rather than trading something different —
and `blocked` is kept distinct from `stood aside`, because a filter declining to
trade is the strategy working and a dead feed is not.

### Other commands

```bash
../.venv/bin/python -m tools.backtest list           # every saved run
../.venv/bin/python -m tools.backtest show 001       # one report
../.venv/bin/python -m tools.backtest show 001 --json
../.venv/bin/python -m tools.backtest star 002,004   # bookmark a shortlist
../.venv/bin/python -m tools.backtest star 002 --clear
../.venv/bin/python -m tools.backtest correlate --ids 001,002,004
```

```bash
../.venv/bin/python -m tools.backtest rerender            # every run
../.venv/bin/python -m tools.backtest rerender --ids 004  # just this one
```

`rerender` re-runs a saved run from its own `spec.json` and rewrites its
`result.json` and `report.md` **in place, keeping its id** — how a run recorded
months ago gains a report section added since. It checks the net P&L against
the stored figure first and leaves a drifted run untouched: re-baselining is a
deliberate decision with a note attached, not a side effect of re-rendering.
Sweeps, walk-forwards and portfolios are skipped.

When the drift was a *fix* and the stored number was wrong, re-baseline it
explicitly:

```bash
../.venv/bin/python -m tools.backtest rerender --ids 016 --rebaseline \
    --reason "the stored figure came from the per-session expiry bug; see PLAN.md"
```

`--reason` is required, and it is appended to that run's own notes with the old
and new figures — so "why is this number different from the one I remember" is
answered where the question gets asked. Without the flag a drifted run is still
refused.

`correlate` without `--ids` does every run, which stops being useful past a
handful. The dashboard's **Backtest history** page does the same thing with
checkboxes: tick the runs you are deciding between, hit **Compare selected**,
and star the ones worth keeping on a shortlist.

Saved runs are also browsable in the dashboard under **Algo Trade → Backtest
history**, which adds things prose cannot do: a month-by-month grid, a
**day-by-day calendar**, a Monte Carlo panel, a what-if bar that re-runs the
same strategy with different days or stops in about a second, an interactive
sweep, a walk-forward panel, and bookmarking plus tick-box selection so a
correlation matrix covers the handful of runs you are choosing between rather
than all of them. **Nothing the dashboard runs is saved** — exploring is free,
recording costs an id.

### The day view, and what it flags

Every session the strategy traded is one cell, hoverable for that day's entry,
exit, strikes, MFE/MAE and the **itemised charges it actually paid**. A month is
an average; this is where a result that looks like an edge turns out to be four
days.

Each day also carries how good the data under it was, measured over the minutes
the position was open, and four heuristics mark days worth opening:

| Flag | Means | Read it as |
|---|---|---|
| **outlier** | P&L more than 5 robust σ from the median | a real move, or a bad print — open it |
| **stale** | ≥40% of held minutes had an *unchanged* basket price | the last traded price was being repeated |
| **gapped** | ≥10% of held minutes had **no** price at all | a leg stopped printing |
| **truncated** | the hold was cut short by a contract roll | expected; see `--hold` |

**`gapped` is the one that questions the result rather than the strategy.** A
minute only counts when *every* leg printed, so on a gapped day the stop could
have been breached in a minute the engine could not see. Measured on the saved
runs, this is not rare and not evenly spread: the NIFTY ATM straddle gaps on
**0** of 1,232 sessions, while the BANKNIFTY 20-delta strangle gaps on **547 of
1,226** — 45% — because a delta-selected strike lands far enough out that it
often does not trade. The same figure for the ±8 iron condor is 283 of 1,234.
The wings are where the data thins out, and the flag is how you see it.

`stale` is deliberately separate, because a genuinely quiet afternoon looks the
same in the P&L. It isolated NSE's thin Saturday special session of 18 May 2024
(71.6% of minutes unchanged) while leaving the normal Budget-day Saturdays
alone.

The panel leads with **net excluding the flagged days**, which is the readable
form of the question — the baseline straddle is −₹36,792 overall and **+₹5.07 L
without its 39 flagged days**. A share of a near-zero net is a meaningless
percentage; the counterfactual is not.

None of this is a verdict. A flag marks a day to open, never a day that is
wrong.

---

## Translating a strategy into a run

**This is where results go wrong.** A strategy described in prose — from a
video, a blog, a friend — usually contains at least one rule this engine cannot
express. Approximating it silently produces a confident, wrong answer.

**The rule: express what fits, state plainly what does not, and record the gap
in `--notes` so it stays attached to the result forever.** If the mismatch is
big enough that the run would not answer the question, say so and do not run it.

### What the engine can express

| | |
|---|---|
| Legs | any mix of CE/PE, buy/sell, multiple lots |
| Strike choice | moneyness, target premium, a **fraction of another strike's premium**, delta, % of spot, points from spot, absolute strike |
| Entry | one fixed clock time |
| Exit | one fixed clock time |
| Stop / target | on the **combined** position, in rupees or % of the credit |
| Trailing stop | fixed give-back from the peak, optionally armed at a trigger |
| Breakeven stop | move the stop to entry once a profit threshold is reached |
| Per-leg stop | % of that leg's own entry premium, or points — closing the whole position **or only that leg** |
| Re-entry | up to N more times a day, after a stop and/or a target |
| Entry conditions | ATM IV band, overnight gap %, intraday move from the open |
| Day filter | specific weekdays |
| Series | weekly or monthly, rolling — or a real expiry date with `--expiry` |
| Expiry rules | nth expiry out, weekly vs monthly, and a days-to-expiry band |
| Holding | intraday, or across sessions with `--hold` — never across a roll |
| Mid-trade adjustment | rule-based repairs while the position is open — roll a decayed leg to the tested one, stop at the straddle, cap it with wings. **`--expiry` only** |
| Costs | brokerage, slippage, and all statutory charges, date-scoped |

### What it cannot — do not fake these

| Rule | Status |
|---|---|
| **Re-entry at fresh strikes** | Re-entry works, but goes back into the *same contracts*. Re-selecting intraday would need the whole chain at every minute. Say which one the strategy means. |
| **Mid-trade adjustment on `--series`** | Expressible on `--expiry` (see below) and refused on the rolling series, which holds ±10 strikes and names no contract. The `roll` leg flag is a different, cruder thing: it re-strikes *every* minute. |
| **Re-striking by anything but moneyness, on `--series`** | The rolling lake is physically indexed on moneyness. On `--expiry` the whole chain is there and `--adjust` re-selects by premium. |
| **VIX or trend filters** | Not possible — no VIX series in the lake. ATM IV is the closest available proxy and it is not the same thing. |
| **Holding across an expiry** | Refused. A hold is truncated at the roll instead, because the same strike on the far side is a different contract. With `--expiry` the boundary is the contract's own date rather than one recovered from vol. |
| **Anything beyond ±10 strikes on `--series`** | No data there. Use `--expiry`, which reaches the whole chain over a shorter history. |

### How to handle a mismatch

- **Delta or premium targeting** → now expressible directly. Use it, and check
  the report's *How the strikes were chosen* table — it says where the rule
  landed and on how many days the target fell outside the data.
- **A conditional entry filter that is not IV, gap or intraday move** → run the
  unconditional version and label it clearly as an upper or lower bound, not as
  the strategy.
- **A repair or roll rule** → expressible. Use `--adjust`, and read *How the
  position was repaired* in the report: an adjusted strategy pays brokerage on
  every leg it touches, so compare gross against the unadjusted version before
  concluding the rule was wrong.
- **Positional / overnight** → use `--hold`. See *Holding across sessions*.

### Worked examples

> *"Sell the ATM straddle at 9:20, stop loss 30% on the combined premium, exit
> at 3:15, only on Thursdays."*

```bash
../.venv/bin/python -m tools.backtest run \
    --name "ATM straddle, 30% SL, Thursdays" \
    --legs "SELL CE 0, SELL PE 0" \
    --entry 09:20 --exit 15:15 --stop-pct 0.3 --weekdays 3 \
    --notes "From <source>. All rules expressed exactly."
```

> *"Sell the ATM straddle and buy three lots each side whose premium is about a
> third of the ATM, 25% stop on each leg individually, one re-entry."*

```bash
../.venv/bin/python -m tools.backtest run \
    --name "Double ratio backspread" \
    --legs "SELL CE 0, SELL PE 0, BUY CE @atm/3 x3, BUY PE @atm/3 x3" \
    --leg-stop-pct 0.25 --leg-action leg --re-entries 1 \
    --notes "From <source>. Re-entry goes back into the same strikes, which is
             a substitution if the source meant the new ATM."
```

> *"Sell the 20-delta strangle, trail once 50% of premium is captured."*

```bash
../.venv/bin/python -m tools.backtest run \
    --name "20-delta strangle, trail after 50%" \
    --legs "SELL CE 20d, SELL PE 20d" \
    --trail-pct 0.2 --trail-trigger-pct 0.5 \
    --notes "From <source>. All rules expressed exactly. Delta computed from
             vendor IV via Black-76, with time-to-expiry approximated."
```

> *"Sell a delta-neutral strangle on Friday. When the gap between the two
> premiums reaches 40% of what you collected, book the cheap side and re-sell it
> at the expensive side's premium. Never cross the other strike — once they meet
> it's a straddle, so stop and buy wings at the breakevens."*

```bash
../.venv/bin/python -m tools.backtest run \
    --name "DN strangle, 40% gap repair, iron fly" \
    --legs "SELL CE 20d, SELL PE 20d" \
    --expiry front --expiry-kind weekly --weekdays 4 --hold 2 --entry 11:00 \
    --adjust "gap>=40%: roll-cheap-to-expensive" --wings breakeven \
    --notes "From <source>. Rules 3-5 expressed directly. 'Support/resistance'
             substituted with 20-delta, which is the source's own equal-delta
             test. The 40% threshold is measured on the credit at ENTRY."
```

All of it is expressible. The one thing to say out loud is that *"sell near
support and resistance"* is a chart judgement, and 20-delta is a substitution
for it — a defensible one, because the source itself defines the strikes by
equal delta and premium, but a substitution nonetheless.

---

## What the data is

Local Parquet, queried by DuckDB. No server, no subscription, no cloud.

| | Coverage |
|---|---|
| **NIFTY**, rolling (`--series`) | 38.9M bars, 1,240 sessions, 2021-08-16 → 2026-08-14, **±10 strikes** |
| **NIFTY**, full chain (`--expiry`) | 138.8M bars, 918 sessions, 99 expiries, **every strike** |
| **BANKNIFTY** | rolling only, 2021-08 → 2026-08 |
| **SENSEX** | rolling only, in progress |
| Resolution | 1 minute |
| Series | weekly and monthly, kept strictly separate |

**The full chain is dense from 2024 onward.** Measured: 2024 has 249 sessions
across 161 strikes, 2025 has 249 across 168, 2026 has 155 across 178. The 2022
and 2023 rows exist — 31 and 234 sessions — but they are a handful of
long-dated contracts that happened to be listed years before they expired, not
a chain. Do not read a `--expiry` result before 2024 as a backtest.

**Implied vol on the full chain is derived, not quoted.** Upstox serves no IV,
so it is solved from put–call parity — see `data/vols.py` and the RESUME entry.
**92.9% of rows carry one**; the rest are deep in the money or below a
five-paise print, where the price says nothing about vol and a null is the
honest answer. Delta selection works on the 92.9%.

Check the live state any time:

```bash
cd ~/Projects/trading/backend && ../.venv/bin/python -m tools.backfill --resume
```

---

## What the data cannot support

**State these limits when reporting a result. Do not quietly exceed them.**

**±10 strikes on the rolling series, and no further.** Dhan's endpoint
hard-stops there — verified by probing ATM+12 through ATM+40, all empty. ±10 is
only about **±2% of spot**, roughly one standard deviation on a weekly. A
premium or delta target beyond that range is *clamped* to the nearest available
contract, and the report says on how many days that happened — read it, because
to that extent the result is about a different strategy.

**`--expiry` lifts that limit** where the full-chain data reaches. A NIFTY
weekly lists ~110 strikes and a monthly ~150, out to 12000–34500. So a far-OTM
strangle is testable now, on the ~2 years Upstox retains, and not on the five
years behind it. Check which window a result actually covers before comparing
it with a rolling-series run.

**A ~7-point non-synchronicity floor.** Two legs priced from the same minute are
both last-traded prices that printed at different instants. Measured against
put-call parity, holding time-to-expiry constant, the residual is 7.15 points
standard deviation. On a 200-point straddle that is ~3.5% noise per entry. It
averages out over hundreds of trades; it bounds what any single trade means.

**Deep ITM prices are unreliable in proportion to depth.** Share of bars
printing below intrinsic: 0.9% at ATM, 10.2% at 5 strikes ITM, 19.5% at 10.
Not staleness — it is the bid-ask spread on contracts that barely trade.
Treat legs beyond ~5 strikes ITM as indicative.

**Stops are evaluated on 1-minute closes.** A spike through the stop that
recovers inside the minute is missed. This flatters tight stops — and it
flatters per-leg and trailing stops most, because they fire more often.

**Slippage is a flat assumption, not a measurement.** The default 0.5 points per
leg per side is a guess, and it is certainly too kind on illiquid strikes. Say
so when a result depends on it. It matters more with re-entry, which pays the
round trip again.

**Expiry dates are recovered, not stored.** Vendor rolling data carries no
expiry, so the cycle boundaries are solved out of price and implied vol
(`expiries.py`). That is exact enough to guarantee a hold never crosses a roll,
and to identify expiry day. The recovered *number* of days is biased — spot
stands in for the forward, and the vendor quotes vol on its own convention — so
only the boundaries are used, never the magnitude. Delta selection and the
margin model still use the half-cycle approximation for time-to-expiry.

---

## Conventions that will bite you

**`restrike` must stay off unless the strategy genuinely rolls.** Legs are
pinned to the strike they entered. The old behaviour — re-selecting by moneyness
every minute — made a position silently follow the money and *cannot lose to a
directional move*. It reported Sharpe 14.7 and a 92% win rate on a naked short
straddle, and +₹11,459 on 2024-06-04 for a trade that actually lost ₹24,019. The
same straddle reads **+₹36.2 L with re-strike on against −₹36,792 pinned**. Use
it only for strategies that deliberately adjust.

**A days-to-expiry band used to silently cancel `--hold`. Fixed 21 Aug 2026.**
`--dte-min 4 --dte-max 4 --hold 2` returned P&L byte-identical to `--hold 0`,
because the band was filtering the *session list* rather than the entry days, so
the sessions a position needed to be carried through were gone before the
schedule was built. Every position truncated to one day and the report called it
"cut short by a contract roll" — a silent no-op wearing the label of a
deliberate safety rule. The band now constrains entry only. If you are reading
an old run that combined the two, its hold did nothing.

**A held position on `--expiry next` used to be spliced across two contracts.
Fixed 21 Aug 2026.** The expiry was resolved per session rather than per
position, so when the front expired mid-hold the ladder shifted underneath the
trade and later sessions were marked against a contract it had never traded. On
NIFTY weeklies this reported **−₹94,168** for a strategy that actually made
**+₹78,284**. A held position is now pinned to the contract it entered, and
`test_a_held_position_keeps_the_contract_it_entered` asserts it. Run 016 was
re-baselined; every other stored run was unaffected and still matches to the
rupee.

**Charges are date-scoped and material.** STT on options rose twice inside this
window (0.0625% → 0.10% Oct 2024 → 0.15% Apr 2026), and exchange fees changed
alongside. Each trade is charged at the rates in force on its own day. Charges
are frequently larger than the edge: the baseline straddle is **+₹1.17 L gross,
−₹36,792 net**. Always report gross and net.

**Margin is modelled, and it is the denominator for every return figure.** SPAN
scenario loss plus exposure on genuinely uncovered shorts, capped at max loss
when bounded, and **never below the net premium paid**. That last clause matters
for the structures premium-ratio selection makes easy: a 1x3 backspread is long
gamma, so every price shock SPAN tests is a *gain* and the scenario margin is
nearly zero — while the position costs ₹16,000 a day in debit. Sanity check: a
1-lot NIFTY straddle comes out at ~₹1.29 L median, matching what Zerodha charges.

**Return on margin is not return on account.** Nobody trades at 100% margin
utilisation. Defined-risk structures look spectacular on this measure because
their margin is small — real, but not the same as making that percentage on
capital. The report says this; keep saying it.

**A drawdown counts from the account's starting balance.** The running peak
includes the money before the first trade, so a run that opens with its worst
losses is measured from where it started. Taking the peak from the equity curve
alone made the first trade unable to contribute to any drawdown, which is only
ever wrong in the flattering direction.

**Only the regular session trades.** 09:15–15:30. The lake also holds bars Dhan
serves past the close and NSE's evening Muhurat sessions; the engine excludes
both, and skips sessions under 300 minutes.

---

## Where things live

| | |
|---|---|
| `db/lake/` | the market data (Parquet, hive-partitioned) |
| `db/backtests/NNN-slug/` | one run: `spec.json`, `result.json`, `report.md` |
| `db/backtests/index.json` | summary of every run |
| `backend/app/backtest/engine.py` | the vectorised engine |
| `backend/app/backtest/selectors.py` | how a leg chooses its contract |
| `backend/app/backtest/costs.py` | date-scoped charges |
| `backend/app/backtest/report.py` | analytics and the markdown report |
| `backend/app/backtest/sweep.py` | parameter grids and heatmaps |
| `backend/app/backtest/walkforward.py` | out-of-sample validation |
| `backend/app/backtest/montecarlo.py` | resampling the trade sequence |
| `backend/app/backtest/registry.py` | numbered storage for runs |
| `backend/app/backtest/expiries.py` | expiry cycles, recovered from price and vol |
| `backend/app/backtest/library.py` | the named strategy library |
| `backend/app/backtest/portfolio.py` | strategies held together, with netted margin |
| `backend/app/runner.py` | the forward runner — saved strategies on live prices |
| `db/forward/` | one journal per strategy, a record per session |
| `db/strategies/` | one JSON per saved strategy |
| `PLAN.md` | the one active document: state, what is next, and §6 — the bugs that cost real debugging |

---

## How to report a result

The generated `report.md` is the deliverable — do not paraphrase it into
something vaguer. When summarising in conversation:

1. **Lead with net, not gross.** Gross without charges is not a result.
2. **Give the capital.** "Made ₹92,813" is meaningless without "on ₹18,577 of
   margin".
3. **Give the worst stretch, and whether it recovered.** Depth alone understates
   it — a drawdown that never recovered is a different fact from one that took
   three weeks.
4. **State the assumptions that carried it** — slippage, the stop, and whether
   charges flipped the sign.
5. **Repeat every substitution**, including how often a premium or delta target
   was clamped to the nearest available strike.
6. **Do not rank strategies on win rate.** Premium selling wins often and loses
   big by construction. Win rate without reward:risk is noise.

## Handing a strategy to an assistant

Three ways, depending on the tool.

**Claude Code, in this folder** — nothing to type. `CLAUDE.md` loads
automatically and points here. Just describe the strategy:

> *"Backtest this: sell the ATM straddle at 9:20, 30% stop on the combined
> premium, exit 3:15, Thursdays only."*

**Claude Code, explicitly** — `/backtest` then the rules. Same result, but it
loads the full procedure rather than relying on the model to go looking.

**Any other assistant with access to this folder** — paste this once:

```text
You have access to my trading folder. Read BACKTESTING.md in the root, in full,
before doing anything. It explains where my market data is, how to run a
backtest, what the engine cannot express, and how to report the result.

Here are the strategy rules I want tested:

<paste the rules>

Translate them into the backtest command. For any rule the engine cannot express
exactly, tell me plainly rather than approximating it silently, and put the
substitution in --notes so it stays attached to the result. If a rule makes the
strategy untestable here, say so instead of testing something similar.

Then run it, and give me the report along with the run id.
```

The saved run appears on the dashboard under **Backtest history** with no
further action — the page reads the registry directly.

## Before believing any result

- **Sweep the neighbourhood.** A result that only works at one setting is a
  curve fit. `sweep` does this in one command and reports what share of the grid
  worked, which is the number that matters — not the best cell.
- **Then walk it forward.** A sweep still chose its winner with hindsight.
  `walkforward` chooses from earlier data only and trades it blind, and the
  efficiency it reports is the closest thing here to an honest expectation.
- **Read the Monte Carlo panel.** The realised drawdown is one draw. If the
  report says losses clustered, the resampled figures are a floor on the risk
  rather than a bound on it, and capital should be sized off the deeper number.
- **Check it is spread across years and weekdays, not carried by one.** The
  month grid and the breakdown tables answer this.
- **Check what the data under it was doing.** The day view's `gapped` and
  `stale` counts say how much of the price path the engine could actually see.
  A run whose wings did not print on half its sessions is a weaker claim than
  its headline, however good the headline is.
- **Check the correlation.** `correlate` — two strategies near +1 are one bet at
  double size.
- **Count the attempts.** Every run and every sweep cell is a chance for
  something to look good by accident. The ids exist so that count stays visible.

## After changing the engine

```bash
../.venv/bin/python -m tools.backtest verify
```

Re-runs every saved run from its own `spec.json` and checks the net P&L still
matches the stored report **to the rupee**. Exits non-zero if anything drifted.

This is the only thing standing between a refactor and silently changing what
every stored backtest means, and it has already caught two real bugs: a legacy
`moneyness` field that re-ran every stored condor as a straddle, and a drawdown
that ignored a first-trade loss. Run it before *and* after touching the engine,
the report, the selectors or the cost model.

A drift is not a rounding difference to wave through. Either the change was
wrong, or every affected run needs re-baselining deliberately, with a note
saying why.

If the change *added* to the report rather than altering what it computes,
follow a clean `verify` with `rerender` so the runs already on disk carry the
new section too. `verify` proves nothing moved; `rerender` writes it out.
