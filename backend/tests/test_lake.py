"""The storage layer's guarantees.

Two of these matter more than the rest. Idempotent ingest is what makes a
five-year backfill restartable — without it every interruption silently doubles
a month's rows and every backtest run afterwards is wrong by an unknown amount.
Schema stability is what stops the lake tearing itself apart on the second
write, because DuckDB widens types on the way through and Parquet will not
union files that disagree about them.
"""

from datetime import date, datetime

import pytest

from app.data import lake
from app.data import schema as sch


@pytest.fixture()
def empty_lake(tmp_path, monkeypatch):
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    return tmp_path / "lake"


def bar(minute: int, strike: float = 24000.0, opt_type: str = "CE",
        close: float = 100.0, day: date = date(2026, 8, 14)) -> dict:
    return {
        "ts": datetime(day.year, day.month, day.day, 9, 15) .replace(
            minute=15 + minute % 45, hour=9 + minute // 45),
        "underlying": "NIFTY", "expiry": None, "series": "WEEK", "strike": strike,
        "opt_type": opt_type, "moneyness": 0,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 100, "oi": 1000, "iv": 0.12, "spot": 24000.0,
    }


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------

def test_write_then_read_back(empty_lake):
    rows = [bar(i) for i in range(10)]
    assert lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "dhan") == 10

    con = lake.connect()
    try:
        count, = con.execute("SELECT count(*) FROM option_bars").fetchone()
    finally:
        con.close()
    assert count == 10


def test_an_empty_lake_still_answers_queries(empty_lake):
    """Every caller would otherwise need a 'have you backfilled yet' branch."""
    con = lake.connect()
    try:
        assert con.execute("SELECT count(*) FROM option_bars").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM spot_bars").fetchone()[0] == 0
    finally:
        con.close()


def test_spot_bars_round_trip(empty_lake):
    rows = [{"ts": datetime(2026, 8, 14, 9, 15 + i), "symbol": "NIFTY",
             "open": 24000.0 + i, "high": 24010.0 + i, "low": 23990.0 + i,
             "close": 24005.0 + i, "volume": 0} for i in range(5)]
    lake.write_bars(sch.SPOT_BARS, "NIFTY", rows, "dhan")
    con = lake.connect()
    try:
        assert con.execute("SELECT count(*) FROM spot_bars").fetchone()[0] == 5
    finally:
        con.close()


# ---------------------------------------------------------------------------
# idempotency — the property that makes a backfill restartable
# ---------------------------------------------------------------------------

def test_rewriting_the_same_window_does_not_duplicate(empty_lake):
    rows = [bar(i) for i in range(10)]
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", list(rows), "dhan")

    con = lake.connect()
    try:
        assert con.execute("SELECT count(*) FROM option_bars").fetchone()[0] == 10
    finally:
        con.close()


def test_a_later_write_wins_for_the_same_bar(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0, close=100.0)], "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0, close=222.0)], "dhan")
    con = lake.connect()
    try:
        rows = con.execute("SELECT close FROM option_bars").fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(222.0)


def test_different_strikes_in_the_same_minute_both_survive(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY",
                    [bar(0, strike=24000.0), bar(0, strike=24100.0),
                     bar(0, strike=24000.0, opt_type="PE")], "dhan")
    con = lake.connect()
    try:
        assert con.execute("SELECT count(*) FROM option_bars").fetchone()[0] == 3
    finally:
        con.close()


def test_appending_a_second_batch_keeps_the_first(empty_lake):
    """The recorder flushes every few minutes; each flush must add, not replace."""
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(i) for i in range(5)], "live")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(i) for i in range(5, 12)], "live")
    con = lake.connect()
    try:
        assert con.execute("SELECT count(*) FROM option_bars").fetchone()[0] == 12
    finally:
        con.close()


# ---------------------------------------------------------------------------
# schema stability and source separation
# ---------------------------------------------------------------------------

def test_a_second_write_does_not_trip_on_widened_types(empty_lake):
    """Regression: DuckDB returns timestamp('s') as microseconds and float32 as
    double, so a naive merge fails on the second write of any month."""
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0)], "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(1)], "dhan")   # would raise
    con = lake.connect()
    try:
        assert con.execute("SELECT count(*) FROM option_bars").fetchone()[0] == 2
    finally:
        con.close()


def test_sources_are_stored_separately_and_both_readable(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0, close=100.0)], "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(1, close=101.0)], "live")
    con = lake.connect()
    try:
        rows = dict(con.execute(
            "SELECT source, count(*) FROM option_bars GROUP BY source").fetchall())
    finally:
        con.close()
    assert rows == {"dhan": 1, "live": 1}


def test_a_bad_vendor_batch_can_be_dropped_without_touching_our_recording(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(i) for i in range(4)], "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(i + 10) for i in range(3)], "live")

    assert lake.replace_source(sch.OPTION_BARS, "NIFTY", "dhan") == 1
    con = lake.connect()
    try:
        rows = dict(con.execute(
            "SELECT source, count(*) FROM option_bars GROUP BY source").fetchall())
    finally:
        con.close()
    assert rows == {"live": 3}


def test_months_land_in_separate_partitions(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY",
                    [bar(0, day=date(2026, 7, 14)), bar(0, day=date(2026, 8, 14))],
                    "dhan")
    files = sorted(p.parent.name for p in empty_lake.rglob("*.parquet"))
    assert files == ["month=2026-07", "month=2026-08"]


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def test_coverage_reports_what_is_present(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(i) for i in range(6)], "dhan")
    rows = lake.coverage(sch.OPTION_BARS)
    assert len(rows) == 1
    assert rows[0]["key"] == "NIFTY"
    assert rows[0]["source"] == "dhan"
    assert rows[0]["bars"] == 6


def test_missing_days_skips_weekends_and_present_days(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0, day=date(2026, 8, 12))], "dhan")
    gaps = lake.missing_days("NIFTY", date(2026, 8, 10), date(2026, 8, 16))
    # 10th Mon, 11th Tue, 12th Wed (present), 13th Thu, 14th Fri; 15-16 weekend.
    assert date(2026, 8, 12) not in gaps
    assert date(2026, 8, 15) not in gaps and date(2026, 8, 16) not in gaps
    assert {date(2026, 8, 10), date(2026, 8, 11),
            date(2026, 8, 13), date(2026, 8, 14)} <= set(gaps)


# ---------------------------------------------------------------------------
# expiry series
# ---------------------------------------------------------------------------

def test_weekly_and_monthly_bars_do_not_overwrite_each_other(empty_lake):
    """Dhan's rolling endpoint never names the contract, so `expiry` is null on
    every row it returns. Without `series` in the dedupe key, the weekly and
    monthly ATM bar for the same minute and strike are the same key — one
    silently replaces the other and the lake ends up holding a spliced
    instrument that never traded. Nothing raises; the prices just stop being
    one contract's.
    """
    week = bar(0, close=100.0)
    month = dict(bar(0, close=250.0), series="MONTH")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [week, month], "dhan")

    rows = lake.query(
        "SELECT series, close FROM option_bars ORDER BY series")
    assert rows == [("MONTH", pytest.approx(250.0)),
                    ("WEEK", pytest.approx(100.0))]


def test_the_same_series_still_deduplicates(empty_lake):
    """Adding `series` to the key must not weaken idempotence within a series."""
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0, close=100.0)], "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0, close=111.0)], "dhan")

    rows = lake.query("SELECT count(*), max(close) FROM option_bars")
    assert rows[0][0] == 1
    assert rows[0][1] == pytest.approx(111.0)     # last write wins


def test_files_written_before_the_series_column_still_merge(empty_lake):
    """A schema change must not require rewriting the whole lake at once. A
    partition written without `series` has to gain it as null on the next
    merge rather than blowing the write up."""
    import pyarrow.parquet as pq

    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0)], "dhan")
    path = next(sch.table_dir(sch.OPTION_BARS).rglob("*.parquet"))
    legacy = pq.read_table(path).drop_columns(["series"])
    pq.write_table(legacy, path)

    written = lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(1)], "dhan")
    assert written == 1
    assert lake.query("SELECT count(*) FROM option_bars")[0][0] == 2


def test_tag_series_only_touches_null_rows_of_one_source(empty_lake):
    """The migration for rows written before the column existed. It must leave
    our own recording alone — live rows identify their contract by a real
    expiry date and are meant to keep a null series."""
    import pyarrow.parquet as pq

    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0)], "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY",
                    [dict(bar(1), series=None)], "live")
    for path in sch.table_dir(sch.OPTION_BARS).rglob("dhan.parquet"):
        pq.write_table(pq.read_table(path).drop_columns(["series"]), path)

    assert lake.tag_series()["rows"] == 1
    assert lake.tag_series()["rows"] == 0          # idempotent

    rows = dict(lake.query("SELECT source, any_value(series) "
                           "FROM option_bars GROUP BY source"))
    assert rows["dhan"] == "WEEK"
    assert rows["live"] is None


# ---------------------------------------------------------------------------
# concurrent reads
# ---------------------------------------------------------------------------

def test_a_torn_read_is_retried():
    """DuckDB parallel scans reopen files by path, so a partition replaced
    mid-scan can fail with a Thrift error even though nothing on disk is
    damaged. Observed twice during an active backfill."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("TProtocolException: Invalid data")
        return "ok"

    assert lake.read(flaky) == "ok"
    assert len(calls) == 3


def test_a_garbled_type_read_is_retried():
    """The other face of the same race: a column type comes back empty and
    DuckDB reports `don't know what type:` with nothing after the colon."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("don't know what type: ")
        return "ok"

    assert lake.read(flaky) == "ok"


def test_a_real_error_is_not_retried():
    """Retrying a genuine SQL or schema error would hide it behind a delay."""
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("Binder Error: no such column")

    with pytest.raises(RuntimeError, match="Binder Error"):
        lake.read(broken)
    assert len(calls) == 1


def test_a_genuine_unsupported_type_is_not_retried():
    """A real conversion failure names the type it could not handle. Only the
    empty-tailed version is the corruption signature."""
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("don't know what type: STRUCT(a INTEGER)")

    with pytest.raises(RuntimeError):
        lake.read(broken)
    assert len(calls) == 1


def test_size_on_disk_counts_files(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0)], "dhan")
    size = lake.size_on_disk()
    assert size["files"] == 1
    assert size["bytes"] > 0


def test_size_on_disk_reports_free_space(empty_lake):
    """Every caller that asks how big the lake is cares more whether the next
    write will land, and this is the one call all of them already make."""
    size = lake.size_on_disk()
    assert size["free_gb"] > 0
    assert size["free_state"] in ("ok", "warn", "critical")


@pytest.mark.parametrize("free, expected", [
    (200.0, "ok"),
    (lake.WARN_FREE_GB + 0.1, "ok"),
    (lake.WARN_FREE_GB - 0.1, "warn"),
    (lake.MIN_FREE_GB + 0.1, "warn"),
    (lake.MIN_FREE_GB - 0.1, "critical"),
    (0.0, "critical"),
])
def test_free_state_grades_the_thresholds(empty_lake, monkeypatch, free, expected):
    """Graded rather than binary because the two thresholds answer different
    questions: `warn` is still a working desk, `critical` is a bulk writer that
    must stop before a Parquet flush fails mid-batch.

    The boundaries are tested either side rather than at the value because an
    off-by-one here shows up as a warning that never fires, which looks exactly
    like a healthy disk."""
    monkeypatch.setattr(lake, "free_gb", lambda *a: free)
    assert lake.size_on_disk()["free_state"] == expected


# ---------------------------------------------------------------------------
# side files — the escape from a quadratic bulk ingest
# ---------------------------------------------------------------------------

def test_a_side_file_is_readable_without_being_compacted(empty_lake):
    """Reads glob the whole partition directory, so a batch is queryable the
    moment it lands. That is what lets a bulk ingest skip the merge."""
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0)], "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(1)], "dhan", part="a")
    rows = lake.query("SELECT count(*) FROM option_bars")
    assert rows[0][0] == 2


def test_a_side_file_does_not_rewrite_the_source_file(empty_lake):
    """The whole point: merging costs a read-modify-write of the month, and on
    a bulk ingest that is quadratic in the number of batches."""
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0)], "dhan")
    target = next(sch.table_dir(sch.OPTION_BARS).rglob("dhan.parquet"))
    before = target.stat().st_mtime_ns

    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(1)], "dhan", part="a")
    assert target.stat().st_mtime_ns == before


def test_compaction_folds_side_files_in_and_removes_them(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0)], "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(1)], "dhan", part="a")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(2)], "dhan", part="b")

    result = lake.compact(sch.OPTION_BARS, "NIFTY", "dhan")
    assert result["files_removed"] == 2
    assert not list(sch.table_dir(sch.OPTION_BARS).rglob("dhan-*.parquet"))
    assert lake.query("SELECT count(*) FROM option_bars")[0][0] == 3


def test_compaction_removes_rows_duplicated_across_side_files(empty_lake):
    """The one case where compaction is correctness, not tidying: a job killed
    between two month writes of a batch marks nothing done, so the contract is
    fetched again and its rows can land in a second file."""
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0), bar(1)], "dhan", part="a")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(1), bar(2)], "dhan", part="b")
    assert lake.query("SELECT count(*) FROM option_bars")[0][0] == 4   # the dup

    lake.compact(sch.OPTION_BARS, "NIFTY", "dhan")
    assert lake.query("SELECT count(*) FROM option_bars")[0][0] == 3


def test_compaction_is_idempotent(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0)], "dhan", part="a")
    lake.compact(sch.OPTION_BARS, "NIFTY", "dhan")
    again = lake.compact(sch.OPTION_BARS, "NIFTY", "dhan")
    assert again == {"partitions": 0, "files_removed": 0}
    assert lake.query("SELECT count(*) FROM option_bars")[0][0] == 1


def test_compaction_leaves_another_source_alone(empty_lake):
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(0)], "dhan", part="a")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [bar(1)], "upstox", part="a")
    lake.compact(sch.OPTION_BARS, "NIFTY", "dhan")
    assert list(sch.table_dir(sch.OPTION_BARS).rglob("upstox-*.parquet"))


# ---------------------------------------------------------------------------
# repairing a spot column stamped with the wrong resolution
# ---------------------------------------------------------------------------

def test_repair_spot_restamps_each_minute_and_redraws_moneyness(empty_lake):
    """The defect this exists for: the Upstox ingest stamped every bar in a
    session with the session's *average* spot. Nothing errored — moneyness
    simply stopped moving intraday, so every spot-relative rule resolved
    against a price the market never traded at."""
    rows = [bar(0, strike=24000.0), bar(30, strike=24000.0)]
    for row in rows:
        row["spot"] = 24000.0          # the day's average, on every bar
        row["moneyness"] = 0
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "upstox")

    per_minute = {r["ts"]: 23800.0 + i * 400.0 for i, r in enumerate(rows)}

    def remoneyness(strike, spot, opt_type):
        return int(round((strike - spot) / 50.0))

    result = lake.repair_spot("NIFTY", "upstox", per_minute, remoneyness)
    assert result["rows"] == 2

    got = lake.query("SELECT spot, moneyness FROM option_bars ORDER BY ts")
    assert [round(r[0], 1) for r in got] == [23800.0, 24200.0]
    assert [r[1] for r in got] == [4, -4]      # it moves now


def test_repair_spot_leaves_other_sources_alone(empty_lake):
    ours = bar(0, strike=24000.0)
    ours["spot"] = 24000.0
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [ours], "dhan")
    lake.repair_spot("NIFTY", "upstox", {ours["ts"]: 99999.0}, None)
    assert lake.query("SELECT spot FROM option_bars")[0][0] == 24000.0


def test_repair_spot_is_idempotent(empty_lake):
    row = bar(0, strike=24000.0)
    row["spot"] = 24000.0
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [row], "upstox")
    series = {row["ts"]: 23950.0}
    first = lake.repair_spot("NIFTY", "upstox", series, None)
    again = lake.repair_spot("NIFTY", "upstox", series, None)
    assert first["rows"] == 1 and again["rows"] == 0


def test_repair_spot_falls_back_to_the_day_when_a_minute_is_missing(empty_lake):
    """A slightly stale spot beats a null moneyness, but the fallback must be
    a price that existed rather than an average of the session."""
    row = bar(0, strike=24000.0)
    row["spot"] = 1.0
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [row], "upstox")
    lake.repair_spot("NIFTY", "upstox", {row["ts"].date(): 23900.0}, None)
    assert lake.query("SELECT spot FROM option_bars")[0][0] == 23900.0


def test_a_file_written_before_a_column_widened_still_reads(tmp_path, monkeypatch):
    """`moneyness` went int8 to int16 when the chain outgrew it.

    Every partition written before that change is still int8 on disk, and
    `_conform` promises the caller a table matching the declared schema. It
    used to hand back whatever the file held, which only worked while the two
    happened to agree.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    from app.data import lake
    from app.data import schema as sch

    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    arrow_schema = sch.SCHEMAS[sch.OPTION_BARS]

    # A file in the old shape: moneyness as int8.
    old_schema = pa.schema([
        f if f.name != "moneyness" else pa.field("moneyness", pa.int8())
        for f in arrow_schema])
    path = tmp_path / "old.parquet"
    pq.write_table(pa.table(
        {f.name: pa.nulls(1, f.type) if f.name != "moneyness"
         else pa.array([-7], pa.int8()) for f in old_schema},
        schema=old_schema), path)

    conformed = lake._conform(pq.read_table(path), arrow_schema)

    assert conformed.schema.field("moneyness").type == pa.int16()
    assert conformed.column("moneyness").to_pylist() == [-7], "the value was lost"


# ---------------------------------------------------------------------------
# deriving moneyness for rows that were recorded without it
# ---------------------------------------------------------------------------

def _live_bar(strike: float, opt_type: str, spot: float = 24000.0, minute: int = 0):
    """A bar shaped like the recorder's: real strike, real spot, no moneyness."""
    return dict(
        ts=datetime(2026, 8, 17, 9, 15 + minute), underlying="NIFTY",
        strike=strike, opt_type=opt_type, expiry=date(2026, 8, 20),
        open=1.0, high=1.0, low=1.0, close=1.0, volume=1, oi=1,
        spot=spot, moneyness=None)


def test_moneyness_is_derived_for_rows_recorded_without_it(empty_lake):
    """Our own recorder writes a real strike and a real spot and no moneyness,
    so every bar it has ever captured is unreachable from a backtest — no rule
    can name a strike without it, and `SELL CE 0` resolves to nothing.

    Nothing is fetched here: it is arithmetic over columns already present.
    """
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [
        _live_bar(24000.0, "CE"), _live_bar(24100.0, "CE", minute=1),
        _live_bar(23900.0, "PE", minute=2),
    ], "live")

    result = lake.derive_moneyness("NIFTY", "live", step=50.0)
    assert result["rows"] == 3

    got = {(r[0], r[1]): r[2] for r in lake.query(
        "SELECT strike, opt_type, moneyness FROM option_bars WHERE source='live'")}
    assert got[(24000.0, "CE")] == 0        # at the money
    assert got[(24100.0, "CE")] == 2        # two strikes above spot: 2 OTM
    # The line the whole module exists for: a put is out of the money BELOW
    # spot, so two strikes down is +2 OTM, not -2.
    assert got[(23900.0, "PE")] == 2


def test_deriving_moneyness_leaves_other_sources_alone(empty_lake):
    """Partition files hold every source together, so a rewrite that is not
    scoped would restamp vendor rows whose moneyness was derived at ingest from
    that minute's own spot."""
    # One vendor row already labelled, and one the vendor left NULL. The second
    # is the one that matters: a row with a value is protected by the
    # already-filled check whatever the scoping does, so testing only that
    # passes happily against a rewrite that ignores `source` entirely.
    lake.write_bars(sch.OPTION_BARS, "NIFTY",
                    [dict(_live_bar(24500.0, "CE"), moneyness=99),
                     dict(_live_bar(24600.0, "CE", minute=5), moneyness=None)],
                    "dhan")
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [_live_bar(24000.0, "CE")], "live")

    lake.derive_moneyness("NIFTY", "live", step=50.0)

    kept = {r[0]: r[1] for r in lake.query(
        "SELECT strike, moneyness FROM option_bars WHERE source='dhan'")}
    assert kept[24500.0] == 99, "a labelled vendor row must not be recomputed"
    assert kept[24600.0] is None, (
        "an UNLABELLED vendor row must be left alone too — deriving it here "
        "would use the recorder's strike step, which is not the vendor's")

    assert lake.query("SELECT moneyness FROM option_bars WHERE source='live'"
                      )[0][0] == 0, "and the live row is still filled in"


def test_deriving_moneyness_is_idempotent(empty_lake):
    """Safe to run after every session — which is the point, since the recorder
    adds bars daily and this has to be part of the routine rather than a
    one-off migration."""
    lake.write_bars(sch.OPTION_BARS, "NIFTY", [_live_bar(24100.0, "CE")], "live")

    first = lake.derive_moneyness("NIFTY", "live", step=50.0)
    second = lake.derive_moneyness("NIFTY", "live", step=50.0)
    assert first["rows"] == 1
    assert second["rows"] == 0, "a row that already has a label is left alone"


def test_a_row_with_no_spot_keeps_a_null_moneyness(empty_lake):
    """A wrong integer here is worse than a missing one: the engine selects on
    this column, so a guess silently trades a strike the rule never named."""
    lake.write_bars(sch.OPTION_BARS, "NIFTY",
                    [dict(_live_bar(24000.0, "CE"), spot=None)], "live")

    lake.derive_moneyness("NIFTY", "live", step=50.0)
    assert lake.query("SELECT moneyness FROM option_bars")[0][0] is None
