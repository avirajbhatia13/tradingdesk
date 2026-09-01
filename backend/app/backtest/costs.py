"""What a trade actually costs, on the date it was actually traded.

Before this module the engine charged a flat `₹20 × legs × 2` and nothing else.
That is wrong in a specific and dangerous way: **brokerage is flat, and every
other charge is a percentage.** Modelling only the flat part means the modelled
cost of a trade does not move when you double the size, while the real cost
does. A strategy therefore looks *better the larger you size it*, which is
exactly backwards and exactly the direction that hurts when the backtest becomes
real money.

For an index option seller the missing piece is mostly STT, which is levied on
the **sell side of the premium** — i.e. on the credit you collect, at the moment
you collect it. It is the single largest non-brokerage cost in a short-premium
book.

## Rates change, and our lake spans the changes

The lake starts in August 2021. Between then and now the statutory rates moved
twice, both times sharply upwards:

* **1 October 2024** — Budget 2024 raised options STT from 0.0625% to 0.10%,
  and NSE replaced its volume-slab transaction charges with SEBI's uniform
  ₹35.03 per lakh of premium.
* **1 April 2026** — options STT raised again from 0.10% to 0.15%, and exercise
  STT from 0.125% to 0.15%.

So a backtest that spans 2021-2026 crosses two rate regimes. Applying today's
rates to a 2022 trade overstates its cost by roughly a third of the STT line;
applying 2022's rates to a trade today understates it by more. Neither is
acceptable in a model whose whole purpose is to stop flattering the strategy, so
`rates_for()` resolves the schedule in force on the trade date and every
calculation takes a date.

This also means a backtest's cost line is *not* a constant you can reason about
in your head across the whole window. That is a property of reality, not a
defect of the model.

## The exercise-STT trap, and the fact that it has largely closed

A long option left to expire in the money is charged STT on its **intrinsic
value** at a rate of its own, separate from the sell-side premium rate.

The received wisdom is that letting an ITM option expire is far more expensive
than squaring it off, and for most of this lake's history that was true — but
only because the two rates differed, and by how much. Comparing the two exits
for a deep ITM long call (200 points intrinsic, 202 premium, one lot):

| Regime | Square off | Let it expire | |
|---|---|---|---|
| before Oct 2024 | ₹9.47 | ₹18.75 | **1.98x worse** |
| Oct 2024 – Mar 2026 | ₹15.15 | ₹18.75 | 1.24x worse |
| from Apr 2026 | ₹22.73 | ₹22.50 | **0.99x — marginally cheaper** |

Budget 2026 raised the premium rate to 0.15% and the exercise rate to the same
0.15%, which all but erased the gap; intrinsic is slightly smaller than premium,
so expiry now comes out fractionally ahead, and saves a squaring-off brokerage
charge on top.

Both facts matter and for different reasons. A backtest over the older data must
model the trap or it will overstate the profit of any strategy that held to
expiry back then. A strategy running *today* should not be built around avoiding
something that is no longer true. This is exactly why the rates are date-scoped
rather than constants — the advice inverted inside our own sample.

Note who pays: exercise STT falls on the **buyer** who exercises, so a short leg
that expires ITM and is assigned does not attract it. Short legs pay their STT
up front on the sell, which they have already done at entry. NIFTY options are
cash settled, so there is no physical-delivery obligation layered on top.

## Sources

Rates below are transcribed from Zerodha's published charge list and the NSE
circulars implementing SEBI's uniform-charge directive, checked August 2026:

* https://zerodha.com/charges/
* https://zerodha.com/z-connect/business-updates/revision-in-exchange-transaction-charges-and-securities-transaction-tax-from-october-1-2024

Rates are policy, not physics — they change at budgets. `SCHEDULES` is a list
precisely so that adding the next change is one entry and no logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Rates:
    """The statutory charge schedule in force from `effective` onwards.

    Every rate is a fraction, not a percentage: 0.0015 is 0.15%. Storing
    percentages is how you get a hundred-fold error that still looks plausible
    on a small trade.
    """

    effective: date
    # On the sell side only, applied to premium turnover.
    stt_sell_premium: float
    # On intrinsic value, paid by a buyer who exercises an ITM option.
    stt_exercise_intrinsic: float
    # Both sides, on premium turnover.
    exchange_txn: float
    # Both sides, on premium turnover.
    sebi_turnover: float
    # Buy side only, on premium turnover.
    stamp_buy: float
    # On (brokerage + exchange + SEBI) — never on STT or stamp duty.
    gst: float
    note: str = ""


# Ordered oldest first. `rates_for` picks the last one whose `effective` has
# arrived, so extending history backwards or forwards is a single insert.
SCHEDULES: tuple[Rates, ...] = (
    Rates(
        effective=date(2020, 7, 1),
        stt_sell_premium=0.000625,          # 0.0625%
        stt_exercise_intrinsic=0.00125,     # 0.125%
        # Pre-uniform-charge NSE slab. Zerodha's applicable rate was 0.05% of
        # premium; other brokers sat between 0.0295% and 0.0495% depending on
        # volume, which is the spread SEBI's July 2024 directive removed.
        exchange_txn=0.0005,                # 0.05%
        sebi_turnover=0.000001,             # ₹10 per crore
        stamp_buy=0.00003,                  # 0.003%, from 1 July 2020
        gst=0.18,
        note="pre-Budget-2024 regime",
    ),
    Rates(
        effective=date(2024, 10, 1),
        stt_sell_premium=0.001,             # 0.10% — Budget 2024
        stt_exercise_intrinsic=0.00125,     # unchanged at 0.125%
        exchange_txn=0.0003503,             # ₹35.03 per lakh of premium
        sebi_turnover=0.000001,
        stamp_buy=0.00003,
        gst=0.18,
        note="Budget 2024 STT hike + SEBI uniform exchange charges",
    ),
    Rates(
        effective=date(2026, 4, 1),
        stt_sell_premium=0.0015,            # 0.15% — Budget 2026
        stt_exercise_intrinsic=0.0015,      # 0.15%
        # Zerodha's current published rate is 0.03553%, up from the 0.03503%
        # introduced in October 2024. The exact date of that small revision is
        # not confirmed, so it is attached to this boundary. The difference is
        # 0.0005% of premium turnover — around ₹0.15 on a ₹30,000 round trip —
        # so the imprecision is immaterial, but it is imprecision and should
        # not be presented otherwise.
        exchange_txn=0.0003553,
        sebi_turnover=0.000001,
        stamp_buy=0.00003,
        gst=0.18,
        note="Budget 2026 STT hike",
    ),
)


def rates_for(day: date) -> Rates:
    """The schedule in force on `day`.

    Dates before the earliest schedule get the earliest schedule rather than an
    error: the lake could be extended backwards by a vendor with deeper history,
    and silently refusing to price those trades would be worse than pricing them
    slightly wrong and saying so here.
    """
    chosen = SCHEDULES[0]
    for schedule in SCHEDULES:
        if schedule.effective <= day:
            chosen = schedule
        else:
            break
    return chosen


@dataclass(frozen=True)
class Fill:
    """One execution. `side` is the side of *this* fill, not of the position.

    A short leg that is opened and then closed produces a SELL fill at entry and
    a BUY fill at exit. That distinction is the whole point: STT attaches to the
    sell, stamp duty to the buy, so a round trip pays each exactly once
    regardless of which direction the position was.
    """

    side: Side
    price: float        # premium per unit
    quantity: int       # units, i.e. lots x lot_size

    @property
    def turnover(self) -> float:
        return self.price * self.quantity


@dataclass(frozen=True)
class Charges:
    """A cost breakdown in rupees. Itemised because a single total is not
    diagnosable — when live costs diverge from modelled ones you need to know
    which line moved."""

    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange
                + self.sebi + self.stamp + self.gst)

    def to_dict(self) -> dict[str, float]:
        return {
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange": round(self.exchange, 2),
            "sebi": round(self.sebi, 2),
            "stamp": round(self.stamp, 2),
            "gst": round(self.gst, 2),
            "total": round(self.total, 2),
        }

    def __add__(self, other: "Charges") -> "Charges":
        return Charges(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange=self.exchange + other.exchange,
            sebi=self.sebi + other.sebi,
            stamp=self.stamp + other.stamp,
            gst=self.gst + other.gst,
        )


@dataclass(frozen=True)
class CostModel:
    """Broker configuration. The statutory side comes from `SCHEDULES`.

    Defaults are Zerodha's F&O options terms, which is what the three accounts
    trade on. `brokerage_per_order` is flat per executed order — note *executed
    order*, not leg: a four-leg strategy entered and exited is eight orders, but
    two legs filled by one order would be one. The engine assumes one order per
    leg per side, which is the conservative reading and matches how the basket
    is actually placed.
    """

    brokerage_per_order: float = 20.0
    # Set False to price a strategy on statutory charges alone — useful for
    # asking "is the edge bigger than the tax", separately from broker choice.
    charge_brokerage: bool = True
    # When set, this schedule applies regardless of trade date. Two uses: the
    # frictionless baseline below, and answering "what would this strategy have
    # made if today's rates had applied throughout" — which is the right
    # question when deciding whether a strategy that worked in 2022 is still
    # worth running, since STT has more than doubled since.
    rates_override: Rates | None = None

    def to_dict(self) -> dict[str, Any]:
        """The whole model, including any rate override.

        The override used to be dropped on save, which made a spec written with
        one — the frictionless baseline, or "what would this have made at
        today's rates throughout" — silently re-run against the real date-scoped
        schedule. Same file, different answer, no error.
        """
        out: dict[str, Any] = {
            "brokerage_per_order": self.brokerage_per_order,
            "charge_brokerage": self.charge_brokerage,
        }
        if self.rates_override is not None:
            rates = asdict(self.rates_override)
            # ISO rather than a date object: this dict is written straight to
            # JSON by the strategy library, which has no encoder of its own,
            # and a bare date made every save with an override raise.
            rates["effective"] = self.rates_override.effective.isoformat()
            out["rates_override"] = rates
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "CostModel":
        payload = payload or {}
        override = payload.get("rates_override")
        rates = None
        if override:
            fields = dict(override)
            effective = fields.get("effective")
            if isinstance(effective, str):
                fields["effective"] = date.fromisoformat(effective)
            rates = Rates(**fields)
        return cls(
            brokerage_per_order=float(payload.get("brokerage_per_order", 20.0)),
            charge_brokerage=bool(payload.get("charge_brokerage", True)),
            rates_override=rates,
        )

    def charge(self, fills: list[Fill], day: date) -> Charges:
        """Total charges for a set of fills executed on `day`."""
        if not fills:
            return Charges()

        rates = self.rates_override or rates_for(day)
        sell_turnover = sum(f.turnover for f in fills if f.side == "SELL")
        buy_turnover = sum(f.turnover for f in fills if f.side == "BUY")
        turnover = sell_turnover + buy_turnover

        brokerage = (self.brokerage_per_order * len(fills)
                     if self.charge_brokerage else 0.0)
        exchange = turnover * rates.exchange_txn
        sebi = turnover * rates.sebi_turnover

        return Charges(
            brokerage=brokerage,
            stt=sell_turnover * rates.stt_sell_premium,
            exchange=exchange,
            sebi=sebi,
            stamp=buy_turnover * rates.stamp_buy,
            gst=(brokerage + exchange + sebi) * rates.gst,
        )

    def round_trip(self, entry: list[Fill], exit_: list[Fill],
                   day: date) -> Charges:
        """Charges for opening and closing, as one figure."""
        return self.charge(entry, day) + self.charge(exit_, day)


def exercise_stt(intrinsic_points: float, quantity: int, day: date) -> float:
    """STT on a long option left to expire in the money.

    **Nothing in the engine calls this, and that is correct rather than an
    oversight — but it is worth stating, because forty lines of docstring above
    describe a trap the engine cannot currently spring.** Every backtest exits
    at `exit_time` by squaring off, so no modelled position is ever held to
    settlement and no exercise STT is ever due. The charge is not missing from
    any run; the situation that incurs it does not arise.

    It exists ready-made for the day the engine grows an expire-at-settlement
    exit — which is a real strategy for a seller who wants to avoid paying the
    spread to close a worthless wing, and the one case where the rate table
    above starts mattering to a result.

    Charged on intrinsic value rather than premium, which is why it dwarfs every
    other cost on the trade. A 200-point ITM call on 75 units is charged on
    ₹15,000 of intrinsic — at 0.15% that is ₹22.50, against a round trip whose
    other charges might total ₹5.

    Returns 0 for out-of-the-money or zero-intrinsic positions, and for
    non-positive quantities, so callers can apply it unconditionally.
    """
    if intrinsic_points <= 0 or quantity <= 0:
        return 0.0
    return intrinsic_points * quantity * rates_for(day).stt_exercise_intrinsic


ZERO_RATES = Rates(
    effective=date(1970, 1, 1),
    stt_sell_premium=0.0, stt_exercise_intrinsic=0.0, exchange_txn=0.0,
    sebi_turnover=0.0, stamp_buy=0.0, gst=0.0,
    note="frictionless — not a real regime",
)

# Zerodha F&O options, current rates. What the engine uses unless told otherwise.
DEFAULT = CostModel()

# No brokerage, no statutory charges, no friction of any kind. Used by tests
# that isolate the P&L arithmetic from the charge arithmetic, and by research
# that wants the raw signal before deciding whether it clears the hurdle.
# Never a realistic answer — a strategy evaluated on this basis has not been
# evaluated.
FREE = CostModel(brokerage_per_order=0.0, charge_brokerage=False,
                 rates_override=ZERO_RATES)
