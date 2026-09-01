"""Resampling a run's trades, and the one thing it gets wrong.

The interesting case is not the reshuffle itself — it is what happens when
reshuffling *cannot reproduce* the drawdown that actually occurred. That means
the losses arrived together rather than independently, which is the normal
shape for option selling, and it means every number this module produces is a
floor on the risk rather than a bound on it. Reporting a shallower tail than
the strategy's own history without saying so would be worse than not running
the simulation at all.
"""

import pytest

from app.backtest import montecarlo


def test_a_clustered_losing_streak_is_recognised():
    """Twenty losses in a row, then wins. No reshuffle of these trades can
    stack the losses as tightly as history did, so the realised drawdown is
    deeper than anything the simulation produces."""
    trades = [-1000.0] * 20 + [200.0] * 80
    out = montecarlo.simulate(trades, paths=500)

    assert out["actual"]["max_drawdown"] == pytest.approx(-20_000.0)
    assert out["losses_cluster"] is True
    assert out["drawdown_vs_typical_ordering"] > 1.0
    assert out["drawdown_vs_worst_ordering"] > 1.0


def test_capital_is_sized_off_whichever_drawdown_is_deeper():
    """When the model has just been shown not to hold, it must not be used to
    lower the capital estimate below what actually happened."""
    trades = [-1000.0] * 20 + [200.0] * 80
    out = montecarlo.simulate(trades, paths=500, peak_margin=50_000.0)
    assert out["capital_floor_p95"] >= 50_000.0 + 20_000.0


def test_an_evenly_mixed_run_is_not_flagged_as_clustered():
    """Alternating small wins and losses has no streak to find, so a bad
    ordering is worse than the one that happened."""
    trades = [(-800.0 if i % 2 else 900.0) for i in range(200)]
    out = montecarlo.simulate(trades, paths=500)
    assert out["losses_cluster"] is False
    assert out["drawdown_vs_worst_ordering"] < 1.0


def test_reshuffling_cannot_change_the_total():
    """Only the sequence varies, so the drawdown moves and the profit does
    not. Bootstrapping resamples the outcome too, which is why the two are
    reported separately rather than as one number."""
    trades = [100.0, -50.0, 300.0, -800.0, 250.0] * 30
    out = montecarlo.simulate(trades, paths=400)
    total = sum(trades)
    assert out["actual"]["total_pnl"] == pytest.approx(total)
    # The bootstrap spreads around the realised total rather than reproducing it.
    assert out["bootstrap_total"]["p5"] < total < out["bootstrap_total"]["p95"]


def test_the_same_run_always_reports_the_same_risk():
    """Two runs of one backtest disagreeing about their own risk figures would
    be worse than useless, so the generator is seeded."""
    trades = [100.0, -50.0, 300.0, -800.0, 250.0] * 30
    first = montecarlo.simulate(trades, paths=300)
    second = montecarlo.simulate(trades, paths=300)
    assert first == second


def test_too_few_trades_says_so_rather_than_guessing():
    out = montecarlo.simulate([100.0, -200.0, 50.0])
    assert "note" in out
    assert "paths" not in out
