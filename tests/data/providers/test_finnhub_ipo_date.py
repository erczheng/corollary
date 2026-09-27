"""Owner decision Q12: ``/stock/profile2``'s ``ipo`` field, read through the Finnhub provider.

Two different answers, kept apart because the tradeability cache treats them
differently: **no date** (``None`` -- the body has no usable ``ipo``: missing,
empty, malformed) is a vendor answer for this session, and **could not ask**
(:class:`FundamentalsError` -- a 403, a 5xx, a timeout, a body that is not an
object) caches nothing at all. Neither ever becomes an assumed IPO.

Replays the recorded ``profile2_*`` fixtures through the harness in
``test_finnhub_provider.py``; no test here makes a live call.
"""

import logging
from datetime import date
from typing import Any

import httpx
import pytest

from corollary.data.providers.finnhub import (
    FINNHUB_TOKEN_HEADER,
    FinnhubProvider,
    ipo_date_from_profile,
)
from corollary.data.providers.fundamentals import FundamentalsError
from corollary.ratelimit import FINNHUB_HOST, HostRateLimiter
from tests.data.providers.test_finnhub_provider import (  # noqa: F401 - fixtures
    TEST_CREDENTIALS,
    ProviderFactory,
    RecordingTransport,
    Served,
    _never_sleep,
    by_symbol,
    fixture_value,
    limiter,
    make_provider,
)

FINNHUB_LOGGER = "corollary.data.providers.finnhub"


def body(**fields: Any) -> str:
    import json

    return json.dumps({"ticker": "NEWCO", "name": "NewCo", "currency": "USD", **fields})


# --------------------------------------------------------------- a date


@pytest.mark.asyncio
async def test_the_recorded_apple_profile_carries_its_ipo_date(
    make_provider: ProviderFactory,
) -> None:
    """``profile2_aapl.json`` was recorded live and carries ``"ipo":"1980-12-12"``."""
    assert fixture_value("profile2_aapl", "ipo") == "1980-12-12"
    provider, transport = make_provider(by_symbol({"AAPL": "profile2_aapl"}))

    assert await provider.ipo_date("AAPL") == date(1980, 12, 12)

    [request] = transport.requests
    assert request.url.path == "/api/v1/stock/profile2"
    assert dict(request.url.params) == {"symbol": "AAPL"}
    assert request.headers[FINNHUB_TOKEN_HEADER] == TEST_CREDENTIALS.token


@pytest.mark.asyncio
async def test_the_symbol_is_normalised_and_a_blank_one_is_refused(
    make_provider: ProviderFactory,
) -> None:
    provider, transport = make_provider(by_symbol({"AAPL": "profile2_aapl"}))
    assert await provider.ipo_date(" aapl ") == date(1980, 12, 12)
    with pytest.raises(ValueError):
        await provider.ipo_date("  ")
    assert len(transport.requests) == 1


# ------------------------------------------------------------ no date


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["profile2_spy", "profile2_unknown"])
async def test_an_empty_profile_is_no_date_and_is_logged_with_its_rule(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture, fixture: str
) -> None:
    """A fund and an unknown symbol both answer ``{}`` -- no date, never an IPO."""
    provider, _ = make_provider(by_symbol({"NEWCO": fixture}))
    with caplog.at_level(logging.WARNING, logger=FINNHUB_LOGGER):
        assert await provider.ipo_date("NEWCO") is None

    [record] = [r for r in caplog.records if getattr(r, "event", None) == "ipo_date_unavailable"]
    assert record.symbol == "NEWCO"  # type: ignore[attr-defined]
    assert "never assumed" in record.rule  # type: ignore[attr-defined]
    assert "no ipo field" in record.cause  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "1980/12/12",
        "19801212",
        "1980-W50-5",
        "2026-02-30",
        "0000-00-00",
        "1980-12-12T00:00:00",
        12345,
        None,
        ["1980-12-12"],
    ],
)
async def test_an_empty_or_malformed_ipo_is_no_date(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture, raw: Any
) -> None:
    provider, _ = make_provider(by_symbol({"NEWCO": (200, body(ipo=raw))}))
    with caplog.at_level(logging.WARNING, logger=FINNHUB_LOGGER):
        assert await provider.ipo_date("NEWCO") is None
    assert any(getattr(r, "event", None) == "ipo_date_unavailable" for r in caplog.records)


def test_the_pure_reader_takes_only_an_iso_calendar_date() -> None:
    assert ipo_date_from_profile("NEWCO", {"ipo": "2026-09-15"}) == date(2026, 9, 15)
    assert ipo_date_from_profile("NEWCO", {"ipo": " 2026-09-15 "}) == date(2026, 9, 15)
    assert ipo_date_from_profile("NEWCO", {"ipo": "2026-9-15"}) is None
    assert ipo_date_from_profile("NEWCO", {}) is None


# --------------------------------------------------------- could not ask


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "text"),
    [
        (403, '{"error":"You don\'t have access to this resource."}'),
        (401, '{"error":"Invalid API key"}'),
        (429, '{"error":"API limit reached"}'),
        (500, '{"error":"boom"}'),
    ],
)
async def test_an_error_status_raises_rather_than_answering_no_date(
    make_provider: ProviderFactory, status: int, text: str
) -> None:
    provider, _ = make_provider(by_symbol({"NEWCO": (status, text)}))
    with pytest.raises(FundamentalsError, match=str(status)):
        await provider.ipo_date("NEWCO")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError],
)
async def test_a_timeout_or_transport_failure_raises(
    make_provider: ProviderFactory, exc: type[httpx.TransportError]
) -> None:
    def explode(request: httpx.Request) -> Served | None:
        raise exc("no answer in time", request=request)

    provider, _ = make_provider(explode)
    with pytest.raises(FundamentalsError, match="no answer in time"):
        await provider.ipo_date("NEWCO")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ['["1980-12-12"]', '"1980-12-12"', "null", "not json"])
async def test_a_body_that_is_not_an_object_raises(
    make_provider: ProviderFactory, text: str
) -> None:
    """A vendor fault is "could not ask", not "no date": nothing may be cached."""
    provider, _ = make_provider(by_symbol({"NEWCO": (200, text)}))
    with pytest.raises(FundamentalsError):
        await provider.ipo_date("NEWCO")


@pytest.mark.asyncio
async def test_an_error_body_echoing_the_token_is_redacted(
    make_provider: ProviderFactory,
) -> None:
    echoed = f'{{"error":"bad header {TEST_CREDENTIALS.token}"}}'
    provider, _ = make_provider(by_symbol({"NEWCO": (403, echoed)}))
    with pytest.raises(FundamentalsError) as caught:
        await provider.ipo_date("NEWCO")
    assert TEST_CREDENTIALS.token not in str(caught.value)


@pytest.mark.asyncio
async def test_an_ipo_lookup_is_metered_against_the_shared_finnhub_bucket() -> None:
    """One token from the same 60/min bucket market cap and news draw on."""
    shared = HostRateLimiter(clock=lambda: 0.0, sleep=_never_sleep)
    transport = RecordingTransport(by_symbol({"AAPL": "profile2_aapl"}))
    provider = FinnhubProvider(
        credentials=TEST_CREDENTIALS,
        client=httpx.AsyncClient(transport=transport),
        limiter=shared,
    )
    await provider.ipo_date("AAPL")
    assert shared.bucket_for(FINNHUB_HOST).available == pytest.approx(59.0)
