"""How far out of the money a strike is — derived from prices, never from labels.

This module exists because of an ambiguity that would have been invisible and
expensive. Dhan's expired-options endpoint is addressed by a label like `ATM+3`,
and its documentation does not say whether that means *three strikes above spot*
or *three strikes out of the money*. For a call those are the same thing. For a
put they are opposite, and picking wrong mirrors the entire put side of a
five-year dataset — every backtest still runs, every number still looks
reasonable, and every put strategy is silently testing the wrong wing.

The fix is to stop trusting the label. Dhan returns the absolute `strike` and
the `spot` on every bar, so moneyness can be computed from the data. Upstox
returns real contracts, so the same computation applies. One derivation, both
vendors, no assumption:

    positive moneyness = further OUT of the money, for calls and puts alike

so `moneyness=4` is a 4-strike-OTM call *or* a 4-strike-OTM put depending on
`opt_type`, and a short strangle is symmetric in the number rather than in the
sign. That is also the convention the backtest engine's LegSpec uses, which is
what lets vendor history and our own recording be queried as one series.

The vendor's own label is kept as a cross-check: `verify_label` compares the two
and reports disagreement, so if Dhan's convention is the opposite of what we
assumed, the ingest says so out loud instead of quietly producing a mirrored
book.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

# Strike spacing per underlying. Derived from the data when possible; these are
# the fallbacks for when a batch is too thin to infer from.
DEFAULT_STEP = {
    "NIFTY": 50.0,
    "BANKNIFTY": 100.0,
    "FINNIFTY": 50.0,
    "MIDCPNIFTY": 25.0,
    "SENSEX": 100.0,
    "BANKEX": 100.0,
}

# A strike this far from spot is not a real chain member for our purposes —
# usually a stale listing or a bad spot print.
#
# Measured against the real Upstox listings rather than guessed: the widest
# legitimate NIFTY chain is the long-dated monthly, 12000 to 34500 against a
# spot near 24000, which is **240 steps**. Weeklies reach about 64. The old
# 200 would have nulled the deepest strikes of every monthly — silently, since
# a null moneyness just makes a row invisible to the chain query rather than
# raising anything.
#
# 300 keeps the whole observed chain and still catches what this guard is for:
# a spot print wrong by an order of magnitude lands past 400.
MAX_ABS_MONEYNESS = 300


def strike_step(strikes: Iterable[float], underlying: str = "") -> float:
    """Most common gap between adjacent strikes.

    Inferred rather than hard-coded because the exchange changes spacing (NIFTY
    has been 50 for years, but MIDCPNIFTY is 25 and SENSEX moved), and a wrong
    step turns every moneyness into a wrong integer.
    """
    ordered = sorted({float(s) for s in strikes if s and float(s) > 0})
    if len(ordered) < 3:
        return DEFAULT_STEP.get(underlying.upper(), 50.0)
    gaps = Counter(round(b - a, 2) for a, b in zip(ordered, ordered[1:]) if b > a)
    if not gaps:
        return DEFAULT_STEP.get(underlying.upper(), 50.0)
    step, _ = gaps.most_common(1)[0]
    return step if step > 0 else DEFAULT_STEP.get(underlying.upper(), 50.0)


def atm_strike(spot: float, step: float) -> float:
    return round(spot / step) * step


def compute(strike: float, spot: float, opt_type: str,
            step: float) -> int | None:
    """Strikes out of the money. Positive is further OTM for both CE and PE."""
    if not strike or not spot or spot <= 0 or step <= 0:
        return None
    atm = atm_strike(spot, step)
    offset = (strike - atm) / step
    # A put is out of the money BELOW spot, so its sign flips. This one line is
    # the whole reason this module exists.
    value = offset if opt_type.upper() == "CE" else -offset
    rounded = int(round(value))
    return rounded if abs(rounded) <= MAX_ABS_MONEYNESS else None


def label_offset(label: str) -> int | None:
    """`ATM+3` -> 3, `ATM-7` -> -7, `ATM` -> 0."""
    if not label:
        return None
    text = label.upper().strip()
    if text == "ATM":
        return 0
    if text.startswith("ATM+"):
        try:
            return int(text[4:])
        except ValueError:
            return None
    if text.startswith("ATM-"):
        try:
            return -int(text[4:])
        except ValueError:
            return None
    return None


def verify_label(rows: list[dict[str, Any]], label: str,
                 opt_type: str) -> dict[str, Any]:
    """Compare the vendor's label against moneyness derived from its own prices.

    Returns a verdict rather than raising: on the call side agreement is
    expected, and on the put side a systematic sign flip tells us the vendor
    means "strikes above spot" where we mean "strikes out of the money". Either
    way the stored value is the derived one — this only reports what the vendor
    meant, so a convention change shows up as a log line and not as a silently
    mirrored dataset.
    """
    expected = label_offset(label)
    derived = [row["moneyness"] for row in rows if row.get("moneyness") is not None]
    if expected is None or not derived:
        return {"checked": 0, "verdict": "unknown"}

    agree = sum(1 for value in derived if value == expected)
    flipped = sum(1 for value in derived if value == -expected)
    total = len(derived)

    if expected == 0:
        verdict = "agrees" if agree / total > 0.8 else "unclear"
    elif agree / total > 0.8:
        verdict = "agrees"
    elif flipped / total > 0.8:
        verdict = "vendor-uses-absolute-offset"
    else:
        verdict = "unclear"

    return {
        "checked": total, "label": label, "opt_type": opt_type,
        "expected": expected, "verdict": verdict,
        "agree_pct": round(agree / total * 100, 1),
        "flipped_pct": round(flipped / total * 100, 1),
    }


def annotate(rows: list[dict[str, Any]], underlying: str,
             step: float | None = None) -> int:
    """Fill `moneyness` on rows that carry both `strike` and `spot`.

    Mutates in place and returns how many were resolved. Rows without a usable
    spot keep a null moneyness rather than a guessed one — a wrong integer here
    is worse than a missing one, because the backtest engine selects on it.

    `step` is **not** inferred from these rows, and that is deliberate. A batch
    handed to this function is one contract's bars, or one rolling moneyness
    level's — either way the distinct strikes in it are whatever the money did
    over the window, not the chain's spacing. Inferring from that sample gives
    a plausible wrong number (three strikes 100 apart reads as a 100-point
    chain) and every moneyness in the batch comes out wrong together.

    So: pass a `step` when you have a genuinely wide sample — a full contract
    listing, say — and otherwise take the underlying's known spacing.
    """
    if step is None or step <= 0:
        step = DEFAULT_STEP.get(underlying.upper(), 50.0)

    resolved = 0
    for row in rows:
        value = compute(
            float(row.get("strike") or 0), float(row.get("spot") or 0),
            row.get("opt_type") or "CE", step)
        row["moneyness"] = value
        if value is not None:
            resolved += 1
    return resolved
