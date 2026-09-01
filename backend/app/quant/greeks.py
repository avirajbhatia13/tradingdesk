"""Black-Scholes greeks, recovered from live prices.

Kite Connect returns no IV and no greeks, and Sensibull has no public API, so
the dashboard derives them itself: invert Black-Scholes on the option's traded
price to get IV, then differentiate at that IV. This is the same pricing core
as StrikeDesk's quant/iv.py, extended with the first-order greeks.

Stated assumptions:
- European exercise (NSE index and stock options are European)
- flat risk-free rate, no dividends
- the option's last traded price is a fair mark

That last one is the weak link. On an illiquid far strike the LTP can be
minutes old, and every greek inherits that staleness. Positions priced off a
stale quote are flagged rather than silently trusted — see `quote_is_usable`.
"""

import math
from typing import Any

DAYS_PER_YEAR = 365.0

# Below this, the IV solve is numerically meaningless: a 5-paise option has no
# information about volatility left in it.
MIN_PRICE = 0.05
IV_LOWER, IV_UPPER = 0.01, 3.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(spot: float, strike: float, t_years: float, sigma: float,
             option_type: str, r: float) -> float:
    if t_years <= 0 or sigma <= 0:
        intrinsic = (spot - strike) if option_type == "CE" else (strike - spot)
        return max(intrinsic, 0.0)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t_years: float,
                option_type: str, r: float) -> float | None:
    """Bisection on sigma over [1%, 300%].

    Bisection over Newton-Raphson deliberately: vega collapses to ~0 on the deep
    OTM strikes an option seller lives on, which makes Newton divide-by-almost-
    zero unstable, while bisection is guaranteed to converge because the BS
    price is monotonic in sigma. Returns None when the price sits outside
    no-arbitrage bounds — stale or illiquid far strikes do this, and None is
    more honest than a garbage number.
    """
    if t_years <= 0 or price is None or price < MIN_PRICE or spot <= 0 or strike <= 0:
        return None
    lo, hi = IV_LOWER, IV_UPPER
    if not (bs_price(spot, strike, t_years, lo, option_type, r)
            <= price
            <= bs_price(spot, strike, t_years, hi, option_type, r)):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if bs_price(spot, strike, t_years, mid, option_type, r) < price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0, 4)


# ---------------------------------------------------------------------------
# Black-76: the same model expressed on the FORWARD instead of spot.
#
# Feeding a forward into the spot-based functions above is a real and subtle
# bug — those apply carry themselves, so the forward gets grown by e^(rT) a
# second time and every greek inherits the error. On a 3-DTE NIFTY option that
# double-count showed up as a ~1.6 vol-point gap between the call and put IV at
# the same strike, which put-call parity says is impossible. Hence a separate,
# explicitly forward-based set.
# ---------------------------------------------------------------------------

def b76_price(forward: float, strike: float, t_years: float, sigma: float,
              option_type: str, r: float) -> float:
    if t_years <= 0 or sigma <= 0:
        intrinsic = (forward - strike) if option_type == "CE" else (strike - forward)
        return max(intrinsic, 0.0) * math.exp(-r * max(t_years, 0.0))
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(forward / strike) + 0.5 * sigma ** 2 * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    discount = math.exp(-r * t_years)
    if option_type == "CE":
        return discount * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
    return discount * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))


def b76_implied_vol(price: float, forward: float, strike: float, t_years: float,
                    option_type: str, r: float) -> float | None:
    """Bisection, for the same reason as `implied_vol` — vega dies on the wings."""
    if t_years <= 0 or price is None or price < MIN_PRICE or forward <= 0 or strike <= 0:
        return None
    lo, hi = IV_LOWER, IV_UPPER
    if not (b76_price(forward, strike, t_years, lo, option_type, r)
            <= price
            <= b76_price(forward, strike, t_years, hi, option_type, r)):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if b76_price(forward, strike, t_years, mid, option_type, r) < price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0, 4)


def b76_greeks(forward: float, strike: float, t_years: float, sigma: float,
               option_type: str, r: float) -> dict[str, float]:
    """Greeks with respect to the FORWARD.

    Delta here is the forward delta — the hedge ratio against the future, which
    is the instrument you would actually hedge with. It differs from spot delta
    by the discount factor (0.9994 at three days), which is below the noise on
    any quote this is derived from.

    Units match `option_greeks`: gamma per point, theta per calendar day, vega
    per vol point.
    """
    if t_years <= 0 or sigma <= 0:
        if option_type == "CE":
            delta = 1.0 if forward > strike else 0.0
        else:
            delta = -1.0 if forward < strike else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(forward / strike) + 0.5 * sigma ** 2 * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(d1)
    discount = math.exp(-r * t_years)

    gamma = discount * pdf_d1 / (forward * sigma * sqrt_t)
    vega = discount * forward * pdf_d1 * sqrt_t / 100.0
    # Common to both sides: F·phi(d1) = K·phi(d2) under Black-76.
    decay = -discount * forward * pdf_d1 * sigma / (2.0 * sqrt_t)

    if option_type == "CE":
        delta = discount * _norm_cdf(d1)
        value = discount * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
    else:
        delta = -discount * _norm_cdf(-d1)
        value = discount * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))

    theta = (decay + r * value) / DAYS_PER_YEAR
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def option_greeks(spot: float, strike: float, t_years: float, sigma: float,
                  option_type: str, r: float) -> dict[str, float]:
    """Per-unit greeks. Multiply by signed quantity for a position.

    Units chosen to be readable on a desk rather than mathematically pure:
      delta  per 1 unit of underlying  (unchanged)
      gamma  delta change per 1 point move in the underlying
      theta  rupees per CALENDAR day (BS theta / 365)
      vega   rupees per 1 VOL POINT, i.e. per 0.01 of sigma
    """
    if t_years <= 0 or sigma <= 0:
        # At expiry the option is its intrinsic value: delta is a step function
        # and the other greeks are gone.
        if option_type == "CE":
            delta = 1.0 if spot > strike else 0.0
        else:
            delta = -1.0 if spot < strike else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(d1)
    discount = math.exp(-r * t_years)

    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0
    decay = -(spot * pdf_d1 * sigma) / (2.0 * sqrt_t)

    if option_type == "CE":
        delta = _norm_cdf(d1)
        theta = (decay - r * strike * discount * _norm_cdf(d2)) / DAYS_PER_YEAR
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (decay + r * strike * discount * _norm_cdf(-d2)) / DAYS_PER_YEAR

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def quote_is_usable(last_price: float | None, exchange_timestamp: int | None,
                    now_epoch: float, max_age_seconds: int = 300) -> bool:
    """Whether a quote is fresh enough to derive greeks from.

    Five minutes is generous for an index option and tight for a deep OTM stock
    strike, which is the point: the flag exists so the UI can grey out numbers
    it should not be trusted on, not to hide them.
    """
    if last_price is None or last_price < MIN_PRICE:
        return False
    if not exchange_timestamp:
        return True  # no timestamp on the packet; assume fresh rather than blank the row
    return (now_epoch - exchange_timestamp) <= max_age_seconds


def scale(greeks: dict[str, float], quantity: float) -> dict[str, float]:
    """Position greeks = per-unit greeks x signed quantity.

    Kite reports NFO quantity in units (lots x lot_size), already signed
    negative for shorts, so a short strangle's theta comes out positive with no
    special-casing here.
    """
    return {key: value * quantity for key, value in greeks.items()}


def empty() -> dict[str, float]:
    return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}


def add(into: dict[str, float], other: dict[str, Any]) -> dict[str, float]:
    for key in ("delta", "gamma", "theta", "vega"):
        into[key] = into.get(key, 0.0) + float(other.get(key) or 0.0)
    return into
