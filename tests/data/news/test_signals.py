"""The stored-signal read shared by Movers and the tradeability refresh (``signals.py``).

Pinned: the refresh's candidate set is decision 21's -- off-watch tickers
carrying a *qualifying* label, over the longest bounded lookback, never every
tagged ticker -- ordered newest qualifying label first; and the one label
selector, canonical row first, else lowest member id, applied *before* the
directional filter.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.data.news.labels import LabelSource
from corollary.data.news.signals import (
    LOOKBACK_DAYS,
    SIGNAL_WINDOW_DAYS,
    lookback_start,
    signal_window_start,
    signalled_tickers,
    stored_signals,
)
from corollary.db.models import Base
from corollary.db.session import create_db_engine, sqlite_url
from tests.api.news_support import add_article, add_label

UTC = timezone.utc
#: Friday 2026-10-09, 11:00 EDT.
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    built = create_db_engine(sqlite_url(tmp_path / "signals.db"))
    Base.metadata.create_all(built)
    yield built
    built.dispose()


def _story(engine: Engine, vendor_id: str, *, at: datetime, tickers: tuple[str, ...] = ("MARKET",),
           vendor: str = "finnhub", canonical_id: int | None = None) -> int:
    return add_article(
        engine, vendor=vendor, vendor_id=vendor_id, headline=f"headline {vendor_id}",
        published_at=at, tickers=tickers, canonical_id=canonical_id,
    )


def _rules(engine: Engine, article_id: int, ticker: str, *, rule_id: str = "guidance_raised",
           direction: str = "bullish") -> None:
    add_label(engine, article_id=article_id, ticker=ticker, source="rules",
              direction=direction, rule_id=rule_id, reasoning="raises full-year guidance")


def _since(engine: Engine, watch: frozenset[str] = frozenset()) -> list[str]:
    with Session(engine) as session:
        return signalled_tickers(session, since=signal_window_start(NOW), watch=watch)


def test_the_window_is_the_longest_bounded_lookback() -> None:
    assert SIGNAL_WINDOW_DAYS == 14 == max(d for d in LOOKBACK_DAYS.values() if d is not None)
    assert signal_window_start(NOW) == lookback_start("2w", NOW)
    # 00:00 EDT on 2026-09-26, thirteen ET dates before the 9th.
    assert signal_window_start(NOW) == datetime(2026, 9, 26, 4, 0, tzinfo=UTC)


def test_a_rules_signal_on_an_untagged_ticker_is_a_candidate(engine: Engine) -> None:
    """The audit's probe: tagged only MARKET, labelled ACME by the headline."""
    story = _story(engine, "acme", at=NOW - timedelta(hours=1))
    _rules(engine, story, "ACME")
    assert _since(engine) == ["ACME"]


def test_an_earlier_day_signal_is_still_a_candidate(engine: Engine) -> None:
    """Deferred or errored yesterday: still in the window, so asked again today."""
    story = _story(engine, "old", at=NOW - timedelta(days=5))
    _rules(engine, story, "OLDR")
    assert _since(engine) == ["OLDR"]


def test_a_signal_before_the_window_is_not(engine: Engine) -> None:
    edge = signal_window_start(NOW)
    inside = _story(engine, "in", at=edge)
    outside = _story(engine, "out", at=edge - timedelta(microseconds=1))
    _rules(engine, inside, "INSD")
    _rules(engine, outside, "OUTS")
    assert _since(engine) == ["INSD"]


def test_a_tagged_ticker_with_no_qualifying_signal_is_not_a_candidate(engine: Engine) -> None:
    tagged = _story(engine, "tag", at=NOW - timedelta(hours=1), tickers=("TAGD", "NEUT", "RETD", "MKTL"))
    add_label(engine, article_id=tagged, ticker="NEUT", source="massive", direction="neutral",
              reasoning="mixed")
    # A rule no shipped pattern carries is nobody's flag.
    _rules(engine, tagged, "RETD", rule_id="retired_pattern")
    assert _since(engine) == []


def test_massive_directional_qualifies_and_watched_and_market_do_not(engine: Engine) -> None:
    story = _story(engine, "m", at=NOW - timedelta(hours=1), tickers=("VEND", "NVDA"))
    add_label(engine, article_id=story, ticker="VEND", source="massive", direction="bearish",
              reasoning="weak quarter")
    _rules(engine, story, "NVDA")
    _rules(engine, story, "MARKET")
    assert _since(engine, watch=frozenset({"NVDA"})) == ["VEND"]


def test_ordered_newest_qualifying_signal_first_ties_by_ticker(engine: Engine) -> None:
    at = NOW - timedelta(hours=3)
    tie = _story(engine, "tie", at=at)
    _rules(engine, tie, "ZZZZ")
    _rules(engine, tie, "AAAA")
    newest = _story(engine, "new", at=NOW - timedelta(minutes=5))
    _rules(engine, newest, "MMMM")
    # An older signal on MMMM does not move it back.
    older = _story(engine, "older", at=NOW - timedelta(days=3))
    _rules(engine, older, "MMMM", rule_id="earnings_beat")
    # A newer *non*-qualifying label on AAAA does not move it forward.
    late = _story(engine, "late", at=NOW - timedelta(minutes=1))
    add_label(engine, article_id=late, ticker="AAAA", source="massive", direction="neutral")
    assert _since(engine) == ["MMMM", "AAAA", "ZZZZ"]


def test_the_pick_is_the_feeds_and_runs_before_the_direction_filter(engine: Engine) -> None:
    """Canonical row's label first: a duplicate's directional label never stands in for it."""
    canonical = _story(engine, "c", at=NOW - timedelta(hours=1), vendor="alpaca")
    duplicate = _story(engine, "d", at=NOW - timedelta(hours=1), canonical_id=canonical)
    add_label(engine, article_id=canonical, ticker="ACME", source="massive", direction="neutral")
    add_label(engine, article_id=duplicate, ticker="ACME", source="massive", direction="bullish")
    with Session(engine) as session:
        assert stored_signals(session, None) == []
    assert _since(engine) == []


def test_two_member_copies_give_one_signal_the_lowest_member_ids(engine: Engine) -> None:
    canonical = _story(engine, "c", at=NOW - timedelta(hours=1), vendor="alpaca")
    first = _story(engine, "d1", at=NOW - timedelta(hours=1), canonical_id=canonical)
    second = _story(engine, "d2", at=NOW - timedelta(hours=1), vendor="massive",
                    canonical_id=canonical)
    add_label(engine, article_id=second, ticker="ACME", source="rules", direction="bullish",
              rule_id="guidance_raised", reasoning="b")
    add_label(engine, article_id=first, ticker="ACME", source="rules", direction="bullish",
              rule_id="guidance_raised", reasoning="a longer reasoning text")
    with Session(engine) as session:
        (only,) = stored_signals(session, None)
    assert only.article_id == canonical
    assert only.source is LabelSource.RULES
    # The lowest member id's, not the shorter text (Movers' old tie-break).
    assert only.reasoning == "a longer reasoning text"
