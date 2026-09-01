"""The forward, and the parity property that proves it is right.

These pin the bug that made this module necessary: greeks were computed against
the index level instead of the forward, which forced a ~4 vol-point wedge
between the call and put IV at the same strike. Put-call parity forbids that,
so it is a property we can assert directly rather than a number to eyeball.
"""

import math

import pytest

from app.quant import forward as fw
from app.quant import greeks as gk

R = 0.07


def _chain_row(strike, call, put, source="mid"):
    return {"strike": strike,
            "ce": {"price": call, "price_source": source},
            "pe": {"price": put, "price_source": source}}


# ---------------------------------------------------------------------------
# parity solve
# ---------------------------------------------------------------------------

def test_parity_recovers_a_forward_we_planted():
    """Price a synthetic chain off a known forward; the solver must find it."""
    forward, t = 24421.0, 3 / 365
    rows = []
    for strike in (24300.0, 24350.0, 24400.0, 24450.0):
        call = gk.b76_price(forward, strike, t, 0.09, "CE", R)
        put = gk.b76_price(forward, strike, t, 0.09, "PE", R)
        rows.append(_chain_row(strike, call, put))

    solved, source = fw.solve(rows, spot=24366.0, t_years=t, r=R)
    assert source == "parity"
    assert solved == pytest.approx(forward, abs=0.5)


def test_the_real_numbers_that_exposed_the_bug():
    """NIFTY 24350 pair, 3 DTE, taken off the live feed."""
    solved = fw.from_parity(call_price=125.6, put_price=54.33, strike=24350.0,
                            t_years=3 / 365, r=R)
    assert solved == pytest.approx(24421.3, abs=0.5)
    # The forward sits 55 points above spot. Using spot is what broke the greeks.
    assert solved - 24366.0 > 50


def test_stale_ltp_quotes_are_not_used_for_parity():
    """A forward inherits the staleness of whatever priced it, so LTP is out."""
    rows = [_chain_row(24350.0, 125.6, 54.33, source="ltp")]
    _, source = fw.solve(rows, spot=24366.0, t_years=3 / 365, r=R)
    assert source != "parity"


def test_falls_back_to_the_future_then_to_carry():
    spot, t = 24366.0, 3 / 365
    value, source = fw.solve([], spot, t, R, future_price=24420.0)
    assert (value, source) == (24420.0, "future")

    value, source = fw.solve([], spot, t, R, future_price=0.0)
    assert source == "carry"
    assert value == pytest.approx(spot * math.exp(R * t), abs=0.1)


def test_an_absurd_parity_result_is_rejected():
    """One bad quote must not drag the forward somewhere impossible."""
    rows = [_chain_row(24350.0, 5000.0, 1.0)]
    _, source = fw.solve(rows, spot=24366.0, t_years=3 / 365, r=R)
    assert source != "parity"


def test_far_strikes_are_ignored():
    """(C - P) is all bid-ask noise once you are far from the money."""
    rows = [_chain_row(21000.0, 3400.0, 2.0)]
    _, source = fw.solve(rows, spot=24366.0, t_years=3 / 365, r=R)
    assert source != "parity"


# ---------------------------------------------------------------------------
# Black-76 itself
# ---------------------------------------------------------------------------

def test_call_and_put_imply_the_same_vol_at_one_strike():
    """The property the old code violated. This is the regression test."""
    forward, strike, t, sigma = 24421.0, 24350.0, 3 / 365, 0.088
    call = gk.b76_price(forward, strike, t, sigma, "CE", R)
    put = gk.b76_price(forward, strike, t, sigma, "PE", R)

    call_iv = gk.b76_implied_vol(call, forward, strike, t, "CE", R)
    put_iv = gk.b76_implied_vol(put, forward, strike, t, "PE", R)
    assert abs(call_iv - put_iv) < 0.0005


def test_feeding_a_forward_into_the_spot_model_is_what_broke_it():
    """Pins the actual defect, so nobody 'simplifies' back into it.

    Spot-based Black-Scholes applies carry itself. Hand it a forward and the
    carry is counted twice, wedging the call and put IVs apart.
    """
    forward, strike, t, sigma = 24421.0, 24350.0, 3 / 365, 0.088
    call = gk.b76_price(forward, strike, t, sigma, "CE", R)
    put = gk.b76_price(forward, strike, t, sigma, "PE", R)

    wrong_call = gk.implied_vol(call, forward, strike, t, "CE", R)
    wrong_put = gk.implied_vol(put, forward, strike, t, "PE", R)
    assert abs(wrong_call - wrong_put) > 0.005          # the wedge appears

    right_call = gk.b76_implied_vol(call, forward, strike, t, "CE", R)
    right_put = gk.b76_implied_vol(put, forward, strike, t, "PE", R)
    assert abs(right_call - right_put) < 0.0005         # and is gone


def test_put_call_parity_holds_on_the_prices():
    forward, strike, t, sigma = 24421.0, 24500.0, 20 / 365, 0.11
    call = gk.b76_price(forward, strike, t, sigma, "CE", R)
    put = gk.b76_price(forward, strike, t, sigma, "PE", R)
    assert call - put == pytest.approx(
        math.exp(-R * t) * (forward - strike), abs=0.01)


def test_atm_deltas_are_near_half_and_sum_to_one():
    forward, t, sigma = 24421.0, 7 / 365, 0.09
    call = gk.b76_greeks(forward, forward, t, sigma, "CE", R)
    put = gk.b76_greeks(forward, forward, t, sigma, "PE", R)
    assert call["delta"] == pytest.approx(0.5, abs=0.01)
    assert put["delta"] == pytest.approx(-0.5, abs=0.01)
    assert call["delta"] - put["delta"] == pytest.approx(math.exp(-R * t), abs=1e-6)


def test_short_premium_decays_in_your_favour():
    """Theta is negative per long unit; scaling by a short quantity flips it."""
    unit = gk.b76_greeks(24421.0, 24800.0, 5 / 365, 0.10, "CE", R)
    assert unit["theta"] < 0
    assert gk.scale(unit, -750)["theta"] > 0


def test_gamma_and_vega_are_symmetric_across_the_pair():
    forward, strike, t, sigma = 24421.0, 24600.0, 10 / 365, 0.10
    call = gk.b76_greeks(forward, strike, t, sigma, "CE", R)
    put = gk.b76_greeks(forward, strike, t, sigma, "PE", R)
    assert call["gamma"] == pytest.approx(put["gamma"], rel=1e-9)
    assert call["vega"] == pytest.approx(put["vega"], rel=1e-9)


def test_expired_options_are_intrinsic_only():
    assert gk.b76_price(24421.0, 24000.0, 0.0, 0.1, "CE", R) == pytest.approx(421.0)
    assert gk.b76_price(24421.0, 24000.0, 0.0, 0.1, "PE", R) == 0.0
    assert gk.b76_greeks(24421.0, 24000.0, 0.0, 0.1, "CE", R)["gamma"] == 0.0
