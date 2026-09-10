"""Black-Scholes prices, greeks, and implied volatility.

Phase 2 decision 10: **IV and greeks are computed locally, for the contracts
the feed leaves empty.** The premise this rested on was stated too strongly
and did not survive the fixtures: the ``indicative`` feed does not serve
``impliedVolatility`` and ``greeks`` on *every* contract, it serves them on
*some*. ``tests/fixtures/alpaca/option_chain_nvda_page1.json`` carries them on
16 of its 100 contracts, ``option_chain_nvda_page2.json`` on 28 of 100, and
``option_chain_nvda_dated.json`` on all 26. The original claim came from
sampling a handful of contracts off an endpoint that returns them ordered by
strike, so the sample was the deep-ITM tail -- where a vendor derivation has
little to work with either.

What survives is the design: vendor analytics pass through untouched and carry
:class:`~corollary.data.providers.interface.AnalyticsSource` ``VENDOR``; the
gaps are filled here and carry ``DERIVED``. That is not a workaround for a
missing measurement -- Alpaca's own OpenAPI document describes both fields as
*"calculated using the Black-Scholes model"*, so the vendor derives them too,
and the $99/mo subscription buys the same arithmetic run on their hardware
with better inputs. (``feed=opra`` answers HTTP 403 *"OPRA agreement is not
signed"* on a paywalled plan, so those better inputs are genuinely
unavailable, not merely unrequested.)

The honest cost, stated because it bounds where these numbers may be used:
**a derived greek is only as good as the two prices it is derived from.** The
contract mid is 15 minutes stale on the ``indicative`` feed, and the spot is
an **IEX** mid -- roughly 2.5% of US volume, not a consolidated price. Correct
method, thin and stale inputs. Fine for a column on the Markets page. Not fine
for sizing an order, which is the same boundary decision 10 draws from the
other side when it says the plan is bought before Phase 6 rather than before
Phase 2.

Two assumptions, stated rather than hidden
------------------------------------------

**Risk-free rate.** An injected parameter, defaulting to
:data:`DEFAULT_RISK_FREE_RATE`. That default is a **placeholder**, not a
measurement — FRED (series ``DGS3MO``) is the real source and is a planned
§7 dependency; wiring it is a later step. The sensitivity is small at the
tenors this app trades: at 30 DTE a 100bp error in ``r`` moves an ATM $150
call by roughly a cent and its implied vol by a fraction of a point. It is
*not* small at a one-year LEAP, so when FRED arrives this default should stop
being reachable rather than merely stop being used.

**Dividend yield.** Zero, via :data:`DEFAULT_DIVIDEND_YIELD`, and this one is
a real assumption with a real direction. There is no per-underlying dividend
source in the codebase, and inventing one per symbol is worse than a stated
constant. The consequence: for a dividend-paying underlying, a call is priced
slightly rich and a put slightly cheap, so a *derived* IV on a call is biased
low and on a put biased high. For SPY at ~1.2% over 30 DTE the forward is off
by about 0.1% of spot — an order of magnitude smaller than the staleness
already baked into an ``indicative`` quote, which is why it is acceptable and
why it is written down.

Why this module is ``float`` and that is not a Decimal violation
----------------------------------------------------------------

CLAUDE.md's rule is *money* as ``Decimal``. Nothing in the interior of this
module is money. ``d1`` and ``d2`` are dimensionless, delta and gamma are
ratios, and implied volatility is a ratio. Black-Scholes is transcendental —
``erf``, ``exp``, ``log``, ``sqrt`` — and has no exact decimal form at all, so
a ``Decimal`` interior would buy nothing but a slower, less accurate
``exp``.

The boundary is explicit and lives in one place: :func:`derive_analytics`
takes ``Decimal`` prices in, converts once, and hands ``Decimal`` back
quantized to :data:`ANALYTICS_PRECISION`. Callers never see the float. The
greeks it returns are *derived sensitivities* rather than money, but they are
``Decimal`` so that arithmetic combining them with position values (delta
times contract value, theta times quantity) stays exact on the money side.

Greek scaling conventions
-------------------------

These match Alpaca's documented example, which is the point — a column that
switches convention when the plan is upgraded would be a silent 100x. See
``tests/pricing/test_blackscholes.py``:

===== ===================================================
delta per $1 of underlying, unscaled
gamma per $1 of underlying squared, unscaled
theta per **calendar day** (the annual figure divided by 365)
vega  per **one volatility point** (annual divided by 100)
rho   per **one rate point** (annual divided by 100)
===== ===================================================
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

__all__ = [
    "ANALYTICS_PRECISION",
    "DEFAULT_DIVIDEND_YIELD",
    "DEFAULT_RISK_FREE_RATE",
    "IMPLIED_VOL_CEILING",
    "IMPLIED_VOL_FLOOR",
    "Analytics",
    "AnalyticsUnavailable",
    "CloseAt",
    "Greeks",
    "default_close_at",
    "derive_analytics",
    "greeks",
    "implied_volatility",
    "norm_cdf",
    "norm_pdf",
    "price",
    "years_to_expiry",
]

#: Placeholder for the 3-month bill. Replace with FRED ``DGS3MO``; see the
#: module docstring for why this is stated rather than silently chosen.
DEFAULT_RISK_FREE_RATE = 0.0425

#: No dividend. A stated assumption with a stated direction of error.
DEFAULT_DIVIDEND_YIELD = 0.0

#: The bracket the implied-vol search runs over. A quote implying less than
#: 0.1% or more than 500% annualised volatility is a broken quote, not a
#: contract with an extreme view, and reporting absence beats clamping to the
#: edge of the bracket and calling it a measurement.
IMPLIED_VOL_FLOOR = 1e-4
IMPLIED_VOL_CEILING = 5.0

#: Six decimal places. Alpaca serves sixteen significant figures of a number
#: derived from a two-decimal quote; carrying that many through would be
#: false precision, and the sixth place is already below anything the UI
#: renders.
ANALYTICS_PRECISION = Decimal("0.000001")

#: The **regular** session close, and the fallback when no calendar is
#: injected. Measuring to midnight instead would overstate time to expiry by
#: two-thirds of a day on the last day, which is where theta is largest and
#: the error matters most -- and measuring to 16:00 on a half-day overstates
#: it by three hours in exactly the same place. See :data:`CloseAt`.
_MARKET_CLOSE = time(16, 0)
_EASTERN = ZoneInfo("America/New_York")

#: ACT/365 fixed. The convention Alpaca's own numbers reproduce under (see the
#: theta test), and the one every options desk quotes vol in.
_DAYS_PER_YEAR = 365.0

_SQRT_2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)

_BISECTION_ITERATIONS = 200
_BISECTION_TOLERANCE = 1e-10


@dataclass(frozen=True, slots=True)
class BlackScholesGreeks:
    """The five sensitivities, as floats, scaled per the table above."""

    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


@dataclass(frozen=True, slots=True)
class Greeks:
    """The five sensitivities as exact decimals, for storage and display."""

    delta: Decimal
    gamma: Decimal
    theta: Decimal
    vega: Decimal
    rho: Decimal


@dataclass(frozen=True, slots=True)
class Analytics:
    """A derived implied volatility and the greeks computed at it."""

    implied_volatility: Decimal
    greeks: Greeks


@dataclass(frozen=True, slots=True)
class AnalyticsUnavailable:
    """No analytics could be derived, and why.

    A distinct type rather than ``None`` so the reason survives to the caller
    and, eventually, to the screen. §8.5's rule is that an admitted gap beats
    a fluent non-answer; that only works if the gap carries its reason.
    """

    reason: str


def norm_cdf(x: float) -> float:
    """The standard normal CDF, via ``math.erf``.

    ``erf`` is in the standard library and is correctly rounded to within an
    ulp on CPython, so there is no reason to carry a rational approximation or
    to take a SciPy dependency for one function.
    """
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def norm_pdf(x: float) -> float:
    """The standard normal PDF."""
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _d1_d2(
    spot: float, strike: float, years: float, vol: float, rate: float, q: float
) -> tuple[float, float]:
    variance = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - q + 0.5 * vol * vol) * years) / variance
    return d1, d1 - variance


def _intrinsic(spot: float, strike: float, is_call: bool) -> float:
    return max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)


def price(
    *,
    spot: float,
    strike: float,
    years: float,
    vol: float,
    is_call: bool,
    rate: float = DEFAULT_RISK_FREE_RATE,
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
) -> float:
    """The Black-Scholes-Merton value of a European option.

    American exercise is not modelled. On a non-dividend-paying underlying an
    American call is worth the European call exactly, and the early-exercise
    premium on the puts this app trades is small relative to a 15-minute-stale
    quote. Naming the gap is the point: these are European values on American
    contracts, which is also what Alpaca's own figures are.

    Both degenerate cases return the no-arbitrage value rather than dividing
    by zero: at ``years == 0`` that is intrinsic, and at ``vol == 0`` it is
    the discounted forward intrinsic.
    """
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError(
            f"spot and strike must be positive, got spot={spot!r} strike={strike!r}"
        )
    if vol < 0.0:
        raise ValueError(f"volatility cannot be negative, got {vol!r}")
    if years <= 0.0:
        return _intrinsic(spot, strike, is_call)

    discount = math.exp(-rate * years)
    carry = math.exp(-dividend_yield * years)
    if vol == 0.0:
        forward = spot * carry - strike * discount
        return max(forward, 0.0) if is_call else max(-forward, 0.0)

    d1, d2 = _d1_d2(spot, strike, years, vol, rate, dividend_yield)
    if is_call:
        return spot * carry * norm_cdf(d1) - strike * discount * norm_cdf(d2)
    return strike * discount * norm_cdf(-d2) - spot * carry * norm_cdf(-d1)


def greeks(
    *,
    spot: float,
    strike: float,
    years: float,
    vol: float,
    is_call: bool,
    rate: float = DEFAULT_RISK_FREE_RATE,
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
) -> BlackScholesGreeks:
    """The five sensitivities, scaled per the table in the module docstring."""
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError(
            f"spot and strike must be positive, got spot={spot!r} strike={strike!r}"
        )
    if years <= 0.0 or vol <= 0.0:
        # Nothing is sensitive to anything once there is no time or no
        # uncertainty left. Delta survives as the step function it becomes.
        if is_call:
            delta = 1.0 if spot > strike else 0.0
        else:
            delta = -1.0 if spot < strike else 0.0
        return BlackScholesGreeks(delta=delta, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    d1, d2 = _d1_d2(spot, strike, years, vol, rate, dividend_yield)
    discount = math.exp(-rate * years)
    carry = math.exp(-dividend_yield * years)
    root_years = math.sqrt(years)
    pdf_d1 = norm_pdf(d1)

    gamma = carry * pdf_d1 / (spot * vol * root_years)
    vega_annual = spot * carry * pdf_d1 * root_years
    decay = -(spot * carry * pdf_d1 * vol) / (2.0 * root_years)

    if is_call:
        delta = carry * norm_cdf(d1)
        theta_annual = (
            decay
            - rate * strike * discount * norm_cdf(d2)
            + dividend_yield * spot * carry * norm_cdf(d1)
        )
        rho_annual = strike * years * discount * norm_cdf(d2)
    else:
        delta = -carry * norm_cdf(-d1)
        theta_annual = (
            decay
            + rate * strike * discount * norm_cdf(-d2)
            - dividend_yield * spot * carry * norm_cdf(-d1)
        )
        rho_annual = -strike * years * discount * norm_cdf(-d2)

    return BlackScholesGreeks(
        delta=delta,
        gamma=gamma,
        theta=theta_annual / _DAYS_PER_YEAR,
        vega=vega_annual / 100.0,
        rho=rho_annual / 100.0,
    )


def implied_volatility(
    *,
    target_price: float,
    spot: float,
    strike: float,
    years: float,
    is_call: bool,
    rate: float = DEFAULT_RISK_FREE_RATE,
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
) -> float | None:
    """Solve for the volatility that reproduces ``target_price``, or ``None``.

    Bisection, not Newton. Vega collapses to nothing on a deep-in-the-money or
    nearly-expired contract, and Newton divides by it — the iteration diverges
    or lands on a wild number exactly where the chain is thinnest and someone
    would be most inclined to believe a printed figure. Price is strictly
    monotone in volatility, so bisection over the bracket cannot fail, and 200
    halvings of a bracket 5 wide is well past the precision of the input.

    ``None`` means the price is outside the no-arbitrage bracket: below
    intrinsic, above the ceiling, non-positive, or the contract has expired.
    Reporting absence is deliberate. Clamping to the edge of the bracket would
    put a number in the IV column that nobody computed, which is exactly the
    failure decision 10 exists to avoid.
    """
    if years <= 0.0 or target_price <= 0.0 or spot <= 0.0 or strike <= 0.0:
        return None

    low, high = IMPLIED_VOL_FLOOR, IMPLIED_VOL_CEILING
    price_low = price(spot=spot, strike=strike, years=years, vol=low,
                      is_call=is_call, rate=rate, dividend_yield=dividend_yield)
    if target_price < price_low:
        return None
    price_high = price(spot=spot, strike=strike, years=years, vol=high,
                       is_call=is_call, rate=rate, dividend_yield=dividend_yield)
    if target_price > price_high:
        return None

    for _ in range(_BISECTION_ITERATIONS):
        mid = 0.5 * (low + high)
        value = price(spot=spot, strike=strike, years=years, vol=mid,
                      is_call=is_call, rate=rate, dividend_yield=dividend_yield)
        if abs(value - target_price) < _BISECTION_TOLERANCE:
            return mid
        if value < target_price:
            low = mid
        else:
            high = mid
        if high - low < _BISECTION_TOLERANCE:
            break
    return 0.5 * (low + high)


#: Resolves a calendar date to the instant that session closes, aware.
#:
#: Injected rather than imported so this module stays pure arithmetic over
#: floats and ``math``. :func:`corollary.calendars.nyse_close_at` is the real
#: one; it pulls in ``exchange_calendars``, pandas and numpy and takes about
#: half a second to build XNYS, and importing that here would put all of it in
#: the import path of the pricer.
CloseAt = Callable[[date], datetime]


def default_close_at(expiration: date) -> datetime:
    """16:00 America/New_York -- the ordinary session close.

    Right on every trading day except the ~10 half-days a year, which is
    precisely why hardcoding it was invisible. CLAUDE.md: *"Session boundaries
    come from a market calendar, never hardcoded 09:30-16:00. Half-days and
    holidays are real."* Pass :func:`corollary.calendars.nyse_close_at` to get
    those right; this is what a caller with no calendar to hand gets. Stated
    in ET rather than as 21:00 UTC because the exchange does not move with
    daylight saving and UTC does.
    """
    return datetime.combine(expiration, _MARKET_CLOSE, tzinfo=_EASTERN)


def years_to_expiry(
    expiration: date, now: datetime, *, close_at: CloseAt = default_close_at
) -> float:
    """Time to expiry in ACT/365 years, measured to that session's close.

    Three traps, two of which CLAUDE.md documents elsewhere in other forms:

    * **The expiration is a date, not an instant.** Treating it as UTC
      midnight puts expiry sixteen hours early and, on the last day, roughly
      doubles theta. Options settle against the close.
    * **The close is not always 16:00.** A contract expiring on the day after
      Thanksgiving settles against 13:00 ET. Three hours out of the last day
      of a contract's life is three hours at the point theta is largest, and
      the error is silent: it simply leaves the contract looking slightly
      alive. That is what ``close_at`` is for.
    * **``now`` must be timezone-aware.** A naive datetime here is the same
      four-or-five-hour error ``UtcDateTime`` refuses at the database
      boundary, and it has no symptom.

    Clamped at zero: a contract past its close has no time value, and a
    negative ``years`` would make ``sqrt`` raise several frames away from the
    cause.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            "now must be timezone-aware; a naive datetime cannot be placed "
            f"against a session close. Got {now!r}"
        )
    close = close_at(expiration)
    remaining: timedelta = close - now
    if remaining.total_seconds() <= 0.0:
        return 0.0
    return remaining.total_seconds() / (_DAYS_PER_YEAR * 86400.0)


def _quantize(value: float) -> Decimal:
    try:
        return Decimal(repr(value)).quantize(
            ANALYTICS_PRECISION, rounding=ROUND_HALF_EVEN
        )
    except InvalidOperation:  # pragma: no cover — nan/inf cannot reach here
        raise ValueError(f"cannot represent {value!r} as a decimal")


def derive_analytics(
    *,
    mid: Decimal,
    spot: Decimal,
    strike: Decimal,
    years: float,
    is_call: bool,
    rate: float = DEFAULT_RISK_FREE_RATE,
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
) -> Analytics | AnalyticsUnavailable:
    """The one boundary between exact decimals and the float interior.

    Takes the quote mid and the underlying spot as ``Decimal`` — which is what
    they are everywhere else in this codebase — converts once, solves, and
    quantizes back. Returns :class:`AnalyticsUnavailable` with a reason rather
    than raising, because a single unpriceable contract must not take down a
    200-row chain.
    """
    if mid <= 0:
        return AnalyticsUnavailable(reason="no two-sided quote to take a mid from")
    if spot <= 0:
        return AnalyticsUnavailable(reason="no price for the underlying")
    if strike <= 0:
        return AnalyticsUnavailable(reason="contract has no usable strike")
    if years <= 0.0:
        return AnalyticsUnavailable(reason="contract has expired")

    spot_f = float(spot)
    strike_f = float(strike)
    vol = implied_volatility(
        target_price=float(mid), spot=spot_f, strike=strike_f, years=years,
        is_call=is_call, rate=rate, dividend_yield=dividend_yield,
    )
    if vol is None:
        return AnalyticsUnavailable(
            reason=(
                "the quote mid implies no volatility between "
                f"{IMPLIED_VOL_FLOOR:.4%} and {IMPLIED_VOL_CEILING:.0%} — it "
                "sits outside the no-arbitrage bounds for this contract"
            )
        )

    computed = greeks(
        spot=spot_f, strike=strike_f, years=years, vol=vol, is_call=is_call,
        rate=rate, dividend_yield=dividend_yield,
    )
    return Analytics(
        implied_volatility=_quantize(vol),
        greeks=Greeks(
            delta=_quantize(computed.delta),
            gamma=_quantize(computed.gamma),
            theta=_quantize(computed.theta),
            vega=_quantize(computed.vega),
            rho=_quantize(computed.rho),
        ),
    )
