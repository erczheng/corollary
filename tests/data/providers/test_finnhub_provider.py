"""Replay recorded Finnhub responses. No test here makes a live call.

Fixtures under ``tests/fixtures/finnhub/`` were captured by
``tests/fixtures/record_finnhub.py`` against the real account and are replayed
through an ``httpx.MockTransport``, so the request the provider actually
builds -- path, query, headers -- is exercised rather than bypassed.

The replay harness is a local one rather than the Alpaca conftest's, and
deliberately so: that one is bound to ``tests/fixtures/alpaca/`` and to
Alpaca's ``{"status_code", "body"}`` envelope through four module-level
helpers, and widening all four to take a directory would put a second
vendor's concerns into the first vendor's fixtures for the sake of thirty
lines. What is *not* duplicated is the property those thirty lines exist for:
the fixture file's **own bytes** are served, so a number's last digit is the
vendor's text and not Python's ``repr`` of a double.
"""

import json
import logging
from decimal import ROUND_DOWN, Decimal, localcontext
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from pydantic import TypeAdapter

import corollary.data.providers.finnhub as finnhub_module
from corollary.api.schemas import JsonMoney
from corollary.data.providers.finnhub import (
    FINNHUB_API_KEY_ENV,
    FINNHUB_TOKEN_HEADER,
    MILLION,
    FinnhubCredentials,
    FinnhubCredentialsError,
    FinnhubProvider,
    market_cap_from_profile,
)
from corollary.data.providers.fundamentals import MarketCap, MarketCapStatus
from corollary.ratelimit import FINNHUB_HOST, HostRateLimiter
from corollary.wire import as_decimal, decode_json

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "finnhub"

#: Obviously fake. Rule 6: no key material in tests or fixtures.
TEST_CREDENTIALS = FinnhubCredentials(token="not-a-real-finnhub-token")

Served = str | tuple[int, str]
Route = Callable[[httpx.Request], Served | None]


def fixture_body_bytes(name: str) -> bytes:
    """The exact bytes of a fixture's ``body``, sliced out of the file.

    ``raw_decode`` reports where the value ended, which is what makes an
    exact slice possible without re-serialising. Re-serialising would put
    every number through Python's float formatter on the way to the wire --
    the round trip this avoids.
    """
    text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    marker = '"body":'
    start = text.index(marker) + len(marker)
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end].encode("utf-8")


def fixture_status(name: str) -> int:
    text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    payload: dict[str, Any] = json.loads(text)
    return int(payload["status_code"])


def fixture_value(name: str, key: str) -> Any:
    """One field of a recorded body, with numbers as exact ``Decimal``."""
    body: dict[str, Any] = json.loads(
        fixture_body_bytes(name).decode("utf-8"), parse_float=Decimal
    )
    return body.get(key)


class RecordingTransport(httpx.MockTransport):
    """A mock transport that logs what it served.

    Returning ``None`` from ``route`` is a test failure rather than a 404:
    an unrouted request means the provider built a URL nobody predicted,
    which is exactly what these tests exist to catch.
    """

    def __init__(self, route: Route) -> None:
        self.requests: list[httpx.Request] = []
        self._route = route
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        served = self._route(request)
        if served is None:
            raise AssertionError(
                f"no fixture routed for {request.method} {request.url}"
            )
        if isinstance(served, tuple):
            status, body = served
            return httpx.Response(
                status_code=status,
                content=body.encode("utf-8"),
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(
            status_code=fixture_status(served),
            content=fixture_body_bytes(served),
            headers={"content-type": "application/json"},
            request=request,
        )

    def symbols_requested(self) -> list[str]:
        return [request.url.params["symbol"] for request in self.requests]


def by_symbol(mapping: dict[str, Served]) -> Route:
    """Serve a different fixture per requested symbol."""

    def choose(request: httpx.Request) -> Served | None:
        return mapping.get(request.url.params.get("symbol", ""))

    return choose


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(
        f"a replay test waited {seconds}s on the rate limiter; the fixture "
        "budget should be large enough that this never happens"
    )


@pytest.fixture
def limiter() -> HostRateLimiter:
    """Generous everywhere, with a clock that never advances.

    ``per_host={}`` opts out of the documented table: these tests are about
    mapping, and the 60/min budget has its own test below.
    """
    return HostRateLimiter(
        requests_per_minute=10_000,
        per_host={},
        clock=lambda: 0.0,
        sleep=_never_sleep,
    )


@pytest.fixture
def make_provider(
    limiter: HostRateLimiter,
) -> Callable[..., tuple[FinnhubProvider, RecordingTransport]]:
    def build(
        route: Route,
        *,
        credentials: FinnhubCredentials = TEST_CREDENTIALS,
        **kwargs: Any,
    ) -> tuple[FinnhubProvider, RecordingTransport]:
        transport = RecordingTransport(route)
        client = httpx.AsyncClient(transport=transport)
        provider = FinnhubProvider(
            credentials=credentials, client=client, limiter=limiter, **kwargs
        )
        return provider, transport

    return build


ProviderFactory = Callable[..., tuple[FinnhubProvider, RecordingTransport]]


# --------------------------------------------------------------------------
# The figure, and its unit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_company_reports_its_market_cap_in_whole_dollars(
    make_provider: ProviderFactory,
) -> None:
    """Finnhub reports **millions**; this interface reports dollars.

    The band is the real assertion. A units error here is wrong by a factor
    of a million in either direction, and both directions produce a number
    that still renders -- ``$4.85M`` for Apple, or ``$4.85 quintillion`` --
    so the test that catches it is the one that says what a mega cap can
    plausibly be worth.
    """
    provider, _ = make_provider(by_symbol({"AAPL": "profile2_aapl"}))
    caps = await provider.market_caps(["AAPL"])

    apple = caps["AAPL"]
    assert apple.status is MarketCapStatus.REPORTED
    assert apple.value is not None
    assert Decimal("1e12") < apple.value < Decimal("2e13")
    assert apple.value == (
        fixture_value("profile2_aapl", "marketCapitalization") * Decimal(1_000_000)
    ).quantize(Decimal(1))
    assert apple.value == apple.value.to_integral_value(), "whole dollars"


@pytest.mark.asyncio
async def test_the_figure_is_an_exact_decimal_and_never_a_float(
    make_provider: ProviderFactory,
) -> None:
    """``4849208.193522442`` survives the wire as text, not as a double.

    ``decode_json`` parses with ``parse_float=Decimal``, so the digits come
    from the fixture's own bytes. Multiplying by ``Decimal(1_000_000)``
    rather than ``1e6`` keeps it that way -- the float form would raise at
    ``as_decimal`` if it ever reached one, and would silently lose digits
    here if it did not.
    """
    provider, _ = make_provider(by_symbol({"AAPL": "profile2_aapl"}))
    apple = (await provider.market_caps(["AAPL"]))["AAPL"]

    assert isinstance(apple.value, Decimal)
    assert apple.value == Decimal("4849208193522")

    # The digits are the fixture's, not a double's. Rounded to whole dollars
    # at construction -- see MarketCap.reported -- so the assertion is that
    # the *integer part* survived intact, which a float round trip would not
    # guarantee at this magnitude and which 1e6 would not give at all.
    exact = Decimal("4849208.193522442") * Decimal(1_000_000)
    assert exact == Decimal("4849208193522.442000000")
    assert apple.value == exact.to_integral_value()


# --------------------------------------------------------------------------
# The three absences
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fund_has_no_market_cap_and_that_is_an_answer(
    make_provider: ProviderFactory,
) -> None:
    """SPY's recorded profile is ``{}``. Null, never zero.

    CLAUDE.md: *"coerced to zero it would sort SPY to the top of an ascending
    list and state, in a column of dollars, that a fund is worth nothing."*
    """
    provider, _ = make_provider(by_symbol({"SPY": "profile2_spy"}))
    spy = (await provider.market_caps(["SPY"]))["SPY"]

    assert spy.status is MarketCapStatus.NOT_FILED
    assert spy.value is None
    assert spy.is_answered is True


@pytest.mark.asyncio
async def test_an_unknown_symbol_answers_exactly_as_a_fund_does(
    make_provider: ProviderFactory,
) -> None:
    """Recorded: ``ZZZZ`` returns ``{}`` with HTTP 200, same as SPY.

    Pinned because it is a claim about the vendor that the code depends on:
    there is no 404 to distinguish "no such symbol" from "no market cap", so
    both are read as ``NOT_FILED`` and neither is treated as a fault.
    """
    provider, _ = make_provider(by_symbol({"ZZZZ": "profile2_unknown"}))
    unknown = (await provider.market_caps(["ZZZZ"]))["ZZZZ"]

    assert unknown.status is MarketCapStatus.NOT_FILED
    assert unknown.value is None


@pytest.mark.asyncio
async def test_a_failed_fetch_is_unavailable_and_not_a_fund(
    make_provider: ProviderFactory,
) -> None:
    """A 500 is not an absence of market cap. Losing that costs the retry."""
    provider, _ = make_provider(by_symbol({"AAPL": (500, '{"error":"boom"}')}))
    apple = (await provider.market_caps(["AAPL"]))["AAPL"]

    assert apple.status is MarketCapStatus.UNAVAILABLE
    assert apple.value is None
    assert apple.is_answered is False
    assert "500" in apple.note


@pytest.mark.asyncio
async def test_a_transport_failure_is_unavailable_rather_than_an_exception(
    make_provider: ProviderFactory,
) -> None:
    def explode(request: httpx.Request) -> Served | None:
        raise httpx.ConnectError("no route to host", request=request)

    provider, _ = make_provider(explode)
    apple = (await provider.market_caps(["AAPL"]))["AAPL"]

    assert apple.status is MarketCapStatus.UNAVAILABLE
    assert "no route to host" in apple.note


@pytest.mark.asyncio
async def test_a_non_usd_profile_is_unavailable_not_a_dollar_figure(
    make_provider: ProviderFactory,
) -> None:
    """A column headed in dollars must not carry euros.

    Nothing in the current universe is a foreign listing, so this is
    insurance rather than a live path -- but the failure it prevents is a
    number that is wrong by an FX rate and looks completely ordinary.
    """
    body = '{"ticker":"SAP","currency":"EUR","marketCapitalization":250000.5}'
    provider, _ = make_provider(by_symbol({"SAP": (200, body)}))
    sap = (await provider.market_caps(["SAP"]))["SAP"]

    assert sap.status is MarketCapStatus.UNAVAILABLE
    assert "EUR" in sap.note


@pytest.mark.asyncio
async def test_a_market_cap_of_zero_is_read_as_no_market_cap(
    make_provider: ProviderFactory,
) -> None:
    """Zero is the vendor's other way of saying nothing, and it is not a price.

    Serving ``0`` would put a company at the bottom of a dollar column and
    claim it is worth nothing -- the same error the fund null exists to
    prevent, one row lower down.
    """
    body = '{"ticker":"NEW","currency":"USD","marketCapitalization":0}'
    provider, _ = make_provider(by_symbol({"NEW": (200, body)}))
    new = (await provider.market_caps(["NEW"]))["NEW"]

    assert new.status is MarketCapStatus.NOT_FILED
    assert new.value is None


# --------------------------------------------------------------------------
# Logging: the two absences must not look alike
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failure_is_logged_with_its_cause_and_a_fund_is_not(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 8, applied to an absence rather than a rejection.

    The wire says ``null`` for both. The log has to say which, or the first
    time the column empties nobody can tell an outage from an ETF.
    """
    provider, _ = make_provider(
        by_symbol({"SPY": "profile2_spy", "AAPL": (503, '{"error":"down"}')})
    )
    with caplog.at_level(logging.DEBUG, logger="corollary.data.providers.finnhub"):
        await provider.market_caps(["SPY", "AAPL"])

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.__dict__["symbol"] for r in warnings] == ["AAPL"]
    assert warnings[0].__dict__["event"] == "market_cap_unavailable"
    assert "503" in warnings[0].__dict__["cause"]

    quiet = [
        r for r in caplog.records if r.__dict__.get("event") == "market_cap_not_filed"
    ]
    assert [r.__dict__["symbol"] for r in quiet] == ["SPY"]
    assert all(r.levelno < logging.WARNING for r in quiet)


@pytest.mark.asyncio
async def test_one_symbols_failure_does_not_cost_the_others_their_answers(
    make_provider: ProviderFactory,
) -> None:
    provider, _ = make_provider(
        by_symbol({"AAPL": "profile2_aapl", "BAD": (500, "{}")})
    )
    caps = await provider.market_caps(["AAPL", "BAD"])

    assert set(caps) == {"AAPL", "BAD"}
    assert caps["AAPL"].status is MarketCapStatus.REPORTED
    assert caps["BAD"].status is MarketCapStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_every_requested_symbol_gets_an_entry_and_duplicates_collapse(
    make_provider: ProviderFactory,
) -> None:
    """A cache downstream must be able to key on what it asked for."""
    provider, transport = make_provider(by_symbol({"AAPL": "profile2_aapl"}))
    caps = await provider.market_caps(["AAPL", "AAPL", " "])

    assert set(caps) == {"AAPL"}
    assert transport.symbols_requested() == ["AAPL"]


@pytest.mark.asyncio
async def test_no_symbols_makes_no_requests(make_provider: ProviderFactory) -> None:
    provider, transport = make_provider(by_symbol({}))
    assert await provider.market_caps([]) == {}
    assert transport.requests == []


# --------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_token_travels_in_a_header_and_never_in_the_query_string(
    make_provider: ProviderFactory,
) -> None:
    """A query string turns up in access logs, proxy logs and tracebacks."""
    provider, transport = make_provider(by_symbol({"AAPL": "profile2_aapl"}))
    await provider.market_caps(["AAPL"])

    request = transport.requests[0]
    assert request.headers[FINNHUB_TOKEN_HEADER] == TEST_CREDENTIALS.token
    assert "token" not in dict(request.url.params)
    assert TEST_CREDENTIALS.token not in str(request.url)


@pytest.mark.asyncio
async def test_an_error_body_that_echoes_the_token_is_redacted(
    make_provider: ProviderFactory,
) -> None:
    """Nothing Finnhub sends today quotes the header back.

    This bounds a channel rather than patching a known hole: a WAF or a
    corporate proxy that reflects request headers into an error page writes
    the credential into a body this provider then quotes into a note, and
    from there into a log. Rule 6 covers log output.
    """
    leak = f'{{"error":"bad token {TEST_CREDENTIALS.token}"}}'
    provider, _ = make_provider(by_symbol({"AAPL": (400, leak)}))
    apple = (await provider.market_caps(["AAPL"]))["AAPL"]

    assert TEST_CREDENTIALS.token not in apple.note
    assert "<redacted>" in apple.note


@pytest.mark.asyncio
async def test_a_401_points_at_the_key_rather_than_at_the_code(
    make_provider: ProviderFactory,
) -> None:
    provider, _ = make_provider(by_symbol({"AAPL": (401, '{"error":"invalid"}')}))
    apple = (await provider.market_caps(["AAPL"]))["AAPL"]

    assert FINNHUB_API_KEY_ENV in apple.note


def test_a_missing_key_names_the_variable() -> None:
    with pytest.raises(FinnhubCredentialsError) as excinfo:
        FinnhubCredentials.from_env({})
    assert FINNHUB_API_KEY_ENV in str(excinfo.value)

    with pytest.raises(FinnhubCredentialsError):
        FinnhubCredentials.from_env({FINNHUB_API_KEY_ENV: "   "})


def test_the_token_is_never_printable() -> None:
    credentials = FinnhubCredentials(token="super-secret-token")
    assert "super-secret-token" not in repr(credentials)
    assert "super-secret-token" not in str(credentials)
    assert "super-secret-token" not in f"{credentials}"


# --------------------------------------------------------------------------
# The budget
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requests_are_metered_against_the_finnhub_bucket() -> None:
    """One token per symbol, out of the 60/min the free tier allows.

    The shared limiter rather than a private one: two limiters against one
    server-side ceiling over-spend by double and look fine locally.
    """
    limiter = HostRateLimiter(clock=lambda: 0.0, sleep=_never_sleep)
    assert limiter.bucket_for(FINNHUB_HOST).capacity == 60.0

    transport = RecordingTransport(by_symbol({"AAPL": "profile2_aapl"}))
    provider = FinnhubProvider(
        credentials=TEST_CREDENTIALS,
        client=httpx.AsyncClient(transport=transport),
        limiter=limiter,
    )
    await provider.market_caps(["AAPL"])

    assert limiter.bucket_for(FINNHUB_HOST).available == pytest.approx(59.0)


def test_the_recorded_fixtures_carry_no_key_material() -> None:
    """The recorder scans before writing; this is the standing check.

    A fixture is committed, so a key that reached one would be in the
    history rather than only on disk.
    """
    for path in FIXTURE_DIR.glob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        assert "token" not in text
        assert "api_key" not in text
        assert "apikey" not in text


def test_a_market_cap_only_carries_a_value_when_it_reports_one() -> None:
    assert MarketCap.not_filed("SPY").value is None
    assert MarketCap.unavailable("AAPL", "boom").value is None
    assert MarketCap.reported("AAPL", Decimal(1)).is_answered is True


# --------------------------------------------------------------------------
# A vendor type change, and the tripwire it must not be confused with
# --------------------------------------------------------------------------


def profile_body(market_cap_literal: str) -> str:
    """A ``/stock/profile2`` body with ``marketCapitalization`` set verbatim."""
    return (
        '{"ticker":"XX","currency":"USD","marketCapitalization":'
        + market_cap_literal
        + "}"
    )


def profile(market_cap_literal: str) -> Any:
    """That body, decoded the way the provider decodes a real response.

    Through :func:`decode_json`, so the value the parser sees is the value
    the wire would produce -- including the shapes ``parse_float=Decimal``
    does *not* intercept.
    """
    return decode_json(profile_body(market_cap_literal))


@pytest.mark.parametrize(
    "literal, expected_in_cause",
    [
        ("true", "bool"),
        ("[1, 2]", "list"),
        ('{"value": 1}', "dict"),
        ('"N/A"', "not a number"),
        ("NaN", "nan"),
        ("Infinity", "inf"),
    ],
)
@pytest.mark.asyncio
async def test_a_vendor_type_change_empties_one_cell_and_never_the_table(
    make_provider: ProviderFactory, literal: str, expected_in_cause: str
) -> None:
    """The finding: ``marketCapitalization: true`` used to be a 500.

    ``as_decimal`` raises ``TypeError`` for a bool, a list and a dict, and
    ``translating`` deliberately does not translate ``TypeError`` -- so the
    exception escaped the ``except FundamentalsError`` around the parser,
    escaped ``market_caps``, and reached the route as an unhandled error. A
    display-only column took the whole stock table down: no prices, no
    volumes, on every poll, with nothing cached to serve instead.

    The premise is asserted rather than described, so this cannot pass for
    the wrong reason: the same value handed straight to ``as_decimal`` still
    raises. What changed is that it no longer gets there.
    """
    raw = profile(literal)["marketCapitalization"]
    if not isinstance(raw, str):
        with pytest.raises(TypeError):
            as_decimal(raw)

    provider, _ = make_provider(
        by_symbol(
            {"AAPL": "profile2_aapl", "XX": (200, profile_body(literal))}
        )
    )
    caps = await provider.market_caps(["XX", "AAPL"])

    assert caps["XX"].status is MarketCapStatus.UNAVAILABLE
    assert caps["XX"].value is None
    assert expected_in_cause.lower() in caps["XX"].note.lower()
    # And the other twenty-five keep their answers.
    assert caps["AAPL"].status is MarketCapStatus.REPORTED


def test_the_decoder_bypass_tripwire_is_still_loud() -> None:
    """A *finite* float is our bug, and stays an untranslated ``TypeError``.

    This is the distinction the fix above is built around. A vendor cannot
    put a finite float in front of the parser -- ``decode_json`` parses every
    JSON number with ``parse_float=Decimal`` -- so one arriving here means
    somebody called ``json.loads`` directly and put an IEEE double on the
    money path. That is precisely what ``as_decimal``'s untranslated
    ``TypeError`` exists to catch, and swallowing it at the same catch site
    as the vendor shapes would have destroyed it.

    Not a ``FundamentalsError``. The type is the difference.
    """
    with pytest.raises(TypeError, match="reached the Decimal boundary"):
        market_cap_from_profile(
            "XX", {"currency": "USD", "marketCapitalization": 4849208.193522442}
        )


@pytest.mark.asyncio
async def test_a_fault_in_our_own_code_is_an_error_log_not_a_dead_table(
    make_provider: ProviderFactory,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tripwire's other half: loud in the log, never a 500.

    The invariant is that a display-only column cannot take the price path
    down, so the residual catch degrades one cell rather than propagating.
    The loudness moves into the log instead of disappearing: ERROR, its own
    event name and the traceback attached -- where a vendor's bad shape is a
    WARNING under ``market_cap_unavailable``. A log reader can still tell the
    two apart, which is the whole point of keeping them distinguishable.
    """
    monkeypatch.setattr(
        finnhub_module,
        "market_cap_from_profile",
        lambda symbol, payload: as_decimal(1.5),
    )
    provider, _ = make_provider(by_symbol({"XX": (200, profile_body("1.5"))}))

    with caplog.at_level(logging.DEBUG, logger="corollary.data.providers.finnhub"):
        cap = (await provider.market_caps(["XX"]))["XX"]

    assert cap.status is MarketCapStatus.UNAVAILABLE
    faults = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "market_cap_internal_fault"
    ]
    assert [record.levelno for record in faults] == [logging.ERROR]
    assert faults[0].exc_info is not None
    assert "TypeError" in faults[0].__dict__["cause"]


@pytest.mark.asyncio
async def test_one_symbols_escaping_exception_does_not_discard_the_others(
    make_provider: ProviderFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gather`` without ``return_exceptions`` throws the good results away.

    ``_market_cap`` catches everything now, so this injects the case that is
    supposed to be impossible: a coroutine that raises anyway. A bare
    ``gather`` propagates the first exception and discards the twenty-five
    answers already in hand, which is the opposite of what the interface
    promises.
    """
    provider, _ = make_provider(
        by_symbol({"AAPL": "profile2_aapl", "SPY": "profile2_spy"})
    )
    original = provider._market_cap

    async def sometimes_explode(symbol: str) -> MarketCap:
        if symbol == "SPY":
            raise RuntimeError("an impossible shape")
        return await original(symbol)

    monkeypatch.setattr(provider, "_market_cap", sometimes_explode)
    caps = await provider.market_caps(["AAPL", "SPY"])

    assert set(caps) == {"AAPL", "SPY"}
    assert caps["AAPL"].status is MarketCapStatus.REPORTED
    assert caps["SPY"].status is MarketCapStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_an_absurd_exponent_is_unavailable_rather_than_an_overflow(
    make_provider: ProviderFactory,
) -> None:
    """``1E+999999999`` is legal JSON, a legal ``Decimal``, and an ``Overflow``.

    The multiply into whole dollars traps by default, and ``decimal.Overflow``
    is an ``ArithmeticError`` no vendor-error handler was catching -- the
    same 500 as the bool case, arriving through the arithmetic rather than
    through the decoder.
    """
    provider, _ = make_provider(
        by_symbol({"XX": (200, profile_body("1E+999999999"))})
    )
    cap = (await provider.market_caps(["XX"]))["XX"]

    assert cap.status is MarketCapStatus.UNAVAILABLE
    assert "whole-dollar" in cap.note


# --------------------------------------------------------------------------
# The precision alarm the column must not trip
# --------------------------------------------------------------------------


def test_a_seventeen_digit_market_cap_does_not_trip_the_precision_alarm(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_money_to_json`` warns on anything a double cannot round-trip.

    Its justification is that *"every figure this app serves is far inside
    that -- the largest on this account is 99728.08."* Market cap breaks the
    premise: Finnhub computes it in a double and emits up to seventeen
    significant digits, and multiplying by an exact million preserves every
    one. One such symbol in a 26-name universe is a WARNING on every poll,
    forever, in the log a rule-8 rejection has to be findable in.

    The precision loss is immaterial for a display-only column; the *alarm*
    is the bug. So the figure is rounded to whole dollars at construction,
    which fixes the cause rather than exempting the column from the check.

    The first assertion is the premise. Without it this would pass against a
    value that could never have tripped the alarm in the first place.
    """
    unrounded = Decimal("2913456.7891234567") * MILLION
    assert Decimal(repr(float(unrounded))) != unrounded, (
        "the premise: this is a value the alarm is about"
    )

    cap = market_cap_from_profile("XX", profile("2913456.7891234567"))
    assert cap.value == Decimal("2913456789123")

    adapter = TypeAdapter(JsonMoney)
    with caplog.at_level(logging.WARNING, logger="corollary.api.schemas"):
        assert adapter.dump_python(cap.value, mode="json") == 2913456789123.0

    assert [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "money_serialization_lossy"
    ] == []


def test_the_alarm_is_real_and_the_unrounded_figure_would_have_tripped_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Proof the test above is not vacuous: the serializer really does warn."""
    adapter = TypeAdapter(JsonMoney)
    with caplog.at_level(logging.WARNING, logger="corollary.api.schemas"):
        adapter.dump_python(Decimal("2913456.7891234567") * MILLION, mode="json")

    assert [
        record.__dict__["event"]
        for record in caplog.records
        if record.__dict__.get("event") == "money_serialization_lossy"
    ] == ["money_serialization_lossy"]


def test_a_half_dollar_market_cap_rounds_to_nearest_and_never_truncates() -> None:
    """Pins the rounding *mode*, which is a choice and not a default.

    ``1.0000005`` million dollars is the discriminating shape: half-up
    answers ``1000001``, ``decimal``'s default half-even answers
    ``1000000``, and truncation answers ``1000000`` too. So this one value
    separates the deliberate choice from both things it could silently
    become -- an inherited context rounding, or a ``ROUND_DOWN`` that looks
    just as tidy.

    Nearest, because this column *sorts*: truncation biases every figure the
    same direction, and a biased dollar column is the one that can order two
    near-equal caps wrongly. Half-even's bias-cancelling is worth nothing
    here -- these values are displayed one per row, never summed -- while
    half-up is what a reader checking the arithmetic by hand computes.
    """
    assert MarketCap.reported("XX", Decimal("1.0000005") * MILLION).value == Decimal(
        1000001
    )
    assert market_cap_from_profile("XX", profile("1.0000005")).value == Decimal(1000001)


def test_the_rounding_mode_is_passed_explicitly_and_not_inherited() -> None:
    """A bare ``quantize`` reads ``getcontext().rounding`` -- global state.

    That is the reason the mode is named at the call site rather than left
    to the default: process-wide rounding is mutable by any library in the
    process, and a money figure that changes because something else changed
    a global is not a figure anybody can reproduce.
    """
    with localcontext() as ctx:
        ctx.rounding = ROUND_DOWN
        assert MarketCap.reported("XX", Decimal("1.0000005") * MILLION).value == (
            Decimal(1000001)
        )


# --------------------------------------------------------------------------
# A fault raised inside ``_get`` is ours, and is logged as ours
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_vendor_exception_from_the_transport_is_logged_as_our_bug(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """``FundamentalsError`` and ``httpx.HTTPError`` are the vendor's failures.

    Everything else reaching the fetch is ours: ``httpx`` raises a bare
    ``RuntimeError`` if the cached provider's client is closed while the app
    keeps serving, and ``ratelimit`` raises ``ValueError`` if the per-host
    table is misedited. Twenty-six ``market_cap_unavailable`` warnings a
    poll, re-fired every retry window with no traceback, would name the
    vendor as the culprit for a fault in this repository -- the exact
    conflation ``_log_internal_fault`` exists to prevent, and which the
    sibling catch twelve lines below already avoided.
    """

    def explode(request: httpx.Request) -> Served | None:
        raise RuntimeError("Cannot send a request, as the client has been closed.")

    provider, _ = make_provider(explode)

    with caplog.at_level(logging.DEBUG, logger="corollary.data.providers.finnhub"):
        cap = (await provider.market_caps(["AAPL"]))["AAPL"]

    assert cap.status is MarketCapStatus.UNAVAILABLE
    events = [record.__dict__.get("event") for record in caplog.records]
    assert "market_cap_unavailable" not in events
    faults = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "market_cap_internal_fault"
    ]
    assert [record.levelno for record in faults] == [logging.ERROR]
    assert faults[0].exc_info is not None
    assert "RuntimeError" in faults[0].__dict__["cause"]


@pytest.mark.asyncio
async def test_an_exceptions_text_is_scrubbed_before_it_reaches_a_log_field(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Every other quotation of third-party text here goes through ``_detail``.

    The reach is exotic -- an exception that embeds a rejected header value,
    which is the token -- but the fix is one call and it closes the channel
    by construction rather than by argument. Rule 6 covers log output.

    Scoped to the fields this module *composes*: message, ``cause`` and
    :attr:`MarketCap.note`. A formatter rendering ``exc_info`` re-derives the
    exception's own ``str``, which no call here can rewrite; see
    ``_log_internal_fault``.
    """

    def explode(request: httpx.Request) -> Served | None:
        raise RuntimeError(f"illegal header value: {TEST_CREDENTIALS.token}")

    provider, _ = make_provider(explode)

    with caplog.at_level(logging.DEBUG, logger="corollary.data.providers.finnhub"):
        cap = (await provider.market_caps(["AAPL"]))["AAPL"]

    assert TEST_CREDENTIALS.token not in cap.note
    for record in caplog.records:
        assert TEST_CREDENTIALS.token not in record.getMessage()
        assert TEST_CREDENTIALS.token not in str(record.__dict__.get("cause", ""))
    faults = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "market_cap_internal_fault"
    ]
    assert "<redacted>" in faults[0].__dict__["cause"]


@pytest.mark.asyncio
async def test_a_transport_errors_text_is_scrubbed_into_the_note(
    make_provider: ProviderFactory,
) -> None:
    """The same channel on the vendor-failure path: ``_get``'s transport catch.

    ``httpx`` builds that message out of the request it failed to send, so a
    rejected header value can land in it, and the note is log output.
    """

    def explode(request: httpx.Request) -> Served | None:
        raise httpx.ConnectError(
            f"bad header {TEST_CREDENTIALS.token}", request=request
        )

    provider, _ = make_provider(explode)
    apple = (await provider.market_caps(["AAPL"]))["AAPL"]

    assert apple.status is MarketCapStatus.UNAVAILABLE
    assert TEST_CREDENTIALS.token not in apple.note
    assert "<redacted>" in apple.note


@pytest.mark.asyncio
async def test_every_residual_catch_scrubs_and_not_only_the_first_one(
    make_provider: ProviderFactory,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader's residual catch, not the fetch's -- the same channel.

    ``_market_cap`` has two ``except Exception`` arms and they were not
    equal: the fetch's passed a scrubbed ``detail`` and the reader's passed
    the exception bare, so ``_log_internal_fault`` re-derived ``str(exc)``
    itself and the scrub was one call site wide. A guard that covers the arm
    somebody remembered is not a guard.

    ``detail`` is required now, which is why this can only regress by a
    deliberate act rather than by adding a third arm and forgetting.
    """

    def raise_with_the_token(symbol: str, payload: Any) -> MarketCap:
        raise RuntimeError(f"illegal header value: {TEST_CREDENTIALS.token}")

    monkeypatch.setattr(
        finnhub_module, "market_cap_from_profile", raise_with_the_token
    )
    provider, _ = make_provider(lambda request: (200, profile_body("4.0")))

    with caplog.at_level(logging.DEBUG, logger="corollary.data.providers.finnhub"):
        apple = (await provider.market_caps(["AAPL"]))["AAPL"]

    assert apple.status is MarketCapStatus.UNAVAILABLE
    assert TEST_CREDENTIALS.token not in apple.note
    faults = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "market_cap_internal_fault"
    ]
    assert len(faults) == 1
    assert TEST_CREDENTIALS.token not in faults[0].getMessage()
    assert TEST_CREDENTIALS.token not in str(faults[0].__dict__["cause"])
    assert "<redacted>" in faults[0].__dict__["cause"]


# --------------------------------------------------------------------------
# The constructor's own contract
# --------------------------------------------------------------------------


def test_a_reported_market_cap_refuses_zero_and_below() -> None:
    """The refusal is on the type, for the reason the rounding is.

    ``finnhub.market_cap_from_profile`` already declines a non-positive
    vendor figure, with the logging that decision needs -- but the *rule*
    belongs to the type, so decision 12's SEC EDGAR replacement cannot
    reintroduce a zero by forgetting. A ``0`` in this column renders, sorts
    to the top of an ascending list and states that a live company is worth
    nothing: CLAUDE.md's fund error, one row lower down.
    """
    for value in (Decimal(0), Decimal("-0.4"), Decimal("-4.5e12")):
        with pytest.raises(ValueError):
            MarketCap.reported("XX", value)


def test_a_reported_market_cap_refuses_a_non_finite_figure() -> None:
    """``NaN`` survives ``quantize`` untouched, and is not a figure.

    ``Decimal("NaN").quantize(...)`` returns ``NaN`` rather than raising, so
    without this the constructor would answer with one; Pydantic then
    serialises it as ``null``, which is harmless and *also*
    indistinguishable from a fund. ``Infinity`` is refused here rather than
    by the ``quantize`` it would have overflowed, so both non-finite shapes
    fail on one stated line instead of two accidents.

    It is also what makes the non-positive check above safe to write as a
    comparison: ``Decimal("NaN") <= 0`` raises ``InvalidOperation``, not
    ``False``.
    """
    for value in (Decimal("NaN"), Decimal("-NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            MarketCap.reported("XX", value)
