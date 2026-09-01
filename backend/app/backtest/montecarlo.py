"""What else could have happened.

A backtest reports one path. The trades arrived in one order, and the worst
drawdown is whatever that particular order produced — a single draw from a
distribution, quoted as though it were the distribution. Size an account off it
and you have sized off a sample of one.

Two resamplings are run, because they answer different questions.

**Shuffle** keeps every trade and only changes the order. Total P&L is
therefore identical in every path, and all the variation is *sequence risk*:
the same winning strategy, with its losers clustered differently. This is the
honest answer to "how deep could the drawdown have been" — it uses no
assumption beyond the trades that actually happened.

**Bootstrap** resamples the trades with replacement, so a path may contain the
worst day twice or not at all. It varies the outcome as well as the order, and
it is the answer to "how likely was this to have lost money at all". It assumes
trades are independent and identically distributed, which is a real assumption
and a slightly generous one — option-selling losses cluster in volatility
regimes, so genuine tail risk is worse than the bootstrap suggests.

Neither is a forecast. Both describe the strategy's own history rearranged, and
a strategy whose 95th-percentile drawdown is three times its historical one has
not been unlucky yet.
"""

from __future__ import annotations

from typing import Any

import numpy as np

DEFAULT_PATHS = 2000

# Fixed so a report is reproducible. Two runs of the same backtest that
# disagreed on their own risk figures would be worse than useless.
SEED = 20240817


def _max_drawdown(paths: np.ndarray) -> np.ndarray:
    """Deepest peak-to-trough loss of each row, vectorised over all paths.

    The leading zero column makes the account's starting balance a peak, so a
    path that opens with its worst losses is measured from where it started
    rather than from after its first trade. Same convention as
    `engine.underwater`; the two disagreeing would put a different drawdown in
    the risk table than in the simulation beside it.
    """
    equity = np.cumsum(paths, axis=1)
    start = np.zeros((equity.shape[0], 1), dtype=equity.dtype)
    peaks = np.maximum.accumulate(np.hstack([start, equity]), axis=1)[:, 1:]
    return (equity - peaks).min(axis=1)


def _percentiles(values: np.ndarray, points=(5, 25, 50, 75, 95)
                 ) -> dict[str, float]:
    result = np.percentile(values, points)
    return {f"p{point}": round(float(value), 2)
            for point, value in zip(points, result)}


def simulate(pnls: list[float], paths: int = DEFAULT_PATHS,
             peak_margin: float | None = None) -> dict[str, Any]:
    """Resample a run's trades and report the spread of outcomes."""
    trades = np.asarray(pnls, dtype=np.float64)
    if trades.size < 10:
        return {"note": "too few trades to resample meaningfully",
                "trades": int(trades.size)}

    rng = np.random.default_rng(SEED)
    n = trades.size

    # Shuffling is done by argsort of random keys rather than a Python loop:
    # 2,000 paths x 1,200 trades is one 2.4M-element sort, a few milliseconds.
    order = rng.random((paths, n)).argsort(axis=1)
    shuffled = trades[order]
    shuffled_dd = _max_drawdown(shuffled)

    drawn = trades[rng.integers(0, n, size=(paths, n))]
    boot_dd = _max_drawdown(drawn)
    boot_total = drawn.sum(axis=1)

    actual_total = float(trades.sum())
    worst_dd = float(_max_drawdown(trades.reshape(1, -1))[0])
    dd_p5 = float(np.percentile(shuffled_dd, 5))     # 5th pct = deepest 5%
    dd_p50 = float(np.percentile(shuffled_dd, 50))

    # How the realised drawdown compares to the reshuffled ones. Both
    # directions are informative and they mean opposite things:
    #
    #   below 1  — history dealt a kinder sequence than chance would. Size the
    #              account off the reshuffled figure, not the realised one.
    #   above 1  — the real drawdown is DEEPER than reshuffling can produce,
    #              which is the signature of losses arriving in clusters.
    #              Shuffling breaks the clustering up, so every figure here
    #              understates the risk rather than bounding it.
    #
    # The second case is the common one for option selling, where losses
    # arrive in volatile weeks rather than independently, and it is the more
    # important of the two to say out loud: a Monte Carlo that quietly
    # reported a shallower tail than the strategy's own history would be worse
    # than not running one.
    versus_typical = abs(worst_dd / dd_p50) if dd_p50 else None
    versus_worst = abs(worst_dd / dd_p5) if dd_p5 else None

    out: dict[str, Any] = {
        "paths": paths,
        "trades": int(n),
        "actual": {"total_pnl": round(actual_total, 2),
                   "max_drawdown": round(worst_dd, 2)},
        # Same trades, different order. Total is invariant, so only the
        # drawdown is reported.
        "shuffled_drawdown": _percentiles(shuffled_dd),
        "bootstrap_drawdown": _percentiles(boot_dd),
        "bootstrap_total": _percentiles(boot_total),
        "probability_of_loss_pct": round(
            float((boot_total <= 0).mean()) * 100.0, 1),
        "drawdown_vs_typical_ordering": (round(versus_typical, 2)
                                         if versus_typical else None),
        "drawdown_vs_worst_ordering": (round(versus_worst, 2)
                                       if versus_worst else None),
        "losses_cluster": bool(versus_worst and versus_worst > 1.0),
    }
    # The capital figure the report leads with, re-derived against the
    # deepest drawdown either source suggests. When losses cluster, the
    # realised drawdown *is* the worse number, and using the reshuffled one
    # would lower the capital estimate on the strength of a model that has
    # just been shown not to hold.
    if peak_margin:
        out["capital_floor_p95"] = round(
            peak_margin + max(abs(dd_p5), abs(worst_dd)), 2)
    return out
