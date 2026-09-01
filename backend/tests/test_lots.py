"""Lot sizes that change, and the promise that turning them on is opt-in.

The exchange revises the contract multiplier — NIFTY went 25, then 75, then 65
inside two years — and a backtest's P&L scales linearly with it. So this is
worth getting right, and it is also worth **not** applying by surprise: every
backtest already on disk was produced with a fixed lot size, and the guarantee
that a stored run re-runs to the rupee is what makes those records mean
anything. The first test below is the one that protects that.
"""

from datetime import date, datetime, time, timedelta

import pytest

from app.backtest import costs as costs_mod
from app.backtest import lots
from app.backtest.engine import LegSpec, StrategySpec, run
from app.data import lake
from app.data import schema as sch

SPOT = 24000.0
FIRST = date(2026, 2, 2)


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(lots, "ROOT", tmp_path / "lots")
    lots.clear_cache()

    rows, day = [], FIRST
    for _ in range(10):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        for minute in range(375):
            stamp = datetime(day.year, day.month, day.day, 9, 15) \
                + timedelta(minutes=minute)
            decay = 1.0 - minute / 500.0
            for level in range(-2, 3):
                for opt_type in ("CE", "PE"):
                    strike = (SPOT + level * 50 if opt_type == "CE"
                              else SPOT - level * 50)
                    price = max((100.0 - 10.0 * level) * decay, 1.0)
                    rows.append({
                        "ts": stamp, "underlying": "NIFTY", "expiry": None,
                        "series": "WEEK", "strike": float(strike),
                        "opt_type": opt_type, "moneyness": level,
                        "open": price, "high": price, "low": price,
                        "close": round(price, 2), "volume": 1000, "oi": 1000,
                        "iv": 13.0, "spot": SPOT,
                    })
        day += timedelta(days=1)
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    return tmp_path


def _spec(**kwargs) -> StrategySpec:
    base = dict(name="t", legs=[LegSpec("CE", "SELL", 0)], lot_size=75,
                slippage_points=0.0, costs=costs_mod.FREE,
                entry_time=time(9, 20), exit_time=time(15, 15))
    base.update(kwargs)
    return StrategySpec(**base)


# ---------------------------------------------------------------------------
# the guarantee
# ---------------------------------------------------------------------------

def test_the_calendar_is_off_by_default(board):
    """The whole point. Every stored run was produced without this, and a
    default that changed them would rewrite history silently."""
    assert _spec().lot_calendar is False
    a = run(_spec(), "NIFTY", FIRST, FIRST + timedelta(days=20))
    b = run(_spec(lot_calendar=False), "NIFTY", FIRST, FIRST + timedelta(days=20))
    assert a.stats["total_pnl"] == b.stats["total_pnl"]


def test_a_spec_round_trips_the_flag(board):
    """A saved run that dropped this would re-run at a different size and
    report a different P&L under the same id."""
    spec = _spec(lot_calendar=True)
    assert StrategySpec.from_dict(spec.to_dict()).lot_calendar is True


def test_a_legacy_spec_without_the_flag_stays_off():
    payload = _spec().to_dict()
    payload.pop("lot_calendar", None)
    assert StrategySpec.from_dict(payload).lot_calendar is False


# ---------------------------------------------------------------------------
# resolving a size
# ---------------------------------------------------------------------------

def test_the_size_in_force_is_the_one_that_applies():
    assert lots.size_on("NIFTY", date(2025, 6, 1), 999) == (75, True)
    assert lots.size_on("NIFTY", date(2026, 6, 1), 999) == (65, True)
    assert lots.size_on("NIFTY", date(2024, 11, 1), 999) == (25, True)


def test_a_date_before_the_record_is_flagged_not_guessed():
    """The lake starts 2021-08 and this record starts 2024-10, so most of
    history is genuinely unknown. Returning the fallback quietly would make a
    five-year run look sized when four years of it were assumed."""
    size, known = lots.size_on("NIFTY", date(2022, 3, 1), 75)
    assert (size, known) == (75, False)


def test_an_unknown_underlying_falls_back_without_error():
    assert lots.size_on("BANKEX", date(2026, 1, 1), 30) == (30, False)


def test_a_refreshed_file_overrides_the_built_in_table(board):
    lots.save("NIFTY", [(date(2020, 1, 1), 50)])
    assert lots.size_on("NIFTY", date(2026, 6, 1), 999) == (50, True)


# ---------------------------------------------------------------------------
# what it does to a run
# ---------------------------------------------------------------------------

def test_sizing_by_calendar_changes_pnl_in_proportion(board):
    """February 2026 is lot 65, so a run pinned at 75 must report exactly
    75/65 of it — linear, because P&L is quantity times points."""
    fixed = run(_spec(lot_size=75), "NIFTY", FIRST, FIRST + timedelta(days=20))
    dated = run(_spec(lot_size=75, lot_calendar=True), "NIFTY",
                FIRST, FIRST + timedelta(days=20))
    assert dated.stats["total_pnl"] == pytest.approx(
        fixed.stats["total_pnl"] * 65 / 75, rel=1e-5)


def test_assumed_sessions_are_counted(board):
    """A count of zero here and a count of a thousand describe very different
    runs, and nothing else in the result distinguishes them."""
    dated = run(_spec(lot_calendar=True), "NIFTY", FIRST, FIRST + timedelta(days=20))
    assert dated.skipped["lot_size_assumed"] == 0

    lots.save("NIFTY", [(date(2099, 1, 1), 50)])       # record starts after
    later = run(_spec(lot_calendar=True), "NIFTY", FIRST, FIRST + timedelta(days=20))
    assert later.skipped["lot_size_assumed"] == len(later.trades)


def test_the_report_says_when_it_was_guessing(board):
    from app.backtest import report as rep

    lots.save("NIFTY", [(date(2099, 1, 1), 50)])
    result = run(_spec(lot_calendar=True), "NIFTY", FIRST, FIRST + timedelta(days=20))
    built = rep.build(result, _spec(lot_calendar=True), "NIFTY",
                      FIRST, FIRST + timedelta(days=20))
    text = rep.to_markdown(built, "001", "t")
    assert "fallback lot size" in text
    assert built["assumptions"]["lot_size_assumed_sessions"] > 0


def test_a_blip_lasting_one_expiry_is_not_a_revision():
    """A monthly listed before a change expires with the old size while the
    weeklies around it carry the new one. Recording that as two revisions
    would put a one-week hole in every calendar."""
    observed = [(date(2025, 1, 2), 75), (date(2025, 1, 9), 75),
                (date(2025, 1, 30), 25),      # the stale monthly
                (date(2025, 2, 6), 75), (date(2025, 2, 13), 75)]
    kept = []
    previous = None
    for i, (expiry, size) in enumerate(observed):
        if size == previous:
            continue
        window = observed[i:i + lots._MIN_CONSECUTIVE]
        if len(window) == lots._MIN_CONSECUTIVE and any(s != size for _, s in window):
            continue
        kept.append((expiry, size))
        previous = size
    assert kept == [(date(2025, 1, 2), 75)]
