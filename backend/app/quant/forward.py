"""The forward price an option chain is actually trading against.

This module exists because of a real discrepancy: greeks computed here did not
agree with Sensibull's, and the cause was the underlying, not the model.

An index option is not priced off the index. NIFTY spot is a statistic — an
average of 50 cash prices — and you cannot hedge an option with it. What the
market prices against is the FORWARD: the index level deliverable at expiry,
which is what the future trades at. The gap between the two is the basis
(carry minus dividends), tens of points on NIFTY, and it is not noise: feeding
spot into Black-Scholes where the market used the forward produces calls that
look systematically cheap and puts systematically dear, which is exactly the
kind of skew artefact that shows up as "the greeks disagree".

Three sources, best first:

1. **Put-call parity at the money.** C - P = (F - K)·e^(-rT), so
   F = K + (C - P)·e^(rT). This is the market's own forward, extracted from the
   chain being priced — no rate assumption, no dividend assumption, no separate
   instrument. It is the synthetic future, and it is what a desk quotes.
2. **The traded future**, when parity is unusable (one side untraded).
3. **spot·e^(rT)**, the textbook fallback, which ignores dividends and so is
   the least accurate — but it beats using bare spot.

Cash equities are their own forward over these horizons; no adjustment.
"""

import math
from typing import Any

# Parity needs a genuine two-sided market on BOTH legs of the strike, and the
# further from the money you go the more the bid-ask noise swamps (C - P).
# Anything beyond this fraction from spot is not a parity candidate.
MAX_ATM_DISTANCE = 0.03

# A forward this far from spot means a bad quote leaked into the parity solve,
# not a real basis. Index basis runs well under 1% at these tenors.
MAX_BASIS = 0.05


def from_parity(call_price: float, put_price: float, strike: float,
                t_years: float, r: float) -> float | None:
    """F = K + (C - P)·e^(rT) — the forward implied by one strike's pair."""
    if call_price <= 0 or put_price <= 0 or strike <= 0 or t_years < 0:
        return None
    return strike + (call_price - put_price) * math.exp(r * t_years)


def _sane(forward: float | None, spot: float) -> bool:
    return bool(forward and spot > 0 and abs(forward - spot) / spot <= MAX_BASIS)


def solve(rows: list[dict[str, Any]], spot: float, t_years: float, r: float,
          future_price: float = 0.0) -> tuple[float, str]:
    """Best available forward for a chain, with the source that produced it.

    `rows` are chain rows shaped {"strike", "ce": {...}, "pe": {...}} where each
    side carries a "price" and "price_source". Only strikes whose BOTH sides are
    genuine two-sided quotes are used, so a stale wing cannot move the forward.
    """
    if spot <= 0:
        return 0.0, "none"

    candidates = []
    for row in rows:
        strike = float(row.get("strike") or 0)
        if strike <= 0 or abs(strike - spot) / spot > MAX_ATM_DISTANCE:
            continue
        ce, pe = row.get("ce") or {}, row.get("pe") or {}
        # Mid-priced only: parity on a stale LTP imports that staleness straight
        # into the forward, and from there into every greek on the chain.
        if ce.get("price_source") != "mid" or pe.get("price_source") != "mid":
            continue
        forward = from_parity(float(ce.get("price") or 0), float(pe.get("price") or 0),
                              strike, t_years, r)
        if _sane(forward, spot):
            candidates.append((abs(strike - spot), forward))

    if candidates:
        # Nearest-the-money strike has the tightest spreads, so the smallest
        # error in (C - P). Take the best three and use the median to stay
        # robust against a single wide quote.
        candidates.sort()
        picks = sorted(f for _, f in candidates[:3])
        return round(picks[len(picks) // 2], 2), "parity"

    if _sane(future_price, spot):
        return round(future_price, 2), "future"

    return round(spot * math.exp(r * t_years), 2), "carry"


def basis_pct(forward: float, spot: float) -> float:
    return round((forward - spot) / spot * 100, 3) if spot > 0 else 0.0


# A parity forward computed for the chain is reused by the positions view for
# this long. Long enough that the two pages agree while you look at both, short
# enough that it cannot outlive a move.
CACHE_SECONDS = 60.0


def for_expiry(hub, underlying: str, expiry, spot: float, t_years: float,
               r: float) -> tuple[float, str]:
    """Forward for one underlying/expiry, for callers without a full chain.

    Positions span many underlyings and expiries and the feed only carries the
    contracts actually held, so put-call parity usually is not available here.
    Order of preference:

    1. a parity forward the chain solved moments ago for this same expiry,
    2. the future expiring on that same date, which IS the forward by definition,
    3. spot·e^(rT).
    """
    import time as _time

    if spot <= 0:
        return 0.0, "none"

    key = (underlying, expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry))
    cached = getattr(hub, "forward_cache", {}).get(key)
    if cached and (_time.time() - cached[2]) <= CACHE_SECONDS:
        return cached[0], cached[1]

    token = hub.future_token_for(underlying, expiry)
    if token:
        price = float((hub.feed.ticks.get(token) or {}).get("last_price") or 0)
        if _sane(price, spot):
            return round(price, 2), "future"

    return round(spot * math.exp(r * t_years), 2), "carry"


def remember(hub, underlying: str, expiry, value: float, source: str) -> None:
    """Publish a solved forward so other views price against the same number."""
    import time as _time

    if source != "parity" or value <= 0:
        return
    key = (underlying, expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry))
    hub.forward_cache[key] = (value, source, _time.time())
