"""The NYSE session calendar, and the half-days it exists to get right.

CLAUDE.md: *"Session boundaries come from a market calendar, never hardcoded
09:30-16:00. Half-days and holidays are real."* The values below are facts
about the exchange, checked against ``exchange_calendars``' XNYS rather than
against this module's own arithmetic.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from corollary.calendars import (
    NYSE_DEFAULT_CLOSE,
    add_session_minutes,
    nyse_close_at,
    nyse_next_open,
    nyse_session_close,
    nyse_session_containing,
    nyse_session_open,
)


def test_an_ordinary_session_closes_at_four() -> None:
    assert nyse_close_at(date(2025, 11, 26)) == datetime(
        2025, 11, 26, 21, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "half_day",
    [
        date(2025, 11, 28),  # the day after Thanksgiving
        date(2025, 12, 24),  # Christmas Eve
        date(2026, 11, 27),
    ],
)
def test_a_half_day_closes_three_hours_early(half_day: date) -> None:
    """13:00 ET, not 16:00.

    Three hours on the last day of a contract's life is not a rounding error:
    it is the stretch where theta is largest, so measuring to 16:00 overstates
    the remaining time value exactly where the overstatement is worth most.
    """
    assert nyse_close_at(half_day) == datetime(
        half_day.year, half_day.month, half_day.day, 18, 0, tzinfo=timezone.utc
    )


def test_a_holiday_has_no_session_and_says_so() -> None:
    """July 3rd 2026, the observed Independence Day. Absent, not 16:00."""
    assert nyse_session_close(date(2026, 7, 3)) is None


def test_a_date_the_calendar_cannot_answer_falls_back_rather_than_raising() -> None:
    """A LEAP beyond the published calendar must not take down a chain.

    ``exchange_calendars`` publishes a bounded range, and an expiration past
    its end is a perfectly ordinary contract. The fallback is the ordinary
    16:00 ET close, which is right for every session except the handful of
    half-days -- and a wrong-by-three-hours ``years`` on one far-dated
    contract is a far smaller error than refusing to price the chain.
    """
    far_future = date(2099, 1, 15)
    assert nyse_session_close(far_future) is None
    assert nyse_close_at(far_future) == datetime(
        2099, 1, 15, 21, 0, tzinfo=timezone.utc
    )


def test_the_fallback_close_is_stated_in_eastern_not_utc() -> None:
    """UTC drifts against the exchange twice a year; ET does not.

    A hardcoded 21:00 UTC close would be an hour wrong for the four months
    the US is on standard time.
    """
    assert NYSE_DEFAULT_CLOSE.hour == 16
    summer = nyse_close_at(date(2026, 7, 1))
    winter = nyse_close_at(date(2026, 1, 5))
    assert summer.hour == 20  # EDT, UTC-4
    assert winter.hour == 21  # EST, UTC-5


def test_the_resolver_answers_the_same_way_twice() -> None:
    """The calendar is loaded once and cached; a second call is not a reload."""
    first = nyse_close_at(date(2026, 3, 20))
    second = nyse_close_at(date(2026, 3, 20))
    assert first == second


# --------------------------------------------------------------------------
# The open side
# --------------------------------------------------------------------------
#
# Added for the intraday price series, which has to drop the extended-hours
# bars Alpaca serves either side of the regular session. Both bounds come
# from the same cached calendar, so a half-day is right for free rather than
# special-cased at the call site.


def test_an_ordinary_session_opens_at_half_past_nine() -> None:
    """14:30 UTC on 26 November 2025 -- 09:30 in New York, on EST."""
    assert nyse_session_open(date(2025, 11, 26)) == datetime(
        2025, 11, 26, 14, 30, tzinfo=timezone.utc
    )


def test_a_half_day_opens_as_usual_and_so_is_shorter_at_the_close_end() -> None:
    """The early close is the whole difference: 3.5 hours, not 6.5.

    This is the case a hardcoded 09:30-16:00 filter gets wrong, and it gets
    it wrong in the direction that matters -- three hours of post-market
    prints appended to the session they follow.
    """
    half_day = date(2025, 11, 28)
    opened, closed = nyse_session_open(half_day), nyse_session_close(half_day)
    assert opened == datetime(2025, 11, 28, 14, 30, tzinfo=timezone.utc)
    assert closed is not None
    assert closed - opened == timedelta(hours=3, minutes=30)

    full_day = date(2025, 11, 26)
    full_open, full_close = nyse_session_open(full_day), nyse_session_close(full_day)
    assert full_open is not None and full_close is not None
    assert full_close - full_open == timedelta(hours=6, minutes=30)


def test_a_holiday_has_no_open_either() -> None:
    """Both halves answer ``None`` for a non-session day, not a default."""
    holiday = date(2026, 7, 3)
    assert nyse_session_open(holiday) is None
    assert nyse_session_close(holiday) is None


def test_a_date_past_the_published_schedule_has_no_open() -> None:
    """``None`` means "the calendar has nothing to say", same as the close."""
    assert nyse_session_open(date(2099, 1, 15)) is None


def test_the_open_is_stated_in_eastern_not_utc() -> None:
    """09:30 ET is 13:30 UTC in summer and 14:30 in winter.

    A constant offset would be an hour wrong for the four months the US is on
    standard time -- the same trap :data:`NYSE_DEFAULT_CLOSE` documents.
    """
    summer = nyse_session_open(date(2026, 7, 1))
    winter = nyse_session_open(date(2026, 1, 5))
    assert summer is not None and winter is not None
    assert (summer.hour, summer.minute) == (13, 30)
    assert (winter.hour, winter.minute) == (14, 30)


def test_the_open_resolver_answers_the_same_way_twice() -> None:
    """Cached like the closes: a second call is a hash, not a calendar build."""
    assert nyse_session_open(date(2026, 3, 20)) == nyse_session_open(
        date(2026, 3, 20)
    )


# --------------------------------------------------------------------- #
# Session-minute arithmetic (Phase 3 step 6, decision 12's windows)
# --------------------------------------------------------------------- #

_ET = ZoneInfo("America/New_York")


def _et(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    """An Eastern wall time as the UTC instant the code stores."""
    return datetime(year, month, day, hour, minute, second, tzinfo=_ET).astimezone(
        timezone.utc
    )


def test_session_minutes_inside_one_session_are_clock_minutes() -> None:
    assert add_session_minutes(_et(2026, 10, 6, 10, 15), 60) == _et(2026, 10, 6, 11, 15)


def test_session_minutes_may_end_exactly_on_the_close() -> None:
    """390 minutes from the open is the close itself, not the next open."""
    assert add_session_minutes(_et(2026, 10, 6, 9, 30), 390) == _et(2026, 10, 6, 16, 0)
    assert add_session_minutes(_et(2026, 10, 6, 15, 0), 60) == _et(2026, 10, 6, 16, 0)


def test_session_minutes_across_a_close_resume_at_the_next_open() -> None:
    """Decision 12: a 15:30 label's 1h window ends at 10:00 the next session."""
    assert add_session_minutes(_et(2026, 10, 6, 15, 30), 60) == _et(2026, 10, 7, 10, 0)


def test_session_minutes_across_a_weekend() -> None:
    """Friday 15:30 plus 60 lands Monday 10:00 (2026-10-12 is a session)."""
    assert add_session_minutes(_et(2026, 10, 9, 15, 30), 60) == _et(2026, 10, 12, 10, 0)


def test_session_minutes_across_a_holiday() -> None:
    """Labor Day 2026-09-07 is closed: Friday's window resumes on Tuesday."""
    assert add_session_minutes(_et(2026, 9, 4, 15, 30), 60) == _et(2026, 9, 8, 10, 0)


def test_session_minutes_across_thanksgiving_into_a_half_day() -> None:
    """Thanksgiving is closed; the half-day after it is a session."""
    assert add_session_minutes(_et(2026, 11, 25, 15, 30), 60) == _et(2026, 11, 27, 10, 0)


def test_session_minutes_across_a_half_day_close() -> None:
    """2026-11-27 closes at 13:00 ET, so 12:30 plus 60 is Monday 10:00.

    A hardcoded 16:00 close would put it at 13:30 the same day, in an empty
    market.
    """
    assert add_session_minutes(_et(2026, 11, 27, 12, 30), 60) == _et(2026, 11, 30, 10, 0)


def test_a_full_session_day_from_a_half_day_runs_into_the_next_session() -> None:
    """The half-day has 210 minutes; the other 180 land on Monday at 12:30."""
    assert add_session_minutes(_et(2026, 11, 27, 9, 30), 390) == _et(2026, 11, 30, 12, 30)


def test_session_minutes_across_the_dst_change() -> None:
    """DST ends 2026-11-01: Monday's 10:00 ET is 15:00 UTC, not Friday's 14:00."""
    landed = add_session_minutes(_et(2026, 10, 30, 15, 30), 60)
    assert landed == datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


def test_session_minutes_from_out_of_session_start_at_the_next_open() -> None:
    """Pre-market, after the close and on a weekend all count from the next open."""
    assert add_session_minutes(_et(2026, 10, 6, 8, 0), 60) == _et(2026, 10, 6, 10, 30)
    assert add_session_minutes(_et(2026, 10, 6, 18, 0), 60) == _et(2026, 10, 7, 10, 30)
    assert add_session_minutes(_et(2026, 10, 10, 12, 0), 60) == _et(2026, 10, 12, 10, 30)
    assert add_session_minutes(_et(2026, 10, 6, 16, 0), 0) == _et(2026, 10, 7, 9, 30)


def test_session_minutes_refuse_a_naive_instant_and_a_negative_count() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        add_session_minutes(datetime(2026, 10, 6, 15, 0), 60)
    with pytest.raises(ValueError, match="non-negative"):
        add_session_minutes(_et(2026, 10, 6, 15, 0), -1)


def test_session_minutes_past_the_published_schedule_raise() -> None:
    """No invented sessions: the calendar cannot answer 2099."""
    with pytest.raises(ValueError, match="published"):
        add_session_minutes(datetime(2099, 1, 15, 15, 0, tzinfo=timezone.utc), 60)


def test_the_session_containing_an_instant_is_half_open() -> None:
    """The open belongs to the session; the close does not."""
    assert nyse_session_containing(_et(2026, 10, 6, 9, 30)) == date(2026, 10, 6)
    assert nyse_session_containing(_et(2026, 10, 6, 15, 59, 59)) == date(2026, 10, 6)
    assert nyse_session_containing(_et(2026, 10, 6, 16, 0)) is None
    assert nyse_session_containing(_et(2026, 10, 6, 9, 29, 59)) is None
    assert nyse_session_containing(_et(2026, 11, 27, 13, 0)) is None
    assert nyse_session_containing(_et(2026, 11, 26, 12, 0)) is None


def test_the_next_open_is_at_or_after_the_instant() -> None:
    assert nyse_next_open(_et(2026, 10, 6, 9, 30)) == _et(2026, 10, 6, 9, 30)
    assert nyse_next_open(_et(2026, 10, 6, 9, 30, 1)) == _et(2026, 10, 7, 9, 30)
    assert nyse_next_open(_et(2026, 12, 24, 13, 0)) == _et(2026, 12, 28, 9, 30)
