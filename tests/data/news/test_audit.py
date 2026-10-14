"""Decision 12's grading, pure -- ``corollary.data.news.audit`` (Phase 3 unit 6.2).

Every input is synthetic and every date a real 2026 NYSE session, read from
the calendar rather than assumed. The bars are real :class:`Bar` instances,
stamped at their **opening** minute as the provider's are, so the
no-look-ahead rule (a bar's close is known one minute after its stamp) is
proven against the type the runner will actually pass.
"""

import random
from collections.abc import Collection, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from corollary.calendars import NYSE_TZ, nyse_session_close, nyse_session_open
from corollary.data.news.audit import (
    BENCHMARK_SYMBOL,
    DISCOVERY_TICKER_CAP,
    FALLBACK_MAX_LAG,
    MATURITY_DELAY,
    PRICE_UNAVAILABLE_AFTER,
    RETURN_EXPONENT,
    VENDOR_STAMP_TOLERANCE,
    WINDOW_SESSION_MINUTES,
    AuditLabel,
    AuditPlan,
    BarFault,
    BarRequest,
    GradeResult,
    LabelOutcomeRecord,
    Scope,
    SkipReason,
    grade,
    plan_audit,
    reference_at,
    reference_instant,
    window_end,
)
from corollary.data.news.labels import Direction
from corollary.data.providers.interface import Bar
from corollary.db.models import LABEL_SKIP_REASONS, SENTIMENT_WINDOWS

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
) -> AuditLabel:
    return AuditLabel(
        label_id=label_id,
        ticker=ticker,
        source=source,
        direction=direction,
        published_at=published,
        labeled_at=published if labeled is None else labeled,
    )


def run(
    labels: Iterable[AuditLabel],
    bars: Iterable[Bar],
    *,
    watch: Collection[str] = ("AAPL", "MSFT", "SPY", "MARKET"),
    tradeable: Mapping[str, bool] | None = None,
    written: Collection[tuple[int, str]] = (),
    run_at: datetime = RUN_AT,
) -> tuple[AuditPlan, GradeResult]:
    plan = plan_audit(
        list(labels),
        watch_universe=watch,
        tradeable={} if tradeable is None else tradeable,
        written=written,
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
