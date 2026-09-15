"""The NYSE session calendar, and the half-days it exists to get right.

CLAUDE.md: *"Session boundaries come from a market calendar, never hardcoded
09:30-16:00. Half-days and holidays are real."* The values below are facts
about the exchange, checked against ``exchange_calendars``' XNYS rather than
against this module's own arithmetic.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from corollary.calendars import (
    NYSE_DEFAULT_CLOSE,
    nyse_close_at,
    nyse_session_close,
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
