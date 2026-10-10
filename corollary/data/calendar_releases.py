"""FRED's release calendar as ``economic`` calendar inputs (Phase 3 Q3, unit 7.2b-R).

Q3: *"FRED ``/releases/dates`` supplies the dates automatically; a committed
per-release ET time table (~15 rows) supplies the times; prior and actual come
from FRED after release. Finnhub's economic calendar is premium and is not
bought."*

What this module does, and only this:

* :func:`release_events` -- pure. Each FRED release date of a release that is
  in ``data/seeds/econ_release_times.csv`` becomes one
  :class:`~corollary.data.calendar_event.CalendarEventInput`: ``kind=economic``,
  ``source=fred``, ``date`` the release date, ``at`` the table's ET time on
  that date as a UTC instant (:meth:`EconReleaseTimes.at`), ``title`` the
  table's release name, ``vendor_id`` ``<release_id>:<date>``.
* :func:`fetch_release_events` -- one FRED read for ``today ..
  today + horizon`` and the mapping above. No storage, **no scheduler
  wiring**: a later unit wires every job and calls
  :func:`corollary.data.calendar.upsert_events` with the result.

What it refuses to do
---------------------

**Invent a time.** A release FRED lists that the table does not time is not
an event here -- FRED has ~800 dated rows a month, most of them daily series
(Coinbase, ICE BofA, Dow Jones) nobody trades around. Each is returned in
:attr:`ReleaseEvents.ignored` with every date it was listed on, so "nothing
for release 441" is a stated outcome, never a silent one.

**Supply a consensus.** ``estimate`` is always ``None``: consensus is
unavailable on every free source (Q3), and ``CalendarEventInput`` refuses one
on an economic row anyway.

**Choose a headline figure.** ``prior`` and ``actual`` are ``None`` today, and
deliberately so -- see :func:`actual_for`. The spec says they "come from FRED
after release" but never says which series and transform is a release's
headline figure, and that is an owner decision this unit does not make.

**Report an empty fetch as a success.** No key, or a fetch that maps to no
event at all, is :class:`ReleasesNotFetched` with the reason -- step 3's
convention (:class:`~corollary.data.macro.risk_free.NotRefreshed`), so a
scheduler never calls a FRED feed fresh that produced nothing.

A rescheduled release, and who removes the old date
---------------------------------------------------

``vendor_id`` is ``<release_id>:<date>`` because FRED's release calendar
carries no stable identity for one issue of a release -- no reference
period, no issue id; the date is all there is. :class:`CalendarEventInput`'s
docstring warns that a movable date in the ``vendor_id`` turns a reschedule
into a second row: if BLS moves CPI from the 14th to the 15th, the next fetch
lists ``10:<15th>`` and not ``10:<14th>``. Upsert alone leaves the 14th
behind. :func:`corollary.data.calendar.replace_window`, given this module's
complete result for ``start..end``, withdraws it (unit 7.2c-1). That is safe
only because a FRED read is all or nothing:
:meth:`~corollary.data.providers.fred.FredProvider.release_dates` raises
rather than return a short or repeated read.

A row :class:`CalendarEventInput` refuses is reported in
:attr:`ReleaseEvents.skipped` and logged, and costs no other row.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

from corollary.data.calendar_event import CalendarEventInput, CalendarKind, CalendarSource
from corollary.data.providers.fred import FRED_API_KEY_ENV, FredReleaseDate
from corollary.data.seeds.calendar_seeds import EconReleaseTimes, load_econ_release_times

__all__ = [
    "FRED_RELEASES_HORIZON_DAYS",
    "IgnoredRelease",
    "ReleaseDatesSource",
    "ReleaseEvents",
    "ReleasesNotFetched",
    "SkippedRelease",
    "actual_for",
    "fetch_release_events",
    "prior_for",
    "release_events",
    "release_vendor_id",
]

logger = logging.getLogger(__name__)

#: Days ahead of ``today`` one fetch covers. Thirty: the width step 0 probed
#: (842 rows, one page of the documented 1000), and wide enough that every
#: monthly release in the table appears at least once. One request a day
#: against the spec's FRED budget of <=20/day.
FRED_RELEASES_HORIZON_DAYS: Final = 30


class ReleaseDatesSource(Protocol):
    """What the fetch needs from FRED: release dates in a window.

    :class:`~corollary.data.providers.fred.FredProvider` satisfies it.
    """

    async def release_dates(self, start: date, end: date) -> Sequence[FredReleaseDate]: ...


@dataclass(frozen=True, slots=True)
class IgnoredRelease:
    """A release FRED listed that the times table does not time, and when it was listed."""

    release_id: int
    release_name: str
    dates: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class ReleaseEvents:
    """One window of FRED release dates, mapped.

    * ``events`` -- one input per ``(release, date)``, ordered ``(date, at,
      release_id)``.
    * ``ignored`` -- releases not in the table, by ``release_id``. No event.
    * ``unscheduled`` -- table release ids with no date in the window. Not an
      error by itself (the Employment Cost Index is quarterly), but a monthly
      release absent from a 30-day window is worth a look: a renumbered
      release id would show up here and in ``ignored`` at once.
    * ``name_mismatches`` -- ``(release_id, FRED's name)`` where FRED's name
      differs from the table's. The table's name is still the title; a
      mismatch may mean the id now names something else, which would make
      the table's time wrong for it.
    """

    start: date
    end: date
    events: tuple[CalendarEventInput, ...]
    ignored: tuple[IgnoredRelease, ...]
    unscheduled: tuple[int, ...]
    name_mismatches: tuple[tuple[int, str], ...]
    #: Timed release dates whose input was refused (a value too wide for its
    #: column, once ``prior``/``actual`` carry figures), ordered by
    #: ``(release_id, date)``. Each is logged; none costs another row.
    skipped: tuple["SkippedRelease", ...] = ()


@dataclass(frozen=True, slots=True)
class SkippedRelease:
    """A timed release date left out of :attr:`ReleaseEvents.events`, and why."""

    release_id: int
    date: date
    reason: str


def _skip(release_id: int, day: date, reason: str) -> SkippedRelease:
    logger.warning(
        "skipped FRED release %d on %s: %s",
        release_id,
        day.isoformat(),
        reason,
        extra={
            "event": "fred_release_row_skipped",
            "rule": (
                "unit 7.2c-1: a release date whose calendar input is refused is "
                "skipped and reported; it never costs the rest of the window"
            ),
            "release_id": release_id,
            "date": day.isoformat(),
            "reason": reason,
        },
    )
    return SkippedRelease(release_id=release_id, date=day, reason=reason)


@dataclass(frozen=True, slots=True)
class ReleasesNotFetched:
    """A run that completed without fault and produced no event, and why.

    Distinct from a failure (which raises) and from :class:`ReleaseEvents`.
    A scheduler records it as *skipped*, never as a success.
    """

    reason: str


def release_vendor_id(release_id: int, day: date) -> str:
    """``10:2026-10-14`` -- the release id and its date. See the module's *known limitation*."""
    return f"{release_id}:{day.isoformat()}"


def prior_for(release_id: int, day: date) -> Decimal | None:
    """The previous period's headline figure for this issue. **Always ``None`` today.**

    Blocked on the same owner decision as :func:`actual_for`; the two will be
    filled in together, from the same series and transform.
    """
    return None


def actual_for(release_id: int, day: date) -> Decimal | None:
    """This issue's headline figure, once published. **Always ``None`` today.**

    **The open question (sent to the owner, unit 7.2b-R):** Q3 says prior and
    actual "come from FRED after release", but not *which* FRED series and
    transform is a release's headline figure. For CPI (release 10) that could
    be the index level ``CPIAUCSL``, its month-over-month percent change, or
    its year-over-year change; for the Employment Situation (release 50),
    the ``PAYEMS`` monthly change or ``UNRATE``. Each choice is a different
    number under the same column, so it is not guessed here.

    The fill-in, once decided: a committed ``release_id -> (series_id,
    transform, unit)`` table beside ``econ_release_times.csv``, an observation
    fetch ~10 minutes after each scheduled ``at`` (spec *Feeds and budgets*),
    and this function -- or its replacement taking the stored observations --
    computing the figure as a ``Decimal`` from FRED's string values. The
    mapping in :func:`release_events` already routes ``prior`` and ``actual``
    through these two functions, so that change does not touch it.
    """
    return None


def _event(release_id: int, title: str, at: datetime, day: date) -> CalendarEventInput:
    return CalendarEventInput(
        kind=CalendarKind.ECONOMIC,
        source=CalendarSource.FRED,
        vendor_id=release_vendor_id(release_id, day),
        title=title,
        date=day,
        at=at,
        estimate=None,  # Q3: consensus is unavailable
        prior=prior_for(release_id, day),
        actual=actual_for(release_id, day),
    )


def release_events(
    rows: Iterable[FredReleaseDate], times: EconReleaseTimes, *, start: date, end: date
) -> ReleaseEvents:
    """Map FRED release dates to inputs through the times table. Pure and order-independent.

    ``start``/``end`` are the window that was asked for; they are carried on
    the result and used for ``unscheduled``, and rows are not filtered by
    them (FRED's window *is* the filter).
    """
    timed: dict[tuple[int, date], CalendarEventInput] = {}
    skipped: dict[tuple[int, date], SkippedRelease] = {}
    ignored: dict[int, tuple[str, set[date]]] = {}
    mismatches: dict[int, str] = {}
    for row in rows:
        known = times.by_id(row.release_id)
        if known is None:
            name, dates = ignored.setdefault(row.release_id, (row.release_name, set()))
            dates.add(row.date)
            continue
        if row.release_name != known.release_name:
            mismatches[row.release_id] = row.release_name
        key = (row.release_id, row.date)
        # FredProvider.release_dates refuses a read with a repeated pair
        # (unit 7.2c-1), so this only collapses a repeat in a caller's own
        # rows; the event depends on the key and the table alone, so the
        # copies cannot disagree.
        if key in timed or key in skipped:
            continue
        at = times.at(row.release_id, row.date)
        if at is None:  # pragma: no cover - by_id answered, so at() does
            raise AssertionError(f"release {row.release_id} is in the table but untimed")
        try:
            timed[key] = _event(row.release_id, known.release_name, at, row.date)
        except ValueError as exc:  # CalendarEventInput's refusals: this row only
            skipped[key] = _skip(row.release_id, row.date, str(exc))

    events = tuple(
        sorted(
            timed.values(),
            key=lambda e: (e.date, e.at, int(e.vendor_id.partition(":")[0])),
        )
    )
    # A skipped row was still scheduled by FRED; it is not "unscheduled".
    scheduled = {release_id for release_id, _ in timed} | {rid for rid, _ in skipped}
    return ReleaseEvents(
        start=start,
        end=end,
        events=events,
        ignored=tuple(
            IgnoredRelease(release_id=rid, release_name=name, dates=tuple(sorted(dates)))
            for rid, (name, dates) in sorted(ignored.items())
        ),
        unscheduled=tuple(
            sorted(row.release_id for row in times.rows if row.release_id not in scheduled)
        ),
        name_mismatches=tuple(sorted(mismatches.items())),
        skipped=tuple(skipped[key] for key in sorted(skipped)),
    )


async def fetch_release_events(
    fred: ReleaseDatesSource | None,
    *,
    today: date,
    horizon_days: int = FRED_RELEASES_HORIZON_DAYS,
    times: EconReleaseTimes | None = None,
) -> ReleaseEvents | ReleasesNotFetched:
    """Release dates for ``today .. today + horizon_days``, mapped. Nothing is stored.

    ``today`` is the **Eastern** session date, a ``date`` and never a
    ``datetime`` (a ``datetime`` is a ``date`` subclass and would type-check).
    ``fred`` is ``None`` when ``FRED_API_KEY`` is unset -- what
    ``api.deps.Registry.fred_provider`` answers then -- and that is a skip.
    A FRED failure raises (:class:`~corollary.data.providers.fred.FredError`)
    to the caller.
    """
    if isinstance(today, datetime) or not isinstance(today, date):
        raise ValueError(f"today must be a calendar date, got {type(today).__name__} ({today!r})")
    if horizon_days < 1:
        raise ValueError(f"horizon_days must be at least 1, got {horizon_days}")
    if fred is None:
        return ReleasesNotFetched(
            reason=f"FRED is unavailable ({FRED_API_KEY_ENV} is unset); "
            "no release dates were fetched"
        )
    table = load_econ_release_times() if times is None else times
    start, end = today, today + timedelta(days=horizon_days)
    rows = await fred.release_dates(start, end)
    result = release_events(rows, table, start=start, end=end)
    logger.info(
        "FRED release dates mapped",
        extra={
            "event": "fred_release_dates_mapped",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rows": len(rows),
            "events": len(result.events),
            "ignored_releases": len(result.ignored),
            "skipped": len(result.skipped),
            "unscheduled": list(result.unscheduled),
        },
    )
    for release_id, fred_name in result.name_mismatches:
        known = table.by_id(release_id)
        logger.warning(
            "FRED names a timed release differently from the times table",
            extra={
                "event": "fred_release_name_mismatch",
                "release_id": release_id,
                "fred_name": fred_name,
                "table_name": known.release_name if known is not None else None,
            },
        )
    if not result.events:
        return ReleasesNotFetched(
            reason=(
                f"FRED listed {len(rows)} release dates for {start.isoformat()}.."
                f"{end.isoformat()} and none became an event; ignored release ids: "
                f"{[i.release_id for i in result.ignored]}; skipped timed rows: "
                f"{len(result.skipped)}"
            )
        )
    return result
