"""``AlpacaProvider.news`` -- the untickered Benzinga feed (Phase 3 decision 21).

Replayed from ``tests/fixtures/alpaca/p4_news_untickered_page{1,2}.json``,
recorded by ``tests/fixtures/record_alpaca_news.py`` over a fixed past window
with **no** ``symbols`` parameter. Page 2 of that recording carries a live
``next_page_token`` (the window held more), so a *terminal* page 2 is served
as a literal body derived from it -- labelled where it happens.

Two facts the recorder's ``measure`` mode established on 2026-09-24, and
which these tests pin:

* the ``start``/``end`` window filters on ``updated_at``, and results sort
  ascending by ``updated_at`` -- so the cursor is an ``updated_at``;
* ``created_at`` is the first publication, and an edit moves only
  ``updated_at`` -- so ``published_at`` is the ``created_at``.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from corollary.data.news.article import (
    NewsAccessDenied,
    NewsFeed,
    NewsProviderError,
)
from corollary.data.providers.alpaca import (
    ALPACA_NEWS_MAX_PAGES,
    ALPACA_NEWS_OVERRUN_FACTOR,
    ALPACA_NEWS_PAGE_LIMIT,
    AlpacaNews,
)
from corollary.ratelimit import ALPACA_DATA_HOST
from tests.data.providers.conftest import (
    TEST_CREDENTIALS,
    Served,
    load_fixture,
    sequence,
)

pytestmark = pytest.mark.asyncio

PAGE1 = "p4_news_untickered_page1"
PAGE2 = "p4_news_untickered_page2"
START = datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc)
#: The provider's wall clock for these tests: after every recorded row, so the
#: cursor's upper clamp (the request's ``now`` when no ``end`` is given) binds
#: only where a test sets it up to. The shared conftest default,
#: ``RECORDED_AT``, is 2026-09-10 -- *before* this window -- and would clamp
#: every cursor back to ``start``.
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
LOGGER = "corollary.data.providers.alpaca"


@pytest.fixture
def make_provider(make_provider: Any) -> Any:
    """The shared builder with this module's clock as the default ``now``."""

    def build(*args: Any, now: datetime = NOW, **kwargs: Any) -> Any:
        return make_provider(*args, now=now, **kwargs)

    return build


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def body_of(name: str) -> dict[str, Any]:
    body: dict[str, Any] = load_fixture(name)["body"]
    return body


def literal(body: Any, status: int = 200) -> tuple[int, str]:
    """A synthesised response. Every number in these bodies is an int or a string."""
    return (status, json.dumps(body))


def terminal(name: str) -> tuple[int, str]:
    """DERIVED from a recorded page: the same rows, ``next_page_token`` null."""
    body = body_of(name)
    return literal({**body, "next_page_token": None})


def row(**overrides: Any) -> dict[str, Any]:
    """SYNTHETIC: one well-formed news row in the recorded shape."""
    base: dict[str, Any] = {
        "author": "Someone",
        "content": "",
        "created_at": "2026-09-23T13:40:00Z",
        "headline": "Synthetic headline",
        "id": 1,
        "images": [],
        "source": "benzinga",
        "summary": "",
        "symbols": ["AAPL"],
        "updated_at": "2026-09-23T13:40:00Z",
        "url": "https://www.benzinga.com/x/1",
    }
    base.update(overrides)
    return base


def page(rows: list[Any], token: str | None = None) -> tuple[int, str]:
    return literal({"news": rows, "next_page_token": token})


# ------------------------------------------------------------------ request


async def test_the_request_is_untickered_ascending_and_at_the_page_ceiling(make_provider):
    provider, transport = make_provider(sequence(terminal(PAGE1)))
    await provider.news(start=START)

    (request,) = transport.requests
    assert request.url.host == ALPACA_DATA_HOST
    assert request.url.path == "/v1beta1/news"
    params = dict(request.url.params)
    assert "symbols" not in params, "the discovery tier reads the whole feed"
    assert params["sort"] == "asc"
    assert params["limit"] == str(ALPACA_NEWS_PAGE_LIMIT) == "50"
    assert params["start"] == "2026-09-23T13:30:00Z"
    assert "end" not in params, "no end means the vendor's own default, now"
    assert "page_token" not in params


async def test_start_is_sent_to_the_second_floored_and_end_when_given(make_provider):
    provider, transport = make_provider(sequence(terminal(PAGE1)))
    await provider.news(
        start=datetime(2026, 9, 23, 9, 30, 0, 999_999, tzinfo=timezone(timedelta(hours=-4))),
        end=datetime(2026, 9, 23, 14, 30, tzinfo=timezone.utc),
    )
    params = transport.params_for("/v1beta1/news")
    # Floored, never rounded up: ``start`` is inclusive, so flooring can only
    # re-read, and rounding up could skip the articles in that second.
    assert params["start"] == "2026-09-23T13:30:00Z"
    assert params["end"] == "2026-09-23T14:30:00Z"


async def test_a_naive_bound_is_refused_before_any_request(make_provider):
    provider, transport = make_provider(sequence(terminal(PAGE1)))
    with pytest.raises(ValueError, match="start"):
        await provider.news(start=datetime(2026, 9, 23, 13, 30))
    with pytest.raises(ValueError, match="end"):
        await provider.news(start=START, end=datetime(2026, 9, 23, 14, 30))
    assert transport.requests == []


async def test_max_pages_below_one_is_refused(make_provider):
    provider, _ = make_provider(sequence(terminal(PAGE1)))
    with pytest.raises(ValueError, match="max_pages"):
        await provider.news(start=START, max_pages=0)


# ------------------------------------------------------------------- decode


async def test_recorded_rows_decode_into_vendor_neutral_articles(make_provider):
    provider, _ = make_provider(sequence(terminal(PAGE1)))
    result = await provider.news(start=START)

    assert isinstance(result, AlpacaNews)
    assert result.skipped == 0
    assert len(result.articles) == 10
    raw = {str(r["id"]): r for r in body_of(PAGE1)["news"]}
    for article in result.articles:
        source = raw[article.vendor_id]
        assert article.vendor == "alpaca"
        assert article.feed is NewsFeed.ALPACA_NEWS
        assert article.headline == source["headline"].strip()
        assert article.url == source["url"]
        # ``source`` is the outlet; ``author`` is a person at it.
        assert article.publisher == "benzinga"
        assert article.published_at == datetime.fromisoformat(
            source["created_at"].replace("Z", "+00:00")
        )
        assert article.published_at.tzinfo is timezone.utc
        assert article.tickers == tuple(source["symbols"])


async def test_published_at_is_created_at_even_on_an_edited_article(make_provider):
    # Recorded: id 61945379 was created 13:30:01 and updated 13:30:04.
    provider, _ = make_provider(sequence(terminal(PAGE1)))
    result = await provider.news(start=START)
    (edited,) = [a for a in result.articles if a.vendor_id == "61945379"]
    assert edited.published_at == datetime(2026, 9, 23, 13, 30, 1, tzinfo=timezone.utc)


async def test_crypto_tags_pass_through_and_an_untagged_article_is_market(make_provider):
    provider, _ = make_provider(sequence(terminal(PAGE1)))
    result = await provider.news(start=START)
    by_id = {a.vendor_id: a for a in result.articles}
    # Recorded: dropping non-equity tags is the ingest's job, not the provider's.
    assert by_id["61945438"].tickers == ("BTCUSD", "SOLUSD")
    assert by_id["61945779"].tickers == ()
    assert by_id["61945779"].is_market


async def test_articles_are_newest_first_and_ties_break_by_id(make_provider):
    rows = [
        row(id=3, created_at="2026-09-23T13:40:00Z"),
        row(id=2, created_at="2026-09-23T13:41:00Z", updated_at="2026-09-23T13:41:00Z"),
        row(id=1, created_at="2026-09-23T13:40:00Z"),
    ]
    provider, _ = make_provider(sequence(page(rows)))
    result = await provider.news(start=START)
    assert [a.vendor_id for a in result.articles] == ["2", "1", "3"]


async def test_an_edited_duplicate_keeps_the_newer_copy(make_provider):
    # An article edited mid-read reappears later in an updated_at-sorted feed;
    # the later read is the edit, and its headline and tags are the current ones.
    first = row(id=7, headline="first read", symbols=["AAPL"])
    again = row(
        id=7, headline="edited", symbols=["AAPL", "MSFT"], updated_at="2026-09-23T13:45:00Z"
    )
    provider, _ = make_provider(sequence(page([first], "t1"), page([again])))
    result = await provider.news(start=START)
    (article,) = result.articles
    assert article.vendor_id == "7"
    assert article.headline == "edited"
    assert article.tickers == ("AAPL", "MSFT")


async def test_a_stale_duplicate_read_second_does_not_replace_the_newer(make_provider):
    # SYNTHETIC: order-independence. Whichever page it arrives on, the copy
    # with the older updated_at loses.
    newer = row(id=7, headline="edited", symbols=["MSFT"], updated_at="2026-09-23T13:45:00Z")
    stale = row(id=7, headline="first read", symbols=["AAPL"], updated_at="2026-09-23T13:40:00Z")
    provider, _ = make_provider(sequence(page([newer], "t1"), page([stale])))
    result = await provider.news(start=START)
    (article,) = result.articles
    assert article.headline == "edited"
    assert article.tickers == ("MSFT",)


# --------------------------------------------------------------- pagination


async def test_pagination_follows_the_token_with_the_same_query(make_provider):
    provider, transport = make_provider(sequence(PAGE1, terminal(PAGE2)))
    result = await provider.news(start=START)

    assert result.complete is True
    assert result.pages == 2
    assert len(result.articles) == 20
    first, second = (dict(r.url.params) for r in transport.requests)
    assert second["page_token"] == body_of(PAGE1)["next_page_token"]
    assert {k: v for k, v in second.items() if k != "page_token"} == first
    assert "symbols" not in second


async def test_a_complete_call_cursors_at_the_newest_updated_at(make_provider):
    provider, _ = make_provider(sequence(PAGE1, terminal(PAGE2)))
    result = await provider.news(start=START)
    newest = max(
        r["updated_at"] for r in body_of(PAGE1)["news"] + body_of(PAGE2)["news"]
    )
    assert newest == "2026-09-23T13:46:46Z"
    assert result.cursor == datetime(2026, 9, 23, 13, 46, 46, tzinfo=timezone.utc)


async def test_the_cursor_is_updated_at_not_created_at(make_provider):
    # SYNTHETIC: an old story edited late. The feed filters and sorts on
    # updated_at, so the cursor must too -- a created_at cursor would sit
    # behind the read position and re-read, or ahead of it and skip.
    rows = [row(id=1, created_at="2026-09-22T09:00:00Z", updated_at="2026-09-23T14:00:00Z")]
    provider, _ = make_provider(sequence(page(rows)))
    result = await provider.news(start=START)
    assert result.cursor == datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)
    assert result.articles[0].published_at == datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)


async def test_a_capped_call_is_incomplete_and_cursors_at_the_newest_second(make_provider):
    provider, transport = make_provider(sequence(PAGE1, terminal(PAGE2)))
    result = await provider.news(start=START, max_pages=1)

    assert len(transport.requests) == 1
    assert result.complete is False
    assert result.pages == 1
    # Page 1's newest updated_at is 13:38:37. No back-off: ``start`` is
    # inclusive, so the next call re-reads that whole second, including any
    # tie still unread behind the token.
    assert result.cursor == datetime(2026, 9, 23, 13, 38, 37, tzinfo=timezone.utc)


async def test_a_one_second_advance_is_progress(make_provider):
    # The old one-second back-off turned this into a cursor at ``start`` --
    # no progress -- although the rows had moved on a full second.
    tied = [row(id=i, updated_at="2026-09-23T13:30:00Z") for i in range(1, 4)]
    later = [row(id=9, updated_at="2026-09-23T13:30:01Z")]
    provider, transport = make_provider(sequence(page(tied + later, "more")))
    result = await provider.news(start=START, max_pages=1)
    assert len(transport.requests) == 1
    assert result.complete is False
    assert result.cursor == START + timedelta(seconds=1)


async def test_a_capped_call_that_cannot_advance_pages_past_the_cap_until_it_can(
    make_provider, caplog
):
    # SYNTHETIC: a full page tied at ``start``. Stopping at the cap would
    # return ``start`` itself, and the next call would read the same page
    # forever. The keyset token orders ties, so following it is safe.
    tied = [row(id=i, updated_at="2026-09-23T13:30:00Z") for i in range(1, 51)]
    more_tied = [row(id=i, updated_at="2026-09-23T13:30:00Z") for i in range(51, 61)]
    moved = [row(id=70, updated_at="2026-09-23T13:31:00Z")]
    provider, transport = make_provider(
        sequence(page(tied, "t1"), page(more_tied, "t2"), page(moved, "t3"))
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await provider.news(start=START, max_pages=1)

    assert [dict(r.url.params).get("page_token") for r in transport.requests] == [
        None,
        "t1",
        "t2",
    ]
    assert result.pages == 3
    assert result.complete is False, "a token was still pending"
    assert result.cursor == datetime(2026, 9, 23, 13, 31, tzinfo=timezone.utc)
    assert len(result.articles) == 61
    (overrun,) = events(caplog, "alpaca_news_page_cap_overrun")
    assert overrun.levelno == logging.WARNING
    assert events(caplog, "alpaca_news_cursor_stalled") == []


@pytest.mark.parametrize(
    "rows",
    [
        [row(id=1, updated_at="2026-09-23T13:30:00Z")],
        [{"id": True}],
    ],
    ids=["tied at start forever", "every row malformed"],
)
async def test_a_cursor_that_never_advances_stops_at_the_hard_ceiling_loudly(
    make_provider, caplog, rows
):
    provider, transport = make_provider(sequence(page(rows, "forever")))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await provider.news(start=START, max_pages=2)

    ceiling = 2 * ALPACA_NEWS_OVERRUN_FACTOR
    assert len(transport.requests) == ceiling, "bounded, never an unbounded loop"
    assert result.pages == ceiling
    assert result.complete is False
    assert result.cursor == START
    (stalled,) = events(caplog, "alpaca_news_cursor_stalled")
    assert stalled.levelno == logging.ERROR
    assert stalled.pages == ceiling
    assert stalled.cursor == START.isoformat()


async def test_a_ceiling_at_start_stops_at_the_cap_because_no_page_can_help(
    make_provider, caplog
):
    # ``end == start``: the cursor is clamped to ``end``, so no amount of
    # paging can move it. Reading ten times the cap would only spend budget.
    rows = [row(id=1, updated_at="2026-09-23T13:30:00Z")]
    provider, transport = make_provider(sequence(page(rows, "forever")))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await provider.news(start=START, end=START, max_pages=1)
    assert len(transport.requests) == 1
    assert result.complete is False
    assert result.cursor == START
    (stalled,) = events(caplog, "alpaca_news_cursor_stalled")
    assert stalled.levelno == logging.ERROR


async def test_a_far_future_row_cannot_push_the_cursor_past_now(make_provider, caplog):
    rows = [row(id=1), row(id=2, updated_at="2099-01-01T00:00:00Z")]
    provider, _ = make_provider(sequence(page(rows)), now=NOW + timedelta(microseconds=5))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await provider.news(start=START)
    assert result.cursor == NOW, "clamped to the request's clock, floored to the second"
    assert {a.vendor_id for a in result.articles} == {"1", "2"}, "the row itself is kept"
    (clamped,) = events(caplog, "alpaca_news_cursor_clamped")
    assert clamped.ceiling == NOW.isoformat()


async def test_a_far_future_row_cannot_push_the_cursor_past_end(make_provider, caplog):
    end = datetime(2026, 9, 23, 14, 30, tzinfo=timezone.utc)
    rows = [row(id=2, updated_at="2099-01-01T00:00:00Z")]
    provider, _ = make_provider(sequence(page(rows)))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await provider.news(start=START, end=end)
    assert result.cursor == end
    assert len(events(caplog, "alpaca_news_cursor_clamped")) == 1


async def test_an_ordinary_cursor_is_not_clamped_or_logged(make_provider, caplog):
    provider, _ = make_provider(sequence(PAGE1, terminal(PAGE2)))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await provider.news(start=START)
    assert events(caplog, "alpaca_news_cursor_clamped") == []


async def test_the_default_page_cap_is_the_named_constant(make_provider):
    provider, transport = make_provider(sequence(page([row()], "forever")))
    result = await provider.news(start=START)
    assert len(transport.requests) == ALPACA_NEWS_MAX_PAGES
    assert result.complete is False


async def test_an_empty_window_is_complete_and_leaves_the_cursor_at_start(make_provider):
    provider, _ = make_provider(sequence(page([])))
    result = await provider.news(start=START)
    assert result == AlpacaNews(articles=(), cursor=START, complete=True, pages=1, skipped=0)


# ------------------------------------------------------------ malformed rows


MALFORMED: list[tuple[str, Any]] = [
    ("not an object", "a string row"),
    ("bool id", row(id=True)),
    ("missing id", {k: v for k, v in row().items() if k != "id"}),
    ("missing headline", row(headline=None)),
    ("blank headline", row(headline="   ")),
    ("null url", row(url=None)),
    ("symbols not a list", row(symbols="AAPL")),
    ("symbols not strings", row(symbols=["AAPL", 7])),
    ("garbage created_at", row(created_at="yesterday")),
    ("overflowing created_at", row(created_at="0001-01-01T00:30:00+01:00")),
    ("summary not a string", row(summary=5)),
    ("source not a string", row(source=["benzinga"])),
]


@pytest.mark.parametrize("label,bad", MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_a_malformed_row_is_skipped_counted_and_logged(make_provider, caplog, label, bad):
    good = row(id=99)
    provider, _ = make_provider(sequence(page([bad, good])))
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.alpaca"):
        result = await provider.news(start=START)
    assert [a.vendor_id for a in result.articles] == ["99"]
    assert result.skipped == 1
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "alpaca_news_row_skipped"]
    assert record.feed == NewsFeed.ALPACA_NEWS.value


async def test_a_missing_updated_at_falls_back_to_created_at_for_the_cursor(make_provider):
    rows = [{k: v for k, v in row(created_at="2026-09-23T13:50:00Z").items() if k != "updated_at"}]
    provider, _ = make_provider(sequence(page(rows)))
    result = await provider.news(start=START)
    assert result.skipped == 0
    assert result.cursor == datetime(2026, 9, 23, 13, 50, tzinfo=timezone.utc)


async def test_the_skip_log_is_scrubbed_and_bounded(make_provider, caplog):
    secret = TEST_CREDENTIALS.secret_key
    bad = row(id=f"x{secret}", created_at=f"{secret}{'z' * 5000}")
    provider, _ = make_provider(sequence(page([bad])))
    with caplog.at_level(logging.WARNING, logger="corollary.data.providers.alpaca"):
        await provider.news(start=START)
    text = "\n".join(
        f"{r.getMessage()} {getattr(r, 'cause', '')} {getattr(r, 'vendor_id', '')}"
        for r in caplog.records
    )
    assert secret not in text
    assert TEST_CREDENTIALS.key_id not in text
    assert len(text) < 2000


# ------------------------------------------------------------------- errors


async def test_a_403_is_access_denied_and_quotes_no_credential(make_provider):
    body = f"forbidden for {TEST_CREDENTIALS.secret_key}"
    provider, _ = make_provider(sequence((403, body)))
    with pytest.raises(NewsAccessDenied) as caught:
        await provider.news(start=START)
    assert TEST_CREDENTIALS.secret_key not in str(caught.value)


async def test_a_server_error_is_a_news_provider_error(make_provider):
    provider, _ = make_provider(sequence((500, "boom")))
    with pytest.raises(NewsProviderError):
        await provider.news(start=START)


@pytest.mark.parametrize(
    "body",
    [[], {"news": "nope"}, {"next_page_token": None}, "just text"],
    ids=["bare list", "news not a list", "no news key", "not json object"],
)
async def test_a_body_without_a_news_list_fails_the_call(make_provider, body):
    served: Served = (200, json.dumps(body)) if body != "just text" else (200, "just text")
    provider, _ = make_provider(sequence(served))
    with pytest.raises(NewsProviderError):
        await provider.news(start=START)


async def test_a_failure_on_a_later_page_fails_the_whole_call(make_provider):
    provider, _ = make_provider(sequence(PAGE1, (500, "boom")))
    with pytest.raises(NewsProviderError):
        await provider.news(start=START)
