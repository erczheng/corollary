"""The FRED client, replayed from recorded responses. No live calls.

Fixtures under ``tests/fixtures/fred/`` were captured on 2026-09-24 against
the real API. The fixture file's **own bytes** are served through an
``httpx.MockTransport``, so the request the provider builds is exercised and a
value's digits are FRED's text, never a double's repr.

What is pinned here:

* values parse straight to ``Decimal``, and FRED's missing-observation marker
  ``"."`` becomes *no value* -- handled before any ``Decimal`` conversion,
  since ``Decimal(".")`` raises;
* the request is metered against the ``api.stlouisfed.org`` bucket;
* the key, which FRED only accepts in the query string, reaches no exception
  message and no log record -- including an ``httpx`` transport error, whose
  message quotes the request URL.
"""

import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from corollary.data.providers.fred import (
    FRED_API_KEY_ENV,
    FredCredentials,
    FredCredentialsError,
    FredError,
    FredObservation,
    FredProvider,
    parse_observations,
)
from corollary.data.providers.interface import ProviderError, RateLimitedError
from corollary.ratelimit import FRED_HOST, HostRateLimiter

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "fred"

#: Obviously fake, and long enough that a partial echo would still be caught.
#: Rule 6: no key material in tests or fixtures.
FAKE_KEY = "notarealfredkeynotarealfredkey00"


def fixture_body_text(name: str) -> str:
    """The exact text of a fixture's ``body``, sliced out of the file."""
    text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    marker = '"body":'
    start = text.index(marker) + len(marker)
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end]


def dgs3mo_body() -> dict[str, Any]:
    body: dict[str, Any] = json.loads(fixture_body_text("p3_observations_dgs3mo"))
    return body


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(f"the limiter slept {seconds}s in a test")


class Recorder:
    def __init__(self, respond: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result: httpx.Response = self._respond(request)
        return result


def _provider(respond: Any, limiter: HostRateLimiter | None = None) -> tuple[FredProvider, Recorder]:
    recorder = Recorder(respond)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    provider = FredProvider(
        credentials=FredCredentials(api_key=FAKE_KEY),
        client=client,
        limiter=limiter
        if limiter is not None
        else HostRateLimiter(clock=lambda: 0.0, sleep=_never_sleep),
    )
    return provider, recorder


def _serve_text(text: str, status: int = 200) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=text.encode("utf-8"))

    return respond


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    formatter = logging.Formatter()
    return "\n".join(
        part
        for record in caplog.records
        for part in (formatter.format(record), repr(record.__dict__))
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_the_recorded_dgs3mo_observations_parse_to_exact_decimals() -> None:
    observations = parse_observations("DGS3MO", dgs3mo_body())
    assert len(observations) == 10
    newest = observations[0]
    assert newest == FredObservation(
        series_id="DGS3MO", date=date(2026, 9, 22), value=Decimal("4.16")
    )
    assert all(isinstance(o.value, Decimal) for o in observations)
    # Newest first, whatever order the vendor chose.
    assert [o.date for o in observations] == sorted(
        (o.date for o in observations), reverse=True
    )


@pytest.mark.parametrize(
    "name,series,expected",
    [
        ("p3_observations_vixcls", "VIXCLS", Decimal("14.21")),
        ("p3_observations_bamlh0a0hym2", "BAMLH0A0HYM2", Decimal("2.68")),
    ],
)
def test_the_other_recorded_series_parse_the_same_way(
    name: str, series: str, expected: Decimal
) -> None:
    body = json.loads(fixture_body_text(name))
    newest = parse_observations(series, body)[0]
    assert newest.date == date(2026, 9, 22)
    assert newest.value == expected


def test_a_missing_observation_is_no_value_not_an_error() -> None:
    """FRED's ``"."`` means *no observation*. ``Decimal(".")`` raises.

    Built from the recorded fixture, since no recorded response happened to
    contain one: the newest observation is replaced by FRED's marker, exactly
    as a holiday row reads.
    """
    body = dgs3mo_body()
    body["observations"][0]["value"] = "."
    observations = parse_observations("DGS3MO", body)
    assert observations[0] == FredObservation(
        series_id="DGS3MO", date=date(2026, 9, 22), value=None
    )
    # The rest are untouched.
    assert observations[1].value is not None


@pytest.mark.parametrize("bad", ["", "abc", "NaN", "Infinity", "4.1.6", None, 4.16])
def test_a_value_that_is_neither_a_number_nor_the_marker_is_refused(bad: Any) -> None:
    body = dgs3mo_body()
    body["observations"][0]["value"] = bad
    with pytest.raises(FredError, match="DGS3MO"):
        parse_observations("DGS3MO", body)


def test_a_body_without_observations_is_refused() -> None:
    with pytest.raises(FredError, match="observations"):
        parse_observations("DGS3MO", {"error_code": 400})


def test_a_bad_date_is_refused() -> None:
    body = dgs3mo_body()
    body["observations"][0]["date"] = "22/09/2026"
    with pytest.raises(FredError):
        parse_observations("DGS3MO", body)


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observations_builds_the_documented_request_and_meters_it() -> None:
    limiter = HostRateLimiter(clock=lambda: 0.0, sleep=_never_sleep)
    provider, recorder = _provider(
        _serve_text(fixture_body_text("p3_observations_dgs3mo")), limiter
    )
    async with provider:
        observations = await provider.observations("DGS3MO", limit=10)

    assert observations[0].value == Decimal("4.16")
    [request] = recorder.requests
    assert request.url.host == FRED_HOST
    assert request.url.path == "/fred/series/observations"
    params = dict(request.url.params)
    assert params == {
        "series_id": "DGS3MO",
        "file_type": "json",
        "sort_order": "desc",
        "limit": "10",
        "api_key": FAKE_KEY,
    }
    # One token spent from FRED's bucket, and only from it.
    assert limiter.bucket_for(FRED_HOST).capacity - limiter.bucket_for(
        FRED_HOST
    ).available == pytest.approx(1)


@pytest.mark.asyncio
async def test_a_429_is_a_rate_limit_error() -> None:
    provider, _ = _provider(_serve_text('{"error_code":429}', status=429))
    async with provider:
        with pytest.raises(RateLimitedError):
            await provider.observations("DGS3MO")


@pytest.mark.asyncio
async def test_a_rejected_key_names_the_variable_and_never_the_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A body that echoes the key back, which is the worst case to redact.
    echo = json.dumps(
        {"error_code": 400, "error_message": f"Bad Request. api_key {FAKE_KEY} is invalid"}
    )
    provider, _ = _provider(_serve_text(echo, status=400))
    with caplog.at_level(logging.DEBUG):
        async with provider:
            with pytest.raises(FredError) as raised:
                await provider.observations("DGS3MO")
    assert isinstance(raised.value, ProviderError)
    assert FRED_API_KEY_ENV in str(raised.value)
    assert FAKE_KEY not in str(raised.value)
    assert FAKE_KEY not in _everything_logged(caplog)


@pytest.mark.asyncio
async def test_a_transport_error_quoting_the_url_is_scrubbed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``httpx`` builds transport errors from the request -- URL, query and all."""

    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused for {request.url}", request=request)

    provider, recorder = _provider(respond)
    with caplog.at_level(logging.DEBUG):
        async with provider:
            with pytest.raises(FredError) as raised:
                await provider.observations("DGS3MO")
    assert FAKE_KEY in str(recorder.requests[0].url)  # the key really was sent
    assert FAKE_KEY not in str(raised.value)
    assert FAKE_KEY not in repr(raised.value)
    assert FAKE_KEY not in _everything_logged(caplog)
    # ``from exc`` keeps the cause for debugging; the cause itself is httpx's
    # and is never logged by this module.
    assert "DGS3MO" in str(raised.value)


def test_the_credentials_never_print_the_key() -> None:
    credentials = FredCredentials(api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(credentials)
    assert FAKE_KEY not in str(credentials)


def test_a_missing_key_is_a_credentials_error_naming_the_variable() -> None:
    with pytest.raises(FredCredentialsError, match=FRED_API_KEY_ENV):
        FredCredentials.from_env({})
    with pytest.raises(FredCredentialsError):
        FredCredentials.from_env({FRED_API_KEY_ENV: "   "})


def test_the_key_is_read_from_the_given_environment() -> None:
    assert FredCredentials.from_env({FRED_API_KEY_ENV: FAKE_KEY}).api_key == FAKE_KEY


@pytest.mark.asyncio
async def test_httpx_request_log_line_is_scrubbed_and_the_filter_leaves_on_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """httpx logs every request URL at INFO -- found by this file's first run."""
    httpx_logger = logging.getLogger("httpx")
    before = list(httpx_logger.filters)
    provider, _ = _provider(_serve_text(fixture_body_text("p3_observations_dgs3mo")))
    with caplog.at_level(logging.INFO, logger="httpx"):
        async with provider:
            await provider.observations("DGS3MO")
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert lines, "httpx logged no request line; this test would prove nothing"
    assert all(FAKE_KEY not in line for line in lines)
    assert any("api_key=<redacted>" in line for line in lines)
    assert httpx_logger.filters == before
