"""``AlpacaProvider.asset_by_cusip`` -- one N-PORT holding's CUSIP to a ticker.

The SPDR sector seed (Phase 3 step 4) reads each fund's holdings from SEC
N-PORT, which names a holding by CUSIP and never by ticker. Alpaca's
``GET /v2/assets/{cusip}`` on the **trading** host resolves one; the bulk
``/v2/assets`` list carries no ``cusip``, so it is one request per CUSIP.

**Only a 404 means "unresolvable".** Every other failure raises, so an outage,
a refused key or a rate limit is never recorded as "this CUSIP has no ticker".

SYNTHETIC: no ``/v2/assets/{cusip}`` response is recorded yet (the SEC
recorder's live run is pending). The 200 bodies below are **recorded rows
from** ``p4_assets_active_sample.json`` (a ``/v2/assets`` list element, the
same asset object the single-asset endpoint returns) served as a single-asset
body; every other body is synthesised here and says so. The CUSIPs are real
issuers' public identifiers, not credentials.
"""

import json
import logging
from typing import Any

import pytest

from corollary.data.providers.interface import (
    EquityAsset,
    FeedAccessError,
    ProviderError,
    RateLimitedError,
)
from corollary.ratelimit import ALPACA_PAPER_TRADING_HOST
from tests.data.providers.conftest import TEST_CREDENTIALS, load_fixture

pytestmark = pytest.mark.asyncio

NVDA_CUSIP = "67066G104"
BRK_B_CUSIP = "084670702"


def recorded_row(symbol: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = load_fixture("p4_assets_active_sample")["body"]
    (row,) = [r for r in rows if r["symbol"] == symbol]
    return row


def literal(body: Any, status: int = 200) -> tuple[int, str]:
    return (status, body if isinstance(body, str) else json.dumps(body))


async def test_a_resolved_cusip_is_one_request_on_the_trading_host(make_provider):
    row = recorded_row("NVDA")
    provider, transport = make_provider(lambda _r: literal(row))

    asset = await provider.asset_by_cusip(NVDA_CUSIP)

    assert asset == EquityAsset(
        symbol="NVDA",
        name=row["name"],
        tradable=row["tradable"],
        has_options="has_options" in row["attributes"],
        exchange=row["exchange"],
    )
    (request,) = transport.requests
    assert request.method == "GET"
    assert request.url.host == ALPACA_PAPER_TRADING_HOST
    assert request.url.path == f"/v2/assets/{NVDA_CUSIP}"
    assert dict(request.url.params) == {}


async def test_the_cusip_is_trimmed_and_upper_cased_before_the_request(make_provider):
    provider, transport = make_provider(lambda _r: literal(recorded_row("NVDA")))
    await provider.asset_by_cusip(" 67066g104 ")
    assert transport.requests[0].url.path == f"/v2/assets/{NVDA_CUSIP}"


async def test_a_recorded_class_share_keeps_its_dot(make_provider):
    provider, _ = make_provider(lambda _r: literal(recorded_row("BRK.B")))
    asset = await provider.asset_by_cusip(BRK_B_CUSIP)
    assert asset is not None and asset.symbol == "BRK.B"


async def test_a_slash_class_share_is_normalised_to_the_dot_form(make_provider):
    # SYNTHETIC: the recorded BRK.B row with its symbol rewritten in the
    # slash form, to prove the normalisation rather than the vendor's habit.
    row = {**recorded_row("BRK.B"), "symbol": "brk/b"}
    provider, _ = make_provider(lambda _r: literal(row))
    asset = await provider.asset_by_cusip(BRK_B_CUSIP)
    assert asset is not None and asset.symbol == "BRK.B"


async def test_a_404_is_the_one_answer_that_means_unresolvable(make_provider):
    # SYNTHETIC body, in Alpaca's {code, message} error shape.
    body = {"code": 40410000, "message": "asset not found for 00000X000"}
    provider, transport = make_provider(lambda _r: literal(body, 404))
    assert await provider.asset_by_cusip("12345X108") is None
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        "<html><body><h1>404 Not Found</h1></body></html>",  # a gateway's page
        "",  # an empty 404
        {"message": "Not Found"},  # JSON, but no Alpaca code
        {"code": "40410000", "message": "asset not found"},  # code as text
        {"code": True, "message": "asset not found"},  # a bool is not a code
        {"code": 40010000, "message": "asset not found"},  # not a 404xxxxx code
        {"code": 40410000, "message": "endpoint not found"},  # not about an asset
        {"code": 40410000},  # no message
        [{"code": 40410000, "message": "asset not found"}],  # not an object
        "null",
    ],
)
async def test_a_404_that_is_not_alpacas_asset_miss_raises(make_provider, body):
    # SYNTHETIC bodies. A moved route or a wrong URL prefix answering 404 to
    # every request must not record every CUSIP as unresolvable.
    provider, transport = make_provider(lambda _r: literal(body, 404))
    with pytest.raises(ProviderError, match="404"):
        await provider.asset_by_cusip(NVDA_CUSIP)
    assert len(transport.requests) == 1


async def test_the_asset_miss_wording_is_matched_case_insensitively(make_provider):
    # SYNTHETIC body.
    body = {"code": 40410000, "message": "Asset Not Found for 12345X108"}
    provider, _ = make_provider(lambda _r: literal(body, 404))
    assert await provider.asset_by_cusip("12345X108") is None


@pytest.mark.parametrize("status", [500, 502, 503, 401, 422])
async def test_any_other_failure_raises_rather_than_reading_as_unresolvable(
    make_provider, status
):
    # SYNTHETIC bodies.
    provider, _ = make_provider(lambda _r: literal({"message": "nope"}, status))
    with pytest.raises(ProviderError, match=str(status)):
        await provider.asset_by_cusip(NVDA_CUSIP)


async def test_a_403_and_a_429_raise_their_own_types(make_provider):
    provider, _ = make_provider(lambda _r: literal({"message": "forbidden"}, 403))
    with pytest.raises(FeedAccessError):
        await provider.asset_by_cusip(NVDA_CUSIP)
    provider, _ = make_provider(lambda _r: literal({"message": "slow down"}, 429))
    with pytest.raises(RateLimitedError):
        await provider.asset_by_cusip(NVDA_CUSIP)


async def test_an_error_body_echoing_the_secret_is_scrubbed(make_provider):
    # SYNTHETIC: a proxy reflecting the request's secret header into its page.
    body = f"upstream error for {TEST_CREDENTIALS.secret_key}"
    provider, _ = make_provider(lambda _r: literal(body, 503))
    with pytest.raises(ProviderError) as caught:
        await provider.asset_by_cusip(NVDA_CUSIP)
    assert TEST_CREDENTIALS.secret_key not in str(caught.value)


@pytest.mark.parametrize(
    "cusip",
    [
        "",
        "67066G10",
        "67066G1045",
        "67066G/04",
        "../v2/acc",
        "67066G10?",
        "000000000",
        "6706 6G104",
        "67066G10X",  # the check character is always a digit
        "ABCDEFGHI",
    ],
)
async def test_a_malformed_cusip_is_refused_before_any_request(make_provider, cusip):
    provider, transport = make_provider(lambda _r: None)
    with pytest.raises(ValueError):
        await provider.asset_by_cusip(cusip)
    assert transport.requests == []


async def test_a_resolved_asset_that_is_not_an_active_us_equity_is_none_and_logged(
    make_provider, caplog
):
    # SYNTHETIC: the recorded NVDA row marked inactive, as a delisted issue reads.
    row = {**recorded_row("NVDA"), "status": "inactive"}
    provider, _ = make_provider(lambda _r: literal(row))
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.alpaca"):
        assert await provider.asset_by_cusip(NVDA_CUSIP) is None
    assert any("inactive" in r.getMessage() for r in caplog.records)


async def test_a_malformed_asset_body_raises_rather_than_reading_as_unresolvable(
    make_provider,
):
    # SYNTHETIC: an active us_equity row whose `tradable` is not a boolean --
    # a vendor shape change, which must not be recorded as "no ticker".
    row = {**recorded_row("NVDA"), "tradable": "yes"}
    provider, _ = make_provider(lambda _r: literal(row))
    with pytest.raises(ProviderError, match="tradable"):
        await provider.asset_by_cusip(NVDA_CUSIP)


@pytest.mark.parametrize("body", [[], "null", {"message": "ok"}])
async def test_a_body_that_is_not_an_asset_raises(make_provider, body):
    # SYNTHETIC.
    provider, _ = make_provider(lambda _r: literal(body))
    with pytest.raises(ProviderError):
        await provider.asset_by_cusip(NVDA_CUSIP)
