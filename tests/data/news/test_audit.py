"""Decisions 12 and 13, pure -- ``corollary.data.news.audit``: grading (Phase 3
unit 6.2), then accuracy and transitions (unit 6.3).

Every input is synthetic and every date a real 2026 NYSE session, read from
the calendar rather than assumed. The bars are real :class:`Bar` instances,
stamped at their **opening** minute as the provider's are, so the
no-look-ahead rule (a bar's close is known one minute after its stamp) is
proven against the type the runner will actually pass.
"""

import random
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from corollary.calendars import NYSE_TZ, nyse_session_close, nyse_session_open
from corollary.data.news.audit import (
    BENCHMARK_SYMBOL,
    DEMOTE_BELOW_PCT,
    DEMOTION_FLOOR,
    DISCOVERY_TICKER_CAP,
    FALLBACK_MAX_LAG,
    MATURITY_DELAY,
    PRICE_UNAVAILABLE_AFTER,
    REPROMOTE_AT_PCT,
    REPROMOTION_FLOOR,
    RETURN_EXPONENT,
    SENTIMENT_AUDIT_CATEGORY,
    TRAILING_WINDOW,
    VENDOR_STAMP_TOLERANCE,
    WINDOW_SESSION_MINUTES,
    AccuracyBasis,
    AccuracyRow,
    AccuracyScope,
    AuditLabel,
    AuditLogFields,
    AuditPlan,
    BarFault,
    BarRequest,
    CurrentStatus,
    GradedOutcome,
    GradeResult,
    LabelOutcomeRecord,
    Scope,
    SentimentEvent,
    SkipReason,
    SourceStatus,
    Transition,
    audit_source,
    audit_sources,
    compute_accuracy,
    decide_transition,
    earliest_reference_needed,
    grade,
    graded_outcome,
    plan_audit,
    reference_at,
    reference_instant,
    window_end,
)
from corollary.data.news.labels import Direction
from corollary.data.providers.interface import Bar
from corollary.db.models import (
    ACCURACY_BASES,
    ACCURACY_SCOPES,
    AUDIT_CATEGORIES,
    LABEL_OUTCOME_SCOPES,
    LABEL_SKIP_REASONS,
    SENTIMENT_SOURCE_STATUSES,
    SENTIMENT_WINDOWS,
)

BULL = Direction.BULLISH
BEAR = Direction.BEARISH
NEUTRAL = Direction.NEUTRAL

#: Saturday 2026-10-10 10:00 ET -- the scheduled run after the week below.
RUN_AT = datetime(2026, 10, 10, 10, 0, tzinfo=NYSE_TZ).astimezone(timezone.utc)

_MINUTE = timedelta(minutes=1)


def et(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    """An Eastern wall time as the UTC instant everything stores."""
    return datetime(year, month, day, hour, minute, second, tzinfo=NYSE_TZ).astimezone(
        timezone.utc
    )


def bar(symbol: str, at: datetime, open_: str, close: str) -> Bar:
    o, c = Decimal(open_), Decimal(close)
    return Bar(
        symbol=symbol,
        at=at,
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=1000,
        trade_count=10,
        vwap=None,
    )


def label(
    label_id: int,
    ticker: str,
    direction: Direction,
    published: datetime,
    labeled: datetime | None = None,
    source: str = "rules",
    *,
    group: int | None = None,
    pick: bool = True,
) -> AuditLabel:
    """A label; by default the only copy of its story -- its own group, and the pick."""
    return AuditLabel(
        label_id=label_id,
        ticker=ticker,
        source=source,
        direction=direction,
        published_at=published,
        labeled_at=published if labeled is None else labeled,
        group_id=1000 + label_id if group is None else group,
        group_pick=pick,
    )


#: A session before every label here: a bare ``True``/``False`` verdict in a
#: test means "this ticker's verdict, known well before the label".
_LONG_AGO = date(2026, 1, 2)


def run(
    labels: Iterable[AuditLabel],
    bars: Iterable[Bar],
    *,
    watch: Collection[str] = ("AAPL", "MSFT", "SPY", "MARKET"),
    tradeable: Mapping[str, bool | Sequence[tuple[date, bool]]] | None = None,
    written: Collection[tuple[int, str]] = (),
    claimed: Collection[tuple[int, str, str]] = (),
    run_at: datetime = RUN_AT,
) -> tuple[AuditPlan, GradeResult]:
    history = {
        ticker: ((_LONG_AGO, verdict),) if isinstance(verdict, bool) else tuple(verdict)
        for ticker, verdict in ({} if tradeable is None else tradeable).items()
    }
    plan = plan_audit(
        list(labels),
        watch_universe=watch,
        tradeable=history,
        written=written,
        claimed_groups=claimed,
        run_at=run_at,
    )
    return plan, grade(plan, list(bars))


def by_key(result: GradeResult) -> dict[tuple[int, str], LabelOutcomeRecord]:
    return {(r.label_id, r.window): r for r in result.records}


def flat_session(symbol: str, day_open: datetime, price: str) -> list[Bar]:
    """A whole extended-hours day at one price: 04:00-19:59 ET, every minute."""
    start = day_open.astimezone(NYSE_TZ).replace(hour=4, minute=0)
    return [
        bar(symbol, (start + i * _MINUTE).astimezone(timezone.utc), price, price)
        for i in range(16 * 60)
    ]


# --------------------------------------------------------------------- #
# The constants agree with the schema
# --------------------------------------------------------------------- #


def test_the_skip_reasons_are_the_schemas() -> None:
    assert tuple(reason.value for reason in SkipReason) == LABEL_SKIP_REASONS


def test_the_windows_are_the_schemas_in_session_minutes() -> None:
    assert tuple(WINDOW_SESSION_MINUTES) == SENTIMENT_WINDOWS
    assert dict(WINDOW_SESSION_MINUTES) == {"1h": 60, "1d": 390}


def test_the_named_policies() -> None:
    assert BENCHMARK_SYMBOL == "SPY"
    assert DISCOVERY_TICKER_CAP == 250
    assert FALLBACK_MAX_LAG == timedelta(minutes=30)
    assert MATURITY_DELAY == timedelta(minutes=15)
    assert PRICE_UNAVAILABLE_AFTER == timedelta(days=7)
    assert RETURN_EXPONENT == Decimal("1E-12")
    assert VENDOR_STAMP_TOLERANCE == timedelta(minutes=15)


# --------------------------------------------------------------------- #
# The reference instant and price
# --------------------------------------------------------------------- #


def test_the_reference_in_session_is_the_last_completed_bars_close() -> None:
    """10:15:30 reads the 10:14 bar (ended 10:15), never the 10:15 bar (ends 10:16)."""
    published = et(2026, 10, 6, 10, 15, 30)
    bars = [
        bar("AAPL", et(2026, 10, 6, 10, 14), "99", "100"),
        bar("AAPL", et(2026, 10, 6, 10, 15), "150", "150"),
        bar("AAPL", et(2026, 10, 6, 11, 14), "101", "102"),
        bar("SPY", et(2026, 10, 6, 10, 14), "500", "500"),
        bar("SPY", et(2026, 10, 6, 10, 15), "900", "900"),
        bar("SPY", et(2026, 10, 6, 11, 14), "500", "505"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.reference_at == published
    assert record.reference_price == Decimal("100")
    assert record.outcome_at == et(2026, 10, 6, 11, 15, 30)
    assert record.outcome_price == Decimal("102")


def test_the_reference_out_of_session_is_the_next_sessions_first_open() -> None:
    """Published 18:00: the price is Wednesday's 09:30 open, not an after-hours print."""
    published = et(2026, 10, 6, 18, 0)
    bars = [
        bar("AAPL", et(2026, 10, 6, 18, 0), "77", "77"),
        bar("AAPL", et(2026, 10, 7, 8, 0), "88", "88"),
        bar("AAPL", et(2026, 10, 7, 9, 30), "100", "101"),
        bar("AAPL", et(2026, 10, 7, 10, 29), "110", "110"),
        bar("SPY", et(2026, 10, 7, 9, 30), "500", "501"),
        bar("SPY", et(2026, 10, 7, 10, 29), "505", "505"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.reference_at == et(2026, 10, 7, 9, 30)
    assert record.reference_price == Decimal("100")
    assert record.outcome_at == et(2026, 10, 7, 10, 30)
    assert record.outcome_price == Decimal("110")
    assert record.benchmark_return == Decimal("0.010000000000")


def test_the_reference_is_the_later_of_published_and_labeled() -> None:
    """Corollary is not credited with a move that happened before it saw the story."""
    published = et(2026, 10, 6, 10, 0)
    labeled = et(2026, 10, 6, 11, 0)
    assert reference_at(label(1, "AAPL", BULL, published, labeled)) == labeled
    bars = [
        bar("AAPL", et(2026, 10, 6, 9, 59), "90", "90"),
        bar("AAPL", et(2026, 10, 6, 10, 59), "100", "100"),
        bar("AAPL", et(2026, 10, 6, 11, 59), "103", "103"),
        bar("SPY", et(2026, 10, 6, 9, 59), "400", "400"),
        bar("SPY", et(2026, 10, 6, 10, 59), "500", "500"),
        bar("SPY", et(2026, 10, 6, 11, 59), "500", "500"),
    ]
    _, result = run([label(1, "AAPL", BULL, published, labeled)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.reference_at == labeled
    assert record.reference_price == Decimal("100")
    assert record.excess_return == Decimal("0.030000000000")


def test_a_published_at_far_after_labeled_at_yields_to_labeled_at() -> None:
    """Decision 1's amendment: a stamp past the tolerance is the vendor's error.

    No poll fetches a story an hour ahead of its own start, so ``labeled_at``
    -- when the story demonstrably existed -- is the reference.
    """
    published = et(2026, 10, 6, 11, 0)
    labeled = et(2026, 10, 6, 10, 0)
    assert reference_instant(label(1, "AAPL", BULL, published, labeled)) == labeled
    assert reference_at(label(1, "AAPL", BULL, published, labeled)) == labeled


def test_a_published_at_just_after_the_poll_start_is_the_reference() -> None:
    """``labeled_at`` is the poll's start, so a story on a later page of the
    same fetch is honestly published a few seconds after it. Using
    ``labeled_at`` there would take the reference before publication -- here
    the 10:13 bar instead of the 10:14 -- and credit the label with a minute
    from before the story existed."""
    labeled = et(2026, 10, 6, 10, 14, 58)
    published = labeled + timedelta(seconds=4)
    item = label(1, "AAPL", BULL, published, labeled)
    assert reference_instant(item) == published.astimezone(timezone.utc)
    assert reference_at(item) == published.astimezone(timezone.utc)


def test_the_vendor_stamp_tolerance_boundary() -> None:
    labeled = et(2026, 10, 6, 10, 0)
    at_bound = label(1, "AAPL", BULL, labeled + VENDOR_STAMP_TOLERANCE, labeled)
    past_bound = label(
        2, "AAPL", BULL, labeled + VENDOR_STAMP_TOLERANCE + timedelta(seconds=1), labeled
    )
    assert reference_instant(at_bound) == (labeled + VENDOR_STAMP_TOLERANCE).astimezone(timezone.utc)
    assert reference_instant(past_bound) == labeled.astimezone(timezone.utc)


def test_a_far_future_published_at_grades_from_labeled_at() -> None:
    """A 2062 typo past the published schedule neither raises nor stalls the label."""
    published = datetime(2062, 3, 1, 15, 0, tzinfo=timezone.utc)
    labeled = _TUESDAY_1015
    bars = _two_point_bars("AAPL", "100", "110") + _two_point_bars("SPY", "500", "500")
    plan, result = run([label(1, "AAPL", BULL, published, labeled)], bars)
    assert plan.unschedulable_label_ids == ()
    record = by_key(result)[(1, "1h")]
    assert record.reference_at == labeled
    assert record.excess_return == Decimal("0.100000000000")


def test_a_labeled_at_past_the_schedule_is_unschedulable_not_raised() -> None:
    """The calendar's past-the-schedule error is caught per label, never per run."""
    far = datetime(2062, 3, 1, 15, 0, tzinfo=timezone.utc)
    bars = _two_point_bars("AAPL", "100", "110") + _two_point_bars("SPY", "500", "500")
    labels = [
        label(1, "AAPL", BULL, _TUESDAY_1015),
        label(2, "AAPL", BULL, _TUESDAY_1015, far),
        label(3, "MSFT", BEAR, far, far),
    ]
    plan, result = run(labels, bars)
    assert plan.unschedulable_label_ids == (2, 3)
    assert {item.label.label_id for item in plan.to_grade} == {1}
    assert {r.label_id for r in result.records} == {1}
    assert by_key(result)[(1, "1h")].correct is True
    assert all(key[0] == 1 for key in result.awaiting_bars)


def test_an_unschedulable_label_with_every_window_written_is_not_named() -> None:
    far = datetime(2062, 3, 1, 15, 0, tzinfo=timezone.utc)
    plan, _ = run([label(2, "AAPL", BULL, far, far)], [], written=[(2, "1h"), (2, "1d")])
    assert plan.unschedulable_label_ids == ()


def test_a_label_late_into_the_session_open_out_of_session_uses_the_next_open() -> None:
    """Published Friday night, labelled Monday 08:00: Monday's open, both late."""
    assert reference_at(
        label(1, "AAPL", BULL, et(2026, 10, 9, 20, 0), et(2026, 10, 12, 8, 0))
    ) == et(2026, 10, 12, 9, 30)


def test_inside_the_first_minute_the_reference_waits_for_the_first_completed_bar() -> None:
    """09:30:20 is after the open print: the 09:30 bar's close at 09:31, never its open.

    The open would credit the label with movement from before it was seen
    (decision 1). SPY is priced at the same 09:31, by the same rule.
    """
    published = et(2026, 10, 6, 9, 30, 20)
    bars = [
        bar("AAPL", et(2026, 10, 6, 9, 29), "70", "70"),
        bar("AAPL", et(2026, 10, 6, 9, 30), "100", "130"),
        bar("AAPL", et(2026, 10, 6, 10, 29), "143", "143"),
        bar("AAPL", et(2026, 10, 6, 10, 30), "999", "999"),
        bar("SPY", et(2026, 10, 6, 9, 30), "500", "600"),
        bar("SPY", et(2026, 10, 6, 10, 29), "600", "600"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.reference_at == et(2026, 10, 6, 9, 31)
    assert record.reference_price == Decimal("130")
    # The window's end is still measured from the label (decision 2).
    assert record.outcome_at == et(2026, 10, 6, 10, 30, 20)
    assert record.outcome_price == Decimal("143")
    assert record.benchmark_return == Decimal("0E-12")
    assert record.excess_return == Decimal("0.100000000000")


def test_a_first_minute_label_with_no_open_bar_falls_back_to_the_first_bar() -> None:
    """No 09:30 bar: the first bar's open at its own stamp, still after the label."""
    published = et(2026, 10, 6, 9, 30, 20)
    bars = [
        bar("AAPL", et(2026, 10, 6, 9, 33), "100", "120"),
        bar("AAPL", et(2026, 10, 6, 10, 29), "110", "110"),
        bar("SPY", et(2026, 10, 6, 9, 32), "500", "500"),
        bar("SPY", et(2026, 10, 6, 10, 29), "500", "500"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.reference_at == et(2026, 10, 6, 9, 33)
    assert record.reference_price == Decimal("100")
    assert record.excess_return == Decimal("0.100000000000")


def test_a_first_minute_label_is_bounded_by_the_fallback_lag() -> None:
    """No 09:30 bar and the first one at 10:01 is more than FALLBACK_MAX_LAG late."""
    published = et(2026, 10, 6, 9, 30, 20)
    bars = [
        bar("AAPL", et(2026, 10, 6, 10, 1), "100", "100"),
        bar("AAPL", et(2026, 10, 6, 10, 29), "110", "110"),
        bar("SPY", et(2026, 10, 6, 9, 30), "500", "500"),
        bar("SPY", et(2026, 10, 6, 10, 29), "500", "500"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    assert (1, "1h") in result.awaiting_bars


def _late_open_bars(symbol: str, first: datetime) -> list[Bar]:
    """Wednesday 2026-10-07 with ``symbol``'s first bar at ``first``, then 10:29 and 15:59."""
    return [
        bar(symbol, first, "100", "100"),
        bar(symbol, et(2026, 10, 7, 10, 29), "110", "110"),
        bar(symbol, et(2026, 10, 7, 15, 59), "110", "110"),
    ]


def test_a_fallback_first_bar_at_the_lag_is_used() -> None:
    """Out of session, first bar stamped exactly FALLBACK_MAX_LAG after the open: graded."""
    open_ = et(2026, 10, 7, 9, 30)
    bars = _late_open_bars("AAPL", open_ + FALLBACK_MAX_LAG) + flat_session("SPY", open_, "500")
    _, result = run([label(1, "AAPL", BULL, et(2026, 10, 6, 18, 0))], bars)
    record = by_key(result)[(1, "1h")]
    assert record.reference_at == open_ + FALLBACK_MAX_LAG
    assert record.reference_price == Decimal("100")


def test_a_fallback_first_bar_past_the_lag_is_no_reference() -> None:
    """A minute later than FALLBACK_MAX_LAG: no reference, so the window waits."""
    open_ = et(2026, 10, 7, 9, 30)
    bars = _late_open_bars("AAPL", open_ + FALLBACK_MAX_LAG + _MINUTE) + flat_session(
        "SPY", open_, "500"
    )
    _, result = run([label(1, "AAPL", BULL, et(2026, 10, 6, 18, 0))], bars)
    assert set(result.awaiting_bars) == {(1, "1h"), (1, "1d")}
    assert result.records == ()


def test_a_fallback_past_the_lag_becomes_price_unavailable_after_seven_days() -> None:
    open_ = et(2026, 10, 7, 9, 30)
    bars = _late_open_bars("AAPL", open_ + FALLBACK_MAX_LAG + _MINUTE) + flat_session(
        "SPY", open_, "500"
    )
    _, result = run(
        [label(1, "AAPL", BULL, et(2026, 10, 6, 18, 0))], bars, run_at=et(2026, 10, 20, 10, 0)
    )
    assert {r.skip_reason for r in result.records} == {SkipReason.PRICE_UNAVAILABLE}
    assert len(result.records) == 2


def test_a_market_label_follows_the_fallback_lag_on_spy() -> None:
    """SPY's own first bar is held to the same bound, on both sides of it."""
    open_ = et(2026, 10, 7, 9, 30)
    published = et(2026, 10, 6, 18, 0)
    at_lag = _late_open_bars("SPY", open_ + FALLBACK_MAX_LAG)
    _, result = run([label(1, "MARKET", BULL, published)], at_lag)
    assert by_key(result)[(1, "1h")].reference_at == open_ + FALLBACK_MAX_LAG
    past_lag = _late_open_bars("SPY", open_ + FALLBACK_MAX_LAG + _MINUTE)
    _, result = run([label(1, "MARKET", BULL, published)], past_lag)
    assert (1, "1h") in result.awaiting_bars


def test_a_halted_open_moves_the_reference_to_the_first_trade_and_spy_with_it() -> None:
    """Out of session, first trade 09:55: reference_at is 09:55 and SPY is priced at 09:55.

    SPY's 09:30 open and its 09:55 bar (not complete until 09:56) are both
    wrong instants; the 09:54 bar's close is SPY at 09:55. 09:55 is inside
    FALLBACK_MAX_LAG of the open. The window's end is unchanged, so the
    window is honestly shorter.
    """
    published = et(2026, 10, 6, 18, 0)
    bars = [
        bar("AAPL", et(2026, 10, 7, 9, 55), "100", "101"),
        bar("AAPL", et(2026, 10, 7, 10, 29), "110", "110"),
        bar("SPY", et(2026, 10, 7, 9, 30), "400", "400"),
        bar("SPY", et(2026, 10, 7, 9, 54), "450", "500"),
        bar("SPY", et(2026, 10, 7, 9, 55), "999", "999"),
        bar("SPY", et(2026, 10, 7, 10, 29), "505", "505"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.reference_at == et(2026, 10, 7, 9, 55)
    assert record.reference_price == Decimal("100")
    assert record.outcome_at == et(2026, 10, 7, 10, 30)
    assert record.outcome_price == Decimal("110")
    assert record.benchmark_return == Decimal("0.010000000000")
    assert record.excess_return == Decimal("0.090000000000")


def test_a_fallback_reference_needs_spy_at_the_same_instant() -> None:
    """SPY's first bar a minute after the ticker's is a different instant: pending."""
    published = et(2026, 10, 6, 18, 0)
    bars = [
        bar("AAPL", et(2026, 10, 7, 9, 30), "100", "101"),
        bar("AAPL", et(2026, 10, 7, 10, 29), "110", "110"),
        bar("SPY", et(2026, 10, 7, 9, 31), "500", "500"),
        bar("SPY", et(2026, 10, 7, 10, 29), "505", "505"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    assert (1, "1h") in result.awaiting_bars


def test_past_the_first_minute_with_no_completed_bar_there_is_no_reference() -> None:
    """A gap after the first minute is not filled from a later bar -- that would look ahead."""
    published = et(2026, 10, 6, 9, 35)
    bars = [
        bar("AAPL", et(2026, 10, 6, 9, 40), "100", "100"),
        bar("AAPL", et(2026, 10, 6, 10, 34), "104", "104"),
        bar("SPY", et(2026, 10, 6, 9, 34), "500", "500"),
        bar("SPY", et(2026, 10, 6, 10, 34), "500", "500"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    assert (1, "1h") not in by_key(result)
    assert (1, "1h") in result.awaiting_bars


# --------------------------------------------------------------------- #
# The windows, in session minutes
# --------------------------------------------------------------------- #


def test_the_1h_window_across_a_close_lands_in_the_next_session() -> None:
    """Decision 12: 15:30 plus 60 session minutes is 10:00 the next session."""
    published = et(2026, 10, 6, 15, 30)
    assert window_end(published, "1h") == et(2026, 10, 7, 10, 0)
    bars = [
        bar("AAPL", et(2026, 10, 6, 15, 29), "100", "100"),
        bar("AAPL", et(2026, 10, 6, 16, 30), "300", "300"),
        bar("AAPL", et(2026, 10, 7, 9, 59), "95", "95"),
        bar("AAPL", et(2026, 10, 7, 10, 0), "300", "300"),
        bar("SPY", et(2026, 10, 6, 15, 29), "500", "500"),
        bar("SPY", et(2026, 10, 7, 9, 59), "500", "500"),
    ]
    _, result = run([label(1, "AAPL", BEAR, published)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.outcome_at == et(2026, 10, 7, 10, 0)
    assert record.outcome_price == Decimal("95")
    assert record.excess_return == Decimal("-0.050000000000")
    assert record.correct is True


def test_a_half_day() -> None:
    """2026-11-27 closes 13:00 ET. A 12:30 label's 1h ends Monday 10:00, its 1d Monday 15:30."""
    half_day_close = nyse_session_close(et(2026, 11, 27, 12, 0).date())
    assert half_day_close == et(2026, 11, 27, 13, 0)
    published = et(2026, 11, 27, 12, 30)
    assert window_end(published, "1h") == et(2026, 11, 30, 10, 0)
    assert window_end(published, "1d") == et(2026, 11, 30, 15, 30)
    bars = [
        bar("AAPL", et(2026, 11, 27, 12, 29), "200", "200"),
        bar("AAPL", et(2026, 11, 27, 13, 30), "999", "999"),
        bar("AAPL", et(2026, 11, 30, 9, 59), "202", "202"),
        bar("AAPL", et(2026, 11, 30, 15, 29), "190", "190"),
        bar("SPY", et(2026, 11, 27, 12, 29), "600", "600"),
        bar("SPY", et(2026, 11, 30, 9, 59), "600", "600"),
        bar("SPY", et(2026, 11, 30, 15, 29), "600", "600"),
    ]
    run_at = et(2026, 12, 5, 10, 0)
    _, result = run([label(1, "AAPL", BULL, published)], bars, run_at=run_at)
    records = by_key(result)
    assert records[(1, "1h")].outcome_price == Decimal("202")
    assert records[(1, "1h")].correct is True
    assert records[(1, "1d")].outcome_at == et(2026, 11, 30, 15, 30)
    assert records[(1, "1d")].outcome_price == Decimal("190")
    assert records[(1, "1d")].correct is False


# --------------------------------------------------------------------- #
# What is correct
# --------------------------------------------------------------------- #


def _two_point_bars(symbol: str, ref: str, out: str) -> list[Bar]:
    """A reference at Tuesday 10:15 (the 10:14 bar) and a 1h outcome at 11:15."""
    return [
        bar(symbol, et(2026, 10, 6, 10, 14), ref, ref),
        bar(symbol, et(2026, 10, 6, 11, 14), out, out),
    ]


_TUESDAY_1015 = et(2026, 10, 6, 10, 15)


def test_excess_return_is_the_tickers_return_less_spys() -> None:
    """AAPL +2%, SPY +1%: excess +1%. A bullish label is right, a bearish one wrong."""
    bars = _two_point_bars("AAPL", "100", "102") + _two_point_bars("SPY", "500", "505")
    _, result = run(
        [label(1, "AAPL", BULL, _TUESDAY_1015), label(2, "AAPL", BEAR, _TUESDAY_1015)], bars
    )
    records = by_key(result)
    for key in ((1, "1h"), (2, "1h")):
        assert records[key].benchmark_return == Decimal("0.010000000000")
        assert records[key].excess_return == Decimal("0.010000000000")
        assert records[key].scope is Scope.WATCH
    assert records[(1, "1h")].correct is True
    assert records[(2, "1h")].correct is False


def test_a_rise_smaller_than_spys_is_not_credited_to_a_bullish_label() -> None:
    """Q5: raw return would credit beta. AAPL +0.5% under SPY +1% is a wrong bullish call."""
    bars = _two_point_bars("AAPL", "100", "100.5") + _two_point_bars("SPY", "500", "505")
    _, result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.excess_return == Decimal("-0.005000000000")
    assert record.correct is False


def test_a_market_label_grades_against_spys_raw_return() -> None:
    """``benchmark_return`` and ``excess_return`` both hold SPY's own return."""
    bars = _two_point_bars("SPY", "500", "495")
    _, result = run([label(1, "MARKET", BEAR, _TUESDAY_1015)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.reference_price == Decimal("500")
    assert record.outcome_price == Decimal("495")
    assert record.benchmark_return == Decimal("-0.010000000000")
    assert record.excess_return == record.benchmark_return
    assert record.correct is True


def test_market_is_watch_even_when_the_universe_omits_it() -> None:
    """``MARKET`` cannot pass a tradeability filter; it must never be skipped as untradeable."""
    bars = _two_point_bars("SPY", "500", "505")
    _, result = run([label(1, "MARKET", BULL, _TUESDAY_1015)], bars, watch=("AAPL",))
    record = by_key(result)[(1, "1h")]
    assert record.skip_reason is None
    assert record.scope is Scope.WATCH
    assert record.correct is True


def test_a_label_on_spy_itself_grades_raw_like_market() -> None:
    """SPY in excess of SPY is zero by construction, which would make every SPY label wrong."""
    bars = _two_point_bars("SPY", "500", "505")
    _, result = run([label(1, "SPY", BULL, _TUESDAY_1015)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.excess_return == record.benchmark_return == Decimal("0.010000000000")
    assert record.correct is True


def test_an_exact_zero_excess_is_incorrect_for_either_direction() -> None:
    """The label claimed a move that did not happen -- and never a NULL ``correct``."""
    bars = _two_point_bars("AAPL", "100", "101") + _two_point_bars("SPY", "500", "505")
    _, result = run(
        [label(1, "AAPL", BULL, _TUESDAY_1015), label(2, "AAPL", BEAR, _TUESDAY_1015)], bars
    )
    records = by_key(result)
    for key in ((1, "1h"), (2, "1h")):
        assert records[key].excess_return == Decimal("0E-12")
        assert records[key].correct is False


def test_neutral_is_graded_for_the_record_with_correct_null() -> None:
    bars = _two_point_bars("AAPL", "100", "102") + _two_point_bars("SPY", "500", "505")
    _, result = run([label(1, "AAPL", NEUTRAL, _TUESDAY_1015)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.skip_reason is None
    assert record.reference_price == Decimal("100")
    assert record.excess_return == Decimal("0.010000000000")
    assert record.correct is None


def test_every_graded_directional_record_carries_a_correct_verdict() -> None:
    """6.1: a directional graded row with NULL ``correct`` drops out of every count."""
    rng = random.Random(6_2)
    labels = []
    bars: list[Bar] = []
    for i in range(40):
        ticker = f"T{i:02d}"
        labels.append(label(i, ticker, BULL if i % 2 else BEAR, _TUESDAY_1015))
        ref = Decimal(rng.randint(5_00, 500_00)) / 100
        out = ref + Decimal(rng.randint(-200, 200)) / 100
        bars += _two_point_bars(ticker, str(ref), str(out))
    bars += _two_point_bars("SPY", "500", "501")
    watch = {f"T{i:02d}" for i in range(40)}
    _, result = run(labels, bars, watch=watch)
    graded = [r for r in result.records if r.window == "1h"]
    assert len(graded) == 40
    assert all(r.correct is not None for r in graded)


# --------------------------------------------------------------------- #
# Returns are Decimal, quantized to a fixed exponent
# --------------------------------------------------------------------- #


def test_returns_are_quantized_to_twelve_places_half_even() -> None:
    """1/3 - 1 has 28 significant digits unquantized; stored, it has twelve places."""
    bars = _two_point_bars("AAPL", "3", "1") + _two_point_bars("SPY", "3", "2")
    _, result = run([label(1, "AAPL", BEAR, _TUESDAY_1015)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.benchmark_return == Decimal("-0.333333333333")
    # -0.666666666667 - (-0.333333333333), each quantized first.
    assert record.excess_return == Decimal("-0.333333333334")
    assert record.benchmark_return is not None and record.excess_return is not None
    assert record.benchmark_return.as_tuple().exponent == -12
    assert record.excess_return.as_tuple().exponent == -12
    assert isinstance(record.excess_return, Decimal)


def test_an_exact_half_rounds_to_even() -> None:
    """2 -> 2.000000000001 is +5E-13 exactly: half-even stores 0E-12, half-up 1E-12.

    So a bullish label is wrong here; under ``ROUND_HALF_UP`` it would be right.
    """
    bars = _two_point_bars("AAPL", "2", "2.000000000001") + _two_point_bars("SPY", "500", "500")
    _, result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.excess_return == Decimal("0E-12")
    assert record.correct is False


def test_no_stored_return_is_negative_zero() -> None:
    """A tiny fall quantizes to -0E-12; stored, it reads 0, never "-0"."""
    bars = _two_point_bars("AAPL", "100", "100") + _two_point_bars(
        "SPY", "500", "499.9999999999999"
    )
    _, result = run(
        [label(1, "AAPL", BULL, _TUESDAY_1015), label(2, "MARKET", BEAR, _TUESDAY_1015)], bars
    )
    records = by_key(result)
    for key in ((1, "1h"), (2, "1h")):
        record = records[key]
        assert record.benchmark_return is not None and record.excess_return is not None
        assert format(record.benchmark_return, "f") == "0.000000000000"
        assert format(record.excess_return, "f") == "0.000000000000"
        assert not record.excess_return.is_signed()
    # And the ticker side: a tiny fall against a flat SPY.
    bars = _two_point_bars("AAPL", "100", "99.99999999999995") + _two_point_bars(
        "SPY", "500", "500"
    )
    _, result = run([label(3, "AAPL", BEAR, _TUESDAY_1015)], bars)
    excess = by_key(result)[(3, "1h")].excess_return
    assert excess is not None and format(excess, "f") == "0.000000000000"


def test_the_widest_return_fits_moneys_forty_characters() -> None:
    """The widest case a price pair can produce, written as ``Money`` writes it.

    A ticker going from three hundredths of a cent to $99,999,999.99 while SPY
    loses almost everything: a twelve-digit integer part and twelve places.
    """
    bars = _two_point_bars("AAPL", "0.0003", "99999999.99") + _two_point_bars(
        "SPY", "99999999.99", "0.0003"
    )
    _, result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.excess_return is not None and record.benchmark_return is not None
    excess_text = format(record.excess_return, "f")
    benchmark_text = format(record.benchmark_return, "f")
    assert excess_text == "333333333299.999999999997"
    assert benchmark_text == "-0.999999999997"
    assert len(excess_text) <= 40 and len(benchmark_text) <= 40
    # And the small end: a tiny negative excess keeps its sign and its places.
    bars = _two_point_bars("AAPL", "300000", "300000.01") + _two_point_bars(
        "SPY", "300000", "300000.02"
    )
    _, result = run([label(2, "AAPL", BULL, _TUESDAY_1015)], bars)
    small = by_key(result)[(2, "1h")]
    # 0.000000033333 - 0.000000066667, each quantized before subtracting.
    assert small.excess_return == Decimal("-0.000000033334")
    assert small.excess_return is not None
    assert len(format(small.excess_return, "f")) <= 40
    assert small.correct is False


# --------------------------------------------------------------------- #
# Maturity and missing prices
# --------------------------------------------------------------------- #


def test_a_window_is_graded_only_fifteen_minutes_after_it_ends() -> None:
    """SIP historical is free past 15 minutes; the two windows mature independently."""
    published = et(2026, 10, 6, 10, 15)
    end_1h = et(2026, 10, 6, 11, 15)
    bars = flat_session("AAPL", published, "100") + flat_session("SPY", published, "500")
    bars += flat_session("AAPL", et(2026, 10, 7, 9, 30), "100")
    bars += flat_session("SPY", et(2026, 10, 7, 9, 30), "500")
    early = end_1h + MATURITY_DELAY - timedelta(seconds=1)
    plan, result = run([label(1, "AAPL", BULL, published)], bars, run_at=early)
    assert plan.to_grade == () and result.records == ()
    plan, result = run([label(1, "AAPL", BULL, published)], bars, run_at=end_1h + MATURITY_DELAY)
    assert set(by_key(result)) == {(1, "1h")}


def test_a_missing_bar_within_seven_days_is_retried_not_recorded() -> None:
    """A transient vendor gap: no record, so next run grades it."""
    published = _TUESDAY_1015
    bars = _two_point_bars("SPY", "500", "505")  # AAPL has no bars at all
    end = window_end(published, "1h")
    run_at = end + PRICE_UNAVAILABLE_AFTER
    _, result = run([label(1, "AAPL", BULL, published)], bars, run_at=run_at)
    assert (1, "1h") not in by_key(result)
    assert (1, "1h") in result.awaiting_bars


def test_a_missing_bar_past_seven_days_is_price_unavailable() -> None:
    published = _TUESDAY_1015
    bars = _two_point_bars("SPY", "500", "505")
    end = window_end(published, "1h")
    run_at = end + PRICE_UNAVAILABLE_AFTER + timedelta(seconds=1)
    _, result = run([label(1, "AAPL", BULL, published)], bars, run_at=run_at)
    record = by_key(result)[(1, "1h")]
    assert record.skip_reason is SkipReason.PRICE_UNAVAILABLE
    assert record.scope is Scope.WATCH
    assert record.reference_at is None and record.reference_price is None
    assert record.outcome_at is None and record.outcome_price is None
    assert record.benchmark_return is None and record.excess_return is None
    assert record.correct is None
    # The 1d window ended a day later, so it is still inside the seven days.
    assert result.awaiting_bars == ((1, "1d"),)


def test_a_missing_spy_bar_is_never_graded_on_the_tickers_raw_return() -> None:
    """6.1: without SPY the row is ``price_unavailable``, not a beta-crediting grade."""
    bars = _two_point_bars("AAPL", "100", "102")  # SPY has no bars at all
    end = window_end(_TUESDAY_1015, "1h")
    _, result = run(
        [label(1, "AAPL", BULL, _TUESDAY_1015)],
        bars,
        run_at=end + PRICE_UNAVAILABLE_AFTER + timedelta(days=1),
    )
    assert by_key(result)[(1, "1h")].skip_reason is SkipReason.PRICE_UNAVAILABLE


def test_an_outcome_bar_from_an_earlier_session_is_not_used() -> None:
    """The window ends Wednesday 10:00; Tuesday's last bar is not Wednesday's price."""
    published = et(2026, 10, 6, 15, 30)
    bars = [
        bar("AAPL", et(2026, 10, 6, 15, 29), "100", "100"),
        bar("AAPL", et(2026, 10, 6, 15, 59), "101", "101"),
        bar("AAPL", et(2026, 10, 7, 10, 0), "102", "102"),
        bar("SPY", et(2026, 10, 6, 15, 29), "500", "500"),
        bar("SPY", et(2026, 10, 7, 9, 59), "500", "500"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    assert (1, "1h") in result.awaiting_bars


def test_a_window_ending_inside_a_sessions_first_minute_is_priced_at_the_open() -> None:
    """Decision 3 mirrors decision 2: 09:30:30 has no completed bar, so the open bar's open.

    A label at 15:00:30 has its 1h window end at 09:30:30 the next session.
    The 09:30 bar ends at 09:31, so its close would be a look-ahead; its open
    is the price at the open, the same rule the reference side uses.
    """
    published = et(2026, 10, 6, 15, 0, 30)
    assert window_end(published, "1h") == et(2026, 10, 7, 9, 30, 30)
    bars = [
        bar("AAPL", et(2026, 10, 6, 14, 59), "100", "100"),
        bar("AAPL", et(2026, 10, 7, 9, 30), "103", "150"),
        bar("SPY", et(2026, 10, 6, 14, 59), "500", "500"),
        bar("SPY", et(2026, 10, 7, 9, 30), "500", "900"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    record = by_key(result)[(1, "1h")]
    assert record.outcome_at == et(2026, 10, 7, 9, 30, 30)
    assert record.outcome_price == Decimal("103")
    assert record.benchmark_return == Decimal("0E-12")
    assert record.excess_return == Decimal("0.030000000000")
    assert record.correct is True


def test_a_first_minute_outcome_needs_a_bar_stamped_at_the_open() -> None:
    """A first bar at 09:31 is not the price at 09:30:30 -- pending, not guessed."""
    published = et(2026, 10, 6, 15, 0, 30)
    bars = [
        bar("AAPL", et(2026, 10, 6, 14, 59), "100", "100"),
        bar("AAPL", et(2026, 10, 7, 9, 31), "103", "103"),
        bar("SPY", et(2026, 10, 6, 14, 59), "500", "500"),
        bar("SPY", et(2026, 10, 7, 9, 30), "500", "500"),
    ]
    _, result = run([label(1, "AAPL", BULL, published)], bars)
    assert (1, "1h") in result.awaiting_bars


# --------------------------------------------------------------------- #
# Vendor-data faults quarantine one (symbol, session), never the audit
# --------------------------------------------------------------------- #


_TUESDAY = date(2026, 10, 6)


def test_a_bad_bar_on_one_symbol_still_lets_the_others_grade() -> None:
    bars = (
        _two_point_bars("AAPL", "0", "1")
        + _two_point_bars("MSFT", "100", "102")
        + _two_point_bars("SPY", "500", "505")
    )
    _, result = run(
        [label(1, "AAPL", BULL, _TUESDAY_1015), label(2, "MSFT", BULL, _TUESDAY_1015)], bars
    )
    assert (1, "1h") in result.awaiting_bars
    assert by_key(result)[(2, "1h")].correct is True
    assert len(result.bar_faults) == 1
    fault = result.bar_faults[0]
    assert isinstance(fault, BarFault)
    assert (fault.symbol, fault.session_date) == ("AAPL", _TUESDAY)
    assert "non-positive" in fault.reason


def test_a_bar_never_read_still_quarantines_its_session() -> None:
    """The 12:00 bar is read by no lookup, but a session with a zero in it is not trusted."""
    bars = (
        _two_point_bars("AAPL", "100", "102")
        + [bar("AAPL", et(2026, 10, 6, 12, 0), "100", "0")]
        + _two_point_bars("SPY", "500", "505")
    )
    _, result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], bars)
    assert (1, "1h") in result.awaiting_bars
    assert [(f.symbol, f.session_date) for f in result.bar_faults] == [("AAPL", _TUESDAY)]


def test_a_bad_spy_bar_in_one_session_leaves_other_sessions_graded() -> None:
    thursday_1015 = et(2026, 10, 8, 10, 15)
    bars = _two_point_bars("AAPL", "100", "102") + _two_point_bars("SPY", "500", "505")
    bars.append(bar("SPY", et(2026, 10, 6, 12, 0), "0", "500"))
    bars += [
        bar("AAPL", et(2026, 10, 8, 10, 14), "100", "100"),
        bar("AAPL", et(2026, 10, 8, 11, 14), "103", "103"),
        bar("SPY", et(2026, 10, 8, 10, 14), "500", "500"),
        bar("SPY", et(2026, 10, 8, 11, 14), "505", "505"),
    ]
    labels = [label(1, "AAPL", BULL, _TUESDAY_1015), label(2, "AAPL", BULL, thursday_1015)]
    _, result = run(labels, bars)
    assert (1, "1h") in result.awaiting_bars
    assert by_key(result)[(2, "1h")].excess_return == Decimal("0.020000000000")
    assert [(f.symbol, f.session_date) for f in result.bar_faults] == [("SPY", _TUESDAY)]


def test_an_identical_duplicate_bar_is_collapsed() -> None:
    bars = _two_point_bars("AAPL", "100", "102") * 2 + _two_point_bars("SPY", "500", "505")
    _, result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], bars)
    assert by_key(result)[(1, "1h")].excess_return == Decimal("0.010000000000")
    assert result.bar_faults == ()


def test_conflicting_duplicate_bars_quarantine_the_session() -> None:
    bars = _two_point_bars("AAPL", "100", "102") + _two_point_bars("SPY", "500", "505")
    bars.append(bar("AAPL", et(2026, 10, 6, 10, 14), "100", "999"))
    _, result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], bars)
    assert (1, "1h") in result.awaiting_bars
    assert len(result.bar_faults) == 1
    assert "conflicting" in result.bar_faults[0].reason
    # The input order does not change the fault reported.
    _, reversed_result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], list(reversed(bars)))
    assert reversed_result == result


def test_an_extended_hours_zero_bar_is_ignored() -> None:
    """A 04:00 pre-market zero is never read, never validated, never reported."""
    bars = _two_point_bars("AAPL", "100", "102") + _two_point_bars("SPY", "500", "505")
    bars += [
        bar("AAPL", et(2026, 10, 6, 4, 0), "0", "0"),
        bar("SPY", et(2026, 10, 6, 19, 59), "0", "0"),
        bar("SPY", et(2026, 10, 10, 10, 14), "0", "0"),  # a Saturday
    ]
    _, result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], bars)
    assert by_key(result)[(1, "1h")].correct is True
    assert result.bar_faults == ()


def test_a_quarantined_window_becomes_price_unavailable_after_seven_days() -> None:
    bars = _two_point_bars("AAPL", "0", "1") + _two_point_bars("SPY", "500", "505")
    end = window_end(_TUESDAY_1015, "1h")
    labels = [label(1, "AAPL", BULL, _TUESDAY_1015)]
    _, inside = run(labels, bars, run_at=end + PRICE_UNAVAILABLE_AFTER)
    assert (1, "1h") in inside.awaiting_bars
    _, after = run(labels, bars, run_at=end + PRICE_UNAVAILABLE_AFTER + timedelta(seconds=1))
    assert by_key(after)[(1, "1h")].skip_reason is SkipReason.PRICE_UNAVAILABLE
    assert [(f.symbol, f.session_date) for f in after.bar_faults] == [("AAPL", _TUESDAY)]


def test_a_non_finite_bar_price_is_a_fault_not_an_exception() -> None:
    # Built by hand: the ``bar`` helper's max()/min() would itself raise on NaN.
    nan_bar = Bar(
        symbol="AAPL",
        at=et(2026, 10, 6, 10, 14),
        open=Decimal("NaN"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=1000,
        trade_count=10,
        vwap=None,
    )
    bars = [nan_bar, bar("AAPL", et(2026, 10, 6, 11, 14), "100", "100")]
    bars += _two_point_bars("SPY", "500", "505")
    _, result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], bars)
    assert (1, "1h") in result.awaiting_bars
    assert [(f.symbol, f.session_date) for f in result.bar_faults] == [("AAPL", _TUESDAY)]
    assert "non-finite" in result.bar_faults[0].reason


def test_a_return_too_wide_for_money_is_a_fault_not_an_exception() -> None:
    """Prices 1E-30 to 1E+30: a return no ``Money`` column can hold. Pending, then reported."""
    bars = _two_point_bars("AAPL", "1E-30", "1E+30") + _two_point_bars("SPY", "500", "505")
    bars += _two_point_bars("MSFT", "100", "102")
    labels = [label(1, "AAPL", BULL, _TUESDAY_1015), label(2, "MSFT", BULL, _TUESDAY_1015)]
    _, result = run(labels, bars)
    assert (1, "1h") in result.awaiting_bars
    assert by_key(result)[(2, "1h")].correct is True
    assert [(f.symbol, f.session_date) for f in result.bar_faults] == [("AAPL", _TUESDAY)]
    assert "cannot be stored" in result.bar_faults[0].reason


# --------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------- #


def test_re_running_a_completed_audit_is_a_no_op() -> None:
    labels = [
        label(1, "AAPL", BULL, _TUESDAY_1015),
        label(2, "NOPE", BEAR, _TUESDAY_1015),
        label(3, "MARKET", NEUTRAL, _TUESDAY_1015),
    ]
    bars = flat_session("AAPL", _TUESDAY_1015, "100") + flat_session("SPY", _TUESDAY_1015, "500")
    bars += flat_session("AAPL", et(2026, 10, 7, 9, 30), "100")
    bars += flat_session("SPY", et(2026, 10, 7, 9, 30), "500")
    tradeable = {"NOPE": False}
    _, first = run(labels, bars, tradeable=tradeable)
    assert len(first.records) == 6
    written = [(r.label_id, r.window) for r in first.records]
    plan, second = run(labels, bars, tradeable=tradeable, written=written)
    assert plan.to_grade == () and plan.skipped == () and plan.bar_requests == ()
    assert second.records == () and second.awaiting_bars == ()


def test_a_half_written_label_emits_only_its_missing_window() -> None:
    bars = flat_session("AAPL", _TUESDAY_1015, "100") + flat_session("SPY", _TUESDAY_1015, "500")
    bars += flat_session("AAPL", et(2026, 10, 7, 9, 30), "100")
    bars += flat_session("SPY", et(2026, 10, 7, 9, 30), "500")
    _, result = run([label(1, "AAPL", BULL, _TUESDAY_1015)], bars, written=[(1, "1h")])
    assert set(by_key(result)) == {(1, "1d")}


def test_the_output_does_not_depend_on_input_order() -> None:
    labels = [label(i, t, BULL, _TUESDAY_1015) for i, t in enumerate(["AAPL", "MSFT", "X", "Y"])]
    bars = []
    for symbol in ("AAPL", "MSFT", "X", "SPY"):
        bars += flat_session(symbol, _TUESDAY_1015, "100")
        bars += flat_session(symbol, et(2026, 10, 7, 9, 30), "101")
    tradeable = {"X": True, "Y": False}
    first_plan, first = run(labels, bars, tradeable=tradeable)
    shuffled_labels = list(reversed(labels))
    shuffled_bars = list(bars)
    random.Random(7).shuffle(shuffled_bars)
    second_plan, second = run(shuffled_labels, shuffled_bars, tradeable=tradeable)
    assert first_plan == second_plan
    assert first == second


def test_a_duplicate_label_id_is_refused() -> None:
    with pytest.raises(ValueError, match="label_id"):
        run([label(1, "AAPL", BULL, _TUESDAY_1015), label(1, "MSFT", BULL, _TUESDAY_1015)], [])


# --------------------------------------------------------------------- #
# What is graded: watch, discovery and the cap
# --------------------------------------------------------------------- #


def _skips(result: GradeResult, reason: SkipReason) -> set[int]:
    return {r.label_id for r in result.records if r.skip_reason is reason}


def test_an_untradeable_off_watch_label_is_skipped_for_both_windows() -> None:
    _, result = run([label(1, "PENNY", BULL, _TUESDAY_1015)], [], tradeable={"PENNY": False})
    records = by_key(result)
    assert set(records) == {(1, "1h"), (1, "1d")}
    for record in records.values():
        assert record.skip_reason is SkipReason.OFF_WATCH_UNTRADEABLE
        assert record.reference_at is None and record.reference_price is None
        assert record.benchmark_return is None and record.correct is None
        assert record.scope is None


def test_an_off_watch_ticker_with_no_verdict_stays_pending_and_is_named() -> None:
    """No permanent skip on the absence of information."""
    labels = [label(1, "UNSEEN", BULL, _TUESDAY_1015), label(2, "UNSEEN", NEUTRAL, _TUESDAY_1015)]
    plan, result = run(labels, [])
    assert result.records == ()
    assert plan.unverdicted_tickers == ("UNSEEN",)
    assert plan.unverdicted_label_counts == (("UNSEEN", 2),)


def test_an_unverdicted_label_is_pending_until_seven_days_past_its_last_window() -> None:
    """Then ``off_watch_untradeable``: it was never shown to pass decision 21's filter."""
    labels = [label(1, "UNSEEN", BULL, _TUESDAY_1015)]
    last_end = window_end(_TUESDAY_1015, "1d")
    plan, result = run(labels, [], run_at=last_end + PRICE_UNAVAILABLE_AFTER)
    assert result.records == ()
    assert plan.unverdicted_label_counts == (("UNSEEN", 1),)
    after = last_end + PRICE_UNAVAILABLE_AFTER + timedelta(seconds=1)
    plan, result = run(labels, [], run_at=after)
    records = by_key(result)
    assert set(records) == {(1, "1h"), (1, "1d")}
    assert all(r.skip_reason is SkipReason.OFF_WATCH_UNTRADEABLE for r in records.values())
    assert all(r.scope is None and r.reference_at is None for r in records.values())
    assert plan.unverdicted_tickers == ("UNSEEN",)
    assert plan.bar_requests == ()


def test_an_unverdicted_label_waits_on_its_unwritten_window_only() -> None:
    """Only 1h left unwritten: seven days past the 1h end is enough."""
    labels = [label(1, "UNSEEN", BULL, _TUESDAY_1015)]
    end_1h = window_end(_TUESDAY_1015, "1h")
    run_at = end_1h + PRICE_UNAVAILABLE_AFTER + timedelta(seconds=1)
    _, result = run(labels, [], written=[(1, "1d")], run_at=run_at)
    assert set(by_key(result)) == {(1, "1h")}


def test_a_membership_skip_covers_the_window_that_has_not_matured() -> None:
    """Decision 10: 1h matured, 1d not -- both get the skip, untradeable or capped."""
    end_1h = window_end(_TUESDAY_1015, "1h")
    early = end_1h + MATURITY_DELAY
    assert window_end(_TUESDAY_1015, "1d") + MATURITY_DELAY > early
    _, result = run(
        [label(1, "PENNY", BULL, _TUESDAY_1015)], [], tradeable={"PENNY": False}, run_at=early
    )
    assert {k: r.skip_reason for k, r in by_key(result).items()} == {
        (1, "1h"): SkipReason.OFF_WATCH_UNTRADEABLE,
        (1, "1d"): SkipReason.OFF_WATCH_UNTRADEABLE,
    }
    counts = {f"D{i:03d}": 1 for i in range(251)}
    tradeable = {ticker: True for ticker in counts}
    plan, result = run(_discovery_labels(counts), [], tradeable=tradeable, run_at=early)
    assert plan.discovery_capped_tickers == ("D250",)
    capped = {(r.label_id, r.window) for r in result.records if r.skip_reason is not None}
    assert capped == {(250, "1h"), (250, "1d")}
    assert {g.window for g in plan.to_grade} == {"1h"}


def test_a_tradeable_off_watch_label_is_graded_as_discovery() -> None:
    bars = _two_point_bars("XYZ", "10", "11") + _two_point_bars("SPY", "500", "505")
    _, result = run([label(1, "XYZ", BULL, _TUESDAY_1015)], bars, tradeable={"XYZ": True})
    record = by_key(result)[(1, "1h")]
    assert record.scope is Scope.DISCOVERY
    assert record.correct is True


def test_a_neutral_label_on_a_selected_discovery_ticker_is_graded_as_discovery() -> None:
    """Decision 12: neutral rides with its ticker -- graded for the record, ``correct`` NULL."""
    bars = _two_point_bars("XYZ", "10", "11") + _two_point_bars("SPY", "500", "505")
    _, result = run(
        [label(1, "XYZ", BULL, _TUESDAY_1015), label(2, "XYZ", NEUTRAL, _TUESDAY_1015)],
        bars,
        tradeable={"XYZ": True},
    )
    record = by_key(result)[(2, "1h")]
    assert record.scope is Scope.DISCOVERY
    assert record.skip_reason is None
    assert record.excess_return == Decimal("0.090000000000")
    assert record.correct is None


def test_the_watch_universe_is_uncapped() -> None:
    watch = {f"W{i:03d}" for i in range(300)}
    labels = [label(i, f"W{i:03d}", BULL, _TUESDAY_1015) for i in range(300)]
    plan, _ = run(labels, [], watch=watch)
    assert len({g.label.ticker for g in plan.to_grade}) == 300
    assert plan.discovery_capped_tickers == ()


def _discovery_labels(counts: Mapping[str, int]) -> list[AuditLabel]:
    labels = []
    next_id = 0
    for ticker, count in counts.items():
        for _ in range(count):
            labels.append(label(next_id, ticker, BULL, _TUESDAY_1015))
            next_id += 1
    return labels


def test_251_tied_discovery_tickers_grade_250_and_the_last_symbol_is_capped() -> None:
    counts = {f"D{i:03d}": 1 for i in range(251)}
    tradeable = {ticker: True for ticker in counts}
    plan, result = run(_discovery_labels(counts), [], tradeable=tradeable)
    graded_tickers = {g.label.ticker for g in plan.to_grade}
    assert len(graded_tickers) == DISCOVERY_TICKER_CAP == 250
    assert "D250" not in graded_tickers
    assert plan.discovery_capped_tickers == ("D250",)
    capped = [r for r in result.records if r.skip_reason is SkipReason.DISCOVERY_CAP]
    assert {(r.label_id, r.window) for r in capped} == {(250, "1h"), (250, "1d")}
    assert all(r.reference_price is None and r.scope is None for r in capped)


def test_the_cap_ranks_by_directional_label_count_before_symbol() -> None:
    """``AAA`` sorts first by symbol but has one label to everyone else's two."""
    counts = {f"D{i:03d}": 2 for i in range(250)}
    counts["AAA"] = 1
    tradeable = {ticker: True for ticker in counts}
    plan, _ = run(_discovery_labels(counts), [], tradeable=tradeable)
    assert plan.discovery_capped_tickers == ("AAA",)
    assert "AAA" not in {g.label.ticker for g in plan.to_grade}


def test_neutral_labels_do_not_count_toward_the_cap_ranking() -> None:
    """Ranked on *directional* counts: three neutrals lose to one bullish."""
    counts = {f"D{i:03d}": 1 for i in range(250)}
    labels = _discovery_labels(counts)
    labels += [label(1000 + i, "AAA", NEUTRAL, _TUESDAY_1015) for i in range(3)]
    tradeable = {ticker: True for ticker in [*counts, "AAA"]}
    plan, result = run(labels, [], tradeable=tradeable)
    assert plan.discovery_capped_tickers == ("AAA",)
    assert _skips(result, SkipReason.DISCOVERY_CAP) == {1000, 1001, 1002}


def test_watch_labels_do_not_consume_discovery_slots() -> None:
    counts = {f"D{i:03d}": 1 for i in range(250)}
    labels = _discovery_labels(counts) + [label(5000, "AAPL", BULL, _TUESDAY_1015)]
    tradeable = {ticker: True for ticker in counts}
    plan, _ = run(labels, [], tradeable=tradeable)
    assert plan.discovery_capped_tickers == ()
    assert len({g.label.ticker for g in plan.to_grade}) == 251


# --------------------------------------------------------------------- #
# The bars the runner must fetch
# --------------------------------------------------------------------- #


def _in_requests(bars: Iterable[Bar], requests: Iterable[BarRequest]) -> list[Bar]:
    return [
        b
        for b in bars
        if any(r.symbol == b.symbol and r.start <= b.at < r.end for r in requests)
    ]


def test_the_bar_requests_cover_every_instant_the_grader_reads() -> None:
    """Grading on only the requested bars gives exactly the answer all the bars give."""
    rng = random.Random(1206)
    days = [et(2026, 10, d, 9, 30) for d in (5, 6, 7, 8, 9)] + [et(2026, 10, 12, 9, 30)]
    symbols = ("AAPL", "MSFT", "XYZ", "SPY")
    bars: list[Bar] = []
    for symbol in symbols:
        price = Decimal(rng.randint(50, 500))
        for day in days:
            start = day.astimezone(NYSE_TZ).replace(hour=4, minute=0)
            for i in range(16 * 60):
                step = Decimal(rng.randint(-20, 20)) / 100
                open_ = price
                price = max(Decimal("1"), price + step)
                at = (start + i * _MINUTE).astimezone(timezone.utc)
                bars.append(bar(symbol, at, str(open_), str(price)))
    published = [
        et(2026, 10, 5, 7, 0),  # pre-market
        et(2026, 10, 5, 9, 30, 30),  # first minute
        et(2026, 10, 5, 12, 0),
        et(2026, 10, 6, 15, 30),  # 1h across the close
        et(2026, 10, 7, 18, 0),  # after hours
        et(2026, 10, 9, 15, 45),  # across the weekend
        et(2026, 10, 10, 9, 0),  # Saturday
    ]
    labels = []
    for i, at in enumerate(published):
        for j, ticker in enumerate(("AAPL", "XYZ", "MARKET")):
            labels.append(label(10 * i + j, ticker, BULL, at, at + timedelta(minutes=j)))
    run_at = et(2026, 10, 17, 10, 0)
    plan, everything = run(labels, bars, tradeable={"XYZ": True}, run_at=run_at)
    assert everything.awaiting_bars == ()
    assert len(everything.records) == len(labels) * 2
    # Label 10 is 09:30:30: its 1d window ends inside the next session's first
    # minute, priced at the open bar's open (decision 3).
    unpriced = {(r.label_id, r.window) for r in everything.records if r.skip_reason is not None}
    assert unpriced == set()
    assert everything.bar_faults == ()
    labels_by_id = {item.label_id: item for item in labels}
    for record in everything.records:
        assert record.reference_at is not None
        assert record.reference_at >= reference_instant(labels_by_id[record.label_id])
    requested = _in_requests(bars, plan.bar_requests)
    assert len(requested) < len(bars)
    assert grade(plan, requested) == everything
    assert {r.symbol for r in plan.bar_requests} == {"AAPL", "XYZ", "SPY"}


def test_bar_requests_are_merged_and_sorted_per_symbol() -> None:
    labels = [
        label(1, "AAPL", BULL, et(2026, 10, 6, 10, 0)),
        label(2, "AAPL", BULL, et(2026, 10, 6, 10, 30)),
    ]
    plan, _ = run(labels, [])
    tuesday, wednesday = date(2026, 10, 6), date(2026, 10, 7)
    tuesday_open, wednesday_open = nyse_session_open(tuesday), nyse_session_open(wednesday)
    tuesday_close = nyse_session_close(tuesday)
    assert tuesday_open is not None and wednesday_open is not None
    assert tuesday_close is not None
    # Never one range across the overnight: the reference session up to its
    # close, and the outcome session up to the latest window end in it.
    assert plan.bar_requests == (
        BarRequest("AAPL", tuesday_open, tuesday_close),
        BarRequest("AAPL", wednesday_open, et(2026, 10, 7, 10, 30)),
        BarRequest("SPY", tuesday_open, tuesday_close),
        BarRequest("SPY", wednesday_open, et(2026, 10, 7, 10, 30)),
    )


def test_a_same_session_window_requests_only_up_to_its_end() -> None:
    end = et(2026, 10, 6, 11, 15)
    plan, _ = run([label(1, "AAPL", BULL, _TUESDAY_1015)], [], written=[(1, "1d")])
    open_ = nyse_session_open(date(2026, 10, 6))
    assert open_ is not None
    assert plan.bar_requests == (BarRequest("AAPL", open_, end), BarRequest("SPY", open_, end))


def test_a_market_label_requests_spy_alone() -> None:
    plan, _ = run([label(1, "MARKET", BULL, _TUESDAY_1015)], [])
    assert {r.symbol for r in plan.bar_requests} == {"SPY"}


def test_skipped_labels_request_no_bars() -> None:
    plan, _ = run([label(1, "PENNY", BULL, _TUESDAY_1015)], [], tradeable={"PENNY": False})
    assert plan.bar_requests == ()


def test_naive_instants_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run([label(1, "AAPL", BULL, datetime(2026, 10, 6, 14, 15))], [])
    with pytest.raises(ValueError, match="timezone-aware"):
        run([], [], run_at=datetime(2026, 10, 10, 14, 0))
    # A naive published_at beside an aware labeled_at is refused too, even
    # though published_at rarely moves the reference.
    with pytest.raises(ValueError, match="published_at must be timezone-aware"):
        run([label(1, "AAPL", BULL, datetime(2026, 10, 6, 14, 15), _TUESDAY_1015)], [])


# --------------------------------------------------------------------- #
# Accuracy and transitions (unit 6.3, decision 13) -- pure
# --------------------------------------------------------------------- #

#: Every transition test's demotion instant, comfortably inside the trailing
#: window so trailing and since-demotion outcomes can be told apart.
DEMOTED_AT = RUN_AT - timedelta(days=10)

#: A reference instant inside both the trailing window and the since-demotion
#: span.
RECENT = RUN_AT - timedelta(days=2)


def outcomes(
    window: str,
    graded: int,
    correct: int,
    *,
    scope: Scope = Scope.WATCH,
    at: datetime = RECENT,
    neutral: int = 0,
    source: str = "massive",
) -> list[GradedOutcome]:
    """``graded`` directional outcomes of which ``correct`` were right, plus
    ``neutral`` neutral ones, all in one window, scope and instant."""
    assert 0 <= correct <= graded
    made = [
        GradedOutcome(
            source=source,
            window=window,
            scope=scope,
            reference_at=at,
            correct=i < correct,
            neutral=False,
        )
        for i in range(graded)
    ]
    made += [
        GradedOutcome(
            source=source, window=window, scope=scope, reference_at=at, correct=None, neutral=True
        )
        for _ in range(neutral)
    ]
    return made


def both(
    graded: int,
    correct_1h: int,
    correct_1d: int | None = None,
    *,
    scope: Scope = Scope.WATCH,
    at: datetime = RECENT,
    neutral: int = 0,
    source: str = "massive",
) -> list[GradedOutcome]:
    """The same sample size in both windows; ``correct_1d`` defaults to 1h's."""
    c1d = correct_1h if correct_1d is None else correct_1d
    return outcomes(
        "1h", graded, correct_1h, scope=scope, at=at, neutral=neutral, source=source
    ) + outcomes("1d", graded, c1d, scope=scope, at=at, neutral=neutral, source=source)


def status(state: SourceStatus, source: str = "massive") -> CurrentStatus:
    changed = None if state is SourceStatus.UNAUDITED else DEMOTED_AT
    return CurrentStatus(source=source, status=state, changed_at=changed)


def row(
    rows: Iterable[AccuracyRow], window: str, basis: AccuracyBasis, scope: AccuracyScope
) -> AccuracyRow:
    found = [r for r in rows if (r.window, r.basis, r.scope) == (window, basis, scope)]
    assert len(found) == 1, found
    return found[0]


def counts(r: AccuracyRow) -> tuple[int, int, int]:
    return (r.graded, r.correct, r.neutral)


def transition(state: SourceStatus, made: list[GradedOutcome]) -> Transition | None:
    return audit_source(status(state), made, RUN_AT).transition


# ------------------------------------------------------- vocabulary


def test_the_transition_constants_are_decision_13s() -> None:
    assert DEMOTE_BELOW_PCT == 52
    assert REPROMOTE_AT_PCT == 66
    assert DEMOTION_FLOOR == 30
    assert REPROMOTION_FLOOR == 100
    assert TRAILING_WINDOW == timedelta(days=28)
    assert SENTIMENT_AUDIT_CATEGORY == "sentiment"
    assert SENTIMENT_AUDIT_CATEGORY in AUDIT_CATEGORIES


def test_the_enums_spell_the_schemas_vocabularies() -> None:
    assert tuple(SourceStatus) == SENTIMENT_SOURCE_STATUSES
    assert tuple(AccuracyBasis) == ACCURACY_BASES
    assert tuple(AccuracyScope) == ACCURACY_SCOPES
    assert tuple(Scope) == LABEL_OUTCOME_SCOPES
    assert {e.value for e in SentimentEvent} == {"sentiment_demoted", "sentiment_repromoted"}


# ------------------------------------------------------- accuracy counts


def test_every_window_basis_and_scope_gets_a_row_zeros_included() -> None:
    rows = compute_accuracy(status(SourceStatus.DEMOTED), [], RUN_AT)
    keys = [(r.window, r.basis, r.scope) for r in rows]
    assert keys == [
        (w, b, s) for w in SENTIMENT_WINDOWS for b in AccuracyBasis for s in AccuracyScope
    ]
    assert all(counts(r) == (0, 0, 0) and r.source == "massive" for r in rows)


def test_since_demotion_is_computed_only_for_a_demoted_source() -> None:
    for state in (SourceStatus.UNAUDITED, SourceStatus.ACTIVE):
        rows = compute_accuracy(status(state), both(40, 20), RUN_AT)
        assert {r.basis for r in rows} == {AccuracyBasis.TRAILING_28D}
        assert len(rows) == 6


def test_neutral_labels_are_excluded_from_graded_and_counted_in_neutral() -> None:
    made = outcomes("1h", 30, 18, neutral=7) + outcomes("1d", 30, 18, neutral=2)
    rows = compute_accuracy(status(SourceStatus.ACTIVE), made, RUN_AT)
    assert counts(row(rows, "1h", AccuracyBasis.TRAILING_28D, AccuracyScope.ALL)) == (30, 18, 7)
    assert counts(row(rows, "1d", AccuracyBasis.TRAILING_28D, AccuracyScope.ALL)) == (30, 18, 2)


def test_neutral_labels_never_make_a_source_eligible() -> None:
    """29 directional plus any number of neutral is still 29 for the floor."""
    made = outcomes("1h", 29, 0, neutral=50) + outcomes("1d", 29, 0, neutral=50)
    assert transition(SourceStatus.ACTIVE, made) is None


def test_skipped_rows_count_nowhere() -> None:
    for reason in LABEL_SKIP_REASONS:
        assert (
            graded_outcome(
                source="massive",
                window="1h",
                scope=None if reason != "price_unavailable" else "watch",
                reference_at=None,
                correct=None,
                skip_reason=reason,
                direction=BULL,
            )
            is None
        )
    kept = graded_outcome(
        source="massive",
        window="1d",
        scope="discovery",
        reference_at=RECENT,
        correct=False,
        skip_reason=None,
        direction=BEAR,
    )
    assert kept == GradedOutcome(
        source="massive",
        window="1d",
        scope=Scope.DISCOVERY,
        reference_at=RECENT,
        correct=False,
        neutral=False,
    )
    neutral = graded_outcome(
        source="rules",
        window="1h",
        scope="watch",
        reference_at=RECENT,
        correct=None,
        skip_reason=None,
        direction=NEUTRAL,
    )
    assert neutral is not None and neutral.neutral and neutral.correct is None


def test_scopes_split_and_all_is_their_sum() -> None:
    made = (
        outcomes("1h", 20, 15, scope=Scope.WATCH, neutral=1)
        + outcomes("1h", 15, 2, scope=Scope.DISCOVERY, neutral=3)
        + outcomes("1d", 20, 15, scope=Scope.WATCH)
        + outcomes("1d", 15, 2, scope=Scope.DISCOVERY)
    )
    rows = compute_accuracy(status(SourceStatus.ACTIVE), made, RUN_AT)
    t = AccuracyBasis.TRAILING_28D
    assert counts(row(rows, "1h", t, AccuracyScope.WATCH)) == (20, 15, 1)
    assert counts(row(rows, "1h", t, AccuracyScope.DISCOVERY)) == (15, 2, 3)
    assert counts(row(rows, "1h", t, AccuracyScope.ALL)) == (35, 17, 4)
    assert counts(row(rows, "1d", t, AccuracyScope.ALL)) == (35, 17, 0)


def test_transitions_read_the_combined_scope() -> None:
    """Neither split alone reaches the 30-label floor, and the watch split is
    at 75% -- yet ``all`` is 35 labels at 48.6%, and ``all`` is what acts."""
    made = (
        outcomes("1h", 20, 15, scope=Scope.WATCH)
        + outcomes("1h", 15, 2, scope=Scope.DISCOVERY)
        + outcomes("1d", 20, 15, scope=Scope.WATCH)
        + outcomes("1d", 15, 2, scope=Scope.DISCOVERY)
    )
    t = transition(SourceStatus.ACTIVE, made)
    assert t is not None and t.new is SourceStatus.DEMOTED
    assert {r.scope for r in t.acted_on} == {AccuracyScope.ALL}
    assert [counts(r) for r in t.acted_on] == [(35, 17, 0), (35, 17, 0)]


def test_the_trailing_window_is_open_at_28_days_and_closed_at_the_run() -> None:
    edge = RUN_AT - TRAILING_WINDOW
    made = (
        outcomes("1h", 1, 1, at=edge)  # exactly 28 days: out
        + outcomes("1h", 1, 1, at=edge + timedelta(microseconds=1))  # in
        + outcomes("1h", 1, 0, at=RUN_AT)  # the run instant itself: in
        + outcomes("1h", 1, 1, at=edge - timedelta(days=1))  # well out
    )
    rows = compute_accuracy(status(SourceStatus.ACTIVE), made, RUN_AT)
    assert counts(row(rows, "1h", AccuracyBasis.TRAILING_28D, AccuracyScope.ALL)) == (2, 1, 0)


def test_the_trailing_boundary_decides_eligibility() -> None:
    """29 labels inside the window and one exactly 28 days old: ineligible.
    Move that one a microsecond later and the source demotes."""
    edge = RUN_AT - TRAILING_WINDOW
    inside = both(29, 10)
    assert transition(SourceStatus.ACTIVE, inside + both(1, 0, at=edge)) is None
    moved = transition(
        SourceStatus.ACTIVE, inside + both(1, 0, at=edge + timedelta(microseconds=1))
    )
    assert moved is not None and moved.new is SourceStatus.DEMOTED


def test_since_demotion_counts_only_labels_referenced_after_the_demotion() -> None:
    made = (
        outcomes("1h", 5, 5, at=DEMOTED_AT - timedelta(days=1))  # before: out
        + outcomes("1h", 3, 3, at=DEMOTED_AT)  # at the instant: out
        + outcomes("1h", 4, 1, at=DEMOTED_AT + timedelta(microseconds=1))  # in
    )
    rows = compute_accuracy(status(SourceStatus.DEMOTED), made, RUN_AT)
    since = AccuracyBasis.SINCE_DEMOTION
    assert counts(row(rows, "1h", since, AccuracyScope.ALL)) == (4, 1, 0)
    assert counts(row(rows, "1h", AccuracyBasis.TRAILING_28D, AccuracyScope.ALL)) == (12, 9, 0)


def test_since_demotion_has_no_28_day_lookback() -> None:
    long_ago = CurrentStatus(
        source="massive", status=SourceStatus.DEMOTED, changed_at=RUN_AT - timedelta(days=200)
    )
    made = outcomes("1d", 3, 2, at=RUN_AT - timedelta(days=100))
    rows = compute_accuracy(long_ago, made, RUN_AT)
    assert counts(row(rows, "1d", AccuracyBasis.SINCE_DEMOTION, AccuracyScope.ALL)) == (3, 2, 0)
    assert counts(row(rows, "1d", AccuracyBasis.TRAILING_28D, AccuracyScope.ALL)) == (0, 0, 0)


def test_an_outcome_referenced_after_the_run_is_excluded_and_reported() -> None:
    """Decision 21: no raise, in neither basis, and back on the audit."""
    present = both(12, 7) + outcomes("1h", 3, 1, scope=Scope.DISCOVERY, neutral=2)
    late_1d = outcomes("1d", 1, 0, at=RUN_AT + timedelta(days=2))
    late_1h = outcomes("1h", 2, 2, at=RUN_AT + timedelta(microseconds=1), neutral=1)
    for state in SourceStatus:
        clean = audit_source(status(state), present, RUN_AT)
        dirty = audit_source(status(state), late_1d + present + late_1h, RUN_AT)
        assert dirty.accuracy == clean.accuracy  # every basis, scope and window
        assert dirty.transition == clean.transition
        assert dirty.future_referenced == tuple(late_1h + late_1d)  # by instant
        assert clean.future_referenced == ()


def test_since_demotion_is_closed_at_the_run() -> None:
    made = outcomes("1h", 2, 1, at=RUN_AT) + outcomes(
        "1h", 5, 5, at=RUN_AT + timedelta(microseconds=1)
    )
    rows = compute_accuracy(status(SourceStatus.DEMOTED), made, RUN_AT)
    for basis in AccuracyBasis:
        assert counts(row(rows, "1h", basis, AccuracyScope.ALL)) == (2, 1, 0)


def test_a_future_outcome_never_completes_a_repromotion() -> None:
    """99 since-demotion labels in each window plus one referenced after the
    run: the floor is not met, so nothing re-promotes."""
    made = both(99, 80) + both(1, 1, at=RUN_AT + _MINUTE)
    audit = audit_source(status(SourceStatus.DEMOTED), made, RUN_AT)
    assert audit.transition is None
    assert len(audit.future_referenced) == 2
    assert transition(SourceStatus.DEMOTED, both(99, 80) + both(1, 1)) is not None


def test_a_future_outcome_never_completes_a_demotion() -> None:
    made = both(29, 0) + both(1, 0, at=RUN_AT + _MINUTE)
    assert transition(SourceStatus.ACTIVE, made) is None
    assert transition(SourceStatus.ACTIVE, both(30, 0)) is not None


def test_audit_sources_reports_future_outcomes_per_source() -> None:
    late = outcomes("1d", 1, 1, at=RUN_AT + _MINUTE, source="rules")
    audits = audit_sources(
        [status(SourceStatus.ACTIVE), status(SourceStatus.ACTIVE, source="rules")],
        both(5, 3) + both(5, 3, source="rules") + late,
        RUN_AT,
    )
    assert [(a.source, a.future_referenced) for a in audits] == [
        ("massive", ()),
        ("rules", tuple(late)),
    ]


# ------------------------------------------------------- the runner's load bound


def test_earliest_reference_needed_is_the_trailing_start_unless_demoted_earlier() -> None:
    trailing_start = RUN_AT - TRAILING_WINDOW
    for state in (SourceStatus.UNAUDITED, SourceStatus.ACTIVE):
        assert earliest_reference_needed(status(state), RUN_AT) == trailing_start
    # Demoted inside the trailing window: the trailing start is still earlier.
    assert earliest_reference_needed(status(SourceStatus.DEMOTED), RUN_AT) == trailing_start
    # Demoted long ago: since_demotion has no lookback, so the demotion is.
    long_ago = RUN_AT - timedelta(days=200)
    demoted = CurrentStatus(source="massive", status=SourceStatus.DEMOTED, changed_at=long_ago)
    assert earliest_reference_needed(demoted, RUN_AT) == long_ago
    with pytest.raises(ValueError, match="run_at must be timezone-aware"):
        earliest_reference_needed(status(SourceStatus.ACTIVE), RUN_AT.replace(tzinfo=None))


def test_loading_only_rows_after_the_earliest_reference_changes_nothing() -> None:
    """The bound is exact: every row at or before it counts nowhere, and one
    microsecond after it counts."""
    long_ago = CurrentStatus(
        source="massive", status=SourceStatus.DEMOTED, changed_at=RUN_AT - timedelta(days=200)
    )
    for st in (status(SourceStatus.ACTIVE), status(SourceStatus.DEMOTED), long_ago):
        edge = earliest_reference_needed(st, RUN_AT)
        made = [
            o
            for at in (
                edge - timedelta(days=30),
                edge,
                edge + timedelta(microseconds=1),
                RUN_AT,
                RUN_AT + _MINUTE,
            )
            for o in both(2, 1, at=at)
        ]
        everything = audit_source(st, made, RUN_AT)
        loaded = audit_source(st, [o for o in made if o.reference_at > edge], RUN_AT)
        assert loaded == everything
        # The row a microsecond after the edge is counted, so the bound is tight.
        tight = audit_source(
            st, [o for o in made if o.reference_at > edge + timedelta(microseconds=1)], RUN_AT
        )
        assert tight.accuracy != everything.accuracy


def test_another_sources_outcome_is_refused() -> None:
    with pytest.raises(ValueError, match="source"):
        compute_accuracy(status(SourceStatus.ACTIVE), outcomes("1h", 1, 1, source="rules"), RUN_AT)


def test_accuracy_is_deterministic_under_reordering() -> None:
    made = both(40, 25, scope=Scope.WATCH) + both(12, 3, scope=Scope.DISCOVERY, neutral=2)
    shuffled = list(made)
    random.Random(7).shuffle(shuffled)
    st = status(SourceStatus.DEMOTED)
    assert compute_accuracy(st, made, RUN_AT) == compute_accuracy(st, shuffled, RUN_AT)


# ------------------------------------------------------- the demotion side


@pytest.mark.parametrize("state", [SourceStatus.UNAUDITED, SourceStatus.ACTIVE])
def test_29_labels_at_40_percent_do_not_demote(state: SourceStatus) -> None:
    assert transition(state, both(29, 12)) is None  # 41.4%, one short of the floor


@pytest.mark.parametrize("state", [SourceStatus.UNAUDITED, SourceStatus.ACTIVE])
def test_the_floor_is_per_window(state: SourceStatus) -> None:
    """30 in 1h and 29 in 1d, both terrible: still ineligible."""
    made = outcomes("1h", 30, 0) + outcomes("1d", 29, 0)
    assert transition(state, made) is None


@pytest.mark.parametrize("state", [SourceStatus.UNAUDITED, SourceStatus.ACTIVE])
def test_30_labels_at_15_of_30_demote(state: SourceStatus) -> None:
    t = transition(state, both(30, 15))
    assert t is not None
    assert (t.previous, t.new) == (state, SourceStatus.DEMOTED)
    assert t.basis is AccuracyBasis.TRAILING_28D


def test_an_active_source_at_16_of_30_is_not_demoted() -> None:
    assert transition(SourceStatus.ACTIVE, both(30, 16)) is None  # 53.3%


def test_an_unaudited_source_at_16_of_30_becomes_active_not_demoted() -> None:
    t = transition(SourceStatus.UNAUDITED, both(30, 16))
    assert t is not None and t.new is SourceStatus.ACTIVE


@pytest.mark.parametrize("state", [SourceStatus.UNAUDITED, SourceStatus.ACTIVE])
@pytest.mark.parametrize("bad", ["1h", "1d"])
def test_below_52_in_one_window_alone_demotes(state: SourceStatus, bad: str) -> None:
    good = "1d" if bad == "1h" else "1h"
    made = outcomes(bad, 30, 15) + outcomes(good, 30, 30)
    t = transition(state, made)
    assert t is not None and t.new is SourceStatus.DEMOTED
    assert f"below 52% in {bad}" in t.reason


def test_exactly_52_percent_is_not_below_52() -> None:
    """26/50 is 52% exactly -- permitted; 25/50 is not. Integer comparison,
    so no float rounding can move the boundary."""
    assert transition(SourceStatus.ACTIVE, both(50, 26)) is None
    demoted = transition(SourceStatus.ACTIVE, both(50, 26, 25))
    assert demoted is not None and demoted.new is SourceStatus.DEMOTED
    promoted = transition(SourceStatus.UNAUDITED, both(50, 26))
    assert promoted is not None and promoted.new is SourceStatus.ACTIVE


def test_unaudited_becomes_active_at_52_or_better_in_both_windows() -> None:
    t = transition(SourceStatus.UNAUDITED, both(30, 16, 29))
    assert t is not None
    assert (t.previous, t.new) == (SourceStatus.UNAUDITED, SourceStatus.ACTIVE)
    assert t.notice is None  # "logged and emits nothing"
    assert t.audit_log == AuditLogFields(
        category="sentiment", field="massive", previous_value="unaudited", new_value="active"
    )


def test_a_demoting_source_is_judged_on_trailing_labels_only() -> None:
    """Old disasters outside the window cannot demote a source doing fine now."""
    old = both(200, 0, at=RUN_AT - timedelta(days=40))
    assert transition(SourceStatus.ACTIVE, old + both(30, 20)) is None


# ------------------------------------------------------- the re-promotion side


def test_a_demoted_source_at_99_labels_and_80_percent_does_not_repromote() -> None:
    assert transition(SourceStatus.DEMOTED, both(99, 79)) is None


def test_the_repromotion_floor_is_per_window() -> None:
    made = outcomes("1h", 100, 90) + outcomes("1d", 99, 90)
    assert transition(SourceStatus.DEMOTED, made) is None


def test_100_labels_at_66_of_100_in_both_windows_repromote() -> None:
    t = transition(SourceStatus.DEMOTED, both(100, 66))
    assert t is not None
    assert (t.previous, t.new) == (SourceStatus.DEMOTED, SourceStatus.ACTIVE)
    assert t.basis is AccuracyBasis.SINCE_DEMOTION
    assert [counts(r) for r in t.acted_on] == [(100, 66, 0), (100, 66, 0)]


@pytest.mark.parametrize(("c1h", "c1d"), [(66, 65), (65, 66)])
def test_66_in_one_window_and_65_in_the_other_does_not_repromote(c1h: int, c1d: int) -> None:
    assert transition(SourceStatus.DEMOTED, both(100, c1h, c1d)) is None


def test_repromotion_counts_only_labels_graded_after_the_demotion() -> None:
    """100 strong labels before the demotion and 99 after: not yet. One more
    after the demotion and it re-promotes -- the pre-demotion labels never
    counted, though every one of them is inside the trailing window."""
    before = both(100, 100, at=DEMOTED_AT - timedelta(days=1))
    after = both(99, 80)
    assert transition(SourceStatus.DEMOTED, before + after) is None
    t = transition(SourceStatus.DEMOTED, before + after + both(1, 1))
    assert t is not None and t.new is SourceStatus.ACTIVE
    assert [r.graded for r in t.acted_on] == [100, 100]


def test_a_demoted_source_is_never_re_demoted() -> None:
    """Terrible since-demotion labels change nothing: demoted stays demoted
    and writes no row (``previous_value <> new_value``)."""
    assert transition(SourceStatus.DEMOTED, both(200, 0)) is None


# ------------------------------------------------------- the dead band


@pytest.mark.parametrize("state", [SourceStatus.ACTIVE, SourceStatus.DEMOTED])
def test_a_source_at_60_percent_stays_where_it_is(state: SourceStatus) -> None:
    """Above the demotion bar and below the re-promotion bar, from both sides,
    with every floor met."""
    assert transition(state, both(150, 90)) is None


def test_nothing_ever_transitions_into_unaudited() -> None:
    for state in SourceStatus:
        for made in (both(150, 0), both(150, 150), both(150, 90), []):
            t = transition(state, made)
            assert t is None or t.new is not SourceStatus.UNAUDITED


# ------------------------------------------------------- the result


def test_a_demotion_carries_the_audit_fields_and_a_warning() -> None:
    t = transition(SourceStatus.ACTIVE, both(30, 15, 20))
    assert t is not None
    assert t.audit_log == AuditLogFields(
        category="sentiment", field="massive", previous_value="active", new_value="demoted"
    )
    assert t.notice is not None
    assert (t.notice.event, t.notice.severity) == (SentimentEvent.DEMOTED, "warning")
    assert t.notice.body == t.reason
    assert t.changed_at == RUN_AT and t.changed_at.tzinfo is timezone.utc


def test_an_unaudited_demotion_also_warns() -> None:
    t = transition(SourceStatus.UNAUDITED, both(30, 3))
    assert t is not None and t.notice is not None
    assert t.notice.event is SentimentEvent.DEMOTED


def test_a_repromotion_carries_an_info_notice() -> None:
    t = transition(SourceStatus.DEMOTED, both(100, 70))
    assert t is not None and t.notice is not None
    assert (t.notice.event, t.notice.severity) == (SentimentEvent.REPROMOTED, "info")
    assert t.audit_log.previous_value == "demoted" and t.audit_log.new_value == "active"


def test_the_reason_carries_both_accuracies_and_their_sample_sizes() -> None:
    t = transition(SourceStatus.ACTIVE, both(30, 15, 20))
    assert t is not None
    assert "trailing_28d" in t.reason
    assert "1h 50.0% (15/30)" in t.reason
    assert "1d 66.6% (20/30)" in t.reason  # 66.67 truncated, never rounded up


def test_the_reason_never_rounds_a_figure_across_its_threshold() -> None:
    """1299/2500 is 51.96%: rounding would print 52.0% beside a demotion for
    being below 52%. The figure is truncated, so it reads 51.9%."""
    t = transition(SourceStatus.ACTIVE, both(2500, 1299))
    assert t is not None and t.new is SourceStatus.DEMOTED
    assert "51.9%" in t.reason and "52.0%" not in t.reason


def test_the_acted_on_rows_are_the_stored_rows() -> None:
    """The figure a transition acted on is the figure stored (decision 13)."""
    result = audit_source(status(SourceStatus.DEMOTED), both(100, 66), RUN_AT)
    assert result.transition is not None
    for acted in result.transition.acted_on:
        assert acted in result.accuracy


def test_at_most_one_transition_per_source_per_run() -> None:
    """Unaudited at 50% demotes, and the same run does not then judge the
    demoted source for re-promotion on the same labels."""
    result = audit_source(status(SourceStatus.UNAUDITED), both(300, 150), RUN_AT)
    assert result.transition is not None and result.transition.new is SourceStatus.DEMOTED
    # The accuracy is the pre-transition state's: trailing only.
    assert {r.basis for r in result.accuracy} == {AccuracyBasis.TRAILING_28D}


def test_audit_sources_runs_each_source_once_in_order() -> None:
    statuses = [status(SourceStatus.ACTIVE, "rules"), status(SourceStatus.UNAUDITED, "massive")]
    made = both(30, 10, source="rules") + both(30, 20, source="massive")
    results = audit_sources(statuses, made, RUN_AT)
    assert [r.source for r in results] == ["massive", "rules"]
    by_source = {r.source: r.transition for r in results}
    assert by_source["rules"] is not None and by_source["rules"].new is SourceStatus.DEMOTED
    assert by_source["massive"] is not None and by_source["massive"].new is SourceStatus.ACTIVE


def test_audit_sources_refuses_a_duplicate_status() -> None:
    with pytest.raises(ValueError, match="more than once"):
        audit_sources([status(SourceStatus.ACTIVE), status(SourceStatus.DEMOTED)], [], RUN_AT)


def test_audit_sources_refuses_an_outcome_with_no_status_row() -> None:
    with pytest.raises(ValueError, match="no status"):
        audit_sources([status(SourceStatus.ACTIVE, "rules")], both(1, 1), RUN_AT)


def test_decide_transition_refuses_missing_accuracy_rows() -> None:
    trailing_only = compute_accuracy(status(SourceStatus.ACTIVE), both(100, 70), RUN_AT)
    with pytest.raises(ValueError, match="since_demotion"):
        decide_transition(status(SourceStatus.DEMOTED), trailing_only, RUN_AT)


# ------------------------------------------------------- input validation


def test_a_directional_outcome_without_a_grade_is_refused() -> None:
    """It would drop out of every count silently (unit 6.1's warning)."""
    with pytest.raises(ValueError, match="directional"):
        GradedOutcome(
            source="massive", window="1h", scope=Scope.WATCH, reference_at=RECENT,
            correct=None, neutral=False,
        )  # fmt: skip


def test_a_neutral_outcome_with_a_grade_is_refused() -> None:
    with pytest.raises(ValueError, match="neutral"):
        GradedOutcome(
            source="massive", window="1h", scope=Scope.WATCH, reference_at=RECENT,
            correct=True, neutral=True,
        )  # fmt: skip


def test_an_unknown_window_is_refused() -> None:
    with pytest.raises(ValueError, match="window"):
        GradedOutcome(
            source="massive", window="1w", scope=Scope.WATCH, reference_at=RECENT,
            correct=True, neutral=False,
        )  # fmt: skip


def test_a_graded_row_without_scope_or_reference_is_refused() -> None:
    for scope, ref in (
        (None, RECENT),
        ("watch", None),
    ):
        with pytest.raises(ValueError):
            graded_outcome(
                source="massive", window="1h", scope=scope, reference_at=ref,
                correct=True, skip_reason=None, direction=BULL,
            )  # fmt: skip


def test_naive_instants_are_refused_by_accuracy() -> None:
    naive = datetime(2026, 10, 1, 14, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        GradedOutcome(
            source="massive", window="1h", scope=Scope.WATCH, reference_at=naive,
            correct=True, neutral=False,
        )  # fmt: skip
    with pytest.raises(ValueError, match="timezone-aware"):
        CurrentStatus(source="massive", status=SourceStatus.DEMOTED, changed_at=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_accuracy(status(SourceStatus.ACTIVE), [], naive)


def test_a_status_row_must_match_its_state() -> None:
    """The schema's ``ck_sentiment_source_status_transition``, restated: an
    unaudited row has no ``changed_at`` and every other row has one."""
    with pytest.raises(ValueError, match="unaudited"):
        CurrentStatus(source="massive", status=SourceStatus.UNAUDITED, changed_at=DEMOTED_AT)
    with pytest.raises(ValueError, match="changed_at"):
        CurrentStatus(source="massive", status=SourceStatus.DEMOTED, changed_at=None)


# --------------------------------------------------------------------- #
# One grade per (group, ticker, source) -- unit 6.4a's audit fix
# --------------------------------------------------------------------- #


def _copies(*, canonical_pick: bool = True) -> list[AuditLabel]:
    """One AAPL story stored twice (decision 3), each copy labelled by rules.

    Label 1 sits on the canonical article, label 2 on the member; ``group_labels``
    picks the canonical's, so by default label 1 is the pick.
    """
    return [
        label(1, "AAPL", BULL, _TUESDAY_1015, group=77, pick=canonical_pick),
        label(2, "AAPL", BULL, _TUESDAY_1015, group=77, pick=not canonical_pick),
    ]


def test_two_copies_of_one_story_yield_one_graded_label_and_one_duplicate() -> None:
    bars = _two_point_bars("AAPL", "100", "102") + _two_point_bars("SPY", "500", "505")
    plan, result = run(_copies(), bars)
    assert {g.label.label_id for g in plan.to_grade} == {1}
    records = by_key(result)
    assert records[(1, "1h")].skip_reason is None and records[(1, "1h")].correct is True
    for window in ("1h", "1d"):
        dup = records[(2, window)]
        assert dup.skip_reason is SkipReason.DUPLICATE_COPY
        assert dup.scope is None and dup.reference_at is None and dup.reference_price is None
        assert dup.outcome_price is None and dup.excess_return is None and dup.correct is None
    # The duplicate's rows come with the plan, needing no bars, and are its only rows.
    assert {(r.label_id, r.window) for r in plan.skipped} == {(2, "1h"), (2, "1d")}


def test_the_pick_is_group_labels_pick_not_the_lowest_label_id() -> None:
    """``group_pick`` is ``signals.group_labels``' choice, carried in; never re-derived."""
    bars = _two_point_bars("AAPL", "100", "102") + _two_point_bars("SPY", "500", "505")
    plan, result = run(_copies(canonical_pick=False), bars)
    assert {g.label.label_id for g in plan.to_grade} == {2}
    assert by_key(result)[(1, "1h")].skip_reason is SkipReason.DUPLICATE_COPY


def test_a_duplicate_is_per_ticker_and_per_source() -> None:
    """One group: another ticker, or another source's label, is its own pick."""
    labels = [
        *_copies(),
        label(3, "MSFT", BULL, _TUESDAY_1015, group=77, pick=True),
        label(4, "AAPL", BULL, _TUESDAY_1015, source="massive", group=77, pick=True),
    ]
    plan, result = run(labels, [])
    assert {g.label.label_id for g in plan.to_grade} == {1, 3, 4}
    assert _skips(result, SkipReason.DUPLICATE_COPY) == {2}


def test_a_duplicate_is_written_before_its_windows_mature() -> None:
    """It needs no bars and no schedule, so it is decided the first time it is seen."""
    early = _TUESDAY_1015 + timedelta(minutes=5)
    plan, result = run(_copies(), [], run_at=early)
    assert plan.to_grade == ()
    assert {(r.label_id, r.window, r.skip_reason) for r in result.records} == {
        (2, "1h", SkipReason.DUPLICATE_COPY),
        (2, "1d", SkipReason.DUPLICATE_COPY),
    }


def test_a_later_labelled_canonical_does_not_regrade_a_claimed_group() -> None:
    """The member was graded first; the canonical copy's label arrives later.

    ``group_labels`` now picks the canonical, but the group already holds an
    outcome row, so the canonical copy is the duplicate and the choice stands.
    """
    canonical = label(5, "AAPL", BULL, _TUESDAY_1015, group=77, pick=True)
    bars = _two_point_bars("AAPL", "100", "102") + _two_point_bars("SPY", "500", "505")
    # The member's label is fully written and so not reloaded: only the claim says so.
    plan, result = run([canonical], bars, claimed=[(77, "AAPL", "rules")])
    assert plan.to_grade == ()
    assert {(r.label_id, r.window) for r in result.records} == {(5, "1h"), (5, "1d")}
    assert _skips(result, SkipReason.DUPLICATE_COPY) == {5}


def test_a_partly_written_member_keeps_its_group_against_a_later_canonical() -> None:
    """The member's 1h is written and its 1d pending: it is still the group's label."""
    member = label(2, "AAPL", BULL, _TUESDAY_1015, group=77, pick=False)
    canonical = label(5, "AAPL", BULL, _TUESDAY_1015, group=77, pick=True)
    plan, result = run([member, canonical], [], written=[(2, "1h")])
    assert {(g.label.label_id, g.window) for g in plan.to_grade} == {(2, "1d")}
    assert _skips(result, SkipReason.DUPLICATE_COPY) == {5}
    # A claim on the group does not demote the label that made it.
    plan, _ = run([member, canonical], [], written=[(2, "1h")], claimed=[(77, "AAPL", "rules")])
    assert {(g.label.label_id, g.window) for g in plan.to_grade} == {(2, "1d")}


def test_a_claim_on_another_ticker_or_source_does_not_touch_the_group() -> None:
    plan, result = run(
        _copies(),
        [],
        claimed=[(77, "MSFT", "rules"), (77, "AAPL", "massive"), (78, "AAPL", "rules")],
    )
    assert {g.label.label_id for g in plan.to_grade} == {1}
    assert _skips(result, SkipReason.DUPLICATE_COPY) == {2}


def test_duplicates_count_nowhere_in_accuracy() -> None:
    bars = _two_point_bars("AAPL", "100", "102") + _two_point_bars("SPY", "500", "505")
    _, result = run(_copies(), bars)
    made: list[GradedOutcome] = []
    for r in result.records:
        g = graded_outcome(
            source="rules",
            window=r.window,
            scope=None if r.scope is None else r.scope.value,
            reference_at=r.reference_at,
            correct=r.correct,
            skip_reason=None if r.skip_reason is None else r.skip_reason.value,
            direction=BULL,
        )
        if g is not None:
            made.append(g)
    assert len(made) == 1
    rows = compute_accuracy(status(SourceStatus.ACTIVE, source="rules"), made, RUN_AT)
    trailing = AccuracyBasis("trailing_28d")
    assert counts(row(rows, "1h", trailing, AccuracyScope("all"))) == (1, 1, 0)
    assert counts(row(rows, "1d", trailing, AccuracyScope("all"))) == (0, 0, 0)


def test_a_duplicate_takes_no_discovery_slot_and_no_unverdicted_count() -> None:
    labels = [
        label(1, "UNSEEN", BULL, _TUESDAY_1015, group=9, pick=True),
        label(2, "UNSEEN", BULL, _TUESDAY_1015, group=9, pick=False),
        label(3, "XYZ", BULL, _TUESDAY_1015, group=8, pick=True),
        label(4, "XYZ", BULL, _TUESDAY_1015, group=8, pick=False),
        label(5, "XYZ", BEAR, _TUESDAY_1015, group=7, pick=False),
    ]
    plan, result = run(labels, [], tradeable={"XYZ": True})
    assert plan.unverdicted_label_counts == (("UNSEEN", 1),)
    assert {g.label.label_id for g in plan.to_grade} == {3}
    assert _skips(result, SkipReason.DUPLICATE_COPY) == {2, 4, 5}


def test_a_group_with_two_picks_is_refused() -> None:
    """``group_labels`` picks exactly one per (group, ticker, source)."""
    labels = [
        label(1, "AAPL", BULL, _TUESDAY_1015, group=77),
        label(2, "AAPL", BULL, _TUESDAY_1015, group=77),
    ]
    with pytest.raises(ValueError, match="pick"):
        run(labels, [])


# --------------------------------------------------------------------- #
# The verdict is the label's, not the run's (Q7) -- unit 6.4a's audit fix
# --------------------------------------------------------------------- #

#: The sessions around ``_TUESDAY_1015``'s.
_MONDAY, _FRIDAY = date(2026, 10, 5), date(2026, 10, 9)


def test_the_verdict_is_the_newest_on_or_before_the_labels_reference_session() -> None:
    """Failing on the label's session, passing later: untradeable. And the reverse."""
    bars = _two_point_bars("XYZ", "10", "11") + _two_point_bars("SPY", "500", "505")
    xyz = [label(1, "XYZ", BULL, _TUESDAY_1015)]

    _, result = run(xyz, bars, tradeable={"XYZ": [(_MONDAY, False), (_FRIDAY, True)]})
    assert _skips(result, SkipReason.OFF_WATCH_UNTRADEABLE) == {1}

    _, result = run(xyz, bars, tradeable={"XYZ": [(_MONDAY, True), (_FRIDAY, False)]})
    record = by_key(result)[(1, "1h")]
    assert record.skip_reason is None and record.scope is Scope.DISCOVERY


def test_a_verdict_for_the_reference_session_itself_counts() -> None:
    bars = _two_point_bars("XYZ", "10", "11") + _two_point_bars("SPY", "500", "505")
    _, result = run(
        [label(1, "XYZ", BULL, _TUESDAY_1015)], bars, tradeable={"XYZ": [(_TUESDAY, True)]}
    )
    assert by_key(result)[(1, "1h")].scope is Scope.DISCOVERY


def test_the_reference_session_not_the_publication_date_picks_the_verdict() -> None:
    """Published Saturday, referenced Monday's open: Monday's verdict applies."""
    saturday = et(2026, 10, 3, 12, 0)
    labels = [label(1, "XYZ", BULL, saturday)]
    _, result = run(labels, [], tradeable={"XYZ": [(date(2026, 10, 2), True), (_MONDAY, False)]})
    assert _skips(result, SkipReason.OFF_WATCH_UNTRADEABLE) == {1}


def test_verdicts_only_after_the_label_leave_it_unverdicted() -> None:
    """No verdict on or before its session: the wait-then-fail-closed path."""
    labels = [label(1, "XYZ", BULL, _TUESDAY_1015)]
    plan, result = run(labels, [], tradeable={"XYZ": [(_FRIDAY, True)]})
    assert result.records == ()
    assert plan.unverdicted_tickers == ("XYZ",)
    last_end = window_end(_TUESDAY_1015, "1d")
    after = last_end + PRICE_UNAVAILABLE_AFTER + timedelta(seconds=1)
    _, result = run(labels, [], tradeable={"XYZ": [(_FRIDAY, True)]}, run_at=after)
    assert _skips(result, SkipReason.OFF_WATCH_UNTRADEABLE) == {1}


def test_two_labels_on_one_ticker_each_read_their_own_session() -> None:
    labels = [
        label(1, "XYZ", BULL, _TUESDAY_1015),
        label(2, "XYZ", BULL, et(2026, 10, 9, 10, 15)),
    ]
    run_at = et(2026, 10, 13, 10, 0)
    plan, result = run(
        labels,
        [],
        tradeable={"XYZ": [(_MONDAY, False), (date(2026, 10, 8), True)]},
        run_at=run_at,
    )
    assert {g.label.label_id for g in plan.to_grade} == {2}
    assert _skips(result, SkipReason.OFF_WATCH_UNTRADEABLE) == {1}


def test_a_verdict_history_is_read_in_any_order() -> None:
    labels = [label(1, "XYZ", BULL, _TUESDAY_1015)]
    history = [(_MONDAY, False), (_FRIDAY, True), (date(2026, 9, 1), True)]
    first, _ = run(labels, [], tradeable={"XYZ": history})
    second, _ = run(labels, [], tradeable={"XYZ": list(reversed(history))})
    assert first == second
    assert first.skipped and first.skipped[0].skip_reason is SkipReason.OFF_WATCH_UNTRADEABLE


def test_two_verdicts_for_one_session_are_refused() -> None:
    """``ticker_tradeability``'s primary key is ``(ticker, session_date)``."""
    with pytest.raises(ValueError, match="XYZ"):
        run(
            [label(1, "XYZ", BULL, _TUESDAY_1015)],
            [],
            tradeable={"XYZ": [(_MONDAY, True), (_MONDAY, False)]},
        )
