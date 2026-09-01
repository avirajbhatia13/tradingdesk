"""Strategy analysis: what a proposed set of legs is worth, costs and risks.

This is the Sensibull builder view, computed locally off our own feed. Given a
basket of legs it returns the payoff at expiry, the payoff *today* (and at any
date in between), combined greeks, breakevens, probability of profit, and a
margin estimate.

Everything is priced on the FORWARD via Black-76 — see quant/forward.py for why
that is not a detail. Each leg carries its own IV, solved from its own live mid,
so the smile is preserved: a strategy builder that reprices every leg at one
flat ATM vol will misprice every spread you actually trade, because the whole
point of a spread is that the wings trade at different vols.

Two numbers here are estimates and are labelled as such in the output:

  margin  Real margin is SPAN + exposure, computed by the exchange from files
          we do not have. What we do instead is defensible rather than a made-up
          percentage: reprice the whole basket across a +/-3.5 sigma range and
          take the worst loss, then add 2% of naked short notional for exposure.
          For a defined-risk spread this converges on the true max loss, which
          is what the exchange charges. For naked shorts it lands in the right
          neighbourhood but will not match Kite's number to the rupee.

  pop     Probability of profit, under a lognormal terminal distribution at the
          book's own IV. It answers "how often does this land in the green",
          which is not the same as "is this a good trade" — a 90% POP credit
          spread that loses 10x when it loses is still a bad trade. It is shown
          next to max loss for exactly that reason.
"""

import math
from dataclasses import dataclass, field
from typing import Any

from app.quant import greeks as gk
from app.quant import payoff

CURVE_POINTS = 161

# Chart range. A flat percentage is wrong in both directions: +/-10% on a
# 3-DTE condor squeezes every breakeven into the middle few pixels, and the
# same 10% on a 60-day position cuts off the part that matters. So the range is
# driven by the two things that actually set the interesting width — how far
# the market can plausibly travel by expiry, and where the strikes are — with a
# small floor so a near-expiry position is not drawn on a knife edge.
RANGE_SIGMA = 4.0
STRIKE_MARGIN = 1.4
MIN_RANGE_PCT = 0.02

# Exchange margin shorthand. SPAN is a worst-case scenario loss scanned over a
# price range, plus a flat exposure percentage of notional.
#
# The floor is the part that matters and the part that is easy to miss: NSE
# scans index options over max(3 sigma, 5% of the underlying). On a quiet
# 9%-vol week 3.5 sigma is only ~2%, so the floor binds and does so by a factor
# of two and a half. Without it a naked NIFTY short estimates at ~57k against
# the ~1.1L Zerodha actually blocks.
MARGIN_SHOCK_SIGMA = 3.5
MIN_SCAN_PCT = 0.05
EXPOSURE_PCT = 0.02
TRADING_DAYS = 252.0


@dataclass
class Leg:
    """One contract in a proposed basket.

    `quantity` is signed and in UNITS (lots x lot_size), matching how Kite
    reports positions, so a short 2-lot NIFTY call is -150 and no part of this
    module needs to know what a lot is.
    """
    instrument_type: str              # 'CE' | 'PE' | 'FUT'
    strike: float
    quantity: float
    entry_price: float                # premium paid (+) or received per unit
    t_years: float
    iv: float | None = None
    tradingsymbol: str = ""
    token: int = 0
    lot_size: int = 0
    expiry: str = ""
    mark_price: float = 0.0           # live price now; defaults to entry
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_option(self) -> bool:
        return self.instrument_type in ("CE", "PE")


def _value(leg: Leg, forward: float, t_remaining: float, r: float,
           vol_multiplier: float = 1.0) -> float:
    """One unit of this leg, at `forward`, with `t_remaining` years left."""
    if not leg.is_option:
        return forward
    if t_remaining <= 0 or not leg.iv:
        intrinsic = (forward - leg.strike) if leg.instrument_type == "CE" else (leg.strike - forward)
        return max(intrinsic, 0.0)
    return gk.b76_price(forward, leg.strike, t_remaining,
                        leg.iv * vol_multiplier, leg.instrument_type, r)


def _book_value(legs: list[Leg], forward: float, elapsed: float, r: float,
                vol_multiplier: float = 1.0) -> float:
    total = 0.0
    for leg in legs:
        remaining = max(leg.t_years - elapsed, 0.0)
        total += _value(leg, forward, remaining, r, vol_multiplier) * leg.quantity
    return total


def net_premium(legs: list[Leg]) -> float:
    """Signed cash at entry. Negative = credit received, positive = debit paid."""
    return sum(leg.entry_price * leg.quantity for leg in legs)


def _sigma(legs: list[Leg]) -> float:
    """Annualised vol representative of the book, quantity-weighted."""
    weighted, total = 0.0, 0.0
    for leg in legs:
        if leg.iv and leg.quantity:
            weighted += leg.iv * abs(leg.quantity)
            total += abs(leg.quantity)
    return (weighted / total) if total else 0.15


def _horizon(legs: list[Leg]) -> float:
    """Nearest expiry in the basket — where the payoff curve is drawn."""
    dated = [leg.t_years for leg in legs if leg.is_option]
    return min(dated) if dated else 0.0


def _curve(legs: list[Leg], forward: float, r: float, elapsed: float,
           low: float, high: float) -> list[dict[str, float]]:
    cost = net_premium(legs)
    step = (high - low) / (CURVE_POINTS - 1)
    out = []
    for i in range(CURVE_POINTS):
        s = low + i * step
        out.append({"spot": round(s, 2),
                    "pnl": round(_book_value(legs, s, elapsed, r) - cost, 2)})
    return out


def _breakevens(curve: list[dict[str, float]]) -> list[float]:
    out = []
    for i in range(1, len(curve)):
        prev, curr = curve[i - 1]["pnl"], curve[i]["pnl"]
        if prev == 0.0:
            out.append(curve[i - 1]["spot"])
        elif (prev < 0) != (curr < 0):
            fraction = abs(prev) / (abs(prev) + abs(curr))
            span = curve[i]["spot"] - curve[i - 1]["spot"]
            out.append(round(curve[i - 1]["spot"] + fraction * span, 2))
    return out


def _bounds(legs: list[Leg], curve: list[dict[str, float]], r: float,
            horizon: float) -> dict[str, Any]:
    """True max profit and max loss — the rule itself is `payoff.bounds`.

    The only part that belongs here is the zero bound, because this module
    prices on the forward via Black-76 and the payoff module prices on spot.
    At expiry every leg is intrinsic, so valuing the book at a forward of
    effectively zero is exact rather than an extrapolation.
    """
    at_zero = round(_book_value(legs, 1e-6, horizon, r) - net_premium(legs), 2)
    return payoff.bounds(legs, curve, at_zero)


def _pop(curve: list[dict[str, float]], forward: float, sigma: float,
         t_years: float) -> float | None:
    """Probability the terminal price lands where the curve is positive.

    Integrates the lognormal density over the profitable stretches of the
    expiry curve, which handles butterflies and condors — anything with more
    than one profitable region — without special-casing them.
    """
    if t_years <= 0 or sigma <= 0 or forward <= 0 or len(curve) < 2:
        return None
    vol = sigma * math.sqrt(t_years)
    if vol <= 0:
        return None

    def cdf(price: float) -> float:
        if price <= 0:
            return 0.0
        # Terminal distribution under the forward measure: median at F.
        z = (math.log(price / forward) + 0.5 * vol * vol) / vol
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    probability = 0.0
    for i in range(1, len(curve)):
        if curve[i]["pnl"] > 0 and curve[i - 1]["pnl"] > 0:
            probability += cdf(curve[i]["spot"]) - cdf(curve[i - 1]["spot"])
    # The tails beyond the drawn range, when the curve is still profitable there.
    if curve[0]["pnl"] > 0:
        probability += cdf(curve[0]["spot"])
    if curve[-1]["pnl"] > 0:
        probability += 1.0 - cdf(curve[-1]["spot"])
    return round(max(0.0, min(1.0, probability)) * 100, 1)


def _margin(legs: list[Leg], forward: float, r: float, sigma: float,
            bounds: dict[str, Any]) -> dict[str, Any]:
    """SPAN-shaped estimate: worst scenario loss, plus exposure on naked shorts.

    Two rules do most of the work, and both mirror how the exchange actually
    charges rather than any formula:

    - Exposure is charged on the UNCOVERED short quantity only. A short call
      sitting under a long call is hedged, and treating the whole short leg as
      naked overstates margin by multiples on every spread you would trade.
    - When the loss is bounded, margin is capped at the max loss. You cannot be
      asked to post more than the position can ever lose.
    - **Premium paid is capital too.** A scenario margin can be legitimately
      zero and the position still cost real money to put on: a 1x3 ratio
      backspread is long gamma, so every price shock SPAN tests is a *gain*,
      and it reported a requirement of ₹0 while costing ₹16,256 a day in net
      debit. Whatever the exchange blocks, the cash for the long legs leaves
      the account, so the figure reported is the larger of the two.
    """
    if not legs or forward <= 0:
        return {"total": 0.0, "span": 0.0, "exposure": 0.0, "estimate": True}

    # A long-only basket risks its premium and nothing more.
    if all(leg.quantity > 0 for leg in legs):
        return {"total": round(max(net_premium(legs), 0.0), 2), "span": 0.0,
                "exposure": 0.0, "estimate": True, "note": "debit paid"}

    daily = sigma / math.sqrt(TRADING_DAYS)
    scan = max(MARGIN_SHOCK_SIGMA * daily, MIN_SCAN_PCT)
    current = _book_value(legs, forward, 0.0, r)

    worst = 0.0
    for step in range(-7, 8):
        move = scan * step / 7.0
        shocked = forward * (1.0 + move)
        # Vol up on the way down, as in risk.py — a crash that left IV flat
        # would understate what the exchange asks for.
        vol_multiplier = 1.0 + (0.15 * abs(move) / daily if move < 0 and daily else 0.0)
        # SPAN prices the position as it stands now, not at expiry.
        pnl = _book_value(legs, shocked, 0.0, r, vol_multiplier) - current
        worst = min(worst, pnl)
    span = abs(round(worst, 2))

    # The wing slopes say what is left unhedged past the outermost strike.
    uncovered = max(0.0, -bounds["up_slope"]) + max(0.0, bounds["down_slope"])
    exposure = round(uncovered * forward * EXPOSURE_PCT, 2)

    # The larger of "what gets blocked" and "what gets paid", not their sum:
    # on a debit spread the max-loss cap already collapses margin to roughly
    # the debit, and adding the two would charge the same rupees twice.
    debit = max(net_premium(legs), 0.0)
    total = max(span + exposure, debit)
    capped = False
    if not bounds["unlimited_loss"] and bounds["max_loss"] is not None:
        ceiling = abs(bounds["max_loss"])
        if total > ceiling:
            total, capped = ceiling, True

    return {"total": round(total, 2), "span": span, "exposure": exposure,
            "debit": round(debit, 2), "estimate": True,
            "capped_at_max_loss": capped}


def analyse(legs: list[Leg], forward: float, r: float,
            days_forward: int = 0) -> dict[str, Any]:
    """Full analysis of a basket. `days_forward` moves the T+n curve."""
    if not legs or forward <= 0:
        return {}

    sigma = _sigma(legs)
    horizon = _horizon(legs)

    # Range: whichever is wider, a 4-sigma move by expiry or the strike spread
    # with room past the outermost leg.
    strikes = [leg.strike for leg in legs if leg.strike > 0]
    sigma_range = sigma * math.sqrt(max(horizon, 1 / 365)) * RANGE_SIGMA
    strike_range = 0.0
    if strikes:
        furthest = max(abs(strike - forward) for strike in strikes)
        strike_range = (furthest / forward) * STRIKE_MARGIN
    span = max(sigma_range, strike_range, MIN_RANGE_PCT)
    low, high = forward * (1 - span), forward * (1 + span)

    expiry_curve = _curve(legs, forward, r, horizon, low, high)
    elapsed = min(max(days_forward, 0) / 365.0, horizon)
    today_curve = _curve(legs, forward, r, elapsed, low, high)

    bounds = _bounds(legs, expiry_curve, r, horizon)
    cost = net_premium(legs)

    combined = gk.empty()
    per_leg: dict[int, dict[str, float]] = {}
    for index, leg in enumerate(legs):
        if leg.is_option and leg.iv and leg.t_years > 0:
            unit = gk.b76_greeks(forward, leg.strike, leg.t_years, leg.iv,
                                 leg.instrument_type, r)
            scaled = gk.scale(unit, leg.quantity)
            per_leg[index] = {"unit_delta": unit["delta"], **scaled}
            gk.add(combined, scaled)
        elif not leg.is_option:
            per_leg[index] = {"unit_delta": 1.0, "delta": leg.quantity,
                              "gamma": 0.0, "theta": 0.0, "vega": 0.0}
            gk.add(combined, {"delta": leg.quantity})

    # Current mark-to-market, for a basket that is already live.
    marked = sum((leg.mark_price - leg.entry_price) * leg.quantity for leg in legs)

    margin = _margin(legs, forward, r, sigma, bounds)
    max_profit, max_loss = bounds["max_profit"], bounds["max_loss"]

    return {
        "legs": [
            {
                "instrument_type": leg.instrument_type, "strike": leg.strike,
                "quantity": leg.quantity, "entry_price": leg.entry_price,
                "mark_price": leg.mark_price, "iv": round(leg.iv * 100, 2) if leg.iv else None,
                "tradingsymbol": leg.tradingsymbol, "token": leg.token,
                "lot_size": leg.lot_size, "expiry": leg.expiry,
                "lots": round(leg.quantity / leg.lot_size, 2) if leg.lot_size else None,
                "value": round(leg.entry_price * leg.quantity, 2),
                # Per-unit delta, which is the number read off a chain. The
                # position delta is in `greeks` and is already quantity-scaled.
                "delta": round(per_leg.get(index, {}).get("unit_delta", 0.0), 4),
                "greeks": {k: round(v, 4)
                           for k, v in per_leg.get(index, {}).items() if k != "unit_delta"},
            }
            for index, leg in enumerate(legs)
        ],
        "forward": round(forward, 2),
        "net_premium": round(cost, 2),
        # Sign convention stated plainly because it is the one thing everyone
        # gets backwards: a credit strategy has negative net_premium.
        "is_credit": cost < 0,
        "credit_received": round(-cost, 2) if cost < 0 else 0.0,
        "debit_paid": round(cost, 2) if cost > 0 else 0.0,
        "marked_pnl": round(marked, 2),
        "max_profit": max_profit,
        "max_loss": max_loss,
        "unlimited_profit": bounds["unlimited_profit"],
        "unlimited_loss": bounds["unlimited_loss"],
        "loss_at_zero": bounds["loss_at_zero"],
        "breakevens": _breakevens(expiry_curve),
        "pop": _pop(expiry_curve, forward, sigma, horizon),
        "greeks": {k: round(v, 4) for k, v in combined.items()},
        "margin": margin,
        "return_on_margin": (
            round(-cost / margin["total"] * 100, 2)
            if cost < 0 and margin["total"] > 0 else None
        ),
        "risk_reward": (
            round(abs(max_profit / max_loss), 2)
            if max_profit is not None and max_loss is not None and max_loss < 0
            else None
        ),
        "iv": round(sigma * 100, 2),
        "days_to_expiry": round(horizon * 365, 1),
        "days_forward": days_forward,
        "curve": expiry_curve,
        "curve_now": today_curve,
        "range": {"low": round(low, 2), "high": round(high, 2)},
    }
