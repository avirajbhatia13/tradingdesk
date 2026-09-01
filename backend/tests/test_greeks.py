"""The pricing core has to be right before anything built on it means anything."""

import math

import pytest

from app.quant import greeks as gk

R = 0.07
SPOT, STRIKE, T, VOL = 24500.0, 24500.0, 30 / 365, 0.14


def test_put_call_parity():
    call = gk.bs_price(SPOT, STRIKE, T, VOL, "CE", R)
    put = gk.bs_price(SPOT, STRIKE, T, VOL, "PE", R)
    assert call - put == pytest.approx(SPOT - STRIKE * math.exp(-R * T), abs=0.01)


def test_delta_stays_in_bounds():
    for strike in (22000, 24500, 27000):
        call = gk.option_greeks(SPOT, strike, T, VOL, "CE", R)
        put = gk.option_greeks(SPOT, strike, T, VOL, "PE", R)
        assert 0.0 < call["delta"] < 1.0
        assert -1.0 < put["delta"] < 0.0
        # Same strike, same expiry: the deltas differ by exactly one.
        assert call["delta"] - put["delta"] == pytest.approx(1.0, abs=0.01)


def test_long_option_bleeds_and_short_option_earns():
    unit = gk.option_greeks(SPOT, 26000, T, VOL, "CE", R)
    assert unit["theta"] < 0, "a long option must lose value with time"

    # Kite reports a short position's quantity as negative, so the sign flip
    # that turns decay into income has to fall out of the multiplication alone.
    short = gk.scale(unit, -75)
    assert short["theta"] > 0
    assert short["vega"] < 0, "short premium is short vol"
    assert short["gamma"] < 0


def test_implied_vol_round_trip():
    for strike, vol in ((24500, 0.13), (26000, 0.18), (22500, 0.22)):
        price = gk.bs_price(SPOT, strike, T, vol, "CE", R)
        assert gk.implied_vol(price, SPOT, strike, T, "CE", R) == pytest.approx(vol, abs=0.005)


def test_implied_vol_refuses_impossible_prices():
    # Below intrinsic and above the underlying: both outside no-arbitrage bounds.
    assert gk.implied_vol(0.5, SPOT, 20000, T, "CE", R) is None
    assert gk.implied_vol(SPOT * 2, SPOT, STRIKE, T, "CE", R) is None
    assert gk.implied_vol(0.0, SPOT, STRIKE, T, "CE", R) is None


def test_at_expiry_greeks_collapse():
    itm = gk.option_greeks(SPOT, 24000, 0.0, VOL, "CE", R)
    otm = gk.option_greeks(SPOT, 25000, 0.0, VOL, "CE", R)
    assert itm["delta"] == 1.0 and otm["delta"] == 0.0
    for key in ("gamma", "theta", "vega"):
        assert itm[key] == 0.0 and otm[key] == 0.0


def test_stale_quote_detection():
    now = 1_700_000_000.0
    assert gk.quote_is_usable(12.5, int(now - 30), now)
    assert not gk.quote_is_usable(12.5, int(now - 900), now)
    assert not gk.quote_is_usable(0.0, int(now), now)
    # No exchange timestamp is not evidence of staleness.
    assert gk.quote_is_usable(12.5, None, now)
