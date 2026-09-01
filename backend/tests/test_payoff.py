"""Payoff curves, checked against a strangle whose answer is known by hand."""

import pytest

from app.quant.payoff import Leg, build

R = 0.07
SPOT = 24500.0
LOT = 50


def short_strangle() -> list[Leg]:
    """Sell 24000 PE at 100 and 25000 CE at 100, one lot each, at expiry."""
    return [
        Leg("PE", 24000.0, -LOT, 100.0, t_years=0.0),
        Leg("CE", 25000.0, -LOT, 100.0, t_years=0.0),
    ]


def test_max_profit_is_the_premium_collected():
    result = build(short_strangle(), SPOT, R)
    assert result["max_profit"] == pytest.approx(200.0 * LOT, abs=1.0)


def test_breakevens_sit_a_premium_beyond_each_strike():
    result = build(short_strangle(), SPOT, R)
    lower, upper = sorted(result["breakevens"])
    # 24000 - 200 and 25000 + 200: the payoff is a straight line through both,
    # so the interpolated breakeven should be exact, not merely close.
    assert lower == pytest.approx(23800.0, abs=1.0)
    assert upper == pytest.approx(25200.0, abs=1.0)


def test_loss_grows_without_limit_on_both_wings():
    result = build(short_strangle(), SPOT, R)
    curve = result["curve"]
    assert curve[0]["pnl"] < 0 and curve[-1]["pnl"] < 0
    assert result["max_loss"] < 0
    # The curve has to reach far enough out to show the wings at all.
    assert result["range"]["low"] < 23800 and result["range"]["high"] > 25200


def test_nearest_breakeven_is_reported_as_a_percentage_of_spot():
    result = build(short_strangle(), SPOT, R)
    # Spot 24500 is 700 from 25200 and 700 from 23800 — 2.86% either way.
    assert result["nearest_breakeven_pct"] == pytest.approx(2.86, abs=0.05)


def test_a_long_dated_leg_keeps_time_value_at_the_near_expiry():
    """A calendar's back month is marked, not treated as expired.

    Short the near-dated call, long the same strike further out. At the near
    expiry the long leg still has extrinsic value, so the combined max loss
    must be smaller than the naked short's would be.
    """
    calendar = [
        Leg("CE", 25000.0, -LOT, 100.0, t_years=0.0, iv=0.14),
        Leg("CE", 25000.0, LOT, 260.0, t_years=60 / 365, iv=0.14),
    ]
    naked = [Leg("CE", 25000.0, -LOT, 100.0, t_years=0.0, iv=0.14)]

    covered_loss = build(calendar, SPOT, R)["max_loss"]
    naked_loss = build(naked, SPOT, R)["max_loss"]
    assert covered_loss > naked_loss


def test_empty_book_returns_nothing_rather_than_a_flat_line():
    assert build([], SPOT, R) == {}
    assert build(short_strangle(), 0.0, R) == {}
