"""Strategy analysis, checked against structures with known closed forms.

Spreads are the right thing to test a payoff engine with, because their max
profit, max loss and breakeven are all exactly computable by hand — so these
are equalities, not eyeballed ranges.

The cases that matter most here are the ones where a wrong answer is dangerous
rather than merely wrong: a naked short call must never report a finite max
loss, and a hedged spread must never report a margin larger than it can lose.
"""

import pytest

from app.quant.strategy import Leg, analyse, net_premium

R = 0.07
T = 7 / 365
LOT = 65
F = 24421.0


def leg(kind, strike, lots, price, t_years=T, iv=0.09):
    return Leg(instrument_type=kind, strike=strike, quantity=lots * LOT,
               entry_price=price, t_years=t_years, iv=iv, lot_size=LOT)


# ---------------------------------------------------------------------------
# defined-risk structures, against their closed forms
# ---------------------------------------------------------------------------

def test_bear_call_spread_matches_the_arithmetic():
    credit = (95.0 - 38.0) * 3 * LOT
    width = (24800 - 24600) * 3 * LOT
    a = analyse([leg("CE", 24600, -3, 95.0), leg("CE", 24800, 3, 38.0)], F, R)

    assert a["credit_received"] == pytest.approx(credit)
    assert a["max_profit"] == pytest.approx(credit)
    assert a["max_loss"] == pytest.approx(credit - width)
    assert a["breakevens"] == pytest.approx([24600 + 57.0], abs=1.0)
    assert a["unlimited_loss"] is False


def test_iron_condor_max_loss_is_the_wing_width_less_credit():
    legs = [leg("PE", 23900, 1, 28.0), leg("PE", 24000, -1, 42.0),
            leg("CE", 24800, -1, 38.0), leg("CE", 24900, 1, 24.0)]
    credit = (42.0 - 28.0 + 38.0 - 24.0) * LOT
    a = analyse(legs, F, R)

    assert a["credit_received"] == pytest.approx(credit)
    assert a["max_profit"] == pytest.approx(credit)
    assert a["max_loss"] == pytest.approx(credit - 100 * LOT)
    assert len(a["breakevens"]) == 2
    assert a["unlimited_loss"] is False


def test_a_debit_spread_reports_a_debit():
    a = analyse([leg("CE", 24400, 1, 128.0), leg("CE", 24700, -1, 42.0)], F, R)
    assert a["is_credit"] is False
    assert a["debit_paid"] == pytest.approx((128.0 - 42.0) * LOT)
    assert a["max_loss"] == pytest.approx(-(128.0 - 42.0) * LOT)


# ---------------------------------------------------------------------------
# unbounded risk — the answers that must never be a comfortable number
# ---------------------------------------------------------------------------

def test_naked_short_call_loss_is_unlimited_not_a_number():
    a = analyse([leg("CE", 24600, -1, 95.0)], F, R)
    assert a["unlimited_loss"] is True
    assert a["max_loss"] is None
    assert a["risk_reward"] is None
    assert a["max_profit"] == pytest.approx(95.0 * LOT)


def test_short_strangle_is_unlimited_on_the_call_side():
    a = analyse([leg("PE", 24000, -1, 42.0), leg("CE", 24800, -1, 38.0)], F, R)
    assert a["unlimited_loss"] is True
    assert len(a["breakevens"]) == 2


def test_short_put_loss_is_large_but_finite():
    """Spot cannot go below zero, so this is a number, not 'unlimited'.

    Saying 'unlimited' here would be the easy way out and it would be wrong —
    the honest answer is a specific, alarming figure.
    """
    a = analyse([leg("PE", 24000, -1, 42.0)], F, R)
    assert a["unlimited_loss"] is False
    assert a["max_loss"] == pytest.approx(-(24000 * LOT) + 42.0 * LOT, rel=1e-3)


def test_long_call_profit_is_unlimited_but_loss_is_the_premium():
    a = analyse([leg("CE", 24600, 1, 95.0)], F, R)
    assert a["unlimited_profit"] is True
    assert a["max_profit"] is None
    assert a["max_loss"] == pytest.approx(-95.0 * LOT)


def test_a_covered_short_call_is_not_unlimited():
    """A future underneath the short call caps the upside loss."""
    future = Leg(instrument_type="FUT", strike=0.0, quantity=LOT,
                 entry_price=F, t_years=T, lot_size=LOT)
    a = analyse([leg("CE", 24600, -1, 95.0), future], F, R)
    assert a["unlimited_loss"] is False


# ---------------------------------------------------------------------------
# margin
# ---------------------------------------------------------------------------

def test_hedged_spread_margin_never_exceeds_its_max_loss():
    """The exchange cannot ask for more than the position can lose."""
    a = analyse([leg("CE", 24600, -3, 95.0), leg("CE", 24800, 3, 38.0)], F, R)
    assert a["margin"]["total"] <= abs(a["max_loss"]) + 0.01


def test_spread_margin_is_far_below_the_naked_equivalent():
    """Buying the wing has to be worth something, or the estimate is useless."""
    naked = analyse([leg("CE", 24600, -1, 95.0)], F, R)["margin"]["total"]
    spread = analyse([leg("CE", 24600, -1, 95.0),
                      leg("CE", 24800, 1, 38.0)], F, R)["margin"]["total"]
    assert spread < naked / 2


def test_exposure_is_charged_only_on_uncovered_shorts():
    """A short leg sitting under a long leg is hedged and attracts no exposure."""
    spread = analyse([leg("CE", 24600, -1, 95.0), leg("CE", 24800, 1, 38.0)], F, R)
    assert spread["margin"]["exposure"] == 0.0
    assert analyse([leg("CE", 24600, -1, 95.0)], F, R)["margin"]["exposure"] > 0


def test_a_backspread_charges_the_premium_it_actually_costs():
    """A scenario margin of zero is not a capital requirement of zero.

    A 1x3 ratio backspread is long gamma, so every price shock SPAN tests is a
    gain and the scan loss is genuinely nil — but the position still costs a
    net debit to put on. It reported ₹0 of margin while costing ₹16,256 a day,
    which made every return figure in a backtest of it divide by nothing.
    """
    a = analyse([leg("CE", 24600, -1, 95.0), leg("CE", 24900, 3, 40.0)], F, R)
    debit = 3 * 40.0 * LOT - 95.0 * LOT
    assert debit > 0                                   # it is a debit position
    # Long gamma: the scan finds far less than the position actually cost, so
    # the old span+exposure figure was the wrong capital by a wide margin.
    assert a["margin"]["span"] + a["margin"]["exposure"] < debit
    assert a["margin"]["total"] == pytest.approx(debit, rel=0.01)


def test_a_debit_spread_is_not_charged_its_premium_twice():
    """The rule is the larger of blocked-and-paid, not their sum. On a debit
    spread the max-loss cap already collapses margin to about the debit, so
    adding the two would charge the same rupees again."""
    a = analyse([leg("CE", 24600, 1, 95.0), leg("CE", 24800, -1, 38.0)], F, R)
    debit = (95.0 - 38.0) * LOT
    assert a["margin"]["total"] == pytest.approx(debit, rel=0.02)


def test_naked_index_short_lands_near_what_zerodha_blocks():
    """~1.1L for one NIFTY lot. The exchange's 5% scan floor is what gets it there."""
    total = analyse([leg("CE", 24600, -1, 95.0)], F, R)["margin"]["total"]
    assert 80_000 < total < 160_000


def test_long_only_basket_risks_only_its_premium():
    a = analyse([leg("CE", 24600, 1, 95.0), leg("PE", 24000, 1, 42.0)], F, R)
    assert a["margin"]["total"] == pytest.approx((95.0 + 42.0) * LOT)


# ---------------------------------------------------------------------------
# greeks, premium and the rest
# ---------------------------------------------------------------------------

def test_short_premium_collects_theta_and_is_short_vega():
    a = analyse([leg("PE", 24000, -1, 42.0), leg("CE", 24800, -1, 38.0)], F, R)
    assert a["greeks"]["theta"] > 0
    assert a["greeks"]["vega"] < 0
    assert a["greeks"]["gamma"] < 0


def test_a_strangle_is_close_to_delta_neutral():
    a = analyse([leg("PE", 24000, -1, 42.0), leg("CE", 24800, -1, 38.0)], F, R)
    assert abs(a["greeks"]["delta"]) < 0.25 * LOT


def test_net_premium_sign_convention():
    """Negative is a credit. Stated as a test because everyone gets it backwards."""
    assert net_premium([leg("CE", 24600, -1, 95.0)]) < 0
    assert net_premium([leg("CE", 24600, 1, 95.0)]) > 0


def test_probability_of_profit_is_a_percentage_and_ranks_sensibly():
    wide = analyse([leg("PE", 23000, -1, 8.0), leg("CE", 26000, -1, 6.0)], F, R)
    tight = analyse([leg("PE", 24400, -1, 120.0), leg("CE", 24450, -1, 118.0)], F, R)
    assert 0 <= tight["pop"] <= 100
    assert wide["pop"] > tight["pop"]


def test_the_curves_cover_the_same_grid():
    """The T+n line is drawn against the expiry curve's x values, so they must align."""
    a = analyse([leg("CE", 24600, -1, 95.0), leg("CE", 24800, 1, 38.0)], F, R,
                days_forward=3)
    assert len(a["curve"]) == len(a["curve_now"])
    assert a["curve"][0]["spot"] == a["curve_now"][0]["spot"]


def test_time_decay_helps_a_short_before_expiry():
    """At the money, the T+n line for short premium sits above the expiry line."""
    legs = [leg("PE", 24400, -1, 120.0), leg("CE", 24400, -1, 128.0)]
    a = analyse(legs, F, R, days_forward=0)
    middle = len(a["curve"]) // 2
    assert a["curve_now"][middle]["pnl"] < a["curve"][middle]["pnl"]


def test_chart_range_tightens_for_a_near_expiry_position():
    """A 3-DTE condor drawn over +/-10% is unreadable; the range has to adapt."""
    near = analyse([leg("PE", 24150, -1, 13.6, t_years=3 / 365),
                    leg("CE", 24550, -1, 35.8, t_years=3 / 365)], F, R)
    far = analyse([leg("PE", 23000, -1, 120.0, t_years=30 / 365),
                   leg("CE", 26000, -1, 140.0, t_years=30 / 365)], F, R)
    near_width = near["range"]["high"] - near["range"]["low"]
    far_width = far["range"]["high"] - far["range"]["low"]
    assert near_width < far_width
    assert near_width / F < 0.10


def test_an_empty_basket_returns_nothing_rather_than_raising():
    assert analyse([], F, R) == {}
    assert analyse([leg("CE", 24600, -1, 95.0)], 0.0, R) == {}
