"""Expiry payoff curves for a combined book — the Sensibull view, computed locally.

Built for the real case rather than the textbook one: the legs handed in here
are usually spread across several expiries and several accounts. The curve is
drawn at the NEAREST expiry in the book, and legs that outlive that date are
marked at their Black-Scholes value on that date using the IV they trade at
right now. That is the standard convention and it is an assumption worth
stating out loud — a calendar spread's curve is only as good as the assumption
that the back month's IV holds.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.quant.greeks import bs_price

CURVE_POINTS = 121
# Wide enough that a short strangle's loss legs are visible on both sides
# without compressing the interesting middle of the curve.
DEFAULT_RANGE_PCT = 0.12


@dataclass
class Leg:
    instrument_type: str          # 'CE' | 'PE' | 'FUT' | 'EQ'
    strike: float
    quantity: float               # signed, in units (negative = short)
    average_price: float
    t_years: float = 0.0          # time from now to this leg's own expiry
    iv: float | None = None


def _leg_value(leg: Leg, spot: float, t_remaining: float, r: float) -> float:
    """What one unit of this leg is worth at `spot`, `t_remaining` years out."""
    if leg.instrument_type in ("FUT", "EQ"):
        return spot
    if t_remaining <= 0 or not leg.iv:
        intrinsic = (spot - leg.strike) if leg.instrument_type == "CE" else (leg.strike - spot)
        return max(intrinsic, 0.0)
    return bs_price(spot, leg.strike, t_remaining, leg.iv, leg.instrument_type, r)


def bounds(legs: Sequence[Any], curve: list[dict[str, float]],
           at_zero: float) -> dict[str, Any]:
    """True max profit and max loss, including outside the drawn range.

    The curve only knows about the window it was drawn over, so reading extremes
    off it alone would quietly cap a naked short call's loss at whatever the
    chart happened to show. Instead the wings are settled analytically from the
    payoff slope beyond the last strike:

      above every strike each call is worth (S - K), so the slope is the net
      call quantity plus any futures;
      below every strike each put is worth (K - S), so the slope is minus the
      net put quantity plus futures.

    Upside is genuinely unbounded when that slope is negative. Downside is not:
    the underlying stops at zero, so a short put's worst case is large but
    finite and gets reported as a number rather than as "unlimited".

    `legs` needs only `instrument_type`, `strike` and `quantity`, which both
    this module's `Leg` and the strategy builder's carry. `at_zero` is the
    book's P&L with the underlying at zero, which each caller computes with its
    own pricing convention — spot and Black-Scholes here, the forward and
    Black-76 in the builder. The rule lives here rather than in both callers
    because two copies of it would eventually disagree about whether a position
    can lose everything, and that is not a disagreement anyone would notice
    until it mattered.
    """
    futures = sum(leg.quantity for leg in legs
                  if leg.instrument_type not in ("CE", "PE"))
    up_slope = sum(leg.quantity for leg in legs
                   if leg.instrument_type == "CE") + futures
    down_slope = -sum(leg.quantity for leg in legs
                      if leg.instrument_type == "PE") + futures

    pnls = [point["pnl"] for point in curve]
    inside_max = max(pnls) if pnls else at_zero
    inside_min = min(pnls) if pnls else at_zero

    unlimited_profit = up_slope > 0
    unlimited_loss = up_slope < 0

    strikes = [leg.strike for leg in legs
               if leg.instrument_type in ("CE", "PE") and leg.strike > 0]

    return {
        "max_profit": None if unlimited_profit else round(max(inside_max, at_zero), 2),
        "max_loss": None if unlimited_loss else round(min(inside_min, at_zero), 2),
        "unlimited_profit": unlimited_profit,
        "unlimited_loss": unlimited_loss,
        "loss_at_zero": round(at_zero, 2),
        "up_slope": up_slope,
        "down_slope": down_slope,
        # Past the outermost strike every option is intrinsic, so the payoff is
        # a straight line from there out and the slopes above describe it
        # exactly. Naming that level is what makes "unbounded" actionable
        # rather than merely alarming.
        "linear_above": round(max(strikes), 2) if strikes else None,
        "linear_below": round(min(strikes), 2) if strikes else None,
    }


def build(legs: list[Leg], spot: float, r: float,
          range_pct: float = DEFAULT_RANGE_PCT) -> dict[str, Any]:
    """Return the payoff curve plus the numbers a seller actually watches."""
    if not legs or spot <= 0:
        return {}

    # The curve is drawn at the first expiry in the book; anything longer-dated
    # still has time value left on that date.
    dated = [leg.t_years for leg in legs if leg.instrument_type in ("CE", "PE")]
    horizon = min(dated) if dated else 0.0

    strikes = [leg.strike for leg in legs if leg.strike > 0]
    low = min([spot * (1 - range_pct)] + strikes) * 0.98
    high = max([spot * (1 + range_pct)] + strikes) * 1.02
    step = (high - low) / (CURVE_POINTS - 1)

    curve: list[dict[str, float]] = []
    for i in range(CURVE_POINTS):
        s = low + i * step
        pnl = 0.0
        for leg in legs:
            remaining = max(leg.t_years - horizon, 0.0)
            value = _leg_value(leg, s, remaining, r)
            pnl += (value - leg.average_price) * leg.quantity
        curve.append({"spot": round(s, 2), "pnl": round(pnl, 2)})

    pnls = [point["pnl"] for point in curve]
    breakevens = []
    for i in range(1, len(curve)):
        prev, curr = pnls[i - 1], pnls[i]
        if prev == 0.0:
            breakevens.append(curve[i - 1]["spot"])
        elif (prev < 0) != (curr < 0):
            # Linear interpolation is exact between two strikes, where the
            # payoff really is a straight line.
            fraction = abs(prev) / (abs(prev) + abs(curr))
            span = curve[i]["spot"] - curve[i - 1]["spot"]
            breakevens.append(round(curve[i - 1]["spot"] + fraction * span, 2))

    # At expiry every leg is intrinsic, so valuing the book at a spot of
    # effectively zero is exact rather than an extrapolation.
    cost = sum(leg.average_price * leg.quantity for leg in legs)
    at_zero = sum(
        _leg_value(leg, 1e-6, max(leg.t_years - horizon, 0.0), r) * leg.quantity
        for leg in legs) - cost

    return {
        "curve": curve,
        "spot": round(spot, 2),
        "horizon_years": round(horizon, 6),
        # `max_profit` and `max_loss` are the extremes of the DRAWN curve, which
        # is what the chart's own labels should read. The true extremes, which
        # run past the edge of the chart and can be unbounded, are in `bounds`.
        "max_profit": round(max(pnls), 2),
        "max_loss": round(min(pnls), 2),
        "bounds": bounds(legs, curve, at_zero),
        "breakevens": breakevens,
        # Distance to the nearest point where the book turns negative — the
        # single most useful number on a short-premium position.
        "nearest_breakeven_pct": _nearest_breakeven_pct(breakevens, spot),
        "range": {"low": round(low, 2), "high": round(high, 2)},
    }


def _nearest_breakeven_pct(breakevens: list[float], spot: float) -> float | None:
    if not breakevens or spot <= 0:
        return None
    return round(min(abs(be - spot) for be in breakevens) / spot * 100, 2)
