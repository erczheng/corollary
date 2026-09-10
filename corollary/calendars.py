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
"""

from datetime import date, datetime, time, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

__all__ = [
    "NYSE_DEFAULT_CLOSE",
    "NYSE_TZ",
    "nyse_close_at",
    "nyse_session_close",
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


@lru_cache(maxsize=1)
def _session_closes() -> dict[date, datetime]:
    """Every published NYSE session mapped to its closing instant, in UTC.

    Materialised into a plain dict once rather than queried through pandas on
    every call. The calendar is ~5,000 sessions, the build happens on first
    use, and the lookup afterwards costs a hash -- which matters because a
    200-row chain resolves a close per row.

    The import is deliberately inside the function. ``exchange_calendars``
    drags in pandas and numpy and takes about half a second to construct XNYS;
    paying that at ``import corollary.calendars`` would put it in the startup
    path of anything that merely mentions the module.
    """
    import exchange_calendars

    calendar = exchange_calendars.get_calendar(_NYSE_CALENDAR)
    return {
        session.date(): close.to_pydatetime().astimezone(timezone.utc)
        for session, close in calendar.closes.items()
    }


def nyse_session_close(day: date) -> datetime | None:
    """The instant NYSE closed (or will close) on ``day``, or ``None``.

    ``None`` means the calendar has no session for that date: a weekend, a
    holiday, or a date outside the published schedule. Three different reasons
    for one answer, and the caller that cares which should ask the calendar
    directly rather than have this function guess.
    """
    return _session_closes().get(day)


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
