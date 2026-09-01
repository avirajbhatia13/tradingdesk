"""Selecting a contract by its real expiry date rather than by rolling series.

Dhan's rolling endpoint never names the contract, so its rows carry a null
`expiry` and a `series` of 'WEEK' or 'MONTH'. Upstox's expired-contract data and
our own recorder both name the instrument, so they carry a real expiry and a
null series — and until this existed, every engine query filtered `series =
'WEEK'` and therefore could not see a single one of those rows. The widest data
in the lake was the only data a backtest could not reach.

The hazard being defended against here is specific. On any session five or six
weekly expiries are live at once, all with a strike at the money, all printing
in the same minute. Getting the ladder wrong does not raise anything — it
returns a full, plausible chain belonging to a different contract.
"""

from datetime import date, datetime, time, timedelta

import pytest

from app.backtest import engine
from app.backtest.engine import LegSpec, StrategySpec, run
from app.data import lake
from app.data import schema as sch

LOT = 75


def _session_rows(day, expiry, price, strikes=(23500.0, 24000.0, 24500.0),
                  spot=24000.0, source="test"):
    """One session of flat-priced bars for one expiry."""
    rows = []
    for minute in range(375):
        stamp = datetime(day.year, day.month, day.day, 9, 15) \
            + timedelta(minutes=minute)
        for opt_type in ("CE", "PE"):
            for strike in strikes:
                offset = int((strike - spot) / 50)
                rows.append({
                    "ts": stamp, "underlying": "NIFTY", "expiry": expiry,
                    "series": None, "strike": strike, "opt_type": opt_type,
                    "moneyness": offset if opt_type == "CE" else -offset,
                    "open": price, "high": price, "low": price, "close": price,
                    "volume": 1000, "oi": 10000, "iv": 12.0, "spot": spot,
                    "source": source,
                })
    return rows


@pytest.fixture()
def laddered_lake(tmp_path, monkeypatch):
    """Three sessions, each with two live expiries priced far apart.

    The near expiry is worth 20 and the far one 200, so which contract a run
    picked is legible in a single number instead of having to be inferred.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    engine.clear_matrix_cache()

    near, far = date(2026, 8, 11), date(2026, 8, 18)
    rows = []
    for day in (date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7)):
        rows += _session_rows(day, near, 20.0)
        rows += _session_rows(day, far, 200.0)
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    lake.write_contracts("NIFTY", [
        {"expiry": near, "strike": 24000.0, "opt_type": "CE", "weekly": True,
         "instrument_key": "K|near"},
        {"expiry": far, "strike": 24000.0, "opt_type": "CE", "weekly": False,
         "instrument_key": "K|far"},
    ])
    return tmp_path


def _spec(**kw):
    return StrategySpec(
        name="t", legs=[LegSpec("CE", "SELL", 0)],
        entry_time=time(9, 20), exit_time=time(15, 15),
        lot_size=LOT, slippage_points=0.0, **kw)


def test_the_front_expiry_is_the_near_one_not_whatever_prints(laddered_lake):
    """Both contracts print at every minute; only the ladder tells them apart."""
    ctx = engine.load_context(_spec(expiry_index=0), "NIFTY",
                              date(2026, 8, 1), date(2026, 8, 31))
    assert ctx.expiry_by_day
    assert set(ctx.expiry_by_day.values()) == {date(2026, 8, 11)}


def test_the_next_expiry_is_reachable_by_index(laddered_lake):
    ctx = engine.load_context(_spec(expiry_index=1), "NIFTY",
                              date(2026, 8, 1), date(2026, 8, 31))
    assert set(ctx.expiry_by_day.values()) == {date(2026, 8, 18)}


def test_the_two_expiries_are_priced_apart_so_the_choice_is_visible(laddered_lake):
    """A run must not average two contracts that share a strike and a minute."""
    front = run(_spec(expiry_index=0), "NIFTY", date(2026, 8, 1), date(2026, 8, 31))
    back = run(_spec(expiry_index=1), "NIFTY", date(2026, 8, 1), date(2026, 8, 31))

    assert front.trades and back.trades
    assert front.trades[0].entry_price == pytest.approx(-20.0)
    assert back.trades[0].entry_price == pytest.approx(-200.0)


def test_the_vendors_weekly_flag_picks_the_monthly(laddered_lake):
    """`expiry_kind` narrows the ladder before the index is applied."""
    ctx = engine.load_context(_spec(expiry_index=0, expiry_kind="monthly"),
                              "NIFTY", date(2026, 8, 1), date(2026, 8, 31))
    assert set(ctx.expiry_by_day.values()) == {date(2026, 8, 18)}

    ctx = engine.load_context(_spec(expiry_index=0, expiry_kind="weekly"),
                              "NIFTY", date(2026, 8, 1), date(2026, 8, 31))
    assert set(ctx.expiry_by_day.values()) == {date(2026, 8, 11)}


def test_days_to_expiry_filters_the_sessions(laddered_lake):
    """`max_dte = 0` is 'expiry day only' — unsayable before this existed.

    Asserted on the schedule rather than on the expiry map, because the band
    constrains where a position may be **opened** and nothing else. The map has
    to keep every session: a held position is carried through sessions that fail
    the band by construction, and dropping them from the day list truncated
    every hold to a single day while reporting it as a contract roll.
    """
    ctx = engine.load_context(_spec(expiry_index=0, max_dte=5), "NIFTY",
                              date(2026, 8, 1), date(2026, 8, 31))
    assert [entry for entry, _ in ctx.schedule] == [date(2026, 8, 6),
                                                    date(2026, 8, 7)]

    ctx = engine.load_context(_spec(expiry_index=0, min_dte=5), "NIFTY",
                              date(2026, 8, 1), date(2026, 8, 31))
    assert [entry for entry, _ in ctx.schedule] == [date(2026, 8, 5),
                                                    date(2026, 8, 6)]


def test_a_missing_expiry_is_skipped_rather_than_substituted(tmp_path,
                                                            monkeypatch):
    """The failure this whole design exists to prevent.

    Found on real data while building it: the backfill had not reached the
    2026-06-09 weekly, so ranking only what was on disk promoted the 14-day
    contract into the front slot. "Sell the front weekly straddle" entered at
    576 points instead of 114 — a different strategy, priced perfectly, with
    nothing anywhere saying so.

    The ladder therefore comes from what the vendor LISTED, and a session whose
    contract is absent is dropped and counted.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    engine.clear_matrix_cache()

    near, far = date(2026, 8, 11), date(2026, 8, 18)
    # Only the FAR contract has bars; the near one was listed and never pulled.
    lake.write_bars(sch.OPTION_BARS, "NIFTY",
                    _session_rows(date(2026, 8, 5), far, 200.0), "test")
    lake.write_contracts("NIFTY", [
        {"expiry": near, "strike": 24000.0, "opt_type": "CE", "weekly": True,
         "instrument_key": "K|near"},
        {"expiry": far, "strike": 24000.0, "opt_type": "CE", "weekly": True,
         "instrument_key": "K|far"},
    ])

    result = run(_spec(expiry_index=0), "NIFTY",
                 date(2026, 8, 1), date(2026, 8, 31))

    assert result.trades == [], "traded a contract the rule did not name"
    assert result.selection.missing_expiry_days == 1
    assert "not in the lake" in result.selection.note


def test_rolling_selection_is_untouched(tmp_path, monkeypatch):
    """Dated selection must be opt-in. Every stored run is a rolling run, and
    `tools.backtest verify` checks them to the rupee."""
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    engine.clear_matrix_cache()

    rows = _session_rows(date(2026, 8, 5), None, 20.0)
    for row in rows:
        row["series"] = "WEEK"
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")

    result = run(_spec(expiry_flag="WEEK"), "NIFTY",
                 date(2026, 8, 1), date(2026, 8, 31))
    assert len(result.trades) == 1
    assert result.trades[0].entry_price == pytest.approx(-20.0)


def test_two_sources_holding_one_contract_are_not_two_candidates(tmp_path,
                                                                monkeypatch):
    """Upstox history and our own recorder overlap on purpose, so the handover
    has no gap. Un-aggregated that is the same strike twice on one board, which
    a premium or delta selector would treat as two contracts."""
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    engine.clear_matrix_cache()

    expiry = date(2026, 8, 11)
    lake.write_bars(sch.OPTION_BARS, "NIFTY",
                    _session_rows(date(2026, 8, 5), expiry, 20.0,
                                  source="upstox"), "upstox")
    lake.write_bars(sch.OPTION_BARS, "NIFTY",
                    _session_rows(date(2026, 8, 5), expiry, 20.0,
                                  source="live"), "live")
    lake.write_contracts("NIFTY", [
        {"expiry": expiry, "strike": 24000.0, "opt_type": "CE", "weekly": True,
         "instrument_key": "K|a"}])

    picked = {date(2026, 8, 5): expiry}
    con = lake.connect()
    try:
        con.register("expiry_pick", engine._expiry_table(picked))
        chains = engine._chains(
            _spec(expiry_index=0), "NIFTY", date(2026, 8, 1), date(2026, 8, 31),
            con, picked)
    finally:
        con.close()

    chain = chains[date(2026, 8, 5)]
    strikes = [row.strike for row in chain.calls]
    assert len(strikes) == len(set(strikes)), f"duplicated board: {strikes}"


def test_time_to_expiry_is_exact_once_the_contract_is_dated(laddered_lake):
    """The engine's clock and the report's margin clock must agree.

    Rolling data forces a half-cycle approximation; a dated contract does not,
    and a delta selected on one clock while margin is sized on another is the
    quiet disagreement that makes a backtest and a live position diverge.
    """
    picked = {date(2026, 8, 5): date(2026, 8, 11)}
    con = lake.connect()
    try:
        con.register("expiry_pick", engine._expiry_table(picked))
        chains = engine._chains(
            _spec(expiry_index=0), "NIFTY", date(2026, 8, 1), date(2026, 8, 31),
            con, picked)
    finally:
        con.close()

    days = chains[date(2026, 8, 5)].t_years * 365
    assert days == pytest.approx(6.5, abs=0.01)      # 6 days out, plus half a session

    from app.backtest.report import _t_years

    assert _t_years(_spec(expiry_index=0), date(2026, 8, 5),
                    {date(2026, 8, 5): date(2026, 8, 11)}) * 365 == \
        pytest.approx(6.5, abs=0.01)


def test_a_hold_never_crosses_a_dated_roll(laddered_lake):
    """With a real date the roll boundary is known, not recovered from vol."""
    spec = _spec(expiry_index=0, hold_days=10)
    ctx = engine.load_context(spec, "NIFTY", date(2026, 8, 1), date(2026, 8, 31))
    for entry, exit_ in ctx.schedule:
        assert exit_ <= ctx.expiry_by_day[entry], (
            f"held {entry} -> {exit_} past its {ctx.expiry_by_day[entry]} expiry")


def test_the_contract_label_never_calls_a_dated_run_weekly():
    """It goes into the saved record; a dated run is not a 'week' run."""
    assert engine.contract_label(_spec(expiry_flag="WEEK")) == "WEEK"
    assert engine.contract_label(_spec(expiry_index=0)) == "front expiry"
    assert engine.contract_label(_spec(expiry_index=1, expiry_kind="monthly")) \
        == "next monthly expiry"
    assert "dte 0..0" in engine.contract_label(
        _spec(expiry_index=0, min_dte=0, max_dte=0))


@pytest.fixture()
def rolling_ladder_lake(tmp_path, monkeypatch):
    """Sessions that straddle an expiry, so the ladder shifts underneath a hold.

    Three expiries priced decades apart — 10, 100 and 1000 — so the contract a
    position is being marked against is legible in a single number rather than
    having to be inferred. Aug 11 expires midway, which promotes Aug 18 from
    'next' to 'front' and Aug 25 into the 'next' slot.
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")
    engine.clear_matrix_cache()

    e1, e2, e3 = date(2026, 8, 11), date(2026, 8, 18), date(2026, 8, 25)
    rows = []
    for day in (date(2026, 8, 6), date(2026, 8, 7)):
        rows += _session_rows(day, e1, 10.0)
        rows += _session_rows(day, e2, 100.0)
        rows += _session_rows(day, e3, 1000.0)
    for day in (date(2026, 8, 12), date(2026, 8, 13)):
        rows += _session_rows(day, e2, 100.0)
        rows += _session_rows(day, e3, 1000.0)
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    lake.write_contracts("NIFTY", [
        {"expiry": e, "strike": 24000.0, "opt_type": "CE", "weekly": True,
         "instrument_key": f"K|{e}"} for e in (e1, e2, e3)
    ])
    return tmp_path


def test_a_days_to_expiry_band_does_not_shorten_a_hold(rolling_ladder_lake):
    """The band says where a position may open, not which sessions exist.

    Found on real data: `--dte-min 4 --dte-max 4 --hold 2` returned P&L
    byte-identical to `--hold 0`, because the DTE filter had already removed
    every session the position needed to be carried through. `_schedule` then
    found no later session to run to, truncated all 38 positions to a single
    day, and reported it as 'cut short by a contract roll' — a silent no-op
    wearing the label of a deliberate safety rule.
    """
    # Aug 6 is 5 days from the Aug 11 front expiry; Aug 7 is 4 and fails the
    # band. The position must still be carried through Aug 7.
    spec = _spec(expiry_index=0, min_dte=5, max_dte=5, hold_days=1)
    ctx = engine.load_context(spec, "NIFTY", date(2026, 8, 1), date(2026, 8, 14))

    assert ctx.schedule, "the band left no session to open a position on"
    entry, exit_ = ctx.schedule[0]
    assert entry == date(2026, 8, 6)
    assert exit_ == date(2026, 8, 7), (
        "the hold was truncated to its entry session: the sessions it needed "
        "to span were filtered out before the schedule was built")


def test_a_held_position_keeps_the_contract_it_entered(rolling_ladder_lake):
    """A hold is marked against one contract, never re-resolved per session.

    With `--expiry next` the ladder shifts the moment the front expires: a
    position opened on Aug 6 enters the Aug 18 contract at 100, but on Aug 12
    'next' has become Aug 25, which prints 1000. Re-resolving per session
    spliced the two into one price path and booked the 900-point step as
    profit. On real NIFTY data this turned a +Rs78,284 strategy into
    -Rs94,168 with nothing anywhere saying so.
    """
    spec = _spec(expiry_index=1, hold_days=4)
    result = run(spec, "NIFTY", date(2026, 8, 1), date(2026, 8, 14))

    assert result.trades
    trade = result.trades[0]
    assert trade.day == date(2026, 8, 6)
    assert engine._as_date(trade.exit_ts) > date(2026, 8, 11), (
        "the hold did not reach past the front expiry, so this asserts nothing")
    # Entered and exited on the same contract, which is flat at 100.
    assert trade.entry_price == pytest.approx(-100.0)
    assert trade.exit_price == pytest.approx(-100.0), (
        "the position was re-priced on a contract it never traded")
    assert trade.gross == pytest.approx(0.0)


def test_an_empty_dated_range_explains_itself_instead_of_raising(tmp_path,
                                                                 monkeypatch):
    """Asking for a dated contract where the lake holds none must not traceback.

    `_expiry_map` returned a 2-tuple on its empty path while the caller unpacked
    three, so `--expiry front` over a range with no dated bars died on
    `ValueError: not enough values to unpack` — in exactly the case the engine
    has a purpose-built explanation for (`_why_no_chain`). Loud, but loud in the
    wrong language: it read as an engine bug rather than as "you have not
    backfilled that range".
    """
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path)
    con = lake.connect()
    try:
        chosen, missing, entry_ok = engine._expiry_map(
            StrategySpec(name="probe", legs=[LegSpec("CE", "SELL", 0)],
                         expiry_index=0),
            "NIFTY", date(2019, 1, 1), date(2019, 12, 31), con)
    finally:
        con.close()

    assert chosen == {}
    assert missing == 0
    assert entry_ok == set()
