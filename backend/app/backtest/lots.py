"""Lot sizes, which are not constant and change a backtest's P&L linearly.

`StrategySpec.lot_size` is one number for a whole run. That is fine for a
strategy tested over a few months and quietly wrong over years, because the
exchange revises the contract multiplier and a "1 lot" position in 2024 is not
a "1 lot" position now:

    NIFTY      25 -> 75 (Feb 2025) -> 65 (Jan 2026)
    BANKNIFTY  15 -> 30 (Feb 2025) -> 35 (Jul 2025) -> 30 (Jan 2026)
    SENSEX     10 -> 20 (Feb 2025)

A five-year NIFTY run at a fixed 75 therefore reports **three times** the
rupees that were actually available in late 2024. Two things soften that and
both should be said out loud rather than discovered later:

- **Return on capital is roughly preserved.** Margin scales with quantity too,
  so both halves of the ratio move together. It is the absolute rupee figures
  that are wrong, which matters most when sizing an account.
- **Brokerage does not scale.** It is flat per order, so a run at too large a
  lot size understates costs relative to the position — a small bias in the
  flattering direction.

## Why this is off by default

Switching every run to per-date sizing would change the P&L of every backtest
already on disk, and the promise that a stored run re-runs **to the rupee** is
the only thing standing between a refactor and silently rewriting history. So
`StrategySpec.lot_calendar` defaults to False and nothing changes until a run
asks for it.

## What is known, and what is not

The table below was read off live Upstox contract listings on 17 Aug 2026 —
every contract carries the lot size in force when it was *listed*, which is why
a monthly listed before a revision expires with the old size while the weeklies
around it already carry the new one (NIFTY 2025-01-30 is exactly that).

**It begins where Upstox's retention begins, 2024-10, and the lake begins
2021-08.** So for most of the lake's history the true lot size is simply not
known here. Rather than guess, `size_on` falls back to the spec's own number
and says it did; the engine counts those sessions and the report prints the
count. An unknown that announces itself is recoverable — one that defaults
silently is not.

Two limits of the table itself, so nobody reads more precision into it than is
there. Dates are **expiry** dates used as trading-date thresholds, so a change
lands within about a week of where it truly falls. And a size is only recorded
once it persists across two expiries — see `_MIN_CONSECUTIVE`.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from datetime import date
from typing import Any

from app.data.schema import LAKE_DIR

ROOT = LAKE_DIR.parent / "lots"

# Observed, not authoritative — see the module docstring. Each entry is the
# first *expiry* seen carrying that size, sorted ascending.
OBSERVED: dict[str, list[tuple[date, int]]] = {
    "NIFTY": [(date(2024, 10, 3), 25), (date(2025, 2, 6), 75),
              (date(2026, 1, 6), 65)],
    "BANKNIFTY": [(date(2024, 10, 1), 15), (date(2025, 2, 27), 30),
                  (date(2025, 7, 31), 35), (date(2026, 1, 27), 30)],
    "SENSEX": [(date(2024, 10, 4), 10), (date(2025, 2, 4), 20)],
}


def _path(underlying: str):
    return ROOT / f"{underlying.upper()}.json"


_CACHE: dict[str, list[tuple[date, int]]] = {}


def calendar(underlying: str) -> list[tuple[date, int]]:
    """Lot-size changes for one underlying, earliest first.

    A refreshed file on disk wins over the built-in table, so a later probe of
    the vendor extends the record without a code change.
    """
    key = underlying.upper()
    if key in _CACHE:
        return _CACHE[key]
    found = list(OBSERVED.get(key, ()))
    path = _path(key)
    if path.exists():
        try:
            payload = json.loads(path.read_text())
            parsed = sorted((date.fromisoformat(row["from"]), int(row["lot_size"]))
                            for row in payload)
            if parsed:
                found = parsed
        except (ValueError, OSError, KeyError, TypeError):
            pass
    _CACHE[key] = found
    return found


def clear_cache() -> None:
    _CACHE.clear()


def size_on(underlying: str, day: date, default: int) -> tuple[int, bool]:
    """The lot size in force on a date, and whether it is actually known.

    Returns `(default, False)` for any date before the record starts. The
    caller is expected to count those rather than ignore them — a run silently
    sized by assumption for four of its five years is a run reporting a number
    nobody can act on.
    """
    changes = calendar(underlying)
    if not changes or day < changes[0][0]:
        return default, False
    index = bisect_right([start for start, _ in changes], day) - 1
    return changes[index][1], True


def known_from(underlying: str) -> date | None:
    changes = calendar(underlying)
    return changes[0][0] if changes else None


def save(underlying: str, changes: list[tuple[date, int]]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    _path(underlying).write_text(json.dumps(
        [{"from": start.isoformat(), "lot_size": size}
         for start, size in sorted(changes)], indent=2))
    _CACHE.pop(underlying.upper(), None)


# A revision that lasts one expiry and reverts is not a revision. It is a
# contract of an older vintage — typically the monthly, listed months earlier —
# sitting between weeklies that already carry the new size. Requiring a size to
# hold for two consecutive expiries filters those out.
#
# The cost of that filter is honest and small: for the week the stale monthly is
# the front contract, this calendar names the wrong size. That is a one-week
# error against the multi-year one it exists to fix, and correcting it would
# mean resolving *which* contract each session trades, which is `expiries.py`'s
# job and a different problem.
_MIN_CONSECUTIVE = 2


async def refresh_from_upstox(client: Any, underlying: str) -> list[tuple[date, int]]:
    """Rebuild the calendar by reading every expiry's contract listing.

    One request per expiry — about 100 for a full window, so seconds. The
    *most common* size in a listing is used rather than the maximum, because a
    listing can hold a few stale contracts and the mode is what the tradeable
    chain actually was.
    """
    from collections import Counter

    observed: list[tuple[date, int]] = []
    for expiry in await client.expiries(underlying):
        try:
            contracts = await client.contracts(underlying, expiry)
        except Exception:
            continue
        tally = Counter(c.lot_size for c in contracts if c.lot_size)
        if tally:
            observed.append((expiry, tally.most_common(1)[0][0]))

    changes: list[tuple[date, int]] = []
    previous: int | None = None
    for i, (expiry, size) in enumerate(observed):
        if size == previous:
            continue
        run = observed[i:i + _MIN_CONSECUTIVE]
        if len(run) == _MIN_CONSECUTIVE and any(s != size for _, s in run):
            continue                       # a one-expiry blip, not a revision
        changes.append((expiry, size))
        previous = size

    if changes:
        save(underlying, changes)
    return changes
