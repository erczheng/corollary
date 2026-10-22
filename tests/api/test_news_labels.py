"""``GET /api/news`` labels (Phase 3 step 5): decision 4's display precedence, Q22's status.

* the displayed label is **rules, then vendor**, for that ticker;
* ``tier``, ``source`` and ``sourceStatus`` are null **exactly** when the
  item is ``unclassified`` (decision 17);
* a Massive ``neutral`` is a label and displays ``neutral``, not unclassified;
* ``sourceStatus`` is ``unaudited`` for both sources (Q22), never ``demoted:
  false``;
* the non-displayed label is served as ``otherLabel`` when both labelled;
* labels on any copy of a duplicate group reach the canonical item;
* the ``sentiment`` filter works on the **displayed** label.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from corollary.api.routes.news import request_now

from .news_support import add_article, add_label, make_seed

UTC = timezone.utc
NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)


@pytest.fixture
def news(app: FastAPI) -> Iterator[TestClient]:
    seed = make_seed()
    app.state.spdr_seed_loader = lambda: seed
    app.dependency_overrides[request_now] = lambda: NOW
    with TestClient(app) as client:
        yield client


def _items(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/api/news", params={"scope": "all", **params})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _item(client: TestClient, ticker: str, **params: Any) -> dict[str, Any]:
    (item,) = [i for i in _items(client, **params) if i["ticker"] == ticker]
    return item


def _article(engine: Engine, vendor_id: str, tickers: list[str], **kwargs: Any) -> int:
    defaults: dict[str, Any] = {
        "vendor": "alpaca",
        "headline": f"headline {vendor_id}",
        "published_at": NOW - timedelta(hours=1),
    }
    defaults.update(kwargs)
    return add_article(engine, vendor_id=vendor_id, tickers=tickers, **defaults)


def test_a_rules_label_displays_with_its_tier_source_and_unaudited_status(
    news: TestClient, db_engine: Engine
) -> None:
    article = _article(db_engine, "a1", ["ACME"])
    add_label(
        db_engine, article_id=article, ticker="ACME", source="rules",
        direction="bullish", rule_id="earnings_beat", reasoning="beats estimates",
    )

    item = _item(news, "ACME")

    assert (item["sentiment"], item["tier"], item["source"], item["sourceStatus"]) == (
        "bullish", "rules", "rules", "unaudited",
    )
    assert item["otherLabel"] is None
    assert "demoted" not in item


def test_a_massive_label_alone_displays_as_the_vendor_tier(
    news: TestClient, db_engine: Engine
) -> None:
    article = _article(db_engine, "m1", ["ACME"], vendor="massive")
    add_label(
        db_engine, article_id=article, ticker="ACME", source="massive",
        direction="bearish", reasoning="weak guidance",
    )

    item = _item(news, "ACME")

    assert (item["sentiment"], item["tier"], item["source"], item["sourceStatus"]) == (
        "bearish", "vendor", "massive", "unaudited",
    )


def test_rules_outrank_the_vendor_and_the_vendor_label_is_served_beside_it(
    news: TestClient, db_engine: Engine
) -> None:
    article = _article(db_engine, "m1", ["ACME"], vendor="massive")
    add_label(db_engine, article_id=article, ticker="ACME", source="massive",
              direction="bullish", reasoning="strong demand")
    add_label(db_engine, article_id=article, ticker="ACME", source="rules",
              direction="bearish", rule_id="guidance_cut", reasoning="cuts guidance")

    item = _item(news, "ACME")

    assert (item["sentiment"], item["tier"], item["source"]) == ("bearish", "rules", "rules")
    assert item["otherLabel"] == {
        "sentiment": "bullish",
        "tier": "vendor",
        "source": "massive",
        "sourceStatus": "unaudited",
        "ruleId": None,
        "reasoning": "strong demand",
    }


def test_a_massive_neutral_is_a_label_not_unclassified(
    news: TestClient, db_engine: Engine
) -> None:
    article = _article(db_engine, "m1", ["ACME"], vendor="massive")
    add_label(db_engine, article_id=article, ticker="ACME", source="massive", direction="neutral")

    item = _item(news, "ACME")

    assert (item["sentiment"], item["tier"]) == ("neutral", "vendor")


def test_a_label_is_per_ticker(news: TestClient, db_engine: Engine) -> None:
    """One article tagged to two names, labelled for one: the other is unclassified."""
    article = _article(db_engine, "a1", ["ACME", "ZETA"])
    add_label(db_engine, article_id=article, ticker="ACME", source="rules",
              direction="bullish", rule_id="earnings_beat")

    assert _item(news, "ACME")["sentiment"] == "bullish"
    zeta = _item(news, "ZETA")
    assert (zeta["sentiment"], zeta["tier"], zeta["source"], zeta["sourceStatus"]) == (
        "unclassified", None, None, None,
    )


def test_a_label_on_a_duplicate_copy_reaches_the_canonical_item(
    news: TestClient, db_engine: Engine
) -> None:
    canonical = _article(db_engine, "a1", ["ACME"])
    duplicate = _article(db_engine, "m1", ["ACME"], vendor="massive", canonical_id=canonical)
    add_label(db_engine, article_id=duplicate, ticker="ACME", source="massive",
              direction="bullish")

    items = [i for i in _items(news) if i["ticker"] == "ACME"]

    assert [i["id"] for i in items] == [f"{canonical}:ACME"]
    assert items[0]["sentiment"] == "bullish"


def test_two_copies_labelled_by_one_source_display_the_canonical_rows_label(
    news: TestClient, db_engine: Engine
) -> None:
    canonical = _article(db_engine, "a1", ["ACME"])
    duplicate = _article(db_engine, "f1", ["ACME"], vendor="finnhub", canonical_id=canonical)
    add_label(db_engine, article_id=duplicate, ticker="ACME", source="rules",
              direction="bearish", rule_id="earnings_miss")
    add_label(db_engine, article_id=canonical, ticker="ACME", source="rules",
              direction="bullish", rule_id="earnings_beat")

    assert _item(news, "ACME")["sentiment"] == "bullish"


@pytest.mark.parametrize(
    ("sentiment", "expected"),
    [
        ("bearish", ["RULE"]),  # rules bearish over a Massive bullish
        ("bullish", ["VEND"]),  # Massive alone; RULE's Massive bullish is not displayed
        ("neutral", ["NEUT"]),
        ("unclassified", ["NONE"]),
    ],
)
def test_the_sentiment_filter_works_on_the_displayed_label(
    news: TestClient, db_engine: Engine, sentiment: str, expected: list[str]
) -> None:
    rule = _article(db_engine, "a1", ["RULE"], vendor="massive")
    add_label(db_engine, article_id=rule, ticker="RULE", source="rules",
              direction="bearish", rule_id="guidance_cut")
    add_label(db_engine, article_id=rule, ticker="RULE", source="massive", direction="bullish")
    vend = _article(db_engine, "m2", ["VEND"], vendor="massive")
    add_label(db_engine, article_id=vend, ticker="VEND", source="massive", direction="bullish")
    neut = _article(db_engine, "m3", ["NEUT"], vendor="massive")
    add_label(db_engine, article_id=neut, ticker="NEUT", source="massive", direction="neutral")
    _article(db_engine, "a4", ["NONE"])

    response = news.get("/api/news", params={"scope": "all", "sentiment": sentiment})

    assert response.status_code == 200, response.text
    body = response.json()
    assert [i["ticker"] for i in body["items"]] == expected
    assert body["total"] == len(expected)
    assert {i["sentiment"] for i in body["items"]} == {sentiment}


# --------------------------------------------------------------------------
# Label-only tickers: a rules label on a company the headline names, untagged
# --------------------------------------------------------------------------


def _acme_on_a_market_story(engine: Engine) -> int:
    """The audit's probe: a Finnhub market story tagged only MARKET, labelled ACME."""
    article = _article(
        engine, "f1", ["MARKET"], vendor="finnhub",
        headline="Acme Widgets Raises Full-Year Guidance",
    )
    add_label(engine, article_id=article, ticker="ACME", source="rules",
              direction="bullish", rule_id="guidance_raised", reasoning="raises full-year guidance")
    return article


def test_a_label_only_ticker_gets_its_own_item_attributed_by_the_headline(
    news: TestClient, db_engine: Engine
) -> None:
    article = _acme_on_a_market_story(db_engine)

    items = _items(news)

    assert [(i["id"], i["ticker"]) for i in items] == [(f"{article}:ACME", "ACME"), (f"{article}:MARKET", "MARKET")]
    acme, market = items
    assert acme["attributedBy"] == "headline"
    assert (acme["sentiment"], acme["tier"], acme["source"]) == ("bullish", "rules", "rules")
    # The MARKET row stays exactly as it was: tag-attributed and unlabelled.
    assert market["attributedBy"] == "tag"
    assert market["sentiment"] == "unclassified"


def test_a_tagged_item_is_attributed_by_its_tag_even_when_also_labelled(
    news: TestClient, db_engine: Engine
) -> None:
    article = _article(db_engine, "a1", ["ACME"])
    add_label(db_engine, article_id=article, ticker="ACME", source="rules",
              direction="bullish", rule_id="earnings_beat")

    (item,) = _items(news)

    assert item["attributedBy"] == "tag"


def test_a_tag_on_any_copy_makes_the_item_tag_attributed(
    news: TestClient, db_engine: Engine
) -> None:
    """Labelled on the canonical copy, tagged only on the duplicate: still a tag."""
    canonical = _article(db_engine, "a1", ["MARKET"])
    duplicate = _article(db_engine, "f1", ["ACME"], vendor="finnhub", canonical_id=canonical)
    add_label(db_engine, article_id=canonical, ticker="ACME", source="rules",
              direction="bullish", rule_id="earnings_beat")
    assert duplicate != canonical

    (item,) = _items(news)

    assert (item["ticker"], item["attributedBy"], item["sentiment"]) == ("ACME", "tag", "bullish")


def test_the_ticker_filter_finds_a_label_only_ticker(news: TestClient, db_engine: Engine) -> None:
    article = _acme_on_a_market_story(db_engine)

    response = news.get("/api/news", params={"scope": "all", "ticker": "acme"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert [i["id"] for i in body["items"]] == [f"{article}:ACME"]
    assert body["total"] == 1


def test_the_sentiment_filter_and_total_count_a_label_only_ticker(
    news: TestClient, db_engine: Engine
) -> None:
    _acme_on_a_market_story(db_engine)

    bullish = news.get("/api/news", params={"scope": "all", "sentiment": "bullish"}).json()
    unclassified = news.get("/api/news", params={"scope": "all", "sentiment": "unclassified"}).json()
    everything = news.get("/api/news", params={"scope": "all"}).json()

    assert [i["ticker"] for i in bullish["items"]] == ["ACME"] and bullish["total"] == 1
    assert [i["ticker"] for i in unclassified["items"]] == ["MARKET"]
    assert everything["total"] == 2


def test_a_label_only_ticker_respects_the_watch_scope(news: TestClient, db_engine: Engine) -> None:
    """ACME is off-watch: the default scope shows the MARKET row only."""
    _acme_on_a_market_story(db_engine)

    body = news.get("/api/news").json()

    assert [i["ticker"] for i in body["items"]] == ["MARKET"]
    assert body["total"] == 1
