"""Running the same strategy across a grid of settings.

A single backtest is one number. The question that decides whether it means
anything is *what happens next door* — at a stop 5% wider, an entry ten minutes
later, a target half as far. An edge is a plateau; a curve fit is a spike, and
they are indistinguishable from the single number at the top.

This is cheap here for one specific reason: loading the leg matrix is ~90% of a
run's wall time, and every setting that does not change *which contracts are
fetched* — stop, target, trail, exit time, weekday filter, lot size, slippage —
reuses the cached matrix. A 25-cell stop/target grid is one load and 25 numpy
passes. Sweeping `entry_time` or the legs themselves does reload, because those
change the snapshot the selectors choose from, so those axes are marked
expensive and the caller is told what it is asking for.

## The multiple-comparisons problem, stated rather than hidden

Sweeping is the single easiest way to fool yourself with a backtest. Run 49
combinations of stop and target on noise and roughly two will look significant
at 5%. This module therefore never reports only the winner: it reports what
share of the grid was profitable, the median cell, and whether the best cell's
neighbours agree with it. A peak surrounded by losses is a fluke however good
the peak looks.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import replace
from datetime import date, time
from typing import Any

import numpy as np

from app.backtest import engine
from app.backtest import engine, report as report_mod
from app.backtest.engine import contract_label

# Axes that change which bars are loaded, so each value pays a fresh matrix
# load rather than reusing the cache.
RELOADS_MATRIX = {"entry_time", "legs", "expiry_flag"}

# A grid this size is already a lot of chances to get lucky; beyond it the
# exercise stops being a robustness check and becomes a search.
MAX_CELLS = 400


def _coerce(field: str, value: Any) -> Any:
    """Let axis values be written the way they are spoken."""
    if field in ("entry_time", "exit_time") and isinstance(value, str):
        hour, _, minute = value.partition(":")
        return time(int(hour), int(minute or 0))
    if field == "weekdays" and isinstance(value, str):
        return tuple(int(part) for part in value.split(",") if part.strip() != "")
    return value


def _label(field: str, value: Any) -> str:
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, tuple):
        return ",".join(str(v) for v in value)
    if value is None:
        return "none"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def run(base: engine.StrategySpec, axes: dict[str, list[Any]],
        underlying: str = "NIFTY", start: date | None = None,
        end: date | None = None) -> dict[str, Any]:
    """Run `base` once for every combination in `axes`.

    Returns every cell, not just the best one. Margin is estimated once from
    the base spec and reused as a common denominator across the grid, which is
    both far cheaper and more comparable than re-estimating per cell — a cell
    that trades on fewer days would otherwise be flattered by a lower peak.
    """
    if not axes:
        raise ValueError("no axes to sweep")
    fields = list(axes)
    for field in fields:
        if not hasattr(base, field):
            raise ValueError(
                f"{field!r} is not a strategy setting. Sweepable settings are "
                f"the fields of StrategySpec, e.g. stop_loss_pct, target_pct, "
                f"trail_stop, exit_time, entry_time, per_leg_stop_pct, "
                f"re_entries, weekdays, slippage_points.")
    values = [[_coerce(field, value) for value in axes[field]] for field in fields]
    combinations = list(itertools.product(*values))
    if len(combinations) > MAX_CELLS:
        raise ValueError(
            f"{len(combinations)} combinations is past the {MAX_CELLS} limit. "
            f"A grid that large is a search, not a robustness check — narrow "
            f"it, or run the interesting corners as separate numbered runs.")

    # The denominator, from the strategy as specified. Done once: 1,200 trades
    # x 25 cells of per-trade SPAN estimation would dominate the sweep, and the
    # figure it produces would not be comparable across cells anyway.
    baseline = engine.run(base, underlying, start, end)
    context = engine.load_context(base, underlying, start, end)
    margin, _ = report_mod.margin_series(baseline, base, context.columns)
    peak_margin = margin.peak or None

    cells: list[dict[str, Any]] = []
    for combination in combinations:
        spec = replace(base, **dict(zip(fields, combination)))
        spec.name = base.name
        result = engine.run(spec, underlying, start, end)
        cells.append(_cell(dict(zip(fields, combination)), fields, result,
                           peak_margin))

    return {
        "axes": {field: [_label(field, v) for v in vals]
                 for field, vals in zip(fields, values)},
        "fields": fields,
        "cells": cells,
        "peak_margin": round(peak_margin, 2) if peak_margin else None,
        "reloads": sorted(set(fields) & RELOADS_MATRIX),
        "summary": _summary(cells, fields),
        "heatmap": _heatmap(cells, fields) if len(fields) == 2 else None,
    }


def _cell(settings: dict[str, Any], fields: list[str],
          result: engine.Result, peak_margin: float | None) -> dict[str, Any]:
    pnls = np.array([t.pnl for t in result.trades], dtype=np.float64)
    if not pnls.size:
        return {"settings": {f: _label(f, settings[f]) for f in fields},
                "trades": 0, "net_pnl": 0.0, "note": "no trades"}

    equity = np.cumsum(pnls)
    drawdown = float(engine.underwater(equity).min())
    total = float(pnls.sum())
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    floor = (peak_margin + abs(drawdown)) if peak_margin else None

    return {
        "settings": {f: _label(f, settings[f]) for f in fields},
        "trades": int(pnls.size),
        "net_pnl": round(total, 2),
        "gross_pnl": round(sum(t.gross for t in result.trades), 2),
        "max_drawdown": round(drawdown, 2),
        "win_rate": round(float(wins.size) / pnls.size * 100.0, 1),
        "average": round(float(pnls.mean()), 2),
        "profit_factor": round(float(wins.sum()) / float(-losses.sum()), 2)
                         if losses.size and losses.sum() else None,
        "sharpe": round(float(pnls.mean() / pnls.std() * np.sqrt(252)), 2)
                  if pnls.std() > 0 else None,
        "capital_floor": round(floor, 2) if floor else None,
        "roi_on_capital_pct": round(total / floor * 100.0, 2) if floor else None,
        "return_to_drawdown": round(total / abs(drawdown), 2) if drawdown else None,
    }


def _summary(cells: list[dict[str, Any]], fields: list[str]) -> dict[str, Any]:
    scored = [c for c in cells if c.get("trades")]
    if not scored:
        return {"note": "no cell produced a trade"}
    values = np.array([c["net_pnl"] for c in scored], dtype=np.float64)
    best = max(scored, key=lambda c: c["net_pnl"])
    worst = min(scored, key=lambda c: c["net_pnl"])
    positive = int((values > 0).sum())

    return {
        "cells": len(cells),
        "profitable_cells": positive,
        "profitable_share_pct": round(positive / len(scored) * 100.0, 1),
        "best": best,
        "worst": worst,
        "median_net_pnl": round(float(np.median(values)), 2),
        "spread": round(float(values.max() - values.min()), 2),
        # The only defence against reading a lucky cell as an edge. Stated as
        # a number the reader has to see, not a caveat in the docs.
        "attempts": len(cells),
    }


def _heatmap(cells: list[dict[str, Any]], fields: list[str]) -> dict[str, Any]:
    """A two-axis grid, plus whether the best cell's neighbours agree with it.

    The neighbour check is the whole reason to draw a heatmap rather than a
    table. A parameter setting that works and whose neighbours also work is a
    plateau you can trade through changing conditions. One that works while
    everything around it loses is a spike, and next year's spike will be
    somewhere else.
    """
    rows = sorted({c["settings"][fields[0]] for c in cells}, key=_sort_key)
    columns = sorted({c["settings"][fields[1]] for c in cells}, key=_sort_key)
    index = {(c["settings"][fields[0]], c["settings"][fields[1]]): c
             for c in cells}

    def metric(cell: dict[str, Any] | None, name: str) -> float | None:
        return cell.get(name) if cell else None

    grid = {
        name: [[metric(index.get((r, c)), name) for c in columns] for r in rows]
        for name in ("net_pnl", "roi_on_capital_pct", "max_drawdown",
                     "win_rate", "sharpe", "trades", "return_to_drawdown")
    }

    values = np.array([[(index.get((r, c)) or {}).get("net_pnl", np.nan)
                        for c in columns] for r in rows], dtype=np.float64)
    ri, ci = np.unravel_index(np.nanargmax(values), values.shape)
    neighbours = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        r, c = ri + dr, ci + dc
        if 0 <= r < values.shape[0] and 0 <= c < values.shape[1]:
            if np.isfinite(values[r, c]):
                neighbours.append(float(values[r, c]))

    peak = float(values[ri, ci])
    around = float(np.mean(neighbours)) if neighbours else None
    return {
        "rows": {"field": fields[0], "values": rows},
        "columns": {"field": fields[1], "values": columns},
        "grid": grid,
        "best_at": {fields[0]: rows[ri], fields[1]: columns[ci]},
        "best_net_pnl": round(peak, 2),
        "neighbour_mean": round(around, 2) if around is not None else None,
        # Near 1.0 the surface is flat around the best setting, which is what
        # you want. Much above it, the peak is standing on its own.
        "peak_to_neighbours": round(peak / around, 2)
                              if around and around > 0 else None,
    }


def _sort_key(label: str):
    """Numeric where possible so 0.2, 0.3, 0.4 do not sort as strings; the
    axes are usually numbers written as labels."""
    try:
        return (0, float(label))
    except (TypeError, ValueError):
        return (1, label)


# ---------------------------------------------------------------------------
# the human-readable artefact
# ---------------------------------------------------------------------------

def to_markdown(grid: dict[str, Any], run_id: str, name: str,
                spec: Any, underlying: str, start: date, end: date) -> str:
    """A sweep read the way it should be read: the shape first, the peak last.

    Deliberately inverted from how these are usually presented. Leading with
    the best cell invites treating it as the result, and it is not — it is the
    maximum of however many draws, and the number that decides whether it means
    anything is how the cells around it did.
    """
    rupees = report_mod._rupees
    summary = grid["summary"]
    lines: list[str] = [f"# {run_id} — {name} *(parameter sweep)*\n"]
    lines.append(
        f"**{underlying} {contract_label(spec).lower()}** · "
        f"`{spec.describe()}` · {start} → {end}\n")
    lines.append(
        f"Varied **{'** and **'.join(grid['fields'])}** across "
        f"{summary.get('cells', 0)} combinations.\n")

    if summary.get("note"):
        return "\n".join(lines + [summary["note"]]) + "\n"

    # ---- the shape of the surface ------------------------------------------
    share = summary["profitable_share_pct"]
    lines.append("## Is this an edge or a lucky setting?\n")
    lines.append(
        f"**{summary['profitable_cells']} of {summary['cells']} settings were "
        f"profitable ({share:.0f}%)**, with a median of "
        f"{rupees(summary['median_net_pnl'], sign=True)}.\n")

    heat = grid.get("heatmap")
    ratio = (heat or {}).get("peak_to_neighbours")
    if share >= 80:
        lines.append(
            "> Most of the grid works. That is what an edge looks like — the "
            "strategy is not depending on one setting being right.\n")
    elif share <= 40:
        lines.append(
            "> **Most of the grid loses.** The profitable corner is more likely "
            "to be the luckiest of "
            f"{summary['attempts']} draws than a setting worth trading.\n")
    if ratio and ratio > 2:
        lines.append(
            f"> ⚠️ **The best cell makes {ratio:.1f}x what its neighbours do.** "
            f"That is a spike, not a plateau. A setting that only works at "
            f"exactly one value will not survive next year's volatility "
            f"regime.\n")
    elif ratio and ratio <= 1.3:
        lines.append(
            f"> The best cell is within {(ratio - 1) * 100:.0f}% of its "
            f"neighbours — a flat surface, which is the good case.\n")

    lines.append(
        f"\n*You have now looked at {summary['attempts']} variations. Run "
        f"enough and one of them looks significant on noise alone; that is why "
        f"this number is printed rather than the best one alone.*\n")

    # ---- the grid ----------------------------------------------------------
    if heat:
        lines.append(f"\n## Net P&L\n")
        lines.append(f"Rows: **{heat['rows']['field']}** · "
                     f"columns: **{heat['columns']['field']}**\n")
        header = " | ".join(str(c) for c in heat["columns"]["values"])
        lines.append(f"| | {header} |")
        lines.append("|---" * (len(heat["columns"]["values"]) + 1) + "|")
        for i, row in enumerate(heat["rows"]["values"]):
            cells = " | ".join(
                rupees(value, sign=True) if value is not None else "—"
                for value in heat["grid"]["net_pnl"][i])
            lines.append(f"| **{row}** | {cells} |")

        lines.append(f"\n## Max drawdown\n")
        lines.append(f"| | {header} |")
        lines.append("|---" * (len(heat["columns"]["values"]) + 1) + "|")
        for i, row in enumerate(heat["rows"]["values"]):
            cells = " | ".join(
                rupees(value) if value is not None else "—"
                for value in heat["grid"]["max_drawdown"][i])
            lines.append(f"| **{row}** | {cells} |")
    else:
        lines.append("\n## Every setting\n")
        fields = grid["fields"]
        lines.append("| " + " | ".join(fields)
                     + " | Trades | Net P&L | Max DD | Win rate | Return ÷ DD |")
        lines.append("|---" * (len(fields) + 5) + "|")
        for cell in grid["cells"]:
            values = " | ".join(str(cell["settings"][f]) for f in fields)
            lines.append(
                f"| {values} | {cell.get('trades', 0)} | "
                f"{rupees(cell.get('net_pnl'), sign=True)} | "
                f"{rupees(cell.get('max_drawdown'))} | "
                f"{cell.get('win_rate', 0)}% | "
                f"{cell.get('return_to_drawdown') or '—'} |")

    # ---- the peak, last ----------------------------------------------------
    best, worst = summary["best"], summary["worst"]
    lines.append("\n## The best and the worst of it\n")
    lines.append("| | Setting | Net P&L | Max drawdown | Win rate |\n"
                 "|---|---|---|---|---|")
    for label, cell in (("Best", best), ("Worst", worst)):
        setting = ", ".join(f"{k} {v}" for k, v in cell["settings"].items())
        lines.append(
            f"| {label} | {setting} | {rupees(cell['net_pnl'], sign=True)} | "
            f"{rupees(cell['max_drawdown'])} | {cell['win_rate']}% |")
    lines.append(
        "\n*Margin is estimated once from the base strategy and reused across "
        "the grid, so the cells share a denominator and are comparable. Run "
        "the setting you choose as its own numbered backtest before trading "
        "it — that run gets the full report, its own margin estimate and its "
        "own Monte Carlo.*\n")
    return "\n".join(lines) + "\n"
