"""The chain's derived greeks state which risk-free rate they used (decision 19).

Done-when for Phase 3 step 3: *the chain's derived greeks state which rate
they used, and use FRED's when it is reachable.* Replayed from the recorded
NVDA chain; no live calls.
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from corollary.data.providers.interface import AnalyticsSource, OptionSnapshot
from corollary.pricing.rates import (
    FALLBACK_RISK_FREE_RATE,
    RateProvenance,
    RiskFreeRate,
    RiskFreeRateSource,
    rate_from_dgs3mo,
)
from tests.data.providers.conftest import by_path, sequence

pytestmark = pytest.mark.asyncio

FRED_RATE = rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22))


def _chain_route() -> Any:
    return by_path(
        {
            "/options/snapshots": sequence(
                "option_chain_nvda_page1", "option_chain_nvda_dated"
            ),
            "/v2/stocks/snapshots": "stock_snapshot_nvda",
        }
    )


async def _chain(make_provider: Any, rates: RiskFreeRateSource | None) -> dict[str, OptionSnapshot]:
    kwargs = {} if rates is None else {"risk_free_rate": rates}
    provider, _ = make_provider(_chain_route(), **kwargs)
    chain: dict[str, OptionSnapshot] = await provider.option_chain("NVDA")
    return chain


def _derived(chain: dict[str, OptionSnapshot]) -> dict[str, OptionSnapshot]:
    return {
        symbol: s
        for symbol, s in chain.items()
        if s.analytics_source is AnalyticsSource.DERIVED
    }


async def test_with_a_fred_observation_derived_greeks_use_it_and_say_so(
    make_provider: Any,
) -> None:
    source = RiskFreeRateSource()
    source.adopt(FRED_RATE)
    derived = _derived(await _chain(make_provider, source))
    assert derived, "nothing was derived; this test would prove nothing"
    for snapshot in derived.values():
        assert snapshot.analytics_rate == FRED_RATE
        assert snapshot.analytics_rate is not None
        assert snapshot.analytics_rate.provenance is RateProvenance.FRED_DGS3MO
        assert snapshot.analytics_rate.observation_date == date(2026, 9, 22)


async def test_without_fred_derived_greeks_use_the_default_and_say_so(
    make_provider: Any,
) -> None:
    derived = _derived(await _chain(make_provider, None))
    assert derived
    for snapshot in derived.values():
        assert snapshot.analytics_rate == FALLBACK_RISK_FREE_RATE
    # And an explicit, never-reached source reads the same.
    derived = _derived(await _chain(make_provider, RiskFreeRateSource()))
    for snapshot in derived.values():
        assert snapshot.analytics_rate is not None
        assert snapshot.analytics_rate.provenance is RateProvenance.DEFAULT
        # 4.25% read as a quoted yield and converted like an observation (Q16).
        assert snapshot.analytics_rate.rate == Decimal("0.0422764153")


async def test_derived_greeks_are_priced_at_the_converted_rate(
    make_provider: Any,
) -> None:
    """Q16: a quoted 4.16% prices at the continuous 0.0413857528, not at 0.0416.

    Three providers over one recorded chain: the FRED observation, a rate
    stated by hand at the hand-computed continuous value (see
    ``tests/pricing/test_rates.py``), and one stated at the unconverted
    quoted yield. The first must match the second exactly and differ from
    the third -- so the conversion reaches the solve, not only the label.
    """
    fred = RiskFreeRateSource()
    fred.adopt(FRED_RATE)
    at_fred = _derived(await _chain(make_provider, fred))

    by_hand = RiskFreeRateSource()
    by_hand.adopt(
        RiskFreeRate(
            rate=Decimal("0.0413857528"),
            provenance=RateProvenance.FRED_DGS3MO,
            observation_date=date(2026, 9, 22),
        )
    )
    at_hand = _derived(await _chain(make_provider, by_hand))

    unconverted = RiskFreeRateSource()
    unconverted.adopt(
        RiskFreeRate(
            rate=Decimal("0.0416"),
            provenance=RateProvenance.FRED_DGS3MO,
            observation_date=date(2026, 9, 22),
        )
    )
    at_quoted = _derived(await _chain(make_provider, unconverted))

    assert at_fred, "nothing was derived; this test would prove nothing"
    assert at_fred.keys() == at_hand.keys()
    for symbol, snapshot in at_fred.items():
        assert snapshot.greeks == at_hand[symbol].greeks, symbol
        assert snapshot.implied_volatility == at_hand[symbol].implied_volatility, symbol
    moved = [
        symbol
        for symbol in at_fred.keys() & at_quoted.keys()
        if at_fred[symbol].greeks != at_quoted[symbol].greeks
    ]
    assert moved, "the 2bp conversion moved no derived greek; nothing pins it"


async def test_the_rate_is_actually_used_not_merely_labelled(make_provider: Any) -> None:
    """Three providers over the same recorded chain.

    A FRED rate of 4.25% must reproduce the default's greeks exactly -- which
    pins that the fallback and an observation share one convention (both
    quoted yields, converted to continuous by Q16's rule) -- while a 1.00%
    rate must move them.
    """
    default = _derived(await _chain(make_provider, None))

    same = RiskFreeRateSource()
    same.adopt(rate_from_dgs3mo(Decimal("4.25"), date(2026, 9, 22)))
    at_same_rate = _derived(await _chain(make_provider, same))

    low = RiskFreeRateSource()
    low.adopt(rate_from_dgs3mo(Decimal("1.00"), date(2026, 9, 22)))
    at_low_rate = _derived(await _chain(make_provider, low))

    assert default.keys() == at_same_rate.keys()
    for symbol, snapshot in default.items():
        assert at_same_rate[symbol].implied_volatility == snapshot.implied_volatility
        assert at_same_rate[symbol].greeks == snapshot.greeks
    moved = [
        symbol
        for symbol in default.keys() & at_low_rate.keys()
        if at_low_rate[symbol].greeks != default[symbol].greeks
    ]
    assert moved, "a 325bp rate change moved no derived greek"


async def test_vendor_and_unavailable_analytics_carry_no_rate_of_ours(
    make_provider: Any,
) -> None:
    source = RiskFreeRateSource()
    source.adopt(FRED_RATE)
    chain = await _chain(make_provider, source)
    vendor = [s for s in chain.values() if s.analytics_source is AnalyticsSource.VENDOR]
    unavailable = [
        s for s in chain.values() if s.analytics_source is AnalyticsSource.UNAVAILABLE
    ]
    assert vendor
    assert all(s.analytics_rate is None for s in vendor)
    assert all(s.analytics_rate is None for s in unavailable)


async def test_the_rate_is_read_per_chain_so_a_refresh_takes_effect(
    make_provider: Any,
) -> None:
    source = RiskFreeRateSource()
    provider, _ = make_provider(
        by_path(
            {
                "/options/snapshots": sequence(
                    "option_chain_nvda_page1",
                    "option_chain_nvda_dated",
                    "option_chain_nvda_page1",
                    "option_chain_nvda_dated",
                ),
                "/v2/stocks/snapshots": "stock_snapshot_nvda",
            }
        ),
        risk_free_rate=source,
    )
    before = _derived(await provider.option_chain("NVDA"))
    source.adopt(FRED_RATE)
    after = _derived(await provider.option_chain("NVDA"))
    assert {s.analytics_rate for s in before.values()} == {FALLBACK_RISK_FREE_RATE}
    assert {s.analytics_rate for s in after.values()} == {FRED_RATE}
