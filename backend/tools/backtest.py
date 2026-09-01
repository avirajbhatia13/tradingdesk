"""Run, list, sweep and compare backtests from one command.

    ../.venv/bin/python -m tools.backtest run --name "short straddle 9:20" \
        --legs "SELL CE 0, SELL PE 0" --entry 09:20 --exit 15:15

    ../.venv/bin/python -m tools.backtest sweep --name "straddle SL grid" \
        --legs "SELL CE 0, SELL PE 0" \
        --over "stop_loss_pct=0.2,0.3,0.4" --over "target_pct=0.3,0.5"

    ../.venv/bin/python -m tools.backtest list
    ../.venv/bin/python -m tools.backtest show 001
    ../.venv/bin/python -m tools.backtest correlate

This is the single entry point on purpose. An assistant pointed at this
repository should not be assembling a report out of library calls and hoping it
got the margin convention right — it runs one command, and the numbered,
reproducible artefact on disk is the result. See `BACKTESTING.md`.

## Leg grammar

    SIDE TYPE <how to pick the strike> [xLOTS] [roll]

The strike-picking part is the interesting half, and it is written the way the
rule is spoken:

    SELL CE 0            at the money
    BUY PE 5             five strikes out of the money
    SELL CE @120         the call trading nearest ₹120
    BUY CE @atm/3        the call trading nearest a third of the ATM call
    BUY PE @atm/3        ...and the put nearest a third of the ATM put
    SELL CE 20d          the 20-delta call
    SELL PE 1%           the put one percent out of the money
    SELL CE 200pt        the call 200 points out of the money
    BUY CE k23000        the 23000 call, by strike

Moneyness is signed and means *strikes out of the money* on both sides, so `+5`
is an OTM call and an OTM put alike.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, time
from typing import Any, Callable

from app.backtest import costs as costs_mod
from app.backtest import library as library_mod
from app.backtest import portfolio as portfolio_mod
from app.backtest import registry, report as report_mod
from app.backtest import selectors as sel
from app.backtest import sweep as sweep_mod
from app.backtest import walkforward as wf_mod
from app.backtest.engine import contract_label, LegSpec, StrategySpec, load_matrix, run as run_backtest

LEG_PATTERN = re.compile(
    r"^\s*(?P<side>BUY|SELL)\s+(?P<type>CE|PE)\s+(?P<pick>\S+)"
    r"(?:\s*[x*]\s*(?P<lots>\d+))?\s*(?P<roll>roll)?\s*$", re.I)

# Ordered: the first pattern that matches wins, so the more specific forms —
# a premium ratio, a delta — are tried before the bare integer that would
# otherwise swallow them.
_SELECTORS: tuple[tuple[re.Pattern, Callable[[Any], sel.Selector]], ...] = (
    # @atm/3, @atm*0.5, @+5/3 — a fraction of another strike's premium.
    (re.compile(r"^@(?P<ref>atm|[+-]?\d+)\s*(?P<op>[/*])\s*(?P<n>[\d.]+)$", re.I),
     lambda m: sel.ByPremiumRatio(
         0 if m["ref"].lower() == "atm" else int(m["ref"]),
         (1.0 / float(m["n"])) if m["op"] == "/" else float(m["n"]))),
    # @120 — a target premium in rupees.
    (re.compile(r"^@(?P<n>[\d.]+)$"),
     lambda m: sel.ByPremium(float(m["n"]))),
    # 20d / 0.2delta — a target delta. Above 1 is read as a percentage,
    # because "the 20 delta" is what people say and 20.0 is not a delta.
    (re.compile(r"^(?P<n>[\d.]+)\s*(?:d|delta)$", re.I),
     lambda m: sel.ByDelta(float(m["n"]) / 100.0 if float(m["n"]) > 1
                           else float(m["n"]))),
    # 1% — a percentage of spot, out of the money.
    (re.compile(r"^(?P<n>[\d.]+)\s*%$"),
     lambda m: sel.ByPctOfSpot(float(m["n"]) / 100.0)),
    # 200pt — a fixed distance in index points, out of the money.
    (re.compile(r"^(?P<n>[\d.]+)\s*(?:pt|pts|points)$", re.I),
     lambda m: sel.ByStrikeOffset(float(m["n"]))),
    # k23000 — one absolute strike.
    (re.compile(r"^(?:k|strike)\s*(?P<n>[\d.]+)$", re.I),
     lambda m: sel.ByStrike(float(m["n"]))),
    # 0, +5, -3 — strikes out of the money, the original axis.
    (re.compile(r"^(?P<n>[+-]?\d+)$"),
     lambda m: sel.ByMoneyness(int(m["n"]))),
)

_LEG_HELP = (
    "Expected e.g. 'SELL CE 0' (at the money), 'BUY PE 5' (five strikes OTM), "
    "'SELL CE @120' (nearest ₹120), 'BUY CE @atm/3' (a third of the ATM "
    "premium), 'SELL CE 20d' (20 delta), 'SELL PE 1%' (1% OTM), "
    "'SELL CE 200pt', 'BUY CE k23000'. Add 'x2' for lots, 'roll' to re-strike."
)


def parse_selector(text: str) -> sel.Selector:
    for pattern, build in _SELECTORS:
        match = pattern.match(text.strip())
        if match:
            return build(match)
    raise ValueError(f"cannot read strike rule {text!r}. {_LEG_HELP}")


def parse_legs(text: str) -> list[LegSpec]:
    """`"SELL CE 0, BUY CE @atm/3 x3"` -> two LegSpecs.

    Raises on anything unparseable rather than skipping it. A silently dropped
    leg turns a hedged position into a naked one and still produces a plausible
    report, which is the worst failure this parser could have.
    """
    legs = []
    for chunk in filter(None, (c.strip() for c in text.split(","))):
        match = LEG_PATTERN.match(chunk)
        if not match:
            raise ValueError(f"cannot read leg {chunk!r}. {_LEG_HELP}")
        selector = parse_selector(match["pick"])
        legs.append(LegSpec(
            opt_type=match["type"].upper(),
            side=match["side"].upper(),
            lots=int(match["lots"] or 1),
            restrike=bool(match["roll"]),
            select=selector,
        ))
    if not legs:
        raise ValueError("no legs given")
    return legs


def _time(text: str, fallback: time) -> time:
    if not text:
        return fallback
    hour, _, minute = text.partition(":")
    return time(int(hour), int(minute or 0))


def _lake_range(underlying: str) -> tuple[date, date]:
    """Default to what the lake actually holds, so a run without explicit dates
    returns trades instead of an empty result to diagnose."""
    from app.data import lake

    rows = lake.query(
        "SELECT min(ts)::DATE, max(ts)::DATE FROM option_bars WHERE underlying = ?",
        [underlying])
    if rows and rows[0][0]:
        return rows[0][0], rows[0][1]
    return date(2021, 1, 1), date.today()


def _expiry_index(args: argparse.Namespace) -> int | None:
    """`--expiry front|next|N` into a ladder position, or None for rolling.

    None is not the same as 0. None means "use the rolling series", which is
    the only thing Dhan's data supports and what every stored run was produced
    with; 0 means "the nearest real expiry", which needs rows that name their
    contract. Defaulting one to the other would silently change what a run is
    about.
    """
    raw = getattr(args, "expiry", None)
    if raw in (None, ""):
        if getattr(args, "dte_min", None) is not None or \
                getattr(args, "dte_max", None) is not None:
            raise SystemExit(
                "--dte-min/--dte-max need --expiry: days to expiry is only "
                "defined once the contract has a date. Rolling series data "
                "carries none.")
        return None
    text = str(raw).strip().lower()
    if text in ("front", "near", "nearest"):
        return 0
    if text == "next":
        return 1
    try:
        value = int(text)
    except ValueError:
        raise SystemExit(f"--expiry: expected front, next or a number, got {raw!r}")
    if value < 0:
        raise SystemExit("--expiry cannot be negative; 0 is the front expiry")
    return value


def _build_spec(args: argparse.Namespace) -> tuple[StrategySpec, date, date]:
    legs = parse_legs(args.legs)
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    if start is None or end is None:
        lo, hi = _lake_range(args.underlying)
        start, end = start or lo, end or hi

    spec = StrategySpec(
        name=args.name,
        legs=legs,
        entry_time=_time(args.entry, time(9, 20)),
        exit_time=_time(args.exit, time(15, 15)),
        stop_loss=args.stop, target=args.target,
        stop_loss_pct=args.stop_pct, target_pct=args.target_pct,
        trail_stop=args.trail, trail_stop_pct=args.trail_pct,
        trail_trigger=args.trail_trigger,
        trail_trigger_pct=args.trail_trigger_pct,
        breakeven_trigger=args.breakeven,
        breakeven_trigger_pct=args.breakeven_pct,
        per_leg_stop_pct=args.leg_stop_pct,
        per_leg_stop_points=args.leg_stop_points,
        per_leg_action=args.leg_action,
        hold_days=args.hold,
        re_entries=args.re_entries,
        re_entry_on=args.re_entry_on,
        re_entry_gap_minutes=args.re_entry_gap,
        min_atm_iv=args.min_iv, max_atm_iv=args.max_iv,
        gap_pct_min=args.gap_min, gap_pct_max=args.gap_max,
        day_move_pct_min=args.move_min, day_move_pct_max=args.move_max,
        expiry_flag=args.series.upper(),
        expiry_index=_expiry_index(args),
        expiry_kind=getattr(args, "expiry_kind", "any"),
        min_dte=getattr(args, "dte_min", None),
        max_dte=getattr(args, "dte_max", None),
        weekdays=tuple(int(d) for d in args.weekdays.split(",")) if args.weekdays else None,
        lot_size=args.lot_size,
        lot_calendar=getattr(args, "lot_calendar", False),
        slippage_points=args.slippage,
        costs=costs_mod.CostModel(brokerage_per_order=args.brokerage),
        adjust=_adjust_plan(args),
    )
    return spec, start, end


def _adjust_plan(args: argparse.Namespace):
    """Build the repair rules, or None for an ordinary fixed-basket run."""
    from app.backtest import adjust as adj

    rules = [r for r in (getattr(args, "adjust", None) or []) if r.strip()]
    if not rules:
        return None
    wings = getattr(args, "wings", None)
    if wings not in (None, "breakeven"):
        try:
            wings = int(wings)
        except (TypeError, ValueError):
            raise SystemExit(
                f"--wings takes 'breakeven' or a number of strikes, not {wings!r}")
    return adj.AdjustPlan(
        rules=tuple(adj.AdjustRule.parse(r) for r in rules),
        max_adjustments=getattr(args, "adjust_max", None),
        no_crossover=not getattr(args, "allow_crossover", False),
        wings=wings,
    )


def cmd_run(args: argparse.Namespace) -> int:
    spec, start, end = _build_spec(args)

    print(f"running {args.name!r} on {args.underlying} {contract_label(spec)} "
          f"{start} → {end} …", file=sys.stderr)
    print(f"  {spec.describe()}", file=sys.stderr)
    result = run_backtest(spec, args.underlying, start, end)
    if not result.trades:
        print(f"no trades: {result.stats.get('note', '')}", file=sys.stderr)
        if result.selection.unresolved_days:
            print(f"  {result.selection.unresolved_days} of "
                  f"{result.selection.days} days could not resolve every leg — "
                  f"is the target inside the ±10 strikes the lake holds?",
                  file=sys.stderr)
        return 1

    columns = load_matrix(spec, args.underlying, start, end)
    report = report_mod.build(result, spec, args.underlying, start, end, columns)

    if args.dry_run:
        print(report_mod.to_markdown(report, "DRY", args.name))
        return 0

    run_id = registry.next_id()
    markdown = report_mod.to_markdown(report, run_id, args.name)
    entry = registry.save(args.name, spec, report, markdown,
                          args.underlying, start, end, notes=args.notes or "")

    directory = registry.find(entry["id"])
    print(f"\nsaved as {entry['id']} — {directory}", file=sys.stderr)
    print(markdown)
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    spec, start, end = _build_spec(args)
    axes: dict[str, list] = {}
    for item in args.over:
        field, _, values = item.partition("=")
        if not values:
            print(f"bad --over {item!r}: expected field=v1,v2,v3", file=sys.stderr)
            return 1
        axes[field.strip()] = [_axis_value(v.strip()) for v in values.split(",")]

    total = 1
    for values in axes.values():
        total *= len(values)
    print(f"sweeping {total} combinations of "
          f"{', '.join(axes)} on {args.underlying} {spec.expiry_flag} …",
          file=sys.stderr)

    try:
        grid = sweep_mod.run(spec, axes, args.underlying, start, end)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    markdown = sweep_mod.to_markdown(grid, "DRY" if args.dry_run else
                                     registry.next_id(), args.name, spec,
                                     args.underlying, start, end)
    if args.dry_run:
        print(markdown)
        return 0
    entry = registry.save_sweep(args.name, spec, grid, markdown,
                                args.underlying, start, end,
                                notes=args.notes or "")
    print(f"\nsaved as {entry['id']} — {registry.find(entry['id'])}",
          file=sys.stderr)
    print(markdown)
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    spec, start, end = _build_spec(args)
    axes: dict[str, list] = {}
    for item in args.over:
        field, _, values = item.partition("=")
        if not values:
            print(f"bad --over {item!r}: expected field=v1,v2,v3", file=sys.stderr)
            return 1
        axes[field.strip()] = [_axis_value(v.strip()) for v in values.split(",")]

    print(f"walking {args.folds} folds forward over {', '.join(axes)} …",
          file=sys.stderr)
    try:
        result = wf_mod.run(spec, axes, args.underlying, start, end,
                            folds=args.folds, scheme=args.scheme,
                            metric=args.metric)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    run_id = "DRY" if args.dry_run else registry.next_id()
    markdown = wf_mod.to_markdown(result, run_id, args.name, spec,
                                  args.underlying, start, end)
    if args.dry_run:
        print(markdown)
        return 0
    entry = registry.save_walkforward(args.name, spec, result, markdown,
                                      args.underlying, start, end,
                                      notes=args.notes or "")
    print(f"\nsaved as {entry['id']} — {registry.find(entry['id'])}",
          file=sys.stderr)
    print(markdown)
    return 0


def _axis_value(text: str):
    """Sweep values are written bare; guess the type the field wants."""
    if text.lower() in ("none", "null", ""):
        return None
    if re.match(r"^\d{1,2}:\d{2}$", text):
        return text
    try:
        return int(text) if re.match(r"^-?\d+$", text) else float(text)
    except ValueError:
        return text


def cmd_list(args: argparse.Namespace) -> int:
    index = registry.load_index()
    if not index:
        print("no backtests yet. Run one with:\n"
              "  ../.venv/bin/python -m tools.backtest run "
              "--name 'short straddle' --legs 'SELL CE 0, SELL PE 0'")
        return 0
    if args.json:
        print(json.dumps(index, indent=2))
        return 0
    # ROI here is on the capital an account would actually have needed, not on
    # margin. Ranking a list by return-on-margin puts every defined-risk
    # structure at the top for reasons that have nothing to do with edge.
    print(f"{'id':<5}{'name':<32}{'net P&L':>12}{'capital':>12}{'ROI':>8}"
          f"{'max DD':>12}{'win%':>7}  period")
    print("-" * 108)
    for entry in index:
        if entry.get("archived") and not args.all:
            continue
        if entry.get("kind") == "walkforward":
            eff = entry.get("efficiency")
            print(f"{entry['id']:<5}{entry['name'][:31]:<32}"
                  f"{(entry.get('net_pnl') or 0):>12,.0f}"
                  f"{(entry.get('capital_floor') or 0):>12,.0f}"
                  f"{(f'{eff:.0%}' if eff is not None else '—'):>8}"
                  f"{(entry.get('max_drawdown') or 0):>12,.0f}"
                  f"{(entry.get('win_rate_pct') or 0):>7.1f}  "
                  f"walk-forward, out-of-sample only")
            continue
        if entry.get("kind") == "sweep":
            best = entry.get("best_net_pnl")
            print(f"{entry['id']:<5}{entry['name'][:31]:<32}"
                  f"{'sweep':>12}{'':>12}{'':>8}"
                  f"{'':>12}{'':>7}  {entry.get('cells', 0)} cells, best "
                  f"{(best or 0):,.0f}")
            continue
        roi = entry.get("roi_on_capital_pct")
        print(f"{entry['id']:<5}{entry['name'][:31]:<32}"
              f"{(entry.get('net_pnl') or 0):>12,.0f}"
              f"{(entry.get('capital_floor') or 0):>12,.0f}"
              f"{(f'{roi:.1f}%' if roi is not None else '—'):>8}"
              f"{(entry.get('max_drawdown') or 0):>12,.0f}"
              f"{(entry.get('win_rate_pct') or 0):>7.1f}  "
              f"{entry.get('start')} → {entry.get('end')}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    run = registry.load(args.id)
    if not run:
        print(f"no run {args.id!r}. Try `list`.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(run.get("report", {}), indent=2))
    else:
        print(run.get("markdown", "(no report)"))
    return 0


def cmd_correlate(args: argparse.Namespace) -> int:
    curves = registry.equity_curves(args.ids.split(",") if args.ids else None)
    if len(curves) < 2:
        print("need at least two saved runs to correlate.", file=sys.stderr)
        return 1
    result = report_mod.correlate(curves)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    names = result["strategies"]
    width = max(len(n) for n in names) + 2
    print("\nDaily-P&L correlation. Two strategies near +1 are one bet at "
          "double size;\nnear 0 they genuinely diversify.\n")
    print(" " * width + "".join(f"{n[:10]:>12}" for n in names))
    for a in names:
        row = "".join(
            f"{(f'{result['matrix'][a][b]:+.2f}' if result['matrix'][a][b] is not None else '—'):>12}"
            for b in names)
        print(f"{a[:width - 2]:<{width}}{row}")
    return 0


def cmd_star(args: argparse.Namespace) -> int:
    for run_id in args.ids.split(","):
        run_id = run_id.strip()
        if not run_id:
            continue
        if registry.star(run_id, not args.clear):
            print(f"{'un-starred' if args.clear else 'starred'} {run_id}")
        else:
            print(f"no run {run_id!r}", file=sys.stderr)
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    """Backtest several saved strategies held together."""
    allocations = []
    for item in args.hold:
        for part in item.split(","):
            part = part.strip()
            if not part:
                continue
            name, _, size = part.partition(":")
            allocations.append(portfolio_mod.Allocation(
                strategy_id=name.strip(), size=int(size or 0)))
    if len(allocations) < 2:
        print("a portfolio needs at least two strategies, e.g. "
              "--hold 'iron-condor-3-8:1,atm-straddle-25-sl-thursdays:2'",
              file=sys.stderr)
        return 1

    print(f"holding {len(allocations)} strategies together …", file=sys.stderr)
    try:
        result = portfolio_mod.run(
            allocations,
            date.fromisoformat(args.start) if args.start else None,
            date.fromisoformat(args.end) if args.end else None)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    run_id = "DRY" if args.dry_run else registry.next_id()
    markdown = portfolio_mod.to_markdown(result, run_id, args.name)
    if args.dry_run:
        print(markdown)
        return 0
    period = result.get("period") or {}
    entry = registry.save_portfolio(
        args.name,
        [{"strategy": a.strategy_id, "size": a.size, "name": a.name}
         for a in allocations],
        result, markdown, allocations[0].underlying,
        date.fromisoformat(period["start"]), date.fromisoformat(period["end"]),
        notes=args.notes or "")
    print(f"\nsaved as {entry['id']} — {registry.find(entry['id'])}",
          file=sys.stderr)
    print(markdown)
    return 0


def cmd_save(args: argparse.Namespace) -> int:
    """Promote a numbered run into the named strategy library."""
    try:
        record = library_mod.from_run(args.id, name=args.name,
                                      notes=args.notes or "", lots=args.lots)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    evidence = library_mod.evidence(record)
    print(f"saved strategy {record['id']!r} — {record['name']}")
    for check in evidence["checks"]:
        mark = "yes" if check["done"] else ("!!" if check.get("warn") else "no")
        print(f"  [{mark:>3}] {check['label']:<26} {check['detail']}")
    return 0


def cmd_strategies(args: argparse.Namespace) -> int:
    records = library_mod.list_all()
    if not records:
        print("no saved strategies yet. Promote a run with:\n"
              "  ../.venv/bin/python -m tools.backtest save 004 "
              "--name 'ATM straddle Thursdays'")
        return 0
    if args.json:
        print(json.dumps(records, indent=2))
        return 0
    print(f"{'id':<34}{'status':<10}{'lots':>5}  {'ready':<7}evidence")
    print("-" * 100)
    for record in records:
        evidence = record["evidence"]
        ticks = "".join("+" if c["done"] else ("!" if c.get("warn") else "-")
                        for c in evidence["checks"])
        runs = ", ".join(evidence["runs"] + evidence["sweeps"]
                         + evidence["walkforwards"]) or "none"
        print(f"{record['id'][:33]:<34}{record['status']:<10}"
              f"{record['lots']:>5}  {ticks:<7}{runs}")
    print("\nevidence: backtested / swept / walk-forward "
          "(+ done, - missing, ! ran and failed)")
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    index = registry.rebuild_index()
    print(f"rebuilt index from {len(index)} run directories")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-run every saved run from its own spec and compare to the record.

    This is the guarantee the whole record rests on: a stored run re-runs **to
    the rupee**. It is the only thing standing between a refactor and silently
    changing what every saved backtest means, and it has already caught two
    real bugs — a legacy `moneyness` field that re-ran every stored condor as a
    straddle, and a drawdown that ignored a first-trade loss.

    Run it before and after touching the engine, the report, the selectors or
    the cost model. A drift is not a rounding difference to wave through.
    """
    from datetime import date as _date

    matched, drifted, skipped = 0, [], 0
    for entry in registry.load_index():
        run_id = entry["id"]
        if entry.get("kind") in ("sweep", "portfolio", "walkforward"):
            skipped += 1
            continue
        saved = registry.load(run_id)
        payload = ((saved or {}).get("spec") or {}).get("spec")
        if not payload:
            skipped += 1
            continue

        spec = StrategySpec.from_dict(payload)
        meta = saved["spec"]
        underlying = meta.get("underlying") or saved["report"]["underlying"]
        period = saved["report"]["period"]
        start = _date.fromisoformat(period["start"])
        end = _date.fromisoformat(period["end"])

        result = run_backtest(spec, underlying, start, end)
        built = report_mod.build(result, spec, underlying, start, end)
        now = built["headline"]["net_pnl"]
        was = saved["report"]["headline"]["net_pnl"]

        if abs(now - was) < 0.01:
            matched += 1
            print(f"  {run_id}  MATCH   {was:>16,.2f}")
        else:
            drifted.append(run_id)
            print(f"  {run_id}  DRIFT   stored {was:>16,.2f}   now {now:>16,.2f}"
                  f"   ({now - was:+,.2f})")

    print(f"\n{matched} match, {len(drifted)} drifted, {skipped} skipped")
    if drifted:
        print("\nA drifted run means the stored report no longer describes what "
              "the engine does.\nEither the change was wrong, or every affected "
              "run needs re-baselining on purpose\nwith a note saying why: "
              + ", ".join(drifted))
    return 1 if drifted else 0


def cmd_rerender(args: argparse.Namespace) -> int:
    """Rebuild saved reports in place, so older runs gain newer report sections.

    A run's *result* is its trades, and those are reproducible from `spec.json`
    to the rupee — which is what makes this safe. Re-running and rewriting the
    report gives a run recorded months ago the analysis the report grew since,
    without allocating a new id and without pretending it was run today.

    The net P&L is checked against the stored figure first and a mismatch aborts
    that run untouched. That is the difference between re-rendering a record and
    quietly re-baselining it: if the numbers moved, the engine changed, and that
    is a decision for a person to make with a note attached.
    """
    from datetime import date as _date

    if getattr(args, "rebaseline", False) and not (args.reason or "").strip():
        print("--rebaseline needs --reason: a re-baselined number without a "
              "stated cause is indistinguishable from a silent overwrite.",
              file=sys.stderr)
        return 2

    wanted = {i.strip().zfill(3) for i in args.ids.split(",") if i.strip()} \
        if args.ids else None

    done, drifted, skipped = [], [], 0
    for entry in registry.load_index():
        run_id = entry["id"]
        if wanted is not None and run_id not in wanted:
            continue
        if entry.get("kind") in ("sweep", "portfolio", "walkforward"):
            skipped += 1
            continue
        saved = registry.load(run_id)
        payload = ((saved or {}).get("spec") or {}).get("spec")
        if not payload:
            skipped += 1
            continue

        spec = StrategySpec.from_dict(payload)
        meta = saved["spec"]
        underlying = meta.get("underlying") or saved["report"]["underlying"]
        period = saved["report"]["period"]
        start = _date.fromisoformat(period["start"])
        end = _date.fromisoformat(period["end"])

        result = run_backtest(spec, underlying, start, end)
        if not result.trades:
            skipped += 1
            print(f"  {run_id}  SKIP    no trades on re-run", file=sys.stderr)
            continue

        # With the matrix, unlike `verify` — the margin block is most of the
        # report's value and it cannot be computed without the columns.
        columns = load_matrix(spec, underlying, start, end)
        built = report_mod.build(result, spec, underlying, start, end, columns)
        now = built["headline"]["net_pnl"]
        was = saved["report"]["headline"]["net_pnl"]
        if abs(now - was) >= 0.01 and not args.rebaseline:
            drifted.append(run_id)
            print(f"  {run_id}  DRIFT   stored {was:>15,.2f}   now {now:>15,.2f}"
                  f"  — left untouched", file=sys.stderr)
            continue

        name = meta.get("name") or entry.get("name") or run_id
        if abs(now - was) >= 0.01:
            # Re-baselining is deliberate and it is recorded. The note is
            # appended to the run's own notes rather than kept in a changelog,
            # because the question "why is this number different from the one I
            # remember" is asked while looking at the run.
            print(f"  {run_id}  REBASE  stored {was:>15,.2f}   now "
                  f"{now:>15,.2f}", file=sys.stderr)
            stamp = _date.today().isoformat()
            built["notes"] = ((meta.get("notes") or "") +
                              f"\n\n[re-baselined {stamp}] net P&L moved from "
                              f"{was:,.2f} to {now:,.2f}. {args.reason}").strip()
            meta["notes"] = built["notes"]
            registry.rewrite_spec(run_id, meta)
        registry.rewrite(run_id, built, report_mod.to_markdown(built, run_id, name))
        done.append(run_id)
        print(f"  {run_id}  WROTE   {was:>15,.2f}   {name}", file=sys.stderr)

    print(f"\n{len(done)} rewritten, {len(drifted)} drifted, {skipped} skipped",
          file=sys.stderr)
    if drifted:
        print("Drifted runs were not written. The engine no longer reproduces "
              "them, which is a\nre-baselining decision, not a re-render: "
              + ", ".join(drifted), file=sys.stderr)
    return 1 if drifted else 0


def _strategy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", required=True, help="what this strategy is called")
    # argparse runs help strings through %-formatting, so the "1%" in the leg
    # grammar has to be doubled — otherwise `--help` raises instead of helping.
    parser.add_argument("--legs", required=True,
                        help=_LEG_HELP.replace("%", "%%"))
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--series", default="WEEK",
                        choices=("WEEK", "MONTH", "week", "month"),
                        help="which ROLLING series to trade. Applies to Dhan's "
                             "data, which names no contract — see --expiry for "
                             "the dated alternative")
    parser.add_argument("--expiry", default=None,
                        help="trade a real expiry DATE instead of a rolling "
                             "series: 'front' (or 0) is the nearest live "
                             "expiry, 'next' (or 1) the one after, and so on. "
                             "This is what reaches the full-chain data — every "
                             "strike, not just ±10 — because those rows carry "
                             "an expiry and no series. Overrides --series")
    parser.add_argument("--expiry-kind", default="any",
                        choices=("any", "weekly", "monthly"),
                        help="narrow the expiry ladder before picking from it, "
                             "using the vendor's own weekly flag")
    parser.add_argument("--dte-min", type=int, default=None,
                        help="only trade sessions with at least this many days "
                             "to expiry. --dte-min 1 is 'never hold into the "
                             "last session'. Needs --expiry")
    parser.add_argument("--dte-max", type=int, default=None,
                        help="only trade sessions with at most this many days "
                             "to expiry. --dte-max 0 is 'expiry day only'. "
                             "Needs --expiry")
    parser.add_argument("--start"); parser.add_argument("--end")
    parser.add_argument("--entry", default="09:20")
    parser.add_argument("--exit", default="15:15")
    parser.add_argument("--stop", type=float, default=None, help="rupees")
    parser.add_argument("--target", type=float, default=None, help="rupees")
    parser.add_argument("--stop-pct", type=float, default=None,
                        help="fraction of the credit, e.g. 0.3")
    parser.add_argument("--target-pct", type=float, default=None)
    parser.add_argument("--trail", type=float, default=None,
                        help="rupees given back from the best the trade has been")
    parser.add_argument("--trail-pct", type=float, default=None,
                        help="same, as a fraction of the credit")
    parser.add_argument("--trail-trigger", type=float, default=None,
                        help="profit in rupees before the trail arms")
    parser.add_argument("--trail-trigger-pct", type=float, default=None)
    parser.add_argument("--breakeven", type=float, default=None,
                        help="profit in rupees after which the stop moves to entry")
    parser.add_argument("--breakeven-pct", type=float, default=None)
    parser.add_argument("--leg-stop-pct", type=float, default=None,
                        help="per-leg stop, fraction of that leg's entry premium")
    parser.add_argument("--leg-stop-points", type=float, default=None)
    parser.add_argument(
        "--adjust", action="append", metavar="RULE",
        help="repair the position while it is open, e.g. "
             "'gap>=40%%: roll-cheap-to-expensive'. Repeatable. Needs --expiry, "
             "because re-selecting a strike mid-trade reads the whole chain at "
             "that minute")
    parser.add_argument(
        "--adjust-max", type=int, default=None, metavar="N",
        help="cap the repairs per position; default is unlimited")
    parser.add_argument(
        "--allow-crossover", action="store_true",
        help="let a rolled leg pass the other leg's strike. Off by default: "
             "once the strikes meet the position is a straddle, which is what "
             "the 'no crossover' rule in these strategies means")
    parser.add_argument(
        "--wings", default=None, metavar="WHERE",
        help="buy protective wings once the position becomes a straddle: "
             "'breakeven' places them at the straddle's breakevens, a number "
             "places them that many strikes out")
    parser.add_argument("--leg-action", default="all", choices=("all", "leg"),
                        help="'all' closes everything on a leg stop; "
                             "'leg' closes only that leg")
    parser.add_argument("--hold", type=int, default=0, metavar="SESSIONS",
                        help="hold across sessions instead of closing the same "
                             "day. Never crosses a contract roll — the hold is "
                             "truncated at expiry and the report says how often")
    parser.add_argument("--re-entries", type=int, default=0,
                        help="extra attempts per day after being stopped out")
    parser.add_argument("--re-entry-on", default="stop",
                        choices=("stop", "target", "both"))
    parser.add_argument("--re-entry-gap", type=int, default=1,
                        help="minutes to wait before re-entering")
    parser.add_argument("--min-iv", type=float, default=None,
                        help="only enter when ATM IV is at least this (decimal)")
    parser.add_argument("--max-iv", type=float, default=None)
    parser.add_argument("--gap-min", type=float, default=None,
                        help="only enter when the open gapped at least this %%")
    parser.add_argument("--gap-max", type=float, default=None)
    parser.add_argument("--move-min", type=float, default=None,
                        help="only enter when spot has moved this %% from the open")
    parser.add_argument("--move-max", type=float, default=None)
    parser.add_argument("--weekdays", default=None, help="0=Mon, e.g. '0,2,4'")
    parser.add_argument("--lot-size", type=int, default=75)
    parser.add_argument("--lot-calendar", action="store_true",
                        help="size each session by the lot size actually in "
                             "force, not one constant. Only known back to "
                             "2024-10; earlier sessions fall back to "
                             "--lot-size and the report says how many")
    parser.add_argument("--slippage", type=float, default=0.5,
                        help="points per leg per side")
    parser.add_argument("--brokerage", type=float, default=20.0, help="per order")
    parser.add_argument("--notes", default="", help="why this run exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the report without saving or consuming an id")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tools.backtest", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True)

    r = subs.add_parser("run", help="run a backtest and save it")
    _strategy_arguments(r)
    r.set_defaults(func=cmd_run)

    s = subs.add_parser(
        "sweep", help="run the same strategy across a grid of settings")
    _strategy_arguments(s)
    s.add_argument("--over", action="append", required=True,
                   metavar="FIELD=V1,V2,V3",
                   help="a setting to vary, e.g. 'stop_loss_pct=0.2,0.3,0.4'. "
                        "Repeat for a second axis and a heatmap is produced.")
    s.set_defaults(func=cmd_sweep)

    w = subs.add_parser(
        "walkforward",
        help="choose settings from history alone and trade them forward blind")
    _strategy_arguments(w)
    w.add_argument("--over", action="append", required=True,
                   metavar="FIELD=V1,V2,V3",
                   help="the setting to choose between, e.g. "
                        "'stop_loss_pct=0.2,0.3,0.4'")
    w.add_argument("--folds", type=int, default=wf_mod.DEFAULT_FOLDS,
                   help="how many times to re-choose and trade forward")
    w.add_argument("--scheme", default="anchored",
                   choices=("anchored", "rolling"),
                   help="'anchored' chooses from all prior history; "
                        "'rolling' from a fixed-length window")
    w.add_argument("--metric", default="net_pnl",
                   choices=("net_pnl", "return_to_drawdown", "sharpe", "win_rate"),
                   help="what 'best' means when choosing a setting")
    w.set_defaults(func=cmd_walkforward)

    l = subs.add_parser("list", help="every saved run")
    l.add_argument("--json", action="store_true")
    l.add_argument("--all", action="store_true", help="include archived")
    l.set_defaults(func=cmd_list)

    sh = subs.add_parser("show", help="one run's report")
    sh.add_argument("id"); sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=cmd_show)

    c = subs.add_parser("correlate", help="daily-P&L correlation between runs")
    c.add_argument("--ids", default=None, help="comma separated, default all")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_correlate)

    pf = subs.add_parser(
        "portfolio", help="backtest saved strategies held together")
    pf.add_argument("--name", required=True)
    pf.add_argument("--hold", action="append", required=True,
                    metavar="ID:SIZE",
                    help="a saved strategy and its size, e.g. "
                         "'iron-condor-3-8:1'. Repeat, or comma-separate.")
    pf.add_argument("--start"); pf.add_argument("--end")
    pf.add_argument("--notes", default="")
    pf.add_argument("--dry-run", action="store_true")
    pf.set_defaults(func=cmd_portfolio)

    sv = subs.add_parser(
        "save", help="promote a run into the named strategy library")
    sv.add_argument("id", help="the run id to save, e.g. 004")
    sv.add_argument("--name", default=None, help="what to call the strategy")
    sv.add_argument("--lots", type=int, default=1,
                    help="intended size when this is run for real")
    sv.add_argument("--notes", default="")
    sv.set_defaults(func=cmd_save)

    sg = subs.add_parser("strategies", help="the saved strategy library")
    sg.add_argument("--json", action="store_true")
    sg.set_defaults(func=cmd_strategies)

    st = subs.add_parser("star", help="bookmark runs for later comparison")
    st.add_argument("ids", help="comma separated, e.g. '002,004,008'")
    st.add_argument("--clear", action="store_true", help="remove the bookmark")
    st.set_defaults(func=cmd_star)

    b = subs.add_parser("rebuild-index", help="regenerate index.json from disk")
    b.set_defaults(func=cmd_rebuild)

    v = subs.add_parser(
        "verify", help="re-run every saved run and check it still matches",
        description="The reproduce-to-the-rupee check. Run it before and "
                    "after any change to the engine, report, selectors or "
                    "costs; exits non-zero if anything drifted.")
    v.set_defaults(func=cmd_verify)

    rr = subs.add_parser(
        "rerender", help="rebuild saved reports in place, keeping their ids",
        description="Re-runs a saved run from its own spec and rewrites its "
                    "report, so a run recorded before the report grew a "
                    "section gains it. Checks the net P&L first and leaves a "
                    "drifted run untouched — re-baselining is a deliberate "
                    "decision, not a side effect of re-rendering.")
    rr.add_argument("--ids", default="",
                    help="comma separated; default is every run")
    rr.add_argument("--rebaseline", action="store_true",
                    help="write a run whose P&L has MOVED, recording the old "
                         "and new figures in its notes. Requires --reason. "
                         "Use only when the engine change was a fix and the "
                         "old number was wrong")
    rr.add_argument("--reason", default="",
                    help="why the number moved; stored in the run's notes")
    rr.set_defaults(func=cmd_rerender)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
