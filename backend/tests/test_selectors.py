"""Choosing which contract a leg enters.

The engine could only ever answer "how many strikes out of the money?", which
made a large class of ordinary strategies unsayable — "sell the 20 delta",
"buy the wing at a third of the ATM premium", "go 1% out". These tests pin the
rules against a chain whose premiums are arithmetic, so the right answer is
checkable rather than plausible.

The clamping tests are the important ones. The lake holds ±10 strikes and a
target outside that range always has *some* nearest match, so a selector that
did not report settling would turn a missing contract into a confident number.
"""

from datetime import date, datetime, time

import pytest

from app.backtest import selectors as sel
from app.backtest.engine import LegSpec, StrategySpec, load_context, run
from app.data import lake
from app.data import schema as sch

DAY = date(2026, 8, 14)
LOT = 75
SPOT = 24000.0
STEP = 50


def _price(moneyness: int) -> float:
    """A clean ladder: 100 at the money, 10 points less per strike out.

    Chosen so every selector's answer is arithmetic — a third of the ATM
    premium is 33.3, which is nearest the 7th strike, and nothing about that
    depends on a pricing model.
    """
    return max(100.0 - 10.0 * moneyness, 5.0)


@pytest.fixture()
def chain_lake(tmp_path, monkeypatch):
    """A flat session with the full ±10 chain, both sides, priced by the ladder."""
    monkeypatch.setattr(sch, "LAKE_DIR", tmp_path / "lake")

    rows = []
    for minute in range(375):
        stamp = datetime(DAY.year, DAY.month, DAY.day, 9, 15)
        stamp = stamp.replace(hour=9 + (15 + minute) // 60,
                              minute=(15 + minute) % 60)
        for moneyness in range(-10, 11):
            for opt_type in ("CE", "PE"):
                strike = (SPOT + moneyness * STEP if opt_type == "CE"
                          else SPOT - moneyness * STEP)
                price = _price(moneyness)
                rows.append({
                    "ts": stamp, "underlying": "NIFTY", "expiry": None,
                    "series": "WEEK", "strike": float(strike),
                    "opt_type": opt_type, "moneyness": moneyness,
                    "open": price, "high": price, "low": price, "close": price,
                    "volume": 1000, "oi": 100000, "iv": 12.0, "spot": SPOT,
                })
    lake.write_bars(sch.OPTION_BARS, "NIFTY", rows, "test")
    return tmp_path


def _spec(*legs, **kwargs) -> StrategySpec:
    base = dict(name="t", lot_size=LOT, slippage_points=0.0,
                entry_time=time(9, 15), exit_time=time(15, 29),
                legs=list(legs))
    base.update(kwargs)
    from app.backtest import costs as costs_mod
    base.setdefault("costs", costs_mod.FREE)
    return StrategySpec(**base)


def _landed(spec: StrategySpec) -> dict[str, int]:
    """Where each leg's rule actually put it, in strikes from the money."""
    context = load_context(spec, "NIFTY", DAY, DAY)
    return {column: stat["moneyness_median"]
            for column, stat in context.selection.per_leg.items()}


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------

def test_moneyness_still_means_what_it_meant(chain_lake):
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByMoneyness(3)))
    assert list(_landed(spec).values()) == [3]


def test_a_premium_target_finds_the_strike_trading_there(chain_lake):
    """100 at the money falling 10 a strike, so ₹50 is the 5th strike out."""
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByPremium(50.0)))
    assert list(_landed(spec).values()) == [5]


def test_a_premium_ratio_is_the_rule_the_engine_was_rebuilt_for(chain_lake):
    """"Buy the call whose LTP is the ATM call's premium over three."

    Unsayable in strikes, because the answer moves with volatility: the same
    rule lands on a near strike on a quiet day and a far one on a wild one.
    Here the ATM is 100, so the target is 33.3 and the nearest is the 7th
    strike at 30.
    """
    spec = _spec(LegSpec("CE", "BUY", lots=3,
                         select=sel.ByPremiumRatio(0, 1 / 3)))
    assert list(_landed(spec).values()) == [7]


def test_each_side_reads_its_own_reference(chain_lake):
    """'Their respective ATM' — the call leg off the call chain, the put leg
    off the put chain. Both land at 7 here because the ladder is symmetric;
    what is being pinned is that they resolve independently."""
    spec = _spec(LegSpec("CE", "BUY", select=sel.ByPremiumRatio(0, 1 / 3)),
                 LegSpec("PE", "BUY", select=sel.ByPremiumRatio(0, 1 / 3)))
    landed = _landed(spec)
    assert len(landed) == 2
    assert set(landed.values()) == {7}


def test_percent_of_spot_goes_out_of_the_money_on_both_sides(chain_lake):
    """1% of 24,000 is 240 points: the 24250 call and the 23750 put, which are
    five strikes out on their own sides."""
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByPctOfSpot(0.01)),
                 LegSpec("PE", "SELL", select=sel.ByPctOfSpot(0.01)))
    assert set(_landed(spec).values()) == {5}


def test_a_point_offset_is_measured_from_spot(chain_lake):
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByStrikeOffset(200)))
    assert list(_landed(spec).values()) == [4]


def test_an_absolute_strike_is_found_by_number(chain_lake):
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByStrike(24300)))
    assert list(_landed(spec).values()) == [6]


def test_a_lower_delta_target_lands_further_out(chain_lake):
    """Delta is not in the lake — no vendor here serves it — so it is computed
    from the IV they do serve. The absolute level depends on the pricing model;
    the ordering must not."""
    near = _landed(_spec(LegSpec("CE", "SELL", select=sel.ByDelta(0.45))))
    far = _landed(_spec(LegSpec("CE", "SELL", select=sel.ByDelta(0.05))))
    assert list(far.values())[0] > list(near.values())[0]


# ---------------------------------------------------------------------------
# settling for the nearest available contract, and saying so
# ---------------------------------------------------------------------------

def test_a_target_past_the_chain_is_reported_as_clamped(chain_lake):
    """The ladder bottoms out at ₹5, so ₹1 is not in the data. The nearest
    contract is returned — it has to be — but the day is counted."""
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByPremium(1.0)))
    context = load_context(spec, "NIFTY", DAY, DAY)
    assert context.selection.clamped_days == 1
    assert context.selection.resolved == 1


def test_a_reachable_target_is_not_clamped(chain_lake):
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByPremium(50.0)))
    context = load_context(spec, "NIFTY", DAY, DAY)
    assert context.selection.clamped_days == 0


def test_clamping_is_counted_per_leg(chain_lake):
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByPremium(50.0)),
                 LegSpec("PE", "SELL", select=sel.ByPremium(9999.0)))
    context = load_context(spec, "NIFTY", DAY, DAY)
    per_leg = context.selection.per_leg
    reachable = [s for s in per_leg.values() if s["side"] == "CE"][0]
    unreachable = [s for s in per_leg.values() if s["side"] == "PE"][0]
    assert reachable["clamped"] == 0
    assert unreachable["clamped"] == 1


# ---------------------------------------------------------------------------
# guard rails
# ---------------------------------------------------------------------------

def test_only_moneyness_can_re_strike():
    """Re-striking follows a leg by moneyness at every minute, which is cheap
    only because the lake is physically indexed that way. Any other rule would
    need the whole chain every minute — the per-minute scan this engine exists
    to avoid — so asking for it is an error rather than a slow path."""
    with pytest.raises(ValueError, match="cannot re-strike"):
        LegSpec("CE", "SELL", restrike=True, select=sel.ByDelta(0.2))
    LegSpec("CE", "SELL", restrike=True, select=sel.ByMoneyness(0))   # allowed


def test_the_shorthand_and_the_selector_cannot_disagree():
    """`LegSpec("CE", "SELL", 3)` is shorthand, not a second source of truth."""
    assert LegSpec("CE", "SELL", 3).select == sel.ByMoneyness(3)
    assert LegSpec("CE", "SELL", select=sel.ByMoneyness(5)).moneyness == 5


def test_a_day_is_never_traded_a_leg_short(chain_lake):
    """A hedged position silently missing its hedge is the worst failure this
    could produce, so a leg that cannot be resolved drops the whole day — and
    says which leg, because 'no trades' otherwise reads identically to an
    unbackfilled date range and the two have opposite fixes."""
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByMoneyness(0)),
                 # Nothing at +40 exists in the lake at all.
                 LegSpec("CE", "BUY", select=sel.ByMoneyness(40)))
    result = run(spec, "NIFTY", DAY, DAY)
    assert result.trades == []
    assert "CE +40" in result.selection.note
    assert "-10 to +10" in result.selection.note


def test_an_unbackfilled_range_says_so_rather_than_blaming_the_legs(chain_lake):
    spec = _spec(LegSpec("CE", "SELL", select=sel.ByMoneyness(0)))
    result = run(spec, "NIFTY", date(2019, 1, 7), date(2019, 1, 11))
    assert result.trades == []
    assert "backfill" in result.selection.note


# ---------------------------------------------------------------------------
# a spec is a reproduction recipe
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("selector", [
    sel.ByMoneyness(-3), sel.ByPremium(120.0), sel.ByPremiumRatio(2, 0.25),
    sel.ByDelta(0.16), sel.ByPctOfSpot(0.015), sel.ByStrikeOffset(350.0),
    sel.ByStrike(24150.0),
])
def test_every_selector_survives_a_round_trip(selector):
    """`asdict` would flatten these to their fields and lose which rule they
    were — `ByDelta(0.2)` and `ByPremium(0.2)` land on disk identically. A
    saved run that cannot be re-run is not a record."""
    assert sel.from_dict(selector.to_dict()) == selector


def test_a_whole_spec_round_trips():
    spec = StrategySpec(
        name="ratio", legs=[
            LegSpec("CE", "SELL", select=sel.ByMoneyness(0)),
            LegSpec("CE", "BUY", lots=3, select=sel.ByPremiumRatio(0, 1 / 3)),
        ],
        entry_time=time(9, 30), exit_time=time(15, 0),
        stop_loss_pct=0.3, trail_stop=2000.0, per_leg_stop_pct=0.25,
        re_entries=2, weekdays=(1, 3), max_atm_iv=0.25)
    rebuilt = StrategySpec.from_dict(spec.to_dict())
    assert rebuilt.describe() == spec.describe()
    assert rebuilt.entry_time == spec.entry_time
    assert rebuilt.weekdays == (1, 3)
    assert rebuilt.trail_stop == 2000.0
    assert rebuilt.per_leg_stop_pct == 0.25
    assert rebuilt.max_atm_iv == 0.25


def test_a_legacy_spec_without_selectors_still_reproduces():
    """Six runs were saved before selectors existed. They carry `moneyness` and
    no `select`, and defaulting to at-the-money would silently re-run every
    stored condor as a straddle under the old name."""
    legacy = {"opt_type": "CE", "side": "BUY", "moneyness": 8, "lots": 2,
              "restrike": False}
    leg = LegSpec.from_dict(legacy)
    assert leg.select == sel.ByMoneyness(8)
    assert leg.lots == 2


# ---------------------------------------------------------------------------
# the leg grammar the CLI and the assistant both use
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("SELL CE 0", sel.ByMoneyness(0)),
    ("BUY PE 5", sel.ByMoneyness(5)),
    ("SELL CE -3", sel.ByMoneyness(-3)),
    ("SELL CE @120", sel.ByPremium(120.0)),
    ("BUY CE @atm/3", sel.ByPremiumRatio(0, 1 / 3)),
    ("BUY CE @atm*0.5", sel.ByPremiumRatio(0, 0.5)),
    ("BUY CE @+5/2", sel.ByPremiumRatio(5, 0.5)),
    ("SELL CE 20d", sel.ByDelta(0.20)),
    ("SELL CE 0.2delta", sel.ByDelta(0.20)),
    ("SELL PE 1%", sel.ByPctOfSpot(0.01)),
    ("SELL CE 200pt", sel.ByStrikeOffset(200.0)),
    ("BUY CE k23000", sel.ByStrike(23000.0)),
])
def test_legs_parse_the_way_the_rule_is_spoken(text, expected):
    from tools.backtest import parse_legs

    leg = parse_legs(text)[0]
    assert leg.select == expected


def test_lots_and_roll_still_parse():
    from tools.backtest import parse_legs

    legs = parse_legs("BUY CE @atm/3 x3, SELL PE 0 roll")
    assert legs[0].lots == 3
    assert legs[1].restrike is True


def test_an_unreadable_leg_raises_rather_than_being_skipped():
    """A silently dropped leg turns a hedged position into a naked one and
    still produces a plausible report."""
    from tools.backtest import parse_legs

    with pytest.raises(ValueError, match="cannot read"):
        parse_legs("SELL CE 0, SELL CE wibble")


def test_a_saved_spec_re_runs_to_the_same_rupee(chain_lake):
    """The guarantee that makes `spec.json` a record rather than a description.

    Six runs saved under the old moneyness-only engine were checked this way
    against the restructured one and reproduced exactly; anything that breaks
    this check has changed what a stored backtest means.
    """
    spec = _spec(
        LegSpec("CE", "SELL", select=sel.ByMoneyness(0)),
        LegSpec("PE", "SELL", select=sel.ByPremium(70.0)),
        LegSpec("CE", "BUY", lots=3, select=sel.ByPremiumRatio(0, 1 / 3)),
        stop_loss_pct=0.4, trail_stop=900.0, per_leg_stop_pct=0.3,
        per_leg_action="leg", re_entries=1)
    first = run(spec, "NIFTY", DAY, DAY)
    rebuilt = run(StrategySpec.from_dict(spec.to_dict()), "NIFTY", DAY, DAY)

    assert [t.pnl for t in first.trades] == [t.pnl for t in rebuilt.trades]
    assert [t.exit_reason for t in first.trades] == \
           [t.exit_reason for t in rebuilt.trades]
    assert [t.strikes for t in first.trades] == [t.strikes for t in rebuilt.trades]
