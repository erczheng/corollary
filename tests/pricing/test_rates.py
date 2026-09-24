"""The risk-free rate a derived greek was computed at, and where it came from.

Phase 3 decision 19: ``AlpacaProvider`` takes the latest FRED ``DGS3MO``
observation, falling back to the old placeholder only when FRED was never
reached, and the chain's derived greeks record which one they used.
"""

from datetime import date
from decimal import Decimal

import pytest

from corollary.pricing.rates import (
    FALLBACK_RISK_FREE_RATE,
    RateProvenance,
    RiskFreeRate,
    RiskFreeRateSource,
    rate_from_dgs3mo,
)


def test_the_fallback_is_the_old_placeholder_exactly() -> None:
    assert FALLBACK_RISK_FREE_RATE.rate == Decimal("0.0425")
    assert FALLBACK_RISK_FREE_RATE.provenance is RateProvenance.DEFAULT
    assert FALLBACK_RISK_FREE_RATE.observation_date is None


def test_dgs3mo_is_quoted_in_percent_and_becomes_a_fraction_exactly() -> None:
    rate = rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22))
    assert rate.rate == Decimal("0.0416")
    assert rate.provenance is RateProvenance.FRED_DGS3MO
    assert rate.observation_date == date(2026, 9, 22)


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
