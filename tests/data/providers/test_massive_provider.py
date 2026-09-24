"""Massive's untickered news feed, replayed. No test here makes a live call.

``p3_reference_news_untickered`` was recorded by ``scripts/probe_phase3.py``
(step 0): ``limit=1000``, kept truncated to 5 rows, with the vendor's real
``next_url``. The ``p4_reference_news_asc_*`` pair was recorded by
``tests/fixtures/record_massive.py`` with the exact query this provider
sends -- ascending, ``published_utc.gt`` -- at ``limit=3`` so the second page
is a real ``next_url`` follow. ``p4_unauthorized`` is the same recorder's
``unauthorized`` mode: a real 401 from a deliberately invalid key. Bodies
built in test code are **synthetic** and say so.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from corollary.data.news.article import (
    NewsAccessDenied,
    NewsFeed,
    NewsProviderError,
    VendorInsight,
)
from corollary.data.providers.massive import (
    MASSIVE_API_KEY_ENV,
    MASSIVE_PAGE_LIMIT,
    MassiveCredentials,
    MassiveCredentialsError,
    MassiveNews,
    MassiveProvider,
)
from corollary.ratelimit import MASSIVE_HOST, HostRateLimiter

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "massive"

#: Obviously fake. Rule 6: no key material in tests or fixtures.
TEST_CREDENTIALS = MassiveCredentials(token="not-a-real-massive-key-000000000000")
SINCE = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)

Served = str | tuple[int, str]
Route = Callable[[httpx.Request], Served | None]


def fixture_envelope(name: str) -> dict[str, Any]:
    envelope: dict[str, Any] = json.loads(
        (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    )
    return envelope


def fixture_body(name: str) -> dict[str, Any]:
    body: dict[str, Any] = fixture_envelope(name)["body"]
    return body


class RecordingTransport(httpx.MockTransport):
    """Serves a fixture's body (or a literal) and logs every request."""

    def __init__(self, route: Route) -> None:
        self.requests: list[httpx.Request] = []
        self._route = route
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        served = self._route(request)
        if served is None:
            raise AssertionError(f"no fixture routed for {request.method} {request.url}")
        if isinstance(served, tuple):
            status, text = served
        else:
            status = int(fixture_envelope(served)["status_code"])
            text = json.dumps(fixture_body(served))
        return httpx.Response(
            status_code=status,
            content=text.encode("utf-8"),
            headers={"content-type": "application/json"},
            request=request,
        )


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(f"a replay test waited {seconds}s on the rate limiter")


def generous_limiter() -> HostRateLimiter:
    return HostRateLimiter(
        requests_per_minute=10_000, per_host={}, clock=lambda: 0.0, sleep=_never_sleep
    )


def build(
    route: Route, *, limiter: HostRateLimiter | None = None, **kwargs: Any
) -> tuple[MassiveProvider, RecordingTransport]:
    transport = RecordingTransport(route)
    provider = MassiveProvider(
        credentials=TEST_CREDENTIALS,
        client=httpx.AsyncClient(transport=transport),
        limiter=limiter or generous_limiter(),
        **kwargs,
    )
    return provider, transport


def untickered_without_next(request: httpx.Request) -> Served | None:
    """SYNTHETIC wrapper: the recorded body with its ``next_url`` removed."""
    body = fixture_body("p3_reference_news_untickered")
    body.pop("next_url")
    return (200, json.dumps(body))


def synthetic_page(stamps: list[str], *, next_url: str | None, first: int = 0) -> str:
    """SYNTHETIC Massive page in the recorded shape."""
    body: dict[str, Any] = {
        "status": "OK",
        "request_id": "synthetic",
        "count": len(stamps),
        "results": [
            {
                "id": f"synthetic-{first + index}",
                "publisher": {"name": "Synthetic Wire"},
                "title": f"synthetic {first + index}",
                "published_utc": stamp,
                "article_url": f"https://example.com/{first + index}",
                "tickers": ["NVDA"],
                "description": "synthetic",
                "insights": [],
            }
            for index, stamp in enumerate(stamps)
        ],
    }
    if next_url is not None:
        body["next_url"] = next_url
    return json.dumps(body)


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_request_is_untickered_ascending_and_from_the_cursor() -> None:
    provider, transport = build(untickered_without_next)
    await provider.news_since(SINCE)

    request = transport.requests[0]
    assert request.url.host == MASSIVE_HOST
    assert request.url.path == "/v2/reference/news"
    assert dict(request.url.params) == {
        "limit": str(MASSIVE_PAGE_LIMIT),
        "order": "asc",
        "sort": "published_utc",
        "published_utc.gt": "2026-09-18T00:00:00Z",
    }
    assert MASSIVE_PAGE_LIMIT == 1000


@pytest.mark.asyncio
async def test_the_key_travels_as_a_bearer_header_and_never_in_a_url() -> None:
    provider, transport = build(untickered_without_next)
    await provider.news_since(SINCE)

    request = transport.requests[0]
    assert request.headers["Authorization"] == f"Bearer {TEST_CREDENTIALS.token}"
    assert TEST_CREDENTIALS.token not in str(request.url)
    assert "apiKey" not in request.url.params


@pytest.mark.asyncio
async def test_a_naive_cursor_is_refused_before_any_request() -> None:
    provider, transport = build(lambda request: None)
    with pytest.raises(ValueError):
        await provider.news_since(datetime(2026, 9, 18))
    assert transport.requests == []


@pytest.mark.asyncio
async def test_requests_are_metered_against_the_massive_bucket() -> None:
    limiter = HostRateLimiter(clock=lambda: 0.0, sleep=_never_sleep)
    assert limiter.bucket_for(MASSIVE_HOST).capacity == 5.0
    provider, _ = build(untickered_without_next, limiter=limiter)
    await provider.news_since(SINCE)
    assert limiter.bucket_for(MASSIVE_HOST).available == pytest.approx(4.0)


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_recorded_page_decodes_into_articles_with_insights() -> None:
    provider, _ = build(untickered_without_next)
    result = await provider.news_since(SINCE)

    assert isinstance(result, MassiveNews)
    rows = fixture_body("p3_reference_news_untickered")["results"]
    assert len(result.articles) == len(rows)
    by_id = {a.vendor_id: a for a in result.articles}
    for row in rows:
        article = by_id[row["id"]]
        assert article.vendor == "massive"
        assert article.feed is NewsFeed.MASSIVE_NEWS
        assert article.headline == row["title"]
        assert article.url == row["article_url"]
        assert article.publisher == row["publisher"]["name"]
        assert article.summary == row["description"]
        assert article.tickers == tuple(t.upper() for t in row["tickers"])
        assert article.published_at.tzinfo is timezone.utc
        assert article.insights == tuple(
            VendorInsight(
                ticker=i["ticker"],
                sentiment=i["sentiment"],
                reasoning=i["sentiment_reasoning"],
            )
            for i in row["insights"]
        )
    first = by_id[rows[0]["id"]]
    assert first.published_at == datetime(2026, 9, 24, 3, 35, tzinfo=timezone.utc)
    assert first.insights[0].sentiment == "neutral"


@pytest.mark.asyncio
async def test_a_mixed_insight_is_kept_verbatim() -> None:
    """SYNTHETIC: ``mixed`` is real (3 of 7,843 in step 0) but not on disk yet."""
    body = json.loads(synthetic_page(["2026-09-24T12:55:00Z"], next_url=None))
    body["results"][0]["insights"] = [
        {"ticker": "nvda", "sentiment": "mixed", "sentiment_reasoning": "both ways"}
    ]
    provider, _ = build(lambda request: (200, json.dumps(body)))
    result = await provider.news_since(SINCE)
    assert result.articles[0].insights == (
        VendorInsight(ticker="NVDA", sentiment="mixed", reasoning="both ways"),
    )


@pytest.mark.asyncio
async def test_a_malformed_insight_is_dropped_and_the_article_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SYNTHETIC: one bad insight must not cost the article its other labels."""
    body = json.loads(synthetic_page(["2026-09-24T12:55:00Z"], next_url=None))
    body["results"][0]["insights"] = [
        {"ticker": "", "sentiment": "positive", "sentiment_reasoning": "x"},
        {"ticker": "NVDA", "sentiment": "positive", "sentiment_reasoning": "y"},
    ]
    provider, _ = build(lambda request: (200, json.dumps(body)))
    with caplog.at_level(logging.WARNING):
        result = await provider.news_since(SINCE)
    assert [i.ticker for i in result.articles[0].insights] == ["NVDA"]
    assert [r for r in caplog.records if getattr(r, "event", "") == "massive_insight_skipped"]


@pytest.mark.asyncio
async def test_a_malformed_row_is_skipped_logged_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SYNTHETIC: a row with no timestamp and a row with no URL."""
    body = json.loads(
        synthetic_page(
            ["2026-09-24T12:55:00Z", "2026-09-24T12:56:00Z", "2026-09-24T12:57:00Z"],
            next_url=None,
        )
    )
    body["results"][0]["published_utc"] = None
    del body["results"][1]["article_url"]
    provider, _ = build(lambda request: (200, json.dumps(body)))
    with caplog.at_level(logging.WARNING):
        result = await provider.news_since(SINCE)

    assert [a.vendor_id for a in result.articles] == ["synthetic-2"]
    assert result.skipped == 2
    assert len([r for r in caplog.records if getattr(r, "event", "") == "massive_news_row_skipped"]) == 2


@pytest.mark.asyncio
async def test_a_timestamp_that_overflows_utc_skips_only_its_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SYNTHETIC: ``0001-01-01T00:30:00+01:00`` raises OverflowError on the
    UTC conversion. One such row must cost that row, not the call -- a raise
    here would repeat on every poll from the same cursor."""
    body = json.loads(
        synthetic_page(
            ["2026-09-24T12:55:00Z", "2026-09-24T12:56:00Z"],
            next_url=None,
        )
    )
    body["results"][0]["published_utc"] = "0001-01-01T00:30:00+01:00"
    provider, _ = build(lambda request: (200, json.dumps(body)))
    with caplog.at_level(logging.WARNING):
        result = await provider.news_since(SINCE)

    assert [a.vendor_id for a in result.articles] == ["synthetic-1"]
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_a_body_without_a_results_list_is_an_error() -> None:
    provider, _ = build(lambda request: (200, '{"status":"ERROR"}'))
    with pytest.raises(NewsProviderError, match="results"):
        await provider.news_since(SINCE)


# --------------------------------------------------------------------------
# Pagination and the cursor
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_recorded_ascending_pages_follow_next_url_and_advance_the_cursor() -> None:
    first = fixture_body("p4_reference_news_asc_page1")
    second_url = first["next_url"]

    def route(request: httpx.Request) -> Served | None:
        if "cursor" in request.url.params:
            assert str(request.url) == second_url
            return "p4_reference_news_asc_page2"
        return "p4_reference_news_asc_page1"

    provider, transport = build(route, max_pages=2)
    result = await provider.news_since(SINCE)

    assert len(transport.requests) == 2
    assert transport.requests[1].headers["Authorization"] == f"Bearer {TEST_CREDENTIALS.token}"
    stamps = [
        row["published_utc"]
        for name in ("p4_reference_news_asc_page1", "p4_reference_news_asc_page2")
        for row in fixture_body(name)["results"]
    ]
    assert stamps == sorted(stamps), "the recorded pages are ascending"
    assert result.pages == 2
    # page 2 carries a next_url of its own, so stopping there is incomplete,
    # and an incomplete read backs its cursor one second off the newest read
    assert fixture_body("p4_reference_news_asc_page2").get("next_url")
    assert not result.complete
    newest = max(a.published_at for a in result.articles)
    assert result.cursor == newest.replace(microsecond=0) - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_pagination_stops_when_there_is_no_next_url() -> None:
    """SYNTHETIC pages."""
    page2 = "https://api.massive.com/v2/reference/news?cursor=abc"

    def route(request: httpx.Request) -> Served | None:
        if request.url.params.get("cursor") == "abc":
            return (200, synthetic_page(["2026-09-24T13:00:00Z"], next_url=None, first=2))
        return (
            200,
            synthetic_page(
                ["2026-09-24T12:55:00Z", "2026-09-24T12:55:00Z"], next_url=page2
            ),
        )

    provider, transport = build(route)
    result = await provider.news_since(SINCE)

    assert len(transport.requests) == 2
    assert result.complete
    assert result.pages == 2
    assert [a.vendor_id for a in result.articles] == [
        "synthetic-2",
        "synthetic-0",
        "synthetic-1",
    ]
    assert result.cursor == datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_the_page_cap_stops_early_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """SYNTHETIC: every page points at another."""
    counter = {"n": 0}

    def route(request: httpx.Request) -> Served | None:
        counter["n"] += 1
        n = counter["n"]
        return (
            200,
            synthetic_page(
                [f"2026-09-24T12:{n:02d}:00Z"],
                next_url=f"https://api.massive.com/v2/reference/news?cursor=p{n}",
                first=n,
            ),
        )

    provider, transport = build(route, max_pages=3)
    with caplog.at_level(logging.WARNING):
        result = await provider.news_since(SINCE)

    assert len(transport.requests) == 3
    assert not result.complete
    # one second below the newest article read -- see the tie test below
    assert result.cursor == datetime(2026, 9, 24, 12, 2, 59, tzinfo=timezone.utc)
    assert [r for r in caplog.records if getattr(r, "event", "") == "massive_news_page_cap"]


class FilteringServer:
    """SYNTHETIC Massive: honours ``published_utc.gt`` and pages ascending.

    Serves ``page_size`` rows a page whatever ``limit`` says, so a handful of
    articles is enough to cross the page cap. ``next_url`` carries the
    filter and an offset, the way a real opaque cursor pins a query.
    """

    def __init__(self, stamps: list[str], *, page_size: int) -> None:
        self.stamps = stamps
        self.page_size = page_size

    def __call__(self, request: httpx.Request) -> Served | None:
        params = request.url.params
        if "cursor" in params:
            gt, offset_text = params["cursor"].split("~")
            offset = int(offset_text)
        else:
            gt, offset = params["published_utc.gt"], 0
        floor = datetime.fromisoformat(gt.replace("Z", "+00:00"))
        matching = [
            (index, stamp)
            for index, stamp in enumerate(self.stamps)
            if datetime.fromisoformat(stamp.replace("Z", "+00:00")) > floor
        ]
        page = matching[offset : offset + self.page_size]
        more = offset + self.page_size < len(matching)
        body = json.loads(synthetic_page([stamp for _, stamp in page], next_url=None))
        for row, (index, _) in zip(body["results"], page):
            row["id"] = f"synthetic-{index}"
        if more:
            body["next_url"] = (
                f"https://api.massive.com/v2/reference/news?cursor={gt}~{offset + self.page_size}"
            )
        return (200, json.dumps(body))


@pytest.mark.asyncio
async def test_a_tie_across_the_page_cap_is_read_by_the_next_call() -> None:
    """The audit's case: page 3 ends on an article stamped ``12:55:00Z`` and
    page 4 starts with two more at ``12:55:00Z``. With the cursor at the
    newest stamp read, the next call's ``.gt=12:55:00Z`` never returns those
    two and nothing says so. One second below it, they are re-read; the
    article already held comes back too, harmlessly -- the store keys on
    ``(vendor, vendor_id)``.
    """
    stamps = [
        "2026-09-24T12:50:00Z",
        "2026-09-24T12:51:00Z",
        "2026-09-24T12:52:00Z",
        "2026-09-24T12:53:00Z",
        "2026-09-24T12:54:00Z",
        "2026-09-24T12:55:00Z",  # last row of page 3
        "2026-09-24T12:55:00Z",  # page 4
        "2026-09-24T12:55:00Z",  # page 4
        "2026-09-24T12:56:00Z",
    ]
    server = FilteringServer(stamps, page_size=2)
    provider, transport = build(server, max_pages=3)

    first = await provider.news_since(SINCE)
    assert not first.complete
    assert {a.vendor_id for a in first.articles} == {f"synthetic-{i}" for i in range(6)}
    assert first.cursor == datetime(2026, 9, 24, 12, 54, 59, tzinfo=timezone.utc)

    before = len(transport.requests)
    second = await provider.news_since(first.cursor)
    assert second.complete
    assert transport.requests[before].url.params["published_utc.gt"] == "2026-09-24T12:54:59Z"
    seen = {a.vendor_id for a in first.articles} | {a.vendor_id for a in second.articles}
    assert seen == {f"synthetic-{i}" for i in range(len(stamps))}, "no article lost to the tie"
    assert second.cursor == datetime(2026, 9, 24, 12, 56, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_an_incomplete_read_never_moves_the_cursor_below_where_it_started() -> None:
    """SYNTHETIC: every article read shares the second just after the cursor,
    so one second below it is the start. The cursor stays put rather than
    moving backwards -- no progress, but nothing skipped."""
    start = datetime(2026, 9, 24, 12, 54, 59, tzinfo=timezone.utc)
    server = FilteringServer(["2026-09-24T12:55:00Z"] * 5, page_size=1)
    provider, _ = build(server, max_pages=2)
    result = await provider.news_since(start)
    assert not result.complete
    assert result.cursor == start


@pytest.mark.asyncio
async def test_the_caller_may_pass_any_earlier_cursor_for_an_overlap() -> None:
    """The poller re-reads ``cursor - overlap`` to catch late arrivals; any
    aware instant is accepted and sent to the second, in UTC."""
    provider, transport = build(lambda request: (200, synthetic_page([], next_url=None)))
    eastern = timezone(timedelta(hours=-4))
    await provider.news_since(datetime(2026, 9, 24, 6, 30, 15, 900_000, tzinfo=eastern))
    assert transport.requests[0].url.params["published_utc.gt"] == "2026-09-24T10:30:15Z"


@pytest.mark.asyncio
async def test_an_empty_answer_keeps_the_cursor_where_it_was() -> None:
    provider, _ = build(lambda request: (200, synthetic_page([], next_url=None)))
    result = await provider.news_since(SINCE)
    assert result.articles == ()
    assert result.cursor == SINCE
    assert result.complete


@pytest.mark.asyncio
async def test_a_next_url_on_another_host_is_refused_and_never_sent_the_key() -> None:
    """Following it would hand the bearer token to whoever the URL names."""
    provider, transport = build(
        lambda request: (
            200,
            synthetic_page(
                ["2026-09-24T12:55:00Z"], next_url="https://evil.example.com/v2/reference/news?cursor=x"
            ),
        )
    )
    with pytest.raises(NewsProviderError, match="next_url"):
        await provider.news_since(SINCE)
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_url",
    [
        "https://api.massive.com:8443/v2/reference/news?cursor=x",
        "http://api.massive.com/v2/reference/news?cursor=x",
        "https://someone@api.massive.com/v2/reference/news?cursor=x",
    ],
)
async def test_a_next_url_on_another_origin_of_the_same_host_is_refused(next_url: str) -> None:
    """Same host is not same origin: another port or scheme is another server
    as far as the bearer token is concerned, and userinfo would re-author the
    request's credentials."""
    provider, transport = build(
        lambda request: (200, synthetic_page(["2026-09-24T12:55:00Z"], next_url=next_url))
    )
    with pytest.raises(NewsProviderError, match="next_url"):
        await provider.news_since(SINCE)
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_a_next_url_naming_the_default_port_explicitly_is_followed() -> None:
    """``:443`` on https is the configured origin, spelled out."""
    explicit = "https://api.massive.com:443/v2/reference/news?cursor=abc"

    def route(request: httpx.Request) -> Served | None:
        if request.url.params.get("cursor") == "abc":
            return (200, synthetic_page(["2026-09-24T13:00:00Z"], next_url=None, first=1))
        return (200, synthetic_page(["2026-09-24T12:55:00Z"], next_url=explicit))

    provider, transport = build(route)
    result = await provider.news_since(SINCE)
    assert len(transport.requests) == 2
    assert result.complete


@pytest.mark.asyncio
async def test_a_next_url_carrying_a_key_is_stripped_before_it_is_followed() -> None:
    """SYNTHETIC: if the vendor ever echoes ``apiKey`` into ``next_url``, no
    URL this provider sends may carry it -- the header authenticates."""
    leaky = f"https://api.massive.com/v2/reference/news?cursor=abc&apiKey={TEST_CREDENTIALS.token}"

    def route(request: httpx.Request) -> Served | None:
        if request.url.params.get("cursor") == "abc":
            return (200, synthetic_page(["2026-09-24T13:00:00Z"], next_url=None, first=1))
        return (200, synthetic_page(["2026-09-24T12:55:00Z"], next_url=leaky))

    provider, transport = build(route)
    await provider.news_since(SINCE)

    assert len(transport.requests) == 2
    for request in transport.requests:
        assert TEST_CREDENTIALS.token not in str(request.url)
        assert "apiKey" not in request.url.params


# --------------------------------------------------------------------------
# Failures and redaction
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_recorded_401_is_access_denied_and_names_the_variable() -> None:
    """Recorded: Massive's real answer to a key it does not recognise. The
    fixture's own ``note`` says what it is not -- a plan refusal to a valid
    key was not recorded, and may differ."""
    envelope = fixture_envelope("p4_unauthorized")
    assert envelope["status_code"] == 401
    assert "invalid key" in envelope["note"]
    provider, _ = build(lambda request: "p4_unauthorized")
    with pytest.raises(NewsAccessDenied) as excinfo:
        await provider.news_since(SINCE)
    message = str(excinfo.value)
    assert MASSIVE_API_KEY_ENV in message
    assert "401" in message
    assert envelope["body"]["error"] in message, "the vendor's reason is quoted"


@pytest.mark.asyncio
async def test_a_429_names_the_five_per_minute_budget() -> None:
    provider, _ = build(lambda request: (429, '{"status":"ERROR"}'))
    with pytest.raises(NewsProviderError, match="5/min"):
        await provider.news_since(SINCE)


@pytest.mark.asyncio
async def test_a_transport_error_quoting_a_keyed_url_is_redacted() -> None:
    """httpx errors quote the URL; a URL with ``apiKey=`` would carry the key."""

    def explode(request: httpx.Request) -> Served | None:
        raise httpx.ConnectError(
            f"failed GET https://api.massive.com/v2/reference/news?apiKey={TEST_CREDENTIALS.token}"
            " and apiKey=someOtherKeyValue123",
            request=request,
        )

    provider, _ = build(explode)
    with pytest.raises(NewsProviderError) as excinfo:
        await provider.news_since(SINCE)
    message = str(excinfo.value)
    assert TEST_CREDENTIALS.token not in message
    assert "someOtherKeyValue123" not in message
    assert "<redacted>" in message


@pytest.mark.asyncio
async def test_a_successful_body_is_not_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrubbing is for text about to be quoted; a 200 quotes nothing."""
    provider, _ = build(untickered_without_next)
    calls: list[str] = []

    def spy(text: str) -> str:
        calls.append(text)
        return text

    monkeypatch.setattr(provider, "_scrub", spy)
    await provider.news_since(SINCE)
    assert calls == []


@pytest.mark.asyncio
async def test_a_skipped_row_is_logged_scrubbed_and_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SYNTHETIC: a vendor id and a timestamp carrying the key and running
    long. The row-skip log quotes both, so both go through the scrub."""
    body = json.loads(synthetic_page(["2026-09-24T12:55:00Z"], next_url=None))
    body["results"][0]["id"] = f"id-{TEST_CREDENTIALS.token}-" + "x" * 5000
    body["results"][0]["published_utc"] = f"when-{TEST_CREDENTIALS.token}-" + "y" * 5000
    provider, _ = build(lambda request: (200, json.dumps(body)))
    with caplog.at_level(logging.WARNING):
        result = await provider.news_since(SINCE)
    assert result.skipped == 1
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "massive_news_row_skipped"]
    quoted = (record.getMessage(), str(getattr(record, "vendor_id")), str(getattr(record, "cause")))
    for text in quoted:
        assert TEST_CREDENTIALS.token not in text
    assert len(record.getMessage()) < 1000
    assert all(len(text) < 400 for text in quoted[1:])


@pytest.mark.asyncio
async def test_a_skipped_insight_is_logged_scrubbed_and_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SYNTHETIC: a long vendor id carrying the key, on an article whose one
    insight is malformed. The article is kept; the insight-skip log quotes
    the id, so it goes through the scrub too."""
    body = json.loads(synthetic_page(["2026-09-24T12:55:00Z"], next_url=None))
    body["results"][0]["id"] = f"id-{TEST_CREDENTIALS.token}-" + "x" * 5000
    body["results"][0]["insights"] = [{"ticker": "NVDA", "sentiment": 7}]
    provider, _ = build(lambda request: (200, json.dumps(body)))
    with caplog.at_level(logging.WARNING):
        result = await provider.news_since(SINCE)
    assert result.skipped == 0 and len(result.articles) == 1
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "massive_insight_skipped"]
    for text in (record.getMessage(), str(getattr(record, "vendor_id"))):
        assert TEST_CREDENTIALS.token not in text
        assert len(text) < 1000


@pytest.mark.asyncio
async def test_an_error_body_echoing_the_key_is_redacted() -> None:
    provider, _ = build(
        lambda request: (500, f'{{"error":"bad key {TEST_CREDENTIALS.token}"}}')
    )
    with pytest.raises(NewsProviderError) as excinfo:
        await provider.news_since(SINCE)
    assert TEST_CREDENTIALS.token not in str(excinfo.value)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def test_a_missing_key_names_the_variable() -> None:
    with pytest.raises(MassiveCredentialsError) as excinfo:
        MassiveCredentials.from_env({})
    assert MASSIVE_API_KEY_ENV in str(excinfo.value)
    with pytest.raises(MassiveCredentialsError):
        MassiveCredentials.from_env({MASSIVE_API_KEY_ENV: "  "})


def test_a_missing_key_leaves_the_provider_unavailable_rather_than_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Never a boot failure: the feed is optional, the app is not."""
    with caplog.at_level(logging.WARNING):
        assert MassiveProvider.available_from_env({}) is None
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "massive_unavailable"]
    assert MASSIVE_API_KEY_ENV in record.getMessage()


@pytest.mark.asyncio
async def test_a_present_key_builds_a_provider() -> None:
    provider = MassiveProvider.available_from_env(
        {MASSIVE_API_KEY_ENV: TEST_CREDENTIALS.token}, limiter=generous_limiter()
    )
    assert isinstance(provider, MassiveProvider)
    await provider.aclose()  # it opened its own httpx.AsyncClient


def test_the_key_is_never_printable() -> None:
    credentials = MassiveCredentials(token="super-secret-massive-key")
    assert "super-secret-massive-key" not in repr(credentials)
    assert "super-secret-massive-key" not in str(credentials)


def test_the_massive_fixtures_carry_no_key_material() -> None:
    for path in FIXTURE_DIR.glob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        assert "apikey" not in text
        assert "api_key" not in text
        assert "bearer" not in text
