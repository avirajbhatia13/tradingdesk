"""The backtest engine, against a lake built so every answer is known by hand.

The first test in this file is the one that matters. During development the P&L
sign was inverted: a short straddle that decayed from 92 to 79 — a clear profit
for the seller — was booked as a loss, and every short-premium strategy came out
negative. It looked plausible enough to believe. The fixture below is
constructed so the correct answer is arithmetic, not judgement.

The long/short mirror test is the cheap structural check: whatever the engine
says a position made, it must say the opposite position lost exactly as much
(before costs and slippage, which hurt both sides).
"""

from datetime import date, datetime, time, timedelta

import pytest

from app.backtest import costs as costs_mod
from app.backtest.engine import LegSpec, StrategySpec, run
from app.data import lake
from app.data import schema as sch

DAY = date(2026, 8, 14)
LOT = 75


@pytest.fixture()
def decaying_lake(tmp_path, monkeypatch):
    """One session where the ATM straddle decays 100 -> 60, on a flat spot.

    CE and PE each fall linearly from 50 to 30 across the day, so a short
    straddle sold at open and covered at close makes exactly 40 points.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")

    rows = []
    for minute in range(375):
        stamp = datetime(DAY.year, DAY.month, DAY.day, 9, 15)
        stamp = stamp.replace(hour=9 + (15 + minute) // 60,
                              minute=(15 + minute) % 60)
        price = 50.0 - 20.0 * (minute / 374.0)
        for opt_type in ("CE", "PE"):
            for level in (0, 4):
                rows.append({
                    "ts": stamp, "underlying": "NIFTY", "expiry": None, "series": "WEEK",
                    "strike": 24000.0 + level * 50, "opt_type": opt_type,
                    "moneyness": level,
                    "open": price, "high": price, "low": price,
                    "close": price if level == 0 else price / 2,
                    "volume": 1000, "oi": 100000, "iv": 0.12, "spot": 24000.0,
                })
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    return tmp_path


@pytest.fixture()
def trending_lake(tmp_path, monkeypatch):
    """A session where spot rallies 500 points, so the ATM strike keeps moving.

    Options are priced as intrinsic plus a flat 20 points of time value, which
    makes every answer exact arithmetic:

      entry, spot 24000, ATM strike 24000 -> CE 20 + PE 20  = 40 credit
      exit,  spot 24500, same strike      -> CE 520 + PE 20 = 540 to buy back
      short straddle P&L = (40 - 540) x 75 = -37,500

    Before legs were pinned to their entry strike the engine returned exactly
    0.0 here: `moneyness = 0` resolved to a *different strike every minute*, so
    by the close it was pricing the 24500 straddle, which is back at 40. The
    rolling series never moves, which is why the bug produced a Sharpe of 14.7
    on real data.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    step, strikes = 50, [24000 + i * 50 for i in range(11)]

    rows = []
    for minute in range(375):
        stamp = datetime(DAY.year, DAY.month, DAY.day, 9, 15)
        stamp = stamp.replace(hour=9 + (15 + minute) // 60,
                              minute=(15 + minute) % 60)
        spot = 24000.0 + 500.0 * (minute / 374.0)
        atm = round(spot / step) * step
        for strike in strikes:
            for opt_type in ("CE", "PE"):
                intrinsic = (max(spot - strike, 0.0) if opt_type == "CE"
                             else max(strike - spot, 0.0))
                offset = (strike - atm) / step
                rows.append({
                    "ts": stamp, "underlying": "NIFTY", "expiry": None, "series": "WEEK",
                    "strike": float(strike), "opt_type": opt_type,
                    "moneyness": int(offset if opt_type == "CE" else -offset),
                    "open": intrinsic + 20.0, "high": intrinsic + 20.0,
                    "low": intrinsic + 20.0, "close": intrinsic + 20.0,
                    "volume": 1000, "oi": 100000, "iv": 0.12, "spot": spot,
                })
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    return tmp_path


def test_a_leg_holds_the_strike_it_entered(trending_lake):
    """The regression this engine was rewritten for.

    Legs used to be selected by moneyness at *every minute*, so a position
    silently re-struck as spot moved instead of holding what it entered. On
    real data that reported +11,459 for 2024-06-04 — a day the trade actually
    lost 24,019 — because the label `moneyness = 0` covered 34 distinct strikes
    that session. Across 2021-2026 it produced a 92% win rate and a Sharpe of
    14.7 on a naked short straddle, which is the signature of a position that
    cannot lose to a directional move.

    Here the true answer is exact: -37,500. The old engine returned 0.0.
    """
    trade = run(_spec(entry_time=time(9, 15), exit_time=time(15, 29)),
                "NIFTY", DAY, DAY).trades[0]
    assert trade.pnl == pytest.approx(-37_500.0, rel=0.02)


def test_the_entered_strike_is_held_all_session(trending_lake):
    """The mechanism behind the test above, asserted directly: the strike
    reported for a leg must not change between entry and exit."""
    from app.backtest.engine import load_matrix

    spec = _spec(entry_time=time(9, 15), exit_time=time(15, 29))
    columns = load_matrix(spec, "NIFTY", DAY, DAY)
    strikes = columns["ce_p0_strike"]
    assert len(set(strikes.tolist())) == 1
    assert strikes[0] == pytest.approx(24000.0)


def test_restrike_is_available_when_a_strategy_really_rolls(trending_lake):
    """Rolling a tested side back to the money is a real thing an option seller
    does, so the old behaviour stays reachable — but only when asked for.

    With both legs re-striking, the basket sits at the ATM premium all day and
    the P&L collapses to roughly nothing. That is correct *for a strategy that
    rolls*, and catastrophic as a default.
    """
    rolled = run(_spec(entry_time=time(9, 15), exit_time=time(15, 29),
                       legs=[LegSpec("CE", "SELL", 0, restrike=True),
                             LegSpec("PE", "SELL", 0, restrike=True)]),
                 "NIFTY", DAY, DAY).trades[0]
    pinned = run(_spec(entry_time=time(9, 15), exit_time=time(15, 29)),
                 "NIFTY", DAY, DAY).trades[0]

    assert rolled.pnl == pytest.approx(0.0, abs=1.0)
    assert pinned.pnl == pytest.approx(-37_500.0, rel=0.02)


def test_a_restriking_leg_and_a_pinned_leg_coexist(trending_lake):
    """Mixed baskets have to keep the two selection routes separate — the
    columns are distinct, so one leg rolling cannot drag the other with it."""
    trade = run(_spec(entry_time=time(9, 15), exit_time=time(15, 29),
                      legs=[LegSpec("CE", "SELL", 0),
                            LegSpec("PE", "SELL", 0, restrike=True)]),
                "NIFTY", DAY, DAY).trades[0]

    # The pinned call loses 500 points of intrinsic; the rolling put stays at
    # 20 and contributes nothing. So the basket loses ~500 x 75.
    assert trade.pnl == pytest.approx(-37_500.0, rel=0.02)


def test_weekly_and_monthly_series_never_mix(tmp_path, monkeypatch):
    """Both series print at the same minute and strike with a null expiry. If
    the engine did not filter on `series`, the pivot's max() would silently pick
    whichever contract was dearer — a spliced instrument that never existed."""
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    rows = []
    for minute in range(30):
        stamp = datetime(DAY.year, DAY.month, DAY.day, 9, 15 + minute)
        for series, price in (("WEEK", 100.0), ("MONTH", 250.0)):
            for opt_type in ("CE", "PE"):
                rows.append({
                    "ts": stamp, "underlying": "NIFTY", "expiry": None,
                    "series": series, "strike": 24000.0, "opt_type": opt_type,
                    "moneyness": 0, "open": price, "high": price, "low": price,
                    "close": price, "volume": 10, "oi": 100, "iv": 0.1,
                    "spot": 24000.0,
                })
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")

    # min_session_bars=0: this fixture is 30 minutes long by design.
    weekly = run(_spec(entry_time=time(9, 15), exit_time=time(9, 44),
                       min_session_bars=0), "NIFTY", DAY, DAY).trades[0]
    monthly = run(_spec(entry_time=time(9, 15), exit_time=time(9, 44),
                        min_session_bars=0, expiry_flag="MONTH"),
                  "NIFTY", DAY, DAY).trades[0]

    assert weekly.entry_price == pytest.approx(-200.0)
    assert monthly.entry_price == pytest.approx(-500.0)


def _spec(**kwargs) -> StrategySpec:
    base = dict(
        name="t", lot_size=LOT, slippage_points=0.0, costs=costs_mod.FREE,
        entry_time=time(9, 15), exit_time=time(15, 29),
        legs=[LegSpec("CE", "SELL", 0), LegSpec("PE", "SELL", 0)],
    )
    base.update(kwargs)
    return StrategySpec(**base)


# ---------------------------------------------------------------------------
# the sign convention
# ---------------------------------------------------------------------------

def test_a_decaying_short_straddle_makes_money(decaying_lake):
    """The regression that matters. Sold at 100, covered at ~60: +40 points."""
    result = run(_spec(), "NIFTY", DAY, DAY)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.pnl > 0
    assert trade.pnl == pytest.approx(40.0 * LOT, rel=0.02)


def test_the_long_side_is_the_exact_mirror(decaying_lake):
    """Structural check: no costs, no slippage, so the two must net to zero."""
    short = run(_spec(), "NIFTY", DAY, DAY).trades[0]
    long = run(_spec(legs=[LegSpec("CE", "BUY", 0), LegSpec("PE", "BUY", 0)]),
               "NIFTY", DAY, DAY).trades[0]
    assert short.pnl == pytest.approx(-long.pnl, rel=1e-6)


def test_entry_price_is_the_signed_net_premium(decaying_lake):
    """A credit basket enters at a negative net premium, same as the builder."""
    trade = run(_spec(), "NIFTY", DAY, DAY).trades[0]
    assert trade.entry_price == pytest.approx(-100.0, abs=0.5)
    assert trade.exit_price == pytest.approx(-60.0, abs=0.5)


# ---------------------------------------------------------------------------
# costs and slippage
# ---------------------------------------------------------------------------

def test_costs_and_slippage_only_ever_reduce_profit(decaying_lake):
    brokerage_only = costs_mod.CostModel(
        brokerage_per_order=20.0, rates_override=costs_mod.ZERO_RATES)
    clean = run(_spec(), "NIFTY", DAY, DAY).trades[0]
    dirty = run(_spec(slippage_points=1.0, costs=brokerage_only),
                "NIFTY", DAY, DAY).trades[0]
    assert dirty.pnl < clean.pnl
    # Two legs, both ends: 4 points of slippage, plus 4 orders at 20.
    assert dirty.pnl == pytest.approx(clean.pnl - 4.0 * LOT - 80.0, rel=0.01)


def test_slippage_scales_with_lots(decaying_lake):
    """The basket price is lots-weighted, so slippage must be too. Charging it
    per leg regardless of size understated the cost of exactly the trades big
    enough to care — a three-lot leg slips three times as much in rupees."""
    one = run(_spec(slippage_points=1.0,
                    legs=[LegSpec("CE", "SELL", 0)]), "NIFTY", DAY, DAY).trades[0]
    three = run(_spec(slippage_points=1.0,
                      legs=[LegSpec("CE", "SELL", 0, lots=3)]),
                "NIFTY", DAY, DAY).trades[0]
    free_three = run(_spec(legs=[LegSpec("CE", "SELL", 0, lots=3)]),
                     "NIFTY", DAY, DAY).trades[0]

    one_cost = run(_spec(legs=[LegSpec("CE", "SELL", 0)]),
                   "NIFTY", DAY, DAY).trades[0].pnl - one.pnl
    three_cost = free_three.pnl - three.pnl
    assert three_cost == pytest.approx(one_cost * 3, rel=1e-6)


# ---------------------------------------------------------------------------
# statutory charges
# ---------------------------------------------------------------------------

def test_statutory_charges_are_applied_on_top_of_brokerage(decaying_lake):
    """The regression this module was written for: the old model charged
    brokerage alone, so STT — the largest cost a premium seller pays — was
    simply absent."""
    brokerage_only = costs_mod.CostModel(
        brokerage_per_order=20.0, rates_override=costs_mod.ZERO_RATES)
    flat = run(_spec(costs=brokerage_only), "NIFTY", DAY, DAY).trades[0]
    real = run(_spec(costs=costs_mod.CostModel()), "NIFTY", DAY, DAY).trades[0]

    assert flat.charges.stt == 0.0
    assert real.charges.stt > 0.0
    assert real.costs > flat.costs
    assert real.pnl < flat.pnl


def test_costs_grow_with_size_while_brokerage_does_not(decaying_lake):
    """One lot versus ten, on the real schedule. The flat model returned the
    same cost for both, which is what made large sizing look free."""
    def straddle(lots: int):
        return run(_spec(costs=costs_mod.CostModel(), legs=[
            LegSpec("CE", "SELL", 0, lots=lots),
            LegSpec("PE", "SELL", 0, lots=lots),
        ]), "NIFTY", DAY, DAY).trades[0]

    one, ten = straddle(1), straddle(10)

    def proportional(trade) -> float:
        """Everything that is a percentage of turnover — i.e. everything the
        flat model was missing."""
        c = trade.charges
        return c.stt + c.exchange + c.sebi + c.stamp

    assert ten.charges.brokerage == pytest.approx(one.charges.brokerage)
    assert proportional(ten) == pytest.approx(proportional(one) * 10)
    # The totals do not scale ten-fold, because brokerage and its GST stay put.
    # That is the point: at one lot the flat fee dominates and the old model
    # was nearly right; at ten lots it is a minority of the bill and the old
    # model was not.
    assert proportional(one) < one.charges.brokerage
    assert proportional(ten) > ten.charges.brokerage


def test_the_charge_breakdown_reconciles_to_the_total(decaying_lake):
    trade = run(_spec(costs=costs_mod.CostModel()), "NIFTY", DAY, DAY).trades[0]
    itemised = trade.charges.to_dict()

    assert trade.costs == pytest.approx(trade.charges.total)
    assert itemised["total"] == pytest.approx(
        sum(v for k, v in itemised.items() if k != "total"), abs=0.01)


def test_gross_minus_costs_is_the_reported_pnl(decaying_lake):
    trade = run(_spec(costs=costs_mod.CostModel()), "NIFTY", DAY, DAY).trades[0]
    assert trade.pnl == pytest.approx(trade.gross - trade.costs)


def test_per_leg_fills_reconstruct_the_basket_slippage(decaying_lake):
    """The engine applies slippage twice by two different routes — to the
    basket price for P&L, and per leg for charges. They must agree, or the
    charge base drifts away from the price the P&L was computed at.

    Summing the signed per-leg fills has to reproduce the basket fill exactly.
    """
    from app.backtest.engine import _round_trip_fills, load_matrix

    spec = _spec(slippage_points=1.0)
    columns = load_matrix(spec, "NIFTY", DAY, DAY)
    leg_prices = {leg.column: columns[leg.column].astype(float)
                  for leg in spec.legs}
    entry_fills, exit_fills = _round_trip_fills(leg_prices, spec, 0, 10)

    # Basket convention: SELL contributes negatively, BUY positively.
    signed_entry = sum(
        leg.sign * fill.price * leg.lots
        for leg, fill in zip(spec.legs, entry_fills))
    raw_entry = sum(
        leg.sign * leg_prices[leg.column][0] * leg.lots for leg in spec.legs)
    slip_combined = spec.slippage_points * sum(leg.lots for leg in spec.legs)

    assert signed_entry == pytest.approx(raw_entry + slip_combined)

    signed_exit = sum(
        leg.sign * fill.price * leg.lots
        for leg, fill in zip(spec.legs, exit_fills))
    raw_exit = sum(
        leg.sign * leg_prices[leg.column][10] * leg.lots for leg in spec.legs)
    assert signed_exit == pytest.approx(raw_exit - slip_combined)


def test_slippage_hurts_the_long_side_too(decaying_lake):
    """Slippage is not a sign flip — it is a cost in both directions."""
    clean = run(_spec(legs=[LegSpec("CE", "BUY", 0)]), "NIFTY", DAY, DAY).trades[0]
    dirty = run(_spec(legs=[LegSpec("CE", "BUY", 0)], slippage_points=1.0),
                "NIFTY", DAY, DAY).trades[0]
    assert dirty.pnl < clean.pnl


# ---------------------------------------------------------------------------
# the matrix cache
# ---------------------------------------------------------------------------

def test_varying_stop_and_target_reuses_the_loaded_matrix(decaying_lake):
    """The point of the cache. A sweep changes stop/target while the underlying
    bars stay identical, so only the first run may touch the lake."""
    from app.backtest import engine as E

    E.clear_matrix_cache()
    calls = []
    original = E.lake.read
    E.lake.read = lambda fn, *a, **k: (calls.append(1), original(fn, *a, **k))[1]
    try:
        for stop in (1000.0, 2000.0, 3000.0, 4000.0):
            run(_spec(stop_loss=stop), "NIFTY", DAY, DAY)
    finally:
        E.lake.read = original
    assert len(calls) == 1


def test_a_different_leg_set_is_a_different_matrix(decaying_lake):
    from app.backtest import engine as E

    E.clear_matrix_cache()
    run(_spec(), "NIFTY", DAY, DAY)
    run(_spec(legs=[LegSpec("CE", "SELL", 4), LegSpec("PE", "SELL", 4)]),
        "NIFTY", DAY, DAY)
    assert len(E._matrix_cache) == 2


def test_side_and_lots_do_not_split_the_cache(decaying_lake):
    """They weight the basket; they do not change which bars are fetched.
    Splitting on them would halve the hit rate of a long/short sweep."""
    from app.backtest import engine as E

    E.clear_matrix_cache()
    run(_spec(), "NIFTY", DAY, DAY)
    run(_spec(legs=[LegSpec("CE", "BUY", 0, lots=7),
                    LegSpec("PE", "BUY", 0, lots=7)]), "NIFTY", DAY, DAY)
    assert len(E._matrix_cache) == 1


def test_entry_time_splits_the_cache(decaying_lake):
    """It must: the strike each leg pins to is resolved at the entry minute, so
    two entry times can hold different contracts."""
    from app.backtest import engine as E

    E.clear_matrix_cache()
    run(_spec(entry_time=time(9, 20)), "NIFTY", DAY, DAY)
    run(_spec(entry_time=time(11, 0)), "NIFTY", DAY, DAY)
    assert len(E._matrix_cache) == 2


def test_the_cache_is_bounded(decaying_lake):
    from app.backtest import engine as E

    E.clear_matrix_cache()
    for level in range(0, E.MATRIX_CACHE_SIZE + 3):
        run(_spec(legs=[LegSpec("CE", "SELL", level)]), "NIFTY", DAY, DAY)
    assert len(E._matrix_cache) == E.MATRIX_CACHE_SIZE


def test_cached_and_uncached_runs_agree(decaying_lake):
    """A cache that changed an answer would be worse than no cache."""
    from app.backtest import engine as E

    E.clear_matrix_cache()
    cold = run(_spec(), "NIFTY", DAY, DAY).trades[0]
    warm = run(_spec(), "NIFTY", DAY, DAY).trades[0]
    assert cold.pnl == pytest.approx(warm.pnl)
    assert cold.entry_price == pytest.approx(warm.entry_price)


# ---------------------------------------------------------------------------
# what counts as a tradeable session
# ---------------------------------------------------------------------------

@pytest.fixture()
def muhurat_lake(tmp_path, monkeypatch):
    """A Diwali Muhurat session: one hour, 18:15-19:14, and nothing else.

    Real NSE data — the lake holds four of these across 2021-2026. They are not
    ordinary sessions and must not be reported as one.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    rows = []
    for minute in range(60):
        stamp = datetime(DAY.year, DAY.month, DAY.day, 18, 15) + \
            timedelta(minutes=minute)
        for opt_type in ("CE", "PE"):
            rows.append({
                "ts": stamp, "underlying": "NIFTY", "expiry": None,
                "series": "WEEK", "strike": 24000.0, "opt_type": opt_type,
                "moneyness": 0, "open": 50.0, "high": 50.0, "low": 50.0,
                "close": 50.0, "volume": 10, "oi": 100, "iv": 0.1,
                "spot": 24000.0,
            })
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    return tmp_path


def test_an_evening_muhurat_session_is_not_traded(muhurat_lake):
    """A 09:20 entry used to resolve to 18:15 and report a one-hour evening
    session as a full trading day. Four such trades sat in the five-year run,
    all small winners, quietly lifting the win rate."""
    assert run(_spec(entry_time=time(9, 20), exit_time=time(15, 15)),
               "NIFTY", DAY, DAY).trades == []


def test_bars_after_the_close_are_not_tradeable(decaying_lake, tmp_path,
                                                monkeypatch):
    """Dhan serves bars to 15:39; NSE F&O closes at 15:30. An exit time inside
    that window used to fill against them."""
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "late")
    rows = []
    for minute in range(375 + 9):        # 09:15 through 15:39
        stamp = datetime(DAY.year, DAY.month, DAY.day, 9, 15) + \
            timedelta(minutes=minute)
        for opt_type in ("CE", "PE"):
            rows.append({
                "ts": stamp, "underlying": "NIFTY", "expiry": None,
                "series": "WEEK", "strike": 24000.0, "opt_type": opt_type,
                "moneyness": 0, "open": 50.0, "high": 50.0, "low": 50.0,
                "close": 50.0, "volume": 10, "oi": 100, "iv": 0.1,
                "spot": 24000.0,
            })
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")

    trade = run(_spec(entry_time=time(9, 20), exit_time=time(15, 35)),
                "NIFTY", DAY, DAY).trades[0]
    assert trade.exit_ts.astype("datetime64[m]").item().time() <= time(15, 30)


def test_a_short_special_session_is_skipped(muhurat_lake):
    """The guard is a bar count, not a holiday calendar — a stale hard-coded
    list of special days would hide real gaps instead of special sessions."""
    assert run(_spec(min_session_bars=0, entry_time=time(18, 15),
                     exit_time=time(19, 14)), "NIFTY", DAY, DAY).trades == []


# ---------------------------------------------------------------------------
# exits
# ---------------------------------------------------------------------------

def test_mae_and_mfe_stop_at_the_exit(decaying_lake):
    """They describe the period the position was open, not the rest of the day.

    On real data a stop that fired at 09:22 for -7,354 reported an MAE of
    -26,077 — a drawdown that happened hours after the trade was closed. Since
    MAE exists to answer "was my stop too tight", measuring past the exit
    inverts the answer.
    """
    early = run(_spec(target=5.0 * LOT), "NIFTY", DAY, DAY).trades[0]
    assert early.exit_reason == "target"
    # The fixture decays monotonically, so after an early target exit the
    # basket keeps gaining. MFE must not include any of that.
    assert early.max_profit == pytest.approx(early.pnl, rel=0.05)
    full = run(_spec(), "NIFTY", DAY, DAY).trades[0]
    assert full.max_profit > early.max_profit

def test_a_target_exits_early_and_is_reported_as_such(decaying_lake):
    result = run(_spec(target=10.0 * LOT), "NIFTY", DAY, DAY)
    trade = result.trades[0]
    assert trade.exit_reason == "target"
    assert trade.pnl == pytest.approx(10.0 * LOT, rel=0.15)
    assert trade.exit_ts < run(_spec(), "NIFTY", DAY, DAY).trades[0].exit_ts


def test_an_unreachable_stop_leaves_a_time_exit(decaying_lake):
    result = run(_spec(stop_loss=999_999.0), "NIFTY", DAY, DAY)
    assert result.trades[0].exit_reason == "time"


def test_percentage_targets_are_measured_against_the_credit(decaying_lake):
    """25% of a 100-point credit on 75 lots is 25 x 75."""
    trade = run(_spec(target_pct=0.25), "NIFTY", DAY, DAY).trades[0]
    assert trade.exit_reason == "target"
    assert trade.pnl == pytest.approx(0.25 * 100.0 * LOT, rel=0.15)


def test_a_stop_beats_a_target_hit_in_the_same_bar():
    """Within one minute we cannot know which came first; assuming the target
    did is how a backtest flatters itself."""
    from app.backtest.engine import _first_exit
    import numpy as np

    levels = {"stop": -400.0, "target": 400.0}
    path = np.array([0.0, 0.0, 500.0])       # bar 2 clears the target only
    idx, reason = _first_exit(path, levels)
    assert (idx, reason) == (2, "target")

    both = np.array([0.0, -500.0])
    idx, reason = _first_exit(both, levels)
    assert reason == "stop"

    # The tie itself: one bar that is at once past the stop and past the
    # target is impossible on a single number, so it is constructed on the
    # dynamic levels — a trade that ran to +500 and collapsed to -400 in the
    # same minute the trail would have fired.
    tie = np.array([0.0, 500.0, -400.0])
    idx, reason = _first_exit(tie, {"stop": -400.0, "trail": 100.0})
    assert (idx, reason) == (2, "stop")


# ---------------------------------------------------------------------------
# selection and filters
# ---------------------------------------------------------------------------

def test_moneyness_selects_a_different_contract(decaying_lake):
    """The +4 series is priced at half the ATM in the fixture."""
    atm = run(_spec(), "NIFTY", DAY, DAY).trades[0]
    otm = run(_spec(legs=[LegSpec("CE", "SELL", 4), LegSpec("PE", "SELL", 4)]),
              "NIFTY", DAY, DAY).trades[0]
    assert otm.entry_price == pytest.approx(atm.entry_price / 2, rel=0.05)


def test_a_weekday_filter_can_exclude_the_only_session(decaying_lake):
    # 14 Aug 2026 is a Friday (weekday 4).
    assert run(_spec(weekdays=(0,)), "NIFTY", DAY, DAY).trades == []
    assert len(run(_spec(weekdays=(4,)), "NIFTY", DAY, DAY).trades) == 1


def test_an_entry_time_after_the_close_produces_no_trade(decaying_lake):
    assert run(_spec(entry_time=time(16, 0)), "NIFTY", DAY, DAY).trades == []


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def test_an_empty_lake_returns_a_result_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "nothing")
    result = run(_spec(), "NIFTY", DAY, DAY)
    assert result.trades == []
    assert result.stats["trades"] == 0
    assert "backfilled" in result.stats["note"]


def test_stats_are_internally_consistent(decaying_lake):
    result = run(_spec(), "NIFTY", DAY, DAY)
    stats = result.stats
    assert stats["trades"] == stats["wins"] + stats["losses"]
    assert stats["total_pnl"] == pytest.approx(sum(t.pnl for t in result.trades))
    assert stats["max_drawdown"] <= 0


def test_the_result_serialises_for_the_api(decaying_lake):
    payload = run(_spec(), "NIFTY", DAY, DAY).to_dict()
    assert payload["stats"]["trades"] == 1
    assert payload["trades"][0]["exit_reason"] == "time"
    assert isinstance(payload["equity"], list)


# ---------------------------------------------------------------------------
# exit rules beyond a fixed stop and target
#
# Every strategy below is expressible on the hosted platforms and none of them
# were expressible here. The fixture is built so each answer is arithmetic: the
# short straddle runs to +3,000, gives it all back, and one leg misbehaves
# while the other does not.
# ---------------------------------------------------------------------------

@pytest.fixture()
def whipsaw_lake(tmp_path, monkeypatch):
    """A session that goes right and then wrong, asymmetrically.

    CE falls 50 -> 30 by midday and then rallies to 80. PE falls 50 -> 10 all
    day and never troubles anyone. So the short straddle:

      entry   CE 50 + PE 50 = 100 credit
      midday  CE 30 + PE 30 =  60   -> +40 points, +3,000
      close   CE 80 + PE 10 =  90   ->  +10 points,  +750

    Spot rises 24,000 -> 24,200 across the session, which is what the
    conditional-entry filters read.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")

    rows = []
    for minute in range(375):
        stamp = datetime(DAY.year, DAY.month, DAY.day, 9, 15)
        stamp = stamp.replace(hour=9 + (15 + minute) // 60,
                              minute=(15 + minute) % 60)
        if minute <= 187:
            call = 50.0 - 20.0 * (minute / 187.0)
        else:
            call = 30.0 + 50.0 * ((minute - 187) / 187.0)
        put = 50.0 - 40.0 * (minute / 374.0)
        for opt_type, price in (("CE", call), ("PE", put)):
            rows.append({
                "ts": stamp, "underlying": "NIFTY", "expiry": None,
                "series": "WEEK", "strike": 24000.0, "opt_type": opt_type,
                "moneyness": 0, "open": price, "high": price, "low": price,
                "close": price, "volume": 1000, "oi": 100000,
                # Vendors quote IV as a percentage; 12.0 means 12%.
                "iv": 12.0, "spot": 24000.0 + 200.0 * (minute / 374.0),
            })
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    return tmp_path


def test_the_baseline_gives_the_profit_back(whipsaw_lake):
    """Without a trail this is what happens: +3,000 at midday, +750 at close."""
    trade = run(_spec(), "NIFTY", DAY, DAY).trades[0]
    assert trade.exit_reason == "time"
    assert trade.max_profit == pytest.approx(40.0 * LOT, rel=0.02)
    assert trade.pnl == pytest.approx(10.0 * LOT, rel=0.05)


def test_a_trailing_stop_keeps_most_of_the_peak(whipsaw_lake):
    """Give back at most 1,500 from the best it has been: out at +1,500."""
    trade = run(_spec(trail_stop=1500.0), "NIFTY", DAY, DAY).trades[0]
    assert trade.exit_reason == "trail"
    assert trade.pnl == pytest.approx(1500.0, abs=120.0)
    assert trade.pnl > run(_spec(), "NIFTY", DAY, DAY).trades[0].pnl


def test_a_trail_does_not_arm_before_its_trigger(whipsaw_lake):
    """A trail armed from the first minute can fire on ordinary noise before
    the trade has made anything. With a trigger above the peak it never arms
    at all, and the trade runs to the close."""
    armed = run(_spec(trail_stop=1500.0, trail_trigger=1000.0),
                "NIFTY", DAY, DAY).trades[0]
    never = run(_spec(trail_stop=1500.0, trail_trigger=99_000.0),
                "NIFTY", DAY, DAY).trades[0]
    assert armed.exit_reason == "trail"
    assert never.exit_reason == "time"


def test_moving_the_stop_to_breakeven_exits_at_zero(whipsaw_lake):
    """The call alone runs to +1,500 and then all the way back through entry,
    which is the shape this rule exists for. The straddle is not used here
    because the put's decay keeps it profitable to the close, so a breakeven
    stop correctly never fires on it — asserted below.
    """
    call = dict(legs=[LegSpec("CE", "SELL", 0)])
    trade = run(_spec(breakeven_trigger=1000.0, **call), "NIFTY", DAY, DAY).trades[0]
    assert trade.exit_reason == "breakeven"
    assert trade.pnl == pytest.approx(0.0, abs=120.0)

    # And it must not fire on a trade that never comes back through entry.
    survives = run(_spec(breakeven_trigger=1000.0), "NIFTY", DAY, DAY).trades[0]
    assert survives.exit_reason == "time"


def test_a_per_leg_stop_closes_the_whole_position(whipsaw_lake):
    """'25% SL on each leg' cannot be said with a combined stop: a straddle
    whose call has run away while the put decayed sits flat overall."""
    trade = run(_spec(per_leg_stop_pct=0.2), "NIFTY", DAY, DAY).trades[0]
    assert trade.exit_reason == "leg stop"
    # The call breaches 20% above its 50-point entry, at 60.
    assert trade.exit_ts < run(_spec(), "NIFTY", DAY, DAY).trades[0].exit_ts


def test_a_per_leg_stop_can_close_only_the_breaching_leg(whipsaw_lake):
    """What the hosted builders do by default, and what most people mean: the
    call is closed at its stop and the put is left to keep decaying."""
    both = run(_spec(per_leg_stop_pct=0.2, per_leg_action="all"),
               "NIFTY", DAY, DAY).trades[0]
    one = run(_spec(per_leg_stop_pct=0.2, per_leg_action="leg"),
              "NIFTY", DAY, DAY).trades[0]
    assert one.pnl > both.pnl


def test_a_leg_that_never_breaches_is_never_stopped(whipsaw_lake):
    """The put only ever falls, so a per-leg stop must not touch it."""
    quiet = run(_spec(legs=[LegSpec("PE", "SELL", 0)], per_leg_stop_pct=0.2),
                "NIFTY", DAY, DAY).trades[0]
    assert quiet.exit_reason == "time"


def test_re_entry_goes_again_after_a_target(decaying_lake):
    """One trade per day was a hard limit, not a modelling choice."""
    once = run(_spec(target=750.0), "NIFTY", DAY, DAY)
    again = run(_spec(target=750.0, re_entries=2, re_entry_on="target"),
                "NIFTY", DAY, DAY)
    assert len(once.trades) == 1
    assert len(again.trades) == 3
    assert [t.attempt for t in again.trades] == [0, 1, 2]
    assert again.trades[0].day == again.trades[2].day


def test_re_entry_stops_at_its_budget(decaying_lake):
    result = run(_spec(target=200.0, re_entries=1, re_entry_on="target"),
                 "NIFTY", DAY, DAY)
    assert len(result.trades) == 2


def test_re_entry_does_not_fire_on_a_reason_it_was_not_asked_for(decaying_lake):
    """Asked to go again after a stop, a trade that ended on target must not."""
    result = run(_spec(target=750.0, re_entries=2, re_entry_on="stop"),
                 "NIFTY", DAY, DAY)
    assert len(result.trades) == 1


def test_an_iv_filter_can_exclude_the_session(whipsaw_lake):
    """ATM IV is 12%. The filter reads decimals, as every other rate here does."""
    assert run(_spec(max_atm_iv=0.20), "NIFTY", DAY, DAY).trades
    assert run(_spec(max_atm_iv=0.10), "NIFTY", DAY, DAY).trades == []
    assert run(_spec(min_atm_iv=0.15), "NIFTY", DAY, DAY).trades == []


def test_a_move_filter_reads_spot_at_the_entry_minute(whipsaw_lake):
    """Spot is up ~0.5% by 13:00, so a 0.4% floor lets the trade through and a
    0.6% floor does not. Both are observable at entry — a filter that needed
    the close would be lookahead."""
    late = dict(entry_time=time(13, 0))
    assert run(_spec(day_move_pct_min=0.4, **late), "NIFTY", DAY, DAY).trades
    assert run(_spec(day_move_pct_min=0.6, **late), "NIFTY", DAY, DAY).trades == []


def test_filtered_days_are_counted_rather_than_vanishing(whipsaw_lake):
    result = run(_spec(min_atm_iv=0.99), "NIFTY", DAY, DAY)
    assert result.skipped["filtered"] == 1
