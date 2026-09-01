"""Several strategies run together, which is not the sum of them run alone.

Every report in this system judges one strategy in isolation. Nobody trades one
strategy. And the difference is not a rounding error — it runs in both
directions at once:

**Margin nets, so the capital is less than the sum.** The exchange sees one
position per underlying, not one per strategy. A short call held by one strategy
sitting under a long call held by another is hedged, whether or not you thought
of them as related. Adding up each strategy's peak margin can overstate the
requirement by a wide margin on a book that happens to offset.

**Drawdowns net too, so the risk is also less than the sum** — unless the
strategies move together, in which case it is not. Two strategies each drawing
₹80,000 draw ₹1.6 L together only at a correlation of +1. Below that they
partially cancel, and *how much* is the entire question behind "how should I
split capital across three accounts".

**But the worst days can still line up.** Netting is not diversification. A book
of four short-premium strategies nets its margin beautifully and then loses on
all four legs on the same gap-down morning. This module reports both: what
netting saved, and how much of the risk was genuinely spread rather than merely
re-labelled.

## What it does

Runs each allocation over a common date range, then for every session takes the
union of that day's open legs **across all strategies** and margins them as one
book, through the same SPAN model everything else here uses. The combined equity
curve is the daily sum. Capital is peak netted margin plus the combined worst
run — the same definition a single report uses, applied to the thing you would
actually be holding.

## The date range

Defaults to the **overlap** of the strategies' own ranges, so the comparison is
like for like. A strategy that only covers the last year would otherwise make
the portfolio look thin for four years and then jump, and the jump would be an
artefact of coverage rather than anything about the strategies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from app.backtest import engine, library, montecarlo, registry
from app.backtest import report as report_mod
from app.backtest.engine import StrategySpec
from app.quant import strategy as strat


@dataclass
class Allocation:
    """One strategy, at a size."""
    strategy_id: str
    size: int = 1
    name: str = ""
    spec: StrategySpec | None = None
    underlying: str = "NIFTY"

    #: filled in as the portfolio runs
    trades: list[Any] = field(default_factory=list)
    columns: dict[str, np.ndarray] = field(default_factory=dict)
    # Position in the book. Two slots may legitimately hold the SAME strategy
    # at different sizes — splitting one idea across accounts, say — and keying
    # anything by `strategy_id` silently merged them into one member: the
    # correlation matrix lost a row and the second slot's margin overwrote the
    # first's.
    slot: int = 0

    @property
    def key(self) -> str:
        return f"{self.slot}:{self.strategy_id}"

    @property
    def label(self) -> str:
        return (self.strategy_id if self.slot == 0
                else f"{self.strategy_id} #{self.slot + 1}")


def _load(allocation: Allocation) -> Allocation:
    record = library.load(allocation.strategy_id)
    if record is None:
        raise ValueError(
            f"no saved strategy {allocation.strategy_id!r}. Promote a run "
            f"first: tools.backtest save <run id> --name '...'")
    allocation.name = record["name"]
    allocation.underlying = record.get("underlying", "NIFTY")
    allocation.spec = StrategySpec.from_dict(record["spec"])
    allocation.size = allocation.size or int(record.get("lots") or 1)
    return allocation


def _range(allocations: list[Allocation], start: date | None,
           end: date | None) -> tuple[date, date, dict[str, list[str]]]:
    """The window every strategy can actually be judged over."""
    from app.data import lake

    spans, notes = [], {}
    for allocation in allocations:
        rows = lake.query(
            "SELECT min(ts)::DATE, max(ts)::DATE FROM option_bars "
            "WHERE underlying = ? AND series = ?",
            [allocation.underlying, allocation.spec.expiry_flag])
        if rows and rows[0][0]:
            spans.append((rows[0][0], rows[0][1]))
    if not spans:
        raise ValueError("no data for any of these strategies")
    lo = max(s[0] for s in spans)
    hi = min(s[1] for s in spans)
    if start:
        lo = max(lo, start)
    if end:
        hi = min(hi, end)
    if lo >= hi:
        raise ValueError(
            "these strategies have no overlapping data, so they cannot be "
            "compared as a portfolio")
    return lo, hi, notes


def run(allocations: list[Allocation], start: date | None = None,
        end: date | None = None) -> dict[str, Any]:
    """Backtest a book of strategies held together."""
    if len(allocations) < 2:
        raise ValueError("a portfolio needs at least two strategies")
    for slot, allocation in enumerate(allocations):
        allocation.slot = slot
    allocations = [_load(a) for a in allocations]
    lo, hi, _ = _range(allocations, start, end)

    for allocation in allocations:
        result = engine.run(allocation.spec, allocation.underlying, lo, hi)
        allocation.trades = result.trades
        allocation.columns = engine.load_matrix(
            allocation.spec, allocation.underlying, lo, hi)

    # One pass builds every leg and margins it — the members and the combined
    # book — rather than each member re-deriving its own through
    # `margin_series`. Same numbers, roughly half the SPAN evaluations, and one
    # place where a leg becomes a margin instead of two that could drift.
    daily, margins, per_day_legs, alone = _combine(allocations)

    standalone: list[dict[str, Any]] = []
    for allocation in allocations:
        pnls = np.array([t.pnl * allocation.size for t in allocation.trades],
                        dtype=np.float64)
        peak = max(alone.get(allocation.key, {}).values(), default=0.0)
        drawdown = (float(engine.underwater(np.cumsum(pnls)).min())
                    if pnls.size else 0.0)
        standalone.append({
            "id": allocation.strategy_id,
            "name": allocation.name,
            "size": allocation.size,
            "underlying": allocation.underlying,
            "trades": int(pnls.size),
            "net_pnl": round(float(pnls.sum()), 2) if pnls.size else 0.0,
            "max_drawdown": round(drawdown, 2),
            # Margined at the allocated size, not at one unit scaled up: the
            # SPAN cap at max loss is not linear, so a 3x position is not
            # always 3x the requirement.
            "peak_margin": round(peak, 2),
            "capital_alone": round(peak + abs(drawdown), 2),
        })

    days = sorted(daily)
    pnls = np.array([daily[d] for d in days], dtype=np.float64)
    if not pnls.size:
        return {"note": "none of these strategies traded in the shared window",
                "start": lo.isoformat(), "end": hi.isoformat()}

    equity = np.cumsum(pnls)
    underwater = engine.underwater(equity)
    drawdown = float(underwater.min())
    peak_margin = max(margins.values()) if margins else 0.0
    capital = peak_margin + abs(drawdown)
    total = float(pnls.sum())

    # What the same book would have cost judged strategy by strategy.
    naive_margin = sum(s["peak_margin"] for s in standalone)
    naive_drawdown = sum(abs(s["max_drawdown"]) for s in standalone)
    naive_capital = naive_margin + naive_drawdown

    years = max((hi - lo).days, 1) / 365.25
    return {
        "period": {"start": lo.isoformat(), "end": hi.isoformat(),
                   "years": round(years, 2), "sessions": len(days)},
        "allocations": standalone,
        "headline": {
            "net_pnl": round(total, 2),
            "peak_margin": round(peak_margin, 2),
            "max_drawdown": round(drawdown, 2),
            "capital_floor": round(capital, 2),
            "roi_on_capital_pct": round(total / capital * 100.0, 2) if capital else None,
            "cagr_on_capital_pct": report_mod._cagr(
                total / capital * 100.0 if capital else None, years),
            "sharpe": report_mod._sharpe(pnls),
            "sortino": report_mod._sortino(pnls),
            "win_rate_pct": round(float((pnls > 0).mean()) * 100.0, 1),
            "return_to_drawdown": round(total / abs(drawdown), 2) if drawdown else None,
        },
        # The two numbers this module exists to produce.
        "netting": {
            "margin_alone": round(naive_margin, 2),
            "margin_together": round(peak_margin, 2),
            "margin_saved": round(naive_margin - peak_margin, 2),
            "margin_saved_pct": round(
                (naive_margin - peak_margin) / naive_margin * 100.0, 1)
                if naive_margin else None,
            "drawdown_alone": round(-naive_drawdown, 2),
            "drawdown_together": round(drawdown, 2),
            "drawdown_saved": round(naive_drawdown - abs(drawdown), 2),
            "drawdown_saved_pct": round(
                (naive_drawdown - abs(drawdown)) / naive_drawdown * 100.0, 1)
                if naive_drawdown else None,
            "capital_alone": round(naive_capital, 2),
            "capital_together": round(capital, 2),
            "capital_saved_pct": round(
                (naive_capital - capital) / naive_capital * 100.0, 1)
                if naive_capital else None,
        },
        "correlation": _correlation(allocations, days),
        "contribution": _contribution(allocations, total),
        "montecarlo": montecarlo.simulate(list(pnls), peak_margin=peak_margin or None),
        "monthly": _monthly(days, pnls),
        "curves": {
            "equity": [{"day": d.isoformat(), "equity": round(float(v), 2)}
                       for d, v in zip(days, equity)],
            "drawdown": [{"day": d.isoformat(), "drawdown": round(float(v), 2)}
                         for d, v in zip(days, underwater)],
            "margin": [{"day": d.isoformat(), "margin": round(margins[d], 2)}
                       for d in days if d in margins],
        },
        "concurrency": _concurrency(per_day_legs, len(allocations)),
    }


def _margin_of(legs: list[Any], spot: float) -> float | None:
    analysis = strat.analyse(legs, spot, report_mod.RISK_FREE_RATE)
    total = (analysis.get("margin") or {}).get("total")
    if total and np.isfinite(total) and total > 0:
        return float(total)
    return None


def _combine(allocations: list[Allocation]
             ) -> tuple[dict[date, float], dict[date, float], dict[date, int],
                        dict[str, dict[date, float]]]:
    """Daily P&L, netted margin, who was live, and each member's own margin.

    The members' margins come from the same legs as the book's, so "what this
    costs alone" and "what it costs here" are two readings of one calculation
    rather than two calculations that could disagree.
    """
    daily: dict[date, float] = {}
    legs_by_day: dict[date, list[Any]] = {}
    spot_by_day: dict[date, float] = {}
    live: dict[date, int] = {}
    alone: dict[str, dict[date, float]] = {}

    for allocation in allocations:
        own = alone.setdefault(allocation.key, {})
        seen: set[date] = set()
        for trade in allocation.trades:
            daily[trade.day] = daily.get(trade.day, 0.0) + trade.pnl * allocation.size
            if trade.day not in seen:
                seen.add(trade.day)
                live[trade.day] = live.get(trade.day, 0) + 1
            row = report_mod.row_at(trade, allocation.columns)
            if row is None:
                continue
            spot = float(allocation.columns["spot"][row])
            if not np.isfinite(spot) or spot <= 0:
                continue
            legs = report_mod.legs_at(trade, allocation.spec,
                                      allocation.columns, row,
                                      size=allocation.size)
            if not legs:
                continue
            legs_by_day.setdefault(trade.day, []).extend(legs)
            spot_by_day.setdefault(trade.day, spot)
            margin = _margin_of(legs, spot)
            if margin is not None:
                own[trade.day] = margin

    margins: dict[date, float] = {}
    for day, legs in legs_by_day.items():
        margin = _margin_of(legs, spot_by_day[day])
        if margin is not None:
            margins[day] = margin
    return daily, margins, live, alone


def _correlation(allocations: list[Allocation],
                 days: list[date]) -> dict[str, Any]:
    """Daily-P&L correlation between the members, on the shared calendar.

    Correlated members are the reason a portfolio's drawdown is not much better
    than its worst member's: netting margin does nothing about them losing on
    the same day.
    """
    series: dict[str, dict[date, float]] = {}
    for allocation in allocations:
        label = allocation.label
        series[label] = {t.day: t.pnl * allocation.size
                         for t in allocation.trades}
    names = list(series)
    matrix: dict[str, dict[str, float | None]] = {}
    worst = None
    for a in names:
        matrix[a] = {}
        for b in names:
            shared = [d for d in days if d in series[a] and d in series[b]]
            if len(shared) < 3:
                matrix[a][b] = None
                continue
            x = np.array([series[a][d] for d in shared])
            y = np.array([series[b][d] for d in shared])
            if x.std() == 0 or y.std() == 0:
                matrix[a][b] = None
                continue
            value = round(float(np.corrcoef(x, y)[0, 1]), 3)
            matrix[a][b] = value
            if a != b and (worst is None or value > worst[2]):
                worst = (a, b, value)
    return {"members": names, "matrix": matrix,
            "most_alike": {"pair": worst[:2], "correlation": worst[2]}
                          if worst else None}


def _contribution(allocations: list[Allocation],
                  total: float) -> list[dict[str, Any]]:
    out = []
    for allocation in allocations:
        made = sum(t.pnl for t in allocation.trades) * allocation.size
        out.append({
            "id": allocation.strategy_id, "name": allocation.name,
            "size": allocation.size, "net_pnl": round(made, 2),
            "share_pct": round(made / total * 100.0, 1) if total else None,
        })
    out.sort(key=lambda row: row["net_pnl"], reverse=True)
    return out


def _monthly(days: list[date], pnls: np.ndarray) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for day, pnl in zip(days, pnls):
        buckets.setdefault(f"{day.year:04d}-{day.month:02d}", []).append(float(pnl))
    return [{"key": key, "trades": len(values), "pnl": round(sum(values), 2),
             "average": round(sum(values) / len(values), 2),
             "win_rate": round(100.0 * sum(1 for v in values if v > 0)
                               / len(values), 1),
             "best": round(max(values), 2), "worst": round(min(values), 2)}
            for key, values in sorted(buckets.items())]


def _concurrency(live: dict[date, int], members: int) -> dict[str, Any]:
    """How often the book was actually full.

    A portfolio whose members rarely trade on the same day nets very little,
    and its capital saving is smaller than the headline suggests.
    """
    if not live:
        return {}
    counts = list(live.values())
    return {
        "members": members,
        "sessions": len(counts),
        "all_live_sessions": sum(1 for c in counts if c == members),
        "all_live_pct": round(sum(1 for c in counts if c == members)
                              / len(counts) * 100.0, 1),
        "average_live": round(sum(counts) / len(counts), 2),
    }


# ---------------------------------------------------------------------------
# the human-readable artefact
# ---------------------------------------------------------------------------

def to_markdown(result: dict[str, Any], run_id: str, name: str) -> str:
    rupees, pct = report_mod._rupees, report_mod._pct

    def cost(value):
        """Margin and capital are costs. `_rupees(sign=True)` would print a
        peak margin as '+₹6.34 L', which reads as a gain."""
        return rupees(value)
    if result.get("note"):
        return f"# {run_id} — {name} *(portfolio)*\n\n{result['note']}\n"

    head, netting, period = result["headline"], result["netting"], result["period"]
    lines = [f"# {run_id} — {name} *(portfolio)*\n"]
    lines.append(
        f"{len(result['allocations'])} strategies held together · "
        f"{period['start']} → {period['end']} · {period['sessions']:,} sessions "
        f"over {period['years']} years\n")

    lines.append("## The book\n")
    lines.append("| Strategy | Size | Trades | Net P&L | Alone it would need |"
                 "\n|---|---|---|---|---|")
    for row in result["allocations"]:
        lines.append(
            f"| {row['name']} | {row['size']} | {row['trades']:,} | "
            f"{rupees(row['net_pnl'], sign=True)} | "
            f"{cost(row['capital_alone'])} |")
    lines.append(f"| **Together** | | | "
                 f"**{rupees(head['net_pnl'], sign=True)}** | "
                 f"**{cost(head['capital_floor'])}** |")

    # ---- the point ---------------------------------------------------------
    lines.append("\n## What holding them together changed\n")
    lines.append("| | Judged one by one | Held together | Difference |\n"
                 "|---|---|---|---|")
    lines.append(
        f"| Peak margin | {rupees(netting['margin_alone'])} | "
        f"**{rupees(netting['margin_together'])}** | "
        f"{pct(-(netting['margin_saved_pct'] or 0))} |")
    lines.append(
        f"| Worst drawdown | {rupees(netting['drawdown_alone'])} | "
        f"**{rupees(netting['drawdown_together'])}** | "
        f"{pct(-(netting['drawdown_saved_pct'] or 0))} |")
    lines.append(
        f"| **Capital needed** | {rupees(netting['capital_alone'])} | "
        f"**{rupees(netting['capital_together'])}** | "
        f"**{pct(-(netting['capital_saved_pct'] or 0))}** |")

    saved = netting.get("capital_saved_pct") or 0
    if saved > 5:
        lines.append(
            f"\n> Holding these together needs **{saved:.0f}% less capital** "
            f"than running them as separate books. Margin nets because the "
            f"exchange sees one position per underlying, and the drawdowns "
            f"partly cancel because the strategies do not lose on the same "
            f"days.\n")
    elif saved < 1:
        lines.append(
            f"\n> **Almost nothing was saved by combining these.** They need "
            f"about as much capital together as apart, which means they are "
            f"either not overlapping in time or losing on the same days — see "
            f"the correlation below.\n")

    concurrency = result.get("concurrency") or {}
    if concurrency:
        lines.append(
            f"\nAll {concurrency['members']} were live together on "
            f"**{concurrency['all_live_pct']}%** of sessions "
            f"({concurrency['all_live_sessions']:,} of "
            f"{concurrency['sessions']:,}), averaging "
            f"{concurrency['average_live']} at a time. Netting only helps on "
            f"the days positions actually overlap.\n")

    # ---- the risk that does not net ---------------------------------------
    correlation = result.get("correlation") or {}
    alike = correlation.get("most_alike")
    if alike:
        lines.append("\n## How alike they are\n")
        names = correlation["members"]
        lines.append("| | " + " | ".join(names) + " |")
        lines.append("|---" * (len(names) + 1) + "|")
        for a in names:
            row = " | ".join(
                f"{correlation['matrix'][a][b]:+.2f}"
                if correlation["matrix"][a][b] is not None else "—"
                for b in names)
            lines.append(f"| **{a}** | {row} |")
        pair, value = alike["pair"], alike["correlation"]
        if value > 0.7:
            lines.append(
                f"\n> ⚠️ **{pair[0]} and {pair[1]} correlate at {value:+.2f}.** "
                f"They are close to one position at double size. Netting the "
                f"margin does nothing about them losing on the same day, which "
                f"is the risk that actually ends accounts.\n")
        else:
            lines.append(
                f"\n*The most alike pair is {pair[0]} and {pair[1]} at "
                f"{value:+.2f}. Below about +0.7 they are genuinely spreading "
                f"risk rather than doubling one bet.*\n")

    # ---- returns -----------------------------------------------------------
    lines.append("\n## What it returned\n")
    lines.append("| | |\n|---|---|")
    lines.append(f"| Net P&L | **{rupees(head['net_pnl'], sign=True)}** |")
    lines.append(f"| Capital needed | **{rupees(head['capital_floor'])}** |")
    lines.append(f"| Return on that capital | **{pct(head['roi_on_capital_pct'])}** |")
    lines.append(f"| Per year, compounded | {pct(head['cagr_on_capital_pct'])} |")
    lines.append(f"| Worst drawdown | {rupees(head['max_drawdown'])} |")
    lines.append(f"| Return ÷ drawdown | {head['return_to_drawdown']} |")
    lines.append(f"| Sharpe / Sortino | {head['sharpe']} / {head['sortino']} |")
    lines.append(f"| Profitable sessions | {head['win_rate_pct']}% |")

    lines.append("\n## Who earned it\n")
    lines.append("| Strategy | Size | Net P&L | Share |\n|---|---|---|---|")
    for row in result["contribution"]:
        lines.append(
            f"| {row['name']} | {row['size']} | "
            f"{rupees(row['net_pnl'], sign=True)} | "
            f"{pct(row['share_pct'], sign=False) if row['share_pct'] is not None else '—'} |")

    mc = result.get("montecarlo") or {}
    if mc.get("paths") and mc.get("losses_cluster"):
        lines.append(
            f"\n> ⚠️ **The book's losses cluster.** Its worst drawdown is "
            f"{mc['drawdown_vs_typical_ordering']}x deeper than a typical "
            f"reshuffle of the same sessions — combining strategies has not "
            f"broken up the bad stretches, it has stacked them.\n")

    lines.append(
        "\n---\n\n*Margin is the SPAN estimate on the union of each session's "
        "open legs across every member, which is how the exchange assesses it. "
        "Sizes scale the model linearly, which is very nearly true for the same "
        "structure and not exactly true.*\n")
    return "\n".join(lines) + "\n"
