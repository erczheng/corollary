"""The article store: normalise, dedupe, upsert (Phase 3 decision 3, decision 21).

Inputs are **SYNTHETIC** unless a test says otherwise: built here, with
example.com-style URLs and invented headlines. Two tests replay recorded
vendor bodies through the provider's own row decoder --
``tests/fixtures/alpaca/p4_news_untickered_page2.json`` -- and say so.

The database is a file-backed SQLite under ``tmp_path`` built from the ORM
metadata, following ``tests/db/conftest.py``: ``create_db_engine`` sets
``PRAGMA foreign_keys=ON``, which the canonical self-FK depends on.
"""

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.data.news.article import NewsArticle, NewsFeed
from corollary.data.news.ingest import (
    MARKET_TICKER,
    URL_MATCH_WINDOW,
    IngestResult,
    filter_tags,
    headline_key,
    store_articles,
    url_key,
)
from corollary.data.providers.interface import AssetDirectory, EquityAsset
from corollary.db.models import Base
from corollary.db.models import NewsArticle as ArticleRow
from corollary.db.models import NewsArticleTicker
from corollary.db.session import create_db_engine, sqlite_url

NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        yield sess


def equity(symbol: str) -> EquityAsset:
    return EquityAsset(
        symbol=symbol, name=f"{symbol} Inc", tradable=True, has_options=True, exchange="NASDAQ"
    )


ASSETS = AssetDirectory(
    assets=tuple(equity(s) for s in ("AAPL", "NVDA", "TSLA", "BRK.B", "SPY", "TXCA"))
)

_VENDOR_FEED = {
    "alpaca": NewsFeed.ALPACA_NEWS,
    "finnhub": NewsFeed.FINNHUB_COMPANY,
    "massive": NewsFeed.MASSIVE_NEWS,
}


def article(vendor: str = "alpaca", vendor_id: str = "1", **overrides: Any) -> NewsArticle:
    """A SYNTHETIC article."""
    fields: dict[str, Any] = {
        "vendor": vendor,
        "vendor_id": vendor_id,
        "feed": _VENDOR_FEED[vendor],
        "url": f"https://example.com/story/{vendor}-{vendor_id}",
        "headline": f"Headline {vendor} {vendor_id}",
        "summary": "A summary",
        "publisher": "Benzinga",
        "published_at": T0,
        "tickers": ("NVDA",),
    }
    fields.update(overrides)
    return NewsArticle(**fields)


def store(session: Session, *articles: NewsArticle, assets: AssetDirectory | None = ASSETS) -> IngestResult:
    result = store_articles(session, articles, assets=assets, now=NOW)
    session.commit()
    return result


def row(session: Session, vendor: str, vendor_id: str) -> ArticleRow:
    found = session.scalars(
        select(ArticleRow).where(ArticleRow.vendor == vendor, ArticleRow.vendor_id == vendor_id)
    ).one()
    return found


def tickers(session: Session, article_id: int) -> set[str]:
    return set(
        session.scalars(
            select(NewsArticleTicker.ticker).where(NewsArticleTicker.article_id == article_id)
        )
    )


def all_rows(session: Session) -> list[ArticleRow]:
    return list(session.scalars(select(ArticleRow).order_by(ArticleRow.id)))


# ---------------------------------------------------------------- url_key


def test_url_key_lowercases_scheme_and_host_but_not_the_path() -> None:
    assert url_key("HTTPS://Example.COM/News/Story-A") == "https://example.com/News/Story-A"


def test_url_key_strips_www() -> None:
    assert url_key("https://www.example.com/a") == "https://example.com/a"


def test_url_key_folds_http_into_https() -> None:
    assert url_key("http://example.com/a") == url_key("https://example.com/a")


def test_url_key_drops_the_fragment() -> None:
    assert url_key("https://example.com/a#comments") == "https://example.com/a"


def test_url_key_drops_tracking_params_and_keeps_meaningful_ones() -> None:
    raw = "https://example.com/a?utm_source=x&UTM_Medium=y&id=42&fbclid=z&gclid=q&page=2"
    assert url_key(raw) == "https://example.com/a?id=42&page=2"


def test_url_key_keeps_the_query_that_identifies_a_finnhub_story() -> None:
    """Finnhub's company-news URLs are ``/api/news?id=<hash>`` -- the id is the story."""
    a = url_key("https://finnhub.io/api/news?id=74e88763")
    b = url_key("https://finnhub.io/api/news?id=9a3b7a2b")
    assert a != b
    assert a == "https://finnhub.io/api/news?id=74e88763"


def test_url_key_sorts_query_params() -> None:
    assert url_key("https://example.com/a?b=2&a=1") == url_key("https://example.com/a?a=1&b=2")


def test_url_key_drops_a_trailing_slash() -> None:
    assert url_key("https://example.com/a/") == "https://example.com/a"
    assert url_key("https://example.com/") == "https://example.com"


def test_url_key_drops_the_default_port_and_surrounding_whitespace() -> None:
    assert url_key("  https://example.com:443/a  ") == "https://example.com/a"
    assert url_key("https://example.com:8443/a") == "https://example.com:8443/a"


def test_url_key_of_an_unparseable_url_is_the_trimmed_text() -> None:
    assert url_key("  not a url  ") == "not a url"


def test_url_key_is_idempotent() -> None:
    once = url_key("HTTP://WWW.Example.com/a/?utm_source=x&b=2#f")
    assert url_key(once) == once


# ---------------------------------------------------------------- headline_key


def test_headline_key_casefolds_and_collapses_whitespace() -> None:
    assert headline_key("  NVIDIA   Beats\tEstimates \n") == "nvidia beats estimates"


def test_headline_key_strips_straight_and_curly_quotes() -> None:
    straight = headline_key("Apple's \"Big\" Day")
    curly = headline_key("Apple’s “Big” Day")
    assert straight == curly == "apple s big day"


def test_headline_key_strips_punctuation_so_retitled_separators_match() -> None:
    assert headline_key("Tesla Q3: Deliveries Rise!") == headline_key("Tesla Q3 - Deliveries Rise")
    assert headline_key("Tesla Q3: Deliveries Rise!") == "tesla q3 deliveries rise"


def test_headline_key_keeps_symbols_and_digits() -> None:
    assert headline_key("Stock Up 5% To $120") == "stock up 5 to $120"


def test_headline_key_of_pure_punctuation_is_empty() -> None:
    assert headline_key("!!! --- ...") == ""


# ---------------------------------------------------------------- tag filtering


def test_filter_tags_keeps_active_equities_and_drops_the_rest() -> None:
    kept, dropped = filter_tags(("NVDA", "BTCUSD", "BTC/USD", "ZZZZ"), ASSETS)
    assert kept == ("NVDA",)
    assert dropped == ("BTCUSD", "BTC/USD", "ZZZZ")


def test_filter_tags_writes_a_class_share_with_a_dot() -> None:
    kept, dropped = filter_tags(("BRK/B", "BRK-B"), ASSETS)
    assert kept == ("BRK.B",)
    assert dropped == ()


def test_filter_tags_never_keeps_a_vendor_market_tag() -> None:
    """``MARKET`` is the store's word for *no tag*, not a symbol a vendor may claim."""
    kept, dropped = filter_tags(("MARKET", "NVDA"), None)
    assert kept == ("NVDA",)
    assert dropped == ("MARKET",)


def test_filter_tags_without_a_directory_keeps_symbol_shaped_tags() -> None:
    """Degraded: the daily asset fetch failed. Dropping every tag would be worse."""
    kept, dropped = filter_tags(("NVDA", "BRK/B", "BTC/USD", "ETH-USD", "ZZZZ"), None)
    assert kept == ("NVDA", "BRK.B", "ZZZZ")
    assert dropped == ("BTC/USD", "ETH-USD")


# ---------------------------------------------------------------- upsert


def test_a_new_article_is_inserted_with_its_keys_and_ingest_time(session: Session) -> None:
    result = store(session, article(url="https://www.example.com/a/?utm_source=x", headline="Big  News!"))
    assert result.inserted == 1
    stored = row(session, "alpaca", "1")
    assert stored.url == "https://www.example.com/a/?utm_source=x"
    assert stored.url_key == url_key(stored.url)
    assert stored.headline_key == headline_key(stored.headline) == "big news"
    assert stored.canonical_id is None
    assert stored.feed == "alpaca_news"
    assert stored.ingested_at == NOW
    assert stored.published_at == T0
    assert tickers(session, stored.id) == {"NVDA"}


def test_a_naive_now_is_refused(session: Session) -> None:
    with pytest.raises(ValueError, match="now"):
        store_articles(session, [article()], assets=ASSETS, now=datetime(2026, 9, 24, 16, 0))


def test_repolling_is_idempotent(session: Session) -> None:
    first = store(session, article())
    second = store(session, article())
    assert first.inserted == 1
    assert second.inserted == 0
    assert second.updated == 0
    assert second.unchanged == 1
    assert second.tags_added == 0
    assert len(all_rows(session)) == 1
    stored = row(session, "alpaca", "1")
    assert stored.ingested_at == NOW
    assert tickers(session, stored.id) == {"NVDA"}


def test_a_reseen_article_gains_new_tags_rather_than_being_skipped(session: Session) -> None:
    """Finnhub /company-news returns one id and URL under NVDA and again under TSLA."""
    store(session, article("finnhub", "142", tickers=("NVDA",)))
    result = store(session, article("finnhub", "142", tickers=("TSLA",)))
    assert result.inserted == 0
    assert result.tags_added == 1
    assert tickers(session, row(session, "finnhub", "142").id) == {"NVDA", "TSLA"}


def test_both_copies_in_one_batch_merge_into_one_row(session: Session) -> None:
    result = store(
        session,
        article("finnhub", "142", tickers=("NVDA",)),
        article("finnhub", "142", tickers=("TSLA",)),
    )
    assert result.inserted == 1
    assert len(all_rows(session)) == 1
    assert tickers(session, row(session, "finnhub", "142").id) == {"NVDA", "TSLA"}


def test_a_market_article_that_gains_a_real_tag_is_no_longer_market(session: Session) -> None:
    """Seen first on the market-wide feed untagged, then under a symbol's own poll."""
    store(session, article("finnhub", "142", feed=NewsFeed.FINNHUB_MARKET, tickers=()))
    stored = row(session, "finnhub", "142")
    assert tickers(session, stored.id) == {MARKET_TICKER}
    store(session, article("finnhub", "142", tickers=("NVDA",)))
    assert tickers(session, stored.id) == {"NVDA"}
    # Provenance is the first fetch: the row keeps the feed it arrived on.
    session.refresh(stored)
    assert stored.feed == "finnhub_market"


def test_a_reseen_copy_with_fewer_tags_removes_none(session: Session) -> None:
    store(session, article(tickers=("NVDA", "TSLA")))
    store(session, article(tickers=()))
    assert tickers(session, row(session, "alpaca", "1").id) == {"NVDA", "TSLA"}


def test_an_edited_article_updates_its_headline_and_key(session: Session) -> None:
    store(session, article(headline="Old Headline", summary="old"))
    result = store(session, article(headline="New Headline!", summary="new"))
    assert result.updated == 1
    stored = row(session, "alpaca", "1")
    assert stored.headline == "New Headline!"
    assert stored.headline_key == "new headline"
    assert stored.summary == "new"
    assert stored.ingested_at == NOW


def test_an_edit_that_drops_the_summary_keeps_the_stored_one(session: Session) -> None:
    store(session, article(summary="kept"))
    result = store(session, article(summary=None))
    assert result.updated == 0
    assert row(session, "alpaca", "1").summary == "kept"


# ---------------------------------------------------------------- tags at ingest


def test_crypto_tags_are_dropped_and_an_article_left_with_none_is_market(session: Session) -> None:
    result = store(session, article(tickers=("BTCUSD", "ETH/USD")))
    assert result.tags_dropped == 2
    assert tickers(session, row(session, "alpaca", "1").id) == {MARKET_TICKER}


def test_mixed_tags_keep_only_the_equity(session: Session) -> None:
    result = store(session, article(tickers=("BTCUSD", "NVDA")))
    assert result.tags_dropped == 1
    assert tickers(session, row(session, "alpaca", "1").id) == {"NVDA"}


def test_an_untagged_article_is_market(session: Session) -> None:
    store(session, article(tickers=()))
    assert tickers(session, row(session, "alpaca", "1").id) == {MARKET_TICKER}


def test_a_class_share_is_stored_with_its_dot(session: Session) -> None:
    store(session, article(tickers=("BRK/B",)))
    assert tickers(session, row(session, "alpaca", "1").id) == {"BRK.B"}


def test_without_an_asset_directory_filtering_degrades_and_says_so(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="corollary.data.news.ingest"):
        result = store(session, article(tickers=("NVDA", "BTC/USD", "QQQQ")), assets=None)
    assert result.degraded_tag_filter is True
    assert result.tags_dropped == 1
    assert tickers(session, row(session, "alpaca", "1").id) == {"NVDA", "QQQQ"}
    assert any("degraded" in record.getMessage() for record in caplog.records)


def test_with_a_directory_filtering_is_not_degraded(session: Session) -> None:
    assert store(session, article()).degraded_tag_filter is False


# ---------------------------------------------------------------- cross-vendor dedupe


def test_two_vendors_copies_with_one_url_collapse(session: Session) -> None:
    """Same URL modulo tracking params, ``www.`` and a fragment -- published hours apart."""
    store(session, article("alpaca", "1", url="https://www.example.com/s?utm_source=bz#top"))
    result = store(
        session,
        article(
            "massive",
            "m-1",
            url="https://example.com/s",
            headline="An entirely different title",
            published_at=T0 + timedelta(hours=3),
        ),
    )
    assert result.duplicates_linked == 1
    canonical = row(session, "alpaca", "1")
    duplicate = row(session, "massive", "m-1")
    assert canonical.canonical_id is None
    assert duplicate.canonical_id == canonical.id


def test_two_vendors_copies_with_one_headline_five_minutes_apart_collapse(session: Session) -> None:
    store(session, article("alpaca", "1", headline="NVIDIA Beats Estimates"))
    store(
        session,
        article(
            "finnhub",
            "f-1",
            url="https://finnhub.io/api/news?id=abc",
            headline="Nvidia beats estimates.",
            published_at=T0 + timedelta(minutes=5),
        ),
    )
    assert row(session, "finnhub", "f-1").canonical_id == row(session, "alpaca", "1").id


def test_the_headline_window_is_inclusive_at_ten_minutes(session: Session) -> None:
    store(session, article("alpaca", "1", headline="Same Words"))
    store(
        session,
        article("massive", "m-1", headline="Same Words", published_at=T0 - timedelta(minutes=10)),
    )
    assert row(session, "massive", "m-1").canonical_id == row(session, "alpaca", "1").id


def test_the_headline_window_is_inclusive_at_ten_minutes_the_other_way(session: Session) -> None:
    """The stored row is the *earlier* one here: exercises the window's lower bound."""
    store(session, article("alpaca", "1", headline="Same Words"))
    store(
        session,
        article("massive", "m-1", headline="Same Words", published_at=T0 + timedelta(minutes=10)),
    )
    assert row(session, "massive", "m-1").canonical_id == row(session, "alpaca", "1").id


def test_the_same_headline_just_outside_ten_minutes_is_a_different_story(session: Session) -> None:
    store(session, article("alpaca", "1", headline="Same Words"))
    store(
        session,
        article(
            "massive",
            "m-1",
            headline="Same Words",
            published_at=T0 + timedelta(minutes=10, seconds=1),
        ),
    )
    assert row(session, "massive", "m-1").canonical_id is None


def test_two_stories_with_one_headline_an_hour_apart_do_not_collapse(session: Session) -> None:
    store(session, article("alpaca", "1", headline="Stocks Open Higher"))
    result = store(
        session,
        article("finnhub", "f-1", headline="Stocks Open Higher", published_at=T0 + timedelta(hours=1)),
    )
    assert result.duplicates_linked == 0
    assert row(session, "finnhub", "f-1").canonical_id is None


def test_a_third_copy_links_to_the_canonical_never_to_a_duplicate(session: Session) -> None:
    """A matches B by URL; C matches only B (by headline). C must point at A, not B."""
    store(session, article("alpaca", "1", url="https://example.com/x", headline="Alpha Title"))
    store(
        session,
        article(
            "massive",
            "m-1",
            url="https://example.com/x",
            headline="Beta Title",
            published_at=T0 + timedelta(hours=2),
        ),
    )
    store(
        session,
        article(
            "finnhub",
            "f-1",
            url="https://finnhub.io/api/news?id=c",
            headline="Beta Title",
            published_at=T0 + timedelta(hours=2, minutes=3),
        ),
    )
    a = row(session, "alpaca", "1")
    assert row(session, "massive", "m-1").canonical_id == a.id
    assert row(session, "finnhub", "f-1").canonical_id == a.id
    # No row anywhere names a non-canonical row.
    ids_with_parent = {r.id for r in all_rows(session) if r.canonical_id is not None}
    assert all(r.canonical_id not in ids_with_parent for r in all_rows(session))


def test_among_several_canonical_matches_the_oldest_wins_then_the_lowest_id(session: Session) -> None:
    """Two unrelated canonical groups both match; the choice must not depend on luck."""
    # Two alpaca stories: same headline, 8 minutes apart -- same vendor, so both
    # canonical. Stored in separate polls, the later one first, so it holds the
    # lower id: "oldest" and "lowest id" point at different rows.
    store(session, article("alpaca", "late", headline="Twin", published_at=T0 + timedelta(minutes=4)))
    store(session, article("alpaca", "early", headline="Twin", published_at=T0 - timedelta(minutes=4)))
    assert row(session, "alpaca", "late").id < row(session, "alpaca", "early").id
    assert row(session, "alpaca", "late").canonical_id is None
    assert row(session, "alpaca", "early").canonical_id is None
    store(session, article("massive", "m-1", headline="Twin", published_at=T0))
    assert row(session, "massive", "m-1").canonical_id == row(session, "alpaca", "early").id


def test_ties_on_published_at_go_to_the_lowest_id(session: Session) -> None:
    store(session, article("alpaca", "a", headline="Twin"))
    store(session, article("alpaca", "b", headline="Twin"))
    first = row(session, "alpaca", "a")
    second = row(session, "alpaca", "b")
    assert first.id < second.id
    store(session, article("massive", "m-1", headline="Twin"))
    assert row(session, "massive", "m-1").canonical_id == first.id


def test_a_batch_stores_identically_whatever_order_it_arrives_in(engine: Engine, tmp_path: Path) -> None:
    batch = [
        article("massive", "m-1", url="https://example.com/x", published_at=T0 + timedelta(minutes=1)),
        article("alpaca", "1", url="https://example.com/x"),
        article("finnhub", "f-1", url="https://example.com/x", published_at=T0 + timedelta(minutes=2)),
    ]

    def snapshot(order: list[NewsArticle], name: str) -> list[tuple[str, str, str | None]]:
        eng = create_db_engine(sqlite_url(tmp_path / f"{name}.db"))
        Base.metadata.create_all(eng)
        with Session(eng) as sess:
            store_articles(sess, order, assets=ASSETS, now=NOW)
            sess.commit()
            by_id = {r.id: r for r in all_rows(sess)}
            out = sorted(
                (
                    r.vendor,
                    r.vendor_id,
                    None if r.canonical_id is None else by_id[r.canonical_id].vendor_id,
                )
                for r in by_id.values()
            )
        eng.dispose()
        return out

    forward = snapshot(batch, "forward")
    backward = snapshot(list(reversed(batch)), "backward")
    assert forward == backward
    assert ("finnhub", "f-1", "1") in forward
    assert ("massive", "m-1", "1") in forward


def test_one_vendors_two_ids_are_two_stories_even_on_one_url(session: Session) -> None:
    """A vendor that assigns two ids is asserting two stories (see the recorded case below)."""
    store(
        session,
        article("alpaca", "1", url="https://www.benzinga.com/quote/SPY", headline="PMI A"),
        article("alpaca", "2", url="https://www.benzinga.com/quote/SPY", headline="PMI B"),
    )
    assert row(session, "alpaca", "1").canonical_id is None
    assert row(session, "alpaca", "2").canonical_id is None


def test_a_group_that_already_holds_this_vendor_is_not_joined(session: Session) -> None:
    """alpaca/1 + massive/m-1 are one group. alpaca/2 matches massive/m-1's headline,
    but joining would put two alpaca ids -- two stories -- in one group."""
    store(session, article("alpaca", "1", url="https://example.com/x", headline="Alpha"))
    store(session, article("massive", "m-1", url="https://example.com/x", headline="Beta"))
    store(session, article("alpaca", "2", url="https://example.com/y", headline="Beta"))
    assert row(session, "massive", "m-1").canonical_id == row(session, "alpaca", "1").id
    assert row(session, "alpaca", "2").canonical_id is None


def test_an_empty_headline_key_never_matches(session: Session) -> None:
    store(session, article("alpaca", "1", headline="!!!"))
    store(session, article("massive", "m-1", headline="???"))
    assert row(session, "massive", "m-1").canonical_id is None


def test_a_reseen_article_keeps_its_canonical_link(session: Session) -> None:
    """Links are decided once, at insert; an edit never re-points a group."""
    store(session, article("alpaca", "1", url="https://example.com/x"))
    store(session, article("massive", "m-1", url="https://example.com/x"))
    store(session, article("massive", "m-1", url="https://example.com/moved", headline="Edited"))
    assert row(session, "massive", "m-1").canonical_id == row(session, "alpaca", "1").id


# ---------------------------------------------------------------- when a URL identifies a story
#
# Decision 3 links by "normalised URL". A URL identifies a story only when it
# is specific (not a bare host, not a /quote/<SYMBOL> page), not already
# shared by two groups or by two ids of one vendor, and within
# URL_MATCH_WINDOW. Otherwise the headline check decides -- a missed link,
# never a wrong one.

GENERIC = "https://example.com/markets/briefs"


def test_the_url_match_window_is_48_hours() -> None:
    assert URL_MATCH_WINDOW == timedelta(hours=48)


@pytest.mark.parametrize("url", ["https://www.benzinga.com/quote/SPY", GENERIC])
@pytest.mark.parametrize("polls", ["one_batch", "one_poll_each"])
def test_the_auditors_generic_url_scenario_links_by_headline_or_not_at_all(
    session: Session, url: str, polls: str
) -> None:
    """Two alpaca stories on one generic URL two days apart; massive serves a copy of
    the second a minute later, and an unrelated story on the same URL 60 days on.
    M2 must link to A2 (its headline), never to A1; M9 must link to nothing."""
    articles = [
        article("alpaca", "A1", url=url, headline="ISM Manufacturing PMI Falls"),
        article(
            "alpaca",
            "A2",
            url=url,
            headline="ISM Services PMI Rises",
            published_at=T0 + timedelta(days=2),
        ),
        article(
            "massive",
            "M2",
            url=url,
            headline="ISM Services PMI Rises",
            published_at=T0 + timedelta(days=2, minutes=1),
        ),
        article(
            "massive",
            "M9",
            url=url,
            headline="Fed Holds Rates Steady",
            published_at=T0 + timedelta(days=60),
        ),
    ]
    if polls == "one_batch":
        store(session, *articles)
    else:
        for one in articles:
            store(session, one)
    assert row(session, "alpaca", "A1").canonical_id is None
    assert row(session, "alpaca", "A2").canonical_id is None
    assert row(session, "massive", "M2").canonical_id == row(session, "alpaca", "A2").id
    assert row(session, "massive", "M9").canonical_id is None


def test_a_url_one_vendor_holds_two_ids_on_does_not_identify_a_story(session: Session) -> None:
    """Without this rule M1 would join A1 by URL: both inside 48h, A1 the oldest."""
    store(
        session,
        article("alpaca", "A1", url=GENERIC, headline="ISM Manufacturing PMI Falls"),
        article(
            "alpaca",
            "A2",
            url=GENERIC,
            headline="ISM Services PMI Rises",
            published_at=T0 + timedelta(hours=1),
        ),
    )
    store(
        session,
        article(
            "massive",
            "M1",
            url=GENERIC,
            headline="Fed Holds Rates Steady",
            published_at=T0 + timedelta(hours=2),
        ),
    )
    assert row(session, "massive", "M1").canonical_id is None


def test_a_url_already_spanning_two_groups_does_not_identify_a_story(session: Session) -> None:
    """A1 and M1 share a URL three days apart, so they are two groups. F1 is inside
    48h of M1 only; without this rule it would join M1 on the URL alone."""
    store(session, article("alpaca", "A1", url=GENERIC, headline="Story One"))
    store(
        session,
        article(
            "massive",
            "M1",
            url=GENERIC,
            headline="Story Two",
            published_at=T0 + timedelta(days=3),
        ),
    )
    assert row(session, "massive", "M1").canonical_id is None
    store(
        session,
        article(
            "finnhub",
            "F1",
            url=GENERIC,
            headline="Story Three",
            published_at=T0 + timedelta(days=3, hours=1),
        ),
    )
    assert row(session, "finnhub", "F1").canonical_id is None


def test_two_ids_of_one_vendor_inside_one_group_still_make_a_url_ambiguous(
    session: Session,
) -> None:
    """Defence in depth. The store never puts one vendor's two ids in one group, so
    in normal operation the two-groups rule already covers this. Built directly
    here -- a group that breaks that invariant (a hand repair, an older writer) --
    the URL must still not be trusted: one group, but alpaca holds two ids on it."""
    store(session, article("alpaca", "A1", url=GENERIC, headline="Story One"))
    a1 = row(session, "alpaca", "A1")
    session.add(
        ArticleRow(
            vendor="alpaca",
            vendor_id="A2",
            feed=NewsFeed.ALPACA_NEWS.value,
            canonical_id=a1.id,
            url=GENERIC,
            url_key=url_key(GENERIC),
            headline="Story Two",
            headline_key=headline_key("Story Two"),
            summary=None,
            publisher="Benzinga",
            published_at=T0 + timedelta(hours=1),
            ingested_at=NOW,
        )
    )
    session.commit()
    store(
        session,
        article(
            "massive",
            "M1",
            url=GENERIC,
            headline="Story Three",
            published_at=T0 + timedelta(hours=2),
        ),
    )
    assert row(session, "massive", "M1").canonical_id is None


def test_an_ambiguous_url_still_links_by_headline(session: Session) -> None:
    store(
        session,
        article("alpaca", "A1", url=GENERIC, headline="Story One"),
        article(
            "alpaca", "A2", url=GENERIC, headline="Story Two", published_at=T0 + timedelta(hours=1)
        ),
    )
    store(
        session,
        article(
            "massive",
            "M1",
            url=GENERIC,
            headline="Story Two",
            published_at=T0 + timedelta(hours=1, minutes=5),
        ),
    )
    assert row(session, "massive", "M1").canonical_id == row(session, "alpaca", "A2").id


def test_a_genuine_cross_vendor_copy_a_few_hours_apart_still_links(session: Session) -> None:
    """A specific URL, one id per vendor, one group: three vendors' copies are one story."""
    url = "https://www.cnbc.com/2026/09/24/nvidia-earnings.html"
    store(session, article("alpaca", "A1", url=url, headline="Nvidia Beats"))
    store(
        session,
        article(
            "massive",
            "M1",
            url=url,
            headline="NVDA tops estimates",
            published_at=T0 + timedelta(hours=4),
        ),
    )
    store(
        session,
        article(
            "finnhub",
            "F1",
            url=f"{url}?utm_source=finnhub",
            headline="Nvidia results beat",
            published_at=T0 + timedelta(hours=6),
        ),
    )
    a1 = row(session, "alpaca", "A1")
    assert row(session, "massive", "M1").canonical_id == a1.id
    assert row(session, "finnhub", "F1").canonical_id == a1.id


@pytest.mark.parametrize("offset", [URL_MATCH_WINDOW, -URL_MATCH_WINDOW], ids=["later", "earlier"])
def test_the_url_window_is_inclusive_at_48_hours(session: Session, offset: timedelta) -> None:
    store(session, article("alpaca", "A1", url="https://example.com/story/x", headline="One Title"))
    store(
        session,
        article(
            "massive",
            "M1",
            url="https://example.com/story/x",
            headline="Another Title",
            published_at=T0 + offset,
        ),
    )
    assert row(session, "massive", "M1").canonical_id == row(session, "alpaca", "A1").id


@pytest.mark.parametrize(
    "offset",
    [URL_MATCH_WINDOW + timedelta(seconds=1), -URL_MATCH_WINDOW - timedelta(seconds=1)],
    ids=["later", "earlier"],
)
def test_the_same_url_just_outside_48_hours_is_a_different_story(
    session: Session, offset: timedelta
) -> None:
    store(session, article("alpaca", "A1", url="https://example.com/story/x", headline="One Title"))
    store(
        session,
        article(
            "massive",
            "M1",
            url="https://example.com/story/x",
            headline="Another Title",
            published_at=T0 + offset,
        ),
    )
    assert row(session, "massive", "M1").canonical_id is None


_GENERIC_URLS = [
    "https://www.benzinga.com/quote/SPY",
    "https://www.benzinga.com/quote/brk.b/",
    "https://stocktwits.com",
    "https://www.example.com/",
    "http://example.com/#top",
    "https://example.com/?utm_source=x",
]


@pytest.mark.parametrize("url", _GENERIC_URLS)
def test_a_generic_url_shape_never_links_on_its_own(session: Session, url: str) -> None:
    """One story each, five minutes apart: the only thing they share is a generic URL."""
    store(session, article("alpaca", "A1", url=url, headline="ISM Manufacturing PMI Falls"))
    store(
        session,
        article(
            "massive",
            "M1",
            url=url,
            headline="Fed Holds Rates Steady",
            published_at=T0 + timedelta(minutes=5),
        ),
    )
    assert row(session, "massive", "M1").canonical_id is None


@pytest.mark.parametrize("url", _GENERIC_URLS)
def test_a_generic_url_falls_through_to_the_headline(session: Session, url: str) -> None:
    store(session, article("alpaca", "A1", url=url, headline="ISM Services PMI Rises"))
    store(
        session,
        article(
            "massive",
            "M1",
            url=url,
            headline="ISM Services PMI Rises",
            published_at=T0 + timedelta(minutes=5),
        ),
    )
    assert row(session, "massive", "M1").canonical_id == row(session, "alpaca", "A1").id


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/?p=123",
        "https://www.benzinga.com/quote/SPY/news/some-story",
        "https://finnhub.io/api/news?id=abc",
    ],
)
def test_a_specific_url_near_a_generic_shape_still_links(session: Session, url: str) -> None:
    store(session, article("alpaca", "A1", url=url, headline="One Title"))
    store(
        session,
        article(
            "massive",
            "M1",
            url=url,
            headline="Another Title",
            published_at=T0 + timedelta(hours=1),
        ),
    )
    assert row(session, "massive", "M1").canonical_id == row(session, "alpaca", "A1").id


# ---------------------------------------------------------------- recorded bodies


def _recorded_alpaca_page2() -> list[NewsArticle]:
    """RECORDED: tests/fixtures/alpaca/p4_news_untickered_page2.json, via the provider decoder."""
    from corollary.data.providers.alpaca import _news_row

    body = json.loads(
        (FIXTURES / "alpaca" / "p4_news_untickered_page2.json").read_text(encoding="utf-8")
    )["body"]
    decoded = [_news_row(item, lambda text: text) for item in body["news"]]
    return [pair[0] for pair in decoded if pair is not None]


def test_recorded_benzinga_items_sharing_a_quote_page_url_stay_separate(session: Session) -> None:
    """RECORDED. Benzinga files its Manufacturing and Services PMI briefs under one URL,
    ``https://www.benzinga.com/quote/SPY``. They are different stories; linking them
    would hide one from the feed."""
    articles = _recorded_alpaca_page2()
    on_spy_page = [a for a in articles if url_key(a.url) == "https://benzinga.com/quote/SPY"]
    assert len({a.headline for a in on_spy_page}) >= 2
    store(session, *articles)
    spy_rows = [r for r in all_rows(session) if r.url_key == "https://benzinga.com/quote/SPY"]
    assert len(spy_rows) == len(on_spy_page)
    assert all(r.canonical_id is None for r in spy_rows)


def test_recorded_page_repolls_idempotently(session: Session) -> None:
    """RECORDED. A second poll of the same page writes nothing."""
    articles = _recorded_alpaca_page2()
    first = store(session, *articles)
    second = store(session, *articles)
    assert first.inserted == len(articles)
    assert second.inserted == 0
    assert second.updated == 0
    assert second.tags_added == 0
    assert second.unchanged == len(articles)
    for stored in all_rows(session):
        assert tickers(session, stored.id)  # every article carries a tag or MARKET
