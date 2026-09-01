"""The report and the registry.

The report is the artefact every decision gets made from, so the tests here are
mostly about numbers meaning what they claim: that a drawdown which never
recovered says so, that return-on-margin uses margin and not something else,
and that the leg parser refuses ambiguity rather than guessing.
"""

from datetime import date, time, timedelta

import numpy as np
import pytest

from app.backtest import registry
from app.backtest import report as R
from app.backtest.costs import Charges
from app.backtest.engine import LegSpec, StrategySpec, Trade


def _spec(**kwargs) -> StrategySpec:
    base = dict(name="t", legs=[LegSpec("CE", "SELL", 0), LegSpec("PE", "SELL", 0)],
                entry_time=time(9, 20), exit_time=time(15, 15), lot_size=75)
    base.update(kwargs)
    return StrategySpec(**base)


def _trade(day: date, pnl: float) -> Trade:
    return Trade(day=day, entry_ts=np.datetime64(f"{day}T09:20"),
                 exit_ts=np.datetime64(f"{day}T15:15"),
                 entry_price=-100.0, exit_price=-90.0, pnl=pnl,
                 gross=pnl + 120.0, costs=120.0, exit_reason="time",
                 max_profit=abs(pnl), max_loss=-abs(pnl))


# ---------------------------------------------------------------------------
# implied vol units
# ---------------------------------------------------------------------------

def test_percent_implied_vol_is_converted_to_a_decimal():
    """Dhan sends 15.16 meaning 15.16%. Feeding that straight into the pricing
    model scales the SPAN scan by a hundred and produces margins in the crores
    without raising anything."""
    assert R._iv(15.16) == pytest.approx(0.1516)
    assert R._iv(0.0) == R.FALLBACK_IV
    assert R._iv(None) == R.FALLBACK_IV
    assert R._iv(float("nan")) == R.FALLBACK_IV


def test_absurd_implied_vol_is_clamped_not_propagated():
    assert R._iv(100000.0) <= 3.0
    assert R._iv(0.0001) >= 0.02


# ---------------------------------------------------------------------------
# drawdown, which is where reports usually stop too early
# ---------------------------------------------------------------------------

def test_a_recovered_drawdown_reports_its_recovery():
    equity = np.array([0.0, 100.0, 40.0, 20.0, 60.0, 130.0])
    days = [date(2026, 1, d) for d in (1, 2, 3, 4, 5, 6)]
    out = R._drawdown(equity, days)
    assert out["depth"] == pytest.approx(-80.0)
    assert out["recovered"] is True
    assert out["recovery_trades"] == 2


def test_a_drawdown_that_never_recovers_says_so():
    """The distinction that matters. Depth alone reads the same either way, and
    a loss you are still carrying is a different fact from one you climbed out
    of in a fortnight."""
    equity = np.array([0.0, 100.0, 40.0, 20.0, 30.0])
    days = [date(2026, 1, d) for d in (1, 2, 3, 4, 5)]
    out = R._drawdown(equity, days)
    assert out["recovered"] is False
    assert out["recovery_days"] is None
    assert out["still_underwater_trades"] == 1


def test_a_drawdown_that_starts_on_the_first_trade_is_counted_in_full():
    """The account's starting balance is a peak.

    Taking the running peak from the equity curve alone makes the first trade
    unable to contribute to a drawdown: a run opening with three losses of 100
    reported 200, because the peak was read as the equity *after* the first
    loss. It is only ever wrong in the flattering direction, and it is wrong
    about exactly the stretch that decides whether a new strategy survives
    being switched on.
    """
    equity = np.array([-100.0, -200.0, -300.0, -250.0])
    days = [date(2026, 1, d) for d in (1, 2, 3, 4)]
    out = R._drawdown(equity, days)
    assert out["depth"] == pytest.approx(-300.0)
    assert out["peak_trade"] == 0
    assert out["recovered"] is False


def test_the_engine_and_the_report_agree_on_drawdown():
    """Two figures for the same quantity in one document is a bug waiting to
    be argued about, so both go through the same helper."""
    from app.backtest.engine import underwater

    pnls = np.array([-100.0, -200.0, 50.0, 400.0, -600.0])
    equity = np.cumsum(pnls)
    assert float(underwater(equity).min()) == pytest.approx(
        R._drawdown(equity, [date(2026, 1, d) for d in (1, 2, 3, 4, 5)])["depth"])


def test_drawdown_duration_is_measured_in_calendar_days_too():
    equity = np.array([0.0, 50.0, -20.0])
    days = [date(2026, 1, 1), date(2026, 1, 10), date(2026, 3, 1)]
    out = R._drawdown(equity, days)
    assert out["decline_days"] == (date(2026, 3, 1) - date(2026, 1, 10)).days


# ---------------------------------------------------------------------------
# streaks and compounding
# ---------------------------------------------------------------------------

def test_streaks_count_consecutive_runs():
    best, worst = R._streaks([1, 1, 1, -1, -1, 1, -1, -1, -1, -1])
    assert (best, worst) == (3, 4)


def test_cagr_is_smaller_than_the_simple_average_over_multiple_years():
    """499% across five years is 100%/yr simple but 43% compounded. Quoting the
    first as though it were the second overstates a strategy badly."""
    simple = 499.6 / 4.99
    compounded = R._cagr(499.6, 4.99)
    assert compounded < simple
    assert compounded == pytest.approx(43.0, abs=1.0)


def test_cagr_is_undefined_when_capital_would_be_wiped_out():
    assert R._cagr(-100.0, 3.0) is None
    assert R._cagr(-150.0, 3.0) is None


# ---------------------------------------------------------------------------
# the assembled report
# ---------------------------------------------------------------------------

def _result(pnls):
    from app.backtest.engine import Result

    trades = [_trade(date(2026, 1, 1 + i), p) for i, p in enumerate(pnls)]
    return Result(strategy="t", trades=trades,
                  stats={"costs_breakdown": {"total": 120.0 * len(trades)},
                         "exit_reasons": {"time": len(trades)}},
                  bars_scanned=100, elapsed_ms=1.0)


def test_report_headline_uses_net_and_gross_separately():
    out = R.build(_result([1000.0, -400.0, 700.0]), _spec(), "NIFTY",
                  date(2026, 1, 1), date(2026, 1, 3))
    assert out["headline"]["net_pnl"] == pytest.approx(1300.0)
    assert out["headline"]["gross_pnl"] == pytest.approx(1300.0 + 360.0)
    assert out["headline"]["total_charges"] == pytest.approx(360.0)


def test_report_without_margin_reports_no_return_rather_than_a_fake_one():
    """Return needs a denominator. Inventing one would be worse than saying
    it is unavailable."""
    out = R.build(_result([100.0, 200.0]), _spec(), "NIFTY",
                  date(2026, 1, 1), date(2026, 1, 2))
    assert out["headline"]["roi_on_peak_margin_pct"] is None
    assert out["margin"]["samples"] == 0


def test_weekday_and_monthly_buckets_partition_every_trade():
    pnls = [100.0, -50.0, 25.0, 75.0, -10.0]
    out = R.build(_result(pnls), _spec(), "NIFTY",
                  date(2026, 1, 1), date(2026, 1, 5))
    assert sum(b["trades"] for b in out["weekday"]) == len(pnls)
    assert sum(b["trades"] for b in out["monthly"]) == len(pnls)
    assert sum(b["pnl"] for b in out["yearly"]) == pytest.approx(sum(pnls))


def test_an_empty_result_is_a_report_not_an_exception():
    from app.backtest.engine import Result

    out = R.build(Result(strategy="t", stats={"note": "nothing"}), _spec(),
                  "NIFTY", date(2026, 1, 1), date(2026, 1, 2))
    assert out["trades"] == 0


def test_markdown_states_the_assumptions_that_carried_the_result():
    """A report that omits its slippage and charge assumptions invites the
    reader to treat a modelled number as a measured one."""
    out = R.build(_result([500.0, -200.0, 300.0]),
                  _spec(slippage_points=1.25), "NIFTY",
                  date(2026, 1, 1), date(2026, 1, 3))
    text = R.to_markdown(out, "007", "Test strategy")
    assert "007 — Test strategy" in text
    assert "1.25 points" in text
    assert "rates in force on each trade's own date" in text
    assert "Verdict" in text


def test_markdown_flags_a_result_that_charges_turned_negative():
    from app.backtest.engine import Result

    trades = [_trade(date(2026, 1, 1 + i), -50.0) for i in range(3)]
    result = Result(strategy="t", trades=trades,
                    stats={"costs_breakdown": {"total": 360.0},
                           "exit_reasons": {"time": 3}})
    out = R.build(result, _spec(), "NIFTY", date(2026, 1, 1), date(2026, 1, 3))
    text = R.to_markdown(out, "008", "Cost bound")
    assert "cost problem, not a signal problem" in text


# ---------------------------------------------------------------------------
# correlation
# ---------------------------------------------------------------------------

def test_correlation_uses_daily_changes_not_cumulative_equity():
    """Two rising equity curves correlate at ~1.0 however differently they move
    day to day. Correlating the cumulative series would call every profitable
    strategy identical to every other."""
    days = [date(2026, 1, d).isoformat() for d in range(1, 11)]

    def curve(daily):
        total, out = 0.0, []
        for day, step in zip(days, daily):
            total += step
            out.append({"day": day, "equity": total})
        return out

    # Both end up +100 with the same upward trend, but their day-to-day moves
    # are opposites. Correlating cumulative equity would call these identical.
    a = curve([20, 0, 20, 0, 20, 0, 20, 0, 20, 0])
    b = curve([0, 20, 0, 20, 0, 20, 0, 20, 0, 20])

    out = R.correlate({"a": a, "b": b})
    assert out["matrix"]["a"]["a"] == pytest.approx(1.0)
    assert out["matrix"]["a"]["b"] < -0.9
    assert out["matrix"]["a"]["b"] == pytest.approx(out["matrix"]["b"]["a"])


def test_correlation_of_a_flat_series_is_undefined_not_zero():
    days = [date(2026, 1, d).isoformat() for d in range(1, 6)]
    flat = [{"day": d, "equity": 0.0} for d in days]
    moving = [{"day": d, "equity": float(i)} for i, d in enumerate(days)]
    out = R.correlate({"flat": flat, "moving": moving})
    assert out["matrix"]["flat"]["moving"] is None


# ---------------------------------------------------------------------------
# the leg parser
# ---------------------------------------------------------------------------

def test_leg_parser_reads_the_spoken_form():
    from tools.backtest import parse_legs

    legs = parse_legs("SELL CE 0, BUY PE 5, SELL CE 3 x2")
    assert [(l.side, l.opt_type, l.moneyness, l.lots) for l in legs] == [
        ("SELL", "CE", 0, 1), ("BUY", "PE", 5, 1), ("SELL", "CE", 3, 2)]


def test_leg_parser_handles_signs_and_the_roll_flag():
    from tools.backtest import parse_legs

    legs = parse_legs("sell ce -3, SELL PE +5 roll")
    assert legs[0].moneyness == -3 and legs[0].restrike is False
    assert legs[1].moneyness == 5 and legs[1].restrike is True


def test_leg_parser_refuses_ambiguity_instead_of_skipping_it():
    """A silently dropped leg turns a hedged position into a naked one and
    still produces a plausible report — the worst failure available here."""
    from tools.backtest import parse_legs

    for bad in ("SELL XX 0", "SELL CE", "hold CE 0", "SELL CE 0, nonsense", ""):
        with pytest.raises(ValueError):
            parse_legs(bad)


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "ROOT", tmp_path / "backtests")
    return tmp_path / "backtests"


def _save(name="Strategy", pnls=(100.0, -50.0)):
    out = R.build(_result(list(pnls)), _spec(), "NIFTY",
                  date(2026, 1, 1), date(2026, 1, 2))
    return registry.save(name, _spec(), out, R.to_markdown(out, "x", name),
                         "NIFTY", date(2026, 1, 1), date(2026, 1, 2))


def test_ids_increment_and_are_zero_padded(store):
    assert _save("First")["id"] == "001"
    assert _save("Second")["id"] == "002"
    assert _save("Third")["id"] == "003"


def test_ids_are_derived_from_disk_so_a_lost_index_cannot_reuse_one(store):
    """Reusing an id would overwrite a saved result — the one thing this
    module exists to prevent."""
    _save("First"); _save("Second")
    (store / registry.INDEX).unlink()
    assert registry.next_id() == "003"


def test_a_run_round_trips_with_all_three_files(store):
    entry = _save("Round trip")
    directory = store / entry["slug"]
    assert (directory / registry.SPEC_FILE).exists()
    assert (directory / registry.RESULT_FILE).exists()
    assert (directory / registry.REPORT_FILE).exists()

    loaded = registry.load("001")
    assert loaded["spec"]["name"] == "Round trip"
    assert loaded["report"]["headline"]["net_pnl"] == pytest.approx(50.0)
    assert "Round trip" in loaded["markdown"]


def test_a_corrupt_index_is_rebuilt_from_the_run_directories(store):
    _save("First"); _save("Second")
    (store / registry.INDEX).write_text("{ this is not json")
    index = registry.load_index()
    assert [e["id"] for e in index] == ["001", "002"]


def test_archiving_marks_but_never_deletes(store):
    """The record of what was tried is the defence against counting only the
    runs that worked, so nothing here removes evidence."""
    _save("Superseded")
    assert registry.archive("001") is True
    assert registry.load_index()[0]["archived"] is True
    assert registry.load("001") is not None


def test_slugs_stay_filesystem_safe(store):
    entry = _save("Straddle 9:20 → 15:15 (v2) / 30% SL")
    assert "/" not in entry["slug"] and " " not in entry["slug"]
    assert (store / entry["slug"]).is_dir()


# ---------------------------------------------------------------------------
# shortlisting runs to compare
#
# Correlating everything stops being useful past a handful of runs — the
# question is always about a few candidates you are deciding between.
# ---------------------------------------------------------------------------

def test_correlation_can_be_restricted_to_a_shortlist(store):
    import json

    for run_id, name, pnls in (("001", "alpha", [10.0, -5.0, 8.0, -2.0]),
                               ("002", "beta", [-9.0, 6.0, -7.0, 3.0]),
                               ("003", "gamma", [4.0, 4.0, 4.0, 4.0])):
        directory = registry.ROOT / f"{run_id}-{name}"
        directory.mkdir(parents=True)
        running, curve = 0.0, []
        for i, pnl in enumerate(pnls):
            running += pnl
            curve.append({"day": f"2026-01-0{i + 1}", "equity": running})
        (directory / registry.SPEC_FILE).write_text(
            json.dumps({"name": name, "underlying": "NIFTY",
                        "start": "2026-01-01", "end": "2026-01-04"}))
        (directory / registry.RESULT_FILE).write_text(
            json.dumps({"curves": {"equity": curve}}))
    registry.rebuild_index()

    assert len(registry.equity_curves()) == 3
    picked = registry.equity_curves(["001", "003"])
    assert len(picked) == 2
    assert all(name.startswith(("001", "003")) for name in picked)


def test_a_shortlist_accepts_unpadded_ids(store):
    """The dashboard sends whatever the row carried; a '1' that silently
    matched nothing would show an empty matrix and look like a bug in the
    correlation rather than in the id."""
    import json

    directory = registry.ROOT / "001-alpha"
    directory.mkdir(parents=True)
    (directory / registry.SPEC_FILE).write_text(json.dumps({"name": "alpha"}))
    (directory / registry.RESULT_FILE).write_text(json.dumps(
        {"curves": {"equity": [{"day": "2026-01-01", "equity": 1.0},
                               {"day": "2026-01-02", "equity": 2.0}]}}))
    registry.rebuild_index()
    assert len(registry.equity_curves(["1"])) == 1


def test_a_walk_forward_curve_is_correlatable(store):
    """Its out-of-sample record is the *more* interesting thing to correlate
    against a plain backtest, so a different shape on disk must not exclude it."""
    import json

    directory = registry.ROOT / "001-alpha-walkforward"
    directory.mkdir(parents=True)
    (directory / registry.SPEC_FILE).write_text(
        json.dumps({"name": "alpha", "kind": "walkforward"}))
    (directory / registry.RESULT_FILE).write_text(json.dumps({
        "kind": "walkforward",
        "curve": [{"day": "2026-01-01", "equity": 1.0},
                  {"day": "2026-01-02", "equity": 3.0}]}))
    registry.rebuild_index()
    assert len(registry.equity_curves()) == 1


def test_a_bookmark_survives_an_index_rebuild(store):
    """Stars live only in the index, so a routine rebuild would otherwise wipe
    every shortlist — which would make the feature untrustworthy."""
    import json

    directory = registry.ROOT / "001-alpha"
    directory.mkdir(parents=True)
    (directory / registry.SPEC_FILE).write_text(json.dumps({"name": "alpha"}))
    (directory / registry.RESULT_FILE).write_text(json.dumps({"headline": {}}))
    registry.rebuild_index()

    assert registry.star("001") is True
    assert [e["starred"] for e in registry.load_index()] == [True]
    registry.rebuild_index()
    assert [e["starred"] for e in registry.load_index()] == [True]

    assert registry.star("001", False) is True
    assert [e["starred"] for e in registry.load_index()] == [False]


def test_starring_a_run_that_does_not_exist_says_so(store):
    assert registry.star("999") is False


# ---------------------------------------------------------------------------
# per-day detail and the data-quality flags
# ---------------------------------------------------------------------------

def _quality_trade(day: date, pnl: float, *, bars: int = 300,
                   flat: int = 0, missing: int = 0) -> Trade:
    trade = _trade(day, pnl)
    trade.bars, trade.flat_bars, trade.missing_bars = bars, flat, missing
    return trade


def test_a_day_carries_its_own_charges_not_just_the_total():
    """The whole reason the day block exists is to answer "why was this day
    worth this much", and charges are frequently the answer — they are larger
    than the edge on the baseline straddle."""
    trade = _quality_trade(date(2026, 1, 5), 1000.0)
    trade.charges = Charges(brokerage=80.0, stt=7.5, gst=16.0)
    days, _ = R._daily([trade])

    assert days[0]["charge_breakdown"]["brokerage"] == 80.0
    assert days[0]["charge_breakdown"]["stt"] == 7.5
    # `total` is dropped from the breakdown because `charges` already is it.
    assert "total" not in days[0]["charge_breakdown"]


def test_the_clock_is_read_off_the_timestamp_not_sliced_off_its_tail():
    """`str(numpy.datetime64)` ends in nanoseconds, so slicing from the right
    yields '00000' rather than a time — and it looked plausible enough in the
    UI to survive a review."""
    days, _ = R._daily([_quality_trade(date(2026, 1, 5), 1000.0)])
    assert days[0]["entry"] == "09:20"
    assert days[0]["exit"] == "15:15"


def test_a_quiet_day_is_flagged_stale_and_an_ordinary_one_is_not():
    """A repeated last traded price is indistinguishable from a quiet market in
    the P&L, which is exactly why it needs surfacing rather than filtering."""
    ordinary = [_quality_trade(date(2026, 1, d), 500.0) for d in range(1, 10)]
    stale = _quality_trade(date(2026, 1, 12), 500.0, bars=300, flat=250)
    days, quality = R._daily(ordinary + [stale])

    assert [d["flags"] for d in days[:9]] == [[]] * 9
    assert "stale" in days[9]["flags"]
    assert days[9]["stale_pct"] == pytest.approx(83.3, abs=0.1)
    assert quality["stale"] == 1


def test_a_day_with_a_silent_leg_is_flagged_gapped():
    """A minute only counts when every leg printed, so a gapped day is one the
    exit logic could not fully see — the stop may have been breached in a
    minute that carried no price at all."""
    days, quality = R._daily(
        [_quality_trade(date(2026, 1, d), 500.0) for d in range(1, 10)]
        + [_quality_trade(date(2026, 1, 12), 500.0, bars=300, missing=90)])

    assert "gapped" in days[9]["flags"]
    assert quality["gapped"] == 1


def test_an_outlier_is_measured_against_the_median_not_the_mean():
    """A handful of huge days drags a mean-and-standard-deviation threshold out
    far enough to cover themselves, so the day that most needs flagging is the
    one that escapes."""
    trades = [_quality_trade(date(2026, 1, 4) + timedelta(days=i),
                             500.0 + (i % 7) * 120.0) for i in range(30)]
    trades.append(_quality_trade(date(2026, 3, 2), -80000.0))
    days, quality = R._daily(trades)

    assert days[-1]["flags"] == ["outlier"]
    assert sum(1 for d in days if d["flags"]) == 1
    # Stated as the counterfactual, which is the readable form: a share of a
    # near-zero net is a meaningless percentage.
    assert quality["net_excluding_flagged"] == pytest.approx(
        sum(t.pnl for t in trades[:30]))


def test_identical_days_produce_no_outliers_rather_than_dividing_by_zero():
    """A zero MAD is the degenerate case — every day identical — and it must
    flag nothing rather than flag everything."""
    days, quality = R._daily(
        [_quality_trade(date(2026, 1, 4) + timedelta(days=i), 500.0)
         for i in range(10)])

    assert quality["outlier_cutoff"] is None
    assert quality["flagged"] == 0


def test_a_catastrophe_is_still_flagged_when_most_days_are_identical():
    """A stop- or target-driven strategy books the same rupee figure most days,
    which puts the MAD at zero — and a zero threshold flags nothing, precisely
    when a disaster is sitting in the tail. The standard deviation takes over."""
    trades = [_quality_trade(date(2026, 1, 4) + timedelta(days=i), 500.0)
              for i in range(30)]
    trades.append(_quality_trade(date(2026, 3, 2), -80000.0))
    days, quality = R._daily(trades)

    assert days[-1]["flags"] == ["outlier"]
    assert sum(1 for d in days if d["flags"]) == 1
    assert quality["net_excluding_flagged"] == pytest.approx(30 * 500.0)
