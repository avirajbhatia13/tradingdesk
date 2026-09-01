"""The strategy library — definitions, as distinct from the attempts.

The load-bearing idea is that a strategy never *claims* to be validated. It
carries a fingerprint, saved runs are matched to it, and what it is allowed to
say is derived from whatever evidence exists. These tests are mostly about that
derivation being impossible to fake: a strategy with no walk-forward must not
read as ready, and one whose walk-forward *failed* must read worse than one
that was never tested at all.
"""

from datetime import date, time

import pytest

from app.backtest import library, registry
from app.backtest import report as R
from app.backtest.engine import LegSpec, StrategySpec, Result, Trade
from app.backtest import selectors as sel

import numpy as np


@pytest.fixture()
def shelf(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "ROOT", tmp_path / "backtests")
    monkeypatch.setattr(library, "ROOT", tmp_path / "strategies")
    return tmp_path


def _spec(**kwargs) -> StrategySpec:
    base = dict(name="t", legs=[LegSpec("CE", "SELL", 0), LegSpec("PE", "SELL", 0)],
                entry_time=time(9, 20), exit_time=time(15, 15), lot_size=75)
    base.update(kwargs)
    return StrategySpec(**base)


def _save_run(name: str, spec: StrategySpec, pnls=(100.0, -50.0)) -> str:
    trades = [
        Trade(day=date(2026, 1, 1 + i), entry_ts=np.datetime64("2026-01-01T09:20"),
              exit_ts=np.datetime64("2026-01-01T15:15"), entry_price=-100.0,
              exit_price=-90.0, pnl=p, gross=p, costs=0.0, exit_reason="time",
              max_profit=abs(p), max_loss=-abs(p))
        for i, p in enumerate(pnls)
    ]
    result = Result(strategy=name, trades=trades,
                    stats={"costs_breakdown": {}, "exit_reasons": {}})
    report = R.build(result, spec, "NIFTY", date(2026, 1, 1), date(2026, 1, 5))
    entry = registry.save(name, spec, report, "md", "NIFTY",
                          date(2026, 1, 1), date(2026, 1, 5))
    return entry["id"]


# ---------------------------------------------------------------------------
# the fingerprint — what makes two runs "the same strategy"
# ---------------------------------------------------------------------------

def test_the_same_strategy_at_a_different_stop_has_one_fingerprint():
    """A run at a 25% stop, a sweep across six stops and a walk-forward
    choosing between them are three pieces of evidence about ONE strategy. A
    fingerprint that included the stop would file them as three strangers."""
    tight = _spec(stop_loss_pct=0.25).to_dict()
    wide = _spec(stop_loss_pct=0.5, target_pct=0.3, trail_stop=2000.0).to_dict()
    none = _spec().to_dict()
    prints = {library.fingerprint(s, "NIFTY") for s in (tight, wide, none)}
    assert len(prints) == 1


@pytest.mark.parametrize("changed", [
    {"legs": [LegSpec("CE", "SELL", 3), LegSpec("PE", "SELL", 3)]},
    {"entry_time": time(10, 0)},
    {"exit_time": time(14, 0)},
    {"weekdays": (3,)},
    {"expiry_flag": "MONTH"},
    {"lot_size": 35},
])
def test_changing_what_the_strategy_is_changes_the_fingerprint(changed):
    base = library.fingerprint(_spec().to_dict(), "NIFTY")
    assert library.fingerprint(_spec(**changed).to_dict(), "NIFTY") != base


def test_a_different_underlying_is_a_different_strategy():
    spec = _spec().to_dict()
    assert library.fingerprint(spec, "NIFTY") != library.fingerprint(spec, "BANKNIFTY")


def test_the_selection_rule_is_part_of_the_identity():
    """"Sell the 20 delta" and "sell 5 strikes out" are different strategies
    even on days they happen to land on the same contract."""
    delta = _spec(legs=[LegSpec("CE", "SELL", select=sel.ByDelta(0.2))])
    strikes = _spec(legs=[LegSpec("CE", "SELL", select=sel.ByMoneyness(5))])
    assert (library.fingerprint(delta.to_dict(), "NIFTY")
            != library.fingerprint(strikes.to_dict(), "NIFTY"))


# ---------------------------------------------------------------------------
# promoting a run
# ---------------------------------------------------------------------------

def test_a_run_can_be_promoted_into_the_library(shelf):
    run_id = _save_run("straddle", _spec(stop_loss_pct=0.25))
    record = library.from_run(run_id, name="ATM straddle", lots=3)
    assert record["name"] == "ATM straddle"
    assert record["lots"] == 3
    assert record["status"] == "draft"
    assert library.load(record["id"])["spec"]["legs"][0]["opt_type"] == "CE"


def test_promoting_leaves_the_run_exactly_where_it_was(shelf):
    run_id = _save_run("straddle", _spec())
    library.from_run(run_id, name="kept")
    assert registry.load(run_id) is not None
    assert [e["id"] for e in registry.load_index()] == [run_id]


def test_a_sweep_cannot_be_promoted(shelf):
    """A sweep varies a setting rather than fixing one, so there is no single
    definition in it to deploy."""
    registry.save_sweep("grid", _spec(), {"summary": {}}, "md", "NIFTY",
                        date(2026, 1, 1), date(2026, 1, 5))
    with pytest.raises(ValueError, match="sweep"):
        library.from_run("001")


def test_promoting_a_run_that_does_not_exist_says_so(shelf):
    with pytest.raises(ValueError, match="no saved run"):
        library.from_run("999")


# ---------------------------------------------------------------------------
# evidence, which is derived and cannot be asserted
# ---------------------------------------------------------------------------

def test_a_strategy_with_only_a_backtest_is_not_ready(shelf):
    run_id = _save_run("straddle", _spec(stop_loss_pct=0.25))
    record = library.from_run(run_id, name="straddle")
    evidence = library.evidence(record)
    assert evidence["runs"] == [run_id]
    assert evidence["ready"] is False
    assert [c["done"] for c in evidence["checks"]] == [True, False, False]


def test_a_sweep_on_the_same_definition_is_found_automatically(shelf):
    """No manual linking: the sweep is filed against the strategy because it
    tests the same definition, which is what the fingerprint is for."""
    run_id = _save_run("straddle", _spec(stop_loss_pct=0.25))
    record = library.from_run(run_id, name="straddle")
    registry.save_sweep("straddle grid", _spec(), {"summary": {"cells": 9}},
                        "md", "NIFTY", date(2026, 1, 1), date(2026, 1, 5))
    evidence = library.evidence(record)
    assert evidence["sweeps"] == ["002"]
    assert evidence["checks"][1]["done"] is True


def test_a_run_on_a_different_definition_is_not_counted(shelf):
    run_id = _save_run("straddle", _spec())
    record = library.from_run(run_id, name="straddle")
    _save_run("strangle", _spec(legs=[LegSpec("CE", "SELL", 5),
                                      LegSpec("PE", "SELL", 5)]))
    assert library.evidence(record)["runs"] == [run_id]


def test_a_failed_walk_forward_is_flagged_rather_than_ticked(shelf):
    """This is the whole point. A walk-forward that ran and failed is *worse*
    evidence than none — reading it as a completed check would turn the most
    important warning in the system into a tick."""
    run_id = _save_run("straddle", _spec(stop_loss_pct=0.25))
    record = library.from_run(run_id, name="straddle")
    registry.save_walkforward(
        "straddle blind", _spec(), {"efficiency": 0.36, "folds": 4,
                                    "out_of_sample": {"net_pnl": 100.0}},
        "md", "NIFTY", date(2026, 1, 1), date(2026, 1, 5))
    check = library.evidence(record)["checks"][2]
    assert check["done"] is False
    assert check["warn"] is True
    assert "36%" in check["detail"]


def test_a_walk_forward_that_held_up_counts(shelf):
    run_id = _save_run("straddle", _spec(stop_loss_pct=0.25))
    record = library.from_run(run_id, name="straddle")
    registry.save_walkforward(
        "straddle blind", _spec(), {"efficiency": 0.82, "folds": 4,
                                    "out_of_sample": {"net_pnl": 100.0}},
        "md", "NIFTY", date(2026, 1, 1), date(2026, 1, 5))
    evidence = library.evidence(record)
    assert evidence["checks"][2]["done"] is True
    assert evidence["checks"][2].get("warn") is not True


# ---------------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------------

def test_status_and_size_are_editable(shelf):
    record = library.from_run(_save_run("s", _spec()), name="s")
    updated = library.update(record["id"], status="paper", lots=5, notes="hi")
    assert (updated["status"], updated["lots"], updated["notes"]) == ("paper", 5, "hi")


def test_the_definition_is_not_editable_in_place(shelf):
    """A changed definition is a different strategy. Editing one in place would
    silently detach it from every run that tested it."""
    record = library.from_run(_save_run("s", _spec()), name="s")
    with pytest.raises(ValueError, match="different strategy"):
        library.update(record["id"], spec={"legs": []})


def test_an_unknown_status_is_refused(shelf):
    record = library.from_run(_save_run("s", _spec()), name="s")
    with pytest.raises(ValueError, match="status must be"):
        library.update(record["id"], status="probably fine")


def test_deleting_a_strategy_keeps_the_runs_that_tested_it(shelf):
    run_id = _save_run("s", _spec())
    record = library.from_run(run_id, name="s")
    assert library.delete(record["id"]) is True
    assert library.load(record["id"]) is None
    assert library.delete(record["id"]) is False
    assert registry.load(run_id) is not None


def test_saving_the_same_name_twice_updates_rather_than_duplicates(shelf):
    run_id = _save_run("s", _spec())
    first = library.from_run(run_id, name="ATM straddle")
    second = library.from_run(run_id, name="ATM straddle", lots=9)
    assert first["id"] == second["id"]
    assert len(library.list_all(with_evidence=False)) == 1
    assert library.load(first["id"])["lots"] == 9
    # The original creation time survives an overwrite, so "when did I first
    # write this down" is not lost every time the size changes.
    assert second["created"] == first["created"]


def test_the_expiry_axis_is_part_of_a_strategys_identity():
    """A front-weekly straddle and a next-weekly straddle are two strategies.

    The fingerprint is what files a run as evidence about a strategy, so if it
    ignored the expiry axis a walk-forward proving one out would silently
    certify the other — and they are different trades with different decay,
    different gamma and different risk.
    """
    from app.backtest.library import fingerprint

    base = {"legs": [{"side": "SELL", "opt_type": "CE", "lots": 1,
                      "select": {"kind": "moneyness", "level": 0}}],
            "entry_time": "09:20", "exit_time": "15:15", "lot_size": 75}

    rolling = fingerprint(dict(base, expiry_flag="WEEK"), "NIFTY")
    front = fingerprint(dict(base, expiry_flag="WEEK", expiry_index=0), "NIFTY")
    nxt = fingerprint(dict(base, expiry_flag="WEEK", expiry_index=1), "NIFTY")
    zero_dte = fingerprint(dict(base, expiry_flag="WEEK", expiry_index=0,
                                max_dte=0), "NIFTY")

    assert len({rolling, front, nxt, zero_dte}) == 4, (
        "two different contracts hashed to one strategy")


def test_a_rolling_strategy_keeps_the_fingerprint_it_already_had():
    """Strategies are on disk keyed by this hash. Changing it for a run that
    did not use the new axis would detach every one of them from its evidence."""
    from app.backtest.library import fingerprint

    spec = {"legs": [{"side": "SELL", "opt_type": "CE", "lots": 1,
                      "select": {"kind": "moneyness", "level": 0}}],
            "entry_time": "09:20", "exit_time": "15:15", "lot_size": 75,
            "expiry_flag": "WEEK"}
    # The value this produced before the expiry axis existed.
    assert fingerprint(spec, "NIFTY") == fingerprint(
        dict(spec, expiry_index=None, expiry_kind="any"), "NIFTY")
