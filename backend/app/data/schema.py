"""Storage schema for the market-data lake.

Two tables, both 1-minute bars, both Parquet on local disk queried in place by
DuckDB. The measured shape of this data on real volumes: 23 bytes per row
compressed, a day-slice query in 7ms, and 4M rows into Arrow in 144ms. Five
years of the NIFTY chain lands at a couple of gigabytes, which is why none of
this needs a server, a subscription, or the cloud.

Conventions worth knowing before you write a query:

**Timestamps are IST wall-clock, naive.** Not UTC. Every venue this app touches
is one exchange in one timezone, and the number a trader means by "09:20" is the
number stored. Converting to UTC would buy nothing and would make every ad-hoc
query wrong by five and a half hours in a way that still returns rows. The
ingest path converts and then asserts every bar lands inside 09:15-15:30, so a
timezone mistake fails loudly rather than silently shifting the session.

**`opt_type`, not `right`.** RIGHT is a reserved word in SQL; a column named
that turns `WHERE right='CE'` into a parser error.

**Prices are float32.** An NSE price has at most two decimals and tops out in
the tens of thousands, so float32 carries every representable value with room
to spare and halves the file. float64 would be storing noise.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pyarrow as pa

from app.config import DB_PATH

# The lake lives beside the SQLite database rather than inside the repo: it is
# data, not source, and it will outgrow anything you want in git.
LAKE_DIR = Path(__file__).resolve().parents[3] / "db" / "lake"

OPTION_BARS = "option_bars"
SPOT_BARS = "spot_bars"
CONTRACTS = "contracts"

# Session bounds, used to validate that ingested timestamps are really IST.
SESSION_OPEN = (9, 15)
SESSION_CLOSE = (15, 30)

# The same bounds as minutes past midnight, which is the form every query wants.
#
# The lake deliberately stores bars outside these bounds rather than rejecting
# them: Dhan serves nine minutes past the 15:30 close, and NSE's Diwali Muhurat
# sessions genuinely trade in the evening (18:00-19:15). Both are real vendor
# responses and throwing them away would be inventing data policy at ingest.
# Filtering belongs at the point of use — see the backtest engine, which trades
# only the regular session.
SESSION_OPEN_MINUTE = SESSION_OPEN[0] * 60 + SESSION_OPEN[1]      # 555
SESSION_CLOSE_MINUTE = SESSION_CLOSE[0] * 60 + SESSION_CLOSE[1]   # 930


option_bar_schema = pa.schema([
    pa.field("ts", pa.timestamp("s")),               # IST wall-clock, naive
    pa.field("underlying", pa.string()),
    pa.field("expiry", pa.date32()),
    # Which expiry series a rolling vendor bar belongs to: 'WEEK' or 'MONTH'.
    #
    # Dhan's rolling endpoint never names the contract, so `expiry` is null for
    # everything it returns — and without this column the weekly and monthly
    # ATM bars for the same minute and strike are indistinguishable. They then
    # collide on the dedupe key and one silently overwrites the other, mixing
    # two different instruments into one price series. Nothing errors; the
    # backtest just runs on a chimera.
    #
    # Null for our own recording, which knows the real contract and fills
    # `expiry` instead. The pair (expiry, series) identifies a contract in both
    # cases, which is why the dedupe key carries both.
    pa.field("series", pa.string()),                 # 'WEEK' | 'MONTH' | null
    pa.field("strike", pa.float32()),
    pa.field("opt_type", pa.string()),               # 'CE' | 'PE'
    # Offset from at-the-money in strike steps at the time of the bar, which is
    # how Dhan serves this data and how most strategies are actually expressed
    # ("sell the third OTM call"). Null for bars we recorded ourselves, where
    # the absolute strike is what we knew.
    #
    # int16, not int8, and that is not cosmetic. Dhan only ever serves +/-10
    # strikes, so int8 was enough until the Upstox pull was widened to the
    # whole listing — and a long-dated NIFTY monthly lists strikes from 12000
    # to 34500, which against a spot near 24000 is **240 steps**. pyarrow
    # refused the first value over 127 and killed the backfill, which is the
    # good outcome: the alternative is 148 wrapping to -108 and labelling an
    # OTM call as deep ITM, in a column the engine selects on.
    pa.field("moneyness", pa.int16()),
    pa.field("open", pa.float32()),
    pa.field("high", pa.float32()),
    pa.field("low", pa.float32()),
    pa.field("close", pa.float32()),
    pa.field("volume", pa.int64()),
    pa.field("oi", pa.int64()),
    pa.field("iv", pa.float32()),                    # nullable
    pa.field("spot", pa.float32()),                  # underlying at the bar
    # Where the row came from, so a backtest can tell vendor history from our
    # own recording and a bad vendor batch can be deleted without touching the
    # rest. See lake.replace_source().
    pa.field("source", pa.string()),                 # 'dhan' | 'live'
])

spot_bar_schema = pa.schema([
    pa.field("ts", pa.timestamp("s")),
    pa.field("symbol", pa.string()),
    pa.field("open", pa.float32()),
    pa.field("high", pa.float32()),
    pa.field("low", pa.float32()),
    pa.field("close", pa.float32()),
    pa.field("volume", pa.int64()),
    pa.field("source", pa.string()),
])


# One row per real expired contract, exactly as the vendor listed it.
#
# The bar tables answer "what did this trade at"; this answers "what was this
# instrument". Kept separate rather than as more columns on `option_bars`
# because it is per-contract and not per-minute: NIFTY's whole two-year
# retention window is about 22,000 rows here against 100 million there, so
# denormalising it would cost gigabytes to store facts that never vary within a
# contract.
#
# Everything the Upstox listing endpoint returns is kept, including fields
# nothing reads yet. Re-fetching costs a request against a rate limit measured
# in hours, and a field that was thrown away at ingest is not recoverable once
# the contract ages out of the vendor's retention window.
contract_schema = pa.schema([
    pa.field("underlying", pa.string()),
    pa.field("expiry", pa.date32()),
    pa.field("strike", pa.float32()),
    pa.field("opt_type", pa.string()),               # 'CE' | 'PE' | 'FUT'
    # The vendor's own answer to whether this is a weekly or a monthly
    # contract, which is worth far more than deriving it from the date: it
    # settles the WEEK/MONTH question without a calendar, and NSE has changed
    # both the weekly expiry weekday and which expiries exist at all.
    #
    # Deliberately NOT copied into `option_bars.series`. That column exists to
    # separate two *rolling* series that share a timestamp and a strike; these
    # rows carry a real expiry date instead, and tagging them 'WEEK' would put
    # five different live weeklies behind one label — the exact contract-mixing
    # bug that column was added to prevent.
    pa.field("weekly", pa.bool_()),
    # The lot size in force when this contract was LISTED, so one expiry can
    # differ from its neighbours around a revision. Reading it per contract is
    # what makes a lot-size calendar exact rather than inferred from where the
    # observed sizes happen to change. See backtest/lots.py.
    pa.field("lot_size", pa.int32()),
    pa.field("tick_size", pa.float32()),
    # Exchange-imposed maximum single order quantity. Nothing backtests on it
    # today, but it is the hard ceiling on how large a leg can actually be
    # traded, so a strategy that sizes past it is untradeable in reality.
    pa.field("freeze_quantity", pa.float32()),
    pa.field("minimum_lot", pa.int32()),
    pa.field("trading_symbol", pa.string()),
    pa.field("instrument_key", pa.string()),         # how the vendor is asked for it
    pa.field("exchange_token", pa.string()),
    pa.field("segment", pa.string()),
    pa.field("exchange", pa.string()),
    pa.field("source", pa.string()),
])

SCHEMAS = {OPTION_BARS: option_bar_schema, SPOT_BARS: spot_bar_schema,
           CONTRACTS: contract_schema}


def table_dir(table: str) -> Path:
    return LAKE_DIR / table


def partition_dir(table: str, key: str, day: date) -> Path:
    """One directory per instrument-month.

    Month granularity is the sweet spot measured against this data: a day per
    directory makes DuckDB open ~1250 files for a five-year scan, and a year
    per directory makes every small query read a 400MB file. A month is ~8MB
    and lets row-group statistics skip most of it.
    """
    if table == CONTRACTS:
        # No time partition: this is a listing, not a time series.
        return table_dir(table) / f"underlying={key}"
    if table == OPTION_BARS:
        return table_dir(table) / f"underlying={key}" / f"month={day:%Y-%m}"
    return table_dir(table) / f"symbol={key}" / f"month={day:%Y-%m}"


def partition_key_column(table: str) -> str:
    return "underlying" if table in (OPTION_BARS, CONTRACTS) else "symbol"


__all__ = [
    "LAKE_DIR", "OPTION_BARS", "SPOT_BARS", "CONTRACTS", "SCHEMAS", "DB_PATH",
    "option_bar_schema", "spot_bar_schema", "contract_schema",
    "table_dir", "partition_dir",
    "partition_key_column", "SESSION_OPEN", "SESSION_CLOSE",
]
