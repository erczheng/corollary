"""``GET /api/news/movers`` -- decision 21's discovery candidates, served ranked.

The candidate rules themselves are ``tests/data/news/test_discovery.py``'s.
Pinned here: what the route reads (stored labels mapped to canonical
articles, the watch universe at read time, the *latest* cached verdict on or
before today's ET session date -- never a vendor), the response shape (last
close as an exact decimal string), and the two empty states the spec
separates: *"no off-watch-list name passed the filter since HH:MM"* versus
*"discovery feeds stale since HH:MM"*. The no-broker guard is
``test_news_routes.py``'s, marked ``risk``.
"""

from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from corollary.api.deps import scheduler_status
from corollary.api.routes.news import (
    DISCOVERY_FEED_JOBS,
    STALE_AFTER_SLOTS,
    TRADEABILITY_JOB,
    request_now,
)
from corollary.engine.scheduler import (
    ContextServices,
    EveryInterval,
    JobStatus,
    TwoRate,
    context_jobs,
)

from .news_support import add_article, add_label, add_manual_watches, add_verdict, make_seed

UTC = timezone.utc
#: Saturday 2026-09-26, 11:00 EDT.
NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
TODAY = date(2026, 9, 26)
#: 00:00 EDT on 2026-09-26.
TODAY_START = datetime(2026, 9, 26, 4, 0, tzinfo=UTC)


#: Slots on a fixed UTC grid every five minutes: 15:00 UTC is one.
FIVE_MINUTES = EveryInterval(timedelta(minutes=5))


def job(name: str, **fields: Any) -> JobStatus:
    base: dict[str, Any] = {
        "name": name,
        "rule": "test",
        "schedule": "test",
        "cadence": FIVE_MINUTES,
        "next_run": NOW + timedelta(minutes=1),
        "last_started": None,
        "last_success": NOW - timedelta(minutes=1),
        "last_failure": None,
        "last_error_type": None,
        "runs": 1,
        "failures": 0,
    }
    base.update(fields)
    return JobStatus(**base)


def healthy() -> dict[str, JobStatus]:
    names = [*DISCOVERY_FEED_JOBS.values(), TRADEABILITY_JOB]
    return {name: job(name) for name in names}


def _client(app: FastAPI, status: Mapping[str, JobStatus] | None) -> TestClient:
    seed = make_seed()
    app.state.spdr_seed_loader = lambda: seed
    app.dependency_overrides[request_now] = lambda: NOW
    app.dependency_overrides[scheduler_status] = lambda: status
    return TestClient(app)


@pytest.fixture
def movers(app: FastAPI) -> Iterator[TestClient]:
    with _client(app, healthy()) as client:
        yield client


def _body(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/api/news/movers", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _signal(
    engine: Engine,
    vendor_id: str,
    ticker: str,
    *,
    source: str = "rules",
    direction: str = "bullish",
    rule_id: str = "earnings_beat",
    at: datetime = NOW - timedelta(hours=1),
    vendor: str = "alpaca",
    canonical_id: int | None = None,
    reasoning: str | None = None,
) -> int:
    article = add_article(
        engine,
        vendor=vendor,
        vendor_id=vendor_id,
        headline=f"{ticker} headline {vendor_id}",
        published_at=at,
        tickers=[ticker],
        canonical_id=canonical_id,
    )
    add_label(
        engine, article_id=article, ticker=ticker, source=source, direction=direction,
        rule_id=rule_id, reasoning=reasoning,
    )
    return article


# --------------------------------------------------------------------------
# What is served, and in what order
# --------------------------------------------------------------------------


def test_a_mover_carries_its_reasons_articles_adv_close_and_session_date(
    movers: TestClient, db_engine: Engine
) -> None:
    article = _signal(db_engine, "a1", "ACME", reasoning="beats estimates")
    add_verdict(db_engine, ticker="ACME", session_date=TODAY, last_close=Decimal("5.00"),
                avg_volume_20d=1_000_000, sessions_available=7)

    (item,) = _body(movers)["items"]

    assert item["ticker"] == "ACME"
    assert item["sector"] == "Other"
    assert item["rankGroup"] == "rules"
    assert item["directions"] == ["bullish"]
    assert item["reasons"] == [
        {
            "source": "rules",
            "tier": "rules",
            "ruleId": "earnings_beat",
            "family": "earnings",
            "direction": "bullish",
            "articleIds": [article],
            "evidence": [
                {"articleId": article, "time": "2026-09-26T14:00:00Z", "reasoning": "beats estimates"}
            ],
            "latestAt": "2026-09-26T14:00:00Z",
        }
    ]
    assert [a["id"] for a in item["articles"]] == [article]
    assert item["articles"][0]["url"].startswith("https://")
    assert item["articleCount"] == 1
    assert item["avgVolume"] == 1_000_000
    assert item["advSessions"] == 7
    assert item["lastClose"] == "5.00"
    assert isinstance(item["lastClose"], str)
    assert item["tradeabilitySessionDate"] == "2026-09-26"


def test_movers_are_ranked_rules_first_then_newest(movers: TestClient, db_engine: Engine) -> None:
    _signal(db_engine, "a1", "OLDR", at=NOW - timedelta(hours=9))
    _signal(db_engine, "m1", "NEWM", source="massive", vendor="massive", at=NOW - timedelta(minutes=5))
    _signal(db_engine, "a2", "NEWR", at=NOW - timedelta(hours=2))
    for ticker in ("OLDR", "NEWM", "NEWR"):
        add_verdict(db_engine, ticker=ticker, session_date=TODAY)

    body = _body(movers)

    assert [i["ticker"] for i in body["items"]] == ["NEWR", "OLDR", "NEWM"]
    assert [i["rankGroup"] for i in body["items"]] == ["rules", "rules", "massive_only"]


def test_conflicting_directions_are_served_side_by_side(
    movers: TestClient, db_engine: Engine
) -> None:
    _signal(db_engine, "a1", "ACME", direction="bearish", rule_id="analyst_downgrade")
    _signal(db_engine, "m1", "ACME", source="massive", vendor="massive", direction="bullish")
    add_verdict(db_engine, ticker="ACME", session_date=TODAY)

    (item,) = _body(movers)["items"]

    assert item["directions"] == ["bearish", "bullish"]
    assert [(r["source"], r["direction"]) for r in item["reasons"]] == [
        ("rules", "bearish"),
        ("massive", "bullish"),
    ]


def test_two_copies_of_one_story_are_one_article(movers: TestClient, db_engine: Engine) -> None:
    canonical = _signal(db_engine, "a1", "ACME")
    _signal(db_engine, "f1", "ACME", vendor="finnhub", canonical_id=canonical)
    add_verdict(db_engine, ticker="ACME", session_date=TODAY)

    (item,) = _body(movers)["items"]

    assert [a["id"] for a in item["articles"]] == [canonical]
    assert item["reasons"][0]["articleIds"] == [canonical]


def test_two_copies_labelled_by_one_source_show_the_label_the_feed_shows(
    movers: TestClient, db_engine: Engine
) -> None:
    """One selector: the canonical row's label, never the shorter reasoning text."""
    canonical = _signal(db_engine, "a1", "ACME", reasoning="beats consensus estimates handily")
    _signal(db_engine, "f1", "ACME", vendor="finnhub", canonical_id=canonical, reasoning="beats")
    add_verdict(db_engine, ticker="ACME", session_date=TODAY)

    (item,) = _body(movers)["items"]

    (evidence,) = item["reasons"][0]["evidence"]
    assert evidence["reasoning"] == "beats consensus estimates handily"


def test_a_label_on_a_member_copy_whose_canonical_is_neutral_is_not_a_signal(
    movers: TestClient, db_engine: Engine
) -> None:
    """The pick runs before the direction filter, as the feed's does."""
    canonical = _signal(db_engine, "m1", "ACME", source="massive", direction="neutral",
                        vendor="massive")
    _signal(db_engine, "a1", "ACME", source="massive", direction="bullish",
            canonical_id=canonical)
    add_verdict(db_engine, ticker="ACME", session_date=TODAY)

    body = _body(movers)

    assert body["items"] == [] and body["signalled"] == 0


def test_a_watched_ticker_is_excluded_and_leaves_the_moment_it_is_watched(
    movers: TestClient, db_engine: Engine
) -> None:
    _signal(db_engine, "a1", "ACME")
    _signal(db_engine, "a2", "NVDA")  # a Markets name: always watched
    add_verdict(db_engine, ticker="ACME", session_date=TODAY)

    before = _body(movers)
    add_manual_watches(db_engine, ["ACME"], at=NOW - timedelta(minutes=1))
    after = _body(movers)

    assert [i["ticker"] for i in before["items"]] == ["ACME"]
    assert before["watchedExcluded"] == 1
    assert after["items"] == []
    assert after["watchedExcluded"] == 2


def test_the_latest_verdict_on_or_before_today_is_read_and_its_date_served(
    movers: TestClient, db_engine: Engine
) -> None:
    _signal(db_engine, "a1", "ACME")
    add_verdict(db_engine, ticker="ACME", session_date=TODAY - timedelta(days=2), passes=False,
                failures="low_volume")
    add_verdict(db_engine, ticker="ACME", session_date=TODAY - timedelta(days=1),
                last_close=Decimal("12.3400"))
    # A row for a session after today's ET date is not today's answer.
    add_verdict(db_engine, ticker="ACME", session_date=TODAY + timedelta(days=3), passes=False,
                failures="low_close")

    (item,) = _body(movers)["items"]

    assert item["tradeabilitySessionDate"] == "2026-09-25"
    assert item["lastClose"] == "12.3400"


def test_a_failing_or_missing_verdict_is_counted_not_served(
    movers: TestClient, db_engine: Engine
) -> None:
    _signal(db_engine, "a1", "FAIL")
    _signal(db_engine, "a2", "WAIT")
    add_verdict(db_engine, ticker="FAIL", session_date=TODAY, passes=False,
                failures="no_completed_session", avg_volume_20d=None, last_close=None,
                sessions_available=0)

    body = _body(movers)

    assert body["items"] == []
    assert (body["signalled"], body["failedTradeability"], body["awaitingCheck"]) == (2, 1, 1)


def test_a_signal_before_the_lookback_is_not_a_mover(movers: TestClient, db_engine: Engine) -> None:
    _signal(db_engine, "a1", "ACME", at=TODAY_START - timedelta(minutes=1))
    add_verdict(db_engine, ticker="ACME", session_date=TODAY)

    assert _body(movers, lookback="today")["items"] == []
    assert [i["ticker"] for i in _body(movers, lookback="3d")["items"]] == ["ACME"]


def test_the_default_lookback_is_today(movers: TestClient) -> None:
    body = _body(movers)
    assert body["lookback"] == "today"
    assert body["since"] == "2026-09-26T04:00:00Z"


# --------------------------------------------------------------------------
# The two empty states
# --------------------------------------------------------------------------


def test_empty_with_fresh_feeds_is_no_name_passed_since_the_lookback(
    movers: TestClient, db_engine: Engine
) -> None:
    add_verdict(db_engine, ticker="ZZZZ", session_date=TODAY,
                checked_at=datetime(2026, 9, 26, 14, 45, tzinfo=UTC))

    body = _body(movers)

    assert body["items"] == []
    assert body["feedsStale"] is False
    assert body["staleSince"] is None
    assert body["since"] == "2026-09-26T04:00:00Z"
    assert body["evaluatedAt"] == "2026-09-26T15:00:00Z"
    assert body["evaluation"]["lastCheckedAt"] == "2026-09-26T14:45:00Z"
    assert body["evaluation"]["scheduled"] is True
    assert [f["feed"] for f in body["feeds"]] == ["alpaca_news", "finnhub_market", "massive_news"]
    assert all(f["scheduled"] and not f["stale"] for f in body["feeds"])


def test_no_scheduler_reads_every_discovery_feed_stale_since_its_newest_row(
    app: FastAPI, db_engine: Engine
) -> None:
    ingested = NOW - timedelta(hours=3)
    add_article(db_engine, vendor="massive", vendor_id="m1", headline="x",
                published_at=ingested, tickers=["ZZZZ"])

    with _client(app, None) as client:
        body = _body(client)

    assert body["feedsStale"] is True
    by_feed = {f["feed"]: f for f in body["feeds"]}
    assert by_feed["massive_news"]["staleSince"] == "2026-09-26T12:00:00Z"
    assert by_feed["massive_news"]["lastIngestedAt"] == "2026-09-26T12:00:00Z"
    assert by_feed["alpaca_news"]["staleSince"] is None  # never delivered anything
    assert all(not f["scheduled"] and f["stale"] for f in body["feeds"])
    # The earliest known-good instant among the stale feeds.
    assert body["staleSince"] == "2026-09-26T12:00:00Z"
    assert body["evaluation"]["scheduled"] is False


def test_a_failing_feed_is_stale_since_its_last_success(app: FastAPI) -> None:
    status = healthy()
    last_ok = NOW - timedelta(minutes=40)
    status["news_alpaca"] = job(
        "news_alpaca", last_success=last_ok, last_failure=NOW - timedelta(minutes=1),
        last_error_type="HTTPStatusError", failures=3,
    )
    with _client(app, status) as client:
        body = _body(client)

    by_feed = {f["feed"]: f for f in body["feeds"]}
    assert by_feed["alpaca_news"]["failing"] is True
    assert by_feed["alpaca_news"]["stale"] is True
    assert by_feed["alpaca_news"]["staleSince"] == "2026-09-26T14:20:00Z"
    assert by_feed["finnhub_market"]["stale"] is False
    assert body["feedsStale"] is True
    assert body["staleSince"] == "2026-09-26T14:20:00Z"


def test_a_feed_whose_latest_run_skipped_is_stale(app: FastAPI) -> None:
    """A skip fetched nothing (no key configured): the feed is not being read."""
    status = healthy()
    status["news_massive"] = job(
        "news_massive", last_success=None, runs=0, skips=4,
        last_skipped=NOW - timedelta(minutes=2), last_skip_reason="no Massive key",
    )
    with _client(app, status) as client:
        body = _body(client)

    massive = next(f for f in body["feeds"] if f["feed"] == "massive_news")
    assert massive["stale"] is True
    assert body["feedsStale"] is True


def test_a_job_that_has_not_run_yet_is_not_stale(app: FastAPI) -> None:
    """No evidence of a fault: just started, before the first slot."""
    status = healthy()
    status["news_finnhub_market"] = job("news_finnhub_market", last_success=None, runs=0)
    with _client(app, status) as client:
        body = _body(client)

    assert body["feedsStale"] is False


def _feed(app: FastAPI, name: str, **fields: Any) -> dict[str, Any]:
    status = healthy()
    status[name] = job(name, **fields)
    with _client(app, status) as client:
        body = _body(client)
    feed = DISCOVERY_FEED_JOBS_BY_JOB[name]
    found: dict[str, Any] = next(f for f in body["feeds"] if f["feed"] == feed)
    found["_body"] = body
    return found


DISCOVERY_FEED_JOBS_BY_JOB = {job_name: feed for feed, job_name in DISCOVERY_FEED_JOBS.items()}


def test_a_job_with_no_next_run_is_stale_while_the_scheduler_runs(app: FastAPI) -> None:
    """Its task died, or it hit ``context_job_unscheduled``: it will never run again."""
    feed = _feed(app, "news_alpaca", next_run=None, stopped=True)
    assert feed["stale"] is True
    assert feed["staleSince"] == "2026-09-26T14:59:00Z"
    assert feed["_body"]["feedsStale"] is True


def test_a_job_not_yet_scheduled_is_not_stale_for_that_alone(app: FastAPI) -> None:
    """``next_run`` is also ``None`` in the instant before a job's loop first asks
    its schedule; only a *stopped* job's ``None`` means it will never run."""
    feed = _feed(app, "news_alpaca", next_run=None, stopped=False)
    assert feed["stale"] is False


def test_the_staleness_multiple_is_three_slots() -> None:
    assert STALE_AFTER_SLOTS == 3


def test_a_feed_is_fresh_until_three_of_its_slots_have_passed_since_its_last_delivery(
    app: FastAPI,
) -> None:
    """A hung run: started, never finished. 15:00 is a slot; 14:45 + 3 slots is 15:00,
    which has not *passed* at 15:00 -- one microsecond earlier it has."""
    at_boundary = _feed(
        app, "news_finnhub_market", last_started=NOW - timedelta(minutes=14),
        last_success=NOW - timedelta(minutes=15),
    )
    past_it = _feed(
        app, "news_finnhub_market", last_started=NOW - timedelta(minutes=14),
        last_success=NOW - timedelta(minutes=15, microseconds=1),
    )
    assert at_boundary["stale"] is False
    assert past_it["stale"] is True
    assert past_it["staleSince"] == "2026-09-26T14:44:59.999999Z"


def test_the_cadence_is_the_job_definitions_own_slots(app: FastAPI) -> None:
    """Saturday: the Alpaca job's slow rate (5 min) applies, not its in-session minute."""
    two_rate = TwoRate(in_session=timedelta(seconds=60), otherwise=timedelta(minutes=5))
    fresh = _feed(app, "news_alpaca", cadence=two_rate, last_success=NOW - timedelta(minutes=12))
    assert fresh["stale"] is False
    one_minute = EveryInterval(timedelta(seconds=60))
    stale = _feed(app, "news_alpaca", cadence=one_minute, last_success=NOW - timedelta(minutes=12))
    assert stale["stale"] is True


def test_a_restart_after_downtime_reads_a_days_old_feed_stale_at_once(
    app: FastAPI, db_engine: Engine
) -> None:
    """Fresh records (nothing completed since the restart), newest row three days old."""
    add_article(db_engine, vendor="massive", vendor_id="m1", headline="x",
                published_at=NOW - timedelta(days=3), tickers=["ZZZZ"])
    feed = _feed(app, "news_massive", last_success=None, runs=0)
    assert feed["stale"] is True
    assert feed["staleSince"] == "2026-09-23T15:00:00Z"
    assert feed["_body"]["staleSince"] == "2026-09-23T15:00:00Z"


def test_a_recent_row_keeps_a_just_restarted_feed_fresh(app: FastAPI, db_engine: Engine) -> None:
    add_article(db_engine, vendor="massive", vendor_id="m1", headline="x",
                published_at=NOW - timedelta(minutes=6), tickers=["ZZZZ"])
    feed = _feed(app, "news_massive", last_success=None, runs=0)
    assert feed["stale"] is False


def test_a_first_run_that_hangs_is_stale_with_no_stale_since(app: FastAPI) -> None:
    """Never delivered anything: stale once its first run has outlived three slots."""
    feed = _feed(app, "news_massive", last_success=None, runs=0,
                 last_started=NOW - timedelta(minutes=20))
    assert feed["stale"] is True
    assert feed["staleSince"] is None
    assert feed["_body"]["staleSince"] is None


def test_the_feed_jobs_named_here_are_the_shipped_scheduler_jobs() -> None:
    """A renamed job would read every discovery feed as unscheduled -- stale for ever."""
    jobs = context_jobs(ContextServices(funds=(), session_factory=lambda: None))  # type: ignore[arg-type, return-value]
    names = {j.name for j in jobs}
    assert set(DISCOVERY_FEED_JOBS.values()) | {TRADEABILITY_JOB} <= names
