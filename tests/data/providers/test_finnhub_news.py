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
from datetime import date, datetime, timedelta, timezone
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
    PUBLISHER_CLOCK_FUTURE_TOLERANCE,
    CompanyNews,
    FinnhubProvider,
    MarketNews,
    correct_publisher_clock,
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


# --------------------------------------------------------------------------
# Benzinga's clock: Finnhub stamps its Eastern wall time as UTC
# --------------------------------------------------------------------------
#
# Measured 2026-09-25 -> 2026-10-09 across all three vendors: 1390 of 1398
# true twins of an Alpaca (Benzinga's own feed) story sat exactly 240 minutes
# early on Finnhub when Finnhub's ``source`` was "Benzinga"; "Yahoo" rows
# matched at 0. Every instant below is SYNTHETIC.


def _utc(*fields: int) -> datetime:
    return datetime(*fields, tzinfo=timezone.utc)


def test_a_benzinga_stamp_in_edt_is_moved_four_hours_later() -> None:
    """MEASURED: -240 min in EDT. 09:30 'UTC' as decoded is 09:30 ET = 13:30 UTC."""
    assert correct_publisher_clock(_utc(2026, 9, 24, 9, 30), "Benzinga") == _utc(
        2026, 9, 24, 13, 30
    )


def test_a_benzinga_stamp_in_est_is_moved_five_hours_later() -> None:
    """PREDICTED, not measured: the fortnight measured lies wholly in EDT, so
    the -300 min EST offset is what the wall-clock reading implies and is
    unverified until November's data exists. Pinned so a change is deliberate."""
    assert correct_publisher_clock(_utc(2026, 12, 1, 9, 30), "Benzinga") == _utc(
        2026, 12, 1, 14, 30
    )


@pytest.mark.parametrize("publisher", ["Yahoo", None, "benzinga", "BENZINGA", " Benzinga"])
def test_any_other_publisher_is_returned_unchanged(publisher: str | None) -> None:
    """Yahoo matched the other vendors at 0. Case variants were never seen and
    are not guessed at: only the exact measured value is corrected."""
    stamp = _utc(2026, 9, 24, 9, 30)
    assert correct_publisher_clock(stamp, publisher) == stamp


def test_the_ambiguous_fall_back_hour_resolves_to_edt() -> None:
    """01:30 on 2026-11-01 happens twice in New York. fold=0 takes the first,
    EDT (-4h): 05:30 UTC, not EST's 06:30."""
    corrected = correct_publisher_clock(_utc(2026, 11, 1, 1, 30), "Benzinga")
    assert corrected == _utc(2026, 11, 1, 5, 30)


def test_the_spring_forward_gap_hour_resolves_deterministically() -> None:
    """02:30 on 2027-03-14 never happens in New York. zoneinfo with fold=0
    applies the offset in force *before* the transition (EST, -5h), so it lands
    on 07:30 UTC -- which is 03:30 EDT. Pinned as zoneinfo's answer, not as a
    claim about what Benzinga meant: no real story is stamped in that hour."""
    corrected = correct_publisher_clock(_utc(2027, 3, 14, 2, 30), "Benzinga")
    assert corrected == _utc(2027, 3, 14, 7, 30)


def test_the_correction_is_pure_and_keeps_the_result_aware_utc() -> None:
    stamp = _utc(2026, 9, 24, 9, 30)
    first = correct_publisher_clock(stamp, "Benzinga")
    second = correct_publisher_clock(stamp, "Benzinga")
    assert first == second
    assert stamp == _utc(2026, 9, 24, 9, 30)  # the input is not touched
    assert first.tzinfo is timezone.utc
    assert correct_publisher_clock(stamp, "Yahoo").tzinfo is timezone.utc


@pytest.mark.asyncio
async def test_the_decoder_corrects_benzinga_rows_and_leaves_yahoo_alone(
    make_provider: ProviderFactory,
) -> None:
    """SYNTHETIC body: two rows with one raw epoch, one per publisher."""
    raw = int(_utc(2026, 9, 24, 9, 30).timestamp())
    rows = json.loads(synthetic_rows(2, day=TODAY))
    rows[0].update({"source": "Benzinga", "datetime": raw})
    rows[1].update({"source": "Yahoo", "datetime": raw})
    provider, _ = make_provider(lambda request: (200, json.dumps(rows)))
    result = await provider.company_news("NVDA", TODAY, TODAY)

    by_publisher = {a.publisher: a.published_at for a in result.articles}
    assert by_publisher["Benzinga"] == _utc(2026, 9, 24, 13, 30)
    assert by_publisher["Yahoo"] == _utc(2026, 9, 24, 9, 30)


def test_the_last_edt_minute_of_fall_back_reads_as_edt() -> None:
    """2026-11-01 01:59 wall time, fold=0: the first (EDT) occurrence, 05:59Z."""
    assert correct_publisher_clock(_utc(2026, 11, 1, 1, 59), "Benzinga") == _utc(
        2026, 11, 1, 5, 59
    )


def test_the_first_minute_after_fall_back_reads_as_est() -> None:
    """2026-11-01 02:00 wall time is unambiguous EST: 07:00Z, an hour after 01:59's
    05:59Z plus the minute -- the repeated hour has no stamp of its own."""
    assert correct_publisher_clock(_utc(2026, 11, 1, 2, 0), "Benzinga") == _utc(
        2026, 11, 1, 7, 0
    )


def test_a_non_utc_aware_input_is_read_as_its_utc_instant() -> None:
    """The wall-clock fields read back are the UTC ones, whatever zone the
    caller handed in: 05:30-04:00 is 09:30Z, which corrects to 13:30Z. Without
    normalising first the fix would read 05:30 as Eastern and land on 09:30Z."""
    eastern_offset = timezone(timedelta(hours=-4))
    stamp = datetime(2026, 9, 24, 5, 30, tzinfo=eastern_offset)
    assert stamp == _utc(2026, 9, 24, 9, 30)
    assert correct_publisher_clock(stamp, "Benzinga") == _utc(2026, 9, 24, 13, 30)


def test_any_publisher_comes_back_in_utc_even_from_a_non_utc_input() -> None:
    stamp = datetime(2026, 9, 24, 5, 30, tzinfo=timezone(timedelta(hours=-4)))
    unchanged = correct_publisher_clock(stamp, "Yahoo")
    assert unchanged == stamp
    assert unchanged.tzinfo is timezone.utc


@pytest.mark.parametrize("publisher", ["Benzinga", "Yahoo", None])
def test_a_naive_input_is_refused_for_every_publisher(publisher: str | None) -> None:
    """A naive stamp has no instant to correct: guessing UTC or local is the
    silent multi-hour error the aware-datetime rule exists to stop."""
    with pytest.raises(ValueError, match="aware"):
        correct_publisher_clock(datetime(2026, 9, 24, 9, 30), publisher)


@pytest.mark.asyncio
async def test_a_benzinga_row_on_the_general_feed_is_not_corrected(
    make_provider: ProviderFactory,
) -> None:
    """SYNTHETIC body. The skew was measured on ``/company-news`` only; the
    general feed is excluded until it is measured, so its Benzinga row keeps the
    epoch exactly as served -- a correctly stamped row pushed 4h forward would
    sort above newer stories and break its link to the Alpaca twin."""
    raw = int(_utc(2026, 9, 24, 9, 30).timestamp())
    rows = json.loads(synthetic_market_rows(1))
    rows[0].update({"source": "Benzinga", "datetime": raw})
    provider, _ = make_provider(lambda request: (200, json.dumps(rows)))
    result = await provider.market_news(None)

    (only,) = result.articles
    assert only.feed is NewsFeed.FINNHUB_MARKET
    assert only.publisher == "Benzinga"
    assert only.published_at == _utc(2026, 9, 24, 9, 30)


def _benzinga_company_body(raw: datetime, *, vendor_id: int = 4242) -> str:
    rows = json.loads(synthetic_rows(1, day=TODAY, first_id=vendor_id))
    rows[0].update({"source": "Benzinga", "datetime": int(raw.timestamp())})
    return json.dumps(rows)


@pytest.mark.asyncio
async def test_a_correction_landing_past_now_keeps_the_raw_stamp_and_warns(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """SYNTHETIC. The day Finnhub fixes the bug upstream, a true-UTC stamp is
    pushed 4h into the future. A corrected stamp later than now plus the
    tolerance is impossible for a published story, so that row keeps its raw
    stamp and the event says which way the evidence points."""
    raw = _utc(2026, 9, 24, 13, 30)  # already true UTC: Finnhub has fixed it
    now = _utc(2026, 9, 24, 13, 35)
    provider, _ = make_provider(
        lambda request: (200, _benzinga_company_body(raw)), clock=lambda: now
    )
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.finnhub"):
        result = await provider.company_news("NVDA", TODAY, TODAY)

    (only,) = result.articles
    assert only.published_at == raw
    (record,) = events(caplog, "finnhub_publisher_clock_future")
    assert record.levelno == logging.WARNING
    fields: Any = record
    assert fields.raw_published_at == "2026-09-24T13:30:00+00:00"
    assert fields.corrected_published_at == "2026-09-24T17:30:00+00:00"
    assert fields.now == "2026-09-24T13:35:00+00:00"
    assert fields.vendor_id == "4242"
    assert fields.symbol == "NVDA"
    assert fields.publisher == "Benzinga"


@pytest.mark.asyncio
async def test_a_correction_exactly_at_the_tolerance_is_kept(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """The boundary permits: corrected == now + tolerance is a story that is
    just fresh with a little clock drift, not evidence of an upstream fix."""
    raw = _utc(2026, 9, 24, 9, 30)  # corrects to 13:30Z
    now = _utc(2026, 9, 24, 13, 30) - PUBLISHER_CLOCK_FUTURE_TOLERANCE
    provider, _ = make_provider(
        lambda request: (200, _benzinga_company_body(raw)), clock=lambda: now
    )
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.finnhub"):
        result = await provider.company_news("NVDA", TODAY, TODAY)

    (only,) = result.articles
    assert only.published_at == _utc(2026, 9, 24, 13, 30)
    assert events(caplog, "finnhub_publisher_clock_future") == []


@pytest.mark.asyncio
async def test_one_second_past_the_tolerance_keeps_the_raw_stamp(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    raw = _utc(2026, 9, 24, 9, 30)
    now = _utc(2026, 9, 24, 13, 30) - PUBLISHER_CLOCK_FUTURE_TOLERANCE - timedelta(seconds=1)
    provider, _ = make_provider(
        lambda request: (200, _benzinga_company_body(raw)), clock=lambda: now
    )
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.finnhub"):
        result = await provider.company_news("NVDA", TODAY, TODAY)

    (only,) = result.articles
    assert only.published_at == raw
    assert len(events(caplog, "finnhub_publisher_clock_future")) == 1


def test_the_future_tolerance_is_five_minutes() -> None:
    assert PUBLISHER_CLOCK_FUTURE_TOLERANCE == timedelta(minutes=5)


@pytest.mark.asyncio
async def test_the_future_warning_never_quotes_the_token(
    make_provider: ProviderFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """The vendor id goes through the provider's redactor. A token can only
    reach this log through vendor text, so the whole rendered record is checked."""
    raw = _utc(2026, 9, 24, 13, 30)
    now = _utc(2026, 9, 24, 13, 35)
    provider, _ = make_provider(
        lambda request: (200, _benzinga_company_body(raw)), clock=lambda: now
    )
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.finnhub"):
        await provider.company_news("NVDA", TODAY, TODAY)

    (record,) = events(caplog, "finnhub_publisher_clock_future")
    rendered = record.getMessage() + repr(vars(record))
    assert TEST_CREDENTIALS.token not in rendered


def test_the_public_names_are_listed_alphabetically() -> None:
    from corollary.data.providers import finnhub

    assert list(finnhub.__all__) == sorted(finnhub.__all__)
