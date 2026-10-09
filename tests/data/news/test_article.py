"""The vendor-neutral article record: validation and normalisation.

Every input here is **synthetic**, built in the test. The recorded vendor
bodies are exercised by the provider tests under ``tests/data/providers/``.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from corollary.data.news.article import FEED_VENDOR, NewsArticle, NewsFeed, VendorInsight

MOMENT = datetime(2026, 9, 24, 12, 55, tzinfo=timezone.utc)


def article(**overrides: Any) -> NewsArticle:
    fields: dict[str, Any] = {
        "vendor": "finnhub",
        "vendor_id": "142384570",
        "feed": NewsFeed.FINNHUB_COMPANY,
        "url": "https://example.com/a",
        "headline": "A headline",
        "summary": "A summary",
        "publisher": "Yahoo",
        "published_at": MOMENT,
        "tickers": ("NVDA",),
    }
    fields.update(overrides)
    return NewsArticle(**fields)


def test_the_feed_values_are_exactly_the_check_constrained_set() -> None:
    """These four strings become a DB CHECK constraint; a fifth is a migration."""
    assert {feed.value for feed in NewsFeed} == {
        "alpaca_news",
        "finnhub_company",
        "finnhub_market",
        "massive_news",
    }
    assert set(FEED_VENDOR) == set(NewsFeed)


def test_a_vendor_that_does_not_own_the_feed_is_refused() -> None:
    with pytest.raises(ValueError, match="belongs to vendor"):
        article(vendor="massive")


def test_a_feed_given_as_its_string_value_is_coerced_to_the_enum() -> None:
    assert article(feed="finnhub_market").feed is NewsFeed.FINNHUB_MARKET


@pytest.mark.parametrize("field", ["vendor_id", "url", "headline"])
def test_a_blank_identity_field_is_refused(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        article(**{field: "   "})


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        article(published_at=datetime(2026, 9, 24, 12, 55))


def test_an_offset_timestamp_is_normalised_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))
    moment = article(published_at=datetime(2026, 9, 24, 8, 55, tzinfo=eastern))
    assert moment.published_at == MOMENT
    assert moment.published_at.tzinfo is timezone.utc


def test_tickers_are_uppercased_deduplicated_and_ordered_as_the_vendor_sent() -> None:
    tagged = article(tickers=(" nvda", "AMD", "NVDA", "", "X:BTCUSD"))
    assert tagged.tickers == ("NVDA", "AMD", "X:BTCUSD")


def test_no_tickers_means_a_market_item() -> None:
    assert article(tickers=()).is_market
    assert not article().is_market


def test_blank_summary_and_publisher_become_none() -> None:
    blank = article(summary="  ", publisher="")
    assert blank.summary is None
    assert blank.publisher is None


def test_an_insight_keeps_the_vendor_word_and_normalises_case() -> None:
    insight = VendorInsight(ticker=" meta ", sentiment="Mixed", reasoning="  ")
    assert insight == VendorInsight(ticker="META", sentiment="mixed", reasoning=None)


def test_an_insight_without_a_ticker_or_a_sentiment_is_refused() -> None:
    with pytest.raises(ValueError):
        VendorInsight(ticker=" ", sentiment="positive", reasoning=None)
    with pytest.raises(ValueError):
        VendorInsight(ticker="META", sentiment="", reasoning=None)


def test_the_record_is_frozen() -> None:
    record = article()
    with pytest.raises(AttributeError):
        record.headline = "changed"  # type: ignore[misc]
