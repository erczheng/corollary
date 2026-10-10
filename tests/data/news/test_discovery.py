"""Decision 21's discovery candidates, the pure layer (``corollary.data.news.discovery``).

The spec's *Testing -> Discovery* items that belong here: what is never a
candidate, the ranking rule, determinism, and attribution as the labeller
already decides it. The tradeability *boundaries* (ADV at 999,999 / 1,000,000,
a close at $4.99 / $5, zero vs one completed session, Q12's IPO dates) are
``test_tradeability.py``'s; this layer reads the verdict the cache already
holds and must never re-decide it, so the verdicts here are built by
:func:`assess_tradeability` itself where a boundary is the point.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from corollary.data.news.article import NewsArticle, NewsFeed
from corollary.data.news.discovery import (
    CachedVerdict,
    DiscoverySignal,
    discover,
    verdict_from_result,
)
from corollary.data.news.labels import Direction, LabelSource, SentimentLabel
from corollary.data.news.rules import (
    RULE_PATTERNS,
    NameBook,
    RuleFamily,
    rules_labels,
)
from corollary.data.news.tradeability import (
    ADV_SESSIONS,
    MIN_AVG_DAILY_VOLUME,
    MIN_LAST_CLOSE,
    TradeabilityFailure,
    TradeabilityResult,
    has_standard_contract,
)
from corollary.data.news.vendor import MASSIVE_SENTIMENT_DIRECTION

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
SESSION = date(2026, 10, 9)


def passing(ticker: str, *, adv: int = 2_500_000, close: str = "41.20") -> CachedVerdict:
    return CachedVerdict(
        ticker=ticker,
        session_date=SESSION,
        passes=True,
        avg_volume_20d=adv,
        last_close=Decimal(close),
        sessions_available=ADV_SESSIONS,
        failures=(),
        checked_at=NOW - timedelta(hours=2),
    )


def signal(
    ticker: str,
    *,
    article_id: int = 1,
    source: LabelSource = LabelSource.RULES,
    direction: Direction = Direction.BULLISH,
    rule_id: str | None = "earnings_beat",
    at: datetime = NOW - timedelta(hours=1),
    reasoning: str | None = "beats estimates",
) -> DiscoverySignal:
    return DiscoverySignal(
        article_id=article_id,
        published_at=at,
        headline=f"headline {article_id}",
        url=f"https://example.com/{article_id}",
        publisher="Benzinga",
        ticker=ticker,
        source=source,
        direction=direction,
        rule_id=rule_id if source is LabelSource.RULES else None,
        reasoning=reasoning,
    )


def massive(ticker: str, direction: Direction, **kwargs: object) -> DiscoverySignal:
    return signal(ticker, source=LabelSource.MASSIVE, direction=direction, **kwargs)  # type: ignore[arg-type]


def tickers(
    signals: Sequence[DiscoverySignal],
    *,
    watched: frozenset[str] = frozenset(),
    verdicts: dict[str, CachedVerdict] | None = None,
) -> list[str]:
    chosen = verdicts if verdicts is not None else {s.ticker: passing(s.ticker) for s in signals}
    result = discover(signals, watched=watched, verdicts=chosen, since=None)
    return [candidate.ticker for candidate in result.candidates]


# --------------------------------------------------------------------------
# What is never a candidate
# --------------------------------------------------------------------------


def test_an_off_watch_ticker_with_a_flagged_rules_label_and_a_pass_is_a_candidate() -> None:
    assert tickers([signal("ACME")]) == ["ACME"]


def test_a_watch_universe_ticker_is_never_a_candidate() -> None:
    assert tickers([signal("NVDA")], watched=frozenset({"NVDA"})) == []


def test_a_ticker_leaves_the_moment_it_is_watched() -> None:
    """Derived on read: the same inputs with ACME watched drop it, nothing stored."""
    signals = [signal("ACME"), signal("ZETA", article_id=2)]
    verdicts = {"ACME": passing("ACME"), "ZETA": passing("ZETA")}
    before = discover(signals, watched=frozenset(), verdicts=verdicts, since=None)
    after = discover(signals, watched=frozenset({"ACME"}), verdicts=verdicts, since=None)

    assert [c.ticker for c in before.candidates] == ["ACME", "ZETA"]
    assert [c.ticker for c in after.candidates] == ["ZETA"]
    assert after.watched_excluded == 1


def test_market_is_never_a_candidate() -> None:
    assert tickers([signal("MARKET")]) == []


@pytest.mark.parametrize("vendor_word", ["neutral", "mixed"])
def test_a_massive_neutral_or_mixed_insight_never_qualifies(vendor_word: str) -> None:
    direction = MASSIVE_SENTIMENT_DIRECTION[vendor_word]
    assert tickers([massive("ACME", direction)]) == []


@pytest.mark.parametrize("vendor_word", ["positive", "negative"])
def test_a_massive_positive_or_negative_insight_qualifies(vendor_word: str) -> None:
    direction = MASSIVE_SENTIMENT_DIRECTION[vendor_word]
    assert tickers([massive("ACME", direction)]) == ["ACME"]


def test_a_rules_label_from_an_unflagged_pattern_does_not_qualify() -> None:
    pattern = next(p for p in RULE_PATTERNS if p.rule_id == "earnings_beat")
    unflagged = {pattern.rule_id: replace(pattern, discovery_flagged=False)}
    result = discover(
        [signal("ACME")], watched=frozenset(), verdicts={"ACME": passing("ACME")},
        since=None, patterns=unflagged,
    )
    assert result.candidates == ()


def test_a_rules_label_with_a_rule_id_no_pattern_has_does_not_qualify() -> None:
    """A retired pattern's stored label is unflagged: nobody flagged it."""
    assert tickers([signal("ACME", rule_id="retired_pattern")]) == []


def test_every_shipped_v1_pattern_is_discovery_flagged() -> None:
    """Decision 21's eight families, every v1 pattern in them flagged."""
    assert {p.family for p in RULE_PATTERNS if p.discovery_flagged} == set(RuleFamily)


@pytest.mark.parametrize("failure", list(TradeabilityFailure))
def test_a_ticker_failing_any_one_tradeability_check_is_never_a_candidate(
    failure: TradeabilityFailure,
) -> None:
    """Every failure the cache can hold, ``no_completed_session`` (Q10) included."""
    failed = replace(passing("ACME"), passes=False, failures=(failure.value,))
    assert tickers([signal("ACME")], verdicts={"ACME": failed}) == []


def test_a_failure_token_this_build_does_not_know_still_fails() -> None:
    """Only ``passes`` decides: an aged-out token (``insufficient_history``,
    pre-Q10) is a failing row, never a crash and never a pass."""
    failed = replace(passing("ACME"), passes=False, failures=("insufficient_history",))
    assert tickers([signal("ACME")], verdicts={"ACME": failed}) == []


def test_a_ticker_with_no_cached_verdict_is_awaiting_a_check_not_a_candidate() -> None:
    result = discover([signal("ACME")], watched=frozenset(), verdicts={}, since=None)

    assert result.candidates == ()
    assert result.awaiting_check == ("ACME",)
    assert result.failed_tradeability == 0


def test_a_class_share_has_no_standard_root_contract_and_is_never_a_candidate() -> None:
    """Q11: OCC writes ``BRKB`` for ``BRK.B``, so the root never equals the ticker."""
    assert has_standard_contract("BRK.B", []) is False
    failed = replace(
        passing("BRK.B"),
        passes=False,
        failures=(TradeabilityFailure.NO_STANDARD_CONTRACT.value,),
    )
    assert tickers([signal("BRK.B")], verdicts={"BRK.B": failed}) == []


def test_the_boundary_verdict_passes_through_unchanged() -> None:
    """ADV at exactly 1,000,000, a close at exactly $5, one completed session:
    the cache said pass, so this layer serves it -- and serves its numbers exactly."""
    result = TradeabilityResult(
        ticker="ACME",
        session_date=SESSION,
        has_options=True,
        standard_root=True,
        avg_volume_20d=MIN_AVG_DAILY_VOLUME,
        last_close=MIN_LAST_CLOSE,
        sessions_available=1,
        failures=(),
    )
    verdict = verdict_from_result(result, checked_at=NOW)
    found = discover([signal("ACME")], watched=frozenset(), verdicts={"ACME": verdict}, since=None)

    (candidate,) = found.candidates
    assert candidate.avg_volume_20d == 1_000_000
    assert candidate.last_close == Decimal("5")
    assert isinstance(candidate.last_close, Decimal)
    assert candidate.sessions_available == 1
    assert candidate.tradeability_session_date == SESSION


def test_a_signal_older_than_the_lookback_does_not_count() -> None:
    old = signal("ACME", at=NOW - timedelta(days=4))
    result = discover(
        [old], watched=frozenset(), verdicts={"ACME": passing("ACME")},
        since=NOW - timedelta(days=1),
    )
    assert result.candidates == ()


# --------------------------------------------------------------------------
# Reasons: kept per source and direction, never netted
# --------------------------------------------------------------------------


def test_conflicting_directions_are_kept_side_by_side() -> None:
    signals = [
        signal("ACME", article_id=1, rule_id="analyst_downgrade", direction=Direction.BEARISH),
        massive("ACME", Direction.BULLISH, article_id=2, reasoning="strong quarter"),
    ]
    (candidate,) = discover(
        signals, watched=frozenset(), verdicts={"ACME": passing("ACME")}, since=None
    ).candidates

    assert candidate.directions == (Direction.BEARISH, Direction.BULLISH)
    assert [(r.source, r.rule_id, r.direction) for r in candidate.reasons] == [
        (LabelSource.RULES, "analyst_downgrade", Direction.BEARISH),
        (LabelSource.MASSIVE, None, Direction.BULLISH),
    ]
    assert candidate.reasons[0].family is RuleFamily.ANALYST_RATING
    assert candidate.reasons[1].family is None
    assert candidate.reasons[1].evidence[0].reasoning == "strong quarter"


def test_one_rule_on_two_articles_is_one_reason_with_both_articles() -> None:
    signals = [
        signal("ACME", article_id=1, at=NOW - timedelta(hours=3)),
        signal("ACME", article_id=2, at=NOW - timedelta(hours=1)),
    ]
    (candidate,) = discover(
        signals, watched=frozenset(), verdicts={"ACME": passing("ACME")}, since=None
    ).candidates

    (reason,) = candidate.reasons
    assert reason.article_ids == (2, 1)
    assert [a.id for a in candidate.articles] == [2, 1]
    assert candidate.latest_at == NOW - timedelta(hours=1)


def test_one_canonical_article_reached_twice_by_one_source_is_refused() -> None:
    """The caller picks one label per (group, ticker, source) -- the feed's rule, in
    ``signals.group_labels``. Two arriving here means it did not, and choosing between
    them here (Movers once kept the shorter reasoning) is a second rule that drifts."""
    signals = [
        signal("ACME", article_id=7, reasoning="a"),
        signal("ACME", article_id=7, reasoning="b"),
    ]
    with pytest.raises(ValueError, match="ACME"):
        discover(signals, watched=frozenset(), verdicts={"ACME": passing("ACME")}, since=None)


def test_one_canonical_article_from_two_sources_is_one_article() -> None:
    signals = [signal("ACME", article_id=7), massive("ACME", Direction.BULLISH, article_id=7)]
    (candidate,) = discover(
        signals, watched=frozenset(), verdicts={"ACME": passing("ACME")}, since=None
    ).candidates

    assert [r.article_ids for r in candidate.reasons] == [(7,), (7,)]
    assert len(candidate.articles) == 1


def test_a_non_qualifying_label_does_not_appear_as_a_reason() -> None:
    signals = [signal("ACME", article_id=1), massive("ACME", Direction.NEUTRAL, article_id=2)]
    (candidate,) = discover(
        signals, watched=frozenset(), verdicts={"ACME": passing("ACME")}, since=None
    ).candidates

    assert [r.source for r in candidate.reasons] == [LabelSource.RULES]
    assert [a.id for a in candidate.articles] == [1]


# --------------------------------------------------------------------------
# Ranking: rules-first, then newest (Q22: confirmed as written)
# --------------------------------------------------------------------------


def test_a_rules_event_outranks_a_massive_only_row_regardless_of_recency() -> None:
    signals = [
        signal("OLDR", article_id=1, at=NOW - timedelta(days=2)),
        massive("NEWM", Direction.BULLISH, article_id=2, at=NOW - timedelta(minutes=1)),
    ]
    assert tickers(signals) == ["OLDR", "NEWM"]


def test_within_a_group_newest_first() -> None:
    signals = [
        signal("AAAA", article_id=1, at=NOW - timedelta(hours=5)),
        signal("BBBB", article_id=2, at=NOW - timedelta(hours=1)),
        massive("CCCC", Direction.BULLISH, article_id=3, at=NOW - timedelta(hours=6)),
        massive("DDDD", Direction.BEARISH, article_id=4, at=NOW - timedelta(hours=2)),
    ]
    assert tickers(signals) == ["BBBB", "AAAA", "DDDD", "CCCC"]


def test_a_massive_label_on_a_rules_row_moves_it_by_its_time() -> None:
    """"Newest qualifying article": a rules row's newest article may be a Massive one."""
    signals = [
        signal("AAAA", article_id=1, at=NOW - timedelta(hours=5)),
        massive("AAAA", Direction.BULLISH, article_id=2, at=NOW - timedelta(minutes=5)),
        signal("BBBB", article_id=3, at=NOW - timedelta(hours=1)),
    ]
    assert tickers(signals) == ["AAAA", "BBBB"]


def test_a_tie_on_time_breaks_by_ticker() -> None:
    at = NOW - timedelta(hours=1)
    signals = [
        signal("ZZZZ", article_id=1, at=at),
        signal("AAAA", article_id=2, at=at),
        signal("MMMM", article_id=3, at=at),
    ]
    assert tickers(signals) == ["AAAA", "MMMM", "ZZZZ"]


def test_the_same_inputs_give_the_same_order_whatever_order_they_arrive_in() -> None:
    at = NOW - timedelta(hours=1)
    signals = [
        signal("ZZZZ", article_id=1, at=at),
        massive("YYYY", Direction.BEARISH, article_id=2, at=at),
        signal("AAAA", article_id=3, at=at - timedelta(minutes=1)),
        massive("ZZZZ", Direction.BEARISH, article_id=4, at=at),
        signal("MMMM", article_id=5, at=at, rule_id="guidance_cut", direction=Direction.BEARISH),
        massive("BBBB", Direction.BULLISH, article_id=6, at=at),
    ]
    verdicts = {s.ticker: passing(s.ticker) for s in signals}
    first = discover(signals, watched=frozenset(), verdicts=verdicts, since=None)
    for shuffled in (list(reversed(signals)), signals[3:] + signals[:3]):
        assert discover(shuffled, watched=frozenset(), verdicts=verdicts, since=None) == first
    assert [c.ticker for c in first.candidates] == ["MMMM", "ZZZZ", "AAAA", "BBBB", "YYYY"]


# --------------------------------------------------------------------------
# Attribution: what the labeller already decides, end to end
# --------------------------------------------------------------------------

_BOOK = NameBook(
    watch=frozenset(),
    watch_names={},
    asset_names={"FSLY": "Fastly, Inc. Class A Common Stock"},
    funds=(),
)


def _signals_from_labeller(feed: NewsFeed, tags: tuple[str, ...]) -> list[DiscoverySignal]:
    vendor = {"alpaca_news": "alpaca", "massive_news": "massive"}.get(feed.value, "finnhub")
    fetched = NewsArticle(
        vendor=vendor,
        vendor_id="synthetic-1",
        feed=feed,
        url="https://example.invalid/a",
        headline="Company Raises Full-Year Guidance",
        summary=None,
        publisher="Synthetic",
        published_at=NOW - timedelta(hours=1),
        tickers=tags,
    )
    labels: Sequence[SentimentLabel] = rules_labels(fetched, _BOOK).labels
    return [
        DiscoverySignal(
            article_id=1,
            published_at=fetched.published_at,
            headline=fetched.headline,
            url=fetched.url,
            publisher=fetched.publisher,
            ticker=label.ticker,
            source=label.source,
            direction=label.direction,
            rule_id=label.rule_id,
            reasoning=label.reasoning,
        )
        for label in labels
    ]


@pytest.mark.parametrize("feed", [NewsFeed.ALPACA_NEWS, NewsFeed.MASSIVE_NEWS])
def test_a_single_tag_alpaca_or_massive_article_attributes_to_its_tag(feed: NewsFeed) -> None:
    assert tickers(_signals_from_labeller(feed, ("FSLY",)), verdicts={"FSLY": passing("FSLY")}) == [
        "FSLY"
    ]


def test_a_single_tag_finnhub_company_row_does_not_attribute() -> None:
    signals = _signals_from_labeller(NewsFeed.FINNHUB_COMPANY, ("FSLY",))
    assert signals == []
    assert tickers(signals, verdicts={"FSLY": passing("FSLY")}) == []


# --------------------------------------------------------------------------
# What the result reports besides its candidates
# --------------------------------------------------------------------------


def test_the_result_counts_why_signalled_names_were_not_served() -> None:
    signals = [
        signal("PASS", article_id=1),
        signal("FAIL", article_id=2),
        signal("WAIT", article_id=3),
        signal("NVDA", article_id=4),
        massive("NEUT", Direction.NEUTRAL, article_id=5),
    ]
    failed = replace(passing("FAIL"), passes=False, failures=("low_volume",))
    result = discover(
        signals,
        watched=frozenset({"NVDA"}),
        verdicts={"PASS": passing("PASS"), "FAIL": failed},
        since=None,
    )

    assert [c.ticker for c in result.candidates] == ["PASS"]
    assert result.signalled == 3
    assert result.watched_excluded == 1
    assert result.failed_tradeability == 1
    assert result.awaiting_check == ("WAIT",)


def test_a_verdict_must_be_for_its_own_ticker() -> None:
    with pytest.raises(ValueError, match="ACME"):
        discover(
            [signal("ACME")], watched=frozenset(), verdicts={"ACME": passing("ZETA")}, since=None
        )
