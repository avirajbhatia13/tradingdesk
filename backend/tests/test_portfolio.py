"""Strategies held together, which is not the sum of them held apart.

Two properties carry the module and both are asserted directly: margin **nets**
(the exchange sees one position per underlying, so a book cannot need more than
its parts) and drawdowns **partly cancel** unless the members move together.
Everything else here is arithmetic that has to add up — a contribution table
that did not sum to the total, or a combined P&L that was not the sum of its
members, would be a portfolio report describing a different portfolio.
"""

from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from app.backtest import costs as costs_mod
from app.backtest import library, portfolio, registry
from app.backtest.engine import LegSpec, StrategySpec
from app.data import lake
from app.data import schema as sch

LOT = 75
SPOT = 24000.0
FIRST = date(2026, 2, 2)          # a Monday


def _sessions(count: int = 40) -> list[date]:
    out, day = [], FIRST
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


@pytest.fixture()
def book(tmp_path, monkeypatch):
    """A lake where the call and the put move in opposite directions.

    Built so a call-selling strategy and a put-selling one lose on *different*
    days: that is what makes the drawdown cancellation real rather than an
    artefact, and it is the whole reason to hold two strategies instead of one.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    monkeypatch.setattr(library, "ROOT", tmp_path / "strategies")
    monkeypatch.setattr(registry, "ROOT", tmp_path / "backtests")

    rows = []
    for index, day in enumerate(_sessions()):
        # Alternating: on even days the call runs against a seller, on odd days
        # the put does.
        call_drift = 20.0 if index % 2 == 0 else -20.0
        for minute in range(375):
            stamp = datetime(day.year, day.month, day.day, 9, 15) \
                + timedelta(minutes=minute)
            progress = minute / 374.0
            for level in range(-3, 4):
                for opt_type in ("CE", "PE"):
                    strike = (SPOT + level * 50 if opt_type == "CE"
                              else SPOT - level * 50)
                    base = max(100.0 - 10.0 * level, 5.0)
                    drift = call_drift if opt_type == "CE" else -call_drift
                    # A common decay on top of the alternating drift, so each
                    # seller nets positive. Without it the two sides cancel
                    # exactly, the book's total is zero, and "share of the
                    # total" has no meaning to test.
                    price = max(base + drift * progress - 5.0 * progress, 1.0)
                    rows.append({
                        "ts": stamp, "underlying": "NIFTY", "expiry": None,
                        "series": "WEEK", "strike": float(strike),
                        "opt_type": opt_type, "moneyness": level,
                        "open": price, "high": price, "low": price,
                        "close": round(price, 2), "volume": 1000, "oi": 1000,
                        "iv": 13.0, "spot": SPOT,
                    })
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")

    def install(name, legs):
        spec = StrategySpec(
            name=name, legs=legs, lot_size=LOT, slippage_points=0.0,
            costs=costs_mod.FREE, entry_time=time(9, 20), exit_time=time(15, 15))
        return library.save(name=name, spec=spec.to_dict(),
                            underlying="NIFTY", lots=1)["id"]

    return {
        "calls": install("short calls", [LegSpec("CE", "SELL", 0)]),
        "puts": install("short puts", [LegSpec("PE", "SELL", 0)]),
    }


def _hold(book, *keys, sizes=None):
    sizes = sizes or {}
    return [portfolio.Allocation(strategy_id=book[k], size=sizes.get(k, 1))
            for k in keys]


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def test_a_portfolio_needs_more_than_one_strategy(book):
    with pytest.raises(ValueError, match="at least two"):
        portfolio.run(_hold(book, "calls"))


def test_an_unknown_strategy_says_how_to_create_one(book):
    with pytest.raises(ValueError, match="no saved strategy"):
        portfolio.run([portfolio.Allocation("nope"),
                       portfolio.Allocation(book["calls"])])


# ---------------------------------------------------------------------------
# the arithmetic has to add up
# ---------------------------------------------------------------------------

def test_the_book_pnl_is_the_sum_of_its_members(book):
    out = portfolio.run(_hold(book, "calls", "puts"))
    members = sum(row["net_pnl"] for row in out["allocations"])
    assert out["headline"]["net_pnl"] == pytest.approx(members, abs=1.0)


def test_size_scales_a_member(book):
    single = portfolio.run(_hold(book, "calls", "puts"))
    double = portfolio.run(_hold(book, "calls", "puts", sizes={"calls": 2}))
    alone = {r["name"]: r for r in single["allocations"]}
    scaled = {r["name"]: r for r in double["allocations"]}
    assert scaled["short calls"]["net_pnl"] == pytest.approx(
        alone["short calls"]["net_pnl"] * 2, rel=1e-6)
    assert scaled["short puts"]["net_pnl"] == pytest.approx(
        alone["short puts"]["net_pnl"], rel=1e-6)


def test_contributions_account_for_the_whole_result(book):
    out = portfolio.run(_hold(book, "calls", "puts"))
    shares = [row["share_pct"] for row in out["contribution"]
              if row["share_pct"] is not None]
    assert sum(shares) == pytest.approx(100.0, abs=0.5)


def test_every_session_both_traded_is_counted_as_both_live(book):
    out = portfolio.run(_hold(book, "calls", "puts"))
    concurrency = out["concurrency"]
    assert concurrency["members"] == 2
    assert concurrency["all_live_pct"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# the two properties the module exists to measure
# ---------------------------------------------------------------------------

def test_margin_nets_so_the_book_never_costs_more_than_its_parts(book):
    """The exchange sees one position per underlying. A book whose combined
    margin exceeded the sum of its members' would mean the model is charging
    for the same risk twice."""
    out = portfolio.run(_hold(book, "calls", "puts"))
    netting = out["netting"]
    assert netting["margin_together"] <= netting["margin_alone"] + 1.0


def test_drawdowns_cancel_when_the_members_lose_on_different_days(book):
    """The fixture is built so the call side and the put side hurt on
    alternating days. Held together the bad days offset, and the combined
    drawdown has to be shallower than the two added up."""
    out = portfolio.run(_hold(book, "calls", "puts"))
    netting = out["netting"]
    assert abs(netting["drawdown_together"]) < abs(netting["drawdown_alone"])
    assert netting["capital_together"] < netting["capital_alone"]


def test_alternating_losers_correlate_negatively(book):
    out = portfolio.run(_hold(book, "calls", "puts"))
    matrix = out["correlation"]["matrix"]
    names = out["correlation"]["members"]
    assert matrix[names[0]][names[0]] == pytest.approx(1.0)
    assert matrix[names[0]][names[1]] < 0


def test_holding_a_strategy_against_itself_saves_nothing(book):
    """The control. Two copies of one strategy are perfectly correlated, so
    there is no diversification to find — only the report saying so would make
    the positive case believable."""
    same = [portfolio.Allocation(book["calls"], 1),
            portfolio.Allocation(book["calls"], 1)]
    out = portfolio.run(same)
    matrix = out["correlation"]["matrix"]
    names = out["correlation"]["members"]
    assert matrix[names[0]][names[1]] == pytest.approx(1.0)
    # Drawdown must scale with size rather than cancel.
    assert abs(out["netting"]["drawdown_together"]) == pytest.approx(
        abs(out["netting"]["drawdown_alone"]), rel=0.02)


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def test_the_markdown_leads_with_what_combining_changed(book):
    out = portfolio.run(_hold(book, "calls", "puts"))
    text = portfolio.to_markdown(out, "099", "test book")
    assert "What holding them together changed" in text
    assert "Judged one by one" in text
    assert "How alike they are" in text
    # Netting only helps where positions overlap, and the report must say how
    # often that was rather than implying it is always.
    assert "live together" in text


def test_a_portfolio_saves_and_reloads(book):
    out = portfolio.run(_hold(book, "calls", "puts"))
    entry = registry.save_portfolio(
        "book", [{"strategy": book["calls"], "size": 1}],
        out, portfolio.to_markdown(out, "001", "book"), "NIFTY",
        FIRST, FIRST + timedelta(days=60))
    assert entry["kind"] == "portfolio"
    reloaded = registry.load(entry["id"])
    assert reloaded["report"]["headline"]["net_pnl"] == out["headline"]["net_pnl"]
    rebuilt = registry.rebuild_index()
    assert [e["kind"] for e in rebuilt] == ["portfolio"]
