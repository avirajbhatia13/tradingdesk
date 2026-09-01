"""Vectorised backtest engine over the Parquet lake.

The design goal is to be faster than the hosted engines by not doing the thing
they do. A conventional backtester walks the session minute by minute in Python,
and at each minute searches for the contract matching the rule — an O(minutes x
contracts) scan with interpreter overhead on every step. Five years of NIFTY is
~470,000 minutes, and that shape takes tens of minutes to run.

This engine splits the work by how often it has to happen:

1. **Once per day, in Python** — pull the whole option chain at one minute and
   let each leg's `Selector` pick its contract out of it. A day is ~42 rows and
   five years is ~1,240 days, so this loop is free, and because it is ordinary
   Python it can express *any* selection rule: strikes out of the money, a
   target premium, a fraction of another leg's premium, a delta, a percentage
   of spot. See `selectors.py`.
2. **Once per run, in SQL** — one DuckDB query pivots the chosen contracts into
   a leg-per-column matrix aligned on timestamp.
3. **Once per run, in numpy** — entry, exit, stop, target, trail and per-leg
   stop evaluation as array arithmetic.

Nothing iterates per minute in Python. A five-year, four-leg backtest is two
queries and a handful of array operations.

**What this models and what it does not.** It fills at the bar's close by
default, applies a configurable per-leg slippage in points, and charges
brokerage plus the statutory charges in force on each trade's own date. It does
not model the order book, partial fills, or margin calls. A backtest that says a
naked short strangle made money is not saying you could have held it through the
margin call.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, Callable, Literal

import numpy as np
import pyarrow as pa

from app.backtest import adjust as adj
from app.backtest import costs as costs_mod
from app.backtest import lots
from app.backtest import selectors as sel
from app.data import lake
from app.data import schema as sch

# Only the regular session is tradeable. The lake also holds bars Dhan serves
# past the 15:30 close, and NSE's Diwali Muhurat sessions, which run 18:00-19:15
# — both real data, neither part of a normal trading day. Without this filter a
# "09:20 entry, 15:15 exit" strategy silently entered Muhurat at 18:15 and
# reported it as an ordinary session, and an exit time of 15:35 filled against
# bars printed after the market had closed.
_SESSION = (f"(extract('hour' FROM b.ts) * 60 + extract('minute' FROM b.ts)) "
            f"BETWEEN {sch.SESSION_OPEN_MINUTE} AND {sch.SESSION_CLOSE_MINUTE}")

Side = Literal["BUY", "SELL"]


def time_to_expiry(expiry_flag: str, day: date,
                   expiry: date | None = None) -> float:
    """Years to expiry — exact when the contract is dated, approximated when not.

    Dhan's rolling data carries no expiry date, so for it this stays a
    deliberate approximation: half a weekly or monthly cycle, which is the
    average across a series of trades. It biases individual days but not the
    aggregate. Margin is far more sensitive to spot and vol than to a day or
    two of remaining time; delta is too, except on the last session, which is
    why a delta-selected leg on expiry day should be read as indicative.

    Pass a real `expiry` and the approximation goes away. That is the whole
    point of selecting by expiry date: a 20-delta strike chosen on expiry day
    was being computed against three and a half days of remaining life, which
    is wrong by a factor of fourteen on the one session where vol matters most.

    Shared with the report so a delta used for selection and a margin used for
    sizing can never be computed on different clocks.
    """
    if expiry is not None:
        # Options stop trading at the close on expiry day. Half a session is
        # the same "middle of the remaining life" convention the rolling
        # approximation uses, and it keeps expiry day non-zero so Black-76
        # stays solvable rather than collapsing to intrinsic.
        days = max((expiry - day).days, 0) + 0.5
        return days / 365.0
    return (3.5 if str(expiry_flag).upper() == "WEEK" else 15.0) / 365.0


@dataclass
class LegSpec:
    """One leg of the strategy.

    `select` decides *which contract to enter*. It is not how the leg is
    tracked afterwards — see `restrike`. `moneyness` is kept as shorthand for
    the common case: `LegSpec("CE", "SELL", 3)` still means three strikes out
    of the money, and constructs `ByMoneyness(3)` behind it.
    """
    opt_type: str                     # 'CE' | 'PE'
    side: Side
    moneyness: int = 0                # shorthand for select=ByMoneyness(n)
    lots: int = 1
    # When True the leg tracks whatever contract currently sits at `moneyness`,
    # re-striking as spot moves. That models a strategy which genuinely rolls —
    # adjusting a tested side back to the money is a real thing an option
    # seller does — but it is emphatically not what "sell the ATM straddle"
    # means, and it must never be the default.
    #
    # It was the default, silently, and it invalidated every backtest this
    # engine produced. A rolling ATM short straddle cannot lose to a directional
    # move, because it re-strikes into the move: it reported a 92% win rate and
    # a Sharpe of 14.7 across 2021-2026, and booked +11,459 on 2024-06-04 for a
    # position that actually lost 24,019. Pinned by
    # `test_a_leg_holds_the_strike_it_entered`.
    restrike: bool = False
    select: sel.Selector | None = None

    def __post_init__(self) -> None:
        self.opt_type = self.opt_type.upper()
        self.side = self.side.upper()          # type: ignore[assignment]
        if self.select is None:
            self.select = sel.ByMoneyness(self.moneyness)
        elif isinstance(self.select, sel.ByMoneyness):
            # Keep the shorthand field truthful when both were supplied, so
            # anything still reading `leg.moneyness` cannot disagree with the
            # selector that actually chose the contract.
            self.moneyness = self.select.moneyness
        if self.restrike and not self.select.supports_restrike:
            raise ValueError(
                f"{type(self.select).__name__} cannot re-strike. Re-striking "
                f"follows a leg by moneyness at every minute, which only works "
                f"because the lake is indexed on moneyness; any other rule "
                f"would need the whole chain at every minute.")
        if self.opt_type not in ("CE", "PE"):
            raise ValueError(f"opt_type must be CE or PE, got {self.opt_type!r}")
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {self.side!r}")

    @property
    def sign(self) -> int:
        return -1 if self.side == "SELL" else 1

    @property
    def column(self) -> str:
        """This leg's column in the matrix.

        Two legs with the same side of the chain and the same selection rule
        share a column, which is correct: they resolve to the same contract.
        """
        suffix = "_roll" if self.restrike else ""
        return f"{self.opt_type.lower()}_{self.select.key}{suffix}"

    def describe(self) -> str:
        body = f"{self.side} {self.opt_type} {self.select.describe()}"
        if self.lots != 1:
            body += f" x{self.lots}"
        return body + (" (re-striking)" if self.restrike else "")

    def to_dict(self) -> dict[str, Any]:
        return {"opt_type": self.opt_type, "side": self.side,
                "lots": self.lots, "restrike": self.restrike,
                "moneyness": self.moneyness,
                "select": self.select.to_dict()}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LegSpec":
        moneyness = int(payload.get("moneyness") or 0)
        # Runs saved before selectors existed carry `moneyness` and no
        # `select`. Defaulting to ByMoneyness(0) there would silently re-run
        # every stored condor as a straddle and report it under the old name.
        selector = (sel.from_dict(payload["select"]) if payload.get("select")
                    else sel.ByMoneyness(moneyness))
        return cls(
            opt_type=payload["opt_type"], side=payload.get("side", "SELL"),
            moneyness=moneyness,
            lots=int(payload.get("lots") or 1),
            restrike=bool(payload.get("restrike")),
            select=selector,
        )


@dataclass
class StrategySpec:
    """A complete intraday rule set.

    Every field maps to something checkable rather than to a free-form
    callback: a spec is data, so it can be serialised into `spec.json`, swept
    over, and reproduced exactly. The cost of that is that a genuinely novel
    rule needs a field rather than a lambda — but the benefit is that no saved
    run can ever be un-reproducible, which matters more when the whole point of
    numbering runs is to keep the count of attempts honest.
    """
    name: str
    legs: list[LegSpec]
    entry_time: time = time(9, 20)
    exit_time: time = time(15, 15)

    # --- exits on the combined position, in rupees ---------------------------
    stop_loss: float | None = None
    target: float | None = None
    # As a fraction of the credit received, an alternative to absolute rupees.
    stop_loss_pct: float | None = None
    target_pct: float | None = None

    # --- trailing --------------------------------------------------------
    # Give back at most this much from the best the trade has been. Arms only
    # once profit has reached `trail_trigger` (immediately, if unset).
    trail_stop: float | None = None
    trail_stop_pct: float | None = None
    trail_trigger: float | None = None
    trail_trigger_pct: float | None = None
    # Once profit reaches this, the stop moves to the entry price. A degenerate
    # trail, but specified so often in its own right that expressing it as one
    # would obscure the rule rather than clarify it.
    breakeven_trigger: float | None = None
    breakeven_trigger_pct: float | None = None

    # --- per-leg stops -------------------------------------------------------
    # "25% SL on each leg" is the most common rule in Indian index-option
    # selling and the combined-position stop cannot express it: a short
    # straddle whose call has doubled while the put decayed can sit flat
    # overall while one side runs away.
    per_leg_stop_pct: float | None = None      # of that leg's own entry premium
    per_leg_stop_points: float | None = None
    # 'all' closes the whole position when any leg breaches. 'leg' closes only
    # the breached leg and lets the rest run, which is what the builders on the
    # hosted platforms do by default and what most people mean.
    per_leg_action: str = "all"

    # --- positional ----------------------------------------------------------
    # Sessions to hold for. 0 is intraday — enter and exit the same day, which
    # is what every strategy here did until expiry dates became recoverable.
    #
    # A hold NEVER crosses a contract roll. The lake stores a rolling series, so
    # the same strike a week later is a different contract, and carrying a
    # position across that boundary would splice two of them into one price
    # path and report a P&L for a position nobody could have held. The hold is
    # therefore truncated at expiry, and the report says how often that bit.
    # See `expiries.py` for how the boundaries are recovered.
    #
    # Positions do not overlap: the next entry is looked for after the current
    # one closes. Overlapping cohorts would need a matrix column per cohort per
    # leg, and "enter, hold, exit, go again" is what these strategies actually
    # do.
    hold_days: int = 0

    # --- re-entry ------------------------------------------------------------
    # How many times to go again after being stopped out, within the same day.
    # Re-entry uses the *same contracts*: re-selecting strikes intraday would
    # need the full chain at every minute, which is the scan this engine is
    # built to avoid. Say so when reporting — "re-enter at the new ATM" is a
    # different strategy from "re-enter the same strikes".
    re_entries: int = 0
    re_entry_gap_minutes: int = 1
    re_entry_on: str = "stop"                  # 'stop' | 'target' | 'both'

    # --- conditional entry ---------------------------------------------------
    # Evaluated on the day, before entering. All are inclusive bounds and any
    # left as None is not tested.
    min_atm_iv: float | None = None            # decimals, e.g. 0.12
    max_atm_iv: float | None = None
    gap_pct_min: float | None = None           # open vs previous session close
    gap_pct_max: float | None = None
    day_move_pct_min: float | None = None      # entry spot vs the day's open
    day_move_pct_max: float | None = None

    # Which rolling series to trade. Every query filters on it, so a weekly and
    # a monthly contract can never end up spliced into one price path.
    #
    # Known gap: rows we recorded ourselves carry a real `expiry` date and a
    # null `series`, so they are excluded by this filter. Selecting those needs
    # expiry-date logic that does not exist yet. Excluding them makes a
    # backtest over own-recorded data return no trades, which is loud; including
    # them would silently mix the front two expiries, which is not.
    expiry_flag: str = "WEEK"
    # Select the contract by its real expiry DATE instead of by rolling series.
    #
    # None keeps the rolling behaviour above, which is what every stored run
    # was produced with and what Dhan's data can support at all. An integer
    # switches to dated selection and means "the nth expiry live on this
    # session", 0 being the front one — which is what makes Upstox's full
    # chain and our own recording reachable, since both carry a real expiry
    # and a null series.
    #
    # The two are mutually exclusive by construction: a row has one or the
    # other, never both.
    expiry_index: int | None = None
    expiry_kind: str = "any"                   # 'any' | 'weekly' | 'monthly'
    # Only trade sessions where the chosen contract has this many days left.
    # `max_dte = 0` is "expiry day only"; `min_dte = 1` is "never hold into
    # the last session". Both need a dated expiry to mean anything.
    min_dte: int | None = None
    max_dte: int | None = None
    weekdays: tuple[int, ...] | None = None    # 0=Mon; None = every trading day
    # Skip sessions with fewer than this many minutes. A normal NSE day is 375;
    # special sessions run 60-110. Set to 0 to trade whatever the lake holds.
    min_session_bars: int = 300
    lot_size: int = 75
    # Use the lot size actually in force on each session instead of the fixed
    # one above. Off by default deliberately: turning it on changes the P&L of
    # every run that spans a revision, and every backtest already on disk was
    # produced without it. See backtest/lots.py, which also explains why the
    # sessions it cannot answer for are counted rather than guessed at.
    lot_calendar: bool = False
    slippage_points: float = 0.5               # per leg, per lot, per side
    # Brokerage plus the statutory charges in force on each trade's own date.
    # See costs.py — the rates changed twice inside the lake's date range.
    costs: costs_mod.CostModel = field(default_factory=costs_mod.CostModel)
    # Rules for repairing the position while it is open — rolling a decayed leg
    # up to the tested one, capping a straddle with wings. Setting this moves
    # the run onto a stateful per-minute walk (`adjust.py`) instead of the
    # vectorised path, because the basket is no longer fixed. None keeps the
    # vectorised engine, which is what every stored run used.
    adjust: "adj.AdjustPlan | None" = None

    def __post_init__(self) -> None:
        if self.hold_days and self.re_entries:
            raise ValueError(
                "re-entry is a within-the-day rule and a hold spans days; the "
                "two cannot both apply. Drop one.")
        if self.hold_days and any(leg.restrike for leg in self.legs):
            raise ValueError(
                "a re-striking leg cannot be held overnight — it follows the "
                "money by moneyness, and across a roll that is a different "
                "contract entirely.")
        if self.hold_days < 0:
            raise ValueError("hold_days cannot be negative")

    def describe(self) -> str:
        body = ", ".join(leg.describe() for leg in self.legs)
        if self.hold_days:
            body += (f" · held {self.hold_days} session"
                     f"{'' if self.hold_days == 1 else 's'}")
        return body

    def to_dict(self) -> dict[str, Any]:
        """A spec as JSON — a reproduction recipe, not a description.

        Written by hand rather than by `dataclasses.asdict` because that
        flattens a selector to its fields and drops which rule it was, turning
        `ByDelta(0.2)` and `ByPremium(0.2)` into the same two characters on
        disk. A saved run that cannot be re-run is not a record.
        """
        from dataclasses import fields as _fields

        skip = {"legs", "costs", "entry_time", "exit_time", "weekdays"}
        out: dict[str, Any] = {f.name: getattr(self, f.name)
                               for f in _fields(self) if f.name not in skip}
        out["entry_time"] = self.entry_time.strftime("%H:%M")
        out["exit_time"] = self.exit_time.strftime("%H:%M")
        out["weekdays"] = list(self.weekdays) if self.weekdays else None
        out["legs"] = [leg.to_dict() for leg in self.legs]
        out["costs"] = self.costs.to_dict()
        out["adjust"] = self.adjust.to_dict() if self.adjust else None
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StrategySpec":
        """Rebuild a spec from `spec.json`, so re-running a saved run with one
        setting changed is mechanical rather than an act of memory."""
        from dataclasses import fields as _fields

        known = {f.name for f in _fields(cls)}
        kwargs = {k: v for k, v in payload.items()
                  if k in known and k not in
                  ("legs", "costs", "entry_time", "exit_time", "weekdays",
                   "adjust")}
        kwargs["legs"] = [LegSpec.from_dict(leg) for leg in payload["legs"]]
        kwargs["entry_time"] = _parse_time(payload.get("entry_time"), time(9, 20))
        kwargs["exit_time"] = _parse_time(payload.get("exit_time"), time(15, 15))
        weekdays = payload.get("weekdays")
        kwargs["weekdays"] = tuple(weekdays) if weekdays else None
        kwargs["costs"] = costs_mod.CostModel.from_dict(payload.get("costs"))
        kwargs["adjust"] = adj.AdjustPlan.from_dict(payload.get("adjust"))
        return cls(**kwargs)


def _parse_time(text: Any, fallback: time) -> time:
    if isinstance(text, time):
        return text
    if not text:
        return fallback
    hour, _, minute = str(text).partition(":")
    return time(int(hour), int(minute or 0))


@dataclass
class Trade:
    day: date
    entry_ts: Any
    exit_ts: Any
    entry_price: float                 # net premium of the basket, signed
    exit_price: float
    pnl: float
    gross: float
    costs: float
    exit_reason: str
    max_profit: float
    max_loss: float
    charges: costs_mod.Charges = field(default_factory=costs_mod.Charges)
    # Which contracts were actually traded. Without this a premium- or
    # delta-selected run is unauditable: the rule is in the spec but the strike
    # it landed on that day is nowhere.
    strikes: dict[str, float] = field(default_factory=dict)
    # 0 for the day's first trade, 1 for the first re-entry, and so on.
    attempt: int = 0
    # Sessions between entry and exit. 0 for an intraday trade.
    sessions_held: int = 0
    # True when a contract roll ended the hold before `hold_days` was reached.
    truncated: bool = False

    # How good the data under this particular trade was. A backtest averages
    # over hundreds of days and hides the ones where the price barely moved
    # because nothing traded, not because the market was quiet — and those are
    # exactly the days that produce an inexplicable outlier. Counted over the
    # minutes the position was actually open.
    bars: int = 0
    # Minutes where a leg did not print at all, so the basket had no price.
    missing_bars: int = 0
    # Minutes whose basket price is identical to the minute before. On a liquid
    # ATM straddle this is near zero; a high share means the last traded price
    # was being repeated, and every exit decision on that stretch was made
    # against a stale number.
    flat_bars: int = 0

    # What a repair rule actually did, one entry per adjustment. Recorded per
    # trade rather than summarised, because "it adjusted 41 times" and "it
    # adjusted once on 41 days" are different strategies and the average cannot
    # tell them apart. Empty for every run without `--adjust`.
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    # The strangle collapsed onto one strike, so adjusting stopped.
    became_straddle: bool = False
    # Protective wings were bought, capping the loss from that minute on.
    wings_added: bool = False


@dataclass
class Selection:
    """What the selectors did, aggregated over the run.

    Reported rather than logged because a premium or delta target that fell
    outside the ±10 strikes the lake holds produces a real number from the
    wrong contract, and nothing downstream can tell.
    """
    days: int = 0
    resolved: int = 0
    clamped_days: int = 0
    unresolved_days: int = 0
    # Sessions dropped because the expiry the rule asked for is one the vendor
    # listed but the lake does not yet hold. Counted rather than substituted —
    # see `_expiry_map`.
    missing_expiry_days: int = 0
    per_leg: dict[str, dict[str, Any]] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days, "resolved": self.resolved,
            "clamped_days": self.clamped_days,
            "unresolved_days": self.unresolved_days,
            "missing_expiry_days": self.missing_expiry_days,
            "per_leg": self.per_leg, "note": self.note,
        }


@dataclass
class Result:
    strategy: str
    trades: list[Trade] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    equity: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0
    bars_scanned: int = 0
    selection: Selection = field(default_factory=Selection)
    skipped: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "stats": self.stats,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "bars_scanned": self.bars_scanned,
            "selection": self.selection.to_dict(),
            "skipped": self.skipped,
            "equity": self.equity,
            "trades": [
                {
                    "day": t.day.isoformat(),
                    "entry": str(t.entry_ts), "exit": str(t.exit_ts),
                    "entry_price": round(t.entry_price, 2),
                    "exit_price": round(t.exit_price, 2),
                    "gross": round(t.gross, 2), "costs": round(t.costs, 2),
                    "charges": t.charges.to_dict(),
                    "pnl": round(t.pnl, 2), "exit_reason": t.exit_reason,
                    "mfe": round(t.max_profit, 2), "mae": round(t.max_loss, 2),
                    "strikes": t.strikes, "attempt": t.attempt,
                    "sessions_held": t.sessions_held,
                    "truncated": t.truncated,
                    "bars": t.bars, "missing_bars": t.missing_bars,
                    "flat_bars": t.flat_bars,
                }
                for t in self.trades
            ],
        }


# ---------------------------------------------------------------------------
# stage 1 — the chain snapshot, and letting the selectors choose
# ---------------------------------------------------------------------------

def _anchor_table(spec: StrategySpec) -> pa.Table:
    """The (opt_type, moneyness) rows that must have printed for a day to count.

    This is what decides the *snapshot minute*: the first minute at or after the
    entry time where all of them traded. For a purely moneyness-based strategy
    that is exactly the old behaviour — every leg had to print — so pinning is
    unchanged for the specs that already existed. For a dynamic selector the
    requirement is the ATM row, which stands in for "the market was trading":
    it is the most liquid contract on the board, and requiring the leg's own
    contract is impossible when which contract that is depends on the snapshot.
    """
    pairs: set[tuple[str, int]] = set()
    for leg in spec.legs:
        if leg.restrike:
            continue                       # rolling legs are never pinned
        moneyness = leg.select.anchor_moneyness()
        if moneyness is not None:
            pairs.add((leg.opt_type, int(moneyness)))
    if not pairs:
        pairs = {(leg.opt_type, 0) for leg in spec.legs}
    ordered = sorted(pairs)
    return pa.table({
        "opt_type": pa.array([p[0] for p in ordered], pa.string()),
        "moneyness": pa.array([p[1] for p in ordered], pa.int8()),
        "tag": pa.array([f"{p[0]}{p[1]}" for p in ordered], pa.string()),
    })


def _roll_table(spec: StrategySpec) -> pa.Table:
    """Legs that follow live moneyness rather than holding a strike."""
    seen: dict[str, tuple[str, int]] = {}
    for leg in spec.legs:
        if leg.restrike:
            seen[leg.column] = (leg.opt_type, leg.moneyness)
    return pa.table({
        "leg": pa.array(list(seen), pa.string()),
        "opt_type": pa.array([v[0] for v in seen.values()], pa.string()),
        "moneyness": pa.array([v[1] for v in seen.values()], pa.int8()),
    })


# ---------------------------------------------------------------------------
# choosing the contract's expiry
# ---------------------------------------------------------------------------
#
# Two kinds of row live in this lake and they identify a contract differently.
#
# Dhan's rolling endpoint never names the contract, so its rows carry a null
# `expiry` and a `series` of 'WEEK' or 'MONTH' — "whatever the front weekly is
# on this date". Upstox's expired-contract endpoint and our own recorder both
# carry a real expiry date and a null series, because they name the instrument.
#
# So the expiry axis is a swappable predicate rather than a fixed `series = ?`.
# For rolling rows it stays exactly that. For dated rows it becomes a join
# against a per-session table of which expiry that session should trade, built
# once in `_expiry_map`. Nothing else in the engine changes: the selectors, the
# pinning and the vectorised accounting all work on whatever chain they are
# handed.


def contract_label(spec: StrategySpec) -> str:
    """How to name the contract a run traded, in one place.

    The report, the sweep, the walk-forward and the CLI banner all say this,
    and a run that selected by date is not a "week" run — printing the rolling
    series for it would put the wrong contract in the record.
    """
    if spec.expiry_index is None:
        return str(spec.expiry_flag).upper()
    which = {0: "front", 1: "next"}.get(spec.expiry_index,
                                        f"#{spec.expiry_index}")
    kind = "" if (spec.expiry_kind or "any") == "any" else f" {spec.expiry_kind}"
    band = ""
    if spec.min_dte is not None or spec.max_dte is not None:
        low = spec.min_dte if spec.min_dte is not None else "-"
        high = spec.max_dte if spec.max_dte is not None else "-"
        band = f", dte {low}..{high}"
    return f"{which}{kind} expiry{band}"


def _expiry_clause(spec: StrategySpec, tag: str) -> tuple[str, str]:
    """The JOIN and the WHERE fragment that pin one query to one contract."""
    if spec.expiry_index is None:
        return "", "AND b.series = ?"
    return (f"JOIN expiry_pick {tag} ON {tag}.day = b.ts::DATE "
            f"AND {tag}.expiry = b.expiry"), ""


def _window_args(spec: StrategySpec, underlying: str, start: date,
                 end: date) -> list:
    """Query parameters for one window. Dated selection carries no series."""
    args: list = [underlying, start, end]
    if spec.expiry_index is None:
        args.append(spec.expiry_flag)
    return args


def _expiry_map(spec: StrategySpec, underlying: str, start: date, end: date,
                con) -> tuple[dict[date, date], int, set[date]]:
    """Which expiry each session should trade, when selecting by date.

    Returns the per-session expiry, how many sessions were dropped for a
    missing contract, and the subset of sessions a position may be *opened* on.
    The last is separate because the days-to-expiry band constrains entry only;
    see the comment on it below.

    Built from the bars themselves rather than from a calendar: an expiry is
    live on a session if the lake actually holds bars for it that day and it
    has not already expired. That way a vendor gap shows up as a session with
    no chain — loud — instead of as a session priced off a contract nothing
    traded.

    `expiry_index` is the position in that day's ladder, 0 being the front.
    `expiry_kind` narrows the ladder to weeklies or monthlies first, using the
    vendor's own `weekly` flag off the `contracts` table, which is the only
    authority worth trusting — NSE has moved the expiry weekday twice inside
    this data and "last expiry of the month" picks the wrong one.
    """
    rows = con.execute(
        """SELECT ts::DATE AS day, expiry
           FROM option_bars
           WHERE underlying = ? AND ts::DATE BETWEEN ? AND ?
             AND expiry IS NOT NULL AND expiry >= ts::DATE
           GROUP BY 1, 2 ORDER BY 1, 2""",
        [underlying, start, end]).fetchall()
    if not rows:
        # Three values, like every other exit. Returning two made the caller's
        # unpack raise, so asking for `--expiry` over a range the lake has no
        # dated bars for died on a traceback — in the one place built to
        # explain the emptiness instead (`_why_no_chain`).
        return {}, 0, set()

    have: dict[date, set[date]] = {}
    for day, expiry in rows:
        have.setdefault(_as_date(day), set()).add(_as_date(expiry))

    # The ladder is built from every expiry the VENDOR LISTED, not from the
    # ones the lake happens to hold. That distinction is the whole safety of
    # this function.
    #
    # Ranking only what is on disk means a backfill that has not reached the
    # front weekly yet silently promotes the next one, and "sell the front
    # weekly straddle" quietly becomes a 14-day contract on some sessions and
    # a same-day one on others. Measured while building this: on 2026-06-09
    # the true front expiry was missing and the substitute entered at 576
    # points against the real 114 — a different strategy, priced perfectly,
    # with nothing anywhere saying so.
    #
    # So the rank comes from the listing and the session is DROPPED when the
    # contract it names is not in the lake. A missing session is loud; a
    # substituted contract is not.
    kind = (spec.expiry_kind or "any").lower()
    listed: list[date] = []
    weekly: dict[date, bool] = {}
    try:
        for row in lake.contracts(underlying):
            if row.get("expiry") is None:
                continue
            listed.append(row["expiry"])
            if row.get("weekly") is not None:
                weekly[row["expiry"]] = row["weekly"]
    except Exception:
        listed = []
    universe = sorted(set(listed))
    if not universe:
        # Nothing listed — fall back to what is on disk, which is the best
        # available answer and is flagged by the caller as unverified.
        universe = sorted({e for expiries in have.values() for e in expiries})

    if kind == "weekly":
        universe = [e for e in universe if weekly.get(e) is not False]
    elif kind == "monthly":
        universe = [e for e in universe if weekly.get(e) is False]

    chosen: dict[date, date] = {}
    entry_ok: set[date] = set()
    missing = 0
    for day in sorted(have):
        ladder = [e for e in universe if e >= day]
        if spec.expiry_index >= len(ladder):
            continue
        expiry = ladder[spec.expiry_index]
        dte = (expiry - day).days
        # The days-to-expiry band decides where a position may be OPENED. It is
        # deliberately NOT a filter on which sessions exist, because a held
        # position has to be carried through the sessions between its entry and
        # its exit — and most of those fail the band by construction. Dropping
        # them from the day list stranded `--hold` completely: `_schedule` found
        # no later session to run to, truncated every position to a single day,
        # and reported it as a contract roll. `--hold 0`, `--hold 2` and
        # `--hold 5` all returned byte-identical P&L, which is what a silent
        # no-op looks like. See PLAN.md §6.
        eligible = ((spec.min_dte is None or dte >= spec.min_dte)
                    and (spec.max_dte is None or dte <= spec.max_dte))
        if expiry not in have[day]:
            # Counted only for sessions the run would actually have entered, so
            # the figure keeps meaning "days the rule wanted and could not get".
            if eligible:
                missing += 1
            continue
        chosen[day] = expiry
        if eligible:
            entry_ok.add(day)
    return chosen, missing, entry_ok


def _expiry_table(chosen: dict[date, date]) -> pa.Table:
    return pa.table({
        "day": pa.array(sorted(chosen), pa.date32()),
        "expiry": pa.array([chosen[d] for d in sorted(chosen)], pa.date32()),
    })


# The full chain at one minute per day.
#
# `aligned` finds the minutes where every anchor row printed, `anchor` takes the
# first such minute at or after the entry time, and the outer select reads off
# *everything* the lake holds at that minute — all strikes, both sides, with
# price, implied vol and spot. That is the board a selector gets to choose from.
#
# The outer select aggregates by contract because two sources can hold the same
# contract-minute — Upstox's history and our own recorder overlap by design, so
# the handover has no gap. Left un-aggregated that is the same strike twice on
# one board, which a premium or delta selector would treat as two candidates.
# The two agree to a tick where they overlap (median difference 0.00 across
# 323,000 matched bars), so which one wins does not matter; that there is only
# one does.
def _chain_sql(spec: StrategySpec) -> str:
    join, where = _expiry_clause(spec, "ep1")
    join2, where2 = _expiry_clause(spec, "ep2")
    # The outer select reaches rows by timestamp alone, so it needs the same
    # predicate — without it every live expiry lands on the board at once.
    join2 = join2.replace("b.ts::DATE", "a.day")
    return f"""
WITH aligned AS (
    SELECT b.ts AS ts, count(DISTINCT a.tag) AS legs
    FROM option_bars b
    JOIN anchors a ON b.opt_type = a.opt_type AND b.moneyness = a.moneyness
    {join}
    WHERE b.underlying = ? AND b.ts::DATE BETWEEN ? AND ? {where}
      AND {_SESSION}
      AND (extract('hour' FROM b.ts) * 60
           + extract('minute' FROM b.ts)) >= ?
    GROUP BY b.ts
    HAVING count(DISTINCT a.tag) = ?
),
anchor AS (SELECT ts::DATE AS day, min(ts) AS ts FROM aligned GROUP BY 1)
SELECT a.day AS day, b.opt_type AS opt_type, max(b.moneyness) AS moneyness,
       b.strike AS strike, max(b.close) AS close, max(b.iv) AS iv,
       max(b.spot) AS spot
FROM anchor a
JOIN option_bars b ON b.ts = a.ts
{join2}
WHERE b.underlying = ? {where2}
GROUP BY 1, 2, 4
ORDER BY 1, 2, 4
"""


def _chains(spec: StrategySpec, underlying: str, start: date, end: date,
            con, expiry_by_day: dict[date, date] | None = None
            ) -> dict[date, sel.Chain]:
    """Read the per-day chain snapshots out of DuckDB."""
    anchors = _anchor_table(spec)
    con.register("anchors", anchors)
    entry_minute = spec.entry_time.hour * 60 + spec.entry_time.minute
    window = _window_args(spec, underlying, start, end)
    outer = [underlying] + ([spec.expiry_flag] if spec.expiry_index is None
                            else [])
    rows = con.execute(
        _chain_sql(spec),
        window + [entry_minute, anchors.num_rows] + outer,
    ).to_arrow_table().to_pydict()

    out: dict[date, sel.Chain] = {}
    for i in range(len(rows["day"])):
        day = _as_date(rows["day"][i])
        chain = out.get(day)
        if chain is None:
            spot = rows["spot"][i]
            chain = sel.Chain(
                day=day, spot=float(spot) if spot is not None else 0.0,
                t_years=time_to_expiry(
                    spec.expiry_flag, day,
                    (expiry_by_day or {}).get(day)),
                calls=[], puts=[])
            out[day] = chain
        # A strike far enough out that moneyness could not be derived is not a
        # candidate a rule can name, and `ChainRow` needs an integer. Dropping
        # it is a no-op on rolling data, where every row carries one.
        if rows["moneyness"][i] is None:
            continue
        price, iv = rows["close"][i], rows["iv"][i]
        row = sel.ChainRow(
            opt_type=rows["opt_type"][i],
            moneyness=int(rows["moneyness"][i]),
            strike=float(rows["strike"][i]),
            price=float(price) if price is not None else float("nan"),
            iv=(float(iv) / 100.0) if iv else 0.0,   # vendors quote percent
        )
        (chain.calls if row.opt_type == "CE" else chain.puts).append(row)
    for chain in out.values():
        chain.calls.sort(key=lambda r: r.moneyness)
        chain.puts.sort(key=lambda r: r.moneyness)
    return out


def _why_no_chain(spec: StrategySpec, underlying: str, start: date, end: date,
                  con) -> str:
    """Explain an empty snapshot rather than leaving 'no trades' to be guessed.

    Asking for a level the lake does not hold produces the same silence as an
    unbackfilled date range, and the two have completely different fixes. This
    costs one cheap query and only runs when there is already nothing to do.
    """
    if spec.expiry_index is None:
        clause, args = "AND series = ?", [spec.expiry_flag]
        described = f"{spec.expiry_flag} bars"
    else:
        clause, args = "AND expiry IS NOT NULL", []
        described = (f"dated bars (expiry #{spec.expiry_index}"
                     f"{'' if spec.expiry_kind == 'any' else ', ' + spec.expiry_kind})")
    rows = con.execute(
        f"""SELECT opt_type, min(moneyness), max(moneyness), count(*)
            FROM option_bars
            WHERE underlying = ? AND ts::DATE BETWEEN ? AND ? {clause}
            GROUP BY opt_type""",
        [underlying, start, end] + args).fetchall()
    if not rows:
        return (f"the lake holds no {described} for {underlying} "
                f"between {start} and {end} — check the backfill")
    have = {row[0]: (row[1], row[2]) for row in rows}
    missing = []
    for opt_type, moneyness in sorted(
            {(leg.opt_type, leg.select.anchor_moneyness())
             for leg in spec.legs if not leg.restrike
             and leg.select.anchor_moneyness() is not None}):
        span = have.get(opt_type)
        if span is None or not (span[0] <= moneyness <= span[1]):
            covered = f"{span[0]:+d} to {span[1]:+d}" if span else "nothing"
            missing.append(f"{opt_type} {moneyness:+d} (lake has {covered})")
    if missing:
        return ("no day had every leg printing at the entry minute; these are "
                "outside the data: " + "; ".join(missing))
    return ("no day had every leg printing at or after the entry minute — the "
            "levels exist but never traded together in the same minute")


def _schedule(spec: StrategySpec, days: list[date], underlying: str,
              expiry_by_day: dict[date, date] | None = None,
              entry_ok: set[date] | None = None
              ) -> list[tuple[date, date]]:
    """Which sessions each position spans, as (entry day, exit day).

    Non-overlapping by construction, and never crossing a contract roll: the
    exit is the earlier of `hold_days` later and the last session of the entry
    day's expiry cycle. Truncating is the safe direction — the alternative is
    pricing one contract with another's bars.

    `entry_ok` restricts which sessions may *open* a position while leaving all
    of them available to be held through. Weekday and days-to-expiry filters
    both arrive here rather than upstream, because a filter that removes
    sessions from `days` silently shortens every hold that needed to span one.

    Days rather than indices, because the caller computes this before the chain
    snapshots exist and `resolve_pins` computes it against a day list that may
    be missing a session the lake had no bars for. Two different index bases
    for the same schedule is a bug waiting to be written; dates mean the same
    thing to both.
    """
    def opens(day: date) -> bool:
        if spec.weekdays is not None and day.weekday() not in spec.weekdays:
            return False
        return entry_ok is None or day in entry_ok

    if not spec.hold_days:
        return [(day, day) for day in days if opens(day)]

    # With a dated expiry the roll boundary is not recovered, it is known: the
    # contract this session trades states the day it dies. `expiries.py` exists
    # because rolling data carries no such date and the boundary has to be
    # solved out of price and vol; where the date is stored there is nothing
    # to solve and nothing to be approximately right about.
    if expiry_by_day:
        boundary = {day: expiry_by_day.get(day) for day in days}
    else:
        from app.backtest import expiries

        try:
            calendar = expiries.calendar(underlying, spec.expiry_flag)
        except Exception:        # no calendar is not a reason to fail a run
            calendar = {}
        boundary = {day: (calendar[day].expiry if day in calendar else None)
                    for day in days}

    spans: list[tuple[date, date]] = []
    index = {day: i for i, day in enumerate(days)}
    i = 0
    while i < len(days):
        entry = days[i]
        if not opens(entry):
            i += 1
            continue
        limit = min(i + spec.hold_days, len(days) - 1)
        expiry = boundary.get(entry)
        if expiry is not None:
            last = index.get(expiry)
            if last is None:
                # The expiry day itself may not be a session this run trades.
                # Truncate at the last session before it rather than holding
                # past a contract that no longer exists.
                earlier = [j for j, day in enumerate(days) if day <= expiry]
                last = earlier[-1] if earlier else None
            if last is not None:
                limit = min(limit, last)
        spans.append((days[i], days[max(limit, i)]))
        i = max(limit, i) + 1
    return spans


def resolve_pins(spec: StrategySpec, chains: dict[date, sel.Chain],
                 underlying: str = "",
                 expiry_by_day: dict[date, date] | None = None,
                 spans: list[tuple[date, date]] | None = None,
                 entry_ok: set[date] | None = None
                 ) -> tuple[pa.Table, Selection, dict[date, dict[str, float]],
                            list[tuple[date, date]]]:
    """Ask every leg's selector to choose its contract, day by day.

    This is the loop the old engine could not have: it runs once per day rather
    than once per minute, so it can be ordinary Python and still cost nothing.
    A day where any leg cannot resolve is dropped entirely rather than traded
    short a leg — a hedged position silently missing its hedge is the worst
    failure this code could produce.
    """
    pinned = [leg for leg in spec.legs if not leg.restrike]
    columns = list(dict.fromkeys(leg.column for leg in pinned))
    selection = Selection(days=len(chains))
    per_leg: dict[str, dict[str, Any]] = {
        leg.column: {"rule": leg.select.describe(), "side": leg.opt_type,
                     "resolved": 0, "clamped": 0,
                     "moneyness": [], "strikes": []}
        for leg in pinned
    }

    ordered = sorted(chains)
    if spans is None:
        spans = _schedule(spec, ordered, underlying, expiry_by_day, entry_ok)
    # Which session each held position was opened on, so a day inside a hold
    # is pinned to the strikes the cohort entered rather than to its own.
    # Built by scanning the day list between the span's endpoints rather than
    # by index arithmetic, because the caller's schedule was computed against
    # every session the expiry map knew about and `ordered` is only the ones
    # that produced a chain snapshot.
    holder = {}
    for entry_day, exit_day in spans:
        if entry_day not in chains:
            continue                       # nothing to select from; not traded
        for day in ordered:
            if entry_day <= day <= exit_day:
                holder[day] = entry_day

    days, legs, types, strikes = [], [], [], []
    per_day: dict[date, dict[str, float]] = {}
    schedule: list[tuple[date, date]] = []
    for day in ordered:
        if day not in holder:
            continue                       # not covered by any position
        chain = chains[holder[day]]
        picks: dict[str, sel.Pick] = {}
        for leg in pinned:
            if leg.column in picks:
                continue
            pick = leg.select.resolve(chain, leg.opt_type)
            if pick is None:
                picks = {}
                break
            picks[leg.column] = pick
        if len(picks) != len(columns):
            selection.unresolved_days += 1
            continue

        if holder[day] == day:
            selection.resolved += 1
            if any(p.clamped for p in picks.values()):
                selection.clamped_days += 1
        chosen: dict[str, float] = {}
        for column, pick in picks.items():
            stat = per_leg[column]
            if holder[day] == day:
                stat["resolved"] += 1
            stat["clamped"] += int(pick.clamped)
            stat["moneyness"].append(pick.row.moneyness)
            stat["strikes"].append(pick.row.strike)
            chosen[column] = pick.row.strike
            days.append(day)          # the session, pinned to the cohort's strike
            legs.append(column)
            types.append(pick.row.opt_type)
            strikes.append(pick.row.strike)
        per_day[day] = chosen

    for stat in per_leg.values():
        landed = stat.pop("moneyness")
        stat.pop("strikes")
        if landed:
            # Where a dynamic rule actually landed is the number that makes it
            # auditable — "the 20-delta call was between 4 and 9 strikes out,
            # usually 6" is the sentence a reader needs.
            stat["moneyness_min"] = int(min(landed))
            stat["moneyness_max"] = int(max(landed))
            stat["moneyness_median"] = int(sorted(landed)[len(landed) // 2])
    selection.per_leg = per_leg

    # Only the spans whose entry day actually resolved can be traded.
    for entry_day, exit_day in spans:
        if entry_day in per_day:
            schedule.append((entry_day, exit_day))

    table = pa.table({
        "day": pa.array(days, pa.date32()),
        "leg": pa.array(legs, pa.string()),
        "opt_type": pa.array(types, pa.string()),
        "strike": pa.array(strikes, pa.float64()),
    })
    return table, selection, per_day, schedule


# ---------------------------------------------------------------------------
# stage 2 — the leg-per-column matrix
# ---------------------------------------------------------------------------

def _matrix_sql(spec: StrategySpec, with_roll: bool) -> str:
    """Pivot the lake into one column per leg, aligned on time.

    Two sources feed the pivot. Pinned legs are joined on the *strike* the
    selector chose, so the series follows one contract all session. Re-striking
    legs are joined on live moneyness, which is the old behaviour, now only ever
    reached when a leg asks for it explicitly.

    Conditional aggregation rather than a join per leg: a four-leg strategy is
    one pass over the filtered rows, executed vectorised.
    """
    selects = []
    for column in dict.fromkeys(leg.column for leg in spec.legs):
        selects.append(f"max(CASE WHEN leg = '{column}' THEN close END) "
                       f"AS {column}")
        selects.append(f"max(CASE WHEN leg = '{column}' THEN strike END) "
                       f"AS {column}_strike")
        # Carried so the report can size margin off the vendor's own implied
        # vol at entry rather than re-solving it per leg per trade.
        selects.append(f"max(CASE WHEN leg = '{column}' THEN iv END) "
                       f"AS {column}_iv")
    join_a, where_a = _expiry_clause(spec, "epa")
    join_b, where_b = _expiry_clause(spec, "epb")
    rolling = f"""
        UNION ALL
        SELECT b.ts, b.spot, b.close, b.strike, b.iv, d.leg
        FROM option_bars b
        JOIN roll_defs d ON b.opt_type = d.opt_type
                        AND b.moneyness = d.moneyness
        {join_b}
        WHERE b.underlying = ? AND b.ts::DATE BETWEEN ? AND ? {where_b}
          AND {_SESSION}
    """ if with_roll else ""
    return f"""
    WITH tracked AS (
        SELECT b.ts AS ts, b.spot AS spot, b.close AS close,
               b.strike AS strike, b.iv AS iv, p.leg AS leg
        FROM option_bars b
        JOIN pins p ON b.ts::DATE = p.day AND b.opt_type = p.opt_type
                   AND b.strike = p.strike
        {join_a}
        WHERE b.underlying = ? AND b.ts::DATE BETWEEN ? AND ? {where_a}
      AND {_SESSION}
        {rolling}
    )
    SELECT ts, ts::DATE AS day, max(spot) AS spot, {', '.join(selects)}
    FROM tracked
    GROUP BY ts
    ORDER BY ts
    """


@dataclass
class Matrix:
    """Everything a run needs that came out of the database."""
    columns: dict[str, np.ndarray]
    selection: Selection
    strikes: dict[date, dict[str, float]]
    atm_iv: dict[date, float]
    # (entry session, exit session) per position. For an intraday spec both are
    # the same day and there is one per session.
    schedule: list[tuple[date, date]] = field(default_factory=list)
    # Which expiry each session traded, when selecting by date. Empty for a
    # rolling series, which has no date to carry. The report reads it so the
    # margin model and the selectors compute time-to-expiry on one clock.
    expiry_by_day: dict[date, date] = field(default_factory=dict)


def _matrix_key(spec: StrategySpec, underlying: str, start: date,
                end: date) -> tuple:
    """What the loaded matrix actually depends on.

    Not the whole spec: stop, target, trail, lot size, costs and slippage are
    applied to an already-loaded matrix, so varying them must hit the cache.
    Side and lots are excluded for the same reason — they weight the basket,
    they do not change which bars are fetched. `entry_time` *is* included,
    because the snapshot each leg is selected from is taken at the entry minute.

    The lake directory is part of the key because tests point `LAKE_DIR` at a
    temporary path per case; without it, the second test to run a straddle over
    the same dates would be served the first test's bars and pass for the wrong
    reason.
    """
    legs = tuple(sorted({(leg.opt_type, leg.select.key, leg.restrike)
                         for leg in spec.legs}))
    return (legs, underlying, start, end, spec.expiry_flag,
            spec.expiry_index, spec.expiry_kind, spec.min_dte, spec.max_dte,
            spec.entry_time,
            # A hold changes which session each bar is pinned to, so two specs
            # differing only in hold length are not the same matrix. Weekdays
            # count for every spec, not just held ones: the schedule now decides
            # which sessions are pinned at all, so a weekday filter changes the
            # bars that come back rather than only which of them are traded.
            spec.hold_days, spec.weekdays,
            str(sch.LAKE_DIR))


# Loading the matrix is ~90% of a backtest's wall time (measured: 1,126 ms of
# 1,257 ms for a four-leg condor over five years). A parameter sweep varies
# stop, target and the like while the underlying bars stay identical, so
# without this every combination pays the full load again — 16 stop/target
# combinations measured 27.6 s uncached against 2.6 s cached, a 10.7x
# difference on exactly the workload sweeps exist to run.
#
# Bounded because one five-year four-leg matrix is ~40 MB; a handful is fine,
# an unbounded dict keyed by date range is not.
MATRIX_CACHE_SIZE = 6
_matrix_cache: "OrderedDict[tuple, Matrix]" = OrderedDict()


def clear_matrix_cache() -> None:
    """Drop cached matrices. Call after ingesting new bars — the cache is keyed
    by date range, and a range that now holds more data would otherwise keep
    answering from the older load."""
    _matrix_cache.clear()


def load_context(spec: StrategySpec, underlying: str, start: date,
                 end: date) -> Matrix:
    """Chain snapshots, selector picks and the aligned leg matrix.

    Three stages rather than one query: snapshot the chain per day, let the
    selectors choose, then follow the chosen contracts. Only the middle stage
    is Python, and it runs once per day.

    Results are cached; callers must treat the arrays as read-only. Every
    consumer in `run()` either reads or copies (`.astype` allocates), so this
    holds today, and a caller that mutates would corrupt every later run
    sharing the key.
    """
    key = _matrix_key(spec, underlying, start, end)
    hit = _matrix_cache.get(key)
    if hit is not None:
        _matrix_cache.move_to_end(key)
        return hit

    window = _window_args(spec, underlying, start, end)
    rolls = _roll_table(spec)

    def once() -> tuple[pa.Table, Selection, dict, dict]:
        con = lake.connect()
        try:
            chosen: dict[date, date] = {}
            missing = 0
            entry_ok: set[date] | None = None
            spans: list[tuple[date, date]] | None = None
            if spec.expiry_index is not None:
                chosen, missing, entry_ok = _expiry_map(
                    spec, underlying, start, end, con)
                # The schedule is settled BEFORE any bars are read, because it
                # decides which contract each session is read *as*.
                spans = _schedule(spec, sorted(chosen), underlying,
                                  chosen, entry_ok)
                # A held session is priced on the contract its position ENTERED,
                # not on whatever the rule would name that morning. Mapping each
                # session to its own front/next contract spliced two contracts
                # into one price path: with `--expiry next`, the ladder shifts
                # every Wednesday, so a Friday entry held into the next week was
                # marked against a contract it had never traded — silently, and
                # with a perfectly plausible P&L. See PLAN.md §6.
                priced_as = dict(chosen)
                for entry_day, exit_day in spans:
                    for day in sorted(chosen):
                        if entry_day <= day <= exit_day:
                            priced_as[day] = chosen[entry_day]
                # Registered before any query runs: every stage of the load
                # joins against it, so one table decides which contract the
                # whole run is about.
                con.register("expiry_pick", _expiry_table(priced_as))
            chains = _chains(spec, underlying, start, end, con, chosen)
            pins, selection, strikes, schedule = resolve_pins(
                spec, chains, underlying, chosen, spans, entry_ok)
            if not chains:
                selection.note = _why_no_chain(spec, underlying, start, end, con)
            # Set last, and deliberately: when every session was dropped for a
            # missing contract, `_why_no_chain` reports that the levels never
            # printed together — true of the chain it can see, and the wrong
            # diagnosis. The reason there is no chain is that the run refused
            # to trade the substitute.
            selection.missing_expiry_days = missing
            if missing:
                selection.note = (
                    f"{missing} session(s) skipped: the expiry the rule names "
                    f"is listed by the vendor but not in the lake, so the run "
                    f"would otherwise have traded a later contract. Finish the "
                    f"backfill and re-run." + (
                        f" Otherwise: {selection.note}" if selection.note else ""))
            atm = {}
            for day, chain in chains.items():
                atm_row = chain.at("CE", 0) or chain.at("PE", 0)
                atm[day] = atm_row.iv if atm_row else 0.0
            if not pins.num_rows and not rolls.num_rows:
                empty = pa.table({"ts": pa.array([], pa.timestamp("us"))})
                return empty, selection, strikes, atm, schedule, chosen
            con.register("pins", pins)
            con.register("roll_defs", rolls)
            arguments = window + (window if rolls.num_rows else [])
            table = con.execute(_matrix_sql(spec, bool(rolls.num_rows)),
                                arguments).to_arrow_table()
            return table, selection, strikes, atm, schedule, chosen
        finally:
            con.close()

    table, selection, strikes, atm, schedule, expiries_used = lake.read(once)
    columns = {name: table.column(name).to_numpy(zero_copy_only=False)
               for name in table.column_names}

    matrix = Matrix(columns=columns, selection=selection, strikes=strikes,
                    atm_iv=atm, schedule=schedule, expiry_by_day=expiries_used)
    _matrix_cache[key] = matrix
    while len(_matrix_cache) > MATRIX_CACHE_SIZE:
        _matrix_cache.popitem(last=False)
    return matrix


def load_matrix(spec: StrategySpec, underlying: str, start: date,
                end: date) -> dict[str, np.ndarray]:
    """The aligned leg matrix alone, for callers that only price legs."""
    return load_context(spec, underlying, start, end).columns


# ---------------------------------------------------------------------------
# stage 3 — the vectorised accounting
# ---------------------------------------------------------------------------

def _combined_price(columns: dict[str, np.ndarray],
                    spec: StrategySpec) -> np.ndarray:
    """Signed net premium of the basket at every minute.

    Positive means the basket costs money (a debit); negative means credit
    received — same convention as the strategy builder, so a number moved
    between the two means the same thing.
    """
    total = np.zeros(len(next(iter(columns.values()))), dtype=np.float64)
    for leg in spec.legs:
        prices = columns[leg.column].astype(np.float64)
        total += leg.sign * prices * leg.lots
    return total


def _round_trip_fills(leg_prices: dict[str, np.ndarray], spec: StrategySpec,
                      entry_i: int, exits: dict[str, int] | int,
                      lot_size: int | None = None
                      ) -> tuple[list[costs_mod.Fill], list[costs_mod.Fill]]:
    """The eight-or-so individual executions behind one basket round trip.

    Slippage is applied per leg in the direction that hurts — a sell fills below
    the printed price, a buy above it. Summed back up this is exactly the
    `slip_combined` adjustment made to the basket price, because the basket is a
    lots-weighted sum of the legs and slippage is signed the same way; the
    equivalence is pinned by a test. Doing it twice is not double-counting:
    the basket figure drives P&L, these fills drive charges, and charges need
    the per-side split that the basket has already collapsed.

    `exits` is per leg, because a per-leg stop can close one side early and its
    charges fall on the price it actually closed at. A single index is accepted
    for the ordinary case where the whole basket closes at once.

    Prices are floored at zero. Slippage on an option trading at 0.05 could
    otherwise produce a negative traded value and a negative STT.
    """
    slip = spec.slippage_points
    size = spec.lot_size if lot_size is None else lot_size
    if isinstance(exits, int):
        exits = {leg.column: exits for leg in spec.legs}
    entry: list[costs_mod.Fill] = []
    exit_: list[costs_mod.Fill] = []
    for leg in spec.legs:
        quantity = leg.lots * size
        opened = float(leg_prices[leg.column][entry_i])
        closed = float(leg_prices[leg.column][exits[leg.column]])
        if leg.side == "SELL":
            entry.append(costs_mod.Fill("SELL", max(opened - slip, 0.0), quantity))
            exit_.append(costs_mod.Fill("BUY", max(closed + slip, 0.0), quantity))
        else:
            entry.append(costs_mod.Fill("BUY", max(opened + slip, 0.0), quantity))
            exit_.append(costs_mod.Fill("SELL", max(closed - slip, 0.0), quantity))
    return entry, exit_


def _levels(spec: StrategySpec, credit: float) -> dict[str, float | None]:
    """Every rupee level the exit logic needs, from whichever form was given.

    `credit` is the absolute value of the basket at entry times the lot size —
    the money at stake, which is what a percentage rule is always a percentage
    of.
    """
    def resolve(absolute: float | None, fraction: float | None) -> float | None:
        if absolute:
            return abs(absolute)
        if fraction:
            return abs(fraction) * credit
        return None

    trail = resolve(spec.trail_stop, spec.trail_stop_pct)
    return {
        "stop": -resolve(spec.stop_loss, spec.stop_loss_pct)
                if resolve(spec.stop_loss, spec.stop_loss_pct) else None,
        "target": resolve(spec.target, spec.target_pct),
        "trail": trail,
        # A trail with no trigger arms from the first minute, which is what
        # "trail by 2,000" means when nothing else is said.
        "trail_trigger": resolve(spec.trail_trigger, spec.trail_trigger_pct)
                         if trail else None,
        "breakeven": resolve(spec.breakeven_trigger,
                             spec.breakeven_trigger_pct),
    }


def _first_exit(pnl_path: np.ndarray,
                levels: dict[str, float | None]) -> tuple[int | None, str]:
    """Earliest index where any exit rule triggers.

    Rules are evaluated against the same bar and the loss-making ones win ties.
    Within one minute we cannot know which came first, and assuming the target
    did is how a backtest flatters itself.

    The trail and the breakeven stop are dynamic levels: they depend on the best
    the trade has been *up to and including* the bar being tested, which is what
    `fmax.accumulate` gives — a running maximum that steps over the NaN minutes
    where a leg did not print rather than poisoning everything after them.
    """
    hits: list[tuple[int, str]] = []

    def first(mask: np.ndarray, reason: str) -> None:
        idx = np.flatnonzero(mask)
        if idx.size:
            hits.append((int(idx[0]), reason))

    if levels.get("stop") is not None:
        first(pnl_path <= levels["stop"], "stop")
    if levels.get("target") is not None:
        first(pnl_path >= levels["target"], "target")

    if levels.get("trail") or levels.get("breakeven") is not None:
        peak = np.fmax.accumulate(pnl_path)
        if levels.get("trail"):
            armed = (peak >= levels["trail_trigger"]
                     if levels.get("trail_trigger") else np.ones_like(peak, bool))
            first(armed & (pnl_path <= peak - levels["trail"]), "trail")
        if levels.get("breakeven") is not None:
            first((peak >= levels["breakeven"]) & (pnl_path <= 0.0), "breakeven")

    if not hits:
        return None, "time"
    # Stop first, then the other protective exits, then target: on a tie the
    # pessimistic reading is the honest one.
    order = {"stop": 0, "leg stop": 1, "trail": 2, "breakeven": 3, "target": 4}
    hits.sort(key=lambda h: (h[0], order.get(h[1], 9)))
    return hits[0]


def _leg_breaches(spec: StrategySpec, leg_paths: dict[str, np.ndarray],
                  entry_i: int, length: int) -> dict[str, int | None]:
    """First bar at which each leg breaches its own stop, relative to entry.

    A short leg breaches when its premium *rises* — the position is short, so a
    more expensive contract is the loss. A long leg breaches when it falls.
    """
    if not (spec.per_leg_stop_pct or spec.per_leg_stop_points):
        return {}
    out: dict[str, int | None] = {}
    for leg in spec.legs:
        path = leg_paths[leg.column][:length]
        opened = float(path[0]) if path.size else float("nan")
        if not np.isfinite(opened) or opened <= 0:
            out[leg.column] = None
            continue
        move = (opened * spec.per_leg_stop_pct if spec.per_leg_stop_pct
                else spec.per_leg_stop_points)
        if leg.side == "SELL":
            breach = np.flatnonzero(path >= opened + move)
        else:
            breach = np.flatnonzero(path <= opened - move)
        out[leg.column] = int(breach[0]) if breach.size else None
    return out


def run(spec: StrategySpec, underlying: str = "NIFTY",
        start: date | None = None, end: date | None = None) -> Result:
    """Backtest `spec` over the lake."""
    import time as _time

    began = _time.perf_counter()
    start = start or date(2020, 1, 1)
    end = end or date.today()

    context = load_context(spec, underlying, start, end)
    columns = context.columns
    result = Result(strategy=spec.name, selection=context.selection)
    if not columns or not len(columns.get("ts", [])):
        result.stats = _summarise([], spec)
        result.elapsed_ms = (_time.perf_counter() - began) * 1000
        return result

    stamps = columns["ts"]
    result.bars_scanned = len(stamps)

    # A minute is only usable if every leg printed in it.
    usable = np.ones(len(stamps), dtype=bool)
    for leg in spec.legs:
        values = columns[leg.column].astype(np.float64)
        usable &= np.isfinite(values) & (values > 0)

    price = _combined_price(columns, spec)
    days = columns["day"]
    spots = columns["spot"].astype(np.float64)
    minutes = _minute_of_day(stamps)
    entry_minute = spec.entry_time.hour * 60 + spec.entry_time.minute
    exit_minute = spec.exit_time.hour * 60 + spec.exit_time.minute

    multiplier = spec.lot_size

    def lot_on(day: date) -> int:
        """The multiplier for one session, and a tally of what was guessed.

        With the calendar off this is the spec's constant, unchanged — which is
        what keeps every stored run reproducing to the rupee.
        """
        if not spec.lot_calendar:
            return spec.lot_size
        size, known = lots.size_on(underlying, day, spec.lot_size)
        if not known:
            skipped["lot_size_assumed"] += 1
        return size

    # Slippage enters the combined price weighted by lots, because the combined
    # price is itself lots-weighted (`_combined_price` multiplies by leg.lots).
    # Charging it per leg instead understated the cost of every strategy sized
    # above one lot per leg — the sizes that actually matter.
    slip_combined = spec.slippage_points * sum(leg.lots for leg in spec.legs)
    leg_prices = {leg.column: columns[leg.column].astype(np.float64)
                  for leg in spec.legs}
    freeze_legs = (spec.per_leg_action == "leg"
                   and bool(spec.per_leg_stop_pct or spec.per_leg_stop_points))

    trades: list[Trade] = []
    skipped = {"weekday": 0, "short_session": 0, "no_entry": 0, "filtered": 0,
               "truncated_at_expiry": 0, "lot_size_assumed": 0}

    if spec.adjust is not None:
        # The basket changes while the position is open, so the vectorised path
        # cannot express it: its whole speed comes from every leg being fixed.
        result.trades = _run_adjusted(
            spec, context, underlying, start, end, lot_on, skipped)
        result.skipped = skipped
        result.stats = _summarise(result.trades, spec)
        result.equity = _equity_curve(result.trades)
        result.elapsed_ms = (_time.perf_counter() - began) * 1000
        return result

    if spec.hold_days:
        result.trades = _run_positional(
            spec, context, columns, stamps, days, minutes, price, usable,
            leg_prices, slip_combined, lot_on, entry_minute, exit_minute,
            skipped)
        result.skipped = skipped
        result.stats = _summarise(result.trades, spec)
        result.equity = _equity_curve(result.trades)
        result.elapsed_ms = (_time.perf_counter() - began) * 1000
        return result

    previous_close: float | None = None
    for day, lo, hi in _day_slices(days):
        day_spots = spots[lo:hi]
        finite_spots = day_spots[np.isfinite(day_spots)]
        session_close = float(finite_spots[-1]) if finite_spots.size else None
        session_open = float(finite_spots[0]) if finite_spots.size else None
        prior, previous_close = previous_close, session_close or previous_close

        if spec.weekdays is not None and day.weekday() not in spec.weekdays:
            skipped["weekday"] += 1
            continue
        # Abnormally short sessions are not ordinary trading days — NSE's
        # special Saturday sessions and the truncated ones around them run a
        # few hours on thin liquidity. Counting them as full days adds trades
        # whose statistics mean something different from every other row.
        # A bar count is used rather than a holiday calendar deliberately: a
        # hard-coded list goes stale and then hides real gaps instead.
        if hi - lo < spec.min_session_bars:
            skipped["short_session"] += 1
            continue
        window = slice(lo, hi)
        day_usable = usable[window]
        if not day_usable.any():
            skipped["no_entry"] += 1
            continue

        day_minutes = minutes[window]
        day_price = price[window]
        day_stamps = stamps[window]
        day_legs = {column: values[window] for column, values in leg_prices.items()}

        first_idx = _first_at_or_after(day_minutes, entry_minute, day_usable)
        if first_idx is None:
            skipped["no_entry"] += 1
            continue
        if not _passes_filters(spec, context, day, session_open, prior,
                               float(day_spots[first_idx])):
            skipped["filtered"] += 1
            continue

        # Resolved once per session rather than per attempt: re-entries are the
        # same day and would otherwise be counted again in the assumed tally.
        day_lot = lot_on(day)

        close_idx = _first_at_or_after(day_minutes, exit_minute, day_usable)
        if close_idx is None or close_idx <= first_idx:
            close_idx = _last_usable(day_usable)
            if close_idx is None or close_idx <= first_idx:
                skipped["no_entry"] += 1
                continue

        # Re-entry: the day is a sequence of attempts, not a single trade. Each
        # attempt re-enters the *same* contracts — re-selecting strikes would
        # need the whole chain at the re-entry minute, which is the per-minute
        # scan this engine avoids. That is a real limitation and it belongs in
        # the notes of any run that uses it.
        cursor: int | None = first_idx
        attempt = 0
        while cursor is not None and cursor < close_idx:
            trade, final = _attempt(
                spec, day, cursor, close_idx, day_price, day_legs, day_usable,
                day_stamps, lo, leg_prices, slip_combined, day_lot,
                freeze_legs, context.strikes.get(day, {}), attempt)
            if trade is None:
                break
            trades.append(trade)
            wanted = {"stop": {"stop", "leg stop", "trail", "breakeven"},
                      "target": {"target"},
                      "both": {"stop", "leg stop", "trail", "breakeven",
                               "target"}}.get(spec.re_entry_on, set())
            if attempt >= spec.re_entries or trade.exit_reason not in wanted:
                break
            attempt += 1
            nxt = final + max(spec.re_entry_gap_minutes, 1)
            cursor = (_first_at_or_after(day_minutes, int(day_minutes[nxt]),
                                         day_usable)
                      if nxt < len(day_minutes) else None)

    result.trades = trades
    result.skipped = skipped
    result.stats = _summarise(trades, spec)
    result.equity = _equity_curve(trades)
    result.elapsed_ms = (_time.perf_counter() - began) * 1000
    return result


def _run_positional(spec: StrategySpec, context: Matrix,
                    columns: dict[str, np.ndarray], stamps: np.ndarray,
                    days: np.ndarray, minutes: np.ndarray, price: np.ndarray,
                    usable: np.ndarray, leg_prices: dict[str, np.ndarray],
                    slip_combined: float, lot_on: Callable[[date], int],
                    entry_minute: int, exit_minute: int,
                    skipped: dict[str, int]) -> list[Trade]:
    """Positions held across sessions.

    The only thing that changes is the *window*. Instead of slicing one day, the
    span runs from the entry bar on the entry session to the exit bar on the
    exit session — and everything downstream is the intraday code unchanged:
    the same stop, target, trail and per-leg logic over a longer path.

    Overnight is simply an absence of bars. A gap is realised at the first bar
    of the next session, which is exactly right — a stop cannot fire at 2 a.m.,
    and modelling one that could would flatter every held position.
    """
    bounds = {day: (lo, hi) for day, lo, hi in _day_slices(days)}
    trades: list[Trade] = []

    for entry_day, exit_day in context.schedule:
        if entry_day not in bounds or exit_day not in bounds:
            continue
        lo, hi = bounds[entry_day]
        exit_lo, exit_hi = bounds[exit_day]
        if hi - lo < spec.min_session_bars:
            skipped["short_session"] += 1
            continue

        entry_offset = _first_at_or_after(
            minutes[lo:hi], entry_minute, usable[lo:hi])
        if entry_offset is None:
            skipped["no_entry"] += 1
            continue
        entry_index = lo + entry_offset

        close_offset = _first_at_or_after(
            minutes[exit_lo:exit_hi], exit_minute, usable[exit_lo:exit_hi])
        if close_offset is None:
            close_offset = _last_usable(usable[exit_lo:exit_hi])
        if close_offset is None:
            skipped["no_entry"] += 1
            continue
        close_index = exit_lo + close_offset
        if close_index <= entry_index:
            skipped["no_entry"] += 1
            continue

        # The whole span, as one window. `_attempt` works on window-relative
        # indices with an absolute offset, so it needs no positional variant.
        window = slice(entry_index, close_index + 1)
        trade, _ = _attempt(
            spec, entry_day, 0, close_index - entry_index,
            price[window], {c: v[window] for c, v in leg_prices.items()},
            usable[window], stamps[window], entry_index, leg_prices,
            # The entry session's size. A hold never crosses an expiry roll, so
            # it cannot straddle a revision either — the two boundaries move
            # together.
            slip_combined, lot_on(entry_day),
            spec.per_leg_action == "leg"
            and bool(spec.per_leg_stop_pct or spec.per_leg_stop_points),
            context.strikes.get(entry_day, {}), 0)
        if trade is None:
            skipped["no_entry"] += 1
            continue
        # How long it was actually held, and whether a roll cut it short.
        held = list(bounds).index(exit_day) - list(bounds).index(entry_day)
        trade.sessions_held = held
        if held < spec.hold_days:
            skipped["truncated_at_expiry"] += 1
            trade.truncated = True
        trades.append(trade)
    return trades


def _attempt(spec: StrategySpec, day: date, entry_idx: int, close_idx: int,
             day_price: np.ndarray, day_legs: dict[str, np.ndarray],
             day_usable: np.ndarray, day_stamps: np.ndarray, lo: int,
             leg_prices: dict[str, np.ndarray], slip_combined: float,
             multiplier: float, freeze_legs: bool,
             strikes: dict[str, float], attempt: int
             ) -> tuple[Trade | None, int]:
    """One entry and its exit. Returns the trade and where in the day it ended."""
    if not day_usable[entry_idx]:
        return None, entry_idx
    held = slice(entry_idx, close_idx + 1)
    valid = day_usable[held]
    length = close_idx + 1 - entry_idx
    if length < 2:
        return None, entry_idx

    windowed = {column: values[held] for column, values in day_legs.items()}
    breaches = _leg_breaches(spec, windowed, entry_idx, length)

    # Per-leg stops shape the path two different ways. 'all' closes everything
    # at the first breach. 'leg' closes only the breached side and freezes its
    # contribution at the price it exited, so the survivors keep running — which
    # is what the hosted builders do and what people mean by "SL on each leg".
    leg_exit = {leg.column: length - 1 for leg in spec.legs}
    forced: int | None = None
    if breaches:
        firing = [b for b in breaches.values() if b is not None and b > 0]
        if spec.per_leg_action == "leg":
            for column, breach in breaches.items():
                if breach is not None and breach > 0:
                    leg_exit[column] = breach
        elif firing:
            forced = min(firing)

    if freeze_legs:
        path = np.zeros(length, dtype=np.float64)
        steps = np.arange(length)
        for leg in spec.legs:
            series = windowed[leg.column]
            cut = leg_exit[leg.column]
            frozen = np.where(steps <= cut, series, series[cut])
            path += leg.sign * frozen * leg.lots
    else:
        path = day_price[held].astype(np.float64)

    entry_price = float(path[0])
    # Slippage always hurts: pay up on entry, give up on exit.
    entry_fill = entry_price + slip_combined

    # P&L is the change in the basket's signed net premium, not its negation.
    # Work it through for a short straddle: you enter at a net premium of -92
    # (a credit of 92 received) and the basket decays to -79. Closing costs 79
    # against the 92 collected, so you made +13 — which is exit minus entry.
    # Writing it the other way round is the single most dangerous bug this file
    # could carry, because it reports every short-premium strategy as a loser
    # and looks superficially plausible while doing it.
    pnl_path = (path - entry_fill) * multiplier
    pnl_path[~valid] = np.nan

    levels = _levels(spec, abs(entry_fill) * multiplier)
    hit_idx, reason = _first_exit(pnl_path, levels)
    if forced is not None and (hit_idx is None or forced <= hit_idx):
        hit_idx, reason = forced, "leg stop"
    if hit_idx is None:
        final, reason = length - 1, "time"
    else:
        final = hit_idx
    if freeze_legs and all(cut < final for cut in leg_exit.values()):
        # Every leg was stopped out before the scheduled close; the position was
        # flat from the last one, so that is where the trade actually ended.
        final, reason = max(leg_exit.values()), "leg stop"

    # Slippage hurts at both ends: a higher net premium is worse to enter,
    # a lower one is worse to exit, whichever direction the basket is.
    exit_fill = float(path[final]) - slip_combined
    gross = (exit_fill - entry_fill) * multiplier

    # Charges are computed from the individual legs, not the basket: STT falls
    # on the sell side and stamp duty on the buy side, so a basket that nets to
    # a credit still pays stamp duty on whatever it bought. `day` rather than
    # today, because the rates changed twice inside the lake's range.
    absolute = {column: lo + entry_idx + min(cut, final)
                for column, cut in leg_exit.items()}
    entry_fills, exit_fills = _round_trip_fills(
        leg_prices, spec, lo + entry_idx, absolute, int(multiplier))
    charges = spec.costs.round_trip(entry_fills, exit_fills, day)
    pnl = gross - charges.total

    # MAE and MFE are measured over the period the position was *open*, not to
    # the scheduled exit. Measuring the whole window made them describe a trade
    # that was never held: a stop that fired at 09:22 for -7,354 still reported
    # an MAE of -26,077 from a drawdown hours after the position was closed,
    # which is exactly backwards for the question these numbers exist to
    # answer — was the stop too tight.
    held_pnl = pnl_path[:final + 1]
    finite = held_pnl[np.isfinite(held_pnl)]

    # Data quality over the minutes actually held. `flat` counts consecutive
    # pairs whose basket price is unchanged, which is what a repeated last
    # traded price looks like from here — the position was being marked, and
    # stopped, against a number nobody was quoting.
    held_valid = valid[:final + 1]
    held_path = path[:final + 1]
    both_priced = held_valid[1:] & held_valid[:-1]
    flat_bars = int(np.count_nonzero(
        both_priced & (np.diff(held_path) == 0.0))) if held_path.size > 1 else 0

    trade = Trade(
        day=day,
        entry_ts=day_stamps[entry_idx],
        exit_ts=day_stamps[entry_idx + final],
        entry_price=float(entry_fill), exit_price=float(exit_fill),
        gross=float(gross), costs=float(charges.total), pnl=float(pnl),
        charges=charges,
        exit_reason=reason,
        max_profit=float(finite.max()) if finite.size else 0.0,
        max_loss=float(finite.min()) if finite.size else 0.0,
        strikes={k: float(v) for k, v in strikes.items()},
        attempt=attempt,
        bars=int(held_valid.size),
        missing_bars=int(np.count_nonzero(~held_valid)),
        flat_bars=flat_bars,
    )
    return trade, entry_idx + final


def _passes_filters(spec: StrategySpec, context: Matrix, day: date,
                    session_open: float | None, previous_close: float | None,
                    entry_spot: float) -> bool:
    """Conditional entry, evaluated on the day before committing.

    Each filter is a bound on something observable at the entry minute, so a
    filtered run is still a run of the same strategy — not a lookahead. A rule
    that needs tomorrow's data cannot be written here, which is the point.
    """
    if spec.min_atm_iv is not None or spec.max_atm_iv is not None:
        iv = context.atm_iv.get(day)
        if not iv:
            return False
        if spec.min_atm_iv is not None and iv < spec.min_atm_iv:
            return False
        if spec.max_atm_iv is not None and iv > spec.max_atm_iv:
            return False

    if spec.gap_pct_min is not None or spec.gap_pct_max is not None:
        if not previous_close or not session_open:
            return False
        gap = (session_open - previous_close) / previous_close * 100.0
        if spec.gap_pct_min is not None and gap < spec.gap_pct_min:
            return False
        if spec.gap_pct_max is not None and gap > spec.gap_pct_max:
            return False

    if spec.day_move_pct_min is not None or spec.day_move_pct_max is not None:
        if not session_open or not np.isfinite(entry_spot):
            return False
        move = (entry_spot - session_open) / session_open * 100.0
        if spec.day_move_pct_min is not None and move < spec.day_move_pct_min:
            return False
        if spec.day_move_pct_max is not None and move > spec.day_move_pct_max:
            return False
    return True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def underwater(equity: np.ndarray) -> np.ndarray:
    """How far below its running peak the equity curve sat, at every trade.

    The running peak counts the account's *starting* balance as a peak, which
    is what the extra zero is doing. Without it the first trade can never
    contribute to a drawdown — a run that opens with twenty straight losses
    reported the depth of nineteen of them, because the peak was taken as the
    equity after the first loss rather than before it. That is only ever
    wrong in the flattering direction, and it is exactly the stretch a new
    strategy is most likely to be abandoned in.
    """
    peaks = np.maximum.accumulate(np.concatenate(([0.0], equity)))[1:]
    return equity - peaks


def _minute_of_day(stamps: np.ndarray) -> np.ndarray:
    as_dt = stamps.astype("datetime64[m]")
    midnight = stamps.astype("datetime64[D]").astype("datetime64[m]")
    return (as_dt - midnight).astype(np.int64)


def _day_slices(days: np.ndarray):
    """Contiguous [start, end) index ranges per day. The array is time-sorted,
    so boundaries are found once rather than by grouping."""
    if not len(days):
        return
    edges = np.flatnonzero(days[1:] != days[:-1]) + 1
    bounds = np.concatenate(([0], edges, [len(days)]))
    for i in range(len(bounds) - 1):
        lo, hi = int(bounds[i]), int(bounds[i + 1])
        # DuckDB hands dates back as numpy datetime64; callers want date.
        yield _as_date(days[lo]), lo, hi


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return np.datetime64(value, "D").astype(object)


def _first_at_or_after(minutes: np.ndarray, target: int,
                       usable: np.ndarray) -> int | None:
    candidates = np.flatnonzero((minutes >= target) & usable)
    return int(candidates[0]) if candidates.size else None


def _last_usable(usable: np.ndarray) -> int | None:
    candidates = np.flatnonzero(usable)
    return int(candidates[-1]) if candidates.size else None


EXIT_REASONS = ("time", "stop", "target", "trail", "breakeven", "leg stop")


def _summarise(trades: list[Trade], spec: StrategySpec) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "note": "no trades — is the lake backfilled for this range?"}

    pnls = np.array([t.pnl for t in trades], dtype=np.float64)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    drawdown = underwater(np.cumsum(pnls))

    gross_win = float(wins.sum()) if wins.size else 0.0
    gross_loss = float(-losses.sum()) if losses.size else 0.0

    return {
        "trades": len(trades),
        "total_pnl": round(float(pnls.sum()), 2),
        "average": round(float(pnls.mean()), 2),
        "median": round(float(np.median(pnls)), 2),
        "wins": int(wins.size), "losses": int(losses.size),
        "win_rate": round(float(wins.size) / len(trades) * 100, 1),
        "average_win": round(float(wins.mean()), 2) if wins.size else 0.0,
        "average_loss": round(float(losses.mean()), 2) if losses.size else 0.0,
        "largest_win": round(float(pnls.max()), 2),
        "largest_loss": round(float(pnls.min()), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy": round(float(pnls.mean()), 2),
        "max_drawdown": round(float(drawdown.min()), 2),
        # Daily Sharpe annualised on 252 sessions. One trade a day makes the
        # per-trade series the daily series.
        "sharpe": round(float(pnls.mean() / pnls.std() * np.sqrt(252)), 2)
                  if pnls.std() > 0 else None,
        "exit_reasons": {
            reason: sum(1 for t in trades if t.exit_reason == reason)
            for reason in EXIT_REASONS
        },
        "re_entries": sum(1 for t in trades if t.attempt),
        "sessions": len({t.day for t in trades}),
        "total_costs": round(sum(t.costs for t in trades), 2),
        "cost_per_trade": round(sum(t.costs for t in trades) / len(trades), 2),
        # Itemised, because when live costs diverge from these you need to know
        # which line moved rather than just that the total did.
        "costs_breakdown": sum(
            (t.charges for t in trades), costs_mod.Charges()).to_dict(),
        # What the strategy made before charges — the hurdle the edge clears.
        # A gross that is healthy and a net that is not is a sizing or
        # trade-frequency problem, not a signal problem, and the two have
        # completely different fixes.
        "gross_pnl": round(sum(t.gross for t in trades), 2),
    }


def _equity_curve(trades: list[Trade]) -> list[dict[str, Any]]:
    running = 0.0
    out = []
    for trade in trades:
        running += trade.pnl
        out.append({"day": trade.day.isoformat(), "pnl": round(trade.pnl, 2),
                    "equity": round(running, 2)})
    return out


def load_adjust_cubes(spec: StrategySpec, underlying: str, start: date,
                      end: date, expiry_by_day: dict[date, date],
                      schedule: list[tuple[date, date]]
                      ) -> dict[date, adj.Cube]:
    """The per-minute chain for an adjustment run.

    Read separately from the leg matrix and deliberately so: the matrix follows
    the contracts a strategy *entered*, and a repair rule needs the ones it
    might move to. Restricted to the sessions the schedule actually covers,
    because the whole chain over a whole date range is a great deal of memory
    to hold for days that are never traded.
    """
    if not schedule:
        return {}
    wanted = {day for entry, exit_ in schedule
              for day in expiry_by_day if entry <= day <= exit_}
    if not wanted:
        return {}
    priced_as = {day: expiry_by_day[day] for day in wanted}
    # A held span is pinned to the contract it entered, matching the matrix.
    for entry, exit_ in schedule:
        for day in wanted:
            if entry <= day <= exit_:
                priced_as[day] = expiry_by_day[entry]

    def once() -> dict[date, adj.Cube]:
        con = lake.connect()
        try:
            con.register("expiry_pick", _expiry_table(priced_as))
            return adj.load_cubes(underlying, min(wanted), max(wanted),
                                  priced_as, con)
        finally:
            con.close()

    return lake.read(once)


def _run_adjusted(spec: StrategySpec, context: Matrix, underlying: str,
                  start: date, end: date, lot_on, skipped: dict
                  ) -> list[Trade]:
    """Positions that repair themselves, walked minute by minute.

    The selectors still choose the opening basket, so an adjustment run enters
    exactly where the equivalent fixed run would; everything after entry is the
    stateful walk in `adjust.py`.
    """
    if spec.expiry_index is None:
        raise ValueError(
            "adjustment rules need --expiry, not --series. Re-selecting a "
            "strike mid-trade needs the whole chain at that minute, and the "
            "rolling series holds only +/-10 strikes with no contract identity.")

    entry_minute = spec.entry_time.hour * 60 + spec.entry_time.minute
    exit_minute = spec.exit_time.hour * 60 + spec.exit_time.minute
    trades: list[Trade] = []

    # The chain cube is loaded in BATCHES of spans rather than all at once.
    # Every strike of every session a long hold touches is gigabytes — a
    # 17-session monthly hold over 20 cycles measured 2.5 GB — and nothing is
    # needed after its span has been walked. Batched rather than one query per
    # span because a weekly strategy has ~100 spans and that many round trips
    # costs more than the memory saves.
    sessions = sorted(context.expiry_by_day)
    batches: list[list[tuple[date, date]]] = []
    current: list[tuple[date, date]] = []
    held = 0
    for span in context.schedule:
        width = sum(1 for day in sessions if span[0] <= day <= span[1])
        if current and held + width > _CUBE_BATCH_SESSIONS:
            batches.append(current)
            current, held = [], 0
        current.append(span)
        held += width
    if current:
        batches.append(current)

    for batch in batches:
        cubes = load_adjust_cubes(spec, underlying, batch[0][0], batch[-1][1],
                                  context.expiry_by_day, batch)
        trades.extend(_walk_batch(spec, context, batch, cubes, lot_on,
                                  entry_minute, exit_minute, skipped))
    return trades


# How many sessions of full chain to hold in memory at once. 60 sessions of
# BANKNIFTY at +/-30 moneyness is roughly 450 MB.
_CUBE_BATCH_SESSIONS = 60


def _walk_batch(spec: StrategySpec, context: Matrix,
                batch: list[tuple[date, date]], cubes: dict[date, adj.Cube],
                lot_on, entry_minute: int, exit_minute: int,
                skipped: dict) -> list[Trade]:
    """Walk one batch of positions against an already-loaded cube."""
    trades: list[Trade] = []
    for entry_day, exit_day in batch:
        strikes = context.strikes.get(entry_day)
        if not strikes:
            skipped["no_entry"] += 1
            continue
        days = [day for day in sorted(cubes) if entry_day <= day <= exit_day]
        cube = adj.merge(cubes, days)
        if cube is None:
            skipped["no_entry"] += 1
            continue

        multiplier = lot_on(entry_day)
        opening = [(leg.opt_type, leg.side, strikes[leg.column], leg.lots)
                   for leg in spec.legs if leg.column in strikes]
        if len(opening) != len(spec.legs):
            skipped["no_entry"] += 1
            continue

        span = len(cubes[entry_day].stamps) if entry_day in cubes else 0
        # A minute is only usable if every opening leg printed in it, which is
        # the same rule the vectorised path applies. Without it the entry lands
        # on the clock rather than on a minute the position could be filled in.
        usable = np.ones(span, dtype=bool)
        for opt_type, _side, strike, _lots in opening:
            series = cube.price.get((opt_type, float(strike)))
            if series is None:
                usable[:] = False
                break
            head = series[:span]
            usable &= np.isfinite(head) & (head > 0)
        entry_slot = _first_at_or_after(cube.minutes[:span], entry_minute,
                                        usable)
        if entry_slot is None:
            skipped["no_entry"] += 1
            continue
        # The close is the last minute of the FINAL session at or before the
        # exit time, which for a held position is days after the entry.
        tail_start = len(cube.minutes) - (
            len(cubes[exit_day].stamps) if exit_day in cubes else 0)
        tail = cube.minutes[tail_start:]
        eligible = np.flatnonzero(tail <= exit_minute)
        if not eligible.size:
            skipped["no_entry"] += 1
            continue
        close_slot = int(tail_start + eligible[-1])
        if close_slot <= entry_slot:
            skipped["no_entry"] += 1
            continue

        outcome = adj.simulate(
            cube, entry_slot, close_slot, opening, spec.adjust,
            slippage=spec.slippage_points, lot_size=multiplier,
            target_points=(spec.target / multiplier
                           if spec.target is not None else None),
            stop_points=(spec.stop_loss / multiplier
                         if spec.stop_loss is not None else None),
            target_pct=spec.target_pct, stop_pct=spec.stop_loss_pct)
        if outcome is None:
            skipped["no_entry"] += 1
            continue

        charges = spec.costs.charge(outcome.fills, entry_day)
        gross = outcome.pnl_points * multiplier
        trades.append(Trade(
            day=entry_day,
            entry_ts=cube.stamps[outcome.entry_slot],
            exit_ts=cube.stamps[outcome.exit_slot],
            entry_price=float(-sum(
                leg.entry_price * leg.lots for leg in outcome.legs
                if leg.entry_slot == outcome.entry_slot and leg.side == "SELL")),
            exit_price=0.0,
            gross=float(gross), costs=float(charges.total),
            pnl=float(gross - charges.total), charges=charges,
            exit_reason=outcome.exit_reason,
            max_profit=float(outcome.peak_points * multiplier),
            max_loss=float(outcome.trough_points * multiplier),
            strikes={f"{leg.opt_type}{i}": leg.strike
                     for i, leg in enumerate(outcome.legs)},
            attempt=0,
            bars=int(outcome.exit_slot - outcome.entry_slot + 1),
            missing_bars=int(outcome.missing_slots),
            flat_bars=0,
            adjustments=[{"minute": a.minute, "rule": a.rule,
                          "closed": a.closed, "opened": a.opened,
                          "reason": a.reason} for a in outcome.adjustments],
            became_straddle=outcome.became_straddle,
            wings_added=outcome.wings_added,
        ))
    return trades
