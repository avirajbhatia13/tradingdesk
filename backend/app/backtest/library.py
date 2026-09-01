"""Named strategies, as distinct from the attempts that tested them.

A **run** is one backtest: this definition, over these dates, with this result.
Runs are numbered and never deleted, because the count of attempts is what keeps
the research honest.

A **strategy** is the definition itself — the thing you would actually deploy.
It has a name rather than a number, it outlives any particular date range, and
it is the object the paper runner and eventually the live runner will read. This
is the one architectural rule of the project made concrete: *a strategy is
defined once and executed by three runtimes.* The moment a strategy lives only
inside a backtest, the backtest and the live bot are two definitions that will
drift.

## Evidence is derived, never claimed

A strategy record does not store "this is validated". It stores nothing about
quality at all. Instead every saved run is matched to it by **fingerprint**, and
what the strategy can claim is computed from whatever runs exist:

    backtested   there is at least one full run
    swept        the settings around it were tested
    validated    a walk-forward exists, and its efficiency is reported

That way a strategy cannot be marked ready by an act of optimism, and the
readiness of everything in the library updates for free as more runs land.

## Why the fingerprint ignores stops

The fingerprint covers what makes the strategy *that strategy* — its legs, the
underlying, the series, the clock, the days it trades. It deliberately excludes
stop, target, trail and the rest, because those are the parameters a sweep
varies: run 004 at a 25% stop, sweep 008 across six stops, and walk-forward 010
choosing between them are three pieces of evidence about **one** strategy, and
a fingerprint that included the stop would file them as three strangers.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.backtest import registry
from app.data.schema import LAKE_DIR

ROOT = LAKE_DIR.parent / "strategies"

# Set by you, and only by you. Readiness is derived; this is intent.
STATUSES = ("draft", "paper", "live", "retired")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "strategy")[:60]


def fingerprint(spec: dict[str, Any], underlying: str) -> str:
    """A stable identity for "the same strategy, differently tuned".

    Built from a canonical string rather than from the dict directly, so a key
    added to `spec.json` later cannot silently re-file every stored strategy.
    """
    legs = [
        f"{leg.get('side')}|{leg.get('opt_type')}|{leg.get('lots', 1)}|"
        f"{leg.get('restrike', False)}|"
        f"{json.dumps(leg.get('select') or {}, sort_keys=True)}"
        for leg in spec.get("legs", [])
    ]
    weekdays = spec.get("weekdays")
    # The expiry axis is part of the identity, not part of the tuning. A front
    # weekly straddle and a next-weekly straddle are two strategies, and
    # filing one as evidence about the other would let a walk-forward on one
    # certify the other.
    #
    # Appended rather than folded into the existing series slot so that every
    # strategy already on disk keeps its fingerprint: a rolling run contributes
    # the empty string here and hashes exactly as it did before.
    expiry_axis = ""
    if spec.get("expiry_index") is not None:
        expiry_axis = (f"e{spec['expiry_index']}"
                       f"/{spec.get('expiry_kind') or 'any'}"
                       f"/{spec.get('min_dte')}-{spec.get('max_dte')}")
    parts = [
        (underlying or "").upper(),
        str(spec.get("expiry_flag", "WEEK")).upper() + expiry_axis,
        str(spec.get("entry_time", "")),
        str(spec.get("exit_time", "")),
        ",".join(str(d) for d in sorted(weekdays)) if weekdays else "all",
        str(spec.get("lot_size", "")),
        ";".join(legs),
    ]
    return hashlib.sha1("::".join(parts).encode()).hexdigest()[:12]


def _path(strategy_id: str) -> Path:
    return ROOT / f"{strategy_id}.json"


def _all_files() -> list[Path]:
    if not ROOT.exists():
        return []
    return sorted(p for p in ROOT.glob("*.json"))


def save(name: str, spec: dict[str, Any], underlying: str,
         notes: str = "", lots: int = 1, status: str = "draft",
         strategy_id: str | None = None) -> dict[str, Any]:
    """Create or overwrite a named strategy.

    `spec` is a `StrategySpec.to_dict()` — the same shape a run stores — so a
    strategy can be handed straight back to the engine, and later to the paper
    and live runners, without translation.
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    strategy_id = strategy_id or slugify(name)
    ROOT.mkdir(parents=True, exist_ok=True)
    existing = load(strategy_id) or {}
    record = {
        "id": strategy_id,
        "name": name,
        "underlying": underlying.upper(),
        "spec": spec,
        "fingerprint": fingerprint(spec, underlying),
        "lots": int(lots),
        "status": status,
        "notes": notes,
        "created": existing.get("created")
                   or datetime.now().isoformat(timespec="seconds"),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    _path(strategy_id).write_text(json.dumps(record, indent=2))
    return record


def from_run(run_id: str, name: str | None = None, notes: str = "",
             lots: int = 1) -> dict[str, Any]:
    """Promote a saved backtest into the strategy library.

    The normal path: you run something, it looks worth keeping, and the
    definition graduates from "attempt number 4" to a named thing you can
    deploy. The run stays exactly where it is — this copies the definition, it
    does not move it.
    """
    run = registry.load(run_id)
    if not run or "spec" not in run:
        raise ValueError(f"no saved run {run_id!r}")
    payload = run["spec"]
    if payload.get("kind") in ("sweep", "walkforward"):
        raise ValueError(
            f"run {run_id} is a {payload['kind']}, which varies a setting "
            f"rather than fixing one. Save the plain run it was based on.")
    return save(
        name=name or payload.get("name") or f"strategy from {run_id}",
        spec=payload["spec"],
        underlying=payload.get("underlying", "NIFTY"),
        notes=notes or payload.get("notes", ""),
        lots=lots,
    )


def load(strategy_id: str) -> dict[str, Any] | None:
    path = _path(strategy_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return None


def update(strategy_id: str, **fields: Any) -> dict[str, Any] | None:
    """Change status, notes or intended size. The definition is not editable
    here on purpose — a changed definition is a different strategy, and editing
    one in place would silently invalidate the evidence attached to it."""
    record = load(strategy_id)
    if record is None:
        return None
    allowed = {"name", "status", "notes", "lots"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(
            f"cannot change {', '.join(sorted(unknown))} on a saved strategy. "
            f"A different definition is a different strategy — save it as one, "
            f"so the runs that tested the old one stay attached to the old one.")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    record.update({k: v for k, v in fields.items()})
    record["lots"] = int(record.get("lots", 1))
    record["updated"] = datetime.now().isoformat(timespec="seconds")
    _path(strategy_id).write_text(json.dumps(record, indent=2))
    return record


def delete(strategy_id: str) -> bool:
    """Remove a strategy from the library.

    Unlike a run this really does delete, because a strategy is a working
    definition rather than a record of an attempt — and the runs that tested
    it, which are the actual evidence, are untouched and still numbered.
    """
    path = _path(strategy_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def evidence(record: dict[str, Any],
             index: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Which saved runs tested this strategy, and what they concluded.

    Derived rather than stored, so it cannot go stale and cannot be asserted.
    Runs are matched on the structural fingerprint, which is why a sweep over
    six stop levels counts as evidence about the same strategy as the single
    run at one of them.
    """
    index = registry.load_index() if index is None else index
    want = record.get("fingerprint")
    matched = {"run": [], "sweep": [], "walkforward": []}

    for entry in index:
        run = registry.load(entry["id"])
        payload = (run or {}).get("spec") or {}
        spec = payload.get("spec")
        if not spec:
            continue
        if fingerprint(spec, payload.get("underlying", "")) != want:
            continue
        kind = entry.get("kind") or payload.get("kind") or "run"
        matched.setdefault(kind, []).append(entry)

    runs = matched["run"]
    sweeps = matched["sweep"]
    walks = matched["walkforward"]
    best = max(runs, key=lambda e: e.get("net_pnl") or 0) if runs else None
    walk = max(walks, key=lambda e: e.get("efficiency") or -99) if walks else None

    checks = [
        {"key": "backtested", "done": bool(runs),
         "label": "Backtested",
         "detail": (f"run {best['id']} · {best.get('net_pnl'):,.0f} on "
                    f"{best.get('capital_floor') or 0:,.0f}" if best
                    else "no full backtest yet")},
        {"key": "swept", "done": bool(sweeps),
         "label": "Settings swept",
         "detail": (f"run {sweeps[-1]['id']} · "
                    f"{sweeps[-1].get('profitable_share_pct')}% of the grid worked"
                    if sweeps else "the neighbouring settings are untested")},
        {"key": "validated", "done": bool(walk),
         "label": "Tested without hindsight",
         "detail": (f"run {walk['id']} · {(walk.get('efficiency') or 0) * 100:.0f}% "
                    f"of the edge survived" if walk
                    else "never walked forward — the numbers above still "
                         "include hindsight")},
    ]
    # The one that decides whether this is worth risking money on. A
    # walk-forward that ran and failed is *worse* evidence than none, so it is
    # called out rather than counted as a tick.
    efficiency = walk.get("efficiency") if walk else None
    if efficiency is not None and efficiency < 0.5:
        checks[2]["done"] = False
        checks[2]["warn"] = True
        checks[2]["detail"] = (
            f"run {walk['id']} · only {efficiency * 100:.0f}% of the edge "
            f"survived — the tuning is mostly fitting noise")

    return {
        "runs": [e["id"] for e in runs],
        "sweeps": [e["id"] for e in sweeps],
        "walkforwards": [e["id"] for e in walks],
        "best_run": best,
        "walkforward": walk,
        "efficiency": efficiency,
        "checks": checks,
        "ready": all(c["done"] for c in checks),
    }


def list_all(with_evidence: bool = True) -> list[dict[str, Any]]:
    """Every saved strategy, newest first, with its evidence attached."""
    index = registry.load_index() if with_evidence else []
    out = []
    for path in _all_files():
        try:
            record = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if with_evidence:
            record["evidence"] = evidence(record, index)
        out.append(record)
    out.sort(key=lambda r: r.get("updated") or "", reverse=True)
    return out
