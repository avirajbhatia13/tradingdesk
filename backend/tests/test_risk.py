"""Scenario risk on a short-premium book — the number that sets position size.

This file did not exist until 26 Aug 2026, which is why the three faults it now
pins survived: a scenario model has nothing to check itself against. It produces
a plausible rupee figure whatever it does, and every one of these errors ran in
the same direction, making the book look safer than it was.

The audit's pre-mortem scenario 3 is the shape of it. Six clean months of
forward tests, a decision to scale one lot to four sized off a VaR tile reading
−₹1.9 L, and a 4-sigma morning that costs ₹6.8 L — of which ₹4.1 L sits in a
family member's account. The tile was not lying about its own arithmetic. It was
answering a question two sigma narrower than the one being asked, with the
bought wings priced at zero.
"""

import math

import pytest

from app.quant import risk as rk

R = 0.065
LOT = 75
SPOT = 25000.0
WEEK = 7 / 365


def short_call(strike, iv=0.12, lots=1, t=WEEK, price=100.0, forward=None):
    return rk.RiskLeg(instrument_type="CE", strike=strike, quantity=-LOT * lots,
                      t_years=t, iv=iv, last_price=price, forward=forward)


def short_put(strike, iv=0.12, lots=1, t=WEEK, price=100.0, forward=None):
    return rk.RiskLeg(instrument_type="PE", strike=strike, quantity=-LOT * lots,
                      t_years=t, iv=iv, last_price=price, forward=forward)


def long_put(strike, iv=0.12, lots=1, t=WEEK, price=10.0, forward=None):
    return rk.RiskLeg(instrument_type="PE", strike=strike, quantity=LOT * lots,
                      t_years=t, iv=iv, last_price=price, forward=forward)


def strangle():
    return [short_call(25600.0), short_put(24400.0)]


# ---------------------------------------------------------------------------
# how far the ladder reaches
# ---------------------------------------------------------------------------

def test_the_ladder_reaches_four_sigma():
    """It stopped at two, and two sigma is an ordinary bad day.

    4 June 2024: NIFTY fell ~5.9% intraday against an ATM IV near 25%, so a
    daily sigma of 1.57% and a move of 3.8 sigma. A ceiling of 2 could not see
    that day at all — not underestimate it, not see it.
    """
    assert max(rk.SHOCKS) >= 4.0
    reached = {abs(s["sigma"]) for s in rk.scenarios(strangle(), SPOT, R)}
    assert reached == set(rk.SHOCKS)


def test_the_tail_is_reported_separately_from_the_everyday_number():
    out = rk.summarise(strangle(), SPOT, R)
    assert out["var"] < 0 and out["tail"] < 0
    # The tail is the one that should size the position, and it is materially
    # worse — that gap is the whole reason it is reported.
    assert out["tail"] < out["var"]
    assert out["tail_sigma"] == 4.0


def test_the_tail_is_far_worse_than_twice_the_two_sigma_loss():
    """Convexity is the point. If the tail were merely linear in sigma a
    parametric VaR would do, and the doubling rule of thumb people carry in
    their head would be safe. It is not."""
    out = rk.summarise(strangle(), SPOT, R)
    assert abs(out["tail"]) > 2.0 * abs(out["var"])


# ---------------------------------------------------------------------------
# the legs that used to disappear
# ---------------------------------------------------------------------------

def test_a_leg_with_no_solvable_iv_still_carries_risk():
    """The expensive one. An option under five paise fails the IV solve, so a
    seller's far wings and every leg on expiry day used to be valued at
    intrinsic — zero while out of the money — and contributed nothing to any
    scenario. The cheapest strikes are exactly the ones that go to 300."""
    priced = [short_call(25600.0, iv=0.12)]
    unpriced = [short_call(25600.0, iv=None)]

    assert rk.summarise(priced, SPOT, R)["tail"] < 0
    assert rk.summarise(unpriced, SPOT, R)["tail"] < 0, \
        "a leg whose IV would not solve must not read as riskless"


def test_a_bought_wing_that_cannot_be_priced_still_caps_the_loss():
    """The same fault from the other side, and the more dangerous one.

    A cheap long put is the only thing standing between a short put and an
    unbounded morning. Valued at intrinsic it is worth nothing in every
    scenario, so the model reported a hedged book as if it were naked — and a
    naked one as if the hedge were there when the hedge was the leg that priced.
    """
    naked = [short_put(24400.0, iv=0.12)]
    hedged = naked + [long_put(23800.0, iv=None, price=0.05)]

    assert rk.summarise(hedged, SPOT, R)["tail"] > rk.summarise(naked, SPOT, R)["tail"]


def test_modelled_legs_are_counted_so_the_substitution_is_visible():
    out = rk.summarise([short_call(25600.0, iv=0.12),
                        short_put(24400.0, iv=None)], SPOT, R)
    assert out["modelled_legs"] == 1
    assert out["iv_source"] == "market"


def test_a_book_with_no_ivs_at_all_says_it_is_assuming():
    out = rk.summarise([short_call(25600.0, iv=None)], SPOT, R)
    assert out["iv_source"] == "assumed"
    assert out["book_iv_pct"] == pytest.approx(rk.ASSUMED_ANNUAL_IV * 100)


# ---------------------------------------------------------------------------
# the pricing curve
# ---------------------------------------------------------------------------

def test_black_76_on_a_carry_forward_reproduces_black_scholes_at_spot():
    """Why a leg with no stored forward prices exactly as it did before.

    B76 with F = S*e^(rT) is algebraically the same model as BS at S. This is
    the check that the forward change moved nothing it should not have — and
    the reason the audit's estimate of that finding was too large.
    """
    from app.quant.greeks import b76_price, bs_price

    for t in (3 / 365, 45 / 365, 180 / 365):
        forward = SPOT * math.exp(R * t)
        assert b76_price(forward, 25600.0, t, 0.12, "CE", R) == pytest.approx(
            bs_price(SPOT, 25600.0, t, 0.12, "CE", R), rel=1e-9)


def test_a_shocked_spot_drags_its_forward_with_it():
    """`_forward_of` has to be given the unshocked spot to scale from. Handed
    the shocked one it would return the stored forward unchanged and silently
    cancel the shock on every leg that carries one."""
    leg = short_call(25600.0, forward=25030.0)
    crashed = SPOT * 0.94

    assert rk._forward_of(leg, crashed, SPOT, R) == pytest.approx(25030.0 * 0.94)
    # And the basis ratio is preserved, not the absolute basis.
    assert (rk._forward_of(leg, crashed, SPOT, R) / crashed
            == pytest.approx(25030.0 / SPOT))


def test_a_leg_with_no_forward_falls_back_to_carry():
    leg = short_call(25600.0, forward=None)
    assert rk._forward_of(leg, SPOT, SPOT, R) == pytest.approx(
        SPOT * math.exp(R * WEEK))


# ---------------------------------------------------------------------------
# what the number actually means
# ---------------------------------------------------------------------------

def test_the_scenario_includes_the_day_of_theta_it_claims_to():
    """The docstring promised the gap net of the decay collected for holding
    through it. The code priced BOTH the base and the shocked valuation a day
    out, so the decay cancelled and the promise was not kept. `decay` is now
    reported separately so the two cannot silently diverge again."""
    rows = rk.scenarios(strangle(), SPOT, R)
    assert all("decay" in s and "shock" in s for s in rows)
    # Short premium collects theta, so a day's decay is a gain.
    assert rows[0]["decay"] > 0
    # And pnl is the two together, to the rounding.
    for s in rows:
        assert s["pnl"] == pytest.approx(s["shock"] + s["decay"], abs=0.02)


def test_a_motionless_day_on_short_premium_makes_money():
    """The sign-convention check. A short book with nothing moving collects its
    theta, and if that came out negative something fundamental is inverted.

    Note this is asserted on the *decay*, not on a 1-sigma scenario. Writing it
    the obvious way — "a 1-sigma rally makes money" — fails, and correctly: at
    7 DTE this strangle loses about ₹1,230 of gamma on a 189-point move against
    ₹602 of daily theta, so its breakeven move is well inside one sigma. That is
    a real and slightly uncomfortable property of short-dated premium, not a
    modelling error, and it is worth knowing that the position is underwater on
    an ordinary day in either direction.
    """
    rows = rk.scenarios(strangle(), SPOT, R)
    assert rows[0]["decay"] > 0
    assert all(s["decay"] == pytest.approx(rows[0]["decay"]) for s in rows), \
        "the flat-day decay is one baseline and must not vary by scenario"


def test_the_breakeven_move_is_inside_one_sigma_at_seven_dte():
    """Pinned because it is the shape of the risk, not an incidental number.

    Both directions lose at 1 sigma, so this position needs a *quieter* than
    average day to make money — and the downside loses more than the upside,
    because vol is bumped on the way down and not on the way up.
    """
    rows = {s["sigma"]: s for s in rk.scenarios(strangle(), SPOT, R)}
    assert rows[1.0]["pnl"] < 0 and rows[-1.0]["pnl"] < 0
    assert rows[-1.0]["pnl"] < rows[1.0]["pnl"]


def test_a_long_book_loses_to_decay_where_a_short_one_gains():
    long_book = [long_put(24400.0, iv=0.12, price=100.0)]
    assert rk.scenarios(long_book, SPOT, R)[0]["decay"] < 0


def test_var_is_never_positive():
    """It is a loss figure. A book that cannot lose at 2 sigma reports 0, not a
    gain — a positive 'VaR' on a tile is read as a loss and acted on as one."""
    out = rk.summarise([long_put(24400.0, iv=0.12)], SPOT, R)
    assert out["var"] <= 0
    assert out["tail"] <= 0


# ---------------------------------------------------------------------------
# how much to trust it
# ---------------------------------------------------------------------------

def test_the_model_market_gap_is_reported():
    """The scenarios are differences between two model prices, so a model that
    disagrees with the market cancels out of them. That is what makes them
    clean and also what hides the disagreement."""
    honest = [short_call(25600.0, iv=0.12, price=0.0)]
    honest[0].last_price = rk._revalue(honest[0], SPOT, 1.0, R, 0.12, SPOT)
    assert rk.mark_gap(honest, SPOT, R) == pytest.approx(0.0, abs=1.0)

    # A leg marked far away from what the model says is a warning about every
    # other number in the summary.
    wrong = [short_call(25600.0, iv=0.12, price=500.0)]
    assert abs(rk.mark_gap(wrong, SPOT, R)) > 1000.0


def test_mark_gap_is_unknown_rather_than_zero_with_no_prices():
    assert rk.mark_gap([short_call(25600.0, iv=0.12, price=0.0)], SPOT, R) is None


def test_vol_rises_on_the_way_down_only():
    rows = rk.scenarios(strangle(), SPOT, R)
    down = [s for s in rows if s["sigma"] < 0]
    up = [s for s in rows if s["sigma"] > 0]
    assert all(s["vol_multiplier"] > 1.0 for s in down)
    assert all(s["vol_multiplier"] == 1.0 for s in up)


# ---------------------------------------------------------------------------
# degenerate input
# ---------------------------------------------------------------------------

def test_an_empty_book_reports_nothing_rather_than_zero_risk():
    assert rk.summarise([], SPOT, R) == {}
    assert rk.scenarios([], SPOT, R) == []


def test_no_spot_reports_nothing():
    assert rk.summarise(strangle(), 0.0, R) == {}


def test_an_expired_leg_is_worth_its_intrinsic():
    expired = rk.RiskLeg("CE", 24000.0, -LOT, 0.0, 0.12, 1000.0)
    assert rk._revalue(expired, SPOT, 1.0, R, 0.12, SPOT) == pytest.approx(1000.0)


def test_a_future_is_worth_spot_whatever_the_shock():
    fut = rk.RiskLeg("FUT", 0.0, LOT, WEEK, None, SPOT)
    assert rk._revalue(fut, 24000.0, 1.0, R, 0.12, SPOT) == 24000.0


# ---------------------------------------------------------------------------
# the scenario the audit wrote, priced
# ---------------------------------------------------------------------------

def test_the_premortem_book_is_no_longer_reported_as_survivable():
    """Four lots of a 20-delta strangle with wings, the position from
    pre-mortem scenario 3. The wings are under five paise so their IV does not
    solve; before this they were worth nothing in every scenario and the tail
    the model would report was both too shallow and the wrong shape.

    The assertion is not on a rupee value — that depends on inputs this test
    invents. It is that the tail is materially worse than the everyday figure a
    position would have been sized off, which is the thing that was not true.
    """
    book = [
        short_call(25600.0, iv=0.11, lots=4, price=12.0),
        short_put(24400.0, iv=0.11, lots=4, price=14.0),
        rk.RiskLeg("CE", 26200.0, LOT * 4, WEEK, None, 0.05),   # wing, unpriceable
        rk.RiskLeg("PE", 23800.0, LOT * 4, WEEK, None, 0.05),   # wing, unpriceable
    ]
    out = rk.summarise(book, SPOT, R)

    assert out["modelled_legs"] == 2, "both wings should be modelled, not dropped"
    assert out["tail"] < out["var"], "the tail must be worse than the ordinary day"
    # The wings do their job: the loss is bounded rather than running away.
    naked = book[:2]
    assert out["tail"] > rk.summarise(naked, SPOT, R)["tail"]


# ---------------------------------------------------------------------------
# the wiring — a fixed model nobody can see is worth nothing
# ---------------------------------------------------------------------------

# One test lived here in the full desk: it built a dashboard view and asserted
# that `tail`, `tail_sigma` and the assumption counters actually reach the
# totals the screen renders, because `var` was once computed on every build
# and shown nowhere. It needs `app.aggregate` and the live-view fakes, neither
# of which is in this repository, so it stays with the dashboard rather than
# being watered down into something that passes here and proves less.


# ---------------------------------------------------------------------------
# margin pressure — the half of a bad day that forces your hand
# ---------------------------------------------------------------------------

def test_a_gap_raises_the_scanned_requirement():
    """The point of the whole tile. A 2% move against a short strangle does not
    just cost money — it moves the position nearer its strikes, so the
    exchange's scan finds a worse outcome from the new level and blocks more at
    exactly the moment the account has less."""
    out = rk.gap_pressure(strangle(), SPOT, R)
    assert out["pnl"] < 0
    assert out["requirement_multiple"] > 1.0


def test_the_requirement_multiple_never_drops_below_one():
    """A gap that happens to reduce the scanned requirement is not something to
    bank on when the question is 'can I survive this' — and the direction that
    reduces it is the one that is not hurting you anyway."""
    long_book = [long_put(24400.0, iv=0.12), rk.RiskLeg("CE", 25600.0, LOT,
                                                        WEEK, 0.12, 17.0)]
    assert rk.gap_pressure(long_book, SPOT, R)["requirement_multiple"] >= 1.0


def test_scan_loss_uses_the_exchange_band_not_its_own():
    """`quant/strategy.py` worked out that NSE's 5% floor binds on a quiet week
    and that missing it under-estimates a naked NIFTY short by a factor of two.
    That finding must not exist in two places, so the band is imported."""
    from app.quant.strategy import MARGIN_SHOCK_SIGMA, MIN_SCAN_PCT

    quiet = [short_call(25600.0, iv=0.09), short_put(24400.0, iv=0.09)]
    daily = rk.daily_sigma(quiet)
    assert MARGIN_SHOCK_SIGMA * daily < MIN_SCAN_PCT, "expected the floor to bind"
    # And the scan actually reaches far enough to find the loss past the strikes.
    assert rk.scan_loss(quiet, SPOT, R) > 0


def test_scan_loss_is_zero_for_a_book_that_cannot_lose_in_the_band():
    assert rk.scan_loss([], SPOT, R) == 0.0
    assert rk.scan_loss(strangle(), 0.0, R) == 0.0


def test_gap_pressure_on_an_empty_book_is_inert():
    out = rk.gap_pressure([], SPOT, R)
    assert out["pnl"] == 0.0 and out["requirement_multiple"] == 1.0
