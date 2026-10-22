"""Unit 5.2: labels are persisted at ingest, and the prune keeps what they touch.

Phase 3 decision 4: every article stored is labelled by every source that can
reach it -- the rules tier on every headline, Massive on the articles it
carries ``insights[]`` for -- and each label is its own ``sentiment_label``
row, upserted by ``(article_id, ticker, source)``. No label, no row.

Decision 21: the prune deletes only canonical groups of which **no** member
carries a label, and nulls ``summary`` on every old article regardless. There
is **no backfill** (spec *Out of scope*: "No label backfill (Q7)") -- labelling
happens when an article is stored, from this unit forward.

Headlines here are SYNTHETIC unless a test says it loads a recorded fixture:
no recorded headline fires a rule.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.routes.markets import UNIVERSE, UNIVERSE_FUND_SYMBOLS
from corollary.data.news.article import NewsArticle, NewsFeed, VendorInsight
from corollary.data.news.assets import AssetDirectoryHolder
from corollary.data.news.labelling import (
    NameBooks,
    label_stored,
    labelled_article_ids,
)
from corollary.data.news.labels import Direction
from corollary.data.news.pollers import NewsStore, prune_news
from corollary.data.news.rules import WATCH_COMPANY_NAMES
from corollary.data.news.vendor import vendor_labels
from corollary.data.providers.interface import AssetDirectory, EquityAsset
from corollary.data.providers.massive import MassiveCredentials, MassiveProvider
from corollary.db.models import Base, NewsArticleTicker, SentimentLabelRow
from corollary.db.models import NewsArticle as ArticleRow
from corollary.db.session import create_db_engine, sqlite_url
from corollary.ratelimit import HostRateLimiter

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)

MASSIVE_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "massive"
#: Obviously fake. Rule 6: no key material in tests or fixtures.
FAKE_CREDENTIALS = MassiveCredentials(token="not-a-real-massive-key-000000000000")

#: SYNTHETIC: fires ``earnings_beat`` on Apple, a watch name (test_rules.py's case).
FIRING_HEADLINE = "Apple Q3 EPS $1.57 Beats $1.43 Estimate, Sales $94.04B Beat $89.53B Estimate"


def equity(symbol: str, name: str) -> EquityAsset:
    return EquityAsset(symbol=symbol, name=name, tradable=True, has_options=True, exchange="NASDAQ")


#: The active US equities these tests' vendor insights may land on. Ingest and
#: the vendor labeller filter through the same directory (5.2 audit L5), so an
#: insight ticker absent here gets no label. The recorded Massive pages also
#: carry tickers deliberately left out -- a preferred (HLPB), warrants
#: (BBAI.WS, MNYWW) and OTC ADRs (BYDDY, FWDYY, ZURVY) -- which are dropped.
DIRECTORY = AssetDirectory(
    assets=(
        equity("AAPL", "Apple Inc."),
        equity("MSFT", "Microsoft Corporation Common Stock"),
        equity("SPY", "SPDR S&P 500 ETF Trust"),
        equity("UEC", "Uranium Energy Corp. Common Stock"),
        equity("CCJ", "Cameco Corporation Common Stock"),
        equity("UUUU", "Energy Fuels Inc Common Shares"),
        equity("URG", "Ur-Energy Inc Common Shares"),
        equity("AG", "First Majestic Silver Corp. Common Shares"),
        equity("HL", "Hecla Mining Company Common Stock"),
        equity("AMZN", "Amazon.com, Inc. Common Stock"),
        equity("META", "Meta Platforms, Inc. Class A Common Stock"),
        equity("EQIX", "Equinix, Inc. Common Stock"),
    )
)
#: Recorded insight tickers that are not in :data:`DIRECTORY`, as decoded.
RECORDED_NON_EQUITIES = frozenset({"HLPB", "BBAI.WS", "MNYWW", "BYDDY", "FWDYY", "ZURVY"})


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "labelling.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(db_engine: Engine) -> Callable[[], Session]:
    return lambda: Session(db_engine)


@pytest.fixture
def holder() -> AssetDirectoryHolder:
    class Source:
        async def active_equities(self) -> AssetDirectory:
            return DIRECTORY

    held = AssetDirectoryHolder(now=lambda: NOW)
    asyncio.run(held.refresh(Source()))
    return held


def watch_of(*symbols: str) -> Callable[[], Any]:
    async def source() -> Iterable[str]:
        return symbols

    return source


@pytest.fixture
def books(holder: AssetDirectoryHolder) -> NameBooks:
    return NameBooks(watch=watch_of("AAPL", "MSFT"), assets=holder, funds=UNIVERSE_FUND_SYMBOLS)


@pytest.fixture
def store(sessions: Callable[[], Session], holder: AssetDirectoryHolder, books: NameBooks) -> NewsStore:
    return NewsStore(session_factory=sessions, assets=holder, books=books)


def alpaca(
    vendor_id: str,
    headline: str,
    *,
    tickers: tuple[str, ...] = ("AAPL",),
    published_at: datetime = NOW - timedelta(minutes=5),
    url: str | None = None,
) -> NewsArticle:
    """A SYNTHETIC Alpaca article."""
    return NewsArticle(
        vendor="alpaca",
        vendor_id=vendor_id,
        feed=NewsFeed.ALPACA_NEWS,
        url=url or f"https://example.com/alpaca/{vendor_id}",
        headline=headline,
        summary="A summary",
        publisher="Benzinga",
        published_at=published_at,
        tickers=tickers,
    )


def massive(vendor_id: str, *insights: VendorInsight, headline: str = "Uranium names move") -> NewsArticle:
    """A SYNTHETIC Massive article."""
    return NewsArticle(
        vendor="massive",
        vendor_id=vendor_id,
        feed=NewsFeed.MASSIVE_NEWS,
        url=f"https://example.com/massive/{vendor_id}",
        headline=headline,
        summary=None,
        publisher="Zacks",
        published_at=NOW - timedelta(minutes=5),
        tickers=tuple(i.ticker for i in insights),
        insights=insights,
    )


def rows(sessions: Callable[[], Session]) -> list[SentimentLabelRow]:
    with sessions() as session:
        found = session.scalars(
            select(SentimentLabelRow).order_by(
                SentimentLabelRow.article_id, SentimentLabelRow.ticker, SentimentLabelRow.source
            )
        ).all()
        session.expunge_all()
        return list(found)


def as_tuple(row: SentimentLabelRow) -> tuple[object, ...]:
    return (row.article_id, row.ticker, row.source, row.tier, row.direction, row.reasoning, row.rule_id)


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError("the limiter must not sleep")


async def recorded(name: str) -> tuple[NewsArticle, ...]:
    """A RECORDED Massive fixture, decoded by the real provider."""
    envelope: dict[str, Any] = json.loads((MASSIVE_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    body = envelope["body"]
    body.pop("next_url", None)
    text = json.dumps(body)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text, headers={"content-type": "application/json"})

    provider = MassiveProvider(
        credentials=FAKE_CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        limiter=HostRateLimiter(requests_per_minute=10_000, per_host={}, clock=lambda: 0.0, sleep=_never_sleep),
    )
    async with provider:
        result = await provider.news_since(datetime(2024, 1, 1, tzinfo=timezone.utc))
    return result.articles


# --------------------------------------------------------------- rules at ingest


@pytest.mark.asyncio
async def test_a_firing_headline_is_stored_with_its_rules_label(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    await store.store([alpaca("1", FIRING_HEADLINE)], now=NOW)
    (label,) = rows(sessions)
    assert label.ticker == "AAPL"
    assert (label.source, label.tier, label.direction) == ("rules", "rules", "bullish")
    assert label.rule_id == "earnings_beat"
    assert label.reasoning is not None and "earnings_beat" in label.reasoning
    assert label.labeled_at == NOW


@pytest.mark.asyncio
async def test_no_label_means_no_row(store: NewsStore, sessions: Callable[[], Session]) -> None:
    result = await store.store(
        [alpaca("1", "Apple holds its annual shareholder meeting"), massive("2")], now=NOW
    )
    assert result.inserted == 2
    assert rows(sessions) == []


@pytest.mark.asyncio
async def test_a_linked_duplicate_is_labelled_on_its_own_row(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    """A cross-vendor copy is still an article; it carries its own label."""
    first = alpaca("1", FIRING_HEADLINE, url="https://example.com/story")
    copy = NewsArticle(
        vendor="finnhub",
        vendor_id="9",
        feed=NewsFeed.FINNHUB_COMPANY,
        url="https://example.com/story",
        headline=FIRING_HEADLINE,
        summary=None,
        publisher="Benzinga",
        published_at=first.published_at,
        tickers=("AAPL",),
    )
    result = await store.store([first, copy], now=NOW)
    assert result.duplicates_linked == 1
    labelled = {row.article_id for row in rows(sessions)}
    assert labelled == {s.article_id for s in result.stored}
    assert len(labelled) == 2


@pytest.mark.asyncio
async def test_without_a_name_book_the_rules_tier_is_skipped_and_said(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, caplog: pytest.LogCaptureFixture
) -> None:
    store = NewsStore(session_factory=sessions, assets=holder, books=None)
    caplog.set_level(logging.INFO, logger="corollary.data.news.labelling")
    await store.store(
        [alpaca("1", FIRING_HEADLINE), massive("2", VendorInsight("UEC", "negative", "Down."))], now=NOW
    )
    assert [(r.ticker, r.source) for r in rows(sessions)] == [("UEC", "massive")]
    (line,) = [r for r in caplog.records if getattr(r, "event", None) == "news_labels_written"]
    assert getattr(line, "rules_available") is False


# --------------------------------------------------------------- idempotence


@pytest.mark.asyncio
async def test_re_storing_the_same_articles_is_a_no_op(
    store: NewsStore, sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    batch = [alpaca("1", FIRING_HEADLINE), massive("2", VendorInsight("UEC", "negative", "Down."))]
    await store.store(batch, now=NOW)
    before = [(as_tuple(r), r.id, r.labeled_at) for r in rows(sessions)]
    assert len(before) == 2

    caplog.set_level(logging.INFO, logger="corollary.data.news.labelling")
    await store.store(batch, now=NOW + timedelta(minutes=3))
    after = [(as_tuple(r), r.id, r.labeled_at) for r in rows(sessions)]
    assert after == before  # same rows, same ids, labeled_at not moved
    (line,) = [r for r in caplog.records if getattr(r, "event", None) == "news_labels_written"]
    assert getattr(line, "rules_written") == 0
    assert getattr(line, "massive_written") == 0
    assert getattr(line, "revised_ignored") == 0
    assert getattr(line, "unchanged") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "revised", "kept", "rejected"),
    [
        # Massive revises its insight on a re-fetch inside the 2h overlap.
        (
            massive("2", VendorInsight("UEC", "negative", "Down.")),
            massive("2", VendorInsight("UEC", "positive", "Up after all.")),
            ("bearish", None),
            ("bullish", None),
        ),
        # The publisher edits the headline; the same article now fires the other way.
        (
            alpaca("1", FIRING_HEADLINE),
            alpaca("1", "Apple Q3 EPS $1.57 Misses $1.63 Estimate"),
            ("bullish", "earnings_beat"),
            ("bearish", "earnings_miss"),
        ),
    ],
    ids=["massive_revised_insight", "rules_edited_headline"],
)
async def test_the_first_written_label_is_immutable_and_a_revision_is_logged(
    store: NewsStore,
    sessions: Callable[[], Session],
    caplog: pytest.LogCaptureFixture,
    first: NewsArticle,
    revised: NewsArticle,
    kept: tuple[str, str | None],
    rejected: tuple[str, str | None],
) -> None:
    """5.2 audit M2 / Q7: the label that was live is the label that is graded."""
    await store.store([first], now=NOW)
    (before,) = rows(sessions)
    assert (before.direction, before.rule_id) == kept

    caplog.set_level(logging.INFO, logger="corollary.data.news.labelling")
    later = NOW + timedelta(minutes=3)
    await store.store([revised], now=later)
    (after,) = rows(sessions)
    assert (as_tuple(after), after.id, after.labeled_at) == (as_tuple(before), before.id, NOW)

    (ignored,) = [r for r in caplog.records if getattr(r, "event", None) == "news_label_revision_ignored"]
    assert ignored.levelno == logging.WARNING
    assert (getattr(ignored, "kept_direction"), getattr(ignored, "kept_rule_id")) == kept
    assert (getattr(ignored, "revised_direction"), getattr(ignored, "revised_rule_id")) == rejected
    assert getattr(ignored, "article_id") == before.article_id
    assert getattr(ignored, "ticker") == before.ticker
    assert getattr(ignored, "headline") == revised.headline
    assert getattr(ignored, "at") == later.isoformat()
    assert getattr(ignored, "kept_labeled_at") == NOW.isoformat()
    (line,) = [r for r in caplog.records if getattr(r, "event", None) == "news_labels_written"]
    assert getattr(line, "revised_ignored") == 1
    assert getattr(line, "unchanged") == 0
    assert getattr(line, "rules_written") == getattr(line, "massive_written") == 0


def test_label_stored_is_deterministic_for_the_same_input(sessions: Callable[[], Session]) -> None:
    """Called directly: same stored articles, same rows, and the second call writes nothing."""
    from corollary.data.news.ingest import store_articles
    from corollary.data.news.rules import NameBook

    book = NameBook(
        watch=("AAPL",), watch_names=WATCH_COMPANY_NAMES, asset_names={"AAPL": "Apple Inc."}, funds=()
    )
    with sessions() as session:
        result = store_articles(session, [alpaca("1", FIRING_HEADLINE)], assets=DIRECTORY, now=NOW)
        first = label_stored(session, result.stored, book, assets=DIRECTORY, now=NOW)
        second = label_stored(session, result.stored, book, assets=DIRECTORY, now=NOW + timedelta(hours=1))
        session.commit()
    assert first.rules_written == 1 and first.unchanged == 0
    assert second.rules_written == 0 and second.revised_ignored == 0 and second.unchanged == 1


# --------------------------------------------------------------- vendor labels, recorded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    [
        "p5_reference_news_mixed_insight",
        "p4_reference_news_asc_page1",
        "p4_reference_news_asc_page2",
        "p3_reference_news_untickered",
    ],
)
async def test_recorded_massive_insights_are_stored_as_vendor_labels(
    store: NewsStore, sessions: Callable[[], Session], fixture: str
) -> None:
    articles = await recorded(fixture)
    result = await store.store(articles, now=NOW)
    by_vendor_id = {s.article.vendor_id: s.article_id for s in result.stored}
    listed = {asset.symbol for asset in DIRECTORY.assets}
    expected = sorted(
        (by_vendor_id[a.vendor_id], label.ticker, "massive", "vendor", label.direction.value, label.reasoning, None)
        for a in articles
        for label in vendor_labels(a).labels
        if label.ticker in listed
    )
    assert expected  # every recorded page carries at least one usable insight
    stored = sorted(as_tuple(r) for r in rows(sessions) if r.source == "massive")
    assert stored == expected
    # L5: no vendor label without the tag row ingest kept for it.
    with sessions() as session:
        tagged = set(session.execute(select(NewsArticleTicker.article_id, NewsArticleTicker.ticker)).tuples())
    assert {(r.article_id, r.ticker) for r in rows(sessions)} <= tagged
    assert not {r.ticker for r in rows(sessions)} & RECORDED_NON_EQUITIES


@pytest.mark.asyncio
async def test_the_recorded_mixed_insight_is_stored_neutral(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    (article,) = await recorded("p5_reference_news_mixed_insight")
    await store.store([article], now=NOW)
    directions = {r.ticker: r.direction for r in rows(sessions) if r.source == "massive"}
    assert directions["UUUU"] == Direction.NEUTRAL.value  # Massive's "mixed"
    assert directions["UEC"] == directions["CCJ"] == Direction.BEARISH.value


# --------------------------------------------------------------- the per-poll line


@pytest.mark.asyncio
async def test_every_store_logs_one_label_line_with_every_count(
    store: NewsStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="corollary.data.news.labelling")
    await store.store(
        [
            alpaca("1", FIRING_HEADLINE),
            # SYNTHETIC: bullish and bearish both fire -> conflicting, no label.
            alpaca("2", "Apple Q1 EPS Beats Estimate, Lowers Full-Year Guidance"),
            massive("3", VendorInsight("UEC", "negative", "Down."), VendorInsight("UEC", "positive", "Up.")),
        ],
        now=NOW,
    )
    (line,) = [r for r in caplog.records if getattr(r, "event", None) == "news_labels_written"]
    for name in (
        "articles", "rules_written", "massive_written", "revised_ignored", "unchanged",
        "rules_conflicting", "rules_unattributed", "rules_vetoed", "rules_suppressed",
        "rules_truncated", "massive_conflicting", "massive_unmapped", "massive_duplicates",
        "vendor_dropped_non_equity", "refused", "rules_available", "watch_available",
        "assets_available",
    ):  # fmt: skip
        assert hasattr(line, name), name
    assert getattr(line, "articles") == 3
    assert getattr(line, "rules_written") == 1
    assert getattr(line, "rules_conflicting") == 1
    assert getattr(line, "massive_conflicting") == 1
    assert getattr(line, "massive_written") == 0


@pytest.mark.asyncio
async def test_an_empty_store_logs_no_label_line(store: NewsStore, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="corollary.data.news.labelling")
    await store.store([], now=NOW)
    assert not [r for r in caplog.records if getattr(r, "event", None) == "news_labels_written"]


# --------------------------------------------------------------- the name book


@pytest.mark.asyncio
async def test_the_book_is_built_once_while_its_inputs_hold(books: NameBooks) -> None:
    first = await books.current()
    second = await books.current()
    assert first.book is second.book
    assert first.watch_available and first.assets_available


@pytest.mark.asyncio
async def test_a_changed_watch_set_rebuilds_the_book(holder: AssetDirectoryHolder) -> None:
    watched: list[str] = ["AAPL"]

    async def source() -> Iterable[str]:
        return tuple(watched)

    books = NameBooks(watch=source, assets=holder, funds=())
    first = await books.current()
    watched.append("MSFT")
    second = await books.current()
    assert first.book is not second.book


@pytest.mark.asyncio
async def test_an_unavailable_watch_source_labels_with_the_asset_list_and_says_so(
    holder: AssetDirectoryHolder, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken() -> Iterable[str]:
        raise RuntimeError("seed table locked")

    caplog.set_level(logging.WARNING, logger="corollary.data.news.labelling")
    built = await NameBooks(watch=broken, assets=holder, funds=()).current()
    assert built.watch_available is False
    assert built.book.is_equity("AAPL")  # from the asset list
    assert [r for r in caplog.records if getattr(r, "event", None) == "news_label_watch_unavailable"]


@pytest.mark.asyncio
async def test_no_asset_list_labels_with_the_watch_universe_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="corollary.data.news.labelling")
    empty = AssetDirectoryHolder(now=lambda: NOW)
    built = await NameBooks(watch=watch_of("AAPL"), assets=empty, funds=()).current()
    assert built.assets_available is False
    assert built.book.is_equity("AAPL") and not built.book.is_equity("MSFT")
    assert [r for r in caplog.records if getattr(r, "event", None) == "news_label_assets_unavailable"]


# --------------------------------------------------------------- funds come from data


def test_universe_fund_symbols_are_the_fund_flagged_entries() -> None:
    assert set(UNIVERSE_FUND_SYMBOLS) == {e.symbol for e in UNIVERSE if e.fund}
    assert UNIVERSE_FUND_SYMBOLS  # the curated universe flags at least one fund


@pytest.mark.asyncio
async def test_the_book_treats_exactly_the_flagged_entries_as_funds(books: NameBooks) -> None:
    built = await books.current()
    for entry in UNIVERSE:
        assert built.book.is_fund(entry.symbol) is entry.fund, entry.symbol
    assert books.funds == frozenset(UNIVERSE_FUND_SYMBOLS)


@pytest.mark.asyncio
async def test_a_fund_lone_tag_takes_no_single_tag_label(store: NewsStore, sessions: Callable[[], Session]) -> None:
    """SYNTHETIC: a subject-less phrase tagged SPY alone fires, and the fund flag
    keeps it off SPY (AUDIT3-M1). Not an earnings phrase: since 5.2 audit M1 the
    earnings family never takes a lone tag, fund or not, so a macro print would
    make the control below silent for the wrong reason."""
    from corollary.data.news.rules import NameBook, rules_labels

    macro = alpaca("1", "Company Prices $1.5B Common Stock Offering", tickers=("SPY",))
    # Control: the same book with no funds puts the single-tag label on SPY,
    # so the absence below is the fund flag's doing and not a silent headline.
    unflagged = NameBook(watch=("AAPL", "MSFT"), watch_names=WATCH_COMPANY_NAMES,
                         asset_names={a.symbol: a.name for a in DIRECTORY.assets}, funds=())  # fmt: skip
    assert [label.ticker for label in rules_labels(macro, unflagged).labels] == ["SPY"]

    await store.store([macro], now=NOW)
    assert [r for r in rows(sessions) if r.ticker == "SPY"] == []


# --------------------------------------------------------------- the prune, with labels attached


OLD = NOW - timedelta(days=120)


@pytest.mark.asyncio
async def test_the_prune_keeps_labelled_groups_and_deletes_unlabelled_ones(
    store: NewsStore, sessions: Callable[[], Session]
) -> None:
    """Decision 21: delete every old canonical group of which no member carries
    a label; null ``summary`` on every old article, labelled or not."""
    canonical = alpaca("1", "Apple holds its meeting", published_at=OLD, url="https://example.com/shared")
    labelled_copy = NewsArticle(
        vendor="massive",
        vendor_id="m1",
        feed=NewsFeed.MASSIVE_NEWS,
        url="https://example.com/shared",
        headline="Apple holds its meeting",
        summary="Copy summary",
        publisher="Zacks",
        published_at=OLD,
        tickers=("AAPL",),
        insights=(VendorInsight("AAPL", "neutral", "Routine."),),
    )
    unlabelled = alpaca("2", "Microsoft holds its meeting", tickers=("MSFT",), published_at=OLD)
    stored = await store.store([canonical, labelled_copy, unlabelled], now=OLD + timedelta(minutes=1))
    assert stored.duplicates_linked == 1
    ids = {s.article.vendor_id: s.article_id for s in stored.stored}
    labels_before = [as_tuple(r) for r in rows(sessions)]
    assert [t[0] for t in labels_before] == [ids["m1"]]

    result = await prune_news(store, NOW)
    assert not isinstance(result, str)
    with sessions() as session:
        left = {row.id: row for row in session.scalars(select(ArticleRow))}
        assert set(left) == {ids["1"], ids["m1"]}  # the labelled copy keeps its canonical row
        assert all(row.summary is None for row in left.values())
        assert labelled_article_ids(session) == frozenset({ids["m1"]})
    assert [as_tuple(r) for r in rows(sessions)] == labels_before  # labels untouched

    again = await prune_news(store, NOW)
    assert getattr(again, "articles_deleted", None) == 0


def test_labelled_article_ids_reads_distinct_labelled_articles(sessions: Callable[[], Session]) -> None:
    from corollary.data.news.ingest import store_articles
    from corollary.data.news.rules import NameBook

    book = NameBook(watch=("AAPL",), watch_names=WATCH_COMPANY_NAMES, asset_names={}, funds=())
    with sessions() as session:
        assert labelled_article_ids(session) == frozenset()
        result = store_articles(
            session,
            [
                alpaca("1", FIRING_HEADLINE),
                massive("2", VendorInsight("UEC", "negative", "Down."), VendorInsight("CCJ", "negative", "Down.")),
                alpaca("3", "Nothing fires here"),
            ],
            assets=DIRECTORY,
            now=NOW,
        )
        label_stored(session, result.stored, book, assets=DIRECTORY, now=NOW)
        ids = {s.article.vendor_id: s.article_id for s in result.stored}
        assert labelled_article_ids(session) == frozenset({ids["1"], ids["2"]})


# --------------------------------------------------------------- vendor tickers ingest dropped (5.2 audit L5)


@pytest.mark.asyncio
async def test_a_vendor_insight_on_a_non_equity_ticker_writes_no_label_and_is_counted(
    store: NewsStore, sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    """SYNTHETIC: a crypto pair and a ticker too long for the table are not
    active US equities, so ingest drops them as tags -- and the vendor tier must
    not label them either, or ``sentiment_label`` holds pairs with no tag row."""
    long_ticker = "Z" * 33
    caplog.set_level(logging.INFO, logger="corollary.data.news.labelling")
    result = await store.store(
        [
            massive(
                "2",
                VendorInsight("UEC", "negative", "Down."),
                VendorInsight("BTCUSD", "positive", "Crypto up."),
                VendorInsight(long_ticker, "positive", "Too long."),
            )
        ],
        now=NOW,
    )
    (stored,) = result.stored
    assert [(r.ticker, r.source) for r in rows(sessions)] == [("UEC", "massive")]
    with sessions() as session:
        tags = set(session.scalars(select(NewsArticleTicker.ticker)))
    assert tags == {"UEC"}

    (dropped,) = [r for r in caplog.records if getattr(r, "event", None) == "news_label_vendor_dropped"]
    assert dropped.levelno == logging.WARNING
    assert getattr(dropped, "dropped") == 2
    assert getattr(dropped, "insights") == [
        {"article_id": stored.article_id, "ticker": "BTCUSD"},
        {"article_id": stored.article_id, "ticker": long_ticker},
    ]
    (line,) = [r for r in caplog.records if getattr(r, "event", None) == "news_labels_written"]
    assert getattr(line, "vendor_dropped_non_equity") == 2
    assert getattr(line, "massive_written") == 1
    assert getattr(line, "refused") == 0  # dropped before the table's CHECKs are reached


def test_two_insights_that_normalise_to_one_listed_ticker_still_meet_the_conflict_rule(
    sessions: Callable[[], Session],
) -> None:
    """Filtering runs before the vendor labeller, so a respelt duplicate that
    disagrees is a conflict (no label), not a first-come label."""
    from corollary.data.news.ingest import store_articles

    brk = AssetDirectory(assets=(equity("BRK.B", "Berkshire Hathaway Inc. Class B"),))
    article = massive(
        "2", VendorInsight("BRK/B", "positive", "Up."), VendorInsight("BRK.B", "negative", "Down.")
    )
    with sessions() as session:
        result = store_articles(session, [article], assets=brk, now=NOW)
        write = label_stored(session, result.stored, None, assets=brk, now=NOW)
        session.commit()
    assert rows(sessions) == []
    assert write.massive_conflicting == 1 and write.vendor_dropped_non_equity == 0


# --------------------------------------------------------------- a label the table would refuse


def test_a_label_the_table_would_refuse_is_logged_and_the_rest_of_the_batch_is_stored(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    """The refuse-and-log path. Since L5 a Massive ticker over the width is
    dropped as a non-equity first, so this reaches it the one way left: a name
    book whose asset list carries a symbol wider than the column."""
    from corollary.data.news.ingest import store_articles
    from corollary.data.news.rules import NameBook

    wide = "W" * 33
    book = NameBook(
        watch=("AAPL",),
        watch_names=WATCH_COMPANY_NAMES,
        asset_names={"AAPL": "Apple Inc.", wide: "Zyxwvut Widgets Inc."},
        funds=(),
    )
    caplog.set_level(logging.WARNING, logger="corollary.data.news.labelling")
    with sessions() as session:
        result = store_articles(
            session,
            [alpaca("1", "Zyxwvut Widgets Raises Full-Year Guidance", tickers=()), alpaca("2", FIRING_HEADLINE)],
            assets=DIRECTORY,
            now=NOW,
        )
        write = label_stored(session, result.stored, book, assets=DIRECTORY, now=NOW)
        session.commit()
    ids = {s.article.vendor_id: s.article_id for s in result.stored}
    assert write.refused == 1 and write.rules_written == 1
    assert [(r.article_id, r.ticker) for r in rows(sessions)] == [(ids["2"], "AAPL")]
    (refused,) = [r for r in caplog.records if getattr(r, "event", None) == "news_label_refused"]
    assert getattr(refused, "article_id") == ids["1"]
    assert getattr(refused, "ticker") == wide
    assert "longer than 32" in getattr(refused, "rule")
    assert getattr(refused, "at") == NOW.isoformat()


# --------------------------------------------------------------- a failed name-book build (5.2 audit L3)


class _BrokenBooks(NameBooks):
    def _build(self, watched: frozenset[str], directory: AssetDirectory | None) -> Any:
        raise RuntimeError("could not compile the name book " + "x" * 1000)


@pytest.mark.asyncio
async def test_a_failed_book_build_skips_the_rules_tier_and_still_stores_the_news(
    sessions: Callable[[], Session], holder: AssetDirectoryHolder, caplog: pytest.LogCaptureFixture
) -> None:
    books = _BrokenBooks(watch=watch_of("AAPL"), assets=holder, funds=UNIVERSE_FUND_SYMBOLS)
    store = NewsStore(session_factory=sessions, assets=holder, books=books)
    caplog.set_level(logging.INFO)
    result = await store.store(
        [alpaca("1", FIRING_HEADLINE), massive("2", VendorInsight("UEC", "negative", "Down."))], now=NOW
    )
    assert result.inserted == 2
    with sessions() as session:
        assert len(session.scalars(select(ArticleRow)).all()) == 2
    assert [(r.ticker, r.source) for r in rows(sessions)] == [("UEC", "massive")]
    (warning,) = [r for r in caplog.records if getattr(r, "event", None) == "news_label_book_unavailable"]
    assert warning.levelno == logging.WARNING
    assert getattr(warning, "rules_available") is False
    error = getattr(warning, "error")
    assert error.startswith("RuntimeError: could not compile") and len(error) <= 300
    (line,) = [r for r in caplog.records if getattr(r, "event", None) == "news_labels_written"]
    assert getattr(line, "rules_available") is False


@pytest.mark.asyncio
async def test_an_unavailable_watch_source_is_logged_as_bounded_text(
    holder: AssetDirectoryHolder, caplog: pytest.LogCaptureFixture
) -> None:
    """5.2 audit L6: the raw exception text is truncated, as the pollers do it."""

    async def broken() -> Iterable[str]:
        raise RuntimeError("seed table locked " + "y" * 5000)

    caplog.set_level(logging.WARNING, logger="corollary.data.news.labelling")
    await NameBooks(watch=broken, assets=holder, funds=()).current()
    (warning,) = [r for r in caplog.records if getattr(r, "event", None) == "news_label_watch_unavailable"]
    assert getattr(warning, "error").startswith("RuntimeError: seed table locked")
    assert len(getattr(warning, "error")) <= 300
    assert len(warning.getMessage()) <= 400


# --------------------------------------------------------------- the prune, through retention.prune


def test_a_labelled_old_article_survives_retention_prune_with_the_real_predicate(
    sessions: Callable[[], Session],
) -> None:
    """``retention.prune`` given ``labelled_article_ids`` itself, over labels
    ``label_stored`` wrote: the labelled old article stays (its own group), the
    unlabelled one goes, and the survivor's summary is nulled regardless."""
    from corollary.data.news.ingest import store_articles
    from corollary.data.news.retention import prune
    from corollary.data.news.rules import NameBook

    book = NameBook(watch=("AAPL",), watch_names=WATCH_COMPANY_NAMES, asset_names={}, funds=())
    with sessions() as session:
        result = store_articles(
            session,
            [
                alpaca("1", FIRING_HEADLINE, published_at=OLD),
                alpaca("2", "Microsoft holds its meeting", tickers=("MSFT",), published_at=OLD),
            ],
            assets=DIRECTORY,
            now=OLD + timedelta(minutes=1),
        )
        label_stored(session, result.stored, book, assets=DIRECTORY, now=OLD + timedelta(minutes=1))
        session.commit()
    ids = {s.article.vendor_id: s.article_id for s in result.stored}
    labels_before = [as_tuple(r) for r in rows(sessions)]
    assert [t[0] for t in labels_before] == [ids["1"]]

    with sessions() as session:
        pruned = prune(session, now=NOW, labelled_article_ids=labelled_article_ids)
        session.commit()
    assert pruned.articles_deleted == 1
    with sessions() as session:
        left = {row.id: row.summary for row in session.scalars(select(ArticleRow))}
    assert left == {ids["1"]: None}
    assert [as_tuple(r) for r in rows(sessions)] == labels_before
