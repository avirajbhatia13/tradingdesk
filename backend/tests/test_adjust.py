"""Rule-based mid-trade adjustment.

Every case here runs on a hand-built chain rather than on the lake, so the
expected P&L is arithmetic rather than a number that came out of a previous
version of the code. That matters more here than anywhere else in the engine:
an adjusted position closes and re-opens legs mid-flight, so a sign error does
not produce an exception, it produces a plausible equity curve.
"""

from datetime import date

import numpy as np
import pytest

from app.backtest import adjust as adj
from app.backtest.costs import CostModel

STEP = 50.0
SLOTS = 10


def _cube(prices: dict[tuple[str, float], list[float]], day=date(2026, 8, 7)):
    """A chain where every strike's price path is written out by hand."""
    stamps = np.array([np.datetime64(f"{day}T09:{15 + i:02d}") for i in
                       range(SLOTS)])
    minutes = np.array([555 + i for i in range(SLOTS)])
    cube = adj.Cube(day=day, minutes=minutes, stamps=stamps,
                    spot=np.full(SLOTS, 24000.0), price={}, step=STEP)
    for key, path in prices.items():
        cube.price[(key[0], float(key[1]))] = np.array(path, dtype=float)
    cube.strikes = sorted({strike for _, strike in cube.price})
    return cube


def _flat(value):
    return [value] * SLOTS


# ---------------------------------------------------------------------------
# the grammar
# ---------------------------------------------------------------------------

def test_a_rule_is_read_the_way_it_is_spoken():
    rule = adj.AdjustRule.parse("gap>=40%: roll-cheap-to-expensive")
    assert rule.trigger == "gap"
    assert rule.threshold == pytest.approx(0.40)
    assert rule.unit == "%"
    assert rule.action == "roll-cheap-to-expensive"


def test_points_and_percent_are_kept_apart():
    """40% of a 200-point credit is 80 points; reading one as the other tests
    the rule at 40x or 1/40th of its intended size and never says so."""
    assert adj.AdjustRule.parse("gap>=40pt: close-cheap").threshold == 40.0
    assert adj.AdjustRule.parse("gap>=40%: close-cheap").threshold == 0.40


def test_an_unreadable_rule_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="cannot read adjustment rule"):
        adj.AdjustRule.parse("when the call doubles, roll it")
    with pytest.raises(ValueError, match="unknown adjustment action"):
        adj.AdjustRule.parse("gap>=40%: do-something-clever")


# ---------------------------------------------------------------------------
# the accounting
# ---------------------------------------------------------------------------

def test_a_short_leg_that_decays_is_a_profit():
    """The sign that must not be got wrong. A credit of 100 decaying to 40 is
    +60, not -60; the inverse reports every premium seller as a loser."""
    cube = _cube({("CE", 24000.0): [100.0] * 5 + [40.0] * 5,
                  ("PE", 24000.0): _flat(0.01)})
    out = adj.simulate(cube, 0, SLOTS - 1,
                       [("CE", "SELL", 24000.0, 1)], adj.AdjustPlan(),
                       slippage=0.0, lot_size=1)
    assert out.pnl_points == pytest.approx(60.0)


def test_slippage_always_hurts_on_both_sides():
    """Sold lower than the print, bought back higher, on a flat market."""
    cube = _cube({("CE", 24000.0): _flat(100.0)})
    out = adj.simulate(cube, 0, SLOTS - 1,
                       [("CE", "SELL", 24000.0, 1)], adj.AdjustPlan(),
                       slippage=0.5, lot_size=1)
    assert out.pnl_points == pytest.approx(-1.0)


def test_a_leg_that_stops_printing_does_not_get_a_stale_mark():
    """A position that cannot be valued must not be valued. Carrying the last
    price forward is how a stop fires against a number nobody was quoting."""
    path = [100.0, 100.0, np.nan, np.nan, 100.0, 100.0, 100.0, 100.0, 100.0,
            100.0]
    cube = _cube({("CE", 24000.0): path})
    out = adj.simulate(cube, 0, SLOTS - 1,
                       [("CE", "SELL", 24000.0, 1)], adj.AdjustPlan(),
                       slippage=0.0, lot_size=1)
    assert out.missing_slots == 2


# ---------------------------------------------------------------------------
# the repair itself
# ---------------------------------------------------------------------------

def _strangle_cube():
    """A call that runs away from a put that decays — the tested case.

    Entry credit is 60 + 60 = 120, so a 40% gap threshold is 48 points. The
    gap reaches 50 at slot 3, which is where the roll must happen.
    """
    return _cube({
        ("CE", 24500.0): [60, 70, 90, 110, 110, 110, 110, 110, 110, 110],
        ("PE", 23500.0): [60, 50, 40, 60.0, 60, 60, 60, 60, 60, 60],
        # Somewhere for the put to roll up to, priced at the call's premium.
        ("PE", 23800.0): [80, 85, 95, 110, 110, 110, 110, 110, 110, 110],
        ("PE", 24000.0): [95, 100, 105, 130, 130, 130, 130, 130, 130, 130],
        ("CE", 24000.0): [130, 140, 150, 170, 170, 170, 170, 170, 170, 170],
    })


def test_the_decayed_leg_is_rolled_to_the_tested_legs_premium():
    """The 40% gap rule: book the cheap side, re-sell it where the dear one is."""
    plan = adj.AdjustPlan(
        rules=(adj.AdjustRule.parse("gap>=40%: roll-cheap-to-expensive"),))
    out = adj.simulate(_strangle_cube(), 0, SLOTS - 1,
                       [("CE", "SELL", 24500.0, 1), ("PE", "SELL", 23500.0, 1)],
                       plan, slippage=0.0, lot_size=1)

    assert len(out.adjustments) == 1
    move = out.adjustments[0]
    assert move.closed == "PE 23500"
    # 23800 prints 110 at that minute, which is exactly the call's premium.
    assert move.opened == "PE 23800"


def test_a_roll_never_crosses_the_tested_strike():
    """The 'no crossover' rule. A put rolled past the call's strike inverts the
    strangle into something the strategy never describes."""
    plan = adj.AdjustPlan(
        rules=(adj.AdjustRule.parse("gap>=40%: roll-cheap-to-expensive"),),
        no_crossover=True)
    out = adj.simulate(_strangle_cube(), 0, SLOTS - 1,
                       [("CE", "SELL", 24500.0, 1), ("PE", "SELL", 23500.0, 1)],
                       plan, slippage=0.0, lot_size=1)
    for leg in out.legs:
        if leg.opt_type == "PE":
            assert leg.strike <= 24500.0


def test_the_adjustment_count_can_be_capped():
    plan = adj.AdjustPlan(
        rules=(adj.AdjustRule.parse("gap>=40%: roll-cheap-to-expensive"),),
        max_adjustments=0)
    out = adj.simulate(_strangle_cube(), 0, SLOTS - 1,
                       [("CE", "SELL", 24500.0, 1), ("PE", "SELL", 23500.0, 1)],
                       plan, slippage=0.0, lot_size=1)
    assert out.adjustments == []


def test_every_fill_is_charged_not_just_the_round_trip():
    """An adjusted position pays brokerage on each repair. A cost model that
    only saw the entry and the exit would make adjusting look free, which is
    precisely the thing being tested."""
    plan = adj.AdjustPlan(
        rules=(adj.AdjustRule.parse("gap>=40%: roll-cheap-to-expensive"),))
    out = adj.simulate(_strangle_cube(), 0, SLOTS - 1,
                       [("CE", "SELL", 24500.0, 1), ("PE", "SELL", 23500.0, 1)],
                       plan, slippage=0.0, lot_size=75)

    # 2 opening + 1 close of the rolled leg + 1 re-open + 2 final closes.
    assert len(out.fills) == 6
    charges = CostModel(brokerage_per_order=20.0).charge(out.fills,
                                                         date(2026, 8, 7))
    assert charges.brokerage == pytest.approx(6 * 20.0)


def test_wings_are_bought_once_the_position_becomes_a_straddle():
    """Rule 5: a naked straddle is capped rather than carried."""
    cube = _cube({
        ("CE", 24000.0): [60, 70, 90, 110, 110, 110, 110, 110, 110, 110],
        ("PE", 23500.0): [60, 50, 40, 60.0, 60, 60, 60, 60, 60, 60],
        ("PE", 24000.0): [95, 100, 105, 110, 110, 110, 110, 110, 110, 110],
        # the wings the breakeven rule should reach for
        ("CE", 24200.0): _flat(12.0),
        ("PE", 23800.0): _flat(11.0),
    })
    plan = adj.AdjustPlan(
        rules=(adj.AdjustRule.parse("gap>=40%: roll-cheap-to-expensive"),),
        wings="breakeven")
    out = adj.simulate(cube, 0, SLOTS - 1,
                       [("CE", "SELL", 24000.0, 1), ("PE", "SELL", 23500.0, 1)],
                       plan, slippage=0.0, lot_size=1)

    assert out.became_straddle
    assert out.wings_added
    bought = sorted(leg.opt_type for leg in out.legs if leg.side == "BUY")
    assert bought == ["CE", "PE"], "a capped straddle needs both wings"


def test_a_target_closes_the_whole_position():
    cube = _cube({("CE", 24000.0): [100.0, 100.0, 20.0] + [20.0] * 7})
    out = adj.simulate(cube, 0, SLOTS - 1, [("CE", "SELL", 24000.0, 1)],
                       adj.AdjustPlan(), slippage=0.0, lot_size=1,
                       target_points=50.0)
    assert out.exit_reason == "target"
    assert out.exit_slot == 2


def test_a_percentage_target_is_measured_on_the_entry_credit():
    """Not on the credit after a repair. Re-basing it would move the target
    every time the position was adjusted."""
    cube = _cube({("CE", 24000.0): [100.0, 100.0, 40.0] + [40.0] * 7})
    out = adj.simulate(cube, 0, SLOTS - 1, [("CE", "SELL", 24000.0, 1)],
                       adj.AdjustPlan(), slippage=0.0, lot_size=1,
                       target_pct=0.5)          # 50% of 100 = 50 points
    assert out.exit_reason == "target"
    assert out.pnl_points == pytest.approx(60.0)
