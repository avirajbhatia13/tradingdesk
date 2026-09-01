"""How a leg chooses which contract to enter.

The engine used to answer one question — "how many strikes out of the money?" —
because that is the axis the lake is indexed on and it made selection a column
filter. That was fast and it was also a cage: "sell the 20-delta call", "buy the
wing trading near a third of the ATM premium", "go 1% out of the money" are all
ordinary things to say and none of them could be said.

This module breaks selection out of the query. The engine pulls the **whole
chain** at one minute per day — every strike the lake holds, both sides, with
price, IV and spot — and each leg's `Selector` picks a row out of it in Python.
A day is ~42 rows and five years is ~1,240 days, so the loop that was
unthinkable per *minute* is free per *day*, and the vectorised path downstream
is untouched: selection still resolves once, and the leg is still pinned to the
contract it entered.

## The honesty problem, and `clamped`

The lake holds ±10 strikes, roughly ±2% of spot. Ask for the 5-delta call on a
quiet day and the honest answer is "not in the data" — but the nearest match is
always *some* row, and returning it silently turns a missing contract into a
confident number. Every selector therefore reports whether the target fell
outside the chain it could see. The engine counts those days and the report
prints the count, so a strategy that was only half-testable says so.

## Why references are moneyness levels, not other legs

`ByPremiumRatio` needs a reference price — "a third of the ATM premium". It
could have referenced another leg by index, which would need dependency
ordering and would break when legs are reordered. Referencing a *moneyness
level on the same side* needs neither: the ATM row is already in the snapshot.
"Their respective ATM" — the call leg off the ATM call, the put leg off the ATM
put — falls out for free, and that is the phrasing these strategies actually
use.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.quant import greeks as gk


@dataclass(frozen=True)
class ChainRow:
    """One contract at the snapshot minute."""
    opt_type: str
    moneyness: int
    strike: float
    price: float
    iv: float


@dataclass
class Chain:
    """Every contract the lake holds for one day, at one minute.

    Sides are pre-split and sorted by moneyness so selectors can scan without
    re-filtering, and because "the next strike out" is only meaningful in order.
    """
    day: Any
    spot: float
    t_years: float
    calls: list[ChainRow]
    puts: list[ChainRow]

    def side(self, opt_type: str) -> list[ChainRow]:
        return self.calls if opt_type.upper() == "CE" else self.puts

    def at(self, opt_type: str, moneyness: int) -> ChainRow | None:
        for row in self.side(opt_type):
            if row.moneyness == moneyness:
                return row
        return None


@dataclass(frozen=True)
class Pick:
    """A selector's answer, plus whether it had to settle.

    `clamped` means the target lay outside the strikes the lake holds and the
    nearest available row was returned instead. It is not an error — it is the
    fact that decides whether a result about "the 5-delta wing" is about the
    5-delta wing.
    """
    row: ChainRow
    clamped: bool = False
    wanted: str = ""


class Selector:
    """Base class. Subclasses pick one row out of one side of the chain."""

    #: SQL-safe identity, used to name the leg's column and key the matrix
    #: cache. Two legs with the same selector and side share a column, which is
    #: correct — they resolve to the same contract.
    key: str = "sel"

    def describe(self) -> str:
        raise NotImplementedError

    def resolve(self, chain: Chain, opt_type: str) -> Pick | None:
        raise NotImplementedError

    def anchor_moneyness(self) -> int | None:
        """The level that must have printed for this selector to be evaluated.

        A moneyness selector needs exactly its own level. A dynamic selector
        needs the chain to exist at all, which the ATM row is the proxy for —
        it is the most liquid contract on the board, so requiring it is close
        to requiring "the market was trading".
        """
        return 0

    #: Whether this selector can also drive minute-by-minute re-striking. Only
    #: moneyness can: the rolling-series layout makes "the current 3rd OTM
    #: call" a column filter, while "the current 20-delta call" would need the
    #: whole chain at every minute, which is the per-minute scan this engine
    #: exists to avoid.
    supports_restrike: bool = False

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError


def _num(value: float) -> str:
    """A number as a SQL-safe identifier fragment."""
    text = f"{value:g}".replace("-", "m").replace(".", "_")
    return text.replace("+", "")


def _nearest(rows: list[ChainRow], value_of, target: float,
             wanted: str) -> Pick | None:
    """The row whose measured value is closest to `target`.

    Clamping is decided by *bracketing*, not by position in the list. If the
    target sits between two rows' values the chain contains the answer; if it
    is beyond every row on both sides of the comparison, the real contract is
    further out than the lake goes and the caller needs to know.
    """
    scored = [(row, value_of(row)) for row in rows]
    scored = [(row, value) for row, value in scored
              if value is not None and math.isfinite(value)]
    if not scored:
        return None
    best = min(scored, key=lambda pair: abs(pair[1] - target))
    values = [value for _, value in scored]
    clamped = not (min(values) <= target <= max(values))
    return Pick(best[0], clamped=clamped, wanted=wanted)


@dataclass(frozen=True)
class ByMoneyness(Selector):
    """`n` strikes out of the money — the original axis, still the default.

    Signed and symmetric: `+5` is an OTM call and an OTM put alike, `-3` is in
    the money on either side. This is the only selector that can re-strike,
    because it is the only one the lake is physically indexed by.
    """
    moneyness: int = 0

    supports_restrike = True

    @property
    def key(self) -> str:
        n = self.moneyness
        return f"p{n}" if n >= 0 else f"m{abs(n)}"

    def anchor_moneyness(self) -> int | None:
        return self.moneyness

    def describe(self) -> str:
        if self.moneyness == 0:
            return "ATM"
        return f"{abs(self.moneyness)} strikes {'OTM' if self.moneyness > 0 else 'ITM'}"

    def resolve(self, chain: Chain, opt_type: str) -> Pick | None:
        row = chain.at(opt_type, self.moneyness)
        return Pick(row, wanted=self.describe()) if row else None

    def to_dict(self) -> dict[str, Any]:
        return {"rule": "moneyness", "moneyness": self.moneyness}


@dataclass(frozen=True)
class ByPremium(Selector):
    """The contract trading nearest a given price.

    "Sell the ₹100 call" — how a lot of desks actually size a short strangle,
    because premium collected is the thing being managed, not distance.
    """
    target: float = 100.0

    @property
    def key(self) -> str:
        return f"prem{_num(self.target)}"

    def describe(self) -> str:
        return f"premium nearest ₹{self.target:g}"

    def resolve(self, chain: Chain, opt_type: str) -> Pick | None:
        return _nearest(chain.side(opt_type), lambda r: r.price, self.target,
                        f"premium ₹{self.target:g}")

    def to_dict(self) -> dict[str, Any]:
        return {"rule": "premium", "target": self.target}


@dataclass(frozen=True)
class ByPremiumRatio(Selector):
    """The contract trading nearest a fraction of another strike's premium.

    This is the one that motivated the rework. "Buy the call whose LTP is the
    ATM call's premium divided by three" is a ratio spread's whole definition,
    and it is unsayable in strikes because the answer moves with vol: the same
    rule lands on the 4th strike on a quiet day and the 9th on a wild one.

    The reference is a moneyness level on the *same side*, so a call leg reads
    off the call chain and a put leg off the put chain. "Their respective ATM"
    needs no extra syntax.
    """
    ref_moneyness: int = 0
    factor: float = 1.0 / 3.0

    @property
    def key(self) -> str:
        return f"ratio{_num(self.ref_moneyness)}x{_num(round(self.factor, 4))}"

    def anchor_moneyness(self) -> int | None:
        # The reference row is the one this rule genuinely cannot work without,
        # so it is what a day must have printed to be tradeable.
        return self.ref_moneyness

    def describe(self) -> str:
        base = ("ATM" if self.ref_moneyness == 0
                else f"{self.ref_moneyness:+d} strike")
        # A third reads better than 0.3333, and these rules are always spoken
        # as divisors.
        if self.factor > 0 and abs(1 / self.factor - round(1 / self.factor)) < 1e-6:
            return f"premium ≈ {base} ÷ {round(1 / self.factor):g}"
        return f"premium ≈ {self.factor:g} × {base}"

    def resolve(self, chain: Chain, opt_type: str) -> Pick | None:
        reference = chain.at(opt_type, self.ref_moneyness)
        if reference is None or not math.isfinite(reference.price):
            return None
        target = reference.price * self.factor
        return _nearest(chain.side(opt_type), lambda r: r.price, target,
                        f"premium ₹{target:.1f} ({self.describe()})")

    def to_dict(self) -> dict[str, Any]:
        return {"rule": "premium_ratio", "ref_moneyness": self.ref_moneyness,
                "factor": self.factor}


@dataclass(frozen=True)
class ByDelta(Selector):
    """The contract nearest a target delta, by absolute value.

    Delta is not in the lake — no vendor here serves it (Dhan ignores the field
    entirely, verified by probing). It is computed from the IV the vendor *does*
    serve, using the same Black-76 core the live dashboard prices with.

    The weak input is time to expiry, which vendor rolling data does not carry;
    the engine supplies an average for the series. Delta is not very sensitive
    to it away from the last session, but on expiry day it is, and a delta
    target on expiry day should be read as indicative.
    """
    target: float = 0.20

    @property
    def key(self) -> str:
        return f"d{_num(round(abs(self.target), 4))}"

    def describe(self) -> str:
        return f"{abs(self.target) * 100:g} delta"

    def resolve(self, chain: Chain, opt_type: str) -> Pick | None:
        if chain.spot <= 0 or chain.t_years <= 0:
            return None
        rate = 0.0

        def delta_of(row: ChainRow) -> float | None:
            if not (row.iv and math.isfinite(row.iv) and row.iv > 0):
                return None
            greeks = gk.b76_greeks(chain.spot, row.strike, chain.t_years,
                                   row.iv, row.opt_type, rate)
            return abs(greeks["delta"])

        return _nearest(chain.side(opt_type), delta_of, abs(self.target),
                        f"{abs(self.target) * 100:g} delta")

    def to_dict(self) -> dict[str, Any]:
        return {"rule": "delta", "target": self.target}


@dataclass(frozen=True)
class ByPctOfSpot(Selector):
    """The strike nearest a percentage away from spot, out of the money.

    Distance in percent rather than strikes, which is what makes a rule
    portable between NIFTY and BANKNIFTY — 1% is 1% on both, while "5 strikes"
    means completely different things at a 50-point and a 100-point step.
    """
    pct: float = 0.01

    @property
    def key(self) -> str:
        return f"pct{_num(round(self.pct * 100, 4))}"

    def describe(self) -> str:
        return f"{self.pct * 100:g}% out of the money"

    def resolve(self, chain: Chain, opt_type: str) -> Pick | None:
        if chain.spot <= 0:
            return None
        away = abs(self.pct)
        target = chain.spot * (1 + away if opt_type.upper() == "CE" else 1 - away)
        return _nearest(chain.side(opt_type), lambda r: r.strike, target,
                        f"strike ≈ {target:.0f} ({self.describe()})")

    def to_dict(self) -> dict[str, Any]:
        return {"rule": "pct_of_spot", "pct": self.pct}


@dataclass(frozen=True)
class ByStrikeOffset(Selector):
    """The strike nearest a fixed number of points away from spot, OTM."""
    points: float = 200.0

    @property
    def key(self) -> str:
        return f"off{_num(self.points)}"

    def describe(self) -> str:
        return f"{abs(self.points):g} points out of the money"

    def resolve(self, chain: Chain, opt_type: str) -> Pick | None:
        if chain.spot <= 0:
            return None
        away = abs(self.points)
        target = chain.spot + (away if opt_type.upper() == "CE" else -away)
        return _nearest(chain.side(opt_type), lambda r: r.strike, target,
                        f"strike ≈ {target:.0f}")

    def to_dict(self) -> dict[str, Any]:
        return {"rule": "strike_offset", "points": self.points}


@dataclass(frozen=True)
class ByStrike(Selector):
    """One fixed strike, for reproducing a specific historical position.

    Only sensible over a short date range — a strike that is at the money in
    January is far off the board by March, and the chain only extends ±10.
    """
    strike: float = 0.0

    @property
    def key(self) -> str:
        return f"k{_num(self.strike)}"

    def describe(self) -> str:
        return f"strike {self.strike:g}"

    def resolve(self, chain: Chain, opt_type: str) -> Pick | None:
        return _nearest(chain.side(opt_type), lambda r: r.strike, self.strike,
                        f"strike {self.strike:g}")

    def to_dict(self) -> dict[str, Any]:
        return {"rule": "strike", "strike": self.strike}


_RULES = {
    "moneyness": lambda d: ByMoneyness(int(d.get("moneyness", 0))),
    "premium": lambda d: ByPremium(float(d.get("target", 100.0))),
    "premium_ratio": lambda d: ByPremiumRatio(
        int(d.get("ref_moneyness", 0)), float(d.get("factor", 1 / 3))),
    "delta": lambda d: ByDelta(float(d.get("target", 0.20))),
    "pct_of_spot": lambda d: ByPctOfSpot(float(d.get("pct", 0.01))),
    "strike_offset": lambda d: ByStrikeOffset(float(d.get("points", 200.0))),
    "strike": lambda d: ByStrike(float(d.get("strike", 0.0))),
}


def from_dict(payload: dict[str, Any] | None) -> Selector:
    """Rebuild a selector from its serialised form.

    Saved runs carry their selectors so `spec.json` stays a reproduction
    recipe rather than a description — the whole point of numbering runs is
    that "run 003 again with a wider stop" is mechanical.
    """
    if not payload:
        return ByMoneyness(0)
    rule = str(payload.get("rule", "moneyness"))
    builder = _RULES.get(rule)
    if builder is None:
        raise ValueError(f"unknown selection rule {rule!r}; "
                         f"expected one of {', '.join(sorted(_RULES))}")
    return builder(payload)
