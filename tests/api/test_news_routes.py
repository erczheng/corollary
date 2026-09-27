"""``GET /api/news`` -- the canonical-row feed (Phase 3 step 4).

What is pinned, from the spec's *API* section and decisions 3, 4 and 21:

* canonical rows only; a duplicate group is shown once, naming the canonical
  row's publisher, once per ticker the group is tagged to;
* step 4 labels nothing: ``unclassified``, no tier, no source, not demoted;
* sector from the SPDR seed, ``Other`` outside it or with no seed (and the
  response says the seed is missing), ``MARKET`` under ``Macro``;
* every lookback's boundary is the start of an ET calendar date, in both
  daylight and standard time -- the same rule ``web/src/lib/news.ts`` uses;
* ``scope=watch`` (the default) is the watch universe plus ``MARKET``;
* filters combine, sort is server-side, pagination is offset/limit <= 200;
* the route depends on no broker and no provider.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from corollary.api import schemas
from corollary.api.deps import (
    broker_for_account,
    fundamentals_data,
    market_data,
    service_registry,
)
from corollary.api.routes.news import request_now
from corollary.api.routes.news import router as news_router
from corollary.data.seeds import SeedError

from .news_support import SEED_AS_OF, add_article, add_manual_watches, make_seed
from .test_schema_contract import ts_interface_fields, ts_union_members, wire_fields

UTC = timezone.utc

#: Saturday 2026-09-26, 11:00 EDT.
NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
#: 00:00 EDT on 2026-09-26.
TODAY_START = datetime(2026, 9, 26, 4, 0, tzinfo=UTC)


def _client(app: FastAPI, *, now: datetime = NOW, seed: Any = "default") -> TestClient:
    chosen = make_seed() if seed == "default" else seed
    app.state.spdr_seed_loader = lambda: chosen
    app.dependency_overrides[request_now] = lambda: now
    return TestClient(app)


@pytest.fixture
def news(app: FastAPI) -> Iterator[TestClient]:
    with _client(app) as client:
        yield client


def _items(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/api/news", params=params)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _pairs(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(item["headline"], item["ticker"]) for item in items]


# --------------------------------------------------------------------------
# Canonical rows, one per ticker
# --------------------------------------------------------------------------


def test_a_duplicate_group_is_served_once_per_ticker_as_its_canonical_row(
    news: TestClient, db_engine: Engine
) -> None:
    canonical = add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="a1",
        headline="Nvidia beats",
        published_at=NOW - timedelta(hours=1),
        tickers=["NVDA"],
        publisher="Benzinga",
    )
    duplicate = add_article(
        db_engine,
        vendor="finnhub",
        vendor_id="f1",
        headline="NVIDIA BEATS (copy)",
        published_at=NOW - timedelta(hours=1, minutes=-2),
        tickers=["NVDA", "TSLA"],
        publisher="Yahoo",
        canonical_id=canonical,
    )

    items = _items(news, scope="all")

    assert sorted(item["id"] for item in items) == [f"{canonical}:NVDA", f"{canonical}:TSLA"]
    assert {item["headline"] for item in items} == {"Nvidia beats"}
    assert {item["publisher"] for item in items} == {"Benzinga"}
    assert not any(item["id"].startswith(f"{duplicate}:") for item in items)


def test_one_article_tagged_to_two_names_is_two_items_with_their_own_sectors(
    news: TestClient, db_engine: Engine
) -> None:
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="a1",
        headline="Chips and banks",
        published_at=NOW - timedelta(hours=1),
        tickers=["NVDA", "JPM"],
    )

    items = _items(news)

    assert {(item["ticker"], item["sector"]) for item in items} == {
        ("NVDA", "Technology"),
        ("JPM", "Financials"),
    }


def test_market_files_under_macro_and_is_dropped_when_the_group_has_a_real_tag(
    news: TestClient, db_engine: Engine
) -> None:
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="m1",
        headline="Fed holds",
        published_at=NOW - timedelta(hours=2),
        tickers=["MARKET"],
    )
    tagged_later = add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="m2",
        headline="Apple story",
        published_at=NOW - timedelta(hours=1),
        tickers=["MARKET"],
    )
    add_article(
        db_engine,
        vendor="massive",
        vendor_id="x2",
        headline="Apple story (massive)",
        published_at=NOW - timedelta(hours=1),
        tickers=["AAPL"],
        canonical_id=tagged_later,
    )

    items = _items(news)

    assert sorted(_pairs(items)) == [("Apple story", "AAPL"), ("Fed holds", "MARKET")]
    macro = next(item for item in items if item["ticker"] == "MARKET")
    assert macro["sector"] == "Macro"


def test_step_four_labels_nothing_and_says_so(news: TestClient, db_engine: Engine) -> None:
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="a1",
        headline="Anything",
        published_at=NOW - timedelta(hours=1),
        tickers=["AAPL", "MARKET"],
    )

    for item in _items(news):
        assert item["sentiment"] == "unclassified"
        assert item["tier"] is None
        assert item["source"] is None
        assert item["demoted"] is False


def test_the_item_carries_url_and_an_aware_utc_time(news: TestClient, db_engine: Engine) -> None:
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="a1",
        headline="Timed",
        published_at=datetime(2026, 9, 26, 13, 30, tzinfo=UTC),
        tickers=["AAPL"],
        url="https://example.com/story",
    )

    (item,) = _items(news)

    assert item["url"] == "https://example.com/story"
    parsed = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == datetime(2026, 9, 26, 13, 30, tzinfo=UTC)


def test_a_missing_publisher_is_null_not_the_vendor(news: TestClient, db_engine: Engine) -> None:
    add_article(
        db_engine,
        vendor="finnhub",
        vendor_id="f1",
        headline="Nameless",
        published_at=NOW - timedelta(hours=1),
        tickers=["AAPL"],
        publisher=None,
    )

    (item,) = _items(news)

    assert item["publisher"] is None


# --------------------------------------------------------------------------
# Sectors and the seed
# --------------------------------------------------------------------------


def test_a_ticker_outside_the_seed_is_other(news: TestClient, db_engine: Engine) -> None:
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="a1",
        headline="Tesla",
        published_at=NOW - timedelta(hours=1),
        tickers=["TSLA"],
    )

    response = news.get("/api/news")

    body = response.json()
    assert [item["sector"] for item in body["items"]] == ["Other"]
    assert body["sectorsAvailable"] is True
    assert body["seedAsOf"] == SEED_AS_OF.isoformat()


def test_with_no_seed_every_ticker_is_other_market_is_macro_and_the_response_says_why(
    app: FastAPI, db_engine: Engine
) -> None:
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="a1",
        headline="Nvidia",
        published_at=NOW - timedelta(hours=1),
        tickers=["NVDA", "MARKET"],
    )
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="a2",
        headline="Macro",
        published_at=NOW - timedelta(hours=1),
        tickers=["MARKET"],
    )

    with _client(app, seed=None) as client:
        body = client.get("/api/news", params={"scope": "all"}).json()

    assert body["sectorsAvailable"] is False
    assert body["seedAsOf"] is None
    assert {(item["ticker"], item["sector"]) for item in body["items"]} == {
        ("NVDA", "Other"),
        ("MARKET", "Macro"),
    }


def test_a_malformed_seed_is_a_503_not_a_half_read_feed(app: FastAPI) -> None:
    def broken() -> None:
        raise SeedError("spdr_holdings.csv, line 3: weight 'x' is not a decimal")

    app.state.spdr_seed_loader = broken
    with TestClient(app) as client:
        response = client.get("/api/news")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "spdr_seed_invalid"


# --------------------------------------------------------------------------
# Lookbacks -- the start of an ET calendar date
# --------------------------------------------------------------------------

#: (lookback, now, the first instant inside the window).
_BOUNDARIES = [
    # EDT, UTC-4.
    ("today", NOW, datetime(2026, 9, 26, 4, 0, tzinfo=UTC)),
    ("3d", NOW, datetime(2026, 9, 24, 4, 0, tzinfo=UTC)),
    ("1w", NOW, datetime(2026, 9, 20, 4, 0, tzinfo=UTC)),
    ("2w", NOW, datetime(2026, 9, 13, 4, 0, tzinfo=UTC)),
    # EST, UTC-5: an hour out if the offset were hardcoded.
    ("today", datetime(2026, 1, 15, 15, 0, tzinfo=UTC), datetime(2026, 1, 15, 5, 0, tzinfo=UTC)),
    ("3d", datetime(2026, 1, 15, 15, 0, tzinfo=UTC), datetime(2026, 1, 13, 5, 0, tzinfo=UTC)),
    # 23:30 EDT is still "today" in New York although it is tomorrow in UTC.
    ("today", datetime(2026, 9, 27, 3, 30, tzinfo=UTC), datetime(2026, 9, 26, 4, 0, tzinfo=UTC)),
]


@pytest.mark.parametrize(("lookback", "now", "start"), _BOUNDARIES)
def test_each_lookback_starts_at_an_et_midnight(
    app: FastAPI, db_engine: Engine, lookback: str, now: datetime, start: datetime
) -> None:
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="inside",
        headline="inside",
        published_at=start,
        tickers=["AAPL"],
    )
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="outside",
        headline="outside",
        published_at=start - timedelta(microseconds=1),
        tickers=["AAPL"],
    )

    with _client(app, now=now) as client:
        body = client.get("/api/news", params={"lookback": lookback}).json()
        everything = client.get("/api/news", params={"lookback": "all"}).json()

    assert [item["headline"] for item in body["items"]] == ["inside"]
    assert datetime.fromisoformat(body["since"].replace("Z", "+00:00")) == start
    assert sorted(item["headline"] for item in everything["items"]) == ["inside", "outside"]
    assert everything["since"] is None


def test_the_default_lookback_is_all(news: TestClient, db_engine: Engine) -> None:
    add_article(
        db_engine,
        vendor="alpaca",
        vendor_id="old",
        headline="old",
        published_at=NOW - timedelta(days=60),
        tickers=["AAPL"],
    )

    body = news.get("/api/news").json()

    assert body["lookback"] == "all"
    assert [item["headline"] for item in body["items"]] == ["old"]


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------


def test_the_default_scope_is_the_watch_universe_plus_market(
    news: TestClient, db_engine: Engine
) -> None:
    for vendor_id, ticker in [
        ("markets", "AAPL"),
        ("leader", "LIN"),
        ("macro", "MARKET"),
        ("off", "ZZZZ"),
    ]:
        add_article(
            db_engine,
            vendor="alpaca",
            vendor_id=vendor_id,
            headline=vendor_id,
            published_at=NOW - timedelta(hours=1),
            tickers=[ticker],
        )

    watch = news.get("/api/news").json()
    everything = news.get("/api/news", params={"scope": "all"}).json()

    assert watch["scope"] == "watch"
    assert sorted(item["ticker"] for item in watch["items"]) == ["AAPL", "LIN", "MARKET"]
    assert sorted(item["ticker"] for item in everything["items"]) == [
        "AAPL",
        "LIN",
        "MARKET",
        "ZZZZ",
    ]


def test_manual_watches_and_position_underlyings_are_in_the_watch_scope(
    news: TestClient, db_engine: Engine
) -> None:
    for ticker in ("QZMA", "QZPO", "QZNO"):
        add_article(
            db_engine,
            vendor="alpaca",
            vendor_id=ticker,
            headline=ticker,
            published_at=NOW - timedelta(hours=1),
            tickers=[ticker],
        )
    add_manual_watches(db_engine, ["QZMA"], at=NOW - timedelta(days=1))
    app: Any = news.app
    app.state.position_underlyings.replace(["QZPO"], at=NOW)

    items = _items(news)

    assert sorted(item["ticker"] for item in items) == ["QZMA", "QZPO"]


# --------------------------------------------------------------------------
# Filters, sort, pagination
# --------------------------------------------------------------------------


@pytest.fixture
def corpus(news: TestClient, db_engine: Engine) -> TestClient:
    rows = [
        ("n1", "Nvidia one", ["NVDA"], "Reuters", 5),
        ("n2", "Bank two", ["JPM"], "Benzinga", 4),
        ("n3", "Fed three", ["MARKET"], "Reuters", 3),
        ("n4", "Tesla four", ["TSLA"], "Benzinga", 2),
        ("n5", "Apple five", ["AAPL"], "Reuters", 1),
    ]
    for vendor_id, headline, tickers, publisher, hours_ago in rows:
        add_article(
            db_engine,
            vendor="alpaca",
            vendor_id=vendor_id,
            headline=headline,
            published_at=NOW - timedelta(hours=hours_ago),
            tickers=tickers,
            publisher=publisher,
        )
    return news


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"ticker": "nvda"}, ["Nvidia one"]),
        ({"ticker": "MARKET"}, ["Fed three"]),
        ({"sector": "Technology"}, ["Nvidia one"]),
        ({"sector": "Macro"}, ["Fed three"]),
        ({"sector": "Other"}, ["Apple five", "Tesla four"]),
        ({"sector": "Utilities"}, []),
        ({"publisher": "Reuters"}, ["Apple five", "Fed three", "Nvidia one"]),
        ({"publisher": "Reuters", "sector": "Macro"}, ["Fed three"]),
        ({"sentiment": "unclassified"}, ["Apple five", "Tesla four", "Fed three", "Bank two", "Nvidia one"]),
        ({"sentiment": "bullish"}, []),
    ],
)
def test_filters_combine(
    corpus: TestClient, params: dict[str, str], expected: list[str]
) -> None:
    got = [item["headline"] for item in _items(corpus, **params)]
    if "sentiment" in params:
        assert got == expected
    else:
        assert sorted(got) == sorted(expected)


def test_sort_newest_is_the_default_and_oldest_reverses_it(corpus: TestClient) -> None:
    newest = [item["headline"] for item in _items(corpus)]
    oldest = [item["headline"] for item in _items(corpus, sort="oldest")]

    assert newest == ["Apple five", "Tesla four", "Fed three", "Bank two", "Nvidia one"]
    assert oldest == list(reversed(newest))


def test_pages_are_offset_and_limit_with_a_real_total(corpus: TestClient) -> None:
    first = corpus.get("/api/news", params={"limit": 2}).json()
    second = corpus.get("/api/news", params={"limit": 2, "offset": 2}).json()
    last = corpus.get("/api/news", params={"limit": 2, "offset": 4}).json()

    assert [item["headline"] for item in first["items"]] == ["Apple five", "Tesla four"]
    assert [item["headline"] for item in second["items"]] == ["Fed three", "Bank two"]
    assert [item["headline"] for item in last["items"]] == ["Nvidia one"]
    assert (first["total"], first["hasMore"]) == (5, True)
    assert (last["total"], last["hasMore"]) == (5, False)
    assert (first["limit"], second["offset"]) == (2, 2)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 201},
        {"limit": 0},
        {"offset": -1},
        {"lookback": "1m"},
        {"scope": "everything"},
        {"sort": "relevance"},
        {"sentiment": "Unclassified"},
    ],
)
def test_out_of_range_parameters_are_a_422(news: TestClient, params: dict[str, Any]) -> None:
    assert news.get("/api/news", params=params).status_code == 422


def test_a_limit_of_200_is_permitted(news: TestClient) -> None:
    assert news.get("/api/news", params={"limit": 200}).status_code == 200


# --------------------------------------------------------------------------
# Structure: no broker, and the frontend's shape
# --------------------------------------------------------------------------

_VENDOR_DEPENDENCIES = {broker_for_account, market_data, fundamentals_data, service_registry}


def _calls(dependant: Any) -> set[Any]:
    found = {dependant.call}
    for child in dependant.dependencies:
        found |= _calls(child)
    return found


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/news", "GET"),
        ("/api/news/watch", "GET"),
        ("/api/news/watch/{ticker}", "POST"),
        ("/api/news/watch/{ticker}", "DELETE"),
    ],
)
def test_no_news_route_depends_on_a_broker_or_a_provider(path: str, method: str) -> None:
    # Read off the router: this FastAPI mounts an included router as one
    # opaque entry in ``app.routes``. That the app serves these paths at all
    # is every other test in this file and ``test_watch_routes.py``.
    routes = [
        route
        for route in news_router.routes
        if getattr(route, "path", None) == path and method in getattr(route, "methods", ())
    ]
    assert len(routes) == 1, path
    dependant: Any = getattr(routes[0], "dependant")
    assert not (_calls(dependant) & _VENDOR_DEPENDENCIES)


def test_the_item_serves_every_field_of_the_frontends_news_item() -> None:
    # The frontend now reads url, source and demoted too, so the two field
    # sets are the same set: any drift either way is a contract break.
    assert ts_interface_fields("NewsItem") == wire_fields(schemas.NewsItem)


def test_the_sentiment_and_tier_unions_match_types_ts() -> None:
    import typing

    assert ts_union_members("Sentiment") == set(typing.get_args(schemas.NewsSentiment))
    assert ts_union_members("SentimentTier") == set(typing.get_args(schemas.NewsSentimentTier))
