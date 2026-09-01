"""Choosing a setting from the past and trading it forward blind.

Two claims are load-bearing here and both are tested against arithmetic rather
than plausibility:

1. **No lookahead.** Every fold's training window must end before its test
   window begins. A walk-forward that leaks is worse than no walk-forward,
   because it produces a reassuring number.
2. **Slicing is exact.** The whole module is cheap because a grid cell is run
   once over the full range and its trades are *sliced* by date, rather than
   re-run per fold. That is only legitimate because the engine carries no state
   between sessions, and it is checked here rather than assumed.
"""

from datetime import date, datetime, time, timedelta

import pytest

from app.backtest import costs as costs_mod
from app.backtest import walkforward as wf
from app.backtest.engine import LegSpec, StrategySpec, run
from app.data import lake
from app.data import schema as sch

LOT = 75
START = date(2025, 1, 6)          # a Monday


@pytest.fixture()
def many_sessions(tmp_path, monkeypatch):
    """120 trading days whose character changes halfway through.

    The first half decays quietly, so a tight stop never fires and wins. The
    second half spikes against the seller every day, so a tight stop is the
    only thing that saves it. A setting chosen on the first half and applied to
    the second is exactly the failure walk-forward exists to catch.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")

    rows = []
    day = START
    for session in range(120):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        calm = session < 60
        for minute in range(375):
            stamp = datetime(day.year, day.month, day.day, 9, 15)
            stamp += timedelta(minutes=minute)
            if calm:
                price = 50.0 - 20.0 * (minute / 374.0)
            else:
                price = 50.0 + 40.0 * (minute / 374.0)
            for opt_type in ("CE", "PE"):
                rows.append({
                    "ts": stamp, "underlying": "NIFTY", "expiry": None,
                    "series": "WEEK", "strike": 24000.0, "opt_type": opt_type,
                    "moneyness": 0, "open": price, "high": price,
                    "low": price, "close": price, "volume": 1000,
                    "oi": 100000, "iv": 12.0, "spot": 24000.0,
                })
        day += timedelta(days=1)
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    return tmp_path


def _spec(**kwargs) -> StrategySpec:
    base = dict(name="t", lot_size=LOT, slippage_points=0.0, costs=costs_mod.FREE,
                entry_time=time(9, 15), exit_time=time(15, 29),
                legs=[LegSpec("CE", "SELL", 0), LegSpec("PE", "SELL", 0)])
    base.update(kwargs)
    return StrategySpec(**base)


# ---------------------------------------------------------------------------
# fold construction — where a lookahead bug would live
# ---------------------------------------------------------------------------

def _days(count: int) -> list[date]:
    return [date(2025, 1, 1) + timedelta(days=i) for i in range(count)]


def test_training_always_ends_before_testing_begins():
    """The one bug that would make every number here a lie."""
    for scheme in ("anchored", "rolling"):
        for (train_lo, train_hi), (test_lo, test_hi) in \
                wf._fold_windows(_days(400), 4, scheme):
            assert train_lo <= train_hi < test_lo <= test_hi


def test_test_windows_are_contiguous_and_do_not_overlap():
    windows = wf._fold_windows(_days(400), 4, "anchored")
    tests = [test for _, test in windows]
    assert len(tests) == 4
    for earlier, later in zip(tests, tests[1:]):
        assert earlier[1] < later[0]


def test_anchored_training_expands_and_rolling_does_not():
    anchored = wf._fold_windows(_days(400), 4, "anchored")
    rolling = wf._fold_windows(_days(400), 4, "rolling")
    assert all(train[0] == anchored[0][0][0] for train, _ in anchored)
    assert len({train[0] for train, _ in rolling}) == len(rolling)


def test_the_first_chunk_is_never_traded():
    """There is no history before it to choose a setting from, so it can only
    ever be training."""
    days = _days(400)
    windows = wf._fold_windows(days, 4, "anchored")
    first_test_start = windows[0][1][0]
    assert first_test_start > days[0]


def test_too_few_days_says_so_rather_than_producing_two_trade_folds():
    with pytest.raises(ValueError, match="too few"):
        wf._fold_windows(_days(20), 4, "anchored")


# ---------------------------------------------------------------------------
# the claim the performance rests on
# ---------------------------------------------------------------------------

def test_slicing_by_date_matches_running_on_that_date_range(many_sessions):
    """A grid cell is run once over the whole range and sliced per fold.

    That collapses folds x cells backtests into cells, and it is only valid
    because each trade is one session and the engine carries no state between
    them. If this ever stops being true the whole module silently starts
    reporting a different strategy.
    """
    spec = _spec(stop_loss_pct=0.25)
    everything = run(spec, "NIFTY", START, START + timedelta(days=400))
    days = sorted({t.day for t in everything.trades})
    lo, hi = days[30], days[70]

    sliced = wf._slice(everything.trades, lo, hi)
    directly = run(spec, "NIFTY", lo, hi)

    assert len(sliced) == len(directly.trades)
    assert [t.pnl for t in sliced] == [t.pnl for t in directly.trades]
    assert [t.day for t in sliced] == [t.day for t in directly.trades]


# ---------------------------------------------------------------------------
# the headline
# ---------------------------------------------------------------------------

def test_a_setting_chosen_on_the_wrong_half_is_caught(many_sessions):
    """The fixture changes character at the halfway point, so whatever the
    early folds choose is wrong for the later ones. Efficiency has to register
    that rather than reporting the in-sample number."""
    result = wf.run(_spec(), {"stop_loss_pct": [0.1, 0.25, 0.5]},
                    "NIFTY", START, START + timedelta(days=400), folds=3)
    assert result["folds"] == 3
    assert result["out_of_sample"]["trades"] > 0
    # The hindsight number is the maximum over the grid, so it can never be
    # beaten by a choice made without seeing the whole period.
    assert result["hindsight"]["average"] >= result["out_of_sample"]["average"]


def test_every_out_of_sample_trade_is_counted_once(many_sessions):
    """Stitching the blind periods together must not double-count a day, which
    is what an overlapping window bug would look like in the P&L."""
    result = wf.run(_spec(), {"stop_loss_pct": [0.2, 0.4]},
                    "NIFTY", START, START + timedelta(days=400), folds=3)
    days = [row["day"] for row in result["curve"]]
    assert len(days) == len(set(days))
    assert days == sorted(days)


def test_the_same_walk_forward_always_chooses_the_same_settings(many_sessions):
    """Ties are broken deterministically, so two runs cannot disagree about
    what history would have told you."""
    axes = {"stop_loss_pct": [0.2, 0.3, 0.4]}
    first = wf.run(_spec(), axes, "NIFTY", START, START + timedelta(days=400))
    second = wf.run(_spec(), axes, "NIFTY", START, START + timedelta(days=400))
    assert [w["settings"] for w in first["windows"]] == \
           [w["settings"] for w in second["windows"]]
    assert first["out_of_sample"] == second["out_of_sample"]


def test_a_ranking_metric_is_per_trade_not_a_total(many_sessions):
    """In-sample and out-of-sample windows hold different numbers of trades, so
    ranking on a total would pick the setting that traded most rather than the
    one that traded best."""
    trades = run(_spec(), "NIFTY", START, START + timedelta(days=400)).trades
    half = wf._score(trades[:20], "net_pnl")
    whole = wf._score(trades[:40], "net_pnl")
    # Doubling the window must not double the score.
    assert abs(whole) < abs(half) * 1.9


def test_an_unknown_metric_is_refused():
    with pytest.raises(ValueError, match="unknown selection metric"):
        wf._score([], "vibes")
