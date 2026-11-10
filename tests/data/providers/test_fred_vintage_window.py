"""The FRED vintage-window fetch (U6c, spec R7 *The fetch*). No live calls.

One request per attempt: ``realtime_start = D-1``, ``realtime_end = D``. U6a's
probe showed FRED clips each row's ``realtime_start`` to the requested window,
so a single-day vintage cannot say what is new, and the two-day window can
(``tests/data/providers/test_fred_vintage_fixtures.py`` pins that reading).

The recorded U6a fixtures are replayed through an ``httpx.MockTransport``, so
the request the provider builds is the one exercised, and every value's digits
are FRED's text. Pinned here:

* the request's exact parameters, ``output_type=1`` and ``units=lin`` sent
  explicitly, the ``observation_start`` lookback per frequency, and that it is
  metered against the FRED bucket;
* the window parser refuses a body that is not the window asked for, the
  ``output_type``/``units`` echo included;
* ``EXHOSLUSM495S``'s recorded 400 is a typed :class:`FredRefusal` carrying
  FRED's message, while a bad key, a plain "series does not exist", a 429, a
  5xx and a transport error stay ordinary errors the scheduler retries;
* the key reaches no exception message and no refusal message.
"""

import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from corollary.data.providers.fred import (
    FredCredentials,
    FredError,
    FredProvider,
    FredRefusal,
    FredVintageRow,
    FredVintageWindow,
    parse_vintage_window,
    vintage_observation_start,
    vintage_window_params,
)
from corollary.data.providers.interface import RateLimitedError
from corollary.ratelimit import FRED_HOST, HostRateLimiter

FIXTURE_DIR: Final = Path(__file__).resolve().parents[2] / "fixtures" / "fred"

#: Obviously fake, and long enough that a partial echo would still be caught.
FAKE_KEY: Final = "notarealfredkeynotarealfredkey00"

PAYEMS_DAY: Final = date(2026, 11, 6)

#: FRED's refusal of every vintage query on ``EXHOSLUSM495S``, verbatim.
NOT_IN_ALFRED: Final = (
    "Bad Request.  The series does not exist in ALFRED but may exist in FRED.  "
    "Try setting realtime_start and realtime_end to today's date or removing the "
    "realtime_start and realtime_end variables."
)


def fixture_body_text(name: str) -> str:
    """The exact text of a fixture's ``body``, sliced out of the file."""
    text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    marker = '"body":'
    start = text.index(marker) + len(marker)
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end]


def fixture_status(name: str) -> int:
    status: int = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))[
        "status_code"
    ]
    return status


def window_body(sid: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(fixture_body_text(f"p3_vintage_{sid}_window"))
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


def _serve_fixture(name: str) -> Any:
    return _serve_text(fixture_body_text(name), fixture_status(name))


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("release_day", "frequency", "start"),
    [
        # Six months before D's month, from its first day.
        (date(2026, 11, 6), "monthly", date(2026, 5, 1)),
        (date(2026, 10, 14), "monthly", date(2026, 4, 1)),
        (date(2026, 2, 11), "monthly", date(2025, 8, 1)),  # across a year end
        (date(2026, 6, 30), "monthly", date(2025, 12, 1)),
        # Fifteen months before D's month.
        (date(2026, 10, 29), "quarterly", date(2025, 7, 1)),
        (date(2026, 1, 30), "quarterly", date(2024, 10, 1)),
        (date(2026, 3, 31), "quarterly", date(2024, 12, 1)),
    ],
)
def test_the_lookback_holds_three_observations(
    release_day: date, frequency: Any, start: date
) -> None:
    assert vintage_observation_start(release_day, frequency) == start


def test_an_unknown_frequency_is_refused_before_any_request() -> None:
    with pytest.raises(ValueError, match="frequency"):
        vintage_observation_start(PAYEMS_DAY, "weekly")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_request_is_exactly_the_two_day_window() -> None:
    limiter = HostRateLimiter(clock=lambda: 0.0, sleep=_never_sleep)
    provider, recorder = _provider(_serve_fixture("p3_vintage_payems_window"), limiter)
    async with provider:
        answer = await provider.vintage_window("PAYEMS", PAYEMS_DAY, frequency="monthly")

    assert isinstance(answer, FredVintageWindow)
    (request,) = recorder.requests  # one request per attempt: R8's budget
    assert request.method == "GET"
    assert request.url.path == "/fred/series/observations"
    assert dict(request.url.params) == {
        "series_id": "PAYEMS",
        "file_type": "json",
        "realtime_start": "2026-11-05",
        "realtime_end": "2026-11-06",
        "observation_start": "2026-05-01",
        # Sent, not left to FRED's defaults: the new-versus-revised split
        # depends on observations by real-time period (1), in levels (lin).
        "output_type": "1",
        "units": "lin",
        "api_key": FAKE_KEY,
    }
    assert request.url.host == FRED_HOST
    bucket = limiter.bucket_for(FRED_HOST)
    assert bucket.capacity - bucket.available == 1  # metered against the FRED bucket


@pytest.mark.asyncio
async def test_a_quarterly_request_reaches_back_fifteen_months() -> None:
    provider, recorder = _provider(_serve_fixture("p3_vintage_payems_window"))
    async with provider:
        # The body is PAYEMS's; only the outgoing request is under test, and a
        # window echo that does not match the request is refused.
        with pytest.raises(FredError, match="realtime"):
            await provider.vintage_window("PAYEMS", date(2026, 10, 29), frequency="quarterly")
    (request,) = recorder.requests
    params = dict(request.url.params)
    assert (params["realtime_start"], params["realtime_end"]) == ("2026-10-28", "2026-10-29")
    assert params["observation_start"] == "2025-07-01"


def test_the_public_params_carry_no_key() -> None:
    assert vintage_window_params("PAYEMS", PAYEMS_DAY, "monthly") == {
        "series_id": "PAYEMS",
        "file_type": "json",
        "realtime_start": "2026-11-05",
        "realtime_end": "2026-11-06",
        "observation_start": "2026-05-01",
        "output_type": "1",
        "units": "lin",
    }


# --------------------------------------------------------------------------
# The window parser
# --------------------------------------------------------------------------


def test_the_recorded_payems_window_parses_with_its_real_time_periods() -> None:
    window = parse_vintage_window(
        "PAYEMS", window_body("payems"), release_day=PAYEMS_DAY, frequency="monthly"
    )
    assert window.series_id == "PAYEMS" and window.release_day == PAYEMS_DAY
    assert window.frequency == "monthly"
    nov5, nov6 = date(2026, 11, 5), PAYEMS_DAY
    sep = [r for r in window.rows if r.date == date(2026, 9, 1)]
    assert sep == [
        FredVintageRow(date(2026, 9, 1), Decimal("159044"), nov5, nov5),
        FredVintageRow(date(2026, 9, 1), Decimal("159128"), nov6, nov6),
    ]
    assert window.rows[0] == FredVintageRow(date(2026, 5, 1), Decimal("158861"), nov5, nov6)
    assert len(window.rows) == 8
    assert all(type(r.value) is Decimal for r in window.rows)


def test_a_missing_value_parses_to_none_never_zero() -> None:
    body = window_body("payems")
    body["observations"][0]["value"] = "."
    window = parse_vintage_window("PAYEMS", body, release_day=PAYEMS_DAY, frequency="monthly")
    assert window.rows[0].value is None


def _broken(mutate: Any) -> dict[str, Any]:
    body = copy.deepcopy(window_body("payems"))
    mutate(body)
    return body


def _set_row(index: int, **fields: Any) -> Any:
    def mutate(body: dict[str, Any]) -> None:
        body["observations"][index].update(fields)

    return mutate


def _drop_last_row(body: dict[str, Any]) -> None:
    body["observations"].pop()


def _duplicate_row(body: dict[str, Any]) -> None:
    body["observations"].append(dict(body["observations"][-1]))
    body["count"] += 1


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        # A row outside the requested window is not this window's answer.
        (_set_row(0, realtime_start="2026-11-04"), "outside"),
        (_set_row(0, realtime_end="2026-11-07"), "outside"),
        (_set_row(3, realtime_start="2026-11-06", realtime_end="2026-11-05"), "ends before"),
        # Two values valid on the same day for the same date.
        (_duplicate_row, "two values"),
        # A count that is not the rows returned: a partial answer.
        (_drop_last_row, "count"),
        (_set_row(0, value="12,000"), "neither a number"),
        (_set_row(0, date="20260501"), "YYYY-MM-DD"),
        (_set_row(0, realtime_start=None), "realtime_start"),
        (lambda b: b.update(realtime_end="2026-11-07"), "realtime_end"),
        # The echo of what was sent: output_type 4 (initial release only) and
        # any non-level units would each be a different question answered.
        (lambda b: b.update(output_type=4), "output_type"),
        (lambda b: b.update(output_type=2), "output_type"),
        (lambda b: b.update(output_type="1"), "output_type"),
        (lambda b: b.update(output_type=True), "output_type"),
        (lambda b: b.pop("output_type"), "output_type"),
        (lambda b: b.update(units="chg"), "units"),
        (lambda b: b.update(units="pch"), "units"),
        (lambda b: b.update(units="LIN"), "units"),
        (lambda b: b.pop("units"), "units"),
        (lambda b: b.pop("count"), "count"),
        (lambda b: b.pop("observations"), "observations"),
    ],
)
def test_a_body_that_is_not_the_window_asked_for_is_refused(mutate: Any, match: str) -> None:
    with pytest.raises(FredError, match=match):
        parse_vintage_window(
            "PAYEMS", _broken(mutate), release_day=PAYEMS_DAY, frequency="monthly"
        )


# --------------------------------------------------------------------------
# Refusal is not error
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhoslusm495s_recorded_400_is_a_refusal_with_freds_reason() -> None:
    provider, _ = _provider(_serve_fixture("p3_vintage_exhoslusm495s_window"))
    async with provider:
        answer = await provider.vintage_window(
            "EXHOSLUSM495S", date(2026, 10, 13), frequency="monthly"
        )
    assert answer == FredRefusal(
        series_id="EXHOSLUSM495S",
        release_day=date(2026, 10, 13),
        # The provider's one scrubber collapses runs of whitespace.
        vendor_message=" ".join(NOT_IN_ALFRED.split()),
    )


@pytest.mark.asyncio
async def test_a_refusal_that_echoes_the_key_carries_no_key() -> None:
    body = json.dumps(
        {
            "error_code": 400,
            "error_message": (
                "Bad Request.  The series does not exist in ALFRED but may exist in "
                f"FRED. api_key={FAKE_KEY}"
            ),
        }
    )
    provider, _ = _provider(_serve_text(body, 400))
    async with provider:
        answer = await provider.vintage_window("EXHOSLUSM495S", PAYEMS_DAY, frequency="monthly")
    assert isinstance(answer, FredRefusal)
    assert FAKE_KEY not in answer.vendor_message
    assert "series does not exist in ALFRED" in answer.vendor_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Bad Request. The series does not exist in ALFRED but may exist in FRED.",
        # Matched whitespace-collapsed and case-folded.
        "Bad Request.  The series\n does  NOT exist\tin Alfred.",
    ],
)
async def test_the_alfred_refusal_is_matched_whatever_its_spacing_and_case(
    message: str,
) -> None:
    body = json.dumps({"error_code": 400, "error_message": message})
    provider, _ = _provider(_serve_text(body, 400))
    async with provider:
        answer = await provider.vintage_window("EXHOSLUSM495S", PAYEMS_DAY, frequency="monthly")
    assert isinstance(answer, FredRefusal)
    assert answer.vendor_message == " ".join(message.split())


@pytest.mark.asyncio
async def test_a_series_that_does_not_exist_at_all_is_an_error_not_a_refusal() -> None:
    """FRED's plain "The series does not exist." is what a seed typo gets.

    ``CPIAUSCL`` for ``CPIAUCSL`` is our fault, not FRED's data policy, so it
    must not be stored as "not available from FRED": it stays a loud
    :class:`FredError`. Only the ALFRED case -- the series exists, its
    vintages do not -- is a refusal.
    """
    body = json.dumps(
        {"error_code": 400, "error_message": "Bad Request.  The series does not exist."}
    )
    provider, _ = _provider(_serve_text(body, 400))
    async with provider:
        with pytest.raises(FredError, match="series does not exist") as raised:
            await provider.vintage_window("CPIAUSCL", PAYEMS_DAY, frequency="monthly")
    assert FAKE_KEY not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 404])
async def test_the_refusal_body_under_another_status_is_an_error(status: int) -> None:
    # The recorded refusal, word for word, but not a 400: a proxy or an
    # outage replaying an error page is transient and retried, never stored
    # as FRED's permanent answer.
    body = fixture_body_text("p3_vintage_exhoslusm495s_window")
    assert "does not exist in ALFRED" in body
    provider, _ = _provider(_serve_text(body, status))
    async with provider:
        with pytest.raises(FredError, match=str(status)):
            await provider.vintage_window("EXHOSLUSM495S", PAYEMS_DAY, frequency="monthly")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"error_message": NOT_IN_ALFRED},
        {"error_code": 500, "error_message": NOT_IN_ALFRED},
        {"error_code": "400", "error_message": NOT_IN_ALFRED},
    ],
    ids=["no-error-code", "error-code-500", "error-code-as-text"],
)
async def test_a_400_with_the_refusal_text_but_not_freds_error_code_is_an_error(
    body: dict[str, Any],
) -> None:
    # Only FRED's own error object is FRED's answer; the message alone, in a
    # body that is not that object, is not.
    provider, _ = _provider(_serve_text(json.dumps(body), 400))
    async with provider:
        with pytest.raises(FredError, match="400"):
            await provider.vintage_window("EXHOSLUSM495S", PAYEMS_DAY, frequency="monthly")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        # FRED answers a bad key with a 400. A credentials fault is not FRED
        # declining the series, and must not be stored as "not available".
        (
            400,
            '{"error_code":400,"error_message":"Bad Request.  The value for variable '
            'api_key is not registered."}',
            FredError,
        ),
        # A 400 that is our own malformed request, not a series refusal.
        (
            400,
            '{"error_code":400,"error_message":"Bad Request.  Variable observation_start '
            'is not a valid date."}',
            FredError,
        ),
        (400, "<html>bad gateway-ish</html>", FredError),
        (429, '{"error_code":429,"error_message":"Too Many Requests."}', RateLimitedError),
        (500, '{"error_code":500,"error_message":"Internal Server Error."}', FredError),
        (503, "upstream unavailable", FredError),
    ],
)
async def test_anything_else_is_an_ordinary_error_not_a_refusal(
    status: int, body: str, error: type[Exception]
) -> None:
    provider, _ = _provider(_serve_text(body, status))
    async with provider:
        with pytest.raises(error) as raised:
            await provider.vintage_window("PAYEMS", PAYEMS_DAY, frequency="monthly")
    assert FAKE_KEY not in str(raised.value)


@pytest.mark.asyncio
async def test_a_transport_error_names_no_key() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    provider, _ = _provider(respond)
    async with provider:
        with pytest.raises(FredError) as raised:
            await provider.vintage_window("PAYEMS", PAYEMS_DAY, frequency="monthly")
    message = str(raised.value)
    assert "ConnectError" in message
    assert FAKE_KEY not in message


@pytest.mark.asyncio
async def test_a_5xx_body_echoing_the_key_names_no_key() -> None:
    provider, _ = _provider(_serve_text(f"oops for api_key={FAKE_KEY}&x=1", 502))
    async with provider:
        with pytest.raises(FredError) as raised:
            await provider.vintage_window("PAYEMS", PAYEMS_DAY, frequency="monthly")
    assert FAKE_KEY not in str(raised.value)
