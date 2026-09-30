"""``GET /api/markets/chain`` states the risk-free rate behind each derived IV.

Phase 3 decision 19, step 3's done-when: *the chain's derived greeks state
which rate they used, and use FRED's when it is reachable.* Three fields per
row -- ``riskFreeRate``, ``riskFreeRateSource``, ``riskFreeRateDate`` --
present exactly where ``ivSource`` is ``derived``: a vendor IV used no rate of
ours, and an absent IV used none.

**Convention (owner decision Q16):** ``riskFreeRate`` is the **continuously
compounded** rate the derived IV was solved at -- the number pricing used --
not FRED's quoted bond-equivalent yield. A quoted 4.16% is served as
``0.0413857528``; the fallback's 4.25% as ``0.0422764153``. Both figures are
hand-computed in ``tests/pricing/test_rates.py``.
"""

from datetime import date
from decimal import Decimal

from corollary.pricing.rates import RiskFreeRateSource, rate_from_dgs3mo
from tests.api.test_markets_routes import MarketClient, by_symbol, nvda_chain, rows

_RATE_FIELDS = ("riskFreeRate", "riskFreeRateSource", "riskFreeRateDate")


def test_a_derived_row_names_the_fred_rate_it_was_solved_at(
    make_market_client: MarketClient,
) -> None:
    source = RiskFreeRateSource()
    source.adopt(rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22)))
    client, _ = make_market_client(nvda_chain(), risk_free_rate=source)

    chain = rows(client, "/api/markets/chain/NVDA")
    derived = [row for row in chain if row["ivSource"] == "derived"]
    assert derived, "nothing was derived; this test would prove nothing"
    for row in derived:
        # Continuous, as pricing used it -- not the quoted 0.0416.
        assert Decimal(str(row["riskFreeRate"])) == Decimal("0.0413857528")
        assert row["riskFreeRateSource"] == "fred_dgs3mo"
        assert row["riskFreeRateDate"] == "2026-09-22"


def test_without_fred_a_derived_row_says_default(
    make_market_client: MarketClient,
) -> None:
    client, _ = make_market_client(nvda_chain())

    derived = [
        row for row in rows(client, "/api/markets/chain/NVDA") if row["ivSource"] == "derived"
    ]
    assert derived
    for row in derived:
        # The fallback converts by the same rule: 4.25% quoted.
        assert Decimal(str(row["riskFreeRate"])) == Decimal("0.0422764153")
        assert row["riskFreeRateSource"] == "default"
        assert row["riskFreeRateDate"] is None


def test_the_rate_fields_are_present_exactly_where_the_iv_is_derived(
    make_market_client: MarketClient,
) -> None:
    source = RiskFreeRateSource()
    source.adopt(rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22)))
    client, _ = make_market_client(nvda_chain(), risk_free_rate=source)

    chain = by_symbol(rows(client, "/api/markets/chain/NVDA"))
    assert {row["ivSource"] for row in chain.values()} == {"vendor", "derived", None}
    for symbol, row in chain.items():
        for name in _RATE_FIELDS:
            assert name in row, (symbol, name)
        if row["ivSource"] == "derived":
            assert row["riskFreeRate"] is not None, symbol
            assert row["riskFreeRateSource"] is not None, symbol
        else:
            assert all(row[name] is None for name in _RATE_FIELDS), symbol
