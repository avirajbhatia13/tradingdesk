"""Rule-based mid-trade adjustment — closing and re-selling legs while a
position is open.

Why this exists
---------------

The vectorised engine prices a *fixed* basket: every leg is pinned at entry and
the only decisions left are when to close all of it. That covers most premium
selling and none of the strategies people actually describe, which almost always
carry a repair rule — "when the call side doubles, roll the put up to match it",
"keep adjusting until it becomes a straddle, then cap it with wings".

Those were previously refused rather than approximated, and the refusal was
correct at the time: re-selecting a strike mid-session needs the whole option
chain at *every* minute, and the rolling vendor series holds ±10 strikes with no
contract identity. But `--expiry` reaches the full chain — every strike, one
named contract — so the data objection no longer holds. What was left was a
tooling gap, and this module closes it.

What it costs
-------------

This path is a stateful Python walk over minutes, not a vectorised sweep, so it
is perhaps 50x slower per session than `engine.run`. That is affordable because
an adjustment strategy is a handful of entries a week rather than one per
session, and because correctness here is worth more than speed: the whole point
is that the position *changes*, and a change is a sequence, not an array.

The accounting, and the sign that must not be got wrong
-------------------------------------------------------

Every leg is tracked in points, per unit, on its own entry price:

    SELL leg  P&L = entry - current        (collected entry, owe current)
    BUY  leg  P&L = current - entry

which is the same convention `engine._attempt` uses on the combined basket —
short premium decaying is a profit. Writing it the other way round reports every
premium seller as a loser and looks plausible while doing it; `engine.py` carries
the same warning for the same reason.

Realised P&L accumulates as legs are closed; unrealised is marked on whatever is
open. The two are summed at every minute so a stop or target sees the same
number a trader would.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

import numpy as np

from app.backtest import costs as costs_mod
from app.data import lake
from app.data import schema as sch


# How far from the money the cube reaches. Wide enough for any repair these
# rules can produce and bounded enough that a two-year run stays in memory:
# 60 levels x 2 sides x 375 minutes x ~100 sessions is ~36 MB of float64.
CUBE_MONEYNESS = 30


# ---------------------------------------------------------------------------
# the rule grammar
# ---------------------------------------------------------------------------

# Written the way the rule is spoken, like the leg grammar it sits beside:
#
#   "gap>=40%: roll-cheap-to-expensive"    the 40% premium-gap repair
#   "gap>=60pt: close-cheap"               book the decayed side outright
#   "loss>=50%: roll-cheap-to-expensive"   trigger on the combined loss instead
#
# A trigger, a threshold, and what to do about it. The threshold is a fraction
# of the entry credit when written with `%` and absolute points when written
# with `pt`, because both are things people say and silently picking one is how
# a rule gets tested at 40x its intended size.
_RULE = re.compile(
    r"^\s*(?P<trigger>gap|loss)\s*>=\s*(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|pt)\s*:\s*(?P<action>[a-z\-]+)\s*$", re.I)

_ACTIONS = {"roll-cheap-to-expensive", "close-cheap"}


@dataclass(frozen=True)
class AdjustRule:
    """One repair rule: when to act, and what to do."""

    trigger: str          # 'gap' | 'loss'
    threshold: float      # fraction of entry credit, or points
    unit: str             # '%' | 'pt'
    action: str

    @classmethod
    def parse(cls, text: str) -> "AdjustRule":
        match = _RULE.match(text or "")
        if not match:
            raise ValueError(
                f"cannot read adjustment rule {text!r}. Expected something like "
                f"'gap>=40%: roll-cheap-to-expensive' — a trigger (gap or loss), "
                f"a threshold in % of the entry credit or in points, and an "
                f"action ({', '.join(sorted(_ACTIONS))}).")
        action = match["action"].lower()
        if action not in _ACTIONS:
            raise ValueError(
                f"unknown adjustment action {action!r}; known actions are "
                f"{', '.join(sorted(_ACTIONS))}")
        value = float(match["value"])
        return cls(trigger=match["trigger"].lower(),
                   threshold=value / 100.0 if match["unit"] == "%" else value,
                   unit=match["unit"], action=action)

    def describe(self) -> str:
        size = (f"{self.threshold * 100:g}% of the entry credit"
                if self.unit == "%" else f"{self.threshold:g} points")
        what = {"gap": "the premium gap between the two sides",
                "loss": "the position's loss"}[self.trigger]
        how = {"roll-cheap-to-expensive":
               "close the decayed side and re-sell it at the premium the "
               "tested side is now trading",
               "close-cheap": "close the decayed side and leave it off"}
        return f"when {what} reaches {size}, {how[self.action]}"


@dataclass
class AdjustPlan:
    """Every rule governing how a position is repaired while it is open."""

    rules: tuple[AdjustRule, ...] = ()
    # Cap on repairs per position. Unlimited is a real setting — the source
    # strategies keep adjusting until the strikes meet — but a cap is how you
    # ask "was the edge in the first roll or the fifth?".
    max_adjustments: int | None = None
    # The no-crossover rule: never roll a leg past the strike of the leg it is
    # being rolled towards. Once they meet, the strangle has become a straddle
    # and there is nothing left to roll into.
    no_crossover: bool = True
    # Buy protective wings once the position has collapsed to a straddle.
    # 'breakeven' places them at the straddle's own breakevens; an integer
    # places them that many strikes out.
    wings: str | int | None = None

    def describe(self) -> list[str]:
        out = [rule.describe() for rule in self.rules]
        if self.max_adjustments is not None:
            out.append(f"at most {self.max_adjustments} adjustments")
        if self.no_crossover:
            out.append("never rolling a leg past the other leg's strike; once "
                       "they meet the position is a straddle and adjusting stops")
        if self.wings == "breakeven":
            out.append("buying wings at the straddle's breakevens once it "
                       "becomes one, which caps the loss")
        elif isinstance(self.wings, int):
            out.append(f"buying wings {self.wings} strikes out once the "
                       f"position becomes a straddle")
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "AdjustPlan | None":
        if not payload:
            return None
        return cls(
            rules=tuple(AdjustRule(**r) if isinstance(r, dict)
                        else AdjustRule.parse(r)
                        for r in payload.get("rules", ())),
            max_adjustments=payload.get("max_adjustments"),
            no_crossover=payload.get("no_crossover", True),
            wings=payload.get("wings"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": [{"trigger": r.trigger, "threshold": r.threshold,
                       "unit": r.unit, "action": r.action} for r in self.rules],
            "max_adjustments": self.max_adjustments,
            "no_crossover": self.no_crossover,
            "wings": self.wings,
        }


# ---------------------------------------------------------------------------
# the chain, every minute
# ---------------------------------------------------------------------------

@dataclass
class Cube:
    """Every strike of one contract, at every minute of one session.

    `price[(opt_type, strike)]` is a float array indexed by minute slot, with
    NaN where the contract did not print. A missing print is never filled in:
    an adjustment that could not actually have been executed must not be, and
    the walk skips those minutes rather than inventing a fill.
    """

    day: date
    minutes: np.ndarray                       # minute-of-day per slot
    stamps: np.ndarray
    spot: np.ndarray
    price: dict[tuple[str, float], np.ndarray]
    strikes: list[float] = field(default_factory=list)
    step: float = 50.0

    def at(self, opt_type: str, strike: float, slot: int) -> float:
        series = self.price.get((opt_type, float(strike)))
        if series is None:
            return float("nan")
        return float(series[slot])

    def nearest_by_premium(self, opt_type: str, target: float, slot: int,
                           bound: float | None = None,
                           side: str = "") -> float | None:
        """The strike whose premium sits closest to `target` at this minute.

        `bound` enforces the no-crossover rule: a call may only be re-sold at a
        strike at or above it, a put at or below. Returning None means the rule
        had nowhere legal to go, which is a decision the caller has to make
        rather than a value that can be fudged.
        """
        best, best_gap = None, float("inf")
        for strike in self.strikes:
            if bound is not None:
                if opt_type == "CE" and strike < bound:
                    continue
                if opt_type == "PE" and strike > bound:
                    continue
            value = self.at(opt_type, strike, slot)
            if not np.isfinite(value) or value <= 0:
                continue
            gap = abs(value - target)
            if gap < best_gap:
                best, best_gap = strike, gap
        return best

    def nearest_strike(self, target: float) -> float | None:
        if not self.strikes:
            return None
        return min(self.strikes, key=lambda s: abs(s - target))


_CUBE_SQL = """
SELECT b.ts AS ts, b.strike AS strike, b.opt_type AS opt_type,
       max(b.close) AS close, max(b.spot) AS spot
FROM option_bars b
JOIN expiry_pick ep ON ep.day = b.ts::DATE AND ep.expiry = b.expiry
WHERE b.underlying = ? AND b.ts::DATE BETWEEN ? AND ?
  AND b.moneyness IS NOT NULL AND abs(b.moneyness) <= {band}
  AND (extract('hour' FROM b.ts) * 60 + extract('minute' FROM b.ts))
      BETWEEN {open} AND {close}
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""


def load_cubes(underlying: str, start: date, end: date,
               expiry_by_day: dict[date, date], con) -> dict[date, Cube]:
    """One `Cube` per session, read in a single pass.

    `expiry_pick` must already be registered by the caller — the same table the
    rest of the engine joins against, so an adjustment run is looking at exactly
    the contract the rule named and not at whatever else printed that minute.
    """
    sql = _CUBE_SQL.format(band=CUBE_MONEYNESS,
                           open=sch.SESSION_OPEN_MINUTE,
                           close=sch.SESSION_CLOSE_MINUTE)
    table = con.execute(sql, [underlying, start, end]).to_arrow_table()
    if not table.num_rows:
        return {}

    stamps = table.column("ts").to_numpy(zero_copy_only=False)
    strikes = table.column("strike").to_numpy(zero_copy_only=False)
    types = table.column("opt_type").to_pylist()
    closes = table.column("close").to_numpy(zero_copy_only=False)
    spots = table.column("spot").to_numpy(zero_copy_only=False)

    days = stamps.astype("datetime64[D]")
    out: dict[date, Cube] = {}
    for day_value in np.unique(days):
        mask = days == day_value
        day = day_value.astype(object)
        slot_stamps = np.unique(stamps[mask])
        slot_of = {stamp: i for i, stamp in enumerate(slot_stamps)}
        minutes = ((slot_stamps.astype("datetime64[m]")
                    - slot_stamps.astype("datetime64[D]"))
                   / np.timedelta64(1, "m")).astype(int)

        cube = Cube(day=day, minutes=minutes, stamps=slot_stamps,
                    spot=np.full(len(slot_stamps), np.nan), price={})
        for stamp, strike, opt_type, close, spot in zip(
                stamps[mask], strikes[mask], np.asarray(types)[mask],
                closes[mask], spots[mask]):
            slot = slot_of[stamp]
            key = (str(opt_type), float(strike))
            series = cube.price.get(key)
            if series is None:
                series = np.full(len(slot_stamps), np.nan)
                cube.price[key] = series
            series[slot] = close if close is not None else np.nan
            if spot is not None:
                cube.spot[slot] = spot

        cube.strikes = sorted({strike for _, strike in cube.price})
        if len(cube.strikes) > 1:
            gaps = np.diff(cube.strikes)
            cube.step = float(np.median(gaps)) if gaps.size else 50.0
        out[day] = cube
    return out


def merge(cubes: dict[date, Cube], days: list[date]) -> Cube | None:
    """Splice consecutive sessions into one continuous walk.

    A held position spans sessions, and its repair rules do not reset at the
    close — a gap that opens overnight is exactly the case the rules exist for.
    Safe to concatenate only because the caller has already pinned the whole
    span to one contract; see the expiry-per-position note in `engine.py`.
    """
    present = [cubes[day] for day in days if day in cubes]
    if not present:
        return None
    if len(present) == 1:
        return present[0]

    strikes = sorted({s for cube in present for _, s in cube.price})
    total = sum(len(cube.stamps) for cube in present)
    merged = Cube(day=present[0].day,
                  minutes=np.concatenate([c.minutes for c in present]),
                  stamps=np.concatenate([c.stamps for c in present]),
                  spot=np.concatenate([c.spot for c in present]),
                  price={}, strikes=strikes, step=present[0].step)
    for opt_type in ("CE", "PE"):
        for strike in strikes:
            key = (opt_type, strike)
            parts = []
            for cube in present:
                series = cube.price.get(key)
                parts.append(series if series is not None
                             else np.full(len(cube.stamps), np.nan))
            merged.price[key] = np.concatenate(parts)
    assert len(merged.stamps) == total
    return merged


# ---------------------------------------------------------------------------
# the open position
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """One leg that is currently open, or was."""

    opt_type: str
    strike: float
    side: str            # 'SELL' | 'BUY'
    lots: int
    entry_price: float   # per unit, after slippage
    entry_slot: int
    exit_price: float | None = None
    exit_slot: int | None = None

    @property
    def open(self) -> bool:
        return self.exit_price is None

    def mark(self, current: float) -> float:
        """P&L in points per unit at `current`. See the module docstring."""
        price = self.exit_price if self.exit_price is not None else current
        if self.side == "SELL":
            return (self.entry_price - price) * self.lots
        return (price - self.entry_price) * self.lots


@dataclass
class Adjustment:
    """One repair, recorded so the report can say what the rule actually did."""

    slot: int
    minute: int
    rule: str
    closed: str
    opened: str
    reason: str = ""


@dataclass
class Outcome:
    """What one position did, from entry to final close."""

    day: date
    entry_slot: int
    exit_slot: int
    legs: list[Position]
    adjustments: list[Adjustment]
    fills: list[costs_mod.Fill]
    pnl_points: float
    peak_points: float
    trough_points: float
    exit_reason: str
    became_straddle: bool = False
    wings_added: bool = False
    missing_slots: int = 0


def _fill(position: Position, price: float, quantity: int,
          opening: bool) -> costs_mod.Fill:
    """The execution a leg produces, from the side of *this* fill.

    A short leg opened and later closed is a SELL then a BUY, which is what
    decides where STT and stamp duty land. Getting this from the position's
    direction instead of the fill's would charge a repair as though it were an
    entry.
    """
    side = position.side if opening else (
        "BUY" if position.side == "SELL" else "SELL")
    return costs_mod.Fill(side=side, price=abs(price), quantity=quantity)


def _mark(legs: list[Position], cube: Cube, slot: int) -> float | None:
    """Total P&L in points across open and closed legs, or None if unpriceable.

    None rather than a stale mark: if any open leg did not print this minute the
    position genuinely could not be valued or acted on, and carrying the last
    price forward is how a stop fires against a number nobody was quoting.
    """
    total = 0.0
    for leg in legs:
        if not leg.open:
            total += leg.mark(0.0)
            continue
        current = cube.at(leg.opt_type, leg.strike, slot)
        if not np.isfinite(current) or current <= 0:
            return None
        total += leg.mark(current)
    return total


def simulate(cube: Cube, entry_slot: int, close_slot: int,
             opening: list[tuple[str, str, float, int]],
             plan: AdjustPlan, *, slippage: float, lot_size: int,
             target_points: float | None = None,
             stop_points: float | None = None,
             target_pct: float | None = None,
             stop_pct: float | None = None) -> Outcome | None:
    """Walk one position minute by minute, repairing it as the rules say.

    `opening` is the basket to enter, as (opt_type, side, strike, lots). It is
    resolved by the caller through the ordinary selectors, so an adjustment run
    picks its first strikes exactly the way every other run does.

    Targets and stops are in **points on the whole position**, converted by the
    caller, because after a repair the "entry credit" is no longer a single
    number the engine can percentage against.
    """
    legs: list[Position] = []
    fills: list[costs_mod.Fill] = []
    quantity = lot_size

    for opt_type, side, strike, lots in opening:
        price = cube.at(opt_type, strike, entry_slot)
        if not np.isfinite(price) or price <= 0:
            return None                      # cannot enter what did not print
        filled = price - slippage if side == "SELL" else price + slippage
        leg = Position(opt_type=opt_type, strike=float(strike), side=side,
                       lots=lots, entry_price=filled, entry_slot=entry_slot)
        legs.append(leg)
        fills.append(_fill(leg, filled, quantity * lots, opening=True))

    # The credit the gap rule measures itself against, fixed at entry. The
    # source rules say "40% of the sum of the two premiums you sold", which is
    # the entry sum and not a running one — re-basing it on the current
    # premiums would make the threshold chase the market and never trigger.
    entry_credit = sum(leg.entry_price * leg.lots
                       for leg in legs if leg.side == "SELL")

    # A percentage exit is a fraction of the credit STANDING AT ENTRY, not of
    # the credit after a repair. Re-basing it would move the target every time
    # the position was adjusted, so "book at 50%" would mean a different number
    # on every roll.
    if target_pct is not None:
        target_points = min(
            target_points if target_points is not None else float("inf"),
            target_pct * abs(entry_credit))
    if stop_pct is not None:
        stop_points = min(
            stop_points if stop_points is not None else float("inf"),
            stop_pct * abs(entry_credit))

    adjustments: list[Adjustment] = []
    peak = trough = 0.0
    missing = 0
    exit_reason = "time"
    final_slot = close_slot
    straddle = False
    wings_added = False

    for slot in range(entry_slot, close_slot + 1):
        value = _mark(legs, cube, slot)
        if value is None:
            missing += 1
            continue
        peak, trough = max(peak, value), min(trough, value)

        if target_points is not None and value >= target_points:
            exit_reason, final_slot = "target", slot
            break
        if stop_points is not None and value <= -abs(stop_points):
            exit_reason, final_slot = "stop", slot
            break
        if slot == close_slot:
            final_slot = slot
            break

        # --- repairs ------------------------------------------------------
        capped = (plan.max_adjustments is not None
                  and len(adjustments) >= plan.max_adjustments)
        if capped or straddle:
            continue

        shorts = [leg for leg in legs if leg.open and leg.side == "SELL"]
        if len(shorts) != 2 or shorts[0].opt_type == shorts[1].opt_type:
            continue                        # a rule about two sides needs two

        marks = [cube.at(leg.opt_type, leg.strike, slot) for leg in shorts]
        if any(not np.isfinite(v) or v <= 0 for v in marks):
            continue

        order = sorted(range(len(shorts)), key=lambda i: marks[i])
        cheap, dear = shorts[order[0]], shorts[order[-1]]
        cheap_mark, dear_mark = marks[order[0]], marks[order[-1]]
        gap = dear_mark - cheap_mark

        for rule in plan.rules:
            level = (rule.threshold * entry_credit if rule.unit == "%"
                     else rule.threshold)
            measure = gap if rule.trigger == "gap" else -value
            if measure < level:
                continue

            # Book the decayed side. This half always happens; what varies is
            # whether anything replaces it.
            price = cheap_mark
            filled = price + slippage          # buying a short back
            cheap.exit_price, cheap.exit_slot = filled, slot
            fills.append(_fill(cheap, filled, quantity * cheap.lots,
                               opening=False))
            closed_label = f"{cheap.opt_type} {cheap.strike:g}"

            if rule.action == "close-cheap":
                adjustments.append(Adjustment(
                    slot=slot, minute=int(cube.minutes[slot]), rule=rule.action,
                    closed=closed_label, opened="—"))
                break

            # Re-sell the same side at the premium the tested leg now trades,
            # which is what makes the position delta-neutral again. The bound is
            # the tested leg's strike: rolling past it would invert the strangle.
            bound = dear.strike if plan.no_crossover else None
            strike = cube.nearest_by_premium(
                cheap.opt_type, dear_mark, slot, bound=bound)
            if strike is None or (plan.no_crossover and strike == dear.strike):
                # Nowhere left to roll: the strikes have met. That is the
                # straddle the no-crossover rule is describing, and it is a
                # state, not a failure.
                straddle = True
                adjustments.append(Adjustment(
                    slot=slot, minute=int(cube.minutes[slot]), rule=rule.action,
                    closed=closed_label, opened="—",
                    reason="strikes met — now a straddle, adjusting stopped"))
                if strike is None:
                    break
                strike = dear.strike

            price = cube.at(cheap.opt_type, strike, slot)
            if not np.isfinite(price) or price <= 0:
                break
            filled = price - slippage
            fresh = Position(opt_type=cheap.opt_type, strike=float(strike),
                             side="SELL", lots=cheap.lots, entry_price=filled,
                             entry_slot=slot)
            legs.append(fresh)
            fills.append(_fill(fresh, filled, quantity * fresh.lots,
                               opening=True))
            if not straddle:
                adjustments.append(Adjustment(
                    slot=slot, minute=int(cube.minutes[slot]), rule=rule.action,
                    closed=closed_label,
                    opened=f"{fresh.opt_type} {fresh.strike:g}"))
            straddle = straddle or abs(fresh.strike - dear.strike) < 1e-9

            # A naked straddle is the exposure the wing rule exists to cap, so
            # the wings go on in the same minute the straddle forms. Real
            # execution buys them first to avoid a margin spike; at one-minute
            # resolution both land in the same bar and the ordering is a
            # broker-level detail this cannot express.
            if straddle and plan.wings and not wings_added:
                wings_added = _add_wings(legs, fills, cube, slot, plan,
                                         quantity, slippage)
            break

    value = _mark(legs, cube, final_slot)
    if value is None:
        # Close on the last minute everything printed rather than abandoning the
        # position; a run that cannot mark its own exit is worse than one that
        # marks it a minute early.
        for slot in range(final_slot, entry_slot - 1, -1):
            value = _mark(legs, cube, slot)
            if value is not None:
                final_slot = slot
                break
    if value is None:
        return None

    for leg in legs:
        if not leg.open:
            continue
        price = cube.at(leg.opt_type, leg.strike, final_slot)
        filled = price + slippage if leg.side == "SELL" else price - slippage
        leg.exit_price, leg.exit_slot = filled, final_slot
        fills.append(_fill(leg, filled, quantity * leg.lots, opening=False))

    total = sum(leg.mark(0.0) for leg in legs)
    return Outcome(day=cube.day, entry_slot=entry_slot, exit_slot=final_slot,
                   legs=legs, adjustments=adjustments, fills=fills,
                   pnl_points=total, peak_points=peak, trough_points=trough,
                   exit_reason=exit_reason, became_straddle=straddle,
                   wings_added=wings_added, missing_slots=missing)


def _add_wings(legs: list[Position], fills: list[costs_mod.Fill], cube: Cube,
               slot: int, plan: AdjustPlan, quantity: int,
               slippage: float) -> bool:
    """Buy protective wings around the straddle, capping the loss.

    'breakeven' is the straddle's own: the strike plus and minus the total
    premium standing at this minute. The source rule insists both wings sit the
    same distance from the centre, so one distance is computed and applied both
    ways rather than solving each side independently.
    """
    shorts = [leg for leg in legs if leg.open and leg.side == "SELL"]
    if len(shorts) != 2:
        return False
    centre = shorts[0].strike
    premium = 0.0
    for leg in shorts:
        current = cube.at(leg.opt_type, leg.strike, slot)
        if not np.isfinite(current):
            return False
        premium += current * leg.lots

    if plan.wings == "breakeven":
        distance = premium
    else:
        distance = float(plan.wings) * cube.step

    added = []
    for opt_type, sign in (("CE", 1.0), ("PE", -1.0)):
        strike = cube.nearest_strike(centre + sign * distance)
        if strike is None:
            return False
        price = cube.at(opt_type, strike, slot)
        if not np.isfinite(price) or price <= 0:
            return False
        added.append((opt_type, strike, price))

    for opt_type, strike, price in added:
        filled = price + slippage
        wing = Position(opt_type=opt_type, strike=float(strike), side="BUY",
                        lots=1, entry_price=filled, entry_slot=slot)
        legs.append(wing)
        fills.append(_fill(wing, filled, quantity, opening=True))
    return True
