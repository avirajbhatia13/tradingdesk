"""Numbered, reproducible backtest runs on disk.

Every run gets an id — `001`, `002`, … — a slug from its name, and a directory
holding three files:

    db/backtests/001-short-straddle-0920/
        spec.json      exactly what was run, enough to reproduce it
        result.json    the full structured report, for the dashboard
        report.md      the human-readable version

Three files rather than one because they have three different readers. The
dashboard wants structured data. A person wants prose. And `spec.json` exists so
that "run 001 again with a wider stop" is a mechanical operation rather than an
attempt to remember what 001 was.

## Why numbering matters more than it looks

A strategy is not tested once. It is tested, tweaked, tested again — and the
tweaking is where self-deception lives. If you try forty variations and keep the
best, you have not found an edge, you have found the luckiest of forty draws.
The only defence is that every attempt is recorded, so the denominator is
visible. An id that increments whether the result was good or bad is what makes
that denominator honest: you cannot quietly not-count the failures.

Nothing here deletes. `archive()` marks a run superseded and leaves it in place.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from app.data.schema import LAKE_DIR

# Beside the lake and the database — data, not source, and never in git.
ROOT = LAKE_DIR.parent / "backtests"

INDEX = "index.json"
SPEC_FILE = "spec.json"
RESULT_FILE = "result.json"
REPORT_FILE = "report.md"


def _series_label(spec: Any) -> str | None:
    """What contract a stored run traded, for the index and the history page.

    A dated run is not a "WEEK" run, and the index is what the dashboard filters
    and groups on — labelling it by the rolling series it did not use would put
    it in the wrong bucket forever.
    """
    try:
        from app.backtest.engine import contract_label

        return contract_label(spec)
    except Exception:
        return getattr(spec, "expiry_flag", None)


def _encode(value: Any) -> Any:
    """JSON for the objects a spec actually contains."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "strategy")[:48]


def _runs() -> list[Path]:
    if not ROOT.exists():
        return []
    return sorted(p for p in ROOT.iterdir()
                  if p.is_dir() and re.match(r"^\d{3,}-", p.name))


def next_id() -> str:
    """The next free id, zero-padded.

    Derived from the directories present rather than from a counter file, so a
    run copied in from elsewhere — or a counter file lost — cannot cause an id
    to be reused and silently overwrite a result.
    """
    used = [int(p.name.split("-", 1)[0]) for p in _runs()]
    return f"{(max(used) + 1) if used else 1:03d}"


def save(name: str, spec: Any, report: dict[str, Any], markdown: str,
         underlying: str, start: date, end: date,
         notes: str = "") -> dict[str, Any]:
    """Write a run and return its index entry."""
    run_id = next_id()
    directory = ROOT / f"{run_id}-{slugify(name)}"
    directory.mkdir(parents=True, exist_ok=True)

    # `to_dict` rather than `asdict`: a spec holds selector objects, and
    # `asdict` flattens them to their fields and loses which rule they were —
    # `ByDelta(0.2)` and `ByPremium(0.2)` would land on disk identically. A
    # saved run that cannot be re-run is not a record.
    if hasattr(spec, "to_dict"):
        body = spec.to_dict()
    elif is_dataclass(spec):
        body = asdict(spec)
    else:
        body = spec
    spec_payload = {
        "name": name, "underlying": underlying,
        "start": start.isoformat(), "end": end.isoformat(),
        "spec": body,
        "notes": notes,
    }
    (directory / SPEC_FILE).write_text(
        json.dumps(spec_payload, indent=2, default=_encode))
    (directory / RESULT_FILE).write_text(
        json.dumps(report, indent=2, default=_encode))
    (directory / REPORT_FILE).write_text(markdown)

    entry = {
        "id": run_id,
        "name": name,
        "slug": directory.name,
        "kind": "run",
        "underlying": underlying,
        "series": report.get("series"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "created": datetime.now().isoformat(timespec="seconds"),
        "trades": (report.get("period") or {}).get("sessions"),
        "net_pnl": (report.get("headline") or {}).get("net_pnl"),
        "roi_on_peak_margin_pct": (report.get("headline") or {}).get("roi_on_peak_margin_pct"),
        "roi_on_capital_pct": (report.get("headline") or {}).get("roi_on_capital_pct"),
        "capital_floor": (report.get("headline") or {}).get("capital_floor"),
        "peak_margin": (report.get("margin") or {}).get("peak"),
        "max_drawdown": (report.get("risk") or {}).get("max_drawdown"),
        "win_rate_pct": (report.get("trade_stats") or {}).get("win_rate_pct"),
        "sharpe": (report.get("risk") or {}).get("sharpe"),
        "archived": False,
        "starred": False,
        "notes": notes,
    }
    index = load_index()
    index.append(entry)
    _write_index(index)
    return entry


def save_portfolio(name: str, allocations: list[dict[str, Any]],
                   result: dict[str, Any], markdown: str, underlying: str,
                   start: date, end: date, notes: str = "") -> dict[str, Any]:
    """Write a portfolio test, numbered from the same sequence as everything
    else — it is still an attempt, and attempts are what the numbering counts."""
    run_id = next_id()
    directory = ROOT / f"{run_id}-{slugify(name)}-portfolio"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SPEC_FILE).write_text(json.dumps({
        "name": name, "underlying": underlying, "kind": "portfolio",
        "start": start.isoformat(), "end": end.isoformat(),
        "allocations": allocations, "notes": notes,
    }, indent=2, default=_encode))
    (directory / RESULT_FILE).write_text(
        json.dumps(dict(result, kind="portfolio"), indent=2, default=_encode))
    (directory / REPORT_FILE).write_text(markdown)

    head = result.get("headline") or {}
    netting = result.get("netting") or {}
    entry = {
        "id": run_id, "name": name, "slug": directory.name, "kind": "portfolio",
        "underlying": underlying,
        "start": start.isoformat(), "end": end.isoformat(),
        "created": datetime.now().isoformat(timespec="seconds"),
        "members": len(allocations),
        "trades": (result.get("period") or {}).get("sessions"),
        "net_pnl": head.get("net_pnl"),
        "capital_floor": head.get("capital_floor"),
        "roi_on_capital_pct": head.get("roi_on_capital_pct"),
        "max_drawdown": head.get("max_drawdown"),
        "win_rate_pct": head.get("win_rate_pct"),
        "capital_saved_pct": netting.get("capital_saved_pct"),
        "archived": False, "starred": False, "notes": notes,
    }
    index = load_index()
    index.append(entry)
    _write_index(index)
    return entry


def save_walkforward(name: str, spec: Any, result: dict[str, Any],
                     markdown: str, underlying: str, start: date, end: date,
                     notes: str = "") -> dict[str, Any]:
    """Write a walk-forward validation, numbered from the same sequence."""
    run_id = next_id()
    directory = ROOT / f"{run_id}-{slugify(name)}-walkforward"
    directory.mkdir(parents=True, exist_ok=True)

    body = spec.to_dict() if hasattr(spec, "to_dict") else spec
    (directory / SPEC_FILE).write_text(json.dumps({
        "name": name, "underlying": underlying, "kind": "walkforward",
        "start": start.isoformat(), "end": end.isoformat(),
        "spec": body, "axes": result.get("axes"), "notes": notes,
    }, indent=2, default=_encode))
    (directory / RESULT_FILE).write_text(
        json.dumps(dict(result, kind="walkforward"), indent=2, default=_encode))
    (directory / REPORT_FILE).write_text(markdown)

    out = result.get("out_of_sample") or {}
    entry = {
        "id": run_id, "name": name, "slug": directory.name,
        "kind": "walkforward", "underlying": underlying,
        "series": _series_label(spec),
        "start": start.isoformat(), "end": end.isoformat(),
        "created": datetime.now().isoformat(timespec="seconds"),
        "trades": out.get("trades"),
        "net_pnl": out.get("net_pnl"),
        "max_drawdown": out.get("max_drawdown"),
        "win_rate_pct": out.get("win_rate"),
        "capital_floor": out.get("capital_floor"),
        "roi_on_capital_pct": out.get("roi_on_capital_pct"),
        "efficiency": result.get("efficiency"),
        "hindsight_net_pnl": (result.get("hindsight") or {}).get("net_pnl"),
        "folds": result.get("folds"),
        "archived": False, "starred": False, "notes": notes,
    }
    index = load_index()
    index.append(entry)
    _write_index(index)
    return entry


def save_sweep(name: str, spec: Any, grid: dict[str, Any], markdown: str,
               underlying: str, start: date, end: date,
               notes: str = "") -> dict[str, Any]:
    """Write a parameter sweep, numbered from the same sequence as runs.

    Same sequence deliberately. A sweep is dozens of backtests, and the reason
    ids exist at all is to keep the count of attempts visible — a sweep that
    numbered itself separately would let forty tries hide behind one id.
    """
    run_id = next_id()
    directory = ROOT / f"{run_id}-{slugify(name)}-sweep"
    directory.mkdir(parents=True, exist_ok=True)

    body = spec.to_dict() if hasattr(spec, "to_dict") else spec
    (directory / SPEC_FILE).write_text(json.dumps({
        "name": name, "underlying": underlying, "kind": "sweep",
        "start": start.isoformat(), "end": end.isoformat(),
        "spec": body, "axes": grid.get("axes"), "notes": notes,
    }, indent=2, default=_encode))
    (directory / RESULT_FILE).write_text(
        json.dumps(dict(grid, kind="sweep"), indent=2, default=_encode))
    (directory / REPORT_FILE).write_text(markdown)

    summary = grid.get("summary") or {}
    entry = {
        "id": run_id, "name": name, "slug": directory.name, "kind": "sweep",
        "underlying": underlying,
        "series": _series_label(spec),
        "start": start.isoformat(), "end": end.isoformat(),
        "created": datetime.now().isoformat(timespec="seconds"),
        "cells": summary.get("cells"),
        "profitable_share_pct": summary.get("profitable_share_pct"),
        "best_net_pnl": (summary.get("best") or {}).get("net_pnl"),
        "median_net_pnl": summary.get("median_net_pnl"),
        "axes": list((grid.get("axes") or {})),
        "archived": False, "starred": False, "notes": notes,
    }
    index = load_index()
    index.append(entry)
    _write_index(index)
    return entry


def load_index() -> list[dict[str, Any]]:
    path = ROOT / INDEX
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        # A corrupt index is recoverable — the run directories are the real
        # record — so rebuild rather than fail every caller.
        return rebuild_index()


def _write_index(index: list[dict[str, Any]]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / INDEX).write_text(json.dumps(index, indent=2, default=_encode))


def rebuild_index() -> list[dict[str, Any]]:
    """Reconstruct the index from the run directories, which are the truth.

    Stars are the exception: they live only in the index, so they are carried
    across rather than reset. Losing every bookmark to a routine rebuild would
    make the feature untrustworthy, which is worse than not having it.
    """
    keep: dict[str, bool] = {}
    path = ROOT / INDEX
    if path.exists():
        try:
            for entry in json.loads(path.read_text()):
                if entry.get("starred"):
                    keep[entry["id"]] = True
        except (ValueError, OSError):
            pass
    index = []
    for directory in _runs():
        try:
            spec = json.loads((directory / SPEC_FILE).read_text())
            report = json.loads((directory / RESULT_FILE).read_text())
        except (ValueError, OSError):
            continue
        if spec.get("kind") == "portfolio":
            head = report.get("headline") or {}
            index.append({
                "id": directory.name.split("-", 1)[0],
                "name": spec.get("name", directory.name),
                "slug": directory.name, "kind": "portfolio",
                "underlying": spec.get("underlying"),
                "start": spec.get("start"), "end": spec.get("end"),
                "created": datetime.fromtimestamp(
                    directory.stat().st_mtime).isoformat(timespec="seconds"),
                "members": len(spec.get("allocations") or []),
                "trades": (report.get("period") or {}).get("sessions"),
                "net_pnl": head.get("net_pnl"),
                "capital_floor": head.get("capital_floor"),
                "roi_on_capital_pct": head.get("roi_on_capital_pct"),
                "max_drawdown": head.get("max_drawdown"),
                "win_rate_pct": head.get("win_rate_pct"),
                "capital_saved_pct": (report.get("netting") or {}).get("capital_saved_pct"),
                "archived": False,
                "starred": keep.get(directory.name.split("-", 1)[0], False),
                "notes": spec.get("notes", ""),
            })
            continue
        if spec.get("kind") == "walkforward":
            out = report.get("out_of_sample") or {}
            index.append({
                "id": directory.name.split("-", 1)[0],
                "name": spec.get("name", directory.name),
                "slug": directory.name, "kind": "walkforward",
                "underlying": spec.get("underlying"),
                "start": spec.get("start"), "end": spec.get("end"),
                "created": datetime.fromtimestamp(
                    directory.stat().st_mtime).isoformat(timespec="seconds"),
                "trades": out.get("trades"), "net_pnl": out.get("net_pnl"),
                "max_drawdown": out.get("max_drawdown"),
                "win_rate_pct": out.get("win_rate"),
                "capital_floor": out.get("capital_floor"),
                "roi_on_capital_pct": out.get("roi_on_capital_pct"),
                "efficiency": report.get("efficiency"),
                "hindsight_net_pnl": (report.get("hindsight") or {}).get("net_pnl"),
                "folds": report.get("folds"),
                "archived": False,
                "starred": keep.get(directory.name.split("-", 1)[0], False),
                "notes": spec.get("notes", ""),
            })
            continue
        if spec.get("kind") == "sweep" or report.get("kind") == "sweep":
            summary = report.get("summary") or {}
            index.append({
                "id": directory.name.split("-", 1)[0],
                "name": spec.get("name", directory.name),
                "slug": directory.name, "kind": "sweep",
                "underlying": spec.get("underlying"),
                "start": spec.get("start"), "end": spec.get("end"),
                "created": datetime.fromtimestamp(
                    directory.stat().st_mtime).isoformat(timespec="seconds"),
                "cells": summary.get("cells"),
                "profitable_share_pct": summary.get("profitable_share_pct"),
                "best_net_pnl": (summary.get("best") or {}).get("net_pnl"),
                "median_net_pnl": summary.get("median_net_pnl"),
                "axes": list((report.get("axes") or {})),
                "archived": False,
                "starred": keep.get(directory.name.split("-", 1)[0], False),
                "notes": spec.get("notes", ""),
            })
            continue
        index.append({
            "id": directory.name.split("-", 1)[0],
            "name": spec.get("name", directory.name),
            "slug": directory.name,
            "underlying": spec.get("underlying"),
            "series": report.get("series"),
            "start": spec.get("start"), "end": spec.get("end"),
            "created": datetime.fromtimestamp(
                directory.stat().st_mtime).isoformat(timespec="seconds"),
            "trades": (report.get("period") or {}).get("sessions"),
            "net_pnl": (report.get("headline") or {}).get("net_pnl"),
            "roi_on_peak_margin_pct": (report.get("headline") or {}).get("roi_on_peak_margin_pct"),
            "roi_on_capital_pct": (report.get("headline") or {}).get("roi_on_capital_pct"),
            "capital_floor": (report.get("headline") or {}).get("capital_floor"),
            "peak_margin": (report.get("margin") or {}).get("peak"),
            "max_drawdown": (report.get("risk") or {}).get("max_drawdown"),
            "win_rate_pct": (report.get("trade_stats") or {}).get("win_rate_pct"),
            "sharpe": (report.get("risk") or {}).get("sharpe"),
            "archived": False,
            "starred": keep.get(directory.name.split("-", 1)[0], False),
            "notes": spec.get("notes", ""),
        })
    _write_index(index)
    return index


def find(run_id: str) -> Path | None:
    """Locate a run by id or by full slug."""
    run_id = str(run_id).strip()
    for directory in _runs():
        if directory.name == run_id or directory.name.split("-", 1)[0] == run_id.zfill(3):
            return directory
    return None


def load(run_id: str) -> dict[str, Any] | None:
    directory = find(run_id)
    if not directory:
        return None
    out: dict[str, Any] = {"slug": directory.name,
                           "id": directory.name.split("-", 1)[0]}
    for key, filename in (("spec", SPEC_FILE), ("report", RESULT_FILE)):
        path = directory / filename
        if path.exists():
            out[key] = json.loads(path.read_text())
    markdown = directory / REPORT_FILE
    if markdown.exists():
        out["markdown"] = markdown.read_text()
    return out


def rewrite(run_id: str, report: dict[str, Any], markdown: str) -> bool:
    """Replace a saved run's report in place, keeping its id and its spec.

    For re-rendering an existing run after the *report* gained a section — not
    for re-baselining one after the engine changed what it computes. The caller
    is expected to have checked that the net P&L still matches; this deliberately
    does not, because a function that silently overwrites the record when the
    numbers moved is the one thing `verify` exists to prevent.
    """
    directory = find(run_id)
    if not directory:
        return False
    (directory / RESULT_FILE).write_text(
        json.dumps(report, indent=2, default=_encode))
    (directory / REPORT_FILE).write_text(markdown)
    return True


def rewrite_spec(run_id: str, spec_payload: dict[str, Any]) -> bool:
    """Replace a saved run's `spec.json`, for recording a re-baseline note.

    Separate from `rewrite` because it is the *record about* the run that is
    changing, not the run. The definition itself must never be edited here — a
    changed definition is a different strategy, and editing one in place would
    detach it from the runs that tested it.
    """
    directory = find(run_id)
    if not directory:
        return False
    (directory / SPEC_FILE).write_text(
        json.dumps(spec_payload, indent=2, default=_encode))
    return True


def archive(run_id: str, archived: bool = True) -> bool:
    """Mark a run superseded without deleting it. The record of what was tried
    is the whole point, so nothing here removes evidence."""
    index = load_index()
    hit = False
    for entry in index:
        if entry["id"] == str(run_id).zfill(3):
            entry["archived"] = archived
            hit = True
    if hit:
        _write_index(index)
    return hit


def star(run_id: str, starred: bool = True) -> bool:
    """Bookmark a run.

    Kept in the index rather than in the run directory because it is a property
    of how *you* are using the record, not of what was run — nothing about the
    backtest changes when you star it, and `rebuild-index` has to be able to
    reconstruct the directories without inventing opinions about them.
    """
    index = load_index()
    hit = False
    for entry in index:
        if entry["id"] == str(run_id).zfill(3):
            entry["starred"] = starred
            hit = True
    if hit:
        _write_index(index)
    return hit


def _curve_of(run: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """The equity curve, wherever this kind of run keeps it.

    A walk-forward's curve is its out-of-sample record, which is the *more*
    interesting thing to correlate against a plain backtest — so it is included
    rather than skipped for having a different shape on disk.
    """
    report = (run or {}).get("report") or {}
    return (report.get("curves") or {}).get("equity") or report.get("curve")


def equity_curves(ids: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Daily equity per run, for the correlation matrix.

    `ids` exists because correlating everything stops being useful the moment
    there are more than a handful of runs — the question is almost always about
    a shortlist.
    """
    wanted = {str(i).strip().zfill(3) for i in ids} if ids else None
    out = {}
    for entry in load_index():
        if wanted is not None and entry["id"] not in wanted:
            continue
        curve = _curve_of(load(entry["id"]))
        if curve:
            out[f"{entry['id']} {entry['name']}"] = curve
    return out
