"""Telling a tradeable price from a carry-forward.

The property that matters is asymmetric, so it is worth stating: calling a
liquid bar stale costs a needless model mark, while calling a stale bar traded
prices a fill nobody could have got — and does it in the *flattering*
direction, because a frozen price never moves against you. Every threshold here
is set with that asymmetry in mind, and the tests below are mostly about the
second kind of mistake.

The measured shape this module exists for, on real NIFTY data: within ±10 of
the money 0.1% of bars are unchanged from the previous minute; beyond ±30,
87.4% are.
"""

import math

import pytest

from app.backtest import liquidity as liq
from app.quant import greeks as gk

SPOT = 24000.0
T = 5 / 365.0
VOL = 0.14


class Row:
    """What fit_smile reads: a chain row that knows whether it printed."""

    def __init__(self, strike, opt_type, price, traded=True):
        self.strike, self.opt_type = strike, opt_type
        self.price, self.traded = price, traded


def _chain(vol=VOL, spot=SPOT, traded=True, skew=0.0):
    """A textbook chain priced off Black-76, optionally with skew."""
    rows = []
    for level in range(-12, 13):
        strike = spot + level * 50
        for opt_type in ("CE", "PE"):
            if (opt_type == "CE" and strike < spot) or \
               (opt_type == "PE" and strike > spot):
                continue
            k = math.log(strike / spot)
            local = vol + skew * k
            price = gk.b76_price(spot, strike, T, local, opt_type, 0.0)
            rows.append(Row(strike, opt_type, price, traded))
    return rows


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def test_a_bar_that_traded_is_traded():
    assert liq.classify(volume=500, close=100.0,
                        previous_close=99.0).state == "traded"


def test_an_unchanged_price_with_no_volume_is_stale():
    """Both conditions, which is the whole point — see the next test."""
    quality = liq.classify(volume=0, close=100.0, previous_close=100.0)
    assert quality.state == "stale"
    assert quality.needs_marking is True


def test_a_liquid_contract_printing_the_same_tick_twice_is_not_stale():
    """The at-the-money contract trades ~1,275 lots a minute and prints the
    same tick constantly. Calling price-unchanged alone stale would condemn
    28% of ATM bars, which would be nonsense."""
    assert liq.classify(volume=1200, close=100.0,
                        previous_close=100.0).state == "traded"


def test_a_quiet_minute_between_two_trades_is_not_stale():
    """Zero volume alone is weak evidence: the market is still live, this
    minute simply had no news."""
    assert liq.classify(volume=0, close=101.5,
                        previous_close=100.0).state == "traded"


def test_a_contract_that_barely_traded_all_session_is_dead():
    assert liq.classify(volume=0, close=5.0, previous_close=5.0,
                        traded_minutes=2).state == "dead"


def test_no_open_interest_means_no_market():
    """Volume can be zero for an hour on a contract people hold. Open interest
    at zero means there is nobody to trade out against at all."""
    assert liq.classify(volume=800, close=5.0, previous_close=4.0,
                        oi=10).state == "dead"


def test_a_nonsense_price_is_dead_not_stale():
    for bad in (0.0, -1.0, float("nan")):
        assert liq.classify(volume=100, close=bad,
                            previous_close=1.0).state == "dead"


def test_dead_bars_are_not_tradeable_and_the_rest_are():
    assert liq.BarQuality("dead").tradeable is False
    assert liq.BarQuality("stale").tradeable is True
    assert liq.BarQuality("traded").tradeable is True


# ---------------------------------------------------------------------------
# the smile
# ---------------------------------------------------------------------------

def test_a_flat_chain_recovers_the_volatility_it_was_priced_with():
    smile = liq.fit_smile(_chain(), SPOT, T)
    assert smile is not None
    vol, extrapolated = smile.vol_at(SPOT)
    assert vol == pytest.approx(VOL, abs=0.01)
    assert extrapolated is False


def test_a_skewed_chain_recovers_its_skew():
    """A real chain is not flat, and a fit that could only do flat would mark
    every wing at the at-the-money vol — which is exactly the error that makes
    a cheap wing look cheaper than it was."""
    smile = liq.fit_smile(_chain(skew=-0.6), SPOT, T)
    assert smile is not None
    low, _ = smile.vol_at(SPOT - 500)
    high, _ = smile.vol_at(SPOT + 500)
    assert low > high, "the put wing should carry the higher vol"


def test_stale_rows_are_excluded_from_the_fit():
    """Fitting the surface to carry-forward prices would launder the very
    staleness the surface exists to correct."""
    rows = _chain(traded=True) + [
        Row(SPOT + 2000, "CE", 999.0, traded=False)]        # absurd, ignored
    smile = liq.fit_smile(rows, SPOT, T)
    assert smile is not None
    assert smile.hi_strike < SPOT + 2000


def test_too_few_traded_strikes_returns_nothing_rather_than_a_default():
    """A thin session must not be papered over with an assumed vol — that
    would be a fabricated price wearing a model's authority."""
    assert liq.fit_smile([Row(SPOT, "CE", 100.0)], SPOT, T) is None
    assert liq.fit_smile([], SPOT, T) is None


def test_the_in_the_money_side_is_not_fitted():
    """Deep ITM price is nearly all intrinsic, so its implied vol is a tiny
    residual on a large number and inverts terribly."""
    smile = liq.fit_smile(_chain(), SPOT, T)
    assert smile is not None
    # Every fitted strike is on the out-of-the-money side of one or the other.
    assert smile.lo_strike <= SPOT <= smile.hi_strike


def test_the_smile_refuses_to_read_far_outside_its_support():
    """A quadratic extrapolated three times its own support turns over and
    hands back a falling — or negative — wing vol. Refusing is the only
    honest answer."""
    smile = liq.fit_smile(_chain(), SPOT, T)
    vol, _ = smile.vol_at(SPOT + 50 * 200)
    assert math.isnan(vol)


def test_reading_just_outside_the_support_is_allowed_but_flagged():
    smile = liq.fit_smile(_chain(), SPOT, T)
    vol, extrapolated = smile.vol_at(smile.hi_strike + 100)
    assert math.isfinite(vol)
    assert extrapolated is True


def test_the_smile_never_returns_an_impossible_volatility():
    smile = liq.fit_smile(_chain(skew=-3.0), SPOT, T)
    for level in range(-12, 13):
        vol, _ = smile.vol_at(SPOT + level * 50)
        if math.isfinite(vol):
            assert liq.MIN_VOL <= vol <= liq.MAX_VOL


# ---------------------------------------------------------------------------
# marking
# ---------------------------------------------------------------------------

def test_a_traded_bar_is_marked_at_what_it_traded_at():
    marking = liq.mark(liq.BarQuality("traded"), 123.45, SPOT, "CE",
                       SPOT, T, None, volume=900)
    assert marking.source == "traded"
    assert marking.price == 123.45


def test_a_stale_bar_is_marked_to_the_surface_not_its_frozen_price():
    """The heart of it. The frozen print says ₹40; the surface says what the
    contract is actually worth now that spot has moved."""
    smile = liq.fit_smile(_chain(), SPOT, T)
    strike = SPOT + 300
    frozen = 40.0
    marking = liq.mark(liq.BarQuality("stale"), frozen, strike, "CE",
                       SPOT, T, smile, volume=0)
    assert marking.source == "model"
    fair = gk.b76_price(SPOT, strike, T, VOL, "CE", 0.0)
    assert marking.price == pytest.approx(fair, rel=0.05)
    assert marking.price != frozen


def test_a_dead_bar_is_refused_rather_than_guessed():
    smile = liq.fit_smile(_chain(), SPOT, T)
    marking = liq.mark(liq.BarQuality("dead"), 5.0, SPOT + 300, "CE",
                       SPOT, T, smile)
    assert marking.source == "unavailable"
    assert marking.usable is False


def test_a_stale_bar_with_no_surface_is_refused_not_left_frozen():
    """Without a fit there is nothing to mark to, and using the frozen print
    is the failure this module exists to prevent."""
    marking = liq.mark(liq.BarQuality("stale"), 40.0, SPOT + 300, "CE",
                       SPOT, T, None)
    assert marking.source == "unavailable"


def test_marking_a_wing_far_outside_the_fit_is_refused():
    smile = liq.fit_smile(_chain(), SPOT, T)
    marking = liq.mark(liq.BarQuality("stale"), 1.0, SPOT + 50 * 200, "CE",
                       SPOT, T, smile)
    assert marking.source == "unavailable"


# ---------------------------------------------------------------------------
# the spread — a fill is not a fair value
# ---------------------------------------------------------------------------

def test_an_illiquid_contract_is_charged_a_wider_spread():
    """Charging a liquid contract's half-point on a ₹6 wing that trades 75
    lots a minute is fiction, and fiction in the flattering direction."""
    liquid = liq.spread_points(200.0, volume=2000, oi=200_000)
    thin = liq.spread_points(6.55, volume=75, oi=38_000)
    assert thin / 6.55 > liquid / 200.0 * 10


def test_the_worst_case_is_the_far_side_of_the_spread():
    marking = liq.Marking(100.0, "model", spread=4.0)
    assert marking.worst("BUY") == 102.0
    assert marking.worst("SELL") == 98.0


def test_a_sell_fill_can_never_be_marked_below_a_tick():
    marking = liq.Marking(0.10, "model", spread=10.0)
    assert marking.worst("SELL") >= 0.05


# ---------------------------------------------------------------------------
# what the report has to be told
# ---------------------------------------------------------------------------

def test_the_tally_separates_traded_fills_from_modelled_ones():
    """A backtest that quietly marked a third of its fills to a model is a
    different claim from one that traded every print."""
    tally = liq.Tally()
    tally.add(liq.Marking(100.0, "traded", spread=0.1))
    tally.add(liq.Marking(50.0, "model", spread=2.0))
    tally.add(liq.Marking(50.0, "model", spread=2.0, extrapolated=True))
    tally.add(liq.Marking(0.0, "unavailable"))

    summary = tally.summary()
    assert summary["fills"] == 4
    assert summary["traded"] == 1
    assert summary["modelled"] == 2
    assert summary["extrapolated"] == 1
    assert summary["refused"] == 1
    assert summary["modelled_pct"] == 50.0
