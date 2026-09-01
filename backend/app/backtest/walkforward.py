"""Out-of-sample validation — the check that a sweep makes necessary.

A sweep reports the best cell in a grid. That number is not achievable, and the
gap is not small: it was chosen *with knowledge of the whole period*, including
the part you are pretending to trade forward into. Quote it as a result and you
are quoting the maximum of forty draws as though it were one.

Walk-forward asks the only question that matters. Split the history into
periods. For each one, choose the setting using **only data that existed
before it**, then trade that setting through the period blind, and keep the
result. Stitch those blind periods together and you have what the strategy
would actually have produced for someone re-tuning it as they went.

The headline is the comparison:

    a sweep would have told you        +₹64,898
    choosing blind and trading forward +₹21,340

If the second number is a small fraction of the first, the optimisation was
fitting noise. If the chosen setting jumps around between folds, there was
never a stable optimum to find and the whole exercise was a coin toss with
extra steps.

## Why this is cheap

Every trade is one session, entered and exited the same day, and the engine
carries no state between days. So a grid cell's trades over 2021-2026 can be
*sliced* by date afterwards rather than re-run per period — which makes a
5-fold walk-forward over a 25-cell grid cost 25 backtests, not 125. The slicing
is exact, not an approximation, and it is the property that makes this
practical to run on every promising strategy rather than only on the ones that
survive a rationalisation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

import numpy as np

from app.backtest import engine
from app.backtest import engine, report as report_mod, sweep as sweep_mod
from app.backtest.engine import contract_label

DEFAULT_FOLDS = 4

# Below this, the optimisation is mostly fitting noise: the setting chosen from
# the past does not carry into the future well enough to be worth choosing.
# Not a bright line — a rule of thumb from the trading literature, stated so
# the report can say something rather than leave the reader to interpret a
# ratio.
EFFICIENCY_FLOOR = 0.5

METRICS = ("net_pnl", "return_to_drawdown", "sharpe", "win_rate")


def _fold_windows(days: list[date], folds: int, scheme: str
                  ) -> list[tuple[tuple[date, date], tuple[date, date]]]:
    """Contiguous (in-sample, out-of-sample) date windows.

    Split on *trading days present in the data*, not calendar dates, so each
    test window holds a similar number of trades. Splitting on the calendar
    would hand the first fold a thin period and the last a fat one whenever
    coverage is uneven, and then read the difference as a change in the edge.
    """
    if len(days) < (folds + 1) * 10:
        raise ValueError(
            f"{len(days)} trading days is too few to split into {folds + 1} "
            f"windows. Use fewer folds, or a longer date range.")
    # `folds + 2` edges cut the days into `folds + 1` chunks. The first chunk is
    # only ever trained on — there is no history before it to choose from — so
    # the remaining `folds` chunks are the ones traded forward blind.
    edges = np.linspace(0, len(days), folds + 2).astype(int)
    windows = []
    for i in range(folds):
        train_lo = 0 if scheme == "anchored" else edges[i]
        train = (days[train_lo], days[edges[i + 1] - 1])
        test = (days[edges[i + 1]], days[edges[i + 2] - 1])
        windows.append((train, test))
    return windows


def _slice(trades: list[Any], lo: date, hi: date) -> list[Any]:
    return [t for t in trades if lo <= t.day <= hi]


def _score(trades: list[Any], metric: str) -> float:
    """Rank a setting over a set of trades.

    Every metric is per-trade or a ratio, never a total, because in-sample and
    out-of-sample windows hold different numbers of trades and a total would
    rank the longer window's setting higher for being longer.
    """
    if metric not in METRICS:
        raise ValueError(f"unknown selection metric {metric!r}; "
                         f"expected one of {', '.join(METRICS)}")
    if not trades:
        return float("-inf")
    pnls = np.array([t.pnl for t in trades], dtype=np.float64)
    if metric == "net_pnl":
        return float(pnls.mean())
    if metric == "return_to_drawdown":
        drawdown = float(engine.underwater(np.cumsum(pnls)).min())
        return float(pnls.sum() / abs(drawdown)) if drawdown else float("inf")
    if metric == "sharpe":
        return float(pnls.mean() / pnls.std()) if pnls.std() > 0 else float("-inf")
    return float((pnls > 0).mean())


def _stats(trades: list[Any], peak_margin: float | None) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "net_pnl": 0.0}
    pnls = np.array([t.pnl for t in trades], dtype=np.float64)
    drawdown = float(engine.underwater(np.cumsum(pnls)).min())
    floor = (peak_margin + abs(drawdown)) if peak_margin else None
    total = float(pnls.sum())
    return {
        "trades": int(pnls.size),
        "net_pnl": round(total, 2),
        "average": round(float(pnls.mean()), 2),
        "max_drawdown": round(drawdown, 2),
        "win_rate": round(float((pnls > 0).mean()) * 100.0, 1),
        "capital_floor": round(floor, 2) if floor else None,
        "roi_on_capital_pct": round(total / floor * 100.0, 2) if floor else None,
        "return_to_drawdown": round(total / abs(drawdown), 2) if drawdown else None,
        "sharpe": round(float(pnls.mean() / pnls.std() * np.sqrt(252)), 2)
                  if pnls.std() > 0 else None,
    }


def run(base: engine.StrategySpec, axes: dict[str, list[Any]],
        underlying: str = "NIFTY", start: date | None = None,
        end: date | None = None, folds: int = DEFAULT_FOLDS,
        scheme: str = "anchored", metric: str = "net_pnl") -> dict[str, Any]:
    """Choose settings from the past, trade them forward, and report the decay."""
    if scheme not in ("anchored", "rolling"):
        raise ValueError("scheme must be 'anchored' (expanding history) or "
                         "'rolling' (fixed-length history)")
    # Checked before any work: a typo'd metric would otherwise score every cell
    # as unrankable and surface as "no cell produced a trade" after running the
    # whole grid.
    if metric not in METRICS:
        raise ValueError(f"unknown selection metric {metric!r}; "
                         f"expected one of {', '.join(METRICS)}")
    fields = list(axes)
    for field in fields:
        if not hasattr(base, field):
            raise ValueError(f"{field!r} is not a strategy setting")
    values = [[sweep_mod._coerce(f, v) for v in axes[f]] for f in fields]

    import itertools
    combinations = list(itertools.product(*values))
    if len(combinations) > sweep_mod.MAX_CELLS:
        raise ValueError(f"{len(combinations)} combinations is past the "
                         f"{sweep_mod.MAX_CELLS} limit")

    # One run per cell over the whole range. Slicing by date afterwards is
    # exact because the engine carries no state between sessions.
    runs: list[dict[str, Any]] = []
    for combination in combinations:
        spec = replace(base, **dict(zip(fields, combination)))
        spec.name = base.name
        result = engine.run(spec, underlying, start, end)
        runs.append({
            "settings": {f: sweep_mod._label(f, v)
                         for f, v in zip(fields, combination)},
            "trades": result.trades,
        })
    if not any(r["trades"] for r in runs):
        return {"note": "no cell produced a trade", "fields": fields}

    baseline = max(runs, key=lambda r: len(r["trades"]))
    days = sorted({t.day for t in baseline["trades"]})
    windows = _fold_windows(days, folds, scheme)

    # Margin from the base strategy, once, as a shared denominator — same
    # reasoning as the sweep.
    context = engine.load_context(base, underlying, start, end)
    seed = engine.run(base, underlying, start, end)
    margin, _ = report_mod.margin_series(seed, base, context.columns)
    peak_margin = margin.peak or None

    chosen: list[dict[str, Any]] = []
    stitched: list[Any] = []
    for (train_lo, train_hi), (test_lo, test_hi) in windows:
        scored = [(r, _score(_slice(r["trades"], train_lo, train_hi), metric))
                  for r in runs]
        scored = [(r, s) for r, s in scored if s != float("-inf")]
        if not scored:
            continue
        # Deterministic ties: the settings string breaks them, so two runs of
        # the same walk-forward cannot disagree about what was chosen.
        best, in_sample_score = max(
            scored, key=lambda pair: (pair[1], str(pair[0]["settings"])))
        in_trades = _slice(best["trades"], train_lo, train_hi)
        out_trades = _slice(best["trades"], test_lo, test_hi)
        stitched.extend(out_trades)
        chosen.append({
            "train": [train_lo.isoformat(), train_hi.isoformat()],
            "test": [test_lo.isoformat(), test_hi.isoformat()],
            "settings": best["settings"],
            "in_sample": _stats(in_trades, peak_margin),
            "out_of_sample": _stats(out_trades, peak_margin),
        })

    stitched.sort(key=lambda t: (t.day, str(t.entry_ts)))
    out_of_sample = _stats(stitched, peak_margin)

    # What a sweep over the whole period would have reported — the number this
    # exists to be compared against.
    hindsight_run = max(
        (r for r in runs if r["trades"]),
        key=lambda r: _score(r["trades"], metric))
    hindsight = _stats(hindsight_run["trades"], peak_margin)

    # And the honest alternative to optimising at all: the middle of the grid.
    totals = sorted((sum(t.pnl for t in r["trades"]), i)
                    for i, r in enumerate(runs) if r["trades"])
    median_run = runs[totals[len(totals) // 2][1]]
    median = _stats(median_run["trades"], peak_margin)

    is_avg = np.mean([f["in_sample"]["average"] for f in chosen
                      if f["in_sample"].get("trades")]) if chosen else 0.0
    os_avg = out_of_sample.get("average", 0.0)
    efficiency = (os_avg / is_avg) if is_avg else None

    return {
        "fields": fields,
        "axes": {f: [sweep_mod._label(f, v) for v in vals]
                 for f, vals in zip(fields, values)},
        "scheme": scheme, "folds": len(chosen), "metric": metric,
        "cells": len(runs),
        "peak_margin": round(peak_margin, 2) if peak_margin else None,
        "windows": chosen,
        "out_of_sample": out_of_sample,
        "hindsight": dict(hindsight, settings=hindsight_run["settings"]),
        "median_cell": dict(median, settings=median_run["settings"]),
        # Out-of-sample profit per trade as a share of in-sample. The single
        # number this whole module exists to produce.
        "efficiency": round(efficiency, 3) if efficiency is not None else None,
        "settings_changed": len({str(f["settings"]) for f in chosen}),
        "curve": [{"day": t.day.isoformat(), "equity": round(v, 2)}
                  for t, v in zip(stitched,
                                  np.cumsum([t.pnl for t in stitched]))],
    }


# ---------------------------------------------------------------------------
# the human-readable artefact
# ---------------------------------------------------------------------------

def to_markdown(result: dict[str, Any], run_id: str, name: str, spec: Any,
                underlying: str, start: date, end: date) -> str:
    rupees = report_mod._rupees
    lines: list[str] = [f"# {run_id} — {name} *(walk-forward)*\n"]
    lines.append(
        f"**{underlying} {contract_label(spec).lower()}** · "
        f"`{spec.describe()}` · {start} → {end}\n")

    if result.get("note"):
        return "\n".join(lines + [result["note"]]) + "\n"

    out = result["out_of_sample"]
    hind = result["hindsight"]
    median = result["median_cell"]
    lines.append(
        f"Chose **{'** and **'.join(result['fields'])}** from history alone, "
        f"{result['folds']} times across the sample, and traded each choice "
        f"forward blind.\n")

    # ---- the comparison ----------------------------------------------------
    lines.append("## What a sweep would have promised, and what you would have got\n")
    lines.append("| | Net P&L | Trades | Per trade | Max drawdown |\n|---|---|---|---|---|")
    lines.append(
        f"| A sweep's best setting, chosen with hindsight | "
        f"**{rupees(hind['net_pnl'], sign=True)}** | {hind['trades']} | "
        f"{rupees(hind['average'], sign=True)} | {rupees(hind['max_drawdown'])} |")
    lines.append(
        f"| **Choosing blind and trading forward** | "
        f"**{rupees(out['net_pnl'], sign=True)}** | {out['trades']} | "
        f"{rupees(out.get('average'), sign=True)} | "
        f"{rupees(out.get('max_drawdown'))} |")
    lines.append(
        f"| Not optimising at all — the middle of the grid | "
        f"{rupees(median['net_pnl'], sign=True)} | {median['trades']} | "
        f"{rupees(median['average'], sign=True)} | "
        f"{rupees(median['max_drawdown'])} |")
    lines.append(
        "\n*The first row covers the whole period and the second only the "
        "out-of-sample part of it, so compare the per-trade column rather than "
        "the totals.*\n")

    efficiency = result.get("efficiency")
    if efficiency is None:
        lines.append("> Efficiency could not be computed — too few in-sample trades.\n")
    elif efficiency < 0:
        lines.append(
            f"> ⚠️ **The optimisation is worse than useless here.** Settings "
            f"chosen from the past *lost* money going forward while the same "
            f"settings looked profitable in-sample. That is the signature of "
            f"fitting noise, and it means the sweep's best cell should not be "
            f"traded.\n")
    elif efficiency < EFFICIENCY_FLOOR:
        lines.append(
            f"> ⚠️ **Efficiency {efficiency:.0%}.** Only {efficiency:.0%} of the "
            f"in-sample edge carried into the periods it was not chosen on. "
            f"Below about {EFFICIENCY_FLOOR:.0%} the optimisation is mostly "
            f"fitting noise, and the honest expectation for this strategy is "
            f"the out-of-sample row, not the sweep's headline.\n")
    else:
        lines.append(
            f"> **Efficiency {efficiency:.0%}.** Most of the in-sample edge "
            f"survived into data it was not chosen on, which is what makes a "
            f"parameter worth choosing at all.\n")

    changed = result["settings_changed"]
    if changed == 1:
        lines.append(
            f"\nThe same setting won every fold — there is a stable optimum "
            f"here rather than a different answer each time.\n")
    elif changed >= result["folds"]:
        lines.append(
            f"\n⚠️ **The chosen setting changed every fold** ({changed} distinct "
            f"choices in {result['folds']}). There is no stable optimum in this "
            f"grid; each fold found a different one, which is what noise looks "
            f"like when you optimise on it.\n")
    else:
        lines.append(
            f"\n{changed} distinct settings won across {result['folds']} folds.\n")

    # ---- the folds ---------------------------------------------------------
    lines.append("\n## Fold by fold\n")
    lines.append("| Chose from | Chose | Traded on | In-sample per trade | "
                 "Out-of-sample per trade | Out-of-sample P&L |\n"
                 "|---|---|---|---|---|---|")
    for window in result["windows"]:
        setting = ", ".join(f"{k} {v}" for k, v in window["settings"].items())
        inside, outside = window["in_sample"], window["out_of_sample"]
        lines.append(
            f"| {window['train'][0]} → {window['train'][1]} | {setting} | "
            f"{window['test'][0]} → {window['test'][1]} | "
            f"{rupees(inside.get('average'), sign=True)} | "
            f"{rupees(outside.get('average'), sign=True)} | "
            f"{rupees(outside.get('net_pnl'), sign=True)} |")
    lines.append(
        f"\n*{result['scheme'].title()} windows: each fold chose its setting "
        f"using only the data to its left, then traded it forward without "
        f"looking. Selection metric: {result['metric']}.*\n")

    if out.get("capital_floor"):
        lines.append(
            f"\nOn the out-of-sample record alone the strategy needed "
            f"**{rupees(out['capital_floor'])}** of capital and returned "
            f"**{report_mod._pct(out.get('roi_on_capital_pct'))}** on it.\n")

    lines.append(
        f"\n---\n\n*{result['cells']} settings were tried. Walk-forward does "
        f"not remove the multiple-comparisons problem — it measures it. The "
        f"out-of-sample row is still one path through one history.*\n")
    return "\n".join(lines) + "\n"
