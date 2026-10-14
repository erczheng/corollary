"""Session boundaries for the exchanges this app trades on.

CLAUDE.md: *"Session boundaries come from a market calendar, never hardcoded
09:30-16:00. Half-days and holidays are real."*

The one caller today is time-to-expiry. A contract expiring on the day after
Thanksgiving settles against a **13:00 ET** close, and measuring it to 16:00
overstates ``years`` by three hours on the single day of a contract's life
where theta is largest. There are about ten such days a year and they are not
distributed randomly -- they cluster on the Friday after Thanksgiving and
Christmas Eve, both of which fall in the busiest weekly-expiry season of the
year.

Why this is its own module rather than part of ``pricing.blackscholes``
-----------------------------------------------------------------------

``blackscholes`` is pure arithmetic over floats and imports nothing but
``math``. ``exchange_calendars`` pulls in pandas and numpy and takes roughly
half a second to build XNYS. Importing that into the maths would make the
pricer expensive to import, hard to test in isolation, and impossible to run
anywhere the dependency is absent.

So the dependency inverts: :func:`nyse_close_at` matches the
``Callable[[date], datetime]`` shape ``years_to_expiry`` accepts, and the
composition happens at the provider. The pricer keeps its own 16:00 ET default
for callers who have no calendar to hand, and that default is *right* on every
day except the half-days -- which is exactly why the bug was invisible.

The fallback, and why it is not a raise
---------------------------------------

``exchange_calendars`` publishes a bounded range (XNYS currently ends in the
year after next). A January 2029 LEAP is an ordinary contract that the
calendar simply cannot answer for, and so is any date past the end of the
published schedule. Refusing to price it would take down a whole chain over
one far-dated row. The fallback is the ordinary 16:00 ET close: wrong by three
hours on a handful of days a decade out, against a contract whose ``years`` is
dominated by everything else about it.

:func:`nyse_session_close` is the honest version -- ``None`` when the calendar
has nothing to say -- and is what a caller that needs to *know* should ask.

The second caller is the intraday price series
--------------------------------------------------

Alpaca's bars feed runs roughly 04:00--20:00 ET, so a session of ``5Min``
bars arrives 192 long where the regular session is 78. Scoping a chart to
regular hours needs the **open** as well as the close, and both come from
here for the same reason: a half-day closes at 13:00 and a hardcoded
09:30--16:00 filter would append three hours of post-market prints to the
session they follow. :func:`nyse_session_open` is the mirror of
:func:`nyse_session_close`, with the same ``None`` for a non-session day.

The third caller is the sentiment self-audit
--------------------------------------------

Decision 12 grades a label over a window measured in **regular-session
minutes**: 1h is the reference plus 60 of them, 1d plus 390. A label at 15:30
has its 1h window end at 10:00 the next session, not at 16:30 in an empty
market, and a half-day has 210 minutes to give rather than 390.
:func:`add_session_minutes` is that clock, built on the same cached sessions
and nothing else -- no 09:30, no 16:00, no weekday test anywhere. Sessions are
half-open, ``[open, close)``: the close is the instant the last minute ends,
so a window may *end* on it, but an instant *at* it is out of session and
counts from the next open. Unlike :func:`nyse_close_at`, nothing here falls
back: an answer past the published schedule raises, because an invented
session would grade a label against a market that was never open.
"""

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Final, NamedTuple
from zoneinfo import ZoneInfo

__all__ = [
    "NYSE_DEFAULT_CLOSE",
    "NYSE_TZ",
    "add_session_minutes",
    "nyse_close_at",
    "nyse_next_open",
    "nyse_session_close",
    "nyse_session_containing",
    "nyse_session_open",
]

#: The regular-session close, in Eastern. Stated as a wall-clock time in ET
#: rather than as 21:00 UTC on purpose: the US moves an hour twice a year and
#: the exchange does not, so a UTC constant is wrong for four months of it.
NYSE_DEFAULT_CLOSE = time(16, 0)

NYSE_TZ = ZoneInfo("America/New_York")

#: The XNYS code in ``exchange_calendars``. NYSE and Nasdaq keep the same
#: session schedule, and every equity and equity option this app touches
#: settles against it.
_NYSE_CALENDAR = "XNYS"

#: How many calendar days :func:`nyse_next_open` looks ahead before deciding
#: the published schedule has ended. The longest real gap between NYSE
#: sessions is a holiday weekend (four days); a fortnight of no sessions is
#: the calendar running out, not the market closing.
_NEXT_OPEN_SEARCH_DAYS: Final = 14


class _Session(NamedTuple):
    """One published session's two boundaries, both UTC instants."""

    opens: datetime
    closes: datetime


@lru_cache(maxsize=1)
def _sessions() -> dict[date, _Session]:
    """Every published NYSE session mapped to its two boundaries, in UTC.

    Materialised into a plain dict once rather than queried through pandas on
    every call. The calendar is ~5,000 sessions, the build happens on first
    use, and the lookup afterwards costs a hash -- which matters because a
    200-row chain resolves a close per row, and a day of ``1Min`` bars
    resolves a session per bar.

    Both ends come out of **one** build, keyed on the session date the two
    pandas Series share. Two independently cached dicts would be two
    half-second builds and, worse, two places for the pair to disagree about
    which days are sessions at all. A date missing from either side is
    dropped: a session with only one boundary cannot bound anything.

    The import is deliberately inside the function. ``exchange_calendars``
    drags in pandas and numpy and takes about half a second to construct XNYS;
    paying that at ``import corollary.calendars`` would put it in the startup
    path of anything that merely mentions the module.
    """
    import exchange_calendars

    calendar = exchange_calendars.get_calendar(_NYSE_CALENDAR)
    opens: dict[date, datetime] = {
        session.date(): opened.to_pydatetime().astimezone(timezone.utc)
        for session, opened in calendar.opens.items()
    }
    return {
        session.date(): _Session(
            opens[session.date()], close.to_pydatetime().astimezone(timezone.utc)
        )
        for session, close in calendar.closes.items()
        if session.date() in opens
    }


def nyse_session_close(day: date) -> datetime | None:
    """The instant NYSE closed (or will close) on ``day``, or ``None``.

    ``None`` means the calendar has no session for that date: a weekend, a
    holiday, or a date outside the published schedule. Three different reasons
    for one answer, and the caller that cares which should ask the calendar
    directly rather than have this function guess.
    """
    session = _sessions().get(day)
    return None if session is None else session.closes


def nyse_session_open(day: date) -> datetime | None:
    """The instant NYSE opened (or will open) on ``day``, or ``None``.

    The mirror of :func:`nyse_session_close`, from the same cached build and
    with the same ``None`` semantics. 09:30 ET on every session including the
    half-days -- an early close is the only thing a half-day changes -- but
    read rather than assumed, because "the open never moves" is exactly the
    kind of fact that holds until the day it does not.

    There is no ``nyse_open_at`` fallback twin. The close has one because a
    LEAP past the end of the published schedule still has to be priced; the
    only caller here is filtering bars that already exist, and a bar on a date
    the calendar cannot answer is one this app would rather drop than guess a
    session for.
    """
    session = _sessions().get(day)
    return None if session is None else session.opens


def nyse_close_at(day: date) -> datetime:
    """The close on ``day``, falling back to 16:00 ET when unknown.

    This is the resolver shape
    :func:`corollary.pricing.blackscholes.years_to_expiry` accepts. See the
    module docstring for why an unanswerable date falls back rather than
    raising.
    """
    close = nyse_session_close(day)
    if close is not None:
        return close
    return datetime.combine(day, NYSE_DEFAULT_CLOSE, tzinfo=NYSE_TZ).astimezone(
        timezone.utc
    )


def _require_aware(instant: datetime) -> None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(
            f"session arithmetic needs a timezone-aware instant, got {instant!r}"
        )


def nyse_session_containing(instant: datetime) -> date | None:
    """The session date whose ``[open, close)`` contains ``instant``, or ``None``.

    Half-open on purpose: the open's own instant is the first moment of the
    session, and the close's is the moment it ended -- 16:00:00 is after the
    market, not in its last minute. ``None`` for pre-market, after-hours, a
    weekend, a holiday, the afternoon of a half-day, and anything past the
    published schedule. NYSE sessions never span midnight Eastern, so the
    only candidate is the instant's own Eastern date.
    """
    _require_aware(instant)
    day = instant.astimezone(NYSE_TZ).date()
    session = _sessions().get(day)
    if session is None:
        return None
    if session.opens <= instant < session.closes:
        return day
    return None


def nyse_next_open(instant: datetime) -> datetime:
    """The first session open at or after ``instant``, as a UTC instant.

    *At or after*: an instant exactly on an open answers that open. Raises
    ``ValueError`` when no open exists within :data:`_NEXT_OPEN_SEARCH_DAYS`
    -- which, given the longest real gap is four days, means the published
    schedule has ended.
    """
    _require_aware(instant)
    day = instant.astimezone(NYSE_TZ).date()
    sessions = _sessions()
    for offset in range(_NEXT_OPEN_SEARCH_DAYS + 1):
        session = sessions.get(day + timedelta(days=offset))
        if session is not None and session.opens >= instant:
            return session.opens
    raise ValueError(
        f"no NYSE open within {_NEXT_OPEN_SEARCH_DAYS} days of {instant.isoformat()}: "
        "past the published schedule"
    )


def add_session_minutes(instant: datetime, minutes: int) -> datetime:
    """``instant`` plus ``minutes`` of **regular-session** time, as a UTC instant.

    Decision 12's window clock. Time only passes while the market is open:

    * an ``instant`` inside a session counts from itself;
    * one outside every session -- pre-market, after the close, a weekend, a
      holiday -- counts from :func:`nyse_next_open`, so ``minutes=0`` answers
      the next open;
    * a count that reaches a session's close exactly **ends on the close**
      (09:30 plus 390 is 16:00 the same day), and one that runs past it
      carries the remainder into the next session's open. A half-day's close
      is its 13:00, read from the calendar like every other boundary.

    Raises ``ValueError`` on a naive instant, a negative count, or an answer
    past the published schedule -- never a fallback session.
    """
    _require_aware(instant)
    if minutes < 0:
        raise ValueError(f"session minutes must be non-negative, got {minutes}")
    remaining = timedelta(minutes=minutes)
    day = nyse_session_containing(instant)
    if day is None:
        cursor = nyse_next_open(instant)
        day = cursor.astimezone(NYSE_TZ).date()
    else:
        cursor = instant.astimezone(timezone.utc)
    sessions = _sessions()
    while True:
        close = sessions[day].closes
        available = close - cursor
        if remaining <= available:
            return cursor + remaining
        remaining -= available
        cursor = nyse_next_open(close)
        day = cursor.astimezone(NYSE_TZ).date()
