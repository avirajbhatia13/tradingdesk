"""Recovering expiry dates the lake does not store.

Vendor rolling data never names the contract. `series = WEEK` means "whatever
the front weekly is on this date", so the same strike on two dates a week apart
is two different contracts. For an intraday backtest that does not matter — the
position opens and closes inside one session. For a **positional** one it is
fatal: holding a strike across a roll would silently splice one contract's
prices onto another's and report a P&L for a position nobody could have held.

So the expiry dates have to come from somewhere, and they can be recovered from
the data itself.

## How

Black-76 prices an option from forward, strike, time and volatility. The lake
carries price, strike, spot **and the vendor's implied vol** — which leaves time
as the only unknown, so it can be solved for by bisection.

Measured on real NIFTY weeklies, the recovered series is unambiguous:

    Mon 3.7 → Tue 2.5 → Wed 1.3 → Thu 0.17 → Fri 7.9 → Mon 3.7 …

Inside a cycle time can only fall. **Any increase is a roll**, and the last
session before it is the expiry. Across 1,235 NIFTY weekly sessions every one
solved, and the jumps were never smaller than 3.3 days — nothing marginal about
the signal.

## What the absolute number is and is not

The recovered figure is biased: a Friday reads ~7.9 days when the true answer is
6. Spot is used where the forward belongs, and the vendor's vol is quoted on its
own convention. **So this module never reports the recovered number** — it uses
it only to find the *boundaries*, and then reports real dates, counted in real
sessions between them. The bias is constant within a cycle, so it cancels
entirely out of the comparison that matters.

## Erring on the safe side

A false roll ends a hold early — conservative, and visible in the report. A
*missed* roll splices two contracts and is silent. The threshold is therefore
set low deliberately: over-detection costs a truncated trade, under-detection
costs the truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.data import lake
from app.data.schema import LAKE_DIR
from app.quant import greeks as gk

ROOT = LAKE_DIR.parent / "expiries"

# Days of recovered time-to-expiry that count as a roll. Inside a cycle the
# figure falls by about one a session, so anything rising by a full day is a
# new contract. Set below the smallest jump actually measured (3.3 days on
# NIFTY weeklies) because a false roll is cheap and a missed one is not.
ROLL_THRESHOLD_DAYS = 1.0

# The bisection's upper bound. Nothing in the lake is further out than a
# quarterly, and a bound too generous makes the solve slower for no gain.
MAX_YEARS = 120 / 365.0

# The hour to sample. Mid-morning: past the opening auction's noise, and every
# session has bars there.
SAMPLE_HOUR = 11


@dataclass(frozen=True)
class Cycle:
    """One contract's life, in the sessions the lake actually holds."""
    expiry: date
    sessions: tuple[date, ...]

    @property
    def start(self) -> date:
        return self.sessions[0]


def _solve_t(price: float, forward: float, strike: float, sigma: float,
             opt_type: str) -> float | None:
    """Time to expiry, by bisection on the Black-76 price.

    Monotonic in time for any option with time value left, which is what makes
    bisection safe here — the same reasoning as the IV solver next door.
    """
    if price <= 0 or forward <= 0 or strike <= 0 or sigma <= 0:
        return None
    lo, hi = 1e-6, MAX_YEARS
    if gk.b76_price(forward, strike, hi, sigma, opt_type, 0.0) < price:
        return None
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if gk.b76_price(forward, strike, mid, sigma, opt_type, 0.0) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _path(underlying: str, series: str):
    return ROOT / f"{underlying.upper()}-{series.upper()}.json"


def build(underlying: str, series: str) -> list[Cycle]:
    """Derive the cycle calendar from the lake."""
    rows = lake.query(
        """SELECT ts::DATE, avg(close), avg(iv), avg(strike), avg(spot)
           FROM option_bars
           WHERE underlying = ? AND series = ? AND opt_type = 'CE'
             AND moneyness = 0 AND iv > 0
             AND extract('hour' FROM ts) = ?
           GROUP BY 1 ORDER BY 1""",
        [underlying.upper(), series.upper(), SAMPLE_HOUR])

    days: list[date] = []
    times: list[float] = []
    for day, price, iv, strike, spot in rows:
        solved = _solve_t(float(price), float(spot), float(strike),
                          float(iv) / 100.0, "CE")
        if solved is not None:
            days.append(day)
            times.append(solved * 365.0)

    cycles: list[Cycle] = []
    current: list[date] = []
    for i, day in enumerate(days):
        rolled = i > 0 and times[i] > times[i - 1] + ROLL_THRESHOLD_DAYS
        if rolled and current:
            cycles.append(Cycle(expiry=current[-1], sessions=tuple(current)))
            current = []
        current.append(day)
    if current:
        # The final cycle has not been observed expiring, so its last session is
        # the last one in the lake rather than a real expiry. Kept, because a
        # hold inside it is still safe — the contract has not rolled.
        cycles.append(Cycle(expiry=current[-1], sessions=tuple(current)))
    return cycles


def _serialise(cycles: list[Cycle]) -> list[dict[str, Any]]:
    return [{"expiry": c.expiry.isoformat(),
             "sessions": [d.isoformat() for d in c.sessions]} for c in cycles]


def _deserialise(payload: list[dict[str, Any]]) -> list[Cycle]:
    return [Cycle(expiry=date.fromisoformat(c["expiry"]),
                  sessions=tuple(date.fromisoformat(d) for d in c["sessions"]))
            for c in payload]


_CACHE: dict[tuple[str, str], list[Cycle]] = {}


def cycles(underlying: str, series: str, refresh: bool = False) -> list[Cycle]:
    """The cycle calendar, built once and cached on disk.

    Cached because it is a property of history, which does not change — and
    because solving 1,200 sessions takes a second or so, which is not something
    to pay on every backtest.
    """
    key = (underlying.upper(), series.upper())
    if not refresh and key in _CACHE:
        return _CACHE[key]
    path = _path(*key)
    if not refresh and path.exists():
        try:
            found = _deserialise(json.loads(path.read_text()))
            _CACHE[key] = found
            return found
        except (ValueError, OSError, KeyError):
            pass
    built = build(*key)
    ROOT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_serialise(built), indent=2))
    _CACHE[key] = built
    return built


def clear_cache() -> None:
    _CACHE.clear()


def calendar(underlying: str, series: str) -> dict[date, Cycle]:
    """Session -> the cycle it belongs to."""
    out: dict[date, Cycle] = {}
    for cycle in cycles(underlying, series):
        for day in cycle.sessions:
            out[day] = cycle
    return out


def expiry_of(underlying: str, series: str, day: date) -> date | None:
    cycle = calendar(underlying, series).get(day)
    return cycle.expiry if cycle else None


def sessions_to_expiry(underlying: str, series: str, day: date) -> int | None:
    """Trading sessions left, counted in the lake's own sessions.

    Sessions rather than calendar days, because that is what actually decays a
    position and what a "hold for 3 days" rule means.
    """
    cycle = calendar(underlying, series).get(day)
    if not cycle or day not in cycle.sessions:
        return None
    return len(cycle.sessions) - 1 - cycle.sessions.index(day)


def is_expiry_day(underlying: str, series: str, day: date) -> bool:
    return sessions_to_expiry(underlying, series, day) == 0


def summarise(underlying: str, series: str) -> dict[str, Any]:
    """What the calendar looks like, for the resume tool and for sanity."""
    found = cycles(underlying, series)
    if not found:
        return {"underlying": underlying, "series": series, "cycles": 0}
    lengths = sorted(len(c.sessions) for c in found)
    weekdays: dict[str, int] = {}
    for cycle in found:
        name = cycle.expiry.strftime("%a")
        weekdays[name] = weekdays.get(name, 0) + 1
    return {
        "underlying": underlying.upper(), "series": series.upper(),
        "cycles": len(found),
        "first": found[0].start.isoformat(),
        "last": found[-1].expiry.isoformat(),
        "sessions_per_cycle_median": lengths[len(lengths) // 2],
        "sessions_per_cycle_range": [lengths[0], lengths[-1]],
        "expiry_weekdays": dict(sorted(weekdays.items(),
                                       key=lambda kv: -kv[1])),
    }
