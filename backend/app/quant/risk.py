"""Scenario risk for a short-premium book.

A parametric VaR (delta x sigma x value) is the wrong tool here. A short
strangle is locally delta-neutral and looks riskless to a linear model, while
its actual danger is entirely in the convexity — the loss from a 2% gap is far
more than twice the loss from a 1% gap. So this does a **full revaluation**:
every leg is re-priced at the shocked level, one day nearer expiry, and the loss
is read off directly.

Two shocks are applied together, because they arrive together in real markets:

  spot   +/- k standard deviations, where sigma is derived from the book's own
         implied vol rather than assumed — the market's estimate of its own
         one-day move is the honest input
  vol    implied vol is bumped UP on down-moves only. Equity index vol is
         strongly negatively correlated with spot; a crash that left IV
         unchanged would understate the loss on short premium badly

## Three things this got wrong until 26 Aug 2026

All three ran in the same direction — understating the loss — and all three were
invisible, because a scenario model has nothing to check itself against.

**It stopped at 2 sigma.** The worst case it would ever report was an ordinary
bad day. Work the arithmetic on a real one: 4 June 2024, NIFTY fell about 5.9%
intraday with ATM IV around 25%, so a daily sigma of 25/sqrt(252) = 1.57% and
the move was **3.8 sigma**. A model whose ceiling is 2 sigma cannot see the day
that matters, and a seller sizing off it is sized for the wrong market. The
ladder now runs to 4 sigma and `tail` reports it.

**Legs with no solvable IV were valued at intrinsic — that is, at zero for
anything out of the money.** They therefore contributed *nothing* to any
scenario. These are not obscure contracts: an option under five paise fails the
IV solve by construction (see `greeks.MIN_PRICE`), and a seller's far wings and
the last session of every expiry live exactly there. So the cheapest strikes —
the ones that go from 0.35 to 310 on a gap, and the bought wings that are the
only thing capping the loss — were the ones the model could not see. They now
inherit the book's own IV, which is a model price rather than a market one, but
a modelled wing beats a wing that is not there.

**It priced a forward IV in a spot model.** The IV handed in is solved on the
forward (Black-76, see `quant/forward.py`), and this used `bs_price` at spot.
Worth being exact about the size, because it is smaller than it sounds: Black-76
with F = S*e^(rT) is *algebraically identical* to Black-Scholes at S, so the
error was never the carry — only the gap between the true forward and S*e^(rT),
which is the dividend yield. On NIFTY that is ~25 points at 45 DTE and ~3 points
at 3 DTE. Real, worth removing since it costs nothing, but not the headline.

## What this is and is not

A stress test with a stated scenario, not a statistical VaR with a confidence
interval. It says "a 4-sigma day costs you this much", which is the question an
option seller is actually asking. It attaches no probability to that day and you
should not either — sigma comes from a one-day implied move, and the empirical
tail of Indian index returns is far fatter than the lognormal that implies.
"""

import math
from dataclasses import dataclass
from typing import Any

from app.quant.greeks import b76_price

TRADING_DAYS = 252.0
ONE_DAY = 1.0 / 365.0

# Sigma multiples to stress, mildest first.
#
# 1 is an ordinary day and 2 a bad one; those two answer "how is this week
# going". 3 and 4 are the ones that decide whether the account survives, and
# they are here because the ladder used to stop at 2 and a seller cannot size a
# position off a number that cannot see its own ruin.
SHOCKS = (1.0, 2.0, 3.0, 4.0)

# The everyday figure — a bad-but-normal session.
HEADLINE_SHOCK = 2.0
# The one that should decide position size.
TAIL_SHOCK = 4.0

# Relative IV bump applied on down-moves, scaled by the size of the move.
# A 2-sigma fall lifting IV ~30% is conservative next to real Indian index
# crashes, where VIX has doubled intraday.
VOL_BUMP_PER_SIGMA = 0.15

# Used when not one leg in the book has a solvable IV, so there is nothing to
# average. Deliberately not silent: `summarise` reports `iv_source` so a number
# resting on this assumption is distinguishable from one resting on the market's.
# It is on the low side for BANKNIFTY, which is the safe direction for an
# assumption you want people to notice and replace.
ASSUMED_ANNUAL_IV = 0.15


@dataclass
class RiskLeg:
    instrument_type: str      # 'CE' | 'PE' | 'FUT' | 'EQ'
    strike: float
    quantity: float           # signed, in units
    t_years: float
    iv: float | None
    # The last traded price. Not an input to any scenario — the shocked and base
    # valuations are both model prices so their difference is the model's own
    # response, uncontaminated by any model-market gap. It is used to REPORT
    # that gap (`mark_gap` below), which is the number that says how much to
    # trust the rest of this.
    last_price: float
    # The forward this leg's expiry actually trades against, from put-call
    # parity or the matching future. None falls back to spot*e^(rT), which
    # reproduces Black-Scholes at spot exactly.
    forward: float | None = None


def _forward_of(leg: RiskLeg, spot: float, base_spot: float, r: float) -> float:
    """This leg's forward at `spot`, preserving whatever basis it carries.

    `base_spot` is the *unshocked* level the stored forward was measured
    against, and it has to be passed rather than inferred: the whole job here is
    to move the forward with a shocked spot, so using the shocked value as its
    own denominator would return the forward unchanged and quietly undo the
    shock on every leg.

    The mapping is to keep the basis *ratio* — a 4% fall in the index moves its
    forward 4% too. The one-day roll-down of the basis itself is ignored as
    second-order: it is a fraction of a basis that is already ~0.1% of spot.

    With no stored forward this falls back to spot*e^(rT), and Black-76 on that
    forward is algebraically identical to Black-Scholes at spot — so a leg with
    no forward prices exactly as it did before.
    """
    if leg.forward and leg.forward > 0 and base_spot > 0:
        return spot * (leg.forward / base_spot)
    return spot * math.exp(r * max(leg.t_years, 0.0))


def book_iv(legs: list[RiskLeg]) -> tuple[float, str]:
    """The book's own annualised IV, and where it came from.

    Quantity-weighted across the legs that have one, so the strikes carrying the
    risk set the number. The source matters as much as the value: a figure
    derived from the market is worth acting on and a fallback constant is worth
    replacing, and a caller that cannot tell them apart will do neither.
    """
    weighted, total = 0.0, 0.0
    for leg in legs:
        if leg.iv and leg.quantity:
            weight = abs(leg.quantity)
            weighted += leg.iv * weight
            total += weight
    if total:
        return weighted / total, "market"
    return ASSUMED_ANNUAL_IV, "assumed"


def _revalue(leg: RiskLeg, spot: float, vol_multiplier: float, r: float,
             fallback_iv: float, base_spot: float,
             t_years: float | None = None) -> float:
    """One unit's value at `spot`, optionally at a different time to expiry.

    A leg whose own IV could not be solved is priced on the book's IV rather
    than collapsed to intrinsic. That substitution is the difference between a
    far wing being modelled and being absent, and absent is the failure that
    made a hedged book look naked and a naked one look hedged.
    """
    if leg.instrument_type in ("FUT", "EQ"):
        return spot

    remaining = leg.t_years if t_years is None else t_years
    if remaining <= 0:
        intrinsic = ((spot - leg.strike) if leg.instrument_type == "CE"
                     else (leg.strike - spot))
        return max(intrinsic, 0.0)

    sigma = (leg.iv or fallback_iv) * vol_multiplier
    forward = _forward_of(leg, spot, base_spot, r)
    return b76_price(forward, leg.strike, remaining, sigma,
                     leg.instrument_type, r)


def daily_sigma(legs: list[RiskLeg]) -> float:
    """One-day move implied by the book's own options, as a fraction of spot."""
    annual, _ = book_iv(legs)
    return annual / math.sqrt(TRADING_DAYS)


def mark_gap(legs: list[RiskLeg], spot: float, r: float) -> float | None:
    """Model value of the book minus its traded value, in rupees.

    The scenarios are differences between two model prices, so a model that
    disagrees with the market cancels out of them — which is what makes them
    clean, and also what makes the disagreement invisible. This surfaces it.
    A gap that is a large share of the book means the IVs, the forward or the
    marks are wrong, and every number here inherits that.

    None when no leg carries a usable traded price.
    """
    fallback, _ = book_iv(legs)
    model = market = 0.0
    seen = False
    for leg in legs:
        if not leg.quantity or leg.last_price is None or leg.last_price <= 0:
            continue
        seen = True
        model += _revalue(leg, spot, 1.0, r, fallback, spot) * leg.quantity
        market += leg.last_price * leg.quantity
    return round(model - market, 2) if seen else None


def scenarios(legs: list[RiskLeg], spot: float, r: float) -> list[dict[str, Any]]:
    """P&L under each spot/vol shock, worst first.

    Every row is the total change from now until tomorrow: the gap, **net of the
    theta collected for holding through it.** That is what the position actually
    does to the account overnight, and it is why the base valuation is taken at
    today's time to expiry while the shocked one is a day nearer. Pricing both a
    day out — as this did until 26 Aug 2026 — cancelled the decay entirely and
    reported a pure shock response under a docstring promising otherwise.

    `decay` on each row separates the two, because netting them hides which is
    doing the work: a scenario that is only survivable because of one day's
    theta is a different position from one that is genuinely bounded.
    """
    if not legs or spot <= 0:
        return []

    sigma = daily_sigma(legs)
    fallback, _ = book_iv(legs)

    # Today, at today's clock. The shocked valuations run one day on.
    current = sum(_revalue(leg, spot, 1.0, r, fallback, spot) * leg.quantity
                  for leg in legs)

    def tomorrow(at_spot: float, vol_multiplier: float) -> float:
        return sum(
            _revalue(leg, at_spot, vol_multiplier, r, fallback, spot,
                     t_years=max(leg.t_years - ONE_DAY, ONE_DAY / 24))
            * leg.quantity
            for leg in legs)

    # What one day costs with nothing moving — the theta line, isolated.
    flat = tomorrow(spot, 1.0)

    out: list[dict[str, Any]] = []
    for shock in SHOCKS:
        for direction in (-1.0, 1.0):
            move = direction * shock * sigma
            shocked_spot = spot * (1.0 + move)
            # Vol rises when the market falls, and is left alone on rallies.
            vol_multiplier = 1.0 + (VOL_BUMP_PER_SIGMA * shock
                                    if direction < 0 else 0.0)
            value = tomorrow(shocked_spot, vol_multiplier)
            out.append({
                "label": f"{move * 100:+.1f}%",
                "sigma": shock * direction,
                "spot": round(shocked_spot, 2),
                "move_pct": round(move * 100, 2),
                "vol_multiplier": round(vol_multiplier, 2),
                "pnl": round(value - current, 2),
                # The same scenario with the day's decay stripped out, and the
                # decay itself. `pnl` is what happens to the account; `shock`
                # is what the market did to you.
                "shock": round(value - flat, 2),
                "decay": round(flat - current, 2),
            })
    return sorted(out, key=lambda s: s["pnl"])


def _at(rows: list[dict[str, Any]], shock: float) -> dict[str, Any] | None:
    """Worst outcome at a given sigma multiple, either direction."""
    matching = [s for s in rows if abs(s["sigma"]) == shock]
    return min(matching, key=lambda s: s["pnl"]) if matching else None


def summarise(legs: list[RiskLeg], spot: float, r: float) -> dict[str, Any]:
    """Headline risk for one underlying.

    Two numbers rather than one, deliberately. `var` is the ordinary bad day and
    is what you watch; `tail` is the day that decides whether the account
    survives and is what should set position size. Reporting only the first is
    how a book ends up four times too large — see the module docstring.
    """
    rows = scenarios(legs, spot, r)
    if not rows:
        return {}

    headline = _at(rows, HEADLINE_SHOCK) or rows[0]
    tail = _at(rows, TAIL_SHOCK) or rows[0]
    annual, iv_source = book_iv(legs)
    modelled = sum(1 for leg in legs
                   if leg.quantity and not leg.iv
                   and leg.instrument_type in ("CE", "PE"))

    return {
        "daily_sigma_pct": round(daily_sigma(legs) * 100, 2),
        "book_iv_pct": round(annual * 100, 2),
        # 'market' means the sigma came from the book's own options. 'assumed'
        # means nothing had a solvable IV and this rests on a constant.
        "iv_source": iv_source,
        # Legs priced on the book's IV because their own would not solve. Not a
        # fault — it is how the far wings get modelled at all — but a scenario
        # carried by several of them is a weaker claim than one that is not.
        "modelled_legs": modelled,
        # Model value minus traded value. A large share of the book means the
        # inputs are off and everything here inherits it.
        "mark_gap": mark_gap(legs, spot, r),
        "worst": rows[0],
        "var": round(min(0.0, headline["pnl"]), 2),   # a loss, so never positive
        "var_scenario": headline["label"],
        "tail": round(min(0.0, tail["pnl"]), 2),
        "tail_scenario": tail["label"],
        "tail_sigma": TAIL_SHOCK,
        "scenarios": rows,
    }


# ---------------------------------------------------------------------------
# Margin pressure — what a gap does to the OTHER side of the equation
# ---------------------------------------------------------------------------
#
# A losing position does not usually end because the loss became unbearable. It
# ends because the margin did: the exchange asks for more at exactly the moment
# the account has less, the broker squares off at whatever the screen says, and
# a position that would have recovered is closed at the worst price of the day.
#
# So the loss is only half the question. A 2% gap moves both sides at once:
#
#   available  falls by the mark-to-market loss
#   required   RISES, because the position is now nearer the strikes and the
#              exchange's scan finds a worse outcome from the new level
#
# Kite's basket API gives the real current requirement and there is no way to
# ask it what that becomes at a hypothetical spot. So the model is used only
# for the RATIO — how much the requirement grows — and that ratio is applied to
# the exchange's own number. Anchoring to the real figure and modelling only the
# change is a much smaller claim than modelling the level.

# The move a trader actually thinks in. Sigma multiples are the right unit for a
# risk ladder and the wrong one for a decision: "2%" is a thing you have seen
# happen, "4 sigma" is a thing you have to convert first.
GAP_PCT = 0.02


def scan_loss(legs: list[RiskLeg], spot: float, r: float) -> float:
    """Worst loss over the exchange's scan band, evaluated from `spot`.

    The shape of a SPAN requirement: scan the position across a price band and
    block the worst outcome. Deliberately imports its band from
    `quant/strategy.py` rather than restating it — that module worked out that
    NSE's floor of 5% binds on a quiet week and that missing it under-estimates
    a naked NIFTY short by a factor of two, and two copies of that finding would
    eventually disagree.

    Returned as a positive number of rupees. Zero for a book that cannot lose
    inside the band.
    """
    from app.quant.strategy import MARGIN_SHOCK_SIGMA, MIN_SCAN_PCT

    if not legs or spot <= 0:
        return 0.0

    fallback, _ = book_iv(legs)
    daily = daily_sigma(legs)
    band = max(MARGIN_SHOCK_SIGMA * daily, MIN_SCAN_PCT)
    base = sum(_revalue(leg, spot, 1.0, r, fallback, spot) * leg.quantity
               for leg in legs)

    worst = 0.0
    for step in range(-7, 8):
        move = band * step / 7.0
        vol_multiplier = 1.0 + (VOL_BUMP_PER_SIGMA * abs(move) / daily
                                if move < 0 and daily else 0.0)
        value = sum(
            _revalue(leg, spot * (1.0 + move), vol_multiplier, r, fallback, spot)
            * leg.quantity for leg in legs)
        worst = min(worst, value - base)
    return abs(round(worst, 2))


def gap_pressure(legs: list[RiskLeg], spot: float, r: float,
                 move_pct: float = GAP_PCT) -> dict[str, Any]:
    """Both sides of the margin equation after a gap of `move_pct`.

    `pnl` is the mark-to-market hit, worse direction — it comes straight out of
    available margin. `requirement_multiple` is how much more the exchange would
    block, as a ratio to apply to its own current figure.

    The multiple is floored at 1.0 on purpose. A gap that happens to reduce the
    scanned requirement is not something to bank on when the question being
    asked is "can I survive this" — and the direction that reduces it is the one
    that is not hurting you anyway.
    """
    if not legs or spot <= 0 or move_pct <= 0:
        return {"pnl": 0.0, "requirement_multiple": 1.0, "move_pct": move_pct}

    fallback, _ = book_iv(legs)
    base = sum(_revalue(leg, spot, 1.0, r, fallback, spot) * leg.quantity
               for leg in legs)
    here = scan_loss(legs, spot, r)

    worst_pnl = 0.0
    worst_multiple = 1.0
    for direction in (-1.0, 1.0):
        gapped = spot * (1.0 + direction * move_pct)
        # Vol rises on the way down, same convention as the ladder above.
        vol_multiplier = 1.0 + (VOL_BUMP_PER_SIGMA * (move_pct / daily_sigma(legs))
                                if direction < 0 and daily_sigma(legs) else 0.0)
        value = sum(
            _revalue(leg, gapped, vol_multiplier, r, fallback, spot) * leg.quantity
            for leg in legs)
        pnl = value - base
        if pnl < worst_pnl:
            worst_pnl = pnl
        there = scan_loss(legs, gapped, r)
        if here > 0:
            worst_multiple = max(worst_multiple, there / here)

    return {
        "pnl": round(worst_pnl, 2),
        "requirement_multiple": round(worst_multiple, 3),
        "move_pct": move_pct,
        "scan_here": here,
    }
