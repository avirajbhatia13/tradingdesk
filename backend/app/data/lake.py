"""Reading and writing the Parquet lake.

DuckDB queries the Parquet files in place — there is no import step and no
server. `connect()` hands back a connection with `option_bars` and `spot_bars`
registered as views over the file layout, so callers write ordinary SQL and
never think about paths.

Two properties this module is responsible for, both of which matter more than
they look:

**Idempotent ingest.** Backfilling five years takes thousands of requests and
will be interrupted — by a rate limit, a dropped connection, or you closing the
laptop. Re-running it must not double-count. Writes therefore replace a whole
(instrument, month, source) partition file rather than appending to it, so a
re-fetched month overwrites cleanly and a half-written file is never mixed with
a good one.

**Live and vendor data stay separable.** Every row carries `source`. Our own
recording and a vendor's history disagree in small ways — different tick
sampling, different rounding — and a backtest that silently splices them is
lying about its own fill assumptions. They are written to separate files so
either can be dropped or preferred without rebuilding the lake.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from app.data import schema as sch

# zstd at level 3 measured 23 bytes/row against snappy's 31 with no meaningful
# write-time cost, and DuckDB decompresses it at scan speed.
COMPRESSION = "zstd"
COMPRESSION_LEVEL = 3
ROW_GROUP_SIZE = 128_000


# A full-lake scan takes ~1.3 s, so the original budget of 3 tries at 0.25 s
# backoff — 1.5 s of waiting — could not outlast a single burst of partition
# writes, and `--resume` and the dashboard's status panel both failed during an
# active backfill. Widened to ~3.75 s of waiting, which costs nothing when
# nothing is writing and is far better than a 500 on a metadata query.
#
# Attempting to reproduce it on demand failed: 24 consecutive full-lake scans,
# single- and multi-threaded, all clean while a backfill ran. So this is a
# wider budget for a transient fault, not a diagnosis of it. If it recurs at
# this budget, the next step is a lock shared between writer and readers —
# with the caveat below about stale locks.
TORN_READ_RETRIES = 5
TORN_READ_BACKOFF = 0.25


def _files(table: str) -> str:
    return str(sch.table_dir(table) / "**" / "*.parquet")


def read(fn, *args, **kwargs):
    """Run a lake read, retrying a torn Parquet read.

    Writes replace a partition with `os.replace`, which is atomic — a reader
    holding the old file keeps reading a complete file. But DuckDB parallelises
    a scan by reopening files *by path*, so a worker can pick up the new inode
    using offsets taken from the old footer and fail with a Thrift error
    ("Invalid data"). Nothing on disk is damaged; the same query run a moment
    later succeeds.

    It surfaces two ways, both seen during an active backfill: a Thrift error
    when the footer is unreadable, and `don't know what type:` — with the type
    name *empty* — when a column's type comes back garbled. The second is the
    reason this matches on signatures rather than on an exception class; DuckDB
    raises the same `duckdb.Error` for real schema problems.

    Neither reproduced in fifty-odd retries afterwards, which is the shape of a
    race rather than a defect: rare enough to be invisible in testing, frequent
    enough to break the dashboard eventually, since the recorder replaces a
    partition every minute of every session.

    A lock shared between writer and readers would remove the race outright
    rather than paper over it, and is the right answer if this ever recurs
    often enough to notice. It is not the answer yet: a stale lock left by a
    killed backfill would take the whole dashboard down, which is a worse
    failure than a retried read.

    Only retries these signatures. A genuine schema or SQL error must fail
    immediately rather than being retried and hidden.
    """
    last: Exception | None = None
    for attempt in range(TORN_READ_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:              # noqa: BLE001 - re-raised below
            if not _is_torn_read(exc):
                raise
            last = exc
            time.sleep(TORN_READ_BACKOFF * (attempt + 1))
    raise last                                 # type: ignore[misc]


def _is_torn_read(exc: Exception) -> bool:
    text = str(exc)
    if any(sign in text for sign in
           ("Invalid data", "TProtocolException", "Thrift",
            "Unexpected end of file", "No magic bytes")):
        return True
    # "don't know what type:" with nothing after the colon. A real unsupported
    # type names itself, so the empty tail is what distinguishes a garbled read
    # from a genuine conversion failure.
    marker = "don't know what type:"
    return marker in text and not text.split(marker, 1)[1].strip()


def connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """A DuckDB connection with the lake mounted as views.

    Views rather than tables: the Parquet files are the database, so a view
    means a new partition written by the recorder is visible to the next query
    without any reload step.
    """
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='Asia/Kolkata'")
    for table in (sch.OPTION_BARS, sch.SPOT_BARS, sch.CONTRACTS):
        pattern = _files(table)
        if any(sch.table_dir(table).rglob("*.parquet")):
            con.execute(
                f"CREATE VIEW {table} AS "
                f"SELECT * FROM read_parquet('{pattern}', hive_partitioning=true, "
                f"union_by_name=true)"
            )
        else:
            # An empty lake still has to answer queries, or every caller needs
            # a "have you backfilled yet" branch.
            fields = ", ".join(
                f"CAST(NULL AS {_duck_type(f.type)}) AS {f.name}"
                for f in sch.SCHEMAS[table]
            )
            con.execute(f"CREATE VIEW {table} AS SELECT {fields} WHERE false")
    return con


def _duck_type(arrow_type: pa.DataType) -> str:
    if pa.types.is_timestamp(arrow_type):
        return "TIMESTAMP"
    if pa.types.is_date(arrow_type):
        return "DATE"
    if pa.types.is_string(arrow_type):
        return "VARCHAR"
    if pa.types.is_float32(arrow_type):
        return "FLOAT"
    if pa.types.is_int64(arrow_type):
        return "BIGINT"
    if pa.types.is_int8(arrow_type):
        return "TINYINT"
    if pa.types.is_int16(arrow_type):
        return "SMALLINT"
    if pa.types.is_int32(arrow_type):
        return "INTEGER"
    if pa.types.is_boolean(arrow_type):
        return "BOOLEAN"
    return "VARCHAR"


def _atomic_write(table: pa.Table, path: Path) -> None:
    """Write via a temp file and rename, so a crash cannot leave a torn file.

    A half-written Parquet in the lake is worse than a missing one: DuckDB
    fails the whole scan on it, so one interrupted backfill would take every
    query down until someone found the file by hand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        pq.write_table(
            table, tmp, compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL,
            row_group_size=ROW_GROUP_SIZE, use_dictionary=True,
        )
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def write_bars(table_name: str, key: str, rows: list[dict[str, Any]],
               source: str, merge: bool = True, part: str | None = None) -> int:
    """Write bars for one instrument, splitting them into month partitions.

    With `merge`, existing rows in the same partition and source are kept and
    de-duplicated against the new ones — which is what makes re-running a
    backfill safe, and what lets the live recorder flush every minute without
    rewriting the month from scratch each time.

    `part` names a **side file** in the same partition, `<source>-<part>.parquet`,
    and skips the merge. Reads glob `**/*.parquet` and union, so a side file is
    visible immediately; `compact` folds them back into the source's own file.
    This exists because merging is a read-modify-write of the whole month, which
    turns a bulk ingest into a quadratic job — see `upstox_backfill.FLUSH_ROWS`.
    The rows still carry `source`, so nothing downstream can tell the difference.
    """
    if not rows:
        return 0

    arrow_schema = sch.SCHEMAS[table_name]
    for row in rows:
        row.setdefault("source", source)

    by_month: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        stamp = row["ts"]
        month = f"{stamp.year:04d}-{stamp.month:02d}"
        by_month.setdefault(month, []).append(row)

    written = 0
    for month, batch in by_month.items():
        day = date(int(month[:4]), int(month[5:7]), 1)
        name = f"{source}-{part}.parquet" if part else f"{source}.parquet"
        path = sch.partition_dir(table_name, key, day) / name

        table = _rows_to_table(batch, arrow_schema)
        if part:
            merge = False
        if merge and path.exists():
            # Existing files are conformed to the declared schema before
            # merging. A round trip through DuckDB widens timestamps and floats,
            # so without this the second write of a month fails on a schema
            # mismatch against the first — and a file written before a column
            # was added has to gain it as nulls rather than blow up the merge.
            existing = _conform(pq.read_table(path), arrow_schema)
            table = pa.concat_tables([existing, table])
        table = _dedupe(table, table_name, arrow_schema)
        _atomic_write(table, path)
        written += len(batch)
    return written


def _conform(table: pa.Table, arrow_schema: pa.Schema) -> pa.Table:
    """Bring a table read off disk up to the declared schema.

    Columns added to the schema after a file was written come back as nulls
    rather than raising, which is what makes a schema change survivable without
    rewriting the whole lake in one shot. Columns no longer in the schema are
    dropped. The DuckDB views already read with `union_by_name`, so this is the
    matching behaviour on the write path.
    """
    columns = []
    for field in arrow_schema:
        if field.name in table.column_names:
            column = table.column(field.name)
            # Cast rather than trust. A column whose declared type has since
            # widened — `moneyness` went int8 to int16 when the chain outgrew
            # it — is still int8 in every file written before the change, and
            # the whole promise of this function is that what it returns
            # matches the declared schema.
            if column.type != field.type:
                column = column.cast(field.type)
            columns.append(column)
        else:
            columns.append(pa.nulls(table.num_rows, field.type))
    return pa.table(columns, schema=arrow_schema)


def _rows_to_table(rows: list[dict[str, Any]], arrow_schema: pa.Schema) -> pa.Table:
    columns = {
        field.name: pa.array([row.get(field.name) for row in rows], field.type)
        for field in arrow_schema
    }
    return pa.table(columns, schema=arrow_schema)


def _dedupe(table: pa.Table, table_name: str,
            arrow_schema: pa.Schema) -> pa.Table:
    """Last write wins per bar, then sort by time.

    Time-sorted is the right physical order here: every backtest walks forward
    through the session, and Parquet row-group statistics then let DuckDB skip
    straight to a date range instead of scanning the month.

    The result is cast back to the declared schema because DuckDB widens types
    on the way through (timestamp('s') comes back as microseconds, float32 as
    double), and a lake whose files disagree about their own types fails on
    the next union read.
    """
    # `series` is part of the key because Dhan's rolling endpoint leaves
    # `expiry` null on every row: without it the weekly and monthly bar for the
    # same minute and strike are the same key, and one silently replaces the
    # other. Live-recorded rows carry a real `expiry` and a null `series`, so
    # the pair identifies a contract for both sources.
    if table_name == sch.OPTION_BARS:
        keys = ["ts", "expiry", "series", "strike", "opt_type"]
    elif table_name == sch.CONTRACTS:
        # A contract is identified by what it is, not by when it was listed.
        keys = ["underlying", "expiry", "strike", "opt_type"]
    else:
        keys = ["ts", "symbol"]

    # Duplicate bars share a timestamp, so ordering the window by `ts` leaves
    # the winner to chance — and in practice picks the OLD row, quietly
    # discarding a corrected re-fetch. An explicit arrival sequence makes
    # "last write wins" actually true, since callers concatenate existing
    # rows before new ones.
    table = table.append_column(
        "_seq", pa.array(range(table.num_rows), pa.int64()))

    con = duckdb.connect(":memory:")
    con.register("incoming", table)
    key_list = ", ".join(keys)
    deduped = con.execute(
        f"SELECT * EXCLUDE (rn, _seq) FROM ("
        f"  SELECT *, row_number() OVER (PARTITION BY {key_list} "
        f"                               ORDER BY _seq DESC) AS rn"
        f"  FROM incoming"
        f") WHERE rn = 1 ORDER BY {key_list}"
    ).to_arrow_table()
    return deduped.select(arrow_schema.names).cast(arrow_schema)


def write_contracts(underlying: str, rows: list[dict[str, Any]],
                    source: str = "upstox") -> int:
    """Upsert contract listings for one underlying.

    Unpartitioned and merge-always, unlike the bar tables: the whole listing is
    a few tens of thousands of rows, so rewriting it costs milliseconds and the
    side-file machinery that makes the bulk bar ingest tractable would be pure
    overhead here.

    Last write wins per contract, which makes re-running a backfill free and
    lets a later run correct a field the vendor has since changed.
    """
    if not rows:
        return 0
    arrow_schema = sch.SCHEMAS[sch.CONTRACTS]
    for row in rows:
        row.setdefault("source", source)
        row.setdefault("underlying", underlying)

    path = sch.partition_dir(sch.CONTRACTS, underlying, date.today()) / f"{source}.parquet"
    table = _rows_to_table(rows, arrow_schema)
    if path.exists():
        table = pa.concat_tables(
            [_conform(pq.read_table(path), arrow_schema), table])
    table = _dedupe(table, sch.CONTRACTS, arrow_schema)
    _atomic_write(table, path)
    return len(rows)


def contracts(underlying: str, source: str = "upstox") -> list[dict[str, Any]]:
    """Every contract listing held for one underlying."""
    def once() -> list[dict[str, Any]]:
        con = connect()
        try:
            result = con.execute(
                "SELECT * FROM contracts WHERE underlying = ? AND source = ?",
                [underlying, source])
            names = [d[0] for d in result.description]
            return [dict(zip(names, row)) for row in result.fetchall()]
        finally:
            con.close()
    return read(once)


def tag_series(value: str = "WEEK", source: str = "dhan") -> dict[str, int]:
    """Label vendor rows written before the `series` column existed.

    Idempotent, and deliberately narrow: it only fills rows whose `series` is
    null *and* whose `source` matches, so re-running it is free and our own
    live recording — which identifies its contracts by a real `expiry` date and
    should keep a null series — is never touched.

    This is safe to run exactly because the journal shows the whole existing
    lake came from one series. It is not a general repair: if both WEEK and
    MONTH rows had already been written untagged they would have collided on
    the dedupe key and one of them would be gone, and no migration can invent
    it back. That is why the ingest fix and this migration ship together.
    """
    arrow_schema = sch.SCHEMAS[sch.OPTION_BARS]
    files, rows = 0, 0
    for path in sch.table_dir(sch.OPTION_BARS).rglob("*.parquet"):
        table = _conform(pq.read_table(path), arrow_schema)
        series = table.column("series").to_pylist()
        sources = table.column("source").to_pylist()
        patched = [
            value if (existing is None and origin == source) else existing
            for existing, origin in zip(series, sources)
        ]
        if patched == series:
            continue
        index = arrow_schema.get_field_index("series")
        table = table.set_column(
            index, arrow_schema.field(index), pa.array(patched, pa.string()))
        _atomic_write(table, path)
        files += 1
        rows += sum(1 for a, b in zip(patched, series) if a != b)
    return {"files": files, "rows": rows}


def repair_spot(underlying: str, source: str,
                spot_at: dict[Any, float],
                remoneyness: Any = None) -> dict[str, int]:
    """Restamp a source's `spot` from a per-minute series, and re-derive
    moneyness from it.

    Written for a real defect: the Upstox ingest stamped every bar in a session
    with the session's *average* spot. Nothing errored — moneyness simply
    stopped moving intraday, and every rule that resolves against spot
    (`ByPctOfSpot`, `ByStrikeOffset`, delta, the day-move entry filter) was
    quietly answering against a price the market never traded at.

    Repairing rather than re-fetching, because the spot series is already in
    the lake: 45M rows cost nothing here and would cost days against a vendor
    rate limit. Idempotent — a row already carrying its minute's spot is
    rewritten to the same value.

    `remoneyness(strike, spot, opt_type) -> int | None` re-derives the label;
    passing None leaves moneyness alone, which is only right if spot was
    already correct.
    """
    arrow_schema = sch.SCHEMAS[sch.OPTION_BARS]
    column = sch.partition_key_column(sch.OPTION_BARS)
    files = changed = 0

    for path in sorted(sch.table_dir(sch.OPTION_BARS)
                       .glob(f"{column}={underlying}/*/*.parquet")):
        table = _conform(pq.read_table(path), arrow_schema)
        sources = table.column("source").to_pylist()
        if source not in set(sources):
            continue

        stamps = table.column("ts").to_pylist()
        spots = table.column("spot").to_pylist()
        strikes = table.column("strike").to_pylist()
        types = table.column("opt_type").to_pylist()
        moneyness = table.column("moneyness").to_pylist()

        new_spots, new_money, touched = [], [], 0
        for i, origin in enumerate(sources):
            spot, level = spots[i], moneyness[i]
            if origin == source:
                found = spot_at.get(stamps[i])
                if found is None and stamps[i] is not None:
                    found = spot_at.get(stamps[i].date())
                if found is not None:
                    if spot is None or abs(found - spot) > 1e-9:
                        touched += 1
                    spot = found
                    if remoneyness is not None:
                        level = remoneyness(strikes[i], spot, types[i])
            new_spots.append(spot)
            new_money.append(level)

        if not touched:
            continue
        for name, values, kind in (("spot", new_spots, None),
                                   ("moneyness", new_money, None)):
            index = arrow_schema.get_field_index(name)
            field = arrow_schema.field(index)
            table = table.set_column(
                index, field, pa.array(values, field.type))
        _atomic_write(table, path)
        files += 1
        changed += touched
    return {"files": files, "rows": changed}


def derive_moneyness(underlying: str, source: str,
                     step: float | None = None) -> dict[str, int]:
    """Fill `moneyness` on rows that have a strike and a spot but no label.

    Written for our own recorder. It writes a real `expiry` and a real `spot` on
    every bar but never derived the moneyness, so 750,872 NIFTY bars — every
    session the dashboard has been left running since 17 Aug 2026 — sat in the
    lake unreachable: no backtest rule can name a strike without it, so
    `SELL CE 0` resolves to nothing at all.

    Nothing is fetched. Strike and spot are already on every row and moneyness
    is arithmetic over them, so this is the cheap half of `repair_spot` — which
    looks like the right tool and is not, because it only rewrites moneyness
    where it also rewrites *spot*, and the recorder's spot was correct from the
    start.

    The step is inferred once, from every strike this source holds for the
    underlying, rather than per partition. A month's file is a handful of
    contracts and the gaps between their strikes are whatever the money did that
    month — inferring from that sample gives a plausible wrong number and every
    row in the partition comes out wrong together. See moneyness.annotate.

    Idempotent: a row that already carries a moneyness is left alone, so this is
    safe to re-run after every session.
    """
    from app.data import moneyness as mny

    arrow_schema = sch.SCHEMAS[sch.OPTION_BARS]
    column = sch.partition_key_column(sch.OPTION_BARS)

    if step is None or step <= 0:
        rows = query(
            "SELECT DISTINCT strike FROM option_bars "
            "WHERE underlying = ? AND source = ? AND strike IS NOT NULL",
            [underlying, source])
        step = mny.strike_step((r[0] for r in rows), underlying)

    files = filled = 0
    for path in sorted(sch.table_dir(sch.OPTION_BARS)
                       .glob(f"{column}={underlying}/*/*.parquet")):
        table = _conform(pq.read_table(path), arrow_schema)
        sources = table.column("source").to_pylist()
        if source not in set(sources):
            continue

        strikes = table.column("strike").to_pylist()
        spots = table.column("spot").to_pylist()
        types = table.column("opt_type").to_pylist()
        levels = table.column("moneyness").to_pylist()

        new_levels, touched = [], 0
        for i, origin in enumerate(sources):
            level = levels[i]
            if origin == source and level is None and spots[i]:
                level = mny.compute(float(strikes[i] or 0), float(spots[i]),
                                    types[i] or "CE", step)
                if level is not None:
                    touched += 1
            new_levels.append(level)

        if not touched:
            continue
        index = arrow_schema.get_field_index("moneyness")
        field = arrow_schema.field(index)
        table = table.set_column(index, field, pa.array(new_levels, field.type))
        _atomic_write(table, path)
        files += 1
        filled += touched

    return {"files": files, "rows": filled, "step": step}


def compact(table_name: str, key: str, source: str) -> dict[str, int]:
    """Fold a source's side files back into its own partition file.

    Bulk ingest writes `<source>-<part>.parquet` beside `<source>.parquet` to
    avoid re-reading the month on every batch. Reads union the whole directory,
    so this is a tidying step and not a correctness one — with one exception
    that makes it worth running: a job killed *between* two month writes of one
    batch marks nothing done, so the contract is fetched again and its rows can
    land twice in different files. The de-duplication here is what removes them.

    Idempotent, so it is safe to run whenever, including on an already-compacted
    lake or after an interrupted run.
    """
    root = sch.table_dir(table_name)
    column = sch.partition_key_column(table_name)
    arrow_schema = sch.SCHEMAS[table_name]
    merged = removed = 0

    for parent in sorted({p.parent for p in
                          root.glob(f"{column}={key}/*/{source}-*.parquet")}):
        parts = sorted(parent.glob(f"{source}-*.parquet"))
        if not parts:
            continue
        target = parent / f"{source}.parquet"
        tables = [_conform(pq.read_table(p), arrow_schema) for p in parts]
        if target.exists():
            tables.insert(0, _conform(pq.read_table(target), arrow_schema))
        table = _dedupe(pa.concat_tables(tables), table_name, arrow_schema)
        _atomic_write(table, target)
        for path in parts:
            path.unlink()
            removed += 1
        merged += 1
    return {"partitions": merged, "files_removed": removed}


def replace_source(table_name: str, key: str, source: str) -> int:
    """Delete every partition file written by one source. Used to re-pull a
    vendor batch found to be wrong without disturbing our own recording."""
    root = sch.table_dir(table_name)
    column = sch.partition_key_column(table_name)
    removed = 0
    for path in root.glob(f"{column}={key}/*/{source}.parquet"):
        path.unlink()
        removed += 1
    return removed


def coverage(table_name: str = sch.OPTION_BARS) -> list[dict[str, Any]]:
    """What is actually in the lake, per instrument and source.

    Wrapped in `read` because this scans every partition, which makes it the
    most likely query in the app to collide with a backfill replacing one —
    and it backs the dashboard's status panel, so the collision showed up as a
    500 on a page that is open all day.
    """
    column = sch.partition_key_column(table_name)

    def once() -> list[dict[str, Any]]:
        con = connect()
        try:
            return [
                dict(zip(("key", "source", "bars", "first", "last", "days"), row),
                     table=table_name)
                for row in con.execute(
                    f"SELECT {column}, source, count(*), min(ts), max(ts), "
                    f"count(DISTINCT ts::DATE) "
                    f"FROM {table_name} GROUP BY 1, 2 ORDER BY 1, 2"
                ).fetchall()
            ]
        finally:
            con.close()
    return read(once)


# Disk space, in gigabytes. Two thresholds because they answer two questions.
#
# `MIN_FREE_GB` is the hard floor a bulk writer stops at. Running a pull out of
# space is worse than not finishing it: Parquet writes start failing mid-batch
# and everything else on the machine starts failing with them. The lake itself
# survives — `_atomic_write` renames a temp file into place, so a failed write
# leaves the old partition intact — but nothing else on a full Mac does.
#
# `WARN_FREE_GB` is the number that has to be visible *before* that, because by
# the time a bulk pull halts the recorder has already been writing into the same
# shrinking space all session. A session the recorder could not flush cannot be
# refetched from any vendor once the retention window closes, which makes it the
# one loss here that is permanent.
#
# These live here rather than beside any one caller because the recorder, both
# backfills and the dashboard all write to or report on the same volume, and a
# floor that only one of them knows about is not a floor.
MIN_FREE_GB = 3.0
WARN_FREE_GB = 10.0


def free_gb(path=None) -> float:
    """Free space on the volume holding the lake."""
    import shutil

    return shutil.disk_usage(path or sch.LAKE_DIR.parent).free / 1e9


def size_on_disk() -> dict[str, Any]:
    """What the lake uses, and what is left to grow into.

    Free space is reported alongside the size because every caller that cares
    how big the lake is cares more whether the next write will land. This is
    the one function all of them already call.
    """
    total, files = 0, 0
    for path in sch.LAKE_DIR.rglob("*.parquet"):
        total += path.stat().st_size
        files += 1
    free = free_gb()
    return {
        "bytes": total, "mb": round(total / 1e6, 1), "files": files,
        "free_gb": round(free, 1),
        "free_state": ("critical" if free < MIN_FREE_GB
                       else "warn" if free < WARN_FREE_GB else "ok"),
    }


def missing_days(underlying: str, start: date, end: date,
                 source: str | None = None) -> list[date]:
    """Trading days in the range with no option bars.

    Weekends are excluded; NSE holidays are not modelled, so a handful of the
    dates this returns are holidays rather than gaps. That is deliberate — a
    stale hard-coded holiday list would quietly hide real gaps, and re-fetching
    a holiday costs one empty response.
    """
    clause = f"AND source = '{source}'" if source else ""

    def once() -> set[date]:
        con = connect()
        try:
            return {
                row[0] for row in con.execute(
                    f"SELECT DISTINCT ts::DATE FROM {sch.OPTION_BARS} "
                    f"WHERE underlying = ? AND ts::DATE BETWEEN ? AND ? {clause}",
                    [underlying, start, end],
                ).fetchall()
            }
        finally:
            con.close()
    # Same race as `coverage`: this is what the health banner runs, and a
    # backfill replacing a partition mid-scan turned it into a red banner
    # about missing days that were not missing.
    present = read(once)

    out, day = [], start
    while day <= end:
        if day.weekday() < 5 and day not in present:
            out.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return out


def query(sql: str, params: Iterable[Any] | None = None) -> list[tuple]:
    def once() -> list[tuple]:
        con = connect()
        try:
            return con.execute(sql, list(params or [])).fetchall()
        finally:
            con.close()
    return read(once)
