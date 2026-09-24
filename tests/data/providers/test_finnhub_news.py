"""Finnhub company news and market news, replayed. No test here makes a live call.

Recorded fixtures (``tests/fixtures/record_finnhub.py news``, 2026-09-24):

* ``p4_company_news_nvda`` -- NVDA, 2026-09-23 -> 2026-09-24: **248 rows, at
  the cap**. Kept truncated to 12 rows; ``stats.row_count`` records the 248.
* ``p4_company_news_arm`` / ``_arm_30d`` -- ARM over two days (15 rows) and
  thirty (142): the non-North-American suspicion did not hold for ARM.
* ``p4_market_news_general`` -- one ``category=general`` page: 100 rows, **0**
  with a non-empty ``related``. Kept truncated to 12 rows.

Every body built in test code below is **synthetic** and says so: the cap
needs ~250 rows, and a recorded file that size is not worth its review.
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx
import pytest

from corollary.data.news.article import (
    NewsAccessDenied,
    NewsFeed,
    NewsProviderError,
)
from corollary.data.providers.finnhub import (
    COMPANY_NEWS_CAP_THRESHOLD,
    MARKET_NEWS_ID_PREFIX,
    MARKET_NEWS_PAGE_SIZE,
    CompanyNews,
    FinnhubProvider,
    MarketNews,
)
from corollary.ratelimit import FINNHUB_HOST, HostRateLimiter
from tests.data.providers.test_finnhub_provider import (
    FIXTURE_DIR,
    TEST_CREDENTIALS,
    ProviderFactory,
    RecordingTransport,
    Route,
    Served,
    _never_sleep,
    fixture_body_bytes,
    limiter,  # noqa: F401 - pytest fixture, used through make_provider
    make_provider,  # noqa: F401 - pytest fixture
)

YESTERDAY = date(2026, 9, 23)
TODAY = date(2026, 9, 24)
OVERFLOW = "finnhub_company_news_overflow"


def recorded(name: str) -> list[dict[str, Any]]:
    body: list[dict[str, Any]] = json.loads(fixture_body_bytes(name))
    return body


def recorded_stats(name: str) -> dict[str, Any]:
    stats: dict[str, Any] = json.loads(
        (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    )["stats"]
    return stats


def synthetic_rows(count: int, *, day: date, first_id: int = 1000) -> str:
    """SYNTHETIC company-news rows, in Finnhub's recorded shape, newest first."""
    base = int(datetime(day.year, day.month, day.day, 23, tzinfo=timezone.utc).timestamp())
    rows = [
        {
            "category": "company",
            "datetime": base - (first_id + index) * 10,
            "headline": f"synthetic headline {first_id + index}",
            "id": first_id + index,
            "image": "",
            "related": "NVDA",
            "source": "Synthetic",
            "summary": "synthetic",
            "url": f"https://example.com/{first_id + index}",
        }
        for index in range(count)
    ]
    return json.dumps(rows)


def by_window(mapping: dict[tuple[str, str], Served]) -> Route:
    """Serve per ``(from, to)`` on ``/company-news``."""

    def choose(request: httpx.Request) -> Served | None:
        if request.url.path != "/api/v1/company-news":
            return None
        return mapping.get((request.url.params["from"], request.url.params["to"]))

    return choose


def windows(transport: RecordingTransport) -> list[tuple[str, str]]:
    return [(r.url.params["from"], r.url.params["to"]) for r in transport.requests]


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", "") == name]


def company_route(name: str) -> Route:
    def choose(request: httpx.Request) -> Served | None:
        return name if request.url.path == "/api/v1/company-news" else None

    return choose


# --------------------------------------------------------------------------
# Company news: decoding
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_company_news_decodes_the_recorded_nvda_body(
    make_provider: ProviderFactory,
) -> None:
    provider, transport = make_provider(company_route("p4_company_news_nvda"))
    result = await provider.company_news("NVDA", YESTERDAY, TODAY)

    assert isinstance(result, CompanyNews)
    assert dict(transport.requests[0].url.params) == {
        "symbol": "NVDA",
        "from": "2026-09-23",
        "to": "2026-09-24",
    }
    rows = recorded("p4_company_news_nvda")
    assert len(result.articles) == len(rows)
    first = next(a for a in result.articles if a.vendor_id == str(rows[0]["id"]))
    assert first.vendor == "finnhub"
    assert first.feed is NewsFeed.FINNHUB_COMPANY
    assert first.headline == rows[0]["headline"]
    assert first.summary == rows[0]["summary"]
    assert first.publisher == rows[0]["source"]
    assert first.url == rows[0]["url"]
    assert first.published_at == datetime.fromtimestamp(rows[0]["datetime"], timezone.utc)
    assert first.published_at.tzinfo is timezone.utc
    assert first.insights == ()


@pytest.mark.asyncio
async def test_a_company_news_row_is_tagged_with_the_queried_symbol(
    make_provider: ProviderFactory,
) -> None:
    """Decision 21: the tag is the queried symbol, not the vendor's reading.

    SYNTHETIC body: ``related`` names another company. The tag must still be
    the symbol that was asked for.
    """
    rows = json.loads(synthetic_rows(1, day=TODAY))
    rows[0]["related"] = "ORCL"
    provider, transport = make_provider(lambda request: (200, json.dumps(rows)))
    result = await provider.company_news("alab", TODAY, TODAY)

    assert transport.requests[0].url.params["symbol"] == "ALAB"
    assert result.symbol == "ALAB"
    assert result.articles[0].tickers == ("ALAB",)


@pytest.mark.asyncio
async def test_arm_returns_rows_despite_the_north_american_only_note(
    make_provider: ProviderFactory,
) -> None:
    provider, _ = make_provider(company_route("p4_company_news_arm"))
    result = await provider.company_news("ARM", YESTERDAY, TODAY)

    assert recorded_stats("p4_company_news_arm")["row_count"] == 15
    assert recorded_stats("p4_company_news_arm_30d")["row_count"] == 142
    assert result.articles
    assert all(a.tickers == ("ARM",) for a in result.articles)


@pytest.mark.asyncio
async def test_articles_come_back_newest_first_and_deterministically(
    make_provider: ProviderFactory,
) -> None:
    provider, _ = make_provider(company_route("p4_company_news_nvda"))
    first = await provider.company_news("NVDA", YESTERDAY, TODAY)
    again = await provider.company_news("NVDA", YESTERDAY, TODAY)

    assert first.articles == again.articles
    stamps = [a.published_at for a in first.articles]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.asyncio
async def test_a_malformed_row_is_skipped_logged_and_counted(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """SYNTHETIC: one row with no headline, one with a string timestamp, one
    with a boolean id."""
    rows = json.loads(synthetic_rows(4, day=TODAY))
    rows[0]["headline"] = ""
    rows[1]["datetime"] = "yesterday"
    rows[2]["id"] = True
    provider, _ = make_provider(lambda request: (200, json.dumps(rows)))
    with caplog.at_level(logging.WARNING):
        result = await provider.company_news("NVDA", TODAY, TODAY)

    assert [a.vendor_id for a in result.articles] == [str(rows[3]["id"])]
    assert result.skipped == 3
    assert len(events(caplog, "finnhub_news_row_skipped")) == 3


@pytest.mark.asyncio
async def test_a_body_that_is_not_a_list_is_an_error(
    make_provider: ProviderFactory,
) -> None:
    provider, _ = make_provider(lambda request: (200, '{"error":"surprise"}'))
    with pytest.raises(NewsProviderError, match="not a list"):
        await provider.company_news("NVDA", TODAY, TODAY)


@pytest.mark.asyncio
async def test_a_window_that_runs_backwards_is_refused_before_any_request(
    make_provider: ProviderFactory,
) -> None:
    provider, transport = make_provider(lambda request: None)
    with pytest.raises(ValueError):
        await provider.company_news("NVDA", TODAY, YESTERDAY)
    with pytest.raises(ValueError):
        await provider.company_news("  ", YESTERDAY, TODAY)
    assert transport.requests == []


# --------------------------------------------------------------------------
# Company news: the ~250-row cap
# --------------------------------------------------------------------------


def test_the_recorded_nvda_window_sits_at_the_cap() -> None:
    """Measured, not assumed: two NVDA days came back at 248 rows."""
    assert recorded_stats("p4_company_news_nvda")["row_count"] >= COMPANY_NEWS_CAP_THRESHOLD


@pytest.mark.asyncio
async def test_a_window_under_the_cap_makes_one_request(
    make_provider: ProviderFactory,
) -> None:
    under = COMPANY_NEWS_CAP_THRESHOLD - 1
    provider, transport = make_provider(
        by_window({("2026-09-23", "2026-09-24"): (200, synthetic_rows(under, day=TODAY))})
    )
    result = await provider.company_news("NVDA", YESTERDAY, TODAY)

    assert windows(transport) == [("2026-09-23", "2026-09-24")]
    assert not result.retried_today
    assert result.overflow_date is None
    assert len(result.articles) == under


@pytest.mark.asyncio
async def test_a_capped_window_is_re_requested_for_today_alone_and_merged(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """The spec's rule: *"A response at the ~250-row cap is re-requested for
    today alone."* SYNTHETIC bodies: the window answers at the cap and today
    alone answers under it, overlapping the window on 50 ids."""
    window = synthetic_rows(COMPANY_NEWS_CAP_THRESHOLD, day=TODAY, first_id=1000)
    today_alone = synthetic_rows(
        120, day=TODAY, first_id=1000 + COMPANY_NEWS_CAP_THRESHOLD - 50
    )
    provider, transport = make_provider(
        by_window(
            {
                ("2026-09-23", "2026-09-24"): (200, window),
                ("2026-09-24", "2026-09-24"): (200, today_alone),
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        result = await provider.company_news("NVDA", YESTERDAY, TODAY)

    assert windows(transport) == [
        ("2026-09-23", "2026-09-24"),
        ("2026-09-24", "2026-09-24"),
    ]
    assert result.retried_today
    assert result.overflow_date is None
    ids = [a.vendor_id for a in result.articles]
    assert len(ids) == len(set(ids)) == COMPANY_NEWS_CAP_THRESHOLD + 120 - 50
    assert not events(caplog, OVERFLOW)


@pytest.mark.asyncio
async def test_today_alone_still_at_the_cap_is_logged_and_not_retried(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """*"If today alone is still at the cap, the overflow is lost for that
    symbol and day ... logged with the symbol and date rather than retried."*"""
    capped = synthetic_rows(COMPANY_NEWS_CAP_THRESHOLD, day=TODAY)
    provider, transport = make_provider(
        by_window(
            {
                ("2026-09-23", "2026-09-24"): (200, capped),
                ("2026-09-24", "2026-09-24"): (200, capped),
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        result = await provider.company_news("NVDA", YESTERDAY, TODAY)

    assert len(transport.requests) == 2
    assert result.retried_today
    assert result.overflow_date == TODAY
    [event] = events(caplog, OVERFLOW)
    assert getattr(event, "symbol") == "NVDA"
    assert getattr(event, "date") == "2026-09-24"
    assert "NVDA" in event.getMessage() and "2026-09-24" in event.getMessage()


@pytest.mark.asyncio
async def test_a_single_day_window_at_the_cap_is_logged_without_a_second_request(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Today alone *is* the window, so there is nothing finer to ask for."""
    provider, transport = make_provider(
        by_window(
            {
                ("2026-09-24", "2026-09-24"): (
                    200,
                    synthetic_rows(COMPANY_NEWS_CAP_THRESHOLD, day=TODAY),
                ),
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        result = await provider.company_news("NVDA", TODAY, TODAY)

    assert len(transport.requests) == 1
    assert not result.retried_today
    assert result.overflow_date == TODAY
    assert events(caplog, OVERFLOW)


# --------------------------------------------------------------------------
# Market news
# --------------------------------------------------------------------------


def market_route(request: httpx.Request) -> Served | None:
    if (
        request.url.path == "/api/v1/news"
        and request.url.params.get("category") == "general"
    ):
        return "p4_market_news_general"
    return None


@pytest.mark.asyncio
async def test_market_news_decodes_the_recorded_general_page(
    make_provider: ProviderFactory,
) -> None:
    provider, transport = make_provider(market_route)
    result = await provider.market_news(None)

    assert isinstance(result, MarketNews)
    assert "minId" not in transport.requests[0].url.params
    rows = recorded("p4_market_news_general")
    assert len(result.articles) == len(rows)
    by_id = {a.vendor_id: a for a in result.articles}
    for row in rows:
        article = by_id[f"{MARKET_NEWS_ID_PREFIX}{row['id']}"]
        assert article.feed is NewsFeed.FINNHUB_MARKET
        assert article.vendor == "finnhub"
        assert article.headline == row["headline"]
        assert article.published_at == datetime.fromtimestamp(row["datetime"], timezone.utc)


@pytest.mark.asyncio
async def test_an_empty_related_field_means_a_market_item(
    make_provider: ProviderFactory,
) -> None:
    """Recorded: 0 of 100 general rows carried a ``related`` value."""
    assert recorded_stats("p4_market_news_general")["rows_with_related"] == 0
    provider, _ = make_provider(market_route)
    result = await provider.market_news(None)

    assert result.articles
    assert all(a.is_market for a in result.articles)


@pytest.mark.asyncio
async def test_related_is_split_on_commas_and_crypto_tags_pass_through(
    make_provider: ProviderFactory,
) -> None:
    """SYNTHETIC ``related``: dropping non-equity tags is the ingest's job."""
    rows = recorded("p4_market_news_general")[:1]
    rows[0]["related"] = "aapl, MSFT,,BINANCE:BTCUSDT "
    provider, _ = make_provider(lambda request: (200, json.dumps(rows)))
    result = await provider.market_news(None)

    assert result.articles[0].tickers == ("AAPL", "MSFT", "BINANCE:BTCUSDT")


@pytest.mark.asyncio
async def test_the_min_id_cursor_is_sent_and_advanced_to_the_largest_id_seen(
    make_provider: ProviderFactory,
) -> None:
    """The page is sorted by time, not id, so the cursor is the max over all rows.

    Recorded: the full page's largest id (8537432) was not its first row's
    (8537331). The kept rows are served reversed, so the largest id is
    neither first nor last and only a max over the page finds it.
    """
    rows = recorded("p4_market_news_general")
    assert recorded_stats("p4_market_news_general")["max_id"] != rows[0]["id"]
    largest = max(int(r["id"]) for r in rows)
    served = list(reversed(rows))
    served.append(served.pop(0))
    assert served[0]["id"] != largest and served[-1]["id"] != largest
    provider, transport = make_provider(lambda request: (200, json.dumps(served)))
    result = await provider.market_news(8_500_000)

    assert transport.requests[0].url.params["minId"] == "8500000"
    assert result.min_id == largest


@pytest.mark.asyncio
async def test_an_empty_page_keeps_the_cursor_where_it_was(
    make_provider: ProviderFactory,
) -> None:
    provider, _ = make_provider(lambda request: (200, "[]"))
    assert (await provider.market_news(8_537_432)).min_id == 8_537_432
    assert (await provider.market_news(None)).min_id is None


@pytest.mark.asyncio
async def test_a_negative_min_id_is_refused(make_provider: ProviderFactory) -> None:
    provider, transport = make_provider(lambda request: None)
    with pytest.raises(ValueError):
        await provider.market_news(-1)
    assert transport.requests == []


# --------------------------------------------------------------------------
# Failures, credentials, budget
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_premium_json_403_is_access_denied_not_a_transport_failure(
    make_provider: ProviderFactory,
) -> None:
    """Step 0's recorded premium answer, served on a news path."""
    provider, _ = make_provider(lambda request: "p3_premium_etf_holdings")
    with pytest.raises(NewsAccessDenied) as excinfo:
        await provider.market_news(None)
    assert "plan" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_transport_error_quoting_the_token_is_redacted(
    make_provider: ProviderFactory,
) -> None:
    def explode(request: httpx.Request) -> Served | None:
        raise httpx.ConnectError(
            f"refused, header {TEST_CREDENTIALS.token}", request=request
        )

    provider, _ = make_provider(explode)
    with pytest.raises(NewsProviderError) as excinfo:
        await provider.company_news("NVDA", YESTERDAY, TODAY)
    assert not isinstance(excinfo.value, NewsAccessDenied)
    assert TEST_CREDENTIALS.token not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_error_body_echoing_the_token_is_redacted(
    make_provider: ProviderFactory,
) -> None:
    provider, _ = make_provider(
        lambda request: (500, f'{{"error":"bad {TEST_CREDENTIALS.token}"}}')
    )
    with pytest.raises(NewsProviderError) as excinfo:
        await provider.market_news(None)
    assert TEST_CREDENTIALS.token not in str(excinfo.value)


@pytest.mark.asyncio
async def test_news_requests_are_metered_against_the_finnhub_bucket() -> None:
    limiter = HostRateLimiter(clock=lambda: 0.0, sleep=_never_sleep)
    transport = RecordingTransport(market_route)
    provider = FinnhubProvider(
        credentials=TEST_CREDENTIALS,
        client=httpx.AsyncClient(transport=transport),
        limiter=limiter,
    )
    await provider.market_news(None)

    assert limiter.bucket_for(FINNHUB_HOST).available == pytest.approx(59.0)
    assert "token" not in dict(transport.requests[0].url.params)


# --------------------------------------------------------------------------
# Audit nits: what the logs say, and what they must never quote
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_today_alone_at_the_threshold_is_a_possible_overflow_with_its_row_count(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """240-249 rows may be a complete answer: the threshold sits under the cap
    on purpose. The log says *possible* and carries the count, so a reader can
    tell 240 (probably whole) from 250 (certainly truncated)."""
    capped = synthetic_rows(COMPANY_NEWS_CAP_THRESHOLD, day=TODAY)
    provider, _ = make_provider(
        by_window(
            {
                ("2026-09-23", "2026-09-24"): (200, capped),
                ("2026-09-24", "2026-09-24"): (200, capped),
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        await provider.company_news("NVDA", YESTERDAY, TODAY)
    [event] = events(caplog, OVERFLOW)
    assert getattr(event, "rows") == COMPANY_NEWS_CAP_THRESHOLD
    message = event.getMessage()
    assert "possible" in message
    assert str(COMPANY_NEWS_CAP_THRESHOLD) in message
    assert "is lost" not in message


FULL_PAGE = "finnhub_market_news_full_page"


def synthetic_market_rows(count: int, *, first_id: int = 9_000_000) -> str:
    """SYNTHETIC general-news rows in the recorded shape."""
    base = int(datetime(2026, 9, 24, 20, tzinfo=timezone.utc).timestamp())
    return json.dumps(
        [
            {
                "category": "top news",
                "datetime": base - index * 60,
                "headline": f"synthetic market headline {index}",
                "id": first_id + index,
                "image": "",
                "related": "",
                "source": "Synthetic",
                "summary": "synthetic",
                "url": f"https://example.com/m/{index}",
            }
            for index in range(count)
        ]
    )


@pytest.mark.asyncio
async def test_a_full_market_page_after_a_cursor_is_a_warning(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """A full page after ``minId`` may have dropped rows between the cursor
    and the oldest row served. Say so rather than advancing in silence."""
    provider, _ = make_provider(
        lambda request: (200, synthetic_market_rows(MARKET_NEWS_PAGE_SIZE))
    )
    with caplog.at_level(logging.INFO):
        await provider.market_news(8_500_000)
    [event] = events(caplog, FULL_PAGE)
    assert event.levelno == logging.WARNING
    assert getattr(event, "rows") == MARKET_NEWS_PAGE_SIZE
    assert getattr(event, "min_id") == 8_500_000


@pytest.mark.asyncio
async def test_a_page_under_the_size_is_silent(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    provider, _ = make_provider(
        lambda request: (200, synthetic_market_rows(MARKET_NEWS_PAGE_SIZE - 1))
    )
    with caplog.at_level(logging.INFO):
        await provider.market_news(8_500_000)
    assert not events(caplog, FULL_PAGE)


@pytest.mark.asyncio
async def test_a_full_first_page_with_no_cursor_is_noted_not_warned(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Recorded: a cold start's page is full -- the fixture's ``stats`` say
    100 rows, though its body keeps 12 for review. With no cursor there is
    nothing it could have skipped, and a warning on every boot is how
    warnings stop being read. The 100 rows served are SYNTHETIC."""
    assert recorded_stats("p4_market_news_general")["row_count"] == MARKET_NEWS_PAGE_SIZE
    provider, _ = make_provider(
        lambda request: (200, synthetic_market_rows(MARKET_NEWS_PAGE_SIZE))
    )
    with caplog.at_level(logging.INFO):
        await provider.market_news(None)
    [event] = events(caplog, FULL_PAGE)
    assert event.levelno == logging.INFO


@pytest.mark.asyncio
async def test_a_skipped_row_is_logged_scrubbed_and_bounded(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """SYNTHETIC: an id and a timestamp carrying the token and running long.
    The row-skip log quotes both, so both go through the provider's scrub."""
    rows = json.loads(synthetic_rows(2, day=TODAY))
    rows[0]["id"] = f"id-{TEST_CREDENTIALS.token}-" + "x" * 5000
    rows[1]["datetime"] = f"when-{TEST_CREDENTIALS.token}-" + "y" * 5000
    provider, _ = make_provider(lambda request: (200, json.dumps(rows)))
    with caplog.at_level(logging.WARNING):
        result = await provider.company_news("NVDA", TODAY, TODAY)
    assert result.skipped == 2
    skipped = events(caplog, "finnhub_news_row_skipped")
    assert len(skipped) == 2
    for record in skipped:
        quoted = (
            record.getMessage(),
            str(getattr(record, "vendor_id")),
            str(getattr(record, "cause")),
        )
        for text in quoted:
            assert TEST_CREDENTIALS.token not in text
        assert len(record.getMessage()) < 1000
        assert all(len(text) < 400 for text in quoted[1:])


def test_the_public_names_are_listed_alphabetically() -> None:
    from corollary.data.providers import finnhub

    assert list(finnhub.__all__) == sorted(finnhub.__all__)
