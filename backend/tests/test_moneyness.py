"""Moneyness derived from prices, and the put-side trap it exists to avoid.

Dhan addresses its expired-options endpoint with a label like `ATM+3` and does
not document whether that means "three strikes above spot" or "three strikes out
of the money". For a call those coincide. For a put they are opposite, and
choosing wrong mirrors the entire put side of a five-year dataset — while every
backtest still runs and every number still looks reasonable. That is the worst
class of bug this project can have, so the derivation never consults a label.
"""

from app.data import moneyness as mny

SPOT = 24_366.0
STEP = 50.0
# Nearest 50 to 24,366.
ATM = 24_350.0


# ---------------------------------------------------------------------------
# the sign convention: positive is always further OUT of the money
# ---------------------------------------------------------------------------

def test_at_the_money_is_zero_for_both_sides():
    assert mny.compute(ATM, SPOT, "CE", STEP) == 0
    assert mny.compute(ATM, SPOT, "PE", STEP) == 0


def test_a_call_above_spot_is_positively_out_of_the_money():
    assert mny.compute(ATM + 4 * STEP, SPOT, "CE", STEP) == 4


def test_a_put_below_spot_is_positively_out_of_the_money():
    """The line the whole module exists for. Below spot is OTM for a put."""
    assert mny.compute(ATM - 4 * STEP, SPOT, "PE", STEP) == 4


def test_in_the_money_is_negative_on_both_sides():
    assert mny.compute(ATM - 3 * STEP, SPOT, "CE", STEP) == -3
    assert mny.compute(ATM + 3 * STEP, SPOT, "PE", STEP) == -3


def test_a_strangle_is_symmetric_in_the_number():
    """Selling the 4-OTM call and the 4-OTM put both read as moneyness 4 —
    which is what lets LegSpec('CE','SELL',4) and LegSpec('PE','SELL',4)
    describe a symmetric strangle."""
    call = mny.compute(ATM + 4 * STEP, SPOT, "CE", STEP)
    put = mny.compute(ATM - 4 * STEP, SPOT, "PE", STEP)
    assert call == put == 4


def test_the_same_strike_reads_opposite_for_a_call_and_a_put():
    strike = ATM + 5 * STEP
    assert mny.compute(strike, SPOT, "CE", STEP) == 5
    assert mny.compute(strike, SPOT, "PE", STEP) == -5


# ---------------------------------------------------------------------------
# strike step inference
# ---------------------------------------------------------------------------

def test_step_is_inferred_from_the_strikes_present():
    assert mny.strike_step([24000, 24050, 24100, 24150, 24200]) == 50.0
    assert mny.strike_step([50000, 50100, 50200, 50300]) == 100.0


def test_a_few_odd_strikes_do_not_move_the_step():
    """Monthly chains list wider strikes alongside the weekly grid."""
    strikes = [24000, 24050, 24100, 24150, 24200, 24250, 24300, 25000, 26000]
    assert mny.strike_step(strikes) == 50.0


def test_too_few_strikes_falls_back_to_the_underlying_default():
    assert mny.strike_step([24000], "NIFTY") == 50.0
    assert mny.strike_step([], "BANKNIFTY") == 100.0
    assert mny.strike_step([], "MIDCPNIFTY") == 25.0


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def test_a_missing_spot_yields_none_rather_than_a_guess():
    """A wrong integer here is worse than a missing one — the backtest engine
    selects on it."""
    assert mny.compute(24_500.0, 0.0, "CE", STEP) is None
    assert mny.compute(0.0, SPOT, "CE", STEP) is None
    assert mny.compute(24_500.0, SPOT, "CE", 0.0) is None


def test_an_absurd_strike_is_rejected():
    assert mny.compute(1_000_000.0, SPOT, "CE", STEP) is None


# ---------------------------------------------------------------------------
# annotate
# ---------------------------------------------------------------------------

def _row(strike, opt_type, spot=SPOT):
    return {"strike": strike, "opt_type": opt_type, "spot": spot}


def test_annotate_fills_every_resolvable_row():
    rows = [_row(ATM, "CE"), _row(ATM + 100, "CE"), _row(ATM - 100, "PE")]
    assert mny.annotate(rows, "NIFTY") == 3
    assert [r["moneyness"] for r in rows] == [0, 2, 2]  # step=50 from NIFTY default


def test_annotate_leaves_unresolvable_rows_null(capsys):
    rows = [_row(ATM, "CE", spot=0.0), _row(ATM + 100, "CE")]
    assert mny.annotate(rows, "NIFTY") == 1
    assert rows[0]["moneyness"] is None
    assert rows[1]["moneyness"] == 2


# ---------------------------------------------------------------------------
# label cross-check
# ---------------------------------------------------------------------------

def test_label_offset_parses_the_vendor_form():
    assert mny.label_offset("ATM") == 0
    assert mny.label_offset("ATM+7") == 7
    assert mny.label_offset("ATM-3") == -3
    assert mny.label_offset("garbage") is None


def test_a_matching_label_reports_agreement():
    rows = [{"moneyness": 3} for _ in range(20)]
    assert mny.verify_label(rows, "ATM+3", "CE")["verdict"] == "agrees"


def test_a_mirrored_put_label_is_detected_not_silently_accepted():
    """If Dhan's ATM+3 means 'three strikes above spot', then for puts our
    derived moneyness is -3 while the label says +3. That must be reported."""
    rows = [{"moneyness": -3} for _ in range(20)]
    verdict = mny.verify_label(rows, "ATM+3", "PE")
    assert verdict["verdict"] == "vendor-uses-absolute-offset"
    assert verdict["flipped_pct"] == 100.0


def test_mixed_evidence_is_reported_as_unclear_rather_than_guessed():
    rows = [{"moneyness": 3} for _ in range(10)] + \
           [{"moneyness": -3} for _ in range(10)]
    assert mny.verify_label(rows, "ATM+3", "PE")["verdict"] == "unclear"


def test_no_derived_values_means_no_verdict():
    assert mny.verify_label([], "ATM+3", "CE")["verdict"] == "unknown"
    assert mny.verify_label([{"moneyness": None}], "ATM+3", "CE")["verdict"] == "unknown"
