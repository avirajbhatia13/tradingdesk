"""Holding a position across sessions, and the expiry calendar that makes it safe.

The lake stores a *rolling* series: `WEEK` means "whatever the front weekly is
on this date", so the same strike a week later is a different contract. An
intraday backtest never notices. A positional one that did not notice would
splice two contracts into one price path and report a P&L for a position nobody
could have held — plausible numbers, no error, which is the only kind of bug
that costs money here.

So the expiry boundaries are recovered from price and implied vol, and the tests
below are in two halves: that the recovery is exact against a lake built with
known expiries, and that no hold ever crosses one.
"""

from datetime import date, datetime, time, timedelta

import pytest

from app.backtest import costs as costs_mod
from app.backtest import expiries
from app.backtest.engine import LegSpec, StrategySpec, run
from app.data import lake
from app.data import schema as sch
from app.quant import greeks as gk

LOT = 75
SPOT = 24000.0
# Three weekly cycles, Monday to Friday, expiring each Thursday.
FIRST = date(2026, 1, 5)          # a Monday
CYCLE = 5                          # sessions per week in the fixture


def _sessions(weeks: int = 3) -> list[date]:
    out = []
    day = FIRST
    for _ in range(weeks * CYCLE):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append(day)
        day += timedelta(days=1)
    return out


def _expiry_for(day: date) -> date:
    """Thursday of that day's week."""
    return day + timedelta(days=(3 - day.weekday()))


@pytest.fixture()
def weekly_lake(tmp_path, monkeypatch):
    """A lake whose options are priced from a KNOWN time to expiry.

    Each contract is priced with Black-76 at a vol of 15% and a time that counts
    down to its own Thursday, so the calendar the module recovers can be checked
    against the one the fixture was built from rather than against a guess.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(expiries, "ROOT", tmp_path / "expiries")
    expiries.clear_cache()

    sigma = 0.15
    rows = []
    for day in _sessions():
        expiry = _expiry_for(day)
        # Fridays belong to the NEXT week's contract, which is what produces the
        # jump the detector looks for.
        if day.weekday() == 4:
            expiry = expiry + timedelta(days=7)
        for minute in range(375):
            stamp = datetime(day.year, day.month, day.day, 9, 15) \
                + timedelta(minutes=minute)
            left = max((expiry - day).days - minute / 375.0, 0.02) / 365.0
            for level in range(-2, 3):
                for opt_type in ("CE", "PE"):
                    strike = (SPOT + level * 50 if opt_type == "CE"
                              else SPOT - level * 50)
                    price = gk.b76_price(SPOT, strike, left, sigma, opt_type, 0.0)
                    rows.append({
                        "ts": stamp, "underlying": "NIFTY", "expiry": None,
                        "series": "WEEK", "strike": float(strike),
                        "opt_type": opt_type, "moneyness": level,
                        "open": price, "high": price, "low": price,
                        "close": round(price, 2), "volume": 1000, "oi": 1000,
                        "iv": sigma * 100, "spot": SPOT,
                    })
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    return tmp_path


# ---------------------------------------------------------------------------
# recovering the calendar
# ---------------------------------------------------------------------------

def test_time_to_expiry_is_solvable_from_price_and_vol():
    """The claim the whole module rests on: Black-76 has one unknown left."""
    price = gk.b76_price(SPOT, SPOT, 5 / 365, 0.15, "CE", 0.0)
    solved = expiries._solve_t(price, SPOT, SPOT, 0.15, "CE")
    assert solved == pytest.approx(5 / 365, rel=1e-3)


def test_a_worthless_option_has_no_recoverable_time():
    assert expiries._solve_t(0.0, SPOT, SPOT, 0.15, "CE") is None
    assert expiries._solve_t(50.0, SPOT, SPOT, 0.0, "CE") is None


def test_the_cycles_recovered_are_the_ones_the_lake_was_built_with(weekly_lake):
    found = expiries.cycles("NIFTY", "WEEK")
    assert [c.expiry.strftime("%A") for c in found[:2]] == ["Thursday", "Thursday"]
    # Mon-Thu is one contract; Friday belongs to the next.
    assert [len(c.sessions) for c in found[:2]] == [4, 5]


def test_expiry_day_is_identified_exactly(weekly_lake):
    thursday = FIRST + timedelta(days=3)
    assert expiries.is_expiry_day("NIFTY", "WEEK", thursday) is True
    assert expiries.is_expiry_day("NIFTY", "WEEK", FIRST) is False


def test_sessions_to_expiry_counts_sessions_not_calendar_days(weekly_lake):
    """Sessions, because that is what decays a position and what 'hold for
    three days' means to anyone saying it."""
    assert expiries.sessions_to_expiry("NIFTY", "WEEK", FIRST) == 3
    assert expiries.sessions_to_expiry("NIFTY", "WEEK",
                                       FIRST + timedelta(days=3)) == 0


def test_the_calendar_is_cached_and_refreshable(weekly_lake):
    first = expiries.cycles("NIFTY", "WEEK")
    assert expiries._path("NIFTY", "WEEK").exists()
    expiries.clear_cache()
    assert [c.expiry for c in expiries.cycles("NIFTY", "WEEK")] == \
           [c.expiry for c in first]


# ---------------------------------------------------------------------------
# holding across sessions
# ---------------------------------------------------------------------------

def _spec(**kwargs) -> StrategySpec:
    base = dict(name="t", lot_size=LOT, slippage_points=0.0, costs=costs_mod.FREE,
                entry_time=time(9, 20), exit_time=time(15, 15),
                legs=[LegSpec("CE", "SELL", 0), LegSpec("PE", "SELL", 0)])
    base.update(kwargs)
    return StrategySpec(**base)


def _range():
    days = _sessions()
    return days[0], days[-1]


def test_intraday_is_still_one_trade_a_session(weekly_lake):
    result = run(_spec(), "NIFTY", *_range())
    assert {t.sessions_held for t in result.trades} == {0}
    assert len(result.trades) == len({t.day for t in result.trades})


def test_a_hold_spans_sessions(weekly_lake):
    result = run(_spec(hold_days=2), "NIFTY", *_range())
    assert result.trades
    assert max(t.sessions_held for t in result.trades) == 2
    for trade in result.trades:
        exit_day = trade.exit_ts.astype("datetime64[D]").astype(object)
        assert exit_day >= trade.day


def test_no_hold_ever_crosses_a_roll(weekly_lake):
    """The property the calendar exists to guarantee. A hold that crossed would
    price one contract with another's bars and never say so."""
    result = run(_spec(hold_days=4), "NIFTY", *_range())
    assert result.trades
    for trade in result.trades:
        exit_day = trade.exit_ts.astype("datetime64[D]").astype(object)
        assert (expiries.expiry_of("NIFTY", "WEEK", trade.day)
                == expiries.expiry_of("NIFTY", "WEEK", exit_day))


def test_a_hold_cut_short_by_expiry_says_so(weekly_lake):
    """Truncation is the safe direction, but a silent truncation would make a
    '4 session hold' quietly mean something else."""
    result = run(_spec(hold_days=4), "NIFTY", *_range())
    truncated = [t for t in result.trades if t.truncated]
    assert truncated
    assert result.skipped["truncated_at_expiry"] == len(truncated)
    assert all(t.sessions_held < 4 for t in truncated)


def test_positions_do_not_overlap(weekly_lake):
    """The next entry is looked for after the current position closes —
    overlapping cohorts would need a matrix column per cohort per leg."""
    result = run(_spec(hold_days=2), "NIFTY", *_range())
    spans = sorted((t.day, t.exit_ts.astype("datetime64[D]").astype(object))
                   for t in result.trades)
    for (_, earlier_exit), (later_entry, _) in zip(spans, spans[1:]):
        assert later_entry > earlier_exit


def test_the_legs_hold_the_strike_they_entered_across_sessions(weekly_lake):
    """The same guarantee the intraday engine gives, over a longer window."""
    result = run(_spec(hold_days=3), "NIFTY", *_range())
    for trade in result.trades:
        assert set(trade.strikes.values()) == {SPOT}


def test_an_exit_rule_can_fire_on_a_later_session(weekly_lake):
    """Exits are the intraday logic over a longer path — nothing is
    re-implemented, so a rule has to work across sessions or the design is wrong.

    Tested with a target rather than a stop because spot is flat in this
    fixture: a short straddle only ever decays here, so a loss stop could never
    fire and the test would pass for the wrong reason.
    """
    loose = run(_spec(hold_days=4), "NIFTY", *_range())
    # Roughly two sessions of decay, so it cannot be reached on the entry day.
    tight = run(_spec(hold_days=4, target=4000.0), "NIFTY", *_range())

    hit = [t for t in tight.trades if t.exit_reason == "target"]
    assert hit, "the target never fired"
    assert any(t.sessions_held >= 1 for t in hit), \
        "every target fired on the entry session; the span is not being used"
    assert sum(t.sessions_held for t in tight.trades) <= \
           sum(t.sessions_held for t in loose.trades)


def test_a_weekday_filter_picks_the_entry_session(weekly_lake):
    result = run(_spec(hold_days=2, weekdays=(0,)), "NIFTY", *_range())
    assert result.trades
    assert {t.day.weekday() for t in result.trades} == {0}


# ---------------------------------------------------------------------------
# combinations that cannot mean anything
# ---------------------------------------------------------------------------

def test_re_entry_and_holding_cannot_both_apply():
    with pytest.raises(ValueError, match="cannot both apply"):
        _spec(hold_days=3, re_entries=1)


def test_a_restriking_leg_cannot_be_held_overnight():
    """Re-striking follows the money by moneyness, and across a roll that label
    points at a different contract entirely."""
    with pytest.raises(ValueError, match="different contract"):
        _spec(hold_days=3, legs=[LegSpec("CE", "SELL", 0, restrike=True)])


def test_a_negative_hold_is_refused():
    with pytest.raises(ValueError, match="negative"):
        _spec(hold_days=-1)
