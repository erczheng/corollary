"""The news poll drivers (Phase 3 decision 21): what the scheduler's news jobs call.

Every provider here is a fake -- no request leaves the process. The database
is file-backed SQLite under ``tmp_path``, per ``tests/db/conftest.py``.
"""

import asyncio
import json
import logging
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ
from corollary.data.news import pollers
from corollary.data.news.article import NewsArticle, NewsFeed, NewsProviderError
from corollary.data.news.assets import AssetDirectoryHolder
from corollary.data.news.ingest import IngestResult, store_articles
from corollary.data.news.pollers import (
    ALPACA_BACKFILL_CAP,
    FIRST_RUN_LOOKBACK,
    MASSIVE_BACKFILL_CAP,
    MASSIVE_OVERLAP,
    AlpacaNewsPoller,
    AssetsRefreshed,
    BuiltUniverse,
    DiscoveryPollResult,
    FinnhubMarketNewsPoller,
    MassiveNewsPoller,
    NewsStore,
    PollSkipped,
    WatchPollResult,
    WatchTierPoller,
    build_watch_universe,
    in_watch_window,
    prune_news,
    refresh_assets,
    refresh_tradeability_cache,
    watch_interval,
)
from corollary.data.news.labelling import labelled_article_ids
from corollary.data.news.tradeability import MAX_TICKERS_PER_RUN, RefreshResult
from corollary.data.news.watchlist import Membership, watch_universe
from corollary.data.providers.finnhub import CompanyNews, MarketNews
from corollary.data.providers.interface import AssetDirectory, EquityAsset
from corollary.data.providers.massive import MassiveNews
from corollary.data.seeds import SeedError, SpdrSeed
from corollary.db.models import Base
from corollary.db.models import NewsArticle as ArticleRow
from corollary.db.models import SentimentLabelRow, WatchSymbol
from corollary.db.session import create_db_engine, sqlite_url

#: Thursday 2026-09-24, 10:00 ET -- a full session, inside the watch window.
NOW = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)


def et(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=NYSE_TZ)


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "pollers.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(db_engine: Engine) -> Callable[[], Session]:
    return lambda: Session(db_engine)


def equity(symbol: str) -> EquityAsset:
    return EquityAsset(
        symbol=symbol, name=f"{symbol} Inc", tradable=True, has_options=True, exchange="NASDAQ"
    )


DIRECTORY = AssetDirectory(assets=tuple(equity(s) for s in ("AAPL", "MSFT", "NVDA", "ORCL")))


class DirectorySource:
    def __init__(self, directory: AssetDirectory = DIRECTORY, fail: bool = False) -> None:
        self.directory = directory
        self.fail = fail

    async def active_equities(self) -> AssetDirectory:
        if self.fail:
            raise RuntimeError("asset list unavailable")
        return self.directory


@pytest.fixture
def holder() -> AssetDirectoryHolder:
    return AssetDirectoryHolder(now=lambda: NOW)


@pytest.fixture
def store(sessions: Callable[[], Session], holder: AssetDirectoryHolder) -> NewsStore:
    return NewsStore(session_factory=sessions, assets=holder, books=None)


_FEED_VENDOR = {
    NewsFeed.ALPACA_NEWS: "alpaca",
    NewsFeed.FINNHUB_COMPANY: "finnhub",
    NewsFeed.FINNHUB_MARKET: "finnhub",
    NewsFeed.MASSIVE_NEWS: "massive",
}


def article(
    feed: NewsFeed,
    vendor_id: str,
    *,
    published_at: datetime = NOW - timedelta(minutes=5),
    tickers: tuple[str, ...] = ("NVDA",),
) -> NewsArticle:
    """A SYNTHETIC article."""
    return NewsArticle(
        vendor=_FEED_VENDOR[feed],
        vendor_id=vendor_id,
        feed=feed,
        url=f"https://example.com/{feed.value}/{vendor_id}",
        headline=f"Headline {feed.value} {vendor_id}",
        summary="A summary",
        publisher="Example",
        published_at=published_at,
        tickers=tickers,
    )


def seed_rows(sessions: Callable[[], Session], *articles: NewsArticle) -> None:
    with sessions() as session:
        store_articles(session, articles, assets=None, now=NOW)
        session.commit()


def row_count(sessions: Callable[[], Session]) -> int:
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(ArticleRow)) or 0


def universe_of(*symbols: str, seed_missing: bool = False) -> BuiltUniverse:
    return BuiltUniverse(
        universe=watch_universe(symbols, (), (), ()),
        seed_missing=seed_missing,
        seed_error=None,
        positions_error=None,
    )


class UniverseBox:
    """A universe the test can change between polls."""

    def __init__(self, *symbols: str, seed_missing: bool = False) -> None:
        self.built = universe_of(*symbols, seed_missing=seed_missing)
        self.calls = 0

    def set(self, *symbols: str) -> None:
        self.built = universe_of(*symbols)

    async def __call__(self) -> BuiltUniverse:
        self.calls += 1
        return self.built


# ------------------------------------------------------------------ fakes


@dataclass
class FakeCompanyNews:
    fail_on: frozenset[str] = frozenset()
    calls: list[tuple[str, date, date]] = field(default_factory=list)

    async def company_news(self, symbol: str, from_date: date, to_date: date) -> CompanyNews:
        self.calls.append((symbol, from_date, to_date))
        if symbol in self.fail_on:
            raise NewsProviderError(f"finnhub 502 for {symbol}")
        return CompanyNews(
            symbol=symbol,
            from_date=from_date,
            to_date=to_date,
            articles=(
                article(NewsFeed.FINNHUB_COMPANY, f"{symbol}-{len(self.calls)}", tickers=(symbol,)),
            ),
            retried_today=False,
            overflow_date=None,
            skipped=0,
        )


@dataclass
class FakeMarketNews:
    answers: list[MarketNews | Exception] = field(default_factory=list)
    calls: list[int | None] = field(default_factory=list)

    async def market_news(self, min_id: int | None) -> MarketNews:
        self.calls.append(min_id)
        answer = self.answers.pop(0) if self.answers else MarketNews(
            articles=(), min_id=min_id, skipped=0
        )
        if isinstance(answer, Exception):
            raise answer
        return answer


@dataclass
class FakeMassive:
    answers: list[MassiveNews | Exception | None] = field(default_factory=list)
    calls: list[datetime] = field(default_factory=list)

    async def news_since(self, published_after: datetime) -> MassiveNews:
        self.calls.append(published_after)
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            # Nothing new: the provider hands back the value passed in.
            return MassiveNews(
                articles=(), cursor=published_after, complete=True, pages=1, skipped=0
            )
        return answer


@dataclass
class FakeAlpacaNewsAnswer:
    articles: tuple[NewsArticle, ...]
    cursor: datetime
    complete: bool = True
    pages: int = 1
    skipped: int = 0


@dataclass
class FakeAlpaca:
    answers: list[FakeAlpacaNewsAnswer | Exception | None] = field(default_factory=list)
    calls: list[datetime] = field(default_factory=list)

    async def news(
        self, *, start: datetime, end: datetime | None = None, max_pages: int = 5
    ) -> FakeAlpacaNewsAnswer:
        self.calls.append(start)
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return FakeAlpacaNewsAnswer(articles=(), cursor=start.replace(microsecond=0))
        return answer


async def wait_for(event: threading.Event, timeout: float = 5.0) -> None:
    """Yield to the loop until ``event`` is set by a worker thread."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for the worker thread")
        await asyncio.sleep(0.005)


# ------------------------------------------------------------------ watch_interval


def test_watch_interval_at_66_symbols_in_window_is_13_6_seconds() -> None:
    interval = watch_interval(66, et(2026, 9, 24, 10))
    assert interval == timedelta(seconds=900) / 66
    assert round(interval.total_seconds(), 1) == 13.6


def test_watch_interval_at_the_108_ceiling_is_8_33_seconds() -> None:
    interval = watch_interval(108, et(2026, 9, 24, 10))
    assert round(interval.total_seconds(), 2) == 8.33


def test_watch_interval_outside_the_window_is_hourly_per_cycle() -> None:
    assert watch_interval(66, et(2026, 9, 24, 3)) == timedelta(seconds=3600) / 66


def test_the_window_opens_at_0600_et_inclusive() -> None:
    assert not in_watch_window(et(2026, 9, 24, 5, 59, 59))
    assert in_watch_window(et(2026, 9, 24, 6))
    assert watch_interval(10, et(2026, 9, 24, 5, 59, 59)) == timedelta(seconds=360)
    assert watch_interval(10, et(2026, 9, 24, 6)) == timedelta(seconds=90)


def test_the_window_closes_an_hour_after_a_full_day_close() -> None:
    assert in_watch_window(et(2026, 9, 24, 16, 59, 59))
    assert not in_watch_window(et(2026, 9, 24, 17))


def test_a_half_day_window_closes_an_hour_after_the_calendar_close() -> None:
    # 2026-11-27, the day after Thanksgiving: the calendar closes at 13:00 ET.
    assert in_watch_window(et(2026, 11, 27, 13, 59, 59))
    assert not in_watch_window(et(2026, 11, 27, 14))
    assert watch_interval(10, et(2026, 11, 27, 15)) == timedelta(seconds=360)


def test_a_holiday_and_a_weekend_are_otherwise() -> None:
    assert not in_watch_window(et(2026, 11, 26, 10))  # Thanksgiving
    assert not in_watch_window(et(2026, 9, 26, 10))  # Saturday
    assert watch_interval(10, et(2026, 11, 26, 10)) == timedelta(seconds=360)


def test_the_window_is_judged_in_et_whatever_zone_now_carries() -> None:
    # 21:30 UTC is 17:30 ET in September: past close + 1h.
    assert not in_watch_window(datetime(2026, 9, 24, 21, 30, tzinfo=timezone.utc))
    assert in_watch_window(datetime(2026, 9, 24, 20, 30, tzinfo=timezone.utc))


def test_watch_interval_refuses_an_empty_universe_and_a_naive_clock() -> None:
    with pytest.raises(ValueError):
        watch_interval(0, NOW)
    with pytest.raises(ValueError):
        watch_interval(10, datetime(2026, 9, 24, 10))


# ------------------------------------------------------------------ watch tier


@pytest.mark.asyncio
async def test_round_robin_polls_one_symbol_per_call_in_sorted_order_and_wraps(
    store: NewsStore,
) -> None:
    provider = FakeCompanyNews()
    poller = WatchTierPoller(provider=provider, universe=UniverseBox("NVDA", "AAPL", "MSFT"), store=store)
    polled = []
    for _ in range(4):
        result = await poller.poll_next(NOW)
        assert isinstance(result, WatchPollResult)
        polled.append(result.symbol)
    assert polled == ["AAPL", "MSFT", "NVDA", "AAPL"]
    assert [call[0] for call in provider.calls] == polled  # one request per poll_next
    assert poller.last_universe_size == 3


@pytest.mark.asyncio
async def test_a_universe_change_continues_after_the_last_polled_symbol(store: NewsStore) -> None:
    box = UniverseBox("AAPL", "MSFT", "NVDA")
    poller = WatchTierPoller(provider=FakeCompanyNews(), universe=box, store=store)
    assert [await _symbol(poller) for _ in range(2)] == ["AAPL", "MSFT"]
    # NVDA removed; AMZN (before the cursor) and ORCL (after it) added.
    box.set("AAPL", "AMZN", "MSFT", "ORCL")
    assert [await _symbol(poller) for _ in range(5)] == ["ORCL", "AAPL", "AMZN", "MSFT", "ORCL"]


@pytest.mark.asyncio
async def test_removing_the_last_polled_symbol_does_not_skip_its_successor(store: NewsStore) -> None:
    box = UniverseBox("AAPL", "MSFT", "NVDA")
    poller = WatchTierPoller(provider=FakeCompanyNews(), universe=box, store=store)
    assert [await _symbol(poller) for _ in range(2)] == ["AAPL", "MSFT"]
    box.set("AAPL", "NVDA")
    assert await _symbol(poller) == "NVDA"


@pytest.mark.asyncio
async def test_no_symbol_is_starved_across_repeated_universe_changes(store: NewsStore) -> None:
    """Every symbol present for a whole cycle is polled within that cycle."""
    box = UniverseBox("AAPL", "MSFT", "NVDA")
    poller = WatchTierPoller(provider=FakeCompanyNews(), universe=box, store=store)
    stable = ("AAPL", "MSFT", "NVDA")
    churn = ["ZS", "AMD", "KO", "A", "ZZZ", "B"]
    polled: list[str] = []
    for extra in churn:
        box.set(*stable, extra)
        polled.append(await _symbol(poller))
    # Six polls over universes of four: every stable symbol came round.
    for symbol in stable:
        assert symbol in polled


@pytest.mark.asyncio
async def test_a_failed_request_still_advances_the_rotation(store: NewsStore) -> None:
    provider = FakeCompanyNews(fail_on=frozenset({"MSFT"}))
    poller = WatchTierPoller(provider=provider, universe=UniverseBox("AAPL", "MSFT", "NVDA"), store=store)
    assert await _symbol(poller) == "AAPL"
    with pytest.raises(NewsProviderError):
        await poller.poll_next(NOW)
    # One bad symbol must not pin the rotation on itself.
    assert await _symbol(poller) == "NVDA"


async def _symbol(poller: WatchTierPoller) -> str:
    result = await poller.poll_next(NOW)
    assert isinstance(result, WatchPollResult)
    return result.symbol


@pytest.mark.parametrize(
    ("moment", "window"),
    [
        (et(2026, 9, 24, 19, 59, 59), (date(2026, 9, 23), date(2026, 9, 24))),
        # 20:00 EDT is 00:00 UTC: the UTC day has turned, the ET day has not.
        (et(2026, 9, 24, 20, 0), (date(2026, 9, 24), date(2026, 9, 25))),
        (et(2026, 9, 24, 23, 59, 59), (date(2026, 9, 24), date(2026, 9, 25))),
        (et(2026, 9, 25, 0, 0), (date(2026, 9, 24), date(2026, 9, 25))),
    ],
)
@pytest.mark.asyncio
async def test_the_date_window_is_in_utc_days_not_et(
    store: NewsStore, moment: datetime, window: tuple[date, date]
) -> None:
    provider = FakeCompanyNews()
    poller = WatchTierPoller(provider=provider, universe=UniverseBox("AAPL"), store=store)
    result = await poller.poll_next(moment)
    assert isinstance(result, WatchPollResult)
    assert provider.calls == [("AAPL", *window)]
    assert (result.from_date, result.to_date) == window


@pytest.mark.asyncio
async def test_a_watch_poll_stores_what_it_fetched(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    poller = WatchTierPoller(provider=FakeCompanyNews(), universe=UniverseBox("AAPL"), store=store)
    result = await poller.poll_next(NOW)
    assert isinstance(result, WatchPollResult)
    assert result.fetched == 1
    assert result.ingest.inserted == 1
    assert row_count(sessions) == 1


@pytest.mark.asyncio
async def test_an_unavailable_finnhub_skips_the_watch_tier(store: NewsStore) -> None:
    box = UniverseBox("AAPL")
    poller = WatchTierPoller(provider=None, universe=box, store=store)
    result = await poller.poll_next(NOW)
    assert isinstance(result, PollSkipped)
    assert "FINNHUB_API_KEY" in result.reason
    assert box.calls == 0


@pytest.mark.asyncio
async def test_an_empty_universe_skips(store: NewsStore) -> None:
    provider = FakeCompanyNews()
    poller = WatchTierPoller(provider=provider, universe=UniverseBox(), store=store)
    result = await poller.poll_next(NOW)
    assert isinstance(result, PollSkipped)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_provider_error_raises_and_stores_nothing(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    provider = FakeCompanyNews(fail_on=frozenset({"AAPL"}))
    poller = WatchTierPoller(provider=provider, universe=UniverseBox("AAPL"), store=store)
    with pytest.raises(NewsProviderError):
        await poller.poll_next(NOW)
    assert row_count(sessions) == 0


@pytest.mark.asyncio
async def test_seed_missing_is_logged_once_per_rotation(
    store: NewsStore, caplog: pytest.LogCaptureFixture
) -> None:
    poller = WatchTierPoller(
        provider=FakeCompanyNews(),
        universe=UniverseBox("AAPL", "MSFT", "NVDA", seed_missing=True),
        store=store,
    )
    with caplog.at_level(logging.WARNING, logger=pollers.__name__):
        results = [await poller.poll_next(NOW) for _ in range(6)]
    assert all(isinstance(r, WatchPollResult) and r.seed_missing for r in results)
    flagged = [r for r in caplog.records if getattr(r, "event", None) == "watch_universe_seed_missing"]
    assert len(flagged) == 2


# ------------------------------------------------------------------ the store lock


@pytest.mark.asyncio
async def test_the_store_lock_serialises_two_concurrent_polls(
    store: NewsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second poll fetches, then waits on the lock until the first has committed."""
    order: list[str] = []
    first_inside = threading.Event()
    release_first = threading.Event()
    real = pollers.store_articles

    def slow_store(session: Session, articles: Iterable[NewsArticle], **kwargs: Any) -> IngestResult:
        batch = list(articles)
        tag = batch[0].feed.value
        order.append(f"enter:{tag}")
        if tag == NewsFeed.FINNHUB_COMPANY.value:
            first_inside.set()
            assert release_first.wait(5)
        result = real(session, batch, **kwargs)
        order.append(f"exit:{tag}")
        return result

    monkeypatch.setattr(pollers, "store_articles", slow_store)
    watch = WatchTierPoller(provider=FakeCompanyNews(), universe=UniverseBox("AAPL"), store=store)
    massive = FakeMassive(
        answers=[
            MassiveNews(
                articles=(article(NewsFeed.MASSIVE_NEWS, "m1"),),
                cursor=NOW,
                complete=True,
                pages=1,
                skipped=0,
            )
        ]
    )
    discovery = MassiveNewsPoller(provider=massive, store=store)

    first = asyncio.create_task(watch.poll_next(NOW))
    await wait_for(first_inside)
    second = asyncio.create_task(discovery.poll(NOW))
    for _ in range(20):
        await asyncio.sleep(0.005)
    assert massive.calls, "the second poll fetched while the first held the lock"
    assert store.lock.locked()
    assert order == ["enter:finnhub_company"], "the second store must wait"
    release_first.set()
    await asyncio.gather(first, second)
    assert order == [
        "enter:finnhub_company",
        "exit:finnhub_company",
        "enter:massive_news",
        "exit:massive_news",
    ]


@pytest.mark.asyncio
async def test_a_cancelled_store_still_holds_off_the_next_one(
    store: NewsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the await cannot stop its thread; the next store must wait for that thread."""
    order: list[str] = []
    first_inside = threading.Event()
    release_first = threading.Event()

    def slow_store(session: Session, articles: Iterable[NewsArticle], **kwargs: Any) -> IngestResult:
        batch = list(articles)
        order.append(f"enter:{batch[0].vendor_id}")
        if batch[0].vendor_id == "one":
            first_inside.set()
            assert release_first.wait(5)
        order.append(f"exit:{batch[0].vendor_id}")
        return IngestResult(0, 0, len(batch), 0, 0, 0, False)

    monkeypatch.setattr(pollers, "store_articles", slow_store)
    first = asyncio.create_task(store.store([article(NewsFeed.ALPACA_NEWS, "one")], now=NOW))
    await wait_for(first_inside)
    first.cancel()
    second = asyncio.create_task(store.store([article(NewsFeed.ALPACA_NEWS, "two")], now=NOW))
    for _ in range(20):
        await asyncio.sleep(0.005)
    assert order == ["enter:one"]
    release_first.set()
    await second
    with pytest.raises(asyncio.CancelledError):
        await first
    assert order == ["enter:one", "exit:one", "enter:two", "exit:two"]


@pytest.mark.asyncio
async def test_a_failed_store_rolls_back_the_whole_batch(
    store: NewsStore, sessions: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = pollers.store_articles

    def half_then_fail(session: Session, articles: Iterable[NewsArticle], **kwargs: Any) -> IngestResult:
        batch = list(articles)
        real(session, batch[:1], **kwargs)
        session.flush()
        raise RuntimeError("disk full")

    monkeypatch.setattr(pollers, "store_articles", half_then_fail)
    with pytest.raises(RuntimeError):
        await store.store(
            [article(NewsFeed.ALPACA_NEWS, "a"), article(NewsFeed.ALPACA_NEWS, "b")], now=NOW
        )
    assert row_count(sessions) == 0


# ------------------------------------------------------------------ discovery: Alpaca


@pytest.mark.asyncio
async def test_alpaca_first_start_is_two_hours_back_then_the_returned_cursor(store: NewsStore) -> None:
    cursor = NOW - timedelta(minutes=3)
    provider = FakeAlpaca(
        answers=[FakeAlpacaNewsAnswer(articles=(article(NewsFeed.ALPACA_NEWS, "1"),), cursor=cursor)]
    )
    poller = AlpacaNewsPoller(provider=provider, store=store)
    first = await poller.poll(NOW)
    assert isinstance(first, DiscoveryPollResult)
    assert first.ingest.inserted == 1
    await poller.poll(NOW + timedelta(minutes=1))
    assert provider.calls == [NOW - FIRST_RUN_LOOKBACK, cursor]
    assert FIRST_RUN_LOOKBACK == timedelta(hours=2)


@pytest.mark.asyncio
async def test_alpaca_error_raises_and_leaves_the_cursor(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    provider = FakeAlpaca(answers=[NewsProviderError("alpaca 500")])
    poller = AlpacaNewsPoller(provider=provider, store=store)
    with pytest.raises(NewsProviderError):
        await poller.poll(NOW)
    await poller.poll(NOW + timedelta(minutes=1))
    assert provider.calls == [NOW - FIRST_RUN_LOOKBACK] * 2
    assert row_count(sessions) == 0


@pytest.mark.asyncio
async def test_a_failed_store_does_not_advance_the_cursor(
    store: NewsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = NOW - timedelta(minutes=3)
    provider = FakeAlpaca(
        answers=[
            FakeAlpacaNewsAnswer(articles=(article(NewsFeed.ALPACA_NEWS, "1"),), cursor=cursor),
        ]
    )
    poller = AlpacaNewsPoller(provider=provider, store=store)

    def boom(*args: Any, **kwargs: Any) -> IngestResult:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(pollers, "store_articles", boom)
    with pytest.raises(RuntimeError):
        await poller.poll(NOW)
    monkeypatch.undo()
    await poller.poll(NOW)
    assert provider.calls == [NOW - FIRST_RUN_LOOKBACK] * 2


@pytest.mark.asyncio
async def test_a_labeller_failure_rolls_the_store_back_and_leaves_the_cursor(
    store: NewsStore, sessions: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit 5.2: articles and their labels commit together or not at all. A
    labeller bug is a failed job, never news silently stored without labels."""
    cursor = NOW - timedelta(minutes=3)
    provider = FakeAlpaca(
        answers=[
            FakeAlpacaNewsAnswer(articles=(article(NewsFeed.ALPACA_NEWS, "1"),), cursor=cursor),
            FakeAlpacaNewsAnswer(articles=(article(NewsFeed.ALPACA_NEWS, "1"),), cursor=cursor),
        ]
    )
    poller = AlpacaNewsPoller(provider=provider, store=store)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("labeller bug")

    monkeypatch.setattr(pollers, "label_stored", boom)
    with pytest.raises(RuntimeError, match="labeller bug"):
        await poller.poll(NOW)
    assert row_count(sessions) == 0  # the articles written before the labeller ran are rolled back
    monkeypatch.undo()
    result = await poller.poll(NOW)
    assert isinstance(result, DiscoveryPollResult) and result.ingest.inserted == 1
    assert provider.calls == [NOW - FIRST_RUN_LOOKBACK] * 2  # the failed poll did not advance it


@pytest.mark.asyncio
async def test_an_unavailable_alpaca_skips(store: NewsStore) -> None:
    result = await AlpacaNewsPoller(provider=None, store=store).poll(NOW)
    assert isinstance(result, PollSkipped)


# ------------------------------------------------------------------ discovery: Finnhub market


@pytest.mark.asyncio
async def test_finnhub_min_id_is_recovered_from_the_largest_stored_general_id(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    # "general:99" sorts after "general:1000" as text; the max must be numeric.
    seed_rows(
        sessions,
        article(NewsFeed.FINNHUB_MARKET, "general:99", tickers=()),
        article(NewsFeed.FINNHUB_MARKET, "general:1000", tickers=()),
        article(NewsFeed.FINNHUB_MARKET, "general:100", tickers=()),
        # A company-news id is not a market-news cursor, however large.
        article(NewsFeed.FINNHUB_COMPANY, "999999", tickers=("NVDA",)),
    )
    provider = FakeMarketNews(
        answers=[
            MarketNews(
                articles=(article(NewsFeed.FINNHUB_MARKET, "general:1005", tickers=()),),
                min_id=1005,
                skipped=0,
            )
        ]
    )
    poller = FinnhubMarketNewsPoller(provider=provider, store=store)
    await poller.poll(NOW)
    await poller.poll(NOW)
    assert provider.calls == [1000, 1005]


@pytest.mark.asyncio
async def test_finnhub_cold_start_has_no_min_id_and_an_empty_page_keeps_the_cursor(
    store: NewsStore,
) -> None:
    provider = FakeMarketNews(
        answers=[
            MarketNews(articles=(), min_id=None, skipped=0),
            MarketNews(articles=(article(NewsFeed.FINNHUB_MARKET, "general:7", tickers=()),), min_id=7, skipped=0),
            MarketNews(articles=(), min_id=None, skipped=0),
        ]
    )
    poller = FinnhubMarketNewsPoller(provider=provider, store=store)
    for _ in range(4):
        await poller.poll(NOW)
    assert provider.calls == [None, None, 7, 7]


@pytest.mark.asyncio
async def test_finnhub_unavailable_skips_and_error_raises(store: NewsStore) -> None:
    assert isinstance(await FinnhubMarketNewsPoller(provider=None, store=store).poll(NOW), PollSkipped)
    failing = FinnhubMarketNewsPoller(
        provider=FakeMarketNews(answers=[NewsProviderError("finnhub 429")]), store=store
    )
    with pytest.raises(NewsProviderError):
        await failing.poll(NOW)


# ------------------------------------------------------------------ discovery: Massive


@pytest.mark.asyncio
async def test_massive_first_run_is_two_hours_back_on_an_empty_store(store: NewsStore) -> None:
    provider = FakeMassive()
    await MassiveNewsPoller(provider=provider, store=store).poll(NOW)
    assert provider.calls == [NOW - FIRST_RUN_LOOKBACK]


@pytest.mark.asyncio
async def test_a_future_stored_massive_row_is_clamped_to_now_and_logged(
    store: NewsStore, sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    # Inverted from the first cut, which adopted ``stored - 2h`` here: a row
    # stamped six hours ahead would have made the first call ask from T+4h and
    # never request (T, T+4h] at all.
    stamp = NOW + timedelta(hours=6)
    seed_rows(sessions, article(NewsFeed.MASSIVE_NEWS, "ahead", published_at=stamp))
    provider = FakeMassive()
    with caplog.at_level(logging.INFO, logger=pollers.__name__):
        await MassiveNewsPoller(provider=provider, store=store).poll(NOW)
    assert provider.calls == [NOW - FIRST_RUN_LOOKBACK]
    clamped = [r for r in caplog.records if getattr(r, "event", None) == "massive_news_cursor_clamped"]
    assert len(clamped) == 1
    assert getattr(clamped[0], "raw") == stamp.isoformat()
    assert getattr(clamped[0], "clamped_to") == NOW.isoformat()
    assert getattr(clamped[0], "source") == "stored"


@pytest.mark.asyncio
async def test_a_future_returned_massive_cursor_is_clamped_to_now_and_logged(
    store: NewsStore, caplog: pytest.LogCaptureFixture
) -> None:
    # The audit's case: one article at T+6h. Unclamped, the next call asks
    # ``published_utc.gt = T+4h`` and every article in (T, T+4h] is skipped.
    ahead = NOW + timedelta(hours=6)
    provider = FakeMassive(
        answers=[
            MassiveNews(
                articles=(article(NewsFeed.MASSIVE_NEWS, "ahead", published_at=ahead),),
                cursor=ahead,
                complete=True,
                pages=1,
                skipped=0,
            ),
        ]
    )
    poller = MassiveNewsPoller(provider=provider, store=store)
    with caplog.at_level(logging.INFO, logger=pollers.__name__):
        first = await poller.poll(NOW)
    assert isinstance(first, DiscoveryPollResult)
    assert poller.cursor == NOW
    assert first.cursor_after == NOW
    later = NOW + timedelta(minutes=1)
    await poller.poll(later)
    assert provider.calls == [NOW - FIRST_RUN_LOOKBACK, NOW - MASSIVE_OVERLAP]
    clamped = [r for r in caplog.records if getattr(r, "event", None) == "massive_news_cursor_clamped"]
    assert [getattr(r, "source") for r in clamped] == ["returned"]
    assert getattr(clamped[0], "raw") == ahead.isoformat()
    assert getattr(clamped[0], "clamped_to") == NOW.isoformat()


@pytest.mark.asyncio
async def test_a_massive_cursor_at_now_exactly_is_not_clamped(
    store: NewsStore, caplog: pytest.LogCaptureFixture
) -> None:
    provider = FakeMassive(
        answers=[MassiveNews(articles=(), cursor=NOW, complete=True, pages=1, skipped=0)]
    )
    poller = MassiveNewsPoller(provider=provider, store=store)
    with caplog.at_level(logging.INFO, logger=pollers.__name__):
        await poller.poll(NOW)
    assert poller.cursor == NOW
    assert not [r for r in caplog.records if getattr(r, "event", None) == "massive_news_cursor_clamped"]


@pytest.mark.asyncio
async def test_massive_reads_back_a_two_hour_overlap_and_never_regresses(store: NewsStore) -> None:
    newest = NOW - timedelta(minutes=1)
    provider = FakeMassive(
        answers=[
            MassiveNews(
                articles=(article(NewsFeed.MASSIVE_NEWS, "1", published_at=newest),),
                cursor=newest,
                complete=True,
                pages=1,
                skipped=0,
            ),
            None,  # nothing new: the provider returns the value passed in
            None,
        ]
    )
    poller = MassiveNewsPoller(provider=provider, store=store)
    for _ in range(3):
        await poller.poll(NOW)
    assert MASSIVE_OVERLAP == timedelta(hours=2)
    # Without the monotonic cursor, each empty answer would slide two more hours back.
    assert provider.calls == [
        NOW - FIRST_RUN_LOOKBACK,
        newest - MASSIVE_OVERLAP,
        newest - MASSIVE_OVERLAP,
    ]


@pytest.mark.asyncio
async def test_massive_error_raises_stores_nothing_and_keeps_the_cursor(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    provider = FakeMassive(answers=[NewsProviderError("massive 503")])
    poller = MassiveNewsPoller(provider=provider, store=store)
    with pytest.raises(NewsProviderError):
        await poller.poll(NOW)
    await poller.poll(NOW)
    assert provider.calls == [NOW - FIRST_RUN_LOOKBACK] * 2
    assert row_count(sessions) == 0


@pytest.mark.asyncio
async def test_massive_unavailable_skips(store: NewsStore) -> None:
    result = await MassiveNewsPoller(provider=None, store=store).poll(NOW)
    assert isinstance(result, PollSkipped)
    assert "MASSIVE_API_KEY" in result.reason


# ------------------------------------------------------------------ discovery: restart backfill


_BACKFILL_FEEDS = [
    pytest.param(NewsFeed.ALPACA_NEWS, ALPACA_BACKFILL_CAP, id="alpaca"),
    pytest.param(NewsFeed.MASSIVE_NEWS, MASSIVE_BACKFILL_CAP, id="massive"),
]


async def _first_request(feed: NewsFeed, store: NewsStore) -> datetime:
    """The instant a fresh poller of ``feed`` first asks from."""
    if feed is NewsFeed.ALPACA_NEWS:
        alpaca = FakeAlpaca()
        await AlpacaNewsPoller(provider=alpaca, store=store).poll(NOW)
        return alpaca.calls[0]
    massive = FakeMassive()
    await MassiveNewsPoller(provider=massive, store=store).poll(NOW)
    return massive.calls[0]


def _start_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == "news_first_run_start"]


def test_the_backfill_caps_are_three_days_for_massive_and_one_for_alpaca() -> None:
    assert MASSIVE_BACKFILL_CAP == timedelta(days=3)
    assert ALPACA_BACKFILL_CAP == timedelta(days=1)


@pytest.mark.parametrize(("feed", "cap"), _BACKFILL_FEEDS)
@pytest.mark.asyncio
async def test_first_run_with_no_stored_row_reads_two_hours_back(
    feed: NewsFeed,
    cap: timedelta,
    store: NewsStore,
    sessions: Callable[[], Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Another feed's rows are not this feed's high-water mark.
    seed_rows(
        sessions,
        article(NewsFeed.FINNHUB_COMPANY, "other", published_at=NOW - timedelta(hours=10)),
    )
    with caplog.at_level(logging.INFO, logger=pollers.__name__):
        assert await _first_request(feed, store) == NOW - FIRST_RUN_LOOKBACK
    [record] = _start_logs(caplog)
    assert getattr(record, "reason") == "default"
    assert getattr(record, "feed") == feed.value
    assert getattr(record, "start") == (NOW - FIRST_RUN_LOOKBACK).isoformat()


@pytest.mark.parametrize(("feed", "cap"), _BACKFILL_FEEDS)
@pytest.mark.asyncio
async def test_first_run_backfills_from_the_newest_stored_row_minus_two_hours(
    feed: NewsFeed,
    cap: timedelta,
    store: NewsStore,
    sessions: Callable[[], Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_rows(
        sessions,
        article(feed, "older", published_at=NOW - timedelta(hours=20)),
        article(feed, "newest", published_at=NOW - timedelta(hours=10)),
    )
    with caplog.at_level(logging.INFO, logger=pollers.__name__):
        assert await _first_request(feed, store) == NOW - timedelta(hours=12)
    [record] = _start_logs(caplog)
    assert getattr(record, "reason") == "stored"
    assert getattr(record, "stored") == (NOW - timedelta(hours=10)).isoformat()


@pytest.mark.parametrize(("feed", "cap"), _BACKFILL_FEEDS)
@pytest.mark.asyncio
async def test_first_run_backfill_is_capped(
    feed: NewsFeed,
    cap: timedelta,
    store: NewsStore,
    sessions: Callable[[], Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_rows(sessions, article(feed, "stale", published_at=NOW - timedelta(days=5)))
    with caplog.at_level(logging.INFO, logger=pollers.__name__):
        assert await _first_request(feed, store) == NOW - cap
    [record] = _start_logs(caplog)
    assert getattr(record, "reason") == "cap"


@pytest.mark.parametrize(("feed", "cap"), _BACKFILL_FEEDS)
@pytest.mark.asyncio
async def test_first_run_backfill_at_the_cap_exactly_is_permitted(
    feed: NewsFeed,
    cap: timedelta,
    store: NewsStore,
    sessions: Callable[[], Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_rows(sessions, article(feed, "edge", published_at=NOW - cap + FIRST_RUN_LOOKBACK))
    with caplog.at_level(logging.INFO, logger=pollers.__name__):
        assert await _first_request(feed, store) == NOW - cap
    [record] = _start_logs(caplog)
    assert getattr(record, "reason") == "stored"


@pytest.mark.parametrize(("feed", "cap"), _BACKFILL_FEEDS)
@pytest.mark.asyncio
async def test_first_run_clamps_a_future_stored_row_to_now(
    feed: NewsFeed,
    cap: timedelta,
    store: NewsStore,
    sessions: Callable[[], Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_rows(sessions, article(feed, "ahead", published_at=NOW + timedelta(hours=6)))
    with caplog.at_level(logging.INFO, logger=pollers.__name__):
        assert await _first_request(feed, store) == NOW - FIRST_RUN_LOOKBACK
    [record] = _start_logs(caplog)
    assert getattr(record, "reason") == "stored"


# ------------------------------------------------------------------ discovery: overlapping calls


class Gated:
    """A discovery provider whose first call waits for ``release``; later calls answer at once."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def _answer(self) -> Any:
        self.calls += 1
        answer = self.answers[self.calls - 1]
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        return answer

    async def news_since(self, published_after: datetime) -> Any:
        return await self._answer()

    async def news(self, *, start: datetime) -> Any:
        return await self._answer()

    async def market_news(self, min_id: int | None) -> Any:
        return await self._answer()


async def _second_finishes_first(poll: Callable[[datetime], Any], gate: Gated) -> None:
    """Start call A, run call B to completion while A waits, then let A finish."""
    first = asyncio.create_task(poll(NOW))
    await asyncio.wait_for(gate.entered.wait(), timeout=5)
    await poll(NOW)
    gate.release.set()
    await asyncio.wait_for(first, timeout=5)


@pytest.mark.asyncio
async def test_an_overlapping_massive_call_cannot_set_the_cursor_back(store: NewsStore) -> None:
    early, late = NOW - timedelta(minutes=50), NOW - timedelta(minutes=10)
    gate = Gated(
        MassiveNews(
            articles=(article(NewsFeed.MASSIVE_NEWS, "a", published_at=early),),
            cursor=early,
            complete=True,
            pages=1,
            skipped=0,
        ),
        MassiveNews(
            articles=(article(NewsFeed.MASSIVE_NEWS, "b", published_at=late),),
            cursor=late,
            complete=True,
            pages=1,
            skipped=0,
        ),
    )
    poller = MassiveNewsPoller(provider=gate, store=store)
    await _second_finishes_first(poller.poll, gate)
    assert poller.cursor == late


@pytest.mark.asyncio
async def test_an_overlapping_alpaca_call_cannot_set_the_cursor_back(store: NewsStore) -> None:
    early, late = NOW - timedelta(minutes=50), NOW - timedelta(minutes=10)
    gate = Gated(
        FakeAlpacaNewsAnswer(articles=(article(NewsFeed.ALPACA_NEWS, "a"),), cursor=early),
        FakeAlpacaNewsAnswer(articles=(article(NewsFeed.ALPACA_NEWS, "b"),), cursor=late),
    )
    poller = AlpacaNewsPoller(provider=gate, store=store)
    await _second_finishes_first(poller.poll, gate)
    assert poller.cursor == late


@pytest.mark.asyncio
async def test_an_overlapping_finnhub_call_cannot_set_the_min_id_back(store: NewsStore) -> None:
    gate = Gated(
        MarketNews(
            articles=(article(NewsFeed.FINNHUB_MARKET, "general:5", tickers=()),),
            min_id=5,
            skipped=0,
        ),
        MarketNews(
            articles=(article(NewsFeed.FINNHUB_MARKET, "general:9", tickers=()),),
            min_id=9,
            skipped=0,
        ),
    )
    poller = FinnhubMarketNewsPoller(provider=gate, store=store)
    await _second_finishes_first(poller.poll, gate)
    assert poller.min_id == 9


# ------------------------------------------------------------------ the universe builder


class FakeSeed:
    """Duck-types the one method leaders_from_seed calls."""

    def leaders(self, per_fund: int = 5) -> dict[str, tuple[str, ...]]:
        return {"XLK": ("AAPL", "MSFT"), "XLE": ("XOM",)}


def add_watch(sessions: Callable[[], Session], ticker: str, *, removed: bool = False) -> None:
    with sessions() as session:
        session.add(
            WatchSymbol(
                ticker=ticker,
                added_at=NOW - timedelta(days=1),
                removed_at=NOW if removed else None,
            )
        )
        session.commit()


async def _positions(*symbols: str) -> list[str]:
    return list(symbols)


@pytest.mark.asyncio
async def test_the_universe_joins_markets_leaders_positions_and_active_manual_watches(
    sessions: Callable[[], Session],
) -> None:
    add_watch(sessions, "PLTR")
    add_watch(sessions, "GME", removed=True)
    built = await build_watch_universe(
        markets=["SPY", "NVDA"],
        position_underlyings=lambda: _positions("TSLA"),
        session_factory=sessions,
        seed_loader=lambda: cast(SpdrSeed, FakeSeed()),
    )
    universe = built.universe
    assert universe.polled_symbols == ("AAPL", "MSFT", "NVDA", "PLTR", "SPY", "TSLA", "XOM")
    assert universe.reasons("PLTR") == {Membership.MANUAL}
    assert universe.reasons("TSLA") == {Membership.POSITION}
    assert "GME" not in universe
    assert not built.seed_missing and built.positions_error is None


@pytest.mark.asyncio
async def test_an_absent_seed_leaves_leaders_empty_and_says_so(sessions: Callable[[], Session]) -> None:
    built = await build_watch_universe(
        markets=["NVDA"],
        position_underlyings=lambda: _positions(),
        session_factory=sessions,
        seed_loader=lambda: None,
    )
    assert built.seed_missing
    assert built.universe.polled_symbols == ("NVDA",)


@pytest.mark.asyncio
async def test_a_malformed_seed_leaves_leaders_empty_and_records_the_error(
    sessions: Callable[[], Session],
) -> None:
    def broken() -> SpdrSeed | None:
        raise SeedError("spdr_holdings.csv: line 3: bad weight")

    built = await build_watch_universe(
        markets=["NVDA"],
        position_underlyings=lambda: _positions(),
        session_factory=sessions,
        seed_loader=broken,
    )
    assert built.seed_error is not None and "bad weight" in built.seed_error
    assert built.universe.polled_symbols == ("NVDA",)


@pytest.mark.asyncio
async def test_a_failing_position_source_yields_no_positions_but_a_universe(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    async def broker_down() -> list[str]:
        raise ConnectionError("paper-api unreachable")

    with caplog.at_level(logging.WARNING, logger=pollers.__name__):
        built = await build_watch_universe(
            markets=["NVDA"],
            position_underlyings=broker_down,
            session_factory=sessions,
            seed_loader=lambda: None,
        )
    assert built.universe.polled_symbols == ("NVDA",)
    assert built.positions_error is not None and "ConnectionError" in built.positions_error
    assert any(getattr(r, "event", None) == "watch_universe_positions_unavailable" for r in caplog.records)


# ------------------------------------------------------------------ assets, tradeability, prune


@pytest.mark.asyncio
async def test_refresh_assets_skips_without_a_source_and_holds_what_it_fetched(
    holder: AssetDirectoryHolder,
) -> None:
    assert isinstance(await refresh_assets(holder, None), PollSkipped)
    result = await refresh_assets(holder, DirectorySource())
    assert isinstance(result, AssetsRefreshed)
    assert result.assets == 4 and result.optionable == 4
    assert holder.current() is DIRECTORY


@pytest.mark.asyncio
async def test_refresh_assets_failure_raises(holder: AssetDirectoryHolder) -> None:
    with pytest.raises(RuntimeError):
        await refresh_assets(holder, DirectorySource(fail=True))


@dataclass
class TradeabilityCall:
    tickers: Sequence[str]
    session_date: date
    assets: AssetDirectory | None
    ipo_dates: object = None


@pytest.fixture
def fake_refresh(monkeypatch: pytest.MonkeyPatch) -> list[TradeabilityCall]:
    calls: list[TradeabilityCall] = []

    async def refresh(
        *,
        provider: object,
        assets: AssetDirectory | None,
        tickers: Sequence[str],
        session_date: date,
        session_factory: object,
        now: object,
        ipo_dates: object,
    ) -> RefreshResult:
        calls.append(TradeabilityCall(list(tickers), session_date, assets, ipo_dates))
        return RefreshResult(
            session_date=session_date, skipped=None, results=(), errors=(), deferred=(),
            bar_requests=1, root_requests=0,
        )

    monkeypatch.setattr(pollers, "refresh_tradeability", refresh)
    return calls


class NoInputs:
    async def has_standard_root(self, ticker: str) -> bool:
        raise AssertionError("not called")

    async def adv_daily_bars(self, symbols: Sequence[str], *, session_date: date) -> Any:
        raise AssertionError("not called")


def label(
    sessions: Callable[[], Session],
    vendor_id: str,
    ticker: str,
    *,
    source: str = "rules",
    direction: str = "bullish",
    rule_id: str | None = "guidance_raised",
) -> None:
    """One ``sentiment_label`` row on the stored article ``vendor_id``, written directly."""
    with sessions() as session:
        article_id = session.scalars(
            select(ArticleRow.id).where(ArticleRow.vendor_id == vendor_id)
        ).one()
        session.add(
            SentimentLabelRow(
                article_id=article_id,
                ticker=ticker,
                source=source,
                tier="rules" if source == "rules" else "vendor",
                direction=direction,
                reasoning="raises full-year guidance",
                rule_id=rule_id if source == "rules" else None,
                labeled_at=NOW,
            )
        )
        session.commit()


async def _refresh(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, now: datetime
) -> RefreshResult | PollSkipped:
    return await refresh_tradeability_cache(
        holder=holder,
        provider=NoInputs(),
        universe=UniverseBox("NVDA"),
        session_factory=sessions,
        now=now,
    )


@pytest.mark.asyncio
async def test_tradeability_candidates_are_qualifying_signals_minus_the_watch(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, fake_refresh: list[TradeabilityCall]
) -> None:
    """Decision 21: only off-watch tickers carrying a qualifying signal, for today's ET date."""
    await holder.refresh(DirectorySource())
    now = et(2026, 9, 24, 21, 30)  # 01:30 UTC on the 25th: the ET date is still the 24th
    seed_rows(
        sessions,
        article(NewsFeed.ALPACA_NEWS, "today", published_at=et(2026, 9, 24, 0, 0), tickers=("ORCL", "NVDA")),
    )
    label(sessions, "today", "ORCL")
    label(sessions, "today", "NVDA")
    result = await _refresh(sessions, holder, now)
    assert isinstance(result, RefreshResult)
    assert fake_refresh == [TradeabilityCall(["ORCL"], date(2026, 9, 24), DIRECTORY)]


@pytest.mark.asyncio
async def test_a_rules_signal_on_a_ticker_the_article_does_not_tag_is_checked(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, fake_refresh: list[TradeabilityCall]
) -> None:
    """The audit's probe: a MARKET-only Finnhub market story whose headline names ACME."""
    await holder.refresh(DirectorySource())
    seed_rows(sessions, article(NewsFeed.FINNHUB_MARKET, "acme", tickers=("MARKET",)))
    label(sessions, "acme", "ACME")
    await _refresh(sessions, holder, NOW)
    assert [call.tickers for call in fake_refresh] == [["ACME"]]


@pytest.mark.asyncio
async def test_an_earlier_day_signal_left_unchecked_is_checked_on_a_later_run(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, fake_refresh: list[TradeabilityCall]
) -> None:
    """Deferred or errored on the 21st, never cached: asked again on the 24th."""
    await holder.refresh(DirectorySource())
    seed_rows(
        sessions,
        article(NewsFeed.ALPACA_NEWS, "old", published_at=et(2026, 9, 21, 9, 0), tickers=("ORCL",)),
    )
    label(sessions, "old", "ORCL", source="massive", direction="bearish", rule_id=None)
    await _refresh(sessions, holder, NOW)
    assert fake_refresh == [TradeabilityCall(["ORCL"], date(2026, 9, 24), DIRECTORY)]


@pytest.mark.asyncio
async def test_a_tagged_ticker_with_no_qualifying_signal_is_not_checked(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, fake_refresh: list[TradeabilityCall]
) -> None:
    await holder.refresh(DirectorySource())
    seed_rows(sessions, article(NewsFeed.ALPACA_NEWS, "tagged", tickers=("ORCL", "MSFT")))
    label(sessions, "tagged", "MSFT", source="massive", direction="neutral", rule_id=None)
    result = await _refresh(sessions, holder, NOW)
    assert isinstance(result, PollSkipped) and "qualifying signal" in result.reason
    assert fake_refresh == []


@pytest.mark.asyncio
async def test_candidates_go_newest_qualifying_signal_first(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, fake_refresh: list[TradeabilityCall]
) -> None:
    await holder.refresh(DirectorySource())
    seed_rows(
        sessions,
        article(NewsFeed.ALPACA_NEWS, "a", published_at=NOW - timedelta(days=3), tickers=("AAPL",)),
        article(NewsFeed.ALPACA_NEWS, "b", published_at=NOW - timedelta(hours=1), tickers=("MSFT",)),
        article(NewsFeed.ALPACA_NEWS, "c", published_at=NOW - timedelta(hours=1), tickers=("ORCL",)),
    )
    for vendor_id, ticker in (("a", "AAPL"), ("b", "MSFT"), ("c", "ORCL")):
        label(sessions, vendor_id, ticker)
    await _refresh(sessions, holder, NOW)
    assert [call.tickers for call in fake_refresh] == [["MSFT", "ORCL", "AAPL"]]


class EmptyBars:
    """No bars for anyone: every checked ticker fails closed and is cached, no root call."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def has_standard_root(self, ticker: str) -> bool:
        raise AssertionError("a ticker with no bars never reaches the root check")

    async def adv_daily_bars(self, symbols: Sequence[str], *, session_date: date) -> Any:
        self.requested.extend(symbols)
        return {}


@pytest.mark.asyncio
async def test_the_per_run_cap_defers_the_oldest_signals(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder
) -> None:
    await holder.refresh(DirectorySource())
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    tickers = [f"Q{a}{b}" for a in letters for b in letters][: MAX_TICKERS_PER_RUN + 1]
    seed_rows(
        sessions,
        *(
            article(
                NewsFeed.FINNHUB_MARKET, f"s{i}", tickers=("MARKET",),
                published_at=NOW - timedelta(minutes=i + 1),
            )
            for i in range(len(tickers))
        ),
    )
    for i, ticker in enumerate(tickers):
        label(sessions, f"s{i}", ticker)
    inputs = EmptyBars()
    result = await refresh_tradeability_cache(
        holder=holder, provider=inputs, universe=UniverseBox("NVDA"),
        session_factory=sessions, now=NOW,
    )
    assert isinstance(result, RefreshResult)
    assert [r.ticker for r in result.results] == tickers[:MAX_TICKERS_PER_RUN]
    assert result.deferred == (tickers[-1],)
    assert inputs.requested == tickers[:MAX_TICKERS_PER_RUN]


class Ipos:
    async def ipo_date(self, symbol: str) -> date | None:
        raise AssertionError("not called")


@pytest.mark.asyncio
async def test_tradeability_passes_the_ipo_date_source_through_and_defaults_to_none(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, fake_refresh: list[TradeabilityCall]
) -> None:
    """Q12: the scheduler hands the Finnhub provider in; absent, partial windows fail closed."""
    await holder.refresh(DirectorySource())
    now = et(2026, 9, 24, 21, 30)
    seed_rows(
        sessions,
        article(NewsFeed.ALPACA_NEWS, "today", published_at=et(2026, 9, 24, 0, 0), tickers=("ORCL",)),
    )
    label(sessions, "today", "ORCL")
    common: dict[str, Any] = dict(
        holder=holder, provider=NoInputs(), universe=UniverseBox("NVDA"),
        session_factory=sessions, now=now,
    )
    source = Ipos()
    await refresh_tradeability_cache(**common, ipo_dates=source)
    await refresh_tradeability_cache(**common)
    assert [call.ipo_dates for call in fake_refresh] == [source, None]


@pytest.mark.asyncio
async def test_tradeability_skips_without_a_directory_or_a_provider(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, fake_refresh: list[TradeabilityCall]
) -> None:
    common: dict[str, Any] = dict(universe=UniverseBox("NVDA"), session_factory=sessions, now=NOW)
    no_dir = await refresh_tradeability_cache(holder=holder, provider=NoInputs(), **common)
    assert isinstance(no_dir, PollSkipped) and "asset directory" in no_dir.reason
    await holder.refresh(DirectorySource())
    no_provider = await refresh_tradeability_cache(holder=holder, provider=None, **common)
    assert isinstance(no_provider, PollSkipped)
    nothing = await refresh_tradeability_cache(holder=holder, provider=NoInputs(), **common)
    assert isinstance(nothing, PollSkipped) and "no off-watch" in nothing.reason
    assert fake_refresh == []


@pytest.mark.asyncio
async def test_a_stale_directory_is_still_used_and_logged_with_its_age(
    sessions: Callable[[], Session], fake_refresh: list[TradeabilityCall], caplog: pytest.LogCaptureFixture
) -> None:
    clock = [NOW - timedelta(hours=30)]
    stale = AssetDirectoryHolder(now=lambda: clock[0])
    await stale.refresh(DirectorySource())
    clock[0] = NOW
    seed_rows(sessions, article(NewsFeed.ALPACA_NEWS, "x", tickers=("ORCL",)))
    label(sessions, "x", "ORCL")
    with caplog.at_level(logging.WARNING, logger=pollers.__name__):
        result = await refresh_tradeability_cache(
            holder=stale, provider=NoInputs(), universe=UniverseBox("NVDA"),
            session_factory=sessions, now=NOW,
        )
    assert isinstance(result, RefreshResult)
    assert [c.tickers for c in fake_refresh] == [["ORCL"]]
    warned = [r for r in caplog.records if getattr(r, "event", None) == "asset_directory_stale_used"]
    assert len(warned) == 1
    assert getattr(warned[0], "age_seconds") == 30 * 3600


@pytest.mark.asyncio
async def test_prune_passes_the_label_predicate_and_runs_under_the_store_lock(
    store: NewsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = pollers.prune

    def spy(session: Session, **kwargs: Any) -> Any:
        seen.update(kwargs)
        seen["locked"] = store.lock.locked()
        return real(session, **kwargs)

    monkeypatch.setattr(pollers, "prune", spy)
    await prune_news(store, NOW)
    assert seen["labelled_article_ids"] is labelled_article_ids
    assert seen["now"] == NOW
    assert seen["locked"] is True


@pytest.mark.asyncio
async def test_prune_deletes_an_old_unlabelled_article_and_commits(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    seed_rows(
        sessions,
        article(NewsFeed.ALPACA_NEWS, "ancient", published_at=NOW - timedelta(days=120)),
        article(NewsFeed.ALPACA_NEWS, "fresh", published_at=NOW - timedelta(days=1)),
    )
    result = await prune_news(store, NOW)
    assert result.articles_deleted == 1
    assert row_count(sessions) == 1


# ------------------------------------------------------------------ isolation


def test_pollers_imports_neither_the_engine_nor_the_api() -> None:
    """Decision 1: a context job is never a rule-9 producer -- it cannot reach the runtime."""
    code = (
        "import json, sys\n"
        "import corollary.data.news.pollers\n"
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.split('.')[:2] in (['corollary', 'engine'], ['corollary', 'api']))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
