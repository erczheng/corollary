"""Black-Scholes, against published values rather than against itself.

Decision 10 derives IV and greeks locally because Alpaca's own OpenAPI
document describes its ``impliedVolatility`` and ``greeks`` as *"calculated
using the Black-Scholes model"* — so this reproduces the vendor's method, not
an approximation of a measurement. That claim is only worth anything if the
arithmetic is checked against something external, so the tests here use:

* Hull, *Options, Futures and Other Derivatives*, Example 15.6 — the standard
  textbook worked example.
* The closed-form at-the-money identity, which needs no reference at all.
* Put-call parity, which any correct pricer satisfies exactly.
* **Alpaca's own documented snapshot example**, which pins the greek
  *scaling* conventions — the part a from-scratch implementation is most
  likely to get wrong, and the part that is invisible until a number looks
  odd on screen.
"""

import math
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from corollary.pricing.blackscholes import (
    ANALYTICS_PRECISION,
    DEFAULT_DIVIDEND_YIELD,
    DEFAULT_RISK_FREE_RATE,
    IMPLIED_VOL_CEILING,
    Analytics,
    AnalyticsUnavailable,
    default_close_at,
    derive_analytics,
    greeks,
    implied_volatility,
    norm_cdf,
    price,
    years_to_expiry,
)

# --------------------------------------------------------------------------
# The normal CDF
# --------------------------------------------------------------------------


def test_norm_cdf_at_the_landmarks() -> None:
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.0) == pytest.approx(0.8413447461, abs=1e-9)
    assert norm_cdf(-1.0) == pytest.approx(0.1586552539, abs=1e-9)
    assert norm_cdf(1.96) == pytest.approx(0.9750021049, abs=1e-9)


def test_norm_cdf_is_symmetric() -> None:
    for x in (0.1, 0.5, 1.3, 2.7, 4.0):
        assert norm_cdf(x) + norm_cdf(-x) == pytest.approx(1.0, abs=1e-12)


# --------------------------------------------------------------------------
# Prices, against published values
# --------------------------------------------------------------------------


def test_hull_example_15_6() -> None:
    """S=42, K=40, r=10%, sigma=20%, T=0.5 gives c=4.76 and p=0.81."""
    kwargs = dict(spot=42.0, strike=40.0, years=0.5, vol=0.20, rate=0.10, dividend_yield=0.0)
    assert price(is_call=True, **kwargs) == pytest.approx(4.76, abs=5e-3)
    assert price(is_call=False, **kwargs) == pytest.approx(0.81, abs=5e-3)


def test_the_at_the_money_closed_form() -> None:
    """With r=q=0 and S=K, c = S(N(sigma*sqrt(T)/2) - N(-sigma*sqrt(T)/2))."""
    spot, vol, years = 100.0, 0.20, 1.0
    half = vol * math.sqrt(years) / 2.0
    expected = spot * (norm_cdf(half) - norm_cdf(-half))
    assert price(
        spot=spot, strike=100.0, years=years, vol=vol, rate=0.0,
        dividend_yield=0.0, is_call=True,
    ) == pytest.approx(expected, abs=1e-12)
    assert expected == pytest.approx(7.9655674, abs=1e-6)


@pytest.mark.parametrize("spot", [80.0, 100.0, 125.0])
@pytest.mark.parametrize("q", [0.0, 0.02])
def test_put_call_parity_holds_exactly(spot: float, q: float) -> None:
    strike, years, vol, rate = 100.0, 0.75, 0.28, 0.045
    call = price(spot=spot, strike=strike, years=years, vol=vol, rate=rate,
                 dividend_yield=q, is_call=True)
    put = price(spot=spot, strike=strike, years=years, vol=vol, rate=rate,
                dividend_yield=q, is_call=False)
    parity = spot * math.exp(-q * years) - strike * math.exp(-rate * years)
    assert call - put == pytest.approx(parity, abs=1e-9)


def test_price_is_monotone_in_volatility() -> None:
    """The property the implied-vol bisection depends on for correctness."""
    previous = -1.0
    for vol in (0.05, 0.10, 0.25, 0.5, 1.0, 2.0):
        value = price(spot=100.0, strike=105.0, years=0.25, vol=vol,
                      rate=0.04, dividend_yield=0.0, is_call=True)
        assert value > previous
        previous = value


def test_at_expiry_the_price_is_intrinsic() -> None:
    assert price(spot=110.0, strike=100.0, years=0.0, vol=0.3, rate=0.04,
                 dividend_yield=0.0, is_call=True) == pytest.approx(10.0)
    assert price(spot=90.0, strike=100.0, years=0.0, vol=0.3, rate=0.04,
                 dividend_yield=0.0, is_call=True) == pytest.approx(0.0)
    assert price(spot=90.0, strike=100.0, years=0.0, vol=0.3, rate=0.04,
                 dividend_yield=0.0, is_call=False) == pytest.approx(10.0)


def test_zero_volatility_is_the_discounted_forward_intrinsic() -> None:
    spot, strike, years, rate = 100.0, 90.0, 1.0, 0.05
    expected = spot - strike * math.exp(-rate * years)
    assert price(spot=spot, strike=strike, years=years, vol=0.0, rate=rate,
                 dividend_yield=0.0, is_call=True) == pytest.approx(expected, abs=1e-9)


# --------------------------------------------------------------------------
# Greeks, and their scaling conventions
# --------------------------------------------------------------------------


def test_delta_stays_inside_its_bounds() -> None:
    call = greeks(spot=100.0, strike=100.0, years=0.5, vol=0.3, rate=0.04,
                  dividend_yield=0.0, is_call=True)
    put = greeks(spot=100.0, strike=100.0, years=0.5, vol=0.3, rate=0.04,
                 dividend_yield=0.0, is_call=False)
    assert 0.0 < call.delta < 1.0
    assert -1.0 < put.delta < 0.0
    # Call delta minus put delta is exp(-qT); at q=0 that is exactly 1.
    assert call.delta - put.delta == pytest.approx(1.0, abs=1e-12)


def test_a_call_is_in_the_money_above_its_strike() -> None:
    """The inversion CLAUDE.md calls out on UnderlyingChart, in greek form."""
    itm = greeks(spot=120.0, strike=100.0, years=0.25, vol=0.3, rate=0.04,
                 dividend_yield=0.0, is_call=True)
    otm = greeks(spot=80.0, strike=100.0, years=0.25, vol=0.3, rate=0.04,
                 dividend_yield=0.0, is_call=True)
    assert itm.delta > 0.9
    assert otm.delta < 0.1


def test_gamma_and_vega_do_not_depend_on_the_right() -> None:
    call = greeks(spot=103.0, strike=100.0, years=0.4, vol=0.27, rate=0.04,
                  dividend_yield=0.01, is_call=True)
    put = greeks(spot=103.0, strike=100.0, years=0.4, vol=0.27, rate=0.04,
                 dividend_yield=0.01, is_call=False)
    assert call.gamma == pytest.approx(put.gamma, abs=1e-12)
    assert call.vega == pytest.approx(put.vega, abs=1e-12)


# Alpaca's documented snapshot for AAPL240426C00162500, from the
# /v1beta1/options/snapshots/{underlying} reference. Copied verbatim.
_ALPACA_IV = 0.3372405712050441
_ALPACA_DELTA = 0.7521304109871954
_ALPACA_GAMMA = 0.06241426404871288
_ALPACA_THETA = -0.2847623059595503
_ALPACA_VEGA = 0.047540520834498785
_ALPACA_RHO = 0.009910739032549095

# The example states no spot, rate or time to expiry, so they were recovered
# from the greeks themselves. Two identities do it: sigma falls out of
# delta, gamma and vega alone as n(d1)^2 / (gamma * 100 * vega), and that
# recovery returns 0.3372405459 against Alpaca's stated 0.3372405712 --
# agreement to seven significant figures, which is only possible if vega is
# scaled by 1/100. Rho then pins T at 0.00821897 years, i.e. exactly 3.00000
# calendar days, and the d1 identity pins r and S.
_RECOVERED_SPOT = 165.772155
_RECOVERED_YEARS = 0.00821897
_RECOVERED_RATE = 0.051520
_STRIKE = 162.5


def test_the_greek_scaling_matches_alpacas_documented_example() -> None:
    """Four of Alpaca's five greeks reproduce to six figures or better.

    This is the test that makes decision 10's claim — *"reproduces the
    vendor's method rather than approximating a measurement"* — checkable
    rather than rhetorical, and it pins the **scaling**, which is the part a
    from-scratch implementation gets wrong silently. Every wrong convention
    here is off by orders of magnitude, not by a rounding: unscaled vega is
    100x, annual theta is 365x, and both would look merely "surprising" in a
    column rather than obviously broken.

    Theta lands within 0.2% rather than exactly. The inputs were recovered
    from the other four greeks, and theta is by far the most sensitive of the
    five to the recovered ``T``, so a residual there is expected; it is three
    orders of magnitude smaller than the gap to any alternative convention.
    """
    computed = greeks(
        spot=_RECOVERED_SPOT, strike=_STRIKE, years=_RECOVERED_YEARS,
        vol=_ALPACA_IV, rate=_RECOVERED_RATE, dividend_yield=0.0, is_call=True,
    )
    # The tolerances are set by the six-decimal recovered constants above,
    # not by the arithmetic: at full precision delta agrees to 1e-16.
    assert computed.delta == pytest.approx(_ALPACA_DELTA, rel=1e-6)
    assert computed.gamma == pytest.approx(_ALPACA_GAMMA, rel=1e-5)
    assert computed.vega == pytest.approx(_ALPACA_VEGA, rel=1e-5)
    assert computed.rho == pytest.approx(_ALPACA_RHO, rel=1e-4)
    assert computed.theta == pytest.approx(_ALPACA_THETA, rel=5e-3)


def test_the_wrong_scaling_conventions_are_nowhere_near() -> None:
    """The three near-misses this codebase must never ship, made explicit."""
    computed = greeks(
        spot=_RECOVERED_SPOT, strike=_STRIKE, years=_RECOVERED_YEARS,
        vol=_ALPACA_IV, rate=_RECOVERED_RATE, dividend_yield=0.0, is_call=True,
    )
    # Vega per 1.00 of vol rather than per point: 100x too big.
    assert computed.vega * 100.0 == pytest.approx(_ALPACA_VEGA * 100.0)
    assert abs(computed.vega * 100.0 - _ALPACA_VEGA) > 4.0
    # Theta per year rather than per day: 365x too big.
    assert abs(computed.theta * 365.0 - _ALPACA_THETA) > 100.0
    # Rho per 1.00 of rate rather than per point: 100x too big.
    assert abs(computed.rho * 100.0 - _ALPACA_RHO) > 0.9


def test_the_recovered_inputs_also_reproduce_alpacas_implied_volatility() -> None:
    """A closed loop: price at Alpaca's IV, then solve back to it."""
    mid = price(
        spot=_RECOVERED_SPOT, strike=_STRIKE, years=_RECOVERED_YEARS,
        vol=_ALPACA_IV, rate=_RECOVERED_RATE, dividend_yield=0.0, is_call=True,
    )
    solved = implied_volatility(
        target_price=mid, spot=_RECOVERED_SPOT, strike=_STRIKE,
        years=_RECOVERED_YEARS, rate=_RECOVERED_RATE, dividend_yield=0.0,
        is_call=True,
    )
    assert solved is not None
    assert solved == pytest.approx(_ALPACA_IV, rel=1e-6)


def test_theta_is_negative_for_a_long_option() -> None:
    for is_call in (True, False):
        assert greeks(spot=100.0, strike=100.0, years=0.25, vol=0.3, rate=0.0,
                      dividend_yield=0.0, is_call=is_call).theta < 0.0


def test_greeks_at_expiry_are_all_zero_except_delta() -> None:
    """No time left means no sensitivity to anything but the spot itself."""
    g = greeks(spot=110.0, strike=100.0, years=0.0, vol=0.3, rate=0.04,
               dividend_yield=0.0, is_call=True)
    assert g.delta == pytest.approx(1.0)
    assert g.gamma == 0.0
    assert g.vega == 0.0
    assert g.theta == 0.0
    assert g.rho == 0.0


# --------------------------------------------------------------------------
# Implied volatility
# --------------------------------------------------------------------------


@pytest.mark.parametrize("true_vol", [0.08, 0.20, 0.3372405712050441, 0.85, 2.5])
@pytest.mark.parametrize("is_call", [True, False])
def test_implied_volatility_round_trips(true_vol: float, is_call: bool) -> None:
    target = price(spot=100.0, strike=105.0, years=0.3, vol=true_vol,
                   rate=0.045, dividend_yield=0.0, is_call=is_call)
    solved = implied_volatility(
        target_price=target, spot=100.0, strike=105.0, years=0.3,
        rate=0.045, dividend_yield=0.0, is_call=is_call,
    )
    assert solved is not None
    assert solved == pytest.approx(true_vol, abs=1e-6)


def test_a_price_below_intrinsic_has_no_implied_volatility() -> None:
    """No sigma prices a call below its own floor, so we report absence.

    Clamping to zero would put a plausible number in the IV column that
    nobody computed — the one thing decision 10 rules out.
    """
    assert implied_volatility(
        target_price=1.0, spot=150.0, strike=100.0, years=0.5,
        rate=0.04, dividend_yield=0.0, is_call=True,
    ) is None


def test_a_price_above_the_ceiling_has_no_implied_volatility() -> None:
    ceiling_price = price(spot=100.0, strike=100.0, years=0.5,
                          vol=IMPLIED_VOL_CEILING, rate=0.04,
                          dividend_yield=0.0, is_call=True)
    assert implied_volatility(
        target_price=ceiling_price * 1.01, spot=100.0, strike=100.0,
        years=0.5, rate=0.04, dividend_yield=0.0, is_call=True,
    ) is None


def test_a_nonpositive_price_or_expired_contract_has_no_implied_volatility() -> None:
    common = dict(spot=100.0, strike=100.0, rate=0.04, dividend_yield=0.0, is_call=True)
    assert implied_volatility(target_price=0.0, years=0.5, **common) is None
    assert implied_volatility(target_price=-1.0, years=0.5, **common) is None
    assert implied_volatility(target_price=5.0, years=0.0, **common) is None


# --------------------------------------------------------------------------
# Time to expiry
# --------------------------------------------------------------------------


def test_years_to_expiry_measures_to_the_close_on_expiration_day() -> None:
    """16:00 America/New_York, ACT/365. A bare date would be off by a session."""
    now = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)  # 16:00 ET
    assert years_to_expiry(date(2026, 9, 10), now) == pytest.approx(0.0)
    assert years_to_expiry(date(2026, 9, 11), now) == pytest.approx(1 / 365, abs=1e-9)


def test_years_to_expiry_is_never_negative() -> None:
    now = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)
    assert years_to_expiry(date(2026, 9, 10), now) == 0.0


def test_years_to_expiry_refuses_a_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        years_to_expiry(date(2026, 9, 11), datetime(2026, 9, 10, 20, 0))


# --------------------------------------------------------------------------
# The Decimal boundary
# --------------------------------------------------------------------------


def test_derive_analytics_returns_decimals_not_floats() -> None:
    result = derive_analytics(
        mid=Decimal("7.25"), spot=Decimal("166.26"), strike=Decimal("162.5"),
        years=0.25, is_call=True,
    )
    assert not isinstance(result, AnalyticsUnavailable)
    assert isinstance(result.implied_volatility, Decimal)
    for value in (result.greeks.delta, result.greeks.gamma, result.greeks.theta,
                  result.greeks.vega, result.greeks.rho):
        assert isinstance(value, Decimal)


def test_derive_analytics_round_trips_a_known_volatility() -> None:
    mid = Decimal(str(price(spot=100.0, strike=105.0, years=0.3, vol=0.42,
                            rate=DEFAULT_RISK_FREE_RATE,
                            dividend_yield=DEFAULT_DIVIDEND_YIELD, is_call=True)))
    result = derive_analytics(mid=mid, spot=Decimal("100"), strike=Decimal("105"),
                              years=0.3, is_call=True)
    assert not isinstance(result, AnalyticsUnavailable)
    assert result.implied_volatility == pytest.approx(Decimal("0.42"), abs=Decimal("1e-5"))


def test_derive_analytics_reports_absence_rather_than_inventing() -> None:
    unavailable = derive_analytics(
        mid=Decimal("0.01"), spot=Decimal("150"), strike=Decimal("100"),
        years=0.5, is_call=True,
    )
    assert isinstance(unavailable, AnalyticsUnavailable)
    assert unavailable.reason


def test_derive_analytics_refuses_a_nonpositive_spot() -> None:
    result = derive_analytics(mid=Decimal("1"), spot=Decimal("0"),
                              strike=Decimal("100"), years=0.5, is_call=True)
    assert isinstance(result, AnalyticsUnavailable)


def test_the_defaults_are_stated_and_documented() -> None:
    """The dividend assumption is zero, and that is a decision, not an oversight."""
    assert DEFAULT_DIVIDEND_YIELD == 0.0
    assert 0.0 <= DEFAULT_RISK_FREE_RATE < 0.20


# --------------------------------------------------------------------------
# Session boundaries come from a calendar, not from a constant
# --------------------------------------------------------------------------


def test_years_to_expiry_takes_the_close_from_an_injected_resolver() -> None:
    """CLAUDE.md: *"Session boundaries come from a market calendar, never
    hardcoded 09:30-16:00. Half-days and holidays are real."*

    A contract expiring the day after Thanksgiving settles against a 13:00 ET
    close. Measuring to 16:00 overstates time to expiry by three hours on the
    single day theta is largest.
    """
    now = datetime(2025, 11, 27, 18, 0, tzinfo=timezone.utc)
    expiry = date(2025, 11, 28)

    def half_day(day: date) -> datetime:
        assert day == expiry
        return datetime(2025, 11, 28, 18, 0, tzinfo=timezone.utc)  # 13:00 ET

    full = years_to_expiry(expiry, now)
    early = years_to_expiry(expiry, now, close_at=half_day)
    assert full - early == pytest.approx(3.0 / 24.0 / 365.0, rel=1e-9)


def test_the_default_resolver_is_the_ordinary_four_oclock_close() -> None:
    """The module stays pure: no calendar dependency reaches into the maths."""
    close = default_close_at(date(2025, 11, 26))
    assert close.utcoffset() is not None
    assert close.astimezone(timezone.utc) == datetime(
        2025, 11, 26, 21, 0, tzinfo=timezone.utc
    )


def test_a_resolver_that_moves_the_close_moves_theta_with_it() -> None:
    """The three hours are not cosmetic -- they are priced."""
    now = datetime(2025, 11, 27, 18, 0, tzinfo=timezone.utc)
    expiry = date(2025, 11, 28)
    common = dict(spot=100.0, strike=100.0, vol=0.30, is_call=True)
    full = price(years=years_to_expiry(expiry, now), **common)
    early = price(
        years=years_to_expiry(
            expiry,
            now,
            close_at=lambda _day: datetime(2025, 11, 28, 18, 0, tzinfo=timezone.utc),
        ),
        **common,
    )
    assert early < full


# --------------------------------------------------------------------------
# The precision the greeks are quantized to
# --------------------------------------------------------------------------


def test_analytics_precision_is_six_places_and_stays_there() -> None:
    """A mutant that survived the whole suite.

    ``ANALYTICS_PRECISION`` widened from ``0.000001`` to ``0.01`` passed every
    test while quantizing gamma to ``0.00`` on essentially every contract --
    the value is a second derivative and lives three or four places below the
    decimal point. Nothing else in the suite looked at a number that small.
    """
    assert ANALYTICS_PRECISION == Decimal("0.000001")


def test_the_sixth_place_is_load_bearing_on_an_ordinary_contract() -> None:
    """The behavioural half, measured against the recorded chain.

    Re-deriving every contract in ``option_chain_nvda_page1`` that has a
    two-sided quote gives 23 sets of greeks. Widening the quantum to
    ``Decimal("0.01")`` would round **rho to 0.00 on all 23**, gamma on 6 and
    vega on 5 -- so the column would not look broken, it would look calm.

    Below is one of those rows: an out-of-the-money call a day from expiry,
    which is about as ordinary as a chain gets.
    """
    analytics = derive_analytics(
        mid=Decimal("0.045"),
        spot=Decimal("206.00"),
        strike=Decimal("240.00"),
        years=years_to_expiry(
            date(2026, 9, 11), datetime(2026, 9, 10, 19, 10, tzinfo=timezone.utc)
        ),
        is_call=True,
    )
    assert isinstance(analytics, Analytics)
    greeks_ = analytics.greeks
    for name, value in (
        ("gamma", greeks_.gamma),
        ("vega", greeks_.vega),
        ("rho", greeks_.rho),
    ):
        assert value != 0, name
        assert abs(value) < Decimal("0.005"), (
            f"{name} = {value} would quantize to 0.00 at two decimal places"
        )
        assert value.as_tuple().exponent == -6, name
