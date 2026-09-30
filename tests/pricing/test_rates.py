"""The risk-free rate a derived greek was computed at, and where it came from.

Phase 3 decision 19: ``AlpacaProvider`` takes the latest FRED ``DGS3MO``
observation, falling back to the old placeholder only when FRED was never
reached, and the chain's derived greeks record which one they used.

Owner decision Q16: ``DGS3MO`` is a bond-equivalent (investment-basis) yield,
simple for a 3-month bill, and Black-Scholes wants a continuous rate --
``r = ln(1 + y*t) / t`` with ``t = 91/365``. Every expected value below is
worked here by a **different algorithm** from the one under test (the
alternating Taylor series of ``ln(1 + x)`` at 60 digits, rather than
``Decimal.ln``), and pinned as a literal as well.
"""

from datetime import date
from decimal import Decimal, localcontext

import pytest

from corollary.pricing.rates import (
    DGS3MO_TERM_YEARS,
    FALLBACK_DGS3MO_PERCENT,
    FALLBACK_RISK_FREE_RATE,
    RATE_QUANTUM,
    RateProvenance,
    RiskFreeRate,
    RiskFreeRateSource,
    continuous_rate_from_bill_yield,
    rate_from_dgs3mo,
)


def _hand_continuous(percent: str) -> Decimal:
    """``ln(1 + y*t) / t`` by Taylor series at 60 digits, rounded to the quantum."""
    with localcontext() as ctx:
        ctx.prec = 60
        t = Decimal(91) / Decimal(365)
        x = Decimal(percent) / Decimal(100) * t
        total = Decimal(0)
        power = x
        for n in range(1, 200):
            total += power / n if n % 2 else -power / n
            power *= x
        return (total / t).quantize(Decimal("1E-10"))


def test_the_bill_term_is_a_13_week_bill_on_a_365_day_year() -> None:
    with localcontext() as ctx:
        ctx.prec = 40  # the module's stated conversion precision
        assert DGS3MO_TERM_YEARS == Decimal(91) / Decimal(365)
    assert RATE_QUANTUM == Decimal("1E-10")


@pytest.mark.parametrize(
    ("percent", "expected"),
    [
        ("4.00", "0.0398018641"),
        ("0", "0"),
        ("20.00", "0.1951734920"),
        ("4.16", "0.0413857528"),
        ("1.00", "0.0099875549"),
    ],
)
def test_dgs3mo_is_converted_to_continuous_compounding(percent: str, expected: str) -> None:
    """At 4.00% the continuous rate is 3.98019% -- 1.98bp below the quoted yield."""
    assert _hand_continuous(percent) == Decimal(expected)
    rate = rate_from_dgs3mo(Decimal(percent), date(2026, 9, 22))
    assert rate.rate == Decimal(expected)
    assert rate.provenance is RateProvenance.FRED_DGS3MO
    assert rate.observation_date == date(2026, 9, 22)


def test_the_conversion_lowers_every_positive_yield_and_by_about_2bp_at_4_percent() -> None:
    quoted = Decimal("0.04")
    converted = continuous_rate_from_bill_yield(quoted)
    assert converted < quoted
    assert quoted - converted == Decimal("0.0001981359")  # 1.98bp


def test_the_conversion_ignores_the_callers_decimal_context() -> None:
    """``ln`` runs under the module's explicit context, never the ambient one."""
    expected = rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22))
    with localcontext() as ctx:
        ctx.prec = 3
        assert rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22)) == expected


def test_the_conversion_is_exact_decimal_and_refuses_a_float() -> None:
    assert isinstance(continuous_rate_from_bill_yield(Decimal("0.0416")), Decimal)
    with pytest.raises(TypeError):
        continuous_rate_from_bill_yield(0.0416)  # type: ignore[arg-type]


def test_a_yield_with_no_logarithm_is_refused() -> None:
    """``1 + y*t <= 0`` has no ``ln``; refused by name rather than as a Decimal trap."""
    with pytest.raises(ValueError, match="1 \+ y"):
        continuous_rate_from_bill_yield(Decimal(-5))


def test_the_fallback_is_the_old_placeholder_read_as_a_quoted_yield() -> None:
    """0.0425 was a placeholder for the same quantity FRED quotes, so it converts too.

    Consistency over the 2bp: a FRED observation of 4.25% and the fallback
    must price identically, which they only do if both are one convention.
    """
    assert FALLBACK_DGS3MO_PERCENT == Decimal("4.25")
    assert FALLBACK_RISK_FREE_RATE.rate == _hand_continuous("4.25")
    assert FALLBACK_RISK_FREE_RATE.rate == Decimal("0.0422764153")
    assert FALLBACK_RISK_FREE_RATE.provenance is RateProvenance.DEFAULT
    assert FALLBACK_RISK_FREE_RATE.observation_date is None
    assert (
        FALLBACK_RISK_FREE_RATE.rate
        == rate_from_dgs3mo(FALLBACK_DGS3MO_PERCENT, date(2026, 9, 22)).rate
    )


def test_a_fred_rate_must_name_its_observation_date() -> None:
    with pytest.raises(ValueError, match="observation date"):
        RiskFreeRate(
            rate=Decimal("0.0416"),
            provenance=RateProvenance.FRED_DGS3MO,
            observation_date=None,
        )


def test_the_default_carries_no_observation_date() -> None:
    with pytest.raises(ValueError, match="observation date"):
        RiskFreeRate(
            rate=Decimal("0.0425"),
            provenance=RateProvenance.DEFAULT,
            observation_date=date(2026, 9, 22),
        )


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_a_non_finite_rate_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        rate_from_dgs3mo(Decimal(bad), date(2026, 9, 22))


def test_a_float_rate_is_refused() -> None:
    with pytest.raises(TypeError):
        RiskFreeRate(
            rate=0.0416,  # type: ignore[arg-type]
            provenance=RateProvenance.FRED_DGS3MO,
            observation_date=date(2026, 9, 22),
        )


def test_the_source_starts_at_the_fallback() -> None:
    assert RiskFreeRateSource().current() == FALLBACK_RISK_FREE_RATE


def test_the_source_adopts_a_fred_rate() -> None:
    source = RiskFreeRateSource()
    fred = rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22))
    source.adopt(fred)
    assert source.current() == fred


def test_the_source_never_adopts_the_default() -> None:
    """The default is what "never reached FRED" looks like -- not a value to set.

    Once an observation has been obtained the fallback must stop being
    reachable; adopting it would make a transient FRED outage silently swap a
    measured rate for a placeholder.
    """
    source = RiskFreeRateSource()
    source.adopt(rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22)))
    with pytest.raises(ValueError, match="default"):
        source.adopt(FALLBACK_RISK_FREE_RATE)
    assert source.current().provenance is RateProvenance.FRED_DGS3MO
