"""OpenFIGI's ISIN mapping -- every test here is SYNTHETIC.

No live call is made: the responses come from
``tests/fixtures/openfigi_synthetic/`` (hand-built, labelled SYNTHETIC) or are
built inline, and are served through an ``httpx.MockTransport`` so the request
the provider actually builds -- URL, method, body, headers -- is what is
asserted on. The live recording is ``tests/fixtures/openfigi/``, read by
``test_openfigi_recorded.py``; it confirmed the response shape assumed here.

What is pinned:

* the request body's exact shape -- a JSON list of ``{"idType": "ID_ISIN",
  "idValue": ...}`` and nothing else (spec Q21, owner condition 2);
* batching at the documented job caps -- 10 keyless, 100 keyed;
* the key travels only as ``X-OPENFIGI-APIKEY``, only when set, and never
  appears in a log record or an error;
* 429 is :class:`RateLimitedError`, never retried; other failures are
  :class:`OpenFigiError` with no ``httpx`` exception chained;
* ``data`` / ``warning`` / ``error`` per job;
* ISINs are shape-checked before any request;
* the bucket stays under the documented ceiling in any rolling window.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from corollary.data.providers.interface import ProviderError, RateLimitedError
from corollary.data.providers.openfigi import (
    KEYED_JOBS_PER_REQUEST,
    KEYLESS_JOBS_PER_REQUEST,
    OPENFIGI_API_KEY_ENV,
    OPENFIGI_KEY_HEADER,
    FigiRecord,
    OpenFigiCredentials,
    OpenFigiError,
    OpenFigiProvider,
    mapping_jobs,
    validate_isin,
)
from corollary.ratelimit import (
    DEFAULT_PER_HOST_BUDGETS,
    OPENFIGI_BUCKET_REQUESTS_PER_MINUTE,
    OPENFIGI_HOST,
    OPENFIGI_KEYED_REQUESTS_PER_6_SECONDS,
    OPENFIGI_KEYLESS_REQUESTS_PER_MINUTE,
    HostRateLimiter,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "openfigi_synthetic"
    / "mapping.synthetic.json"
)

#: Rule 6: an obviously fake key, so a leak is greppable and harmless.
FAKE_KEY = "SYNTHETIC-OPENFIGI-KEY-0123456789"


def load_fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["_synthetic"].startswith("SYNTHETIC")
    return data


def isin(n: int) -> str:
    """A shape-valid synthetic ISIN, distinct per ``n``."""
    return f"XS{n:09d}0"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


Handler = Callable[[httpx.Request], httpx.Response]


def echo_warning(request: httpx.Request) -> httpx.Response:
    """Answer every job with "no match", in order -- the minimal valid reply."""
    jobs = json.loads(request.content)
    return httpx.Response(200, json=[{"warning": "No identifier found."} for _ in jobs])


def make_provider(
    handler: Handler,
    *,
    api_key: str | None = None,
    seen: list[httpx.Request] | None = None,
    limiter: HostRateLimiter | None = None,
) -> OpenFigiProvider:
    def recording(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    clock = FakeClock()
    return OpenFigiProvider(
        credentials=OpenFigiCredentials(api_key=api_key),
        transport=httpx.MockTransport(recording),
        limiter=limiter or HostRateLimiter(clock=clock, sleep=clock.sleep),
    )


# ------------------------------------------------------------------ request


@pytest.mark.asyncio
async def test_the_request_is_one_post_to_the_mapping_url_with_the_pinned_body() -> None:
    """Owner condition 2 of Q21: the body is a list of ISIN jobs and nothing else.

    No ``exchCode`` and no other filter: the acceptance rule filters to US
    listings on the *response* side (Q17), so the request asks only "what is
    this ISIN". Exact equality, so a new field fails here.
    """
    fixture = load_fixture()
    seen: list[httpx.Request] = []
    isins = [job["idValue"] for job in fixture["request"]]
    async with make_provider(echo_warning, seen=seen) as provider:
        await provider.map_isins(isins)
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.openfigi.com/v3/mapping"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body == fixture["request"]
    assert body == [{"idType": "ID_ISIN", "idValue": i} for i in isins]
    assert all(set(job) == {"idType", "idValue"} for job in body)


@pytest.mark.asyncio
async def test_a_redirect_is_never_followed_and_the_key_goes_nowhere_else() -> None:
    """Spec Q21: the exempt call passes ``follow_redirects=False``.

    A 307 to another host would replay the POST -- key header included -- at
    that host. Not followed, it is one request, refused as a non-200, and the
    redirect target never sees anything.
    """
    seen: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            307, headers={"Location": "https://paper-api.example.invalid/v2/orders"}
        )

    async with make_provider(redirect, api_key=FAKE_KEY, seen=seen) as provider:
        with pytest.raises(OpenFigiError) as raised:
            await provider.map_isins([isin(1)])
    assert len(seen) == 1
    assert seen[0].url.host == "api.openfigi.com"
    assert "307" in str(raised.value)
    assert FAKE_KEY not in str(raised.value)


def test_the_provider_takes_a_transport_never_a_client() -> None:
    """Spec Q21 / finding 3: there is no way to hand the provider a client."""
    with pytest.raises(TypeError):
        OpenFigiProvider(  # type: ignore[call-arg]
            credentials=OpenFigiCredentials(),
            client=None,
        )


class _ForwardingMock(httpx.MockTransport):
    """A MockTransport subclass -- which could override ``handle_async_request``."""


@pytest.mark.parametrize(
    "transport",
    [
        pytest.param(lambda: httpx.AsyncHTTPTransport(), id="real_transport"),
        pytest.param(lambda: _ForwardingMock(echo_warning), id="mock_subclass"),
        pytest.param(lambda: object(), id="not_a_transport"),
    ],
)
def test_the_provider_refuses_any_transport_but_a_plain_mock(
    transport: Callable[[], object],
) -> None:
    """OF-U1b finding 5: a transport sees the exempt request and can send it anywhere.

    Production and the recorder pass none; tests pass an ``httpx.MockTransport``.
    Anything else -- a real transport, or a subclass that could override
    ``handle_async_request`` -- is refused at construction, through either
    entry point.
    """
    with pytest.raises(TypeError, match="MockTransport"):
        OpenFigiProvider(
            credentials=OpenFigiCredentials(),
            transport=transport(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="MockTransport"):
        OpenFigiProvider.from_env(env={}, transport=transport())


@pytest.mark.asyncio
async def test_the_provider_accepts_no_transport_and_a_plain_mock() -> None:
    async with OpenFigiProvider.from_env(env={}) as keyless:
        assert keyless.jobs_per_request == 10
    async with OpenFigiProvider.from_env(
        env={}, transport=httpx.MockTransport(echo_warning)
    ) as mocked:
        assert mocked.jobs_per_request == 10


def test_mapping_jobs_is_the_pinned_shape() -> None:
    assert mapping_jobs(["IE000S9YS762"]) == [
        {"idType": "ID_ISIN", "idValue": "IE000S9YS762"}
    ]


@pytest.mark.asyncio
async def test_no_key_means_no_key_header() -> None:
    seen: list[httpx.Request] = []
    async with make_provider(echo_warning, seen=seen) as provider:
        await provider.map_isins([isin(1)])
    assert OPENFIGI_KEY_HEADER.lower() not in {k.lower() for k in seen[0].headers}


@pytest.mark.asyncio
async def test_a_key_travels_only_as_its_header() -> None:
    seen: list[httpx.Request] = []
    async with make_provider(echo_warning, api_key=FAKE_KEY, seen=seen) as provider:
        await provider.map_isins([isin(1)])
    request = seen[0]
    assert request.headers[OPENFIGI_KEY_HEADER] == FAKE_KEY
    assert FAKE_KEY not in str(request.url)
    assert FAKE_KEY.encode() not in request.content
    others = {k: v for k, v in request.headers.items() if k.lower() != "x-openfigi-apikey"}
    assert FAKE_KEY not in json.dumps(others)


# ----------------------------------------------------------------- batching


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "count", "expected_sizes"),
    [
        (None, 10, [10]),
        (None, 11, [10, 1]),
        (None, 29, [10, 10, 9]),
        (FAKE_KEY, 100, [100]),
        (FAKE_KEY, 101, [100, 1]),
    ],
    ids=["keyless-10", "keyless-11", "keyless-29", "keyed-100", "keyed-101"],
)
async def test_jobs_are_batched_at_the_documented_cap(
    api_key: str | None, count: int, expected_sizes: list[int]
) -> None:
    assert (KEYLESS_JOBS_PER_REQUEST, KEYED_JOBS_PER_REQUEST) == (10, 100)
    seen: list[httpx.Request] = []
    isins = [isin(n) for n in range(count)]
    async with make_provider(echo_warning, api_key=api_key, seen=seen) as provider:
        results = await provider.map_isins(isins)
    sizes = [len(json.loads(r.content)) for r in seen]
    assert sizes == expected_sizes
    sent = [job["idValue"] for r in seen for job in json.loads(r.content)]
    assert sent == isins
    assert list(results) == isins


@pytest.mark.asyncio
async def test_duplicates_are_asked_once_and_an_empty_input_asks_nothing() -> None:
    seen: list[httpx.Request] = []
    async with make_provider(echo_warning, seen=seen) as provider:
        assert await provider.map_isins([]) == {}
        assert seen == []
        results = await provider.map_isins([isin(1), isin(1), isin(2)])
    assert [job["idValue"] for job in json.loads(seen[0].content)] == [isin(1), isin(2)]
    assert list(results) == [isin(1), isin(2)]


@pytest.mark.asyncio
async def test_map_isin_batches_returns_each_requests_jobs_and_decoded_reply() -> None:
    """The recorder's seam: what was sent, what came back, and the parse of it.

    One request per batch, exactly as :meth:`map_isins` makes them (that
    method is built on this one), and each batch carries the jobs that went
    on the wire and the body OpenFIGI answered with -- so a fixture can store
    both without the recorder ever making a request of its own.
    """
    seen: list[httpx.Request] = []
    isins = [isin(n) for n in range(29)]
    async with make_provider(echo_warning, seen=seen) as provider:
        batches = await provider.map_isin_batches(isins)
    assert [len(b.jobs) for b in batches] == [10, 10, 9]
    assert len(seen) == 3
    for batch, request in zip(batches, seen):
        assert list(batch.jobs) == json.loads(request.content)
        assert batch.payload == [{"warning": "No identifier found."}] * len(batch.jobs)
        assert list(batch.results) == list(batch.isins)
        assert all(r.warning == "No identifier found." for r in batch.results.values())
    assert [i for b in batches for i in b.isins] == isins


@pytest.mark.asyncio
async def test_map_isin_batches_validates_before_any_request_and_dedupes() -> None:
    seen: list[httpx.Request] = []
    async with make_provider(echo_warning, seen=seen) as provider:
        assert await provider.map_isin_batches([]) == []
        with pytest.raises(ValueError):
            await provider.map_isin_batches([isin(1), "not-an-isin"])
        with pytest.raises(TypeError):
            await provider.map_isin_batches(isin(1))
        assert seen == []
        batches = await provider.map_isin_batches([isin(1), isin(1)])
    assert [b.isins for b in batches] == [(isin(1),)]


# ------------------------------------------------------------------ parsing


@pytest.mark.asyncio
async def test_data_warning_and_error_are_read_per_job() -> None:
    fixture = load_fixture()
    isins = [job["idValue"] for job in fixture["request"]]

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture["response"])

    async with make_provider(reply) as provider:
        results = await provider.map_isins(isins)

    linde = results["IE000S9YS762"]
    assert linde.matched and linde.warning is None and linde.error is None
    assert linde.records == (
        FigiRecord(
            figi="BBG000SYNTH1",
            ticker="LIN",
            exch_code="US",
            market_sector="Equity",
            security_type="Common Stock",
            security_type2="Common Stock",
            composite_figi="BBG000SYNTH1",
            share_class_figi="BBG001SYNTH1",
            name="SYNTHETIC LINDE PLC",
            security_description="LIN",
        ),
    )
    # Several candidates are all kept, in order: choosing is the acceptance rule's job.
    aon = results["IE00BLP1HW54"]
    assert [(r.ticker, r.exch_code) for r in aon.records] == [("AON", "US"), ("4VK", "GR")]
    assert aon.records[1].security_description is None

    no_match = results["US0000000002"]
    assert not no_match.matched
    assert no_match.records == () and no_match.warning == "No identifier found."
    assert no_match.error is None

    refused = results["XS0000000009"]
    assert not refused.matched
    assert refused.records == () and refused.error == "Invalid idValue format."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},  # not a list
        [],  # one job sent, zero results
        [{"warning": "x"}, {"warning": "y"}],  # one job sent, two results
        ["not an object"],
        [{}],  # none of data / warning / error
        [{"data": [], "warning": "both"}],
        [{"data": "not a list"}],
        [{"data": ["not an object"]}],
        [{"data": [{"figi": 7}]}],  # a field that is not a string
        [{"warning": 7}],
    ],
)
async def test_a_malformed_reply_refuses_the_whole_batch(payload: Any) -> None:
    """Fail closed: a half-read reply would be a partial answer presented as whole."""

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with make_provider(reply) as provider:
        with pytest.raises(OpenFigiError):
            await provider.map_isins([isin(1)])


# ------------------------------------------------------------------- errors


@pytest.mark.asyncio
async def test_429_is_rate_limited_and_never_retried() -> None:
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"ratelimit-reset": "30"}, text="Too Many Requests")

    async with make_provider(reply, seen=seen) as provider:
        with pytest.raises(RateLimitedError) as caught:
            await provider.map_isins([isin(n) for n in range(25)])
    assert len(seen) == 1, "a 429 must stop the run, not retry or carry on"
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [201, 400, 401, 403, 413, 500, 503])
async def test_any_other_non_200_is_a_provider_error(status: int) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"refused {FAKE_KEY}")

    async with make_provider(reply, api_key=FAKE_KEY) as provider:
        with pytest.raises(OpenFigiError) as caught:
            await provider.map_isins([isin(1)])
    assert not isinstance(caught.value, RateLimitedError)
    assert isinstance(caught.value, ProviderError)
    assert str(status) in str(caught.value)
    assert FAKE_KEY not in str(caught.value)


@pytest.mark.asyncio
async def test_a_transport_failure_chains_no_httpx_exception() -> None:
    """The httpx exception carries the request, headers and all -- it must not ride along."""

    def reply(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {FAKE_KEY}", request=request)

    async with make_provider(reply, api_key=FAKE_KEY) as provider:
        with pytest.raises(OpenFigiError) as caught:
            await provider.map_isins([isin(1)])
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "ConnectError" in str(caught.value)
    assert FAKE_KEY not in str(caught.value)


@pytest.mark.asyncio
async def test_an_undecodable_body_is_a_provider_error_with_nothing_chained() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with make_provider(reply) as provider:
        with pytest.raises(OpenFigiError) as caught:
            await provider.map_isins([isin(1)])
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.asyncio
async def test_the_key_never_reaches_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    async with make_provider(echo_warning, api_key=FAKE_KEY) as provider:
        await provider.map_isins([isin(n) for n in range(150)])
    async with make_provider(reply, api_key=FAKE_KEY) as provider:
        with pytest.raises(OpenFigiError):
            await provider.map_isins([isin(1)])
    assert caplog.records, "nothing was logged, so this proved nothing"
    assert FAKE_KEY not in caplog.text
    for record in caplog.records:
        assert FAKE_KEY not in record.getMessage()


def test_credentials_hide_the_key_and_read_it_optionally() -> None:
    assert OPENFIGI_API_KEY_ENV == "OPENFIGI_API_KEY"
    assert FAKE_KEY not in repr(OpenFigiCredentials(api_key=FAKE_KEY))
    assert FAKE_KEY not in str(OpenFigiCredentials(api_key=FAKE_KEY))
    assert OpenFigiCredentials.from_env({}).api_key is None
    assert OpenFigiCredentials.from_env({OPENFIGI_API_KEY_ENV: "   "}).api_key is None
    assert OpenFigiCredentials.from_env({OPENFIGI_API_KEY_ENV: FAKE_KEY}).api_key == FAKE_KEY
    assert OpenFigiProvider.from_env({}).jobs_per_request == KEYLESS_JOBS_PER_REQUEST
    keyed = OpenFigiProvider.from_env({OPENFIGI_API_KEY_ENV: FAKE_KEY})
    assert keyed.jobs_per_request == KEYED_JOBS_PER_REQUEST


# --------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "value", ["IE000S9YS762", "US0378331005", "BMG0450A1053", "XS0000000009"]
)
def test_a_shape_valid_isin_is_accepted(value: str) -> None:
    assert validate_isin(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "US037833100",  # 11 chars
        "US03783310055",  # 13 chars
        "us0378331005",  # lower-case country
        "1S0378331005",  # digit in the country code
        "US037833100A",  # letter as the final digit
        "US03783310-5",  # punctuation
        " US0378331005",  # whitespace
        "US037833100５",  # a non-ASCII digit
        "ＵS0378331005",  # a non-ASCII letter
        "Linde plc",  # a name is never an identifier
    ],
)
def test_a_malformed_isin_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        validate_isin(value)


@pytest.mark.asyncio
async def test_one_bad_isin_refuses_the_call_before_any_request() -> None:
    seen: list[httpx.Request] = []
    async with make_provider(echo_warning, seen=seen) as provider:
        with pytest.raises(ValueError):
            await provider.map_isins([isin(1), "Linde plc", isin(2)])
        with pytest.raises(TypeError):
            await provider.map_isins("IE000S9YS762")  # type: ignore[arg-type]
    assert seen == []


# ------------------------------------------------------------------- bucket


def test_the_bucket_is_registered_for_the_openfigi_host() -> None:
    assert OPENFIGI_HOST == "api.openfigi.com"
    assert (OPENFIGI_KEYLESS_REQUESTS_PER_MINUTE, OPENFIGI_KEYED_REQUESTS_PER_6_SECONDS) == (
        25,
        25,
    )
    budget = DEFAULT_PER_HOST_BUDGETS[OPENFIGI_HOST]
    assert budget.requests == OPENFIGI_BUCKET_REQUESTS_PER_MINUTE == 12
    assert budget.window_seconds == 60.0


@pytest.mark.asyncio
async def test_no_rolling_window_exceeds_the_documented_ceiling() -> None:
    """Worst case from a full bucket: C + r*T, measured, against both ceilings."""
    clock = FakeClock()
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    stamps: list[float] = []
    for _ in range(120):
        await limiter.acquire(OPENFIGI_HOST)
        stamps.append(clock.now)

    def worst(window: float) -> int:
        return max(
            sum(1 for s in stamps if start <= s <= start + window + 1e-9) for start in stamps
        )

    assert worst(60.0) == 24
    assert worst(60.0) <= OPENFIGI_KEYLESS_REQUESTS_PER_MINUTE
    assert worst(6.0) <= OPENFIGI_KEYED_REQUESTS_PER_6_SECONDS


@pytest.mark.asyncio
async def test_each_request_spends_one_token_from_the_openfigi_bucket() -> None:
    clock = FakeClock()
    limiter = HostRateLimiter(clock=clock, sleep=clock.sleep)
    async with make_provider(echo_warning, limiter=limiter) as provider:
        await provider.map_isins([isin(n) for n in range(29)])
    bucket = limiter.bucket_for(OPENFIGI_HOST)
    assert bucket.available == pytest.approx(OPENFIGI_BUCKET_REQUESTS_PER_MINUTE - 3)
