"""FRED ``/fred/releases/dates``, replayed from a recorded response. No live calls.

Unit 7.2b-R. The fixture ``p3_release_dates_forward.json`` was recorded by
``tests/fixtures/record_fred.py`` with the very parameters
:func:`~corollary.data.providers.fred.release_dates_params` builds, over a
window that crosses the 2026-11-01 end of daylight time, so the mapping tests
in ``tests/data/test_calendar_releases.py`` see an EDT and an EST date of the
same release from real data.

Documented at https://fred.stlouisfed.org/docs/api/fred/releases_dates.html:
``include_release_dates_with_no_data`` defaults to ``false``, which "excludes
release dates that do not have data" -- that is, every *future* date. ``true``
is the whole point of the call. ``limit`` is 1..1000, so a long window pages
by ``offset``.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from corollary.data.providers.fred import (
    RELEASE_DATES_PAGE_LIMIT,
    FredCredentials,
    FredError,
    FredProvider,
    FredReleaseDate,
    parse_release_dates,
    release_dates_params,
)
from corollary.ratelimit import FRED_HOST, HostRateLimiter

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "fred"
FORWARD_FIXTURE = "p3_release_dates_forward"

#: Obviously fake. Rule 6.
FAKE_KEY = "notarealfredkeynotarealfredkey00"


def fixture_envelope(name: str) -> dict[str, Any]:
    envelope: dict[str, Any] = json.loads(
        (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    )
    return envelope


def fixture_body_text(name: str) -> str:
    """The exact text of a fixture's ``body``, sliced out of the file."""
    text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    marker = '"body":'
    start = text.index(marker) + len(marker)
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end]


def recorded_window() -> tuple[date, date]:
    params = fixture_envelope(FORWARD_FIXTURE)["params"]
    return date.fromisoformat(params["realtime_start"]), date.fromisoformat(
        params["realtime_end"]
    )


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


def _provider(
    respond: Any, limiter: HostRateLimiter | None = None
) -> tuple[FredProvider, Recorder]:
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


def _serve_text(text: str) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=text.encode("utf-8"))

    return respond


def _row(release_id: Any, day: Any, name: Any = "Some Release") -> dict[str, Any]:
    return {
        "release_id": release_id,
        "release_name": name,
        "release_last_updated": "2026-10-01 07:00:00-05",
        "date": day,
    }


def _page(rows: list[dict[str, Any]], *, count: int | None = None, offset: int = 0) -> dict[str, Any]:
    return {
        "realtime_start": "2026-10-10",
        "realtime_end": "2026-11-09",
        "order_by": "release_date",
        "sort_order": "asc",
        "count": len(rows) if count is None else count,
        "offset": offset,
        "limit": 1000,
        "release_dates": rows,
    }


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


def test_the_params_are_the_documented_forward_looking_ones() -> None:
    assert release_dates_params(date(2026, 10, 10), date(2026, 11, 9), offset=0) == {
        "file_type": "json",
        "include_release_dates_with_no_data": "true",
        "realtime_start": "2026-10-10",
        "realtime_end": "2026-11-09",
        "order_by": "release_date",
        "sort_order": "asc",
        "limit": RELEASE_DATES_PAGE_LIMIT,
        "offset": 0,
    }
    # The documented ceiling for this endpoint (``/release/dates``, singular,
    # allows 10000; ``/releases/dates`` does not).
    assert RELEASE_DATES_PAGE_LIMIT == 1000


def test_the_fixture_was_recorded_with_exactly_these_params() -> None:
    envelope = fixture_envelope(FORWARD_FIXTURE)
    start, end = recorded_window()
    expected = {k: str(v) for k, v in release_dates_params(start, end, offset=0).items()}
    assert envelope["params"] == expected
    assert envelope["path"] == "/releases/dates"
    assert "api_key" not in json.dumps(envelope["params"])


@pytest.mark.asyncio
async def test_release_dates_builds_the_documented_request_and_meters_it() -> None:
    limiter = HostRateLimiter(clock=lambda: 0.0, sleep=_never_sleep)
    provider, recorder = _provider(_serve_text(fixture_body_text(FORWARD_FIXTURE)), limiter)
    start, end = recorded_window()
    async with provider:
        rows = await provider.release_dates(start, end)

    assert rows
    [request] = recorder.requests
    assert request.method == "GET"
    assert request.url.host == FRED_HOST
    assert request.url.path == "/fred/releases/dates"
    assert dict(request.url.params) == {
        "file_type": "json",
        "include_release_dates_with_no_data": "true",
        "realtime_start": start.isoformat(),
        "realtime_end": end.isoformat(),
        "order_by": "release_date",
        "sort_order": "asc",
        "limit": "1000",
        "offset": "0",
        "api_key": FAKE_KEY,
    }
    bucket = limiter.bucket_for(FRED_HOST)
    assert bucket.capacity - bucket.available == pytest.approx(1)


# --------------------------------------------------------------------------
# The recorded body
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_recorded_forward_dates_parse_whole_and_in_order() -> None:
    body = json.loads(fixture_body_text(FORWARD_FIXTURE))
    provider, _ = _provider(_serve_text(fixture_body_text(FORWARD_FIXTURE)))
    start, end = recorded_window()
    async with provider:
        rows = await provider.release_dates(start, end)

    # Nothing dropped: one parsed row per recorded row, and the recording
    # was a single complete page.
    assert body["count"] == len(body["release_dates"]) == len(rows)
    assert all(isinstance(r, FredReleaseDate) for r in rows)
    assert all(start <= r.date <= end for r in rows)
    assert rows == sorted(rows, key=lambda r: (r.date, r.release_id))
    # Future dates are present -- the reason for include_release_dates_with_no_data.
    assert max(r.date for r in rows) > start


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_window_longer_than_a_page_is_read_by_offset() -> None:
    first = [_row(10, "2026-10-14"), _row(50, "2026-10-14")]
    second = [_row(46, "2026-10-15")]

    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        if offset == 0:
            body = _page(first, count=3)
        elif offset == 2:
            body = _page(second, count=3, offset=2)
        else:  # pragma: no cover
            raise AssertionError(f"unexpected offset {offset}")
        return httpx.Response(200, content=json.dumps(body).encode("utf-8"))

    provider, recorder = _provider(respond)
    async with provider:
        rows = await provider.release_dates(date(2026, 10, 10), date(2026, 11, 9), page_limit=2)

    assert [r.release_id for r in rows] == [10, 50, 46]
    assert [r.url.params["offset"] for r in recorder.requests] == ["0", "2"]
    assert {r.url.params["limit"] for r in recorder.requests} == {"2"}


@pytest.mark.asyncio
async def test_an_empty_page_short_of_the_count_is_refused() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        rows = [_row(10, "2026-10-14")] if offset == 0 else []
        body = _page(rows, count=5, offset=offset)
        return httpx.Response(200, content=json.dumps(body).encode("utf-8"))

    provider, _ = _provider(respond)
    async with provider:
        with pytest.raises(FredError, match="count"):
            await provider.release_dates(date(2026, 10, 10), date(2026, 11, 9), page_limit=1)


@pytest.mark.asyncio
async def test_paging_stops_at_a_ceiling_rather_than_looping() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        body = _page([_row(10, "2026-10-14")], count=10_000_000, offset=offset)
        return httpx.Response(200, content=json.dumps(body).encode("utf-8"))

    provider, recorder = _provider(respond)
    async with provider:
        with pytest.raises(FredError, match="pages"):
            await provider.release_dates(date(2026, 10, 10), date(2026, 11, 9), page_limit=1)
    assert len(recorder.requests) <= 10


@pytest.mark.asyncio
async def test_a_backwards_window_is_refused_before_any_request() -> None:
    provider, recorder = _provider(_serve_text("{}"))
    async with provider:
        with pytest.raises(ValueError):
            await provider.release_dates(date(2026, 11, 9), date(2026, 10, 10))
    assert recorder.requests == []


# --------------------------------------------------------------------------
# Malformed bodies refuse whole
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        _row("10", "2026-10-14"),
        _row(True, "2026-10-14"),
        _row(None, "2026-10-14"),
        _row(10, None),
        _row(10, "2026-13-40"),
        _row(10, "2026-10-14T08:30:00Z"),
        # date.fromisoformat accepts these since 3.11; FRED never sends them.
        _row(10, "20261014"),
        _row(10, "2026-W42-3"),
        _row(10, "2026-10-14", name=None),
        _row(10, "2026-10-14", name="   "),
        "not an object",
    ],
)
def test_a_malformed_row_refuses_the_whole_body(row: Any) -> None:
    payload = _page([_row(50, "2026-10-14"), row])
    with pytest.raises(FredError):
        parse_release_dates(payload)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"count": 1},
        {"release_dates": {}, "count": 0},
        {"release_dates": [], "count": "0"},
        {"release_dates": [], "count": -1},
        {"release_dates": [], "count": True},
    ],
)
def test_a_body_that_is_not_a_page_is_refused(payload: Any) -> None:
    with pytest.raises(FredError):
        parse_release_dates(payload)


def test_a_well_formed_page_parses() -> None:
    page = parse_release_dates(_page([_row(10, "2026-10-14", name="Consumer Price Index")]))
    assert page.count == 1
    assert page.rows == (
        FredReleaseDate(release_id=10, release_name="Consumer Price Index", date=date(2026, 10, 14)),
    )


# --------------------------------------------------------------------------
# Unit 7.2c-1: paging is checked against count, not trusted to it
# --------------------------------------------------------------------------


def _paged(pages: dict[int, dict[str, Any]]) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        if offset not in pages:  # pragma: no cover
            raise AssertionError(f"unexpected offset {offset}")
        return httpx.Response(200, content=json.dumps(pages[offset]).encode("utf-8"))

    return respond


async def _release_dates(respond: Any, *, page_limit: int = 2) -> list[FredReleaseDate]:
    provider, _ = _provider(respond)
    async with provider:
        return await provider.release_dates(
            date(2026, 10, 10), date(2026, 11, 9), page_limit=page_limit
        )


@pytest.mark.asyncio
async def test_a_row_repeated_across_pages_is_refused_not_counted() -> None:
    # The calendar shifted between requests: page 2 repeats page 1's last row,
    # and the third distinct row was never seen. len(rows) reaches count anyway.
    first = [_row(10, "2026-10-14"), _row(50, "2026-10-14")]
    repeat = [_row(50, "2026-10-14")]
    respond = _paged({0: _page(first, count=3), 2: _page(repeat, count=3, offset=2)})
    with pytest.raises(FredError, match="distinct"):
        await _release_dates(respond)


@pytest.mark.asyncio
async def test_a_row_repeated_within_a_page_is_refused() -> None:
    rows = [_row(10, "2026-10-14"), _row(10, "2026-10-14")]
    with pytest.raises(FredError, match="distinct"):
        await _release_dates(_paged({0: _page(rows, count=2)}))


@pytest.mark.asyncio
async def test_more_rows_than_the_count_are_refused() -> None:
    rows = [_row(10, "2026-10-14"), _row(50, "2026-10-14")]
    with pytest.raises(FredError, match="distinct"):
        await _release_dates(_paged({0: _page(rows, count=1)}))


@pytest.mark.asyncio
async def test_a_repeat_hidden_by_a_surplus_row_is_refused() -> None:
    # Three rows against a count of two: the repeat makes the distinct pairs
    # come out at exactly the count, so only the row total gives it away.
    rows = [_row(10, "2026-10-14"), _row(10, "2026-10-14"), _row(50, "2026-10-14")]
    with pytest.raises(FredError, match="distinct"):
        await _release_dates(_paged({0: _page(rows, count=2)}), page_limit=3)


@pytest.mark.asyncio
async def test_a_count_that_changes_between_pages_is_refused() -> None:
    # [a, b, c, d] loses b after page 1: offset 2 is now [d], count 3. Three
    # distinct rows against a count of three -- and c was never read.
    first = [_row(10, "2026-10-14"), _row(50, "2026-10-14")]
    later = [_row(46, "2026-10-16")]
    respond = _paged({0: _page(first, count=4), 2: _page(later, count=3, offset=2)})
    with pytest.raises(FredError, match="count"):
        await _release_dates(respond)


@pytest.mark.asyncio
async def test_distinct_rows_equal_to_the_count_are_accepted() -> None:
    rows = [_row(10, "2026-10-14"), _row(10, "2026-10-15")]  # one release, two dates
    got = await _release_dates(_paged({0: _page(rows, count=2)}))
    assert [(r.release_id, r.date) for r in got] == [
        (10, date(2026, 10, 14)),
        (10, date(2026, 10, 15)),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        _page([_row("x" * 500, "2026-10-14")]),
        {"release_dates": [], "count": "y" * 500},
    ],
    ids=["release_id", "count"],
)
def test_a_refusal_quotes_at_most_forty_characters_of_fred_text(payload: Any) -> None:
    with pytest.raises(FredError) as info:
        parse_release_dates(payload)
    message = str(info.value)
    assert "x" * 41 not in message and "y" * 41 not in message
    assert len(message) < 160
