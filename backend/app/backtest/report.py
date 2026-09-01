"""Turning a backtest into a report you can actually judge a strategy by.

`engine.run()` answers "what happened". This answers the questions that decide
whether a strategy is worth trading:

* **How much capital does it tie up?** A strategy making ₹50,000 on ₹1L of
  margin and one making ₹50,000 on ₹10L are not the same strategy. Without
  margin there is no return, only profit — and profit alone is unrankable.
* **How bad does it get, and for how long?** Peak-to-trough loss is only half
  of it. A 15% drawdown you sit in for three weeks is survivable; the same
  drawdown lasting nine months is the one that makes you switch strategies at
  the bottom.
* **Is the result spread across the sample, or carried by a handful of days?**
  Monthly and weekday breakdowns answer that, and they routinely reveal that an
  "edge" is one expiry-week effect wearing a costume.

The metric set deliberately mirrors what StockMock and AlgoTest report, so a
number here can be compared against a number there without translation. Where
this goes further, it is on purpose: charges are itemised and date-scoped, the
slippage assumption is stated rather than buried, and margin is modelled per
trade rather than quoted once.

## The margin model

Margin is computed at each trade's entry from the exchange-shaped SPAN estimate
in `quant/strategy.py` — worst-case scenario loss plus exposure on genuinely
uncovered short quantity, capped at max loss when the position is bounded. It
is an *estimate*, and it is the same estimate the live strategy builder uses,
which is the point: the number in a backtest report and the number on the
option-chain page cannot drift apart.

Two figures are reported and they answer different questions. **Peak margin** is
what the account must be able to post on the worst day — the capital you
actually need. **Median margin** is what is typically deployed. Return on peak is
the conservative figure; return on median flatters and is included because
comparison platforms quote something closer to it.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from app.backtest import montecarlo
from app.backtest.engine import (Result, StrategySpec, contract_label,
                                 time_to_expiry,
                                 underwater)
from app.quant import strategy as strat

TRADING_DAYS = 252
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")

# Dhan reports implied vol as a percentage (15.16 meaning 15.16%), while every
# pricing function here wants a decimal. Getting this wrong scales the SPAN
# scan range by a hundred and produces margins in the crores without erroring.
IV_IS_PERCENT = True

# When a bar carries no usable IV, fall back to this rather than dropping the
# trade from the margin series. Roughly NIFTY's long-run ATM level.
FALLBACK_IV = 0.13

# Matches the app's configured rate so a margin quoted in a report and one shown
# on the option-chain page are computed on the same basis.
RISK_FREE_RATE = float(__import__("os").getenv("TRADING_RISK_FREE_RATE", "0.07"))


@dataclass
class Margin:
    peak: float = 0.0
    median: float = 0.0
    average: float = 0.0
    minimum: float = 0.0
    per_lot_median: float = 0.0
    samples: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "peak": round(self.peak, 2), "median": round(self.median, 2),
            "average": round(self.average, 2), "minimum": round(self.minimum, 2),
            "per_lot_median": round(self.per_lot_median, 2),
            "samples": self.samples, "note": self.note,
        }


def _iv(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return FALLBACK_IV
    if not math.isfinite(value) or value <= 0:
        return FALLBACK_IV
    if IV_IS_PERCENT:
        value /= 100.0
    # A bar can carry a nonsense IV; clamp rather than let it drive the scan.
    return min(max(value, 0.02), 3.0)


def margin_series(result: Result, spec: StrategySpec,
                  columns: dict[str, np.ndarray],
                  expiry_by_day: dict[date, date] | None = None
                  ) -> tuple[Margin, list[float]]:
    """Margin at every trade's entry, using the live builder's own model.

    Runs per trade rather than once for the whole backtest because margin moves
    with spot, with vol, and with how far the strikes sat from the money on that
    particular day — quoting a single figure would hide exactly the days when
    the requirement spiked.
    """
    stamps = columns.get("ts")
    if stamps is None or not len(stamps) or not result.trades:
        return Margin(note="no trades"), []

    index = {int(t): i for i, t in enumerate(stamps.astype("datetime64[m]").astype(np.int64))}
    lots = max(sum(leg.lots for leg in spec.legs), 1)
    values: list[float] = []

    for trade in result.trades:
        key = int(np.datetime64(trade.entry_ts, "m").astype(np.int64))
        row = index.get(key)
        if row is None:
            continue
        spot = float(columns["spot"][row])
        if not math.isfinite(spot) or spot <= 0:
            continue

        legs = legs_at(trade, spec, columns, row, size=1,
                       expiry_by_day=expiry_by_day)
        if not legs:
            continue

        analysis = strat.analyse(legs, spot, RISK_FREE_RATE)
        total = (analysis.get("margin") or {}).get("total")
        if total and math.isfinite(total) and total > 0:
            values.append(float(total))

    if not values:
        return Margin(note="margin could not be estimated"), []

    peak = max(values)
    median = statistics.median(values)
    return Margin(
        peak=peak, median=median, average=sum(values) / len(values),
        minimum=min(values), per_lot_median=median / lots, samples=len(values),
        note="SPAN + exposure estimate, same model as the live builder",
    ), values


def row_at(trade: Any, columns: dict[str, np.ndarray]) -> int | None:
    """The matrix row a trade entered on."""
    stamps = columns.get("ts")
    if stamps is None or not len(stamps):
        return None
    index = {int(t): i for i, t in
             enumerate(stamps.astype("datetime64[m]").astype(np.int64))}
    return index.get(int(np.datetime64(trade.entry_ts, "m").astype(np.int64)))


def legs_at(trade: Any, spec: StrategySpec, columns: dict[str, np.ndarray],
            row: int, size: int = 1,
            expiry_by_day: dict[date, date] | None = None) -> list[Any]:
    """One trade's legs as priced positions, for the margin model.

    Pulled out of `margin_series` so a *portfolio* can concatenate the legs of
    several strategies and margin them as one book. That is the whole point of
    netting: the exchange sees one position per underlying, not one per
    strategy, and a short call under another strategy's long call is hedged
    whether or not you thought of them as related.
    """
    stamps = columns["ts"]
    legs = []
    for leg in spec.legs:
        price = float(columns[leg.column][row])
        strike = float(columns[f"{leg.column}_strike"][row])
        if not math.isfinite(price) or not math.isfinite(strike):
            return []
        sigma = _iv(columns.get(f"{leg.column}_iv", [None] * len(stamps))[row])
        legs.append(strat.Leg(
            instrument_type=leg.opt_type, strike=strike,
            quantity=leg.sign * leg.lots * size * spec.lot_size,
            entry_price=price,
            t_years=_t_years(spec, trade.day, expiry_by_day),
            iv=sigma, lot_size=spec.lot_size, mark_price=price,
        ))
    return legs


def _t_years(spec: StrategySpec, day: date,
             expiry_by_day: dict[date, date] | None = None) -> float:
    """Time to expiry. Shared with the engine so a delta used to *select* a
    strike and a margin used to size it are computed on the same clock.

    That sharing is the whole point, and it is why the expiry map has to reach
    this far: with a dated contract the engine resolves the exact remaining
    life, so a report still using the half-cycle approximation would margin a
    position the selector had already priced differently — on expiry day, by a
    factor of seven.
    """
    return time_to_expiry(spec.expiry_flag, day,
                          (expiry_by_day or {}).get(day))


def _streaks(pnls: list[float]) -> tuple[int, int]:
    best = worst = run_up = run_down = 0
    for value in pnls:
        if value > 0:
            run_up, run_down = run_up + 1, 0
        elif value < 0:
            run_down, run_up = run_down + 1, 0
        else:
            run_up = run_down = 0
        best, worst = max(best, run_up), max(worst, run_down)
    return best, worst


def _drawdown(equity: np.ndarray, days: list[date]) -> dict[str, Any]:
    """Depth, and — the part most reports omit — how long it lasted.

    Two durations are reported because they answer different questions. The
    *decline* is how long the loss took to accumulate; the *recovery* is how
    long you had to hold on before a new high. Traders abandon strategies during
    the second one, so it is the number that predicts whether a plan survives
    contact with its own worst stretch.
    """
    if not len(equity):
        return {}
    # The starting balance counts as a peak — see `engine.underwater`.
    peaks = np.maximum.accumulate(np.concatenate(([0.0], equity)))[1:]
    below = equity - peaks
    trough = int(np.argmin(below))
    depth = float(below[trough])

    # Where the decline began. When the peak was the starting balance rather
    # than any trade, that is trade zero — argmax would otherwise point at the
    # least-bad trade inside the decline and report a decline shorter than it
    # was.
    peak_at = 0
    if trough and peaks[trough] > 0:
        peak_at = int(np.argmax(equity[: trough + 1]))
    recovered = None
    for i in range(trough, len(equity)):
        if equity[i] >= peaks[trough]:
            recovered = i
            break

    def gap(a: int, b: int) -> int:
        return (days[b] - days[a]).days if 0 <= a < len(days) and 0 <= b < len(days) else 0

    return {
        "depth": round(depth, 2),
        "peak_trade": peak_at, "trough_trade": trough,
        "decline_trades": trough - peak_at,
        "decline_days": gap(peak_at, trough),
        "recovered": recovered is not None,
        "recovery_trades": (recovered - trough) if recovered is not None else None,
        "recovery_days": gap(trough, recovered) if recovered is not None else None,
        "still_underwater_trades": None if recovered is not None
                                   else len(equity) - 1 - trough,
    }


def _bucket(trades: list[Any], key, order: tuple[str, ...] | None = None
            ) -> list[dict[str, Any]]:
    """Group trades and summarise each group.

    `order` exists because the natural sort of a named bucket is wrong: sorting
    weekdays alphabetically puts Friday first and Wednesday last, which reads
    as a finding rather than as an artefact.
    """
    groups: dict[Any, list[float]] = {}
    for trade in trades:
        groups.setdefault(key(trade), []).append(trade.pnl)
    rank = ({name: i for i, name in enumerate(order)} if order else {})
    out = []
    for name in sorted(groups, key=lambda n: (rank.get(str(n), len(rank)), n)):
        values = groups[name]
        wins = [v for v in values if v > 0]
        out.append({
            "key": str(name),
            "trades": len(values),
            "pnl": round(sum(values), 2),
            "average": round(sum(values) / len(values), 2),
            "win_rate": round(100.0 * len(wins) / len(values), 1),
            "best": round(max(values), 2),
            "worst": round(min(values), 2),
        })
    return out


# A day is called an outlier when its P&L sits this many robust standard
# deviations from the median. Robust — median and MAD rather than mean and
# standard deviation — because a handful of huge days is exactly what we are
# looking for, and they drag a mean-based threshold out far enough to hide
# themselves. 5.0 is deliberately loose: on a fat-tailed short-premium series a
# 3-sigma rule flags a tenth of the sample and stops meaning anything.
OUTLIER_SIGMAS = 5.0

# Share of the held minutes whose basket price never moved. A liquid ATM
# straddle re-prices most minutes, so a high number here means the last traded
# price was being repeated and the exits on that stretch were decided against a
# stale quote. Not proof of a bad trade — a genuinely quiet expiry-day
# afternoon looks similar — which is why it is surfaced rather than filtered.
STALE_SHARE_PCT = 40.0

# Share of the held minutes where a leg did not print at all.
GAPPED_SHARE_PCT = 10.0


def _clock(stamp: Any) -> str:
    """HH:MM out of whatever the engine put in the trade.

    Timestamps arrive as numpy datetime64 from the matrix path and as plain
    datetimes from the positional one, and the two stringify differently.
    """
    if hasattr(stamp, "strftime"):
        return stamp.strftime("%H:%M")
    text = str(stamp)
    return text[11:16] if len(text) >= 16 else text


def _daily(trades: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every trading day as its own record, with what was wrong with it.

    The aggregate tables answer "did this strategy work". They cannot answer
    "why was 2024-06-04 worth ₹24,000", and that second question is where a
    backtest is usually found to be standing on bad data rather than on an
    edge. Each day therefore carries its own charges, its own MFE/MAE, the
    strikes it actually traded, and how much of its price path was stale or
    missing.

    Flags are heuristics and are named as such in the UI. They mark a day as
    worth opening, never as wrong.
    """
    pnls = np.array([t.pnl for t in trades], dtype=np.float64)
    median = float(np.median(pnls))
    # 1.4826 makes the MAD comparable to a standard deviation on normal data,
    # so the threshold can be read in the units people already think in.
    mad = float(np.median(np.abs(pnls - median))) * 1.4826
    # A MAD of zero means over half the days are identical to the rupee, which
    # a stop- or target-driven strategy genuinely produces — and it would
    # otherwise divide the threshold by nothing and flag no day at all, exactly
    # when a catastrophic one is sitting in the tail. Fall back to the standard
    # deviation, and only give up when every day really is the same number.
    spread = mad if mad > 0 else float(pnls.std())
    cutoff = OUTLIER_SIGMAS * spread if spread > 0 else None

    days: list[dict[str, Any]] = []
    for trade in trades:
        bars = trade.bars or 0
        stale_pct = round(100.0 * trade.flat_bars / bars, 1) if bars else 0.0
        missing_pct = round(100.0 * trade.missing_bars / bars, 1) if bars else 0.0
        deviation = (abs(trade.pnl - median) / spread) if spread > 0 else 0.0

        flags = []
        if cutoff is not None and abs(trade.pnl - median) > cutoff:
            flags.append("outlier")
        if stale_pct >= STALE_SHARE_PCT:
            flags.append("stale")
        if missing_pct >= GAPPED_SHARE_PCT:
            flags.append("gapped")
        if trade.truncated:
            flags.append("truncated")

        # `total` is dropped from the breakdown because `charges` already is
        # it, and the weekday because a date carries it — both of them 1,200
        # times over is a third of this block for nothing.
        breakdown = {k: v for k, v in trade.charges.to_dict().items()
                     if k != "total" and v}

        days.append({
            "day": trade.day.isoformat(),
            "entry": _clock(trade.entry_ts),
            "exit": _clock(trade.exit_ts),
            "entry_price": round(trade.entry_price, 2),
            "exit_price": round(trade.exit_price, 2),
            "gross": round(trade.gross, 2),
            "charges": round(trade.costs, 2),
            "charge_breakdown": breakdown,
            "pnl": round(trade.pnl, 2),
            "mfe": round(trade.max_profit, 2),
            "mae": round(trade.max_loss, 2),
            "exit_reason": trade.exit_reason,
            "strikes": {k: round(v) for k, v in trade.strikes.items()},
            "attempt": trade.attempt,
            "sessions_held": trade.sessions_held,
            "bars": bars,
            "stale_pct": stale_pct,
            "missing_pct": missing_pct,
            "deviation": round(deviation, 2),
            "flags": flags,
        })

    flagged = [d for d in days if d["flags"]]
    counts = {name: sum(1 for d in days if name in d["flags"])
              for name in ("outlier", "stale", "gapped", "truncated")}
    # What the flagged days are actually worth. A run whose whole profit is
    # three flagged days is a different object from one where they cancel out,
    # and that is the number worth leading with.
    flagged_pnl = round(sum(d["pnl"] for d in flagged), 2)
    total = float(pnls.sum())

    quality = dict(counts, **{
        "days": len(days),
        "flagged": len(flagged),
        "flagged_pnl": flagged_pnl,
        # Stated as "what is left without them" rather than as a share of the
        # net. A share is unreadable when the net is near zero — 39 days worth
        # −₹5.4 L against a −₹36.8 K net is a ratio of 1,477%, which sounds
        # like a rounding error rather than the point. The difference is the
        # point: without those days this strategy made half a million.
        "net": round(total, 2),
        "net_excluding_flagged": round(total - flagged_pnl, 2),
        "median_pnl": round(median, 2),
        "robust_sigma": round(spread, 2),
        "outlier_cutoff": round(cutoff, 2) if cutoff else None,
        "thresholds": {
            "outlier_sigmas": OUTLIER_SIGMAS,
            "stale_share_pct": STALE_SHARE_PCT,
            "gapped_share_pct": GAPPED_SHARE_PCT,
        },
        "note": ("Flags mark days worth opening, not days that are wrong. "
                 "A stale or gapped day means the price path under it was "
                 "thin, so that day's exit was decided against a quote that "
                 "may not have been live."),
    })
    return days, quality


def _expiries_used(spec: StrategySpec, underlying: str, start: date,
                   end: date) -> dict[date, date]:
    """Which expiry each session traded, from the engine's own cached load.

    Recomputing it here would be a second implementation of the ladder, and
    the two would eventually disagree about which contract a run was about.
    `load_context` is cached and was already called to produce this result, so
    this is a dictionary lookup rather than a query.
    """
    if spec.expiry_index is None:
        return {}
    try:
        from app.backtest.engine import load_context

        return load_context(spec, underlying, start, end).expiry_by_day
    except Exception:
        return {}


def build(result: Result, spec: StrategySpec, underlying: str,
          start: date, end: date,
          columns: dict[str, np.ndarray] | None = None) -> dict[str, Any]:
    """The full report for one backtest run."""
    trades = result.trades
    if not trades:
        return {"trades": 0, "note": result.stats.get("note", "no trades")}

    pnls = [t.pnl for t in trades]
    array = np.array(pnls, dtype=np.float64)
    equity = np.cumsum(array)
    days = [t.day for t in trades]
    wins = array[array > 0]
    losses = array[array < 0]

    margin, margin_values = (
        margin_series(result, spec, columns, _expiries_used(spec, underlying,
                                                            start, end))
        if columns else (Margin(note="not computed"), []))
    drawdown = _drawdown(equity, days)
    capital_floor = (margin.peak + abs(drawdown.get("depth") or 0.0)
                     if margin.peak else None)

    span_days = max((end - start).days, 1)
    years = span_days / 365.25
    total = float(array.sum())

    gross_loss = float(-losses.sum()) if losses.size else 0.0
    avg_win = float(wins.mean()) if wins.size else 0.0
    avg_loss = float(losses.mean()) if losses.size else 0.0
    # Expectancy per rupee risked, the form both comparison platforms quote.
    expectancy_ratio = (avg_win * (wins.size / len(trades))
                        + avg_loss * (losses.size / len(trades)))
    best_streak, worst_streak = _streaks(pnls)

    roi_peak = (total / margin.peak * 100.0) if margin.peak else None
    roi_median = (total / margin.median * 100.0) if margin.median else None
    # The figure a reader would actually experience: profit against the money
    # that has to sit in the account to survive the strategy's own worst run.
    # Return-on-margin is the number comparison platforms quote and it flatters
    # defined-risk structures enormously, so this leads the report instead.
    roi_capital = (total / capital_floor * 100.0) if capital_floor else None

    monthly = _bucket(trades, lambda t: f"{t.day.year:04d}-{t.day.month:02d}")
    monthly_pnls = [m["pnl"] for m in monthly]
    days_detail, quality = _daily(trades)

    return {
        "underlying": underlying,
        "series": contract_label(spec),
        "period": {"start": start.isoformat(), "end": end.isoformat(),
                   "years": round(years, 2), "sessions": len(trades)},
        "assumptions": {
            "lot_size": spec.lot_size,
            "lot_calendar": spec.lot_calendar,
            "lot_size_assumed_sessions": (result.skipped or {}).get(
                "lot_size_assumed", 0),
            # Summed lots across legs is the right divisor for per-lot margin,
            # but it reads as position size and is not — a one-lot straddle
            # sums to two. Both are reported so neither is mistaken for the
            # other.
            "legs": len(spec.legs),
            "lots_total": sum(leg.lots for leg in spec.legs),
            "lots_per_leg": sorted({leg.lots for leg in spec.legs}),
            "structure": spec.describe(),
            "slippage_points_per_leg_per_side": spec.slippage_points,
            "brokerage_per_order": spec.costs.brokerage_per_order,
            "entry_time": spec.entry_time.strftime("%H:%M"),
            "exit_time": spec.exit_time.strftime("%H:%M"),
            "stop_loss": spec.stop_loss, "target": spec.target,
            "stop_loss_pct": spec.stop_loss_pct, "target_pct": spec.target_pct,
            "trail_stop": spec.trail_stop, "trail_stop_pct": spec.trail_stop_pct,
            "trail_trigger": spec.trail_trigger,
            "trail_trigger_pct": spec.trail_trigger_pct,
            "breakeven_trigger": spec.breakeven_trigger,
            "breakeven_trigger_pct": spec.breakeven_trigger_pct,
            "per_leg_stop_pct": spec.per_leg_stop_pct,
            "per_leg_stop_points": spec.per_leg_stop_points,
            "per_leg_action": spec.per_leg_action,
            "re_entries": spec.re_entries,
            "re_entry_on": spec.re_entry_on,
            "hold_days": spec.hold_days,
            "sessions_held_avg": (
                round(sum(t.sessions_held for t in trades) / len(trades), 2)
                if spec.hold_days else 0),
            "truncated_at_expiry": sum(1 for t in trades if t.truncated),
            "adjust": (spec.adjust.to_dict() if spec.adjust else None),
            "adjust_rules": (spec.adjust.describe() if spec.adjust else []),
            "adjustments_total": sum(len(t.adjustments) for t in trades),
            "adjusted_trades": sum(1 for t in trades if t.adjustments),
            "became_straddle": sum(1 for t in trades if t.became_straddle),
            "wings_added": sum(1 for t in trades if t.wings_added),
            "filters": {k: v for k, v in (
                ("min_atm_iv", spec.min_atm_iv),
                ("max_atm_iv", spec.max_atm_iv),
                ("gap_pct_min", spec.gap_pct_min),
                ("gap_pct_max", spec.gap_pct_max),
                ("day_move_pct_min", spec.day_move_pct_min),
                ("day_move_pct_max", spec.day_move_pct_max),
            ) if v is not None},
            "weekdays": list(spec.weekdays) if spec.weekdays else None,
            "min_session_bars": spec.min_session_bars,
            "restrike_legs": [leg.column for leg in spec.legs if leg.restrike],
        },
        # How the selectors actually behaved: how often a target fell outside
        # the ±10 strikes the lake holds, and where a dynamic rule landed. A
        # premium- or delta-selected run is unreadable without this.
        "selection": result.selection.to_dict(),
        "skipped": result.skipped,
        "headline": {
            "net_pnl": round(total, 2),
            "gross_pnl": round(sum(t.gross for t in trades), 2),
            "total_charges": round(sum(t.costs for t in trades), 2),
            "roi_on_peak_margin_pct": round(roi_peak, 2) if roi_peak else None,
            "roi_on_median_margin_pct": round(roi_median, 2) if roi_median else None,
            "capital_floor": round(capital_floor, 2) if capital_floor else None,
            "roi_on_capital_pct": round(roi_capital, 2) if roi_capital else None,
            "cagr_on_capital_pct": _cagr(roi_capital, years),
            # Two different questions. Simple average is what comparison
            # platforms quote and assumes profits are withdrawn. CAGR assumes
            # they are left in and compounded, and is the smaller, honester
            # number over a multi-year sample: 499% across five years is 100%
            # a year simple but 43% compounded.
            "annualised_on_peak_pct": round(roi_peak / years, 2)
                                      if roi_peak and years else None,
            "cagr_on_peak_pct": _cagr(roi_peak, years),
        },
        "margin": dict(margin.to_dict(), **{
            # The number actually worth acting on. Margin is what the exchange
            # blocks per trade; it says nothing about surviving a losing run.
            # An account holding only the peak margin gets liquidated the first
            # time the strategy draws down, so the floor is margin plus the
            # worst cumulative loss the strategy has historically produced —
            # and history is not a bound, which is why this is a floor and not
            # a recommendation.
            "capital_floor": round(
                margin.peak + abs(drawdown.get("depth") or 0.0), 2)
            if margin.peak else None,
        }),
        "trade_stats": {
            "trades": len(trades),
            "wins": int(wins.size), "losses": int(losses.size),
            "win_rate_pct": round(100.0 * wins.size / len(trades), 1),
            "loss_rate_pct": round(100.0 * losses.size / len(trades), 1),
            "average": round(float(array.mean()), 2),
            "median": round(float(np.median(array)), 2),
            "average_win": round(avg_win, 2),
            "average_loss": round(avg_loss, 2),
            "largest_win": round(float(array.max()), 2),
            "largest_loss": round(float(array.min()), 2),
            "profit_factor": round(float(wins.sum()) / gross_loss, 2)
                             if gross_loss else None,
            "expectancy": round(expectancy_ratio, 2),
            "reward_to_risk": round(abs(avg_win / avg_loss), 2) if avg_loss else None,
            "max_win_streak": best_streak,
            "max_loss_streak": worst_streak,
            "avg_monthly_pnl": round(statistics.mean(monthly_pnls), 2)
                               if monthly_pnls else None,
        },
        "risk": {
            "max_drawdown": drawdown.get("depth"),
            "max_drawdown_pct_of_peak_margin":
                round(abs(drawdown.get("depth", 0)) / margin.peak * 100.0, 2)
                if margin.peak else None,
            "drawdown_decline_days": drawdown.get("decline_days"),
            "drawdown_decline_trades": drawdown.get("decline_trades"),
            "drawdown_recovered": drawdown.get("recovered"),
            "drawdown_recovery_days": drawdown.get("recovery_days"),
            "drawdown_recovery_trades": drawdown.get("recovery_trades"),
            "still_underwater_trades": drawdown.get("still_underwater_trades"),
            "return_to_max_drawdown": round(total / abs(drawdown["depth"]), 2)
                                      if drawdown.get("depth") else None,
            "sharpe": _sharpe(array),
            "sortino": _sortino(array),
        },
        "charges": result.stats.get("costs_breakdown", {}),
        "exit_reasons": result.stats.get("exit_reasons", {}),
        # Every session on its own, so a day that looks wrong can be opened
        # rather than inferred from a monthly total.
        "days": days_detail,
        "quality": quality,
        "monthly": monthly,
        "yearly": _bucket(trades, lambda t: t.day.year),
        "weekday": _bucket(trades, lambda t: WEEKDAY_NAMES[t.day.weekday()]
                           if t.day.weekday() < 5 else "Weekend",
                           order=WEEKDAY_NAMES + ("Weekend",)),
        # Seasonality: every January together, rather than chronologically.
        # Separates "March 2023 was bad" from "March is bad".
        "month_of_year": _bucket(trades, lambda t: MONTH_NAMES[t.day.month - 1],
                                 order=MONTH_NAMES),
        # What the historical drawdown could have been had the same trades
        # arrived in a different order. See montecarlo.py — the single realised
        # path is one sample, and capital sized off it is sized off one sample.
        "montecarlo": montecarlo.simulate(pnls, peak_margin=margin.peak or None),
        "curves": {
            "equity": [{"day": d.isoformat(), "equity": round(float(v), 2)}
                       for d, v in zip(days, equity)],
            "drawdown": [{"day": d.isoformat(), "drawdown": round(float(v), 2)}
                         for d, v in zip(days, underwater(equity))],
            "margin": [round(v, 2) for v in margin_values],
        },
        "engine": {
            "bars_scanned": result.bars_scanned,
            "elapsed_ms": round(result.elapsed_ms, 1),
        },
    }


def _cagr(total_return_pct: float | None, years: float) -> float | None:
    """Compound annual growth rate. Undefined once cumulative losses would
    wipe the capital out, which is itself the answer in that case."""
    if total_return_pct is None or years <= 0:
        return None
    growth = 1.0 + total_return_pct / 100.0
    if growth <= 0:
        return None
    return round((growth ** (1.0 / years) - 1.0) * 100.0, 2)


def _sharpe(array: np.ndarray) -> float | None:
    """Annualised on 252 sessions. One trade a day makes the per-trade series
    the daily series, which is what this assumes."""
    if array.size < 2 or array.std() == 0:
        return None
    return round(float(array.mean() / array.std() * math.sqrt(TRADING_DAYS)), 2)


def _sortino(array: np.ndarray) -> float | None:
    """Like Sharpe but penalising only downside deviation. Reported because a
    premium-selling curve is deliberately asymmetric — many small gains, rare
    large losses — and Sharpe punishes the upside variance it wants."""
    if array.size < 2:
        return None
    downside = array[array < 0]
    if downside.size < 2 or downside.std() == 0:
        return None
    return round(float(array.mean() / downside.std() * math.sqrt(TRADING_DAYS)), 2)


def correlate(reports: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Daily-P&L correlation between saved runs, aligned on trading day.

    Two strategies that each look good but move together are one bet at double
    size. This is the check that says whether a second strategy diversifies the
    book or just levers it.
    """
    names = sorted(reports)
    series = {}
    for name in names:
        series[name] = {row["day"]: row["equity"] for row in reports[name]}

    matrix: dict[str, dict[str, float | None]] = {}
    for a in names:
        matrix[a] = {}
        for b in names:
            shared = sorted(set(series[a]) & set(series[b]))
            if len(shared) < 3:
                matrix[a][b] = None
                continue
            # Correlate daily changes, not cumulative equity: two rising curves
            # correlate at ~1.0 whatever their day-to-day behaviour, which is
            # exactly the illusion this is meant to dispel.
            da = np.diff([series[a][d] for d in shared])
            db = np.diff([series[b][d] for d in shared])
            if da.std() == 0 or db.std() == 0:
                matrix[a][b] = None
            else:
                matrix[a][b] = round(float(np.corrcoef(da, db)[0, 1]), 3)
    return {"strategies": names, "matrix": matrix}


# ---------------------------------------------------------------------------
# the human-readable artifact
# ---------------------------------------------------------------------------

def _rupees(value: float | None, sign: bool = False) -> str:
    """Indian units, because these are rupees and 1,23,45,678 read as
    '1.23 Cr' is the form the number is actually thought about in."""
    if value is None:
        return "—"
    magnitude = abs(value)
    if magnitude >= 1e7:
        body = f"{magnitude / 1e7:,.2f} Cr"
    elif magnitude >= 1e5:
        body = f"{magnitude / 1e5:,.2f} L"
    else:
        body = f"{magnitude:,.0f}"
    body = "₹" + body
    if value < 0:
        return "−" + body
    return ("+" + body) if sign else body


def _pct(value: float | None, sign: bool = True) -> str:
    if value is None:
        return "—"
    body = f"{abs(value):,.2f}%"
    if value < 0:
        return "−" + body
    return ("+" + body) if sign else body


def to_markdown(report: dict[str, Any], run_id: str, name: str) -> str:
    """A report a non-technical reader can act on.

    Ordered by what actually decides whether to trade something: the verdict
    first, then capital and the worst stretch, then the statistics. Tables come
    last because nobody makes a decision from a monthly grid — they confirm one.
    """
    if not report.get("trades", 1):
        return f"# {run_id} — {name}\n\nNo trades. {report.get('note', '')}\n"

    head, stats = report["headline"], report["trade_stats"]
    risk, margin, period = report["risk"], report["margin"], report["period"]
    a = report["assumptions"]
    lines: list[str] = []

    lines.append(f"# {run_id} — {name}\n")
    sizes = a.get("lots_per_leg") or [1]
    size = (f"{sizes[0]} lot" if len(sizes) == 1 and sizes[0] == 1
            else f"{sizes[0]} lots" if len(sizes) == 1
            else f"{min(sizes)}–{max(sizes)} lots")
    lot = (f"lot size per session" if a.get("lot_calendar")
           else f"lot size {a['lot_size']}")
    lines.append(
        f"**{report['underlying']} {report['series'].lower()}** · "
        f"`{a.get('structure', '')}` · {size} per leg "
        f"({lot}) · {period['start']} → {period['end']} · "
        f"{period['sessions']:,} sessions over {period['years']} years\n")

    # A run sized by assumption for most of its length is not comparable with
    # one that was not, and the difference is invisible in the P&L. Said here,
    # at the top, rather than in a table nobody reaches.
    assumed = a.get("lot_size_assumed_sessions") or 0
    if a.get("lot_calendar") and assumed:
        share = 100.0 * assumed / max(period["sessions"], 1)
        lines.append(
            f"> **{assumed:,} of {period['sessions']:,} sessions ({share:.0f}%) "
            f"used the fallback lot size of {a['lot_size']}** — the recorded "
            f"calendar does not reach back that far. Those sessions carry the "
            f"same error this setting exists to remove.\n")

    # ---- verdict -----------------------------------------------------------
    net, gross, charges = head["net_pnl"], head["gross_pnl"], head["total_charges"]
    lines.append("## Verdict\n")
    if net > 0 and head.get("roi_on_capital_pct") is not None:
        lines.append(
            f"Made **{_rupees(net, sign=True)}** over {period['years']} years. "
            f"Against the **{_rupees(head['capital_floor'])}** an account would "
            f"have needed to survive this strategy's own worst run, that is "
            f"**{_pct(head['roi_on_capital_pct'])}** in total — "
            f"**{_pct(head.get('cagr_on_capital_pct'))} a year compounded**.\n")
    elif net > 0:
        lines.append(
            f"Made **{_rupees(net, sign=True)}** over {period['years']} years.\n")
    else:
        lines.append(
            f"**Lost {_rupees(abs(net))}** over {period['years']} years"
            + (f" against {_rupees(head['capital_floor'])} of capital"
               if head.get("capital_floor") else "") + ".\n")
    if gross > 0 and net <= 0:
        lines.append(
            f"> The strategy itself was profitable — **{_rupees(gross, sign=True)} "
            f"before costs** — but charges of {_rupees(charges)} took it negative. "
            f"This is a cost problem, not a signal problem.\n")
    elif gross > 0:
        lines.append(
            f"> Charges consumed {charges / gross * 100:.0f}% of the gross profit "
            f"({_rupees(charges)} of {_rupees(gross)}).\n")

    if not risk.get("drawdown_recovered"):
        lines.append(
            f"> ⚠️ The worst drawdown of **{_rupees(risk['max_drawdown'])}** was "
            f"**never recovered** — the curve was still below its old high "
            f"{risk['still_underwater_trades']} trades later.\n")

    selection = report.get("selection") or {}
    clamped = selection.get("clamped_days") or 0
    if clamped:
        share = clamped / max(selection.get("resolved") or 1, 1) * 100
        lines.append(
            f"> ⚠️ On **{clamped} of {selection.get('resolved')} days "
            f"({share:.0f}%)** the contract this strategy asked for was outside "
            f"the ±10 strikes the data holds, and the nearest available one was "
            f"used instead. To that extent this is a result about a different "
            f"strategy — see *How the strikes were chosen*.\n")

    # ---- capital -----------------------------------------------------------
    lines.append("\n## Capital and return\n")
    lines.append("| | |\n|---|---|")
    lines.append(f"| Net P&L | **{_rupees(net, sign=True)}** |")
    lines.append(f"| Gross, before charges | {_rupees(gross, sign=True)} |")
    lines.append(f"| Total charges | {_rupees(charges)} |")
    lines.append(f"| Peak margin needed | **{_rupees(margin['peak'])}** |")
    lines.append(f"| Typical margin (median) | {_rupees(margin['median'])} |")
    lines.append(f"| Margin per lot (median) | {_rupees(margin['per_lot_median'])} |")
    if margin.get("capital_floor"):
        lines.append(f"| **Capital you would actually need** | "
                     f"**{_rupees(margin['capital_floor'])}** |")
    if head.get("roi_on_capital_pct") is not None:
        lines.append(f"| **Return on that capital** | "
                     f"**{_pct(head['roi_on_capital_pct'])}** |")
        lines.append(f"| **Per year, compounded** | "
                     f"**{_pct(head.get('cagr_on_capital_pct'))}** |")
    lines.append(f"| Return on peak margin *(flattering)* | "
                 f"{_pct(head['roi_on_peak_margin_pct'])} |")
    lines.append(f"| Per year on margin, simple | {_pct(head['annualised_on_peak_pct'])} |")
    lines.append(f"| Per year on margin, compounded | {_pct(head.get('cagr_on_peak_pct'))} |")
    lines.append(
        "\n*Peak margin is what the account must be able to post on the worst "
        "day — the capital actually required. Median is what is typically "
        "deployed. Both are SPAN + exposure estimates from the same model the "
        "live option-chain page uses.*\n")
    if margin.get("capital_floor") and margin.get("peak"):
        multiple = margin["capital_floor"] / margin["peak"]
        lines.append(
            f"\n> **Capital needed is {multiple:.1f}x the margin.** Margin is "
            f"what the exchange blocks per trade; it says nothing about "
            f"surviving a losing run. An account holding only "
            f"{_rupees(margin['peak'])} would have been wiped out by this "
            f"strategy's own worst stretch. The figure above is margin plus "
            f"the deepest cumulative loss it actually produced — a floor, not "
            f"a recommendation, since history is not a bound.\n")
    lines.append(
        "> **Read return-on-margin carefully.** It is not return on your "
        "account. Nobody trades at 100% margin utilisation — a real account "
        "holds a buffer for adverse moves, so the return on the money you "
        "actually set aside is materially lower than the figure above. "
        "Defined-risk structures look spectacular on this measure precisely "
        "because their margin is small; that is a real advantage, but it is "
        "not the same as making that percentage on your capital.\n")

    # ---- risk --------------------------------------------------------------
    lines.append("\n## The worst it got\n")
    lines.append("| | |\n|---|---|")
    lines.append(f"| Max drawdown | **{_rupees(risk['max_drawdown'])}** |")
    lines.append(f"| As % of peak margin | {_pct(risk['max_drawdown_pct_of_peak_margin'], sign=False)} |")
    lines.append(f"| Took this long to fall | {risk['drawdown_decline_days']} days "
                 f"({risk['drawdown_decline_trades']} trades) |")
    if risk.get("drawdown_recovered"):
        lines.append(f"| Recovered after | {risk['drawdown_recovery_days']} days "
                     f"({risk['drawdown_recovery_trades']} trades) |")
    else:
        lines.append(f"| Recovered | **no — still underwater at the end** |")
    lines.append(f"| Return ÷ max drawdown | {risk['return_to_max_drawdown']} |")
    lines.append(f"| Worst losing streak | {stats['max_loss_streak']} trades in a row |")
    lines.append(f"| Best winning streak | {stats['max_win_streak']} trades in a row |")
    lines.append(
        "\n*Recovery time matters more than depth. A drawdown you sit in for a "
        "year is the one that makes people abandon a working strategy at the "
        "bottom.*\n")

    # ---- what else could have happened -------------------------------------
    mc = report.get("montecarlo") or {}
    if mc.get("paths"):
        shuffled = mc["shuffled_drawdown"]
        lines.append("\n## What else could have happened\n")
        lines.append(
            f"The drawdown above is one draw. Re-dealing the same "
            f"{mc['trades']:,} trades in {mc['paths']:,} different orders — same "
            f"trades, same total profit, only the sequence changed:\n")
        lines.append("| | Max drawdown |\n|---|---|")
        lines.append(f"| Best 5% of orderings | {_rupees(shuffled['p95'])} |")
        lines.append(f"| Typical (median) | {_rupees(shuffled['p50'])} |")
        lines.append(f"| **What actually happened** | "
                     f"**{_rupees(mc['actual']['max_drawdown'])}** |")
        lines.append(f"| **Worst 5% of orderings** | "
                     f"**{_rupees(shuffled['p5'])}** |")
        if mc.get("capital_floor_p95"):
            lines.append(f"| Capital needed at the deeper of the two | "
                         f"**{_rupees(mc['capital_floor_p95'])}** |")
        versus_worst = mc.get("drawdown_vs_worst_ordering")
        versus_typical = mc.get("drawdown_vs_typical_ordering")
        if mc.get("losses_cluster") and versus_typical:
            lines.append(
                f"\n> ⚠️ **The losses arrived in clusters.** The drawdown that "
                f"actually happened is **{versus_typical:.1f}x deeper than a "
                f"typical reshuffle** and {versus_worst:.1f}x deeper than the "
                f"worst of {mc['paths']:,} of them — reshuffling cannot produce "
                f"it at all. That is what a losing streak concentrated in a few "
                f"volatile weeks looks like, and it means the resampled figures "
                f"below are a floor on the risk rather than a bound on it.\n")
        elif versus_worst and versus_worst <= 0.67:
            lines.append(
                f"\n> **The realised drawdown was on the lucky side.** A bad "
                f"ordering of these same trades is {1 / versus_worst:.1f}x "
                f"deeper. Size the account off that figure, not off the one "
                f"history happened to deal.\n")
        lines.append(
            f"\nResampling the trades *with replacement* — varying the outcome "
            f"as well as the order — **{mc['probability_of_loss_pct']}%** of "
            f"paths ended in a loss, and the middle half of them landed between "
            f"{_rupees(mc['bootstrap_total']['p25'], sign=True)} and "
            f"{_rupees(mc['bootstrap_total']['p75'], sign=True)}.\n")
        lines.append(
            "\n*Neither figure is a forecast. Both rearrange this strategy's "
            "own history, and they assume trades are independent — option "
            "selling losses cluster in volatile weeks, so real tail risk is "
            "worse than the resampling shows.*\n")

    # ---- trade statistics --------------------------------------------------
    lines.append("\n## Trade statistics\n")
    lines.append("| | |\n|---|---|")
    lines.append(f"| Trades | {stats['trades']:,} |")
    lines.append(f"| Win rate | {stats['win_rate_pct']}% "
                 f"({stats['wins']:,} win / {stats['losses']:,} lose) |")
    lines.append(f"| Average trade | {_rupees(stats['average'], sign=True)} |")
    lines.append(f"| Median trade | {_rupees(stats['median'], sign=True)} |")
    lines.append(f"| Average win | {_rupees(stats['average_win'], sign=True)} |")
    lines.append(f"| Average loss | {_rupees(stats['average_loss'], sign=True)} |")
    lines.append(f"| Best day | {_rupees(stats['largest_win'], sign=True)} |")
    lines.append(f"| Worst day | {_rupees(stats['largest_loss'], sign=True)} |")
    lines.append(f"| Reward : risk | {stats['reward_to_risk']} |")
    lines.append(f"| Profit factor | {stats['profit_factor']} |")
    lines.append(f"| Expectancy per trade | {_rupees(stats['expectancy'], sign=True)} |")
    lines.append(f"| Average month | {_rupees(stats['avg_monthly_pnl'], sign=True)} |")
    lines.append(f"| Sharpe / Sortino | {risk['sharpe']} / {risk['sortino']} |")

    if stats["win_rate_pct"] >= 55 and stats["reward_to_risk"] and stats["reward_to_risk"] < 1:
        lines.append(
            f"\n*Wins {stats['win_rate_pct']}% of the time but the average loss is "
            f"bigger than the average win — the classic premium-selling shape. A "
            f"high win rate alone says nothing; the two must be read together.*\n")

    # ---- charges -----------------------------------------------------------
    charges_detail = report.get("charges") or {}
    if charges_detail:
        lines.append("\n## Where the charges went\n")
        lines.append("| Charge | Amount | Share |\n|---|---|---|")
        total = charges_detail.get("total") or 1
        for key, label in (("brokerage", "Brokerage"), ("stt", "STT"),
                           ("exchange", "Exchange transaction"),
                           ("sebi", "SEBI turnover"), ("stamp", "Stamp duty"),
                           ("gst", "GST")):
            value = charges_detail.get(key, 0)
            lines.append(f"| {label} | {_rupees(value)} | {value / total * 100:.0f}% |")
        lines.append(f"| **Total** | **{_rupees(charges_detail.get('total'))}** | |")

    # ---- assumptions -------------------------------------------------------
    lines.append("\n## What was assumed\n")
    lines.append("| | |\n|---|---|")
    lines.append(f"| Entry / exit | {a['entry_time']} → {a['exit_time']} |")
    lines.append(f"| Slippage | {a['slippage_points_per_leg_per_side']} points "
                 f"per leg per side |")
    lines.append(f"| Brokerage | ₹{a['brokerage_per_order']:.0f} per executed order |")
    lines.append(f"| Statutory charges | STT, exchange, SEBI, stamp duty and GST, "
                 f"**at the rates in force on each trade's own date** |")
    stop = a["stop_loss"] or (f"{a['stop_loss_pct'] * 100:.0f}% of credit"
                              if a["stop_loss_pct"] else None)
    target = a["target"] or (f"{a['target_pct'] * 100:.0f}% of credit"
                             if a["target_pct"] else None)
    lines.append(f"| Stop loss | {_rupees(stop) if isinstance(stop, (int, float)) else (stop or 'none')} |")
    lines.append(f"| Target | {_rupees(target) if isinstance(target, (int, float)) else (target or 'none')} |")

    trail = a.get("trail_stop") or (f"{a['trail_stop_pct'] * 100:.0f}% of credit"
                                    if a.get("trail_stop_pct") else None)
    if trail:
        trigger = a.get("trail_trigger") or (
            f"{a['trail_trigger_pct'] * 100:.0f}% of credit"
            if a.get("trail_trigger_pct") else None)
        body = _rupees(trail) if isinstance(trail, (int, float)) else trail
        lines.append(f"| Trailing stop | give back {body}"
                     + (f", armed once profit reaches "
                        f"{_rupees(trigger) if isinstance(trigger, (int, float)) else trigger}"
                        if trigger else ", armed immediately") + " |")
    breakeven = a.get("breakeven_trigger") or (
        f"{a['breakeven_trigger_pct'] * 100:.0f}% of credit"
        if a.get("breakeven_trigger_pct") else None)
    if breakeven:
        body = (_rupees(breakeven) if isinstance(breakeven, (int, float))
                else breakeven)
        lines.append(f"| Move to breakeven | once profit reaches {body} |")
    if a.get("per_leg_stop_pct") or a.get("per_leg_stop_points"):
        rule = (f"{a['per_leg_stop_pct'] * 100:.0f}% of that leg's entry premium"
                if a.get("per_leg_stop_pct")
                else f"{a['per_leg_stop_points']:g} points")
        action = ("close only that leg and let the rest run"
                  if a.get("per_leg_action") == "leg"
                  else "close the whole position")
        lines.append(f"| Per-leg stop | {rule} — then {action} |")
    if a.get("hold_days"):
        truncated = a.get("truncated_at_expiry") or 0
        body = (f"{a['hold_days']} sessions, averaging "
                f"{a.get('sessions_held_avg')} — **positions are held "
                f"overnight**")
        if truncated:
            body += (f". {truncated} were cut short by a contract roll: a hold "
                     f"never crosses an expiry, because the lake stores a "
                     f"rolling series and the same strike on the far side is a "
                     f"different contract")
        lines.append(f"| Held for | {body} |")
        lines.append("| Overnight gaps | **realised at the next session's "
                     "first bar** — a stop cannot fire while the market is "
                     "shut, so held positions carry gap risk no intraday "
                     "strategy has |")
    if a.get("re_entries"):
        lines.append(f"| Re-entry | up to {a['re_entries']} more times per day "
                     f"after a {a.get('re_entry_on', 'stop')}, **into the same "
                     f"strikes** |")
    filters = a.get("filters") or {}
    if filters:
        lines.append("| Only entered when | "
                     + "; ".join(f"{k.replace('_', ' ')} {v:g}"
                                 for k, v in filters.items()) + " |")
    if a.get("weekdays"):
        names = ", ".join(WEEKDAY_NAMES[d] for d in a["weekdays"] if d < 5)
        lines.append(f"| Days traded | {names} |")
    if a["restrike_legs"]:
        lines.append(f"| Re-striking legs | {', '.join(a['restrike_legs'])} "
                     f"— **these follow the money instead of holding the strike entered** |")
    if a.get("adjust_rules"):
        lines.append("| Adjusted | " + "; ".join(a["adjust_rules"]) + " |")
    reasons = report.get("exit_reasons") or {}
    closed = ", ".join(f"{count} {reason}"
                       for reason, count in reasons.items() if count)
    lines.append(f"| Closed by | {closed or '—'} |")
    skipped = report.get("skipped") or {}
    if skipped.get("filtered"):
        lines.append(f"| Days skipped by the entry filter | "
                     f"{skipped['filtered']} |")

    # ---- what the repair rules actually did --------------------------------
    if a.get("adjust_rules"):
        total = a.get("adjustments_total") or 0
        touched = a.get("adjusted_trades") or 0
        trades_n = (report.get("trade_stats") or {}).get("trades") or 0
        lines.append("\n## How the position was repaired\n")
        lines.append("| | |\n|---|---|")
        lines.append(f"| Adjustments made | **{total:,}** |")
        lines.append(f"| Trades that adjusted at all | {touched} of "
                     f"{trades_n} |")
        if touched:
            lines.append(f"| Adjustments per adjusted trade | "
                         f"{total / touched:.1f} |")
        lines.append(f"| Collapsed into a straddle | "
                     f"{a.get('became_straddle') or 0} |")
        if a.get("adjust", {}).get("wings"):
            lines.append(f"| Capped with wings | {a.get('wings_added') or 0} |")
        lines.append(
            "\n*Every repair is a round trip on one leg, so the brokerage line "
            "above scales with this count rather than with the number of "
            "trades. An adjusted strategy that looks worse than the version "
            "that leaves the position alone is usually paying for the repairs "
            "rather than being wrong about the market — compare the gross "
            "figures before concluding either way.*\n")

    # ---- how the contracts were chosen -------------------------------------
    selection = report.get("selection") or {}
    per_leg = selection.get("per_leg") or {}
    if per_leg:
        dynamic = any(not stat["rule"].endswith(("ATM", "OTM", "ITM"))
                      for stat in per_leg.values())
        if dynamic or selection.get("clamped_days"):
            lines.append("\n## How the strikes were chosen\n")
            lines.append("| Leg | Rule | Landed on | Outside the data |"
                         "\n|---|---|---|---|")
            for column, stat in per_leg.items():
                if stat.get("moneyness_min") is None:
                    landed = "—"
                elif stat["moneyness_min"] == stat["moneyness_max"]:
                    landed = f"{stat['moneyness_min']:+d}"
                else:
                    landed = (f"{stat['moneyness_min']:+d} to "
                              f"{stat['moneyness_max']:+d}, "
                              f"usually {stat['moneyness_median']:+d}")
                miss = stat.get("clamped", 0)
                share = (f"{miss} days ({miss / stat['resolved'] * 100:.0f}%)"
                         if miss and stat.get("resolved") else "never")
                lines.append(f"| {stat['side']} | {stat['rule']} | {landed} "
                             f"| {share} |")
            lines.append(
                "\n*'Landed on' is where the rule put the leg, in strikes from "
                "at-the-money. A rule that ranges widely is doing its job — "
                "that variation is the reason it was written as a rule rather "
                "than as a fixed strike. 'Outside the data' counts the days the "
                "target sat beyond the ±10 strikes the lake holds, where the "
                "nearest available contract was substituted.*\n")
        if selection.get("unresolved_days"):
            lines.append(
                f"\n{selection['unresolved_days']} of {selection.get('days')} "
                f"days were dropped because at least one leg could not be "
                f"resolved at the entry minute. A day is never traded a leg "
                f"short.\n")

    # ---- breakdowns --------------------------------------------------------
    def table(title: str, rows: list[dict[str, Any]], note: str = "") -> None:
        if not rows:
            return
        lines.append(f"\n## {title}\n")
        lines.append("| | Trades | P&L | Average | Win rate |\n|---|---|---|---|---|")
        for row in rows:
            lines.append(f"| {row['key']} | {row['trades']} | "
                         f"{_rupees(row['pnl'], sign=True)} | "
                         f"{_rupees(row['average'], sign=True)} | {row['win_rate']}% |")
        if note:
            lines.append(f"\n*{note}*\n")

    table("By weekday", report.get("weekday", []),
          "A result carried by one weekday is usually an expiry-cycle effect "
          "rather than an edge.")
    table("By year", report.get("yearly", []),
          "Consistency across years matters more than the total. One "
          "exceptional year hiding four flat ones is not a strategy.")
    table("By month of the year", report.get("month_of_year", []),
          "Every January together, every February together. This separates "
          "'March 2023 was bad' from 'March is bad' — the second is a "
          "seasonality claim and needs more than five samples to make.")

    monthly = report.get("monthly", [])
    if monthly:
        wins = sum(1 for m in monthly if m["pnl"] > 0)
        lines.append(f"\n## By month\n")
        lines.append(f"{wins} of {len(monthly)} months were profitable "
                     f"({wins / len(monthly) * 100:.0f}%).\n")
        lines.append("| Month | Trades | P&L |\n|---|---|---|")
        for row in monthly:
            lines.append(f"| {row['key']} | {row['trades']} | "
                         f"{_rupees(row['pnl'], sign=True)} |")

    engine = report.get("engine", {})
    lines.append(f"\n---\n\n*Run over {engine.get('bars_scanned', 0):,} bars in "
                 f"{engine.get('elapsed_ms', 0):.0f} ms. Margin, charges and "
                 f"slippage are modelled estimates — see `BACKTESTING.md` for "
                 f"what the data can and cannot support.*\n")

    return "\n".join(lines) + "\n"
