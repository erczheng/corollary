"""The NYSE session calendar, and the half-days it exists to get right.

CLAUDE.md: *"Session boundaries come from a market calendar, never hardcoded
09:30-16:00. Half-days and holidays are real."* The values below are facts
about the exchange, checked against ``exchange_calendars``' XNYS rather than
against this module's own arithmetic.
"""

from datetime import date, datetime, timezone

import pytest

from corollary.calendars import (
    NYSE_DEFAULT_CLOSE,
    nyse_close_at,
    nyse_session_close,
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
