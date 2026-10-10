"""Massive's per-ticker insights become one vendor label per ticker.

Phase 3 design, *Testing*: *"``vendor.py``: one Massive article tagging
several tickers yields one label per ticker with its own direction; an
article with no insight for a ticker yields no row for it."* And the Massive
constraint: ``mixed`` *"is neither directional value. The mapping has to say
so explicitly"* -- tested here on a **real** recorded insight,
``tests/fixtures/massive/p5_reference_news_mixed_insight.json``, recorded by
``tests/fixtures/record_massive.py mixed``.

Every test marked SYNTHETIC builds its article by hand; the rest decode a
recorded response through :class:`MassiveProvider`, exactly as the poller
does, with no live call.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from corollary.data.news.article import NewsArticle, NewsFeed, VendorInsight
from corollary.data.news.labels import (
    Direction,
    LabelSource,
    SentimentLabel,
    SentimentTier,
)
from corollary.data.news.vendor import (
    MASSIVE_SENTIMENT_DIRECTION,
    vendor_labels,
)
from corollary.data.providers.massive import MassiveCredentials, MassiveProvider
from corollary.ratelimit import HostRateLimiter

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "massive"
MIXED_FIXTURE = "p5_reference_news_mixed_insight"
UNTICKERED_FIXTURE = "p3_reference_news_untickered"
#: Obviously fake. Rule 6: no key material in tests or fixtures.
TEST_CREDENTIALS = MassiveCredentials(token="not-a-real-massive-key-000000000000")
SINCE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def fixture_body(name: str) -> dict[str, Any]:
    envelope: dict[str, Any] = json.loads(
        (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    )
    assert envelope["status_code"] == 200
    body: dict[str, Any] = envelope["body"]
    return body


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(f"a replay test waited {seconds}s on the rate limiter")


async def decode(name: str) -> tuple[NewsArticle, ...]:
    """The fixture's body through the real provider, ``next_url`` dropped."""
    body = fixture_body(name)
    body.pop("next_url", None)
    text = json.dumps(body)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text, headers={"content-type": "application/json"})

    provider = MassiveProvider(
        credentials=TEST_CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        limiter=HostRateLimiter(
            requests_per_minute=10_000, per_host={}, clock=lambda: 0.0, sleep=_never_sleep
        ),
    )
    async with provider:
        result = await provider.news_since(SINCE)
    return result.articles


def article(*insights: VendorInsight, tickers: tuple[str, ...] = ("AAA", "BBB")) -> NewsArticle:
    """SYNTHETIC: a Massive article carrying the given insights."""
    return NewsArticle(
        vendor="massive",
        vendor_id="synthetic-1",
        feed=NewsFeed.MASSIVE_NEWS,
        url="https://example.invalid/a",
        headline="A synthetic headline",
        summary=None,
        publisher="Synthetic",
        published_at=datetime(2026, 9, 24, 12, 55, tzinfo=timezone.utc),
        tickers=tickers,
        insights=insights,
    )


def by_ticker(labels: tuple[SentimentLabel, ...]) -> dict[str, SentimentLabel]:
    keyed = {label.ticker: label for label in labels}
    assert len(keyed) == len(labels), "two labels for one ticker break UNIQUE(article, ticker, source)"
    return keyed


# ------------------------------------------------------------- the mapping


def test_the_mapping_names_all_four_massive_values_and_nothing_else() -> None:
    assert MASSIVE_SENTIMENT_DIRECTION == {
        "positive": Direction.BULLISH,
        "negative": Direction.BEARISH,
        "neutral": Direction.NEUTRAL,
        "mixed": Direction.NEUTRAL,
    }


@pytest.mark.asyncio
async def test_a_real_mixed_insight_is_a_neutral_label_never_a_direction() -> None:
    (recorded,) = await decode(MIXED_FIXTURE)
    raw = {i.ticker: i.sentiment for i in recorded.insights}
    assert raw["UUUU"] == "mixed", "the fixture must carry Massive's real mixed value"

    result = vendor_labels(recorded)
    uuuu = by_ticker(result.labels)["UUUU"]
    assert uuuu.direction is Direction.NEUTRAL
    assert uuuu.direction not in (Direction.BULLISH, Direction.BEARISH)
    # Massive's own words survive verbatim -- the only place "mixed" is still legible.
    assert uuuu.reasoning is not None and uuuu.reasoning.startswith("Q2 revenues surged 496%")
    assert result.unmapped == ()


@pytest.mark.asyncio
async def test_one_real_article_tagging_four_tickers_gives_each_its_own_direction() -> None:
    (recorded,) = await decode(MIXED_FIXTURE)
    labels = by_ticker(vendor_labels(recorded).labels)
    assert {t: l.direction for t, l in labels.items()} == {
        "UEC": Direction.BEARISH,
        "CCJ": Direction.BEARISH,
        "UUUU": Direction.NEUTRAL,  # mixed
        "URG": Direction.NEUTRAL,
    }
    for label in labels.values():
        assert label.source is LabelSource.MASSIVE
        assert label.tier is SentimentTier.VENDOR
        assert label.rule_id is None
        assert label.reasoning


@pytest.mark.asyncio
async def test_every_recorded_insight_yields_exactly_one_label_with_its_own_direction() -> None:
    articles = await decode(UNTICKERED_FIXTURE)
    assert articles
    expected = {"positive": Direction.BULLISH, "negative": Direction.BEARISH, "neutral": Direction.NEUTRAL}
    multi = 0
    for recorded in articles:
        labels = by_ticker(vendor_labels(recorded).labels)
        assert set(labels) == {i.ticker for i in recorded.insights}
        for insight in recorded.insights:
            assert labels[insight.ticker].direction is expected[insight.sentiment]
            assert labels[insight.ticker].reasoning == insight.reasoning
        if len({l.direction for l in labels.values()}) > 1:
            multi += 1
    assert multi > 0, "the recorded page should hold an article whose tickers disagree"


def test_a_tagged_ticker_without_an_insight_gets_no_label() -> None:
    """SYNTHETIC: every recorded Massive article so far scores every ticker it
    tags (all five fixtures were checked when this was written), so the case
    the spec names has to be built by hand."""
    scored = VendorInsight(ticker="AAA", sentiment="positive", reasoning="r")
    result = vendor_labels(article(scored, tickers=("AAA", "BBB")))
    assert [l.ticker for l in result.labels] == ["AAA"]
    assert "BBB" not in {l.ticker for l in result.labels}


# ------------------------------------------------------------- SYNTHETIC


def test_an_article_with_no_insights_yields_no_labels() -> None:
    """SYNTHETIC."""
    result = vendor_labels(article())
    assert result.labels == ()
    assert result.unmapped == ()


def test_an_unknown_sentiment_is_never_guessed_and_is_reported() -> None:
    """SYNTHETIC: a fifth value is a vendor shape change -- no label, surfaced."""
    odd = VendorInsight(ticker="AAA", sentiment="very_positive", reasoning="?")
    fine = VendorInsight(ticker="BBB", sentiment="positive", reasoning="up")
    result = vendor_labels(article(odd, fine))
    assert [l.ticker for l in result.labels] == ["BBB"]
    assert result.unmapped == (odd,)


def test_a_repeated_insight_that_agrees_is_one_label_the_first() -> None:
    """SYNTHETIC: UNIQUE (article_id, ticker, source) allows one row."""
    first = VendorInsight(ticker="AAA", sentiment="positive", reasoning="first")
    again = VendorInsight(ticker="AAA", sentiment="positive", reasoning="second")
    result = vendor_labels(article(first, again))
    assert result.labels == (
        SentimentLabel(
            ticker="AAA",
            source=LabelSource.MASSIVE,
            tier=SentimentTier.VENDOR,
            direction=Direction.BULLISH,
            reasoning="first",
            rule_id=None,
        ),
    )
    assert result.conflicting == ()
    # Collapsed, not dropped: the second insight is returned for the caller to log.
    assert result.duplicates == (again,)
    assert result.unmapped == ()


def test_an_unknown_sentiment_beside_a_known_one_labels_that_ticker_with_nothing() -> None:
    """SYNTHETIC: {AAA: positive} and {AAA: <unknown>} is not a bullish AAA.

    The unknown value could be the vendor's word for the opposite direction,
    so the ticker is conflicting -- silence beats a wrong label -- and the
    unknown insight is still reported as unmapped. The order does not matter.
    """
    known = VendorInsight(ticker="AAA", sentiment="positive", reasoning="up")
    odd = VendorInsight(ticker="AAA", sentiment="very_negative", reasoning="?")
    other = VendorInsight(ticker="BBB", sentiment="negative", reasoning="down")
    for insights in ((known, odd, other), (odd, known, other)):
        result = vendor_labels(article(*insights))
        assert [l.ticker for l in result.labels] == ["BBB"]
        assert result.conflicting == ("AAA",)
        assert result.unmapped == (odd,)
        assert result.duplicates == ()


def test_two_unknown_sentiments_on_one_ticker_are_unmapped_not_conflicting() -> None:
    """SYNTHETIC: nothing known to disagree with -- reported as unmapped only."""
    one = VendorInsight(ticker="AAA", sentiment="strange", reasoning=None)
    two = VendorInsight(ticker="AAA", sentiment="stranger", reasoning=None)
    result = vendor_labels(article(one, two))
    assert result.labels == ()
    assert result.unmapped == (one, two)
    assert result.conflicting == ()
    assert result.duplicates == ()


def test_a_repeated_insight_that_disagrees_labels_that_ticker_with_nothing() -> None:
    """SYNTHETIC: silence beats a wrong label (PRD §9) -- and the conflict is reported."""
    up = VendorInsight(ticker="AAA", sentiment="positive", reasoning="up")
    down = VendorInsight(ticker="AAA", sentiment="negative", reasoning="down")
    other = VendorInsight(ticker="BBB", sentiment="neutral", reasoning=None)
    result = vendor_labels(article(up, down, other))
    assert [l.ticker for l in result.labels] == ["BBB"]
    assert result.labels[0].reasoning is None
    assert result.conflicting == ("AAA",)
    assert result.duplicates == ()


def test_an_insight_naming_an_untagged_ticker_still_labels_it() -> None:
    """SYNTHETIC: decision 21 -- each insight names its own ticker; no attribution rule."""
    insight = VendorInsight(ticker="ZZZ", sentiment="negative", reasoning="r")
    result = vendor_labels(article(insight, tickers=("AAA",)))
    assert [(l.ticker, l.direction) for l in result.labels] == [("ZZZ", Direction.BEARISH)]


def test_the_same_article_gives_the_same_labels_in_the_same_order() -> None:
    """SYNTHETIC: pure and deterministic -- vendor order kept."""
    insights = (
        VendorInsight(ticker="BBB", sentiment="negative", reasoning="b"),
        VendorInsight(ticker="AAA", sentiment="mixed", reasoning="a"),
    )
    once = vendor_labels(article(*insights))
    assert once == vendor_labels(article(*insights))
    assert [l.ticker for l in once.labels] == ["BBB", "AAA"]


# ------------------------------------------------------------- the record


def test_source_tier_cannot_be_edited_at_runtime() -> None:
    """A mutable table would let any caller re-pair a source with another tier."""
    from corollary.data.news.labels import SOURCE_TIER

    assert dict(SOURCE_TIER) == {
        LabelSource.RULES: SentimentTier.RULES,
        LabelSource.MASSIVE: SentimentTier.VENDOR,
    }
    with pytest.raises(TypeError):
        SOURCE_TIER[LabelSource.MASSIVE] = SentimentTier.RULES  # type: ignore[index]


def test_a_label_refuses_a_source_and_tier_that_do_not_pair() -> None:
    with pytest.raises(ValueError, match="tier vendor"):
        SentimentLabel(
            ticker="AAA",
            source=LabelSource.MASSIVE,
            tier=SentimentTier.RULES,
            direction=Direction.BULLISH,
            reasoning=None,
            rule_id="beat",
        )


def test_a_rules_label_must_name_its_rule_and_a_vendor_label_must_not() -> None:
    with pytest.raises(ValueError, match="must name the rule"):
        SentimentLabel(
            ticker="AAA",
            source=LabelSource.RULES,
            tier=SentimentTier.RULES,
            direction=Direction.BULLISH,
            reasoning=None,
            rule_id=None,
        )
    with pytest.raises(ValueError, match="no rule_id"):
        SentimentLabel(
            ticker="AAA",
            source=LabelSource.MASSIVE,
            tier=SentimentTier.VENDOR,
            direction=Direction.BULLISH,
            reasoning=None,
            rule_id="beat",
        )
