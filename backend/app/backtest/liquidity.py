"""Telling a price you could have traded from one nobody traded.

Every strike Dhan sells is liquid — within ±10 of the money on NIFTY, **0.1%**
of bars carry a price unchanged from the minute before. So the engine's entire
tradeability test was `price > 0`, and it was right, by accident: the vendor's
±10 clamp was a data limitation doing silent duty as a liquidity filter.

Upstox sells every strike. Beyond ±30, **87.4%** of bars are unchanged from the
previous minute — the `close` is a carry-forward of a print that may be hours
old. Nothing distinguishes it from a live quote, and a backtest that fills
against it reports a trade nobody could have made. Worse, the error is
*flattering*: a frozen price never moves against you, so the more illiquid the
strike, the better the strategy looks.

So the data got wider and the engine got less safe, with no test failing and no
error raised. This module is what closes that.

## Three states, not two

Refusing to trade anything illiquid throws away the width that was just bought
— a wing you would genuinely sell at 25 strikes out is a real trade with a real
market, it just does not print every minute. So a bar is one of three things:

- **traded** — it printed this minute. Use it.
- **stale** — a carry-forward. The contract has a market; this minute has no
  news. Mark it to the surface and say so.
- **dead** — nothing traded all session, or there is no open interest. There
  was no market. Refuse.

## Why the surface and not the last print

An option's fair value comes from spot, strike, time and volatility, and
volatility is a smooth function of strike that the liquid part of the chain
pins down. That is how a desk quotes a wing: not from its last trade, but from
the surface the traded strikes imply. `fit_smile` recovers that curve from
whatever printed, and `fair_value` reads the wing off it.

**The extrapolation is the weak point and is deliberately bounded.** A smile
fitted on ±10 and read at ±40 is an extrapolation of three times its own
support; the quadratic would turn over and hand back nonsense. So the fit is
clamped to the range it was calibrated on, flagged when it extrapolates, and
`fair_value` refuses beyond `MAX_EXTRAPOLATION_STRIKES`.

## How good is the mark, measured

Held out every third traded strike on a real NIFTY board (2025-07-15 11:30,
2 DTE), fitted on the rest, and compared the model against what actually
traded:

- **beyond 10 strikes out** — where marking actually happens — median error
  **₹0.72**, about 11% of a ₹3-4 wing.
- **within 10 strikes** — median error ₹13.66, around 12%. Large, and mostly
  irrelevant: those bars trade every minute, so they are marked at their own
  print and never reach the model.

Two things that matters for. First, the error is worst exactly where a
quadratic misfits a very short-dated smile, which is steep and closer to a V
than a parabola — so **trust the mark less as expiry approaches**. Second, 11%
on a wing is not precision. It is better than the alternative, which is a
frozen print of unbounded staleness, but it is an estimate and the report must
carry it as one.

Time to expiry is measured to the **minute**, not the day. Using whole days at
2 DTE moved the median error from ₹13.66 to ₹17.77 on its own.

## A fair value is still not a fill

Marking a stale bar to the surface answers *what was it worth*, which is not
*what would I have paid*. Those differ by the spread, and on a contract trading
75 lots a minute the spread is not the flat half-point the engine charges
everywhere. `spread_points` scales it with actual liquidity, and the caller is
expected to run the pessimistic case as well — see `Marking.worst`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.quant import greeks as gk

# A bar is stale if its close is unchanged from the previous minute *and*
# nothing traded in it. Either alone is weak evidence: a liquid contract can
# print the same price twice, and a zero-volume minute between two trades is
# still bracketed by a live market. Together they mean a carry-forward.
#
# Both are required deliberately. Using price-unchanged alone would call 28% of
# at-the-money bars stale, which is nonsense — the ATM contract trades 1,275
# lots a minute and simply prints the same tick twice.
STALE_NEEDS_BOTH = True

# Sessions where a contract never traded at all. Below this many traded minutes
# in a session there is no market to speak of, whatever the last price says.
DEAD_BELOW_TRADED_MINUTES = 5

# Open interest below which a contract is treated as having no market, whatever
# its volume did. A contract nobody holds cannot be exited.
DEAD_BELOW_OI = 500

# How far past the calibrated strike range a smile may be read, in strikes.
# The fit is quadratic in log-moneyness: reliable inside its support, and a
# parabola that turns over and returns a *rising* wing vol — or a negative one
# — some way outside it. Refusing is the only honest option there.
MAX_EXTRAPOLATION_STRIKES = 12

# Floor and ceiling on any volatility this module will hand back. NIFTY has
# never printed outside this and a fit that wants to is a fit that has failed.
MIN_VOL, MAX_VOL = 0.02, 3.0

# Spread as a fraction of price, against liquidity. Anchored at the observed
# medians: the at-the-money contract trades ~1,275 lots a minute and quotes
# inside a tick on a ₹200 option (~0.025%), while a 30-strike wing trades ~75
# and quotes far wider. These are estimates and are labelled as such wherever
# they reach a report — no vendor here sells historical quotes, so a spread
# cannot be measured from this data, only modelled.
SPREAD_AT_FULL_LIQUIDITY = 0.0005
SPREAD_AT_NO_LIQUIDITY = 0.15
LIQUID_MINUTE_VOLUME = 1000.0


@dataclass(frozen=True)
class BarQuality:
    """What a single bar is, and whether it can be traded against."""
    state: str                      # 'traded' | 'stale' | 'dead'
    minutes_stale: int = 0

    @property
    def tradeable(self) -> bool:
        return self.state != "dead"

    @property
    def needs_marking(self) -> bool:
        return self.state == "stale"


@dataclass
class Smile:
    """Implied volatility as a function of strike, fitted to what traded.

    Quadratic in log-moneyness — the standard shape, and the most a sparse
    chain supports. Anything richer (SVI, SABR) fits the noise in a chain this
    thin and buys nothing the extrapolation limit does not already cost.
    """
    a: float                        # level
    b: float                        # skew
    c: float                        # curvature
    spot: float
    t_years: float
    lo_strike: float
    hi_strike: float
    step: float
    points: int = 0

    def vol_at(self, strike: float) -> tuple[float, bool]:
        """Volatility at a strike, and whether that required extrapolating."""
        if strike <= 0 or self.spot <= 0:
            return float("nan"), True
        edge = self.step * MAX_EXTRAPOLATION_STRIKES
        outside = strike < self.lo_strike or strike > self.hi_strike
        if strike < self.lo_strike - edge or strike > self.hi_strike + edge:
            return float("nan"), True

        # Read the curve at the boundary rather than beyond it: past its
        # support the parabola turns over, and a wing vol that falls as you go
        # further out is worse than no answer.
        clamped = min(max(strike, self.lo_strike), self.hi_strike)
        k = math.log(clamped / self.spot)
        vol = self.a + self.b * k + self.c * k * k
        if not math.isfinite(vol):
            return float("nan"), outside
        return min(max(vol, MIN_VOL), MAX_VOL), outside


@dataclass
class Marking:
    """What a bar is worth, how that was decided, and the pessimistic case."""
    price: float
    source: str                     # 'traded' | 'model' | 'unavailable'
    spread: float = 0.0
    extrapolated: bool = False

    @property
    def usable(self) -> bool:
        return self.source != "unavailable" and self.price > 0

    def worst(self, side: str) -> float:
        """The far side of the spread — what a fill would actually have cost.

        Reported alongside the fair value rather than instead of it, because a
        strategy that only survives the mid is a strategy that does not survive.
        """
        half = self.spread / 2.0
        return self.price + half if side.upper() == "BUY" else max(
            self.price - half, 0.05)


def classify(volume: float, close: float, previous_close: float | None,
             oi: float = 0.0, traded_minutes: int = 0,
             minutes_stale: int = 0) -> BarQuality:
    """What state one bar is in.

    `traded_minutes` is how many minutes of the *session* this contract
    printed in, which is what separates a quiet contract from a dead one — a
    single bar cannot tell you that on its own.
    """
    if close is None or not math.isfinite(close) or close <= 0:
        return BarQuality("dead")
    if oi and oi < DEAD_BELOW_OI:
        return BarQuality("dead")
    if traded_minutes and traded_minutes < DEAD_BELOW_TRADED_MINUTES:
        return BarQuality("dead")

    unchanged = (previous_close is not None
                 and abs(close - previous_close) < 1e-9)
    quiet = not volume or volume <= 0
    stale = (unchanged and quiet) if STALE_NEEDS_BOTH else (unchanged or quiet)
    if stale:
        return BarQuality("stale", minutes_stale=minutes_stale + 1)
    return BarQuality("traded")


def _implied(price: float, spot: float, strike: float, t_years: float,
             opt_type: str) -> float | None:
    """Invert one price for its volatility.

    `r=0` throughout, which is what makes the spot-based solver and the
    forward-based `b76_price` agree — at zero carry the forward *is* spot. The
    two must not be mixed at a non-zero rate; see the warning in greeks.py,
    where doing so cost a 1.6 vol-point call/put disagreement.

    Only the solver's own "outside no-arbitrage bounds" None is tolerated. A
    TypeError here means the call is wrong, and swallowing it silently turns
    every fit into "not enough points" — which is precisely how the signature
    mismatch on this line went unnoticed until the tests ran.
    """
    vol = gk.implied_vol(price, spot, strike, t_years, opt_type, 0.0)
    if vol is None or not math.isfinite(vol) or not (MIN_VOL <= vol <= MAX_VOL):
        return None
    return float(vol)


def fit_smile(rows: Iterable[Any], spot: float, t_years: float,
              step: float = 50.0, min_points: int = 4) -> Smile | None:
    """Fit volatility against strike using only the contracts that traded.

    `rows` need `strike`, `opt_type`, `price` and a truthy `traded` flag.
    Stale rows are excluded on purpose: fitting the surface to carry-forward
    prices would launder the very staleness this exists to correct.

    Returns None when too little traded to say anything, which is the right
    answer on a thin session and must not be papered over with a default vol.
    """
    if spot <= 0 or t_years <= 0:
        return None

    xs: list[float] = []
    ys: list[float] = []
    strikes: list[float] = []
    for row in rows:
        if not getattr(row, "traded", True):
            continue
        strike = float(getattr(row, "strike", 0) or 0)
        price = float(getattr(row, "price", 0) or 0)
        opt_type = getattr(row, "opt_type", "CE")
        if strike <= 0 or price <= 0:
            continue
        # Out-of-the-money only. The in-the-money side is mostly intrinsic, so
        # its implied vol is a tiny time-value residual on a large number and
        # inverts terribly — one paisa of price error moves it enormously.
        if (opt_type.upper() == "CE" and strike < spot) or \
           (opt_type.upper() == "PE" and strike > spot):
            continue
        vol = _implied(price, spot, strike, t_years, opt_type)
        if vol is None:
            continue
        xs.append(math.log(strike / spot))
        ys.append(vol)
        strikes.append(strike)

    if len(xs) < min_points:
        return None

    coeffs = _quadratic_fit(xs, ys)
    if coeffs is None:
        return None
    a, b, c = coeffs
    return Smile(a=a, b=b, c=c, spot=spot, t_years=t_years,
                 lo_strike=min(strikes), hi_strike=max(strikes),
                 step=step, points=len(xs))


def _quadratic_fit(xs: Sequence[float],
                   ys: Sequence[float]) -> tuple[float, float, float] | None:
    """Least squares on [1, x, x^2], by normal equations.

    Written out rather than pulled from numpy.polyfit because this runs inside
    the per-day snapshot loop and the arrays are a few dozen points; the call
    overhead dominates the arithmetic. Falls back to a flat fit when the system
    is singular, which happens when every traded strike sits at one price.
    """
    n = len(xs)
    if n == 0:
        return None
    s0 = float(n)
    s1 = sum(xs)
    s2 = sum(x * x for x in xs)
    s3 = sum(x ** 3 for x in xs)
    s4 = sum(x ** 4 for x in xs)
    t0 = sum(ys)
    t1 = sum(x * y for x, y in zip(xs, ys))
    t2 = sum(x * x * y for x, y in zip(xs, ys))

    matrix = [[s0, s1, s2], [s1, s2, s3], [s2, s3, s4]]
    vector = [t0, t1, t2]
    solved = _solve3(matrix, vector)
    if solved is None:
        return (t0 / s0, 0.0, 0.0)      # flat: better than nothing, honest
    a, b, c = solved
    if not all(math.isfinite(v) for v in (a, b, c)):
        return (t0 / s0, 0.0, 0.0)
    return a, b, c


def _solve3(matrix: list[list[float]],
            vector: list[float]) -> tuple[float, float, float] | None:
    """Gaussian elimination with partial pivoting on a 3x3."""
    rows = [list(matrix[i]) + [vector[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(3):
            if r == col:
                continue
            factor = rows[r][col] / rows[col][col]
            for k in range(col, 4):
                rows[r][k] -= factor * rows[col][k]
    try:
        return (rows[0][3] / rows[0][0], rows[1][3] / rows[1][1],
                rows[2][3] / rows[2][2])
    except ZeroDivisionError:
        return None


def spread_points(price: float, volume: float, oi: float = 0.0) -> float:
    """Estimated bid-ask width, in points, for a contract this liquid.

    **This is a model, not a measurement.** No vendor here sells historical
    quotes — the lake stores traded prices only — so a spread cannot be
    recovered from this data at any width. What can be said is that it scales
    inversely with activity, and that charging a liquid contract's half-point
    on a ₹6 wing that trades 75 lots a minute is fiction in the flattering
    direction.

    Anchored at the observed medians, interpolated on sqrt(volume) because
    depth thins far faster than volume does.
    """
    if price <= 0:
        return 0.0
    activity = max(float(volume or 0.0), 0.0)
    ratio = min(math.sqrt(activity / LIQUID_MINUTE_VOLUME), 1.0) \
        if LIQUID_MINUTE_VOLUME > 0 else 1.0
    fraction = (SPREAD_AT_NO_LIQUIDITY
                + (SPREAD_AT_FULL_LIQUIDITY - SPREAD_AT_NO_LIQUIDITY) * ratio)
    if oi and oi < DEAD_BELOW_OI * 4:
        fraction *= 1.5
    return max(price * fraction, 0.05)


def mark(quality: BarQuality, price: float, strike: float, opt_type: str,
         spot: float, t_years: float, smile: Smile | None,
         volume: float = 0.0, oi: float = 0.0) -> Marking:
    """What this bar is worth, and by what authority.

    A traded bar is worth what it traded at. A stale one is worth what the
    surface says, if the surface reaches that far. A dead one is worth nothing
    that can be acted on, and says so rather than guessing.
    """
    if quality.state == "dead":
        return Marking(0.0, "unavailable")

    if quality.state == "traded":
        return Marking(price, "traded",
                       spread=spread_points(price, volume, oi))

    if smile is None:
        return Marking(0.0, "unavailable")
    vol, extrapolated = smile.vol_at(strike)
    if not math.isfinite(vol):
        return Marking(0.0, "unavailable")
    modelled = gk.b76_price(spot, strike, t_years, vol, opt_type, 0.0)
    if not math.isfinite(modelled) or modelled <= 0:
        return Marking(0.0, "unavailable")
    return Marking(float(modelled), "model",
                   spread=spread_points(modelled, volume, oi),
                   extrapolated=extrapolated)


@dataclass
class Tally:
    """How a run's fills were arrived at, for the report to print.

    A backtest that quietly marked a third of its fills to a model is a
    different claim from one that traded every print, and the difference has to
    survive into the report or the number means nothing.
    """
    traded: int = 0
    modelled: int = 0
    extrapolated: int = 0
    refused: int = 0
    spread_paid: float = 0.0
    _prices: list[float] = field(default_factory=list)

    def add(self, marking: Marking) -> None:
        if marking.source == "traded":
            self.traded += 1
        elif marking.source == "model":
            self.modelled += 1
            if marking.extrapolated:
                self.extrapolated += 1
        else:
            self.refused += 1
        if marking.usable:
            self.spread_paid += marking.spread
            self._prices.append(marking.price)

    @property
    def total(self) -> int:
        return self.traded + self.modelled + self.refused

    @property
    def modelled_pct(self) -> float:
        return 100.0 * self.modelled / self.total if self.total else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "fills": self.total,
            "traded": self.traded,
            "modelled": self.modelled,
            "modelled_pct": round(self.modelled_pct, 1),
            "extrapolated": self.extrapolated,
            "refused": self.refused,
            "avg_spread": round(
                self.spread_paid / len(self._prices), 2) if self._prices else 0.0,
        }
