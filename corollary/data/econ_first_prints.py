"""When an economic release's first print is fetched, and one fill run (spec R8, unit U6e).

The scheduler's ``econ_first_prints`` job runs :func:`fill_first_prints`.
This module holds the rule that decides, row by row, whether a release is due
an attempt, the one helper that makes an attempt, and the per-release
transaction that records it. The schedule that wakes the job is in
``corollary.engine.scheduler`` and reads this module's constants, so the two
cannot disagree about when an attempt is due.

**When a release is due (R8).** A release with no stored value is due an
attempt at each of these instants, and at no other:

* **its release day:** the release time plus :data:`ATTEMPT_DELAY` (10
  minutes), then every :data:`ATTEMPT_EVERY` (30 minutes), up to and including
  :data:`RELEASE_DAY_UNTIL` (18:00 ET) on the release's ET date. With the
  seed's three times that is 08:40, 09:25 and 10:10 ET first. The grid is the
  release's own, from its own calendar row: it is **not** gated on the NYSE
  session, because the jobs report is released on Good Friday, when NYSE is
  shut, and a half-day's 13:00 close says nothing about when FRED posts;
* **the daily catch-up:** :data:`CATCH_UP_AT` (10:00 ET) on every NYSE trading
  day -- the days ``calendar_releases`` refreshes the rows on, at the same
  time -- for every release whose first attempt instant has passed by then. That
  is R8's "release time has passed", read as "the release-day grid has begun":
  a 10:00 release is not fetched at 10:00 sharp, before it can have posted.

**The rule is stateless, so a wake costs nothing unless something is due.**
A release is due at ``now`` exactly when its latest due instant at or before
``now`` is later than its last recorded attempt (or there is none). The
evidence is ``econ_release_fetch``, the attempt table, so a restart, an extra
wake, or two runs in one slot never fetch twice, and a slot missed while the
process was down is fetched at the next run rather than lost. An attempt is
dated by the instant **before** its request, so it can only ever vouch for a
slot it started at or after.

**The window is every stored live economic row.** The calendar keeps no
retention, so "the stored calendar window" R8 names is the whole table; a
release before it was never stored and is never backfilled (spec, *Out of
scope*). ``unavailable`` rows are retried at every catch-up while they remain
(R8). That costs one request per unfilled row per trading day -- a refused
series' rows (``EXHOSLUSM495S``, monthly) accumulate one each a month.

**Failures are per release.** An exception from the fetch, from
:func:`~corollary.data.econ_values.first_print`, or from the store becomes
that release's ``error`` attempt (scrubbed through
:func:`~corollary.data.econ_values.attempt_of_error`) and the run moves on to
the next. A ``ValueError`` out of ``first_print`` is a caller's bug (a
frequency or series mismatch), logged at ERROR as one and recorded as an
``error`` -- never as ``not_posted``, ``no_value`` or ``refused``. The seed
load and the calendar read are **not** per release: a refused seed
(:class:`~corollary.data.seeds.SeedError`) or a corrupted calendar row fails
the whole run, the way ``calendar_releases`` fails on a refused seed.

**Budget.** One FRED request per attempt, through the provider's own
``api.stlouisfed.org`` limiter (120/min). No other request is made.

**Known limit: the schedule learns of a new row only when this job runs**
(U6e audit L2). The release-day wakes come from
:attr:`FirstPrintsRun.pending`, which the scheduler's ``ReleaseAttemptPlan``
takes from this job's own runs, and the scheduler sleeps toward the slot it
was last given without asking again. A row ``calendar_releases`` stores
*after* this job's last read is therefore not woken for until the job next
runs for another reason -- in the worst case the next 10:00 catch-up. Two
ways in: a first boot, where both start-up catch-ups run at once and this one
reads the table before the refresh has written it (a boot at 07:00 on a CPI
day first asks at 10:00, not 08:40); and a reschedule that the 10:00 refresh
writes just after this job's 10:00 run read the table (the next trading day's
release is then first asked at that day's 10:00). **Nothing is lost**: the due
rule is evaluated against the stored attempts at every run, so the catch-up
asks for the release and its release-day grid resumes from there
(``test_known_limit_a_row_stored_after_the_jobs_last_run_waits_for_the_next_catch_up``
pins that bound). What is lost is the early release-day attempts before that
catch-up. Refreshing the plan from ``calendar_releases`` would not close it on
its own, because the sleeping job would not re-read the plan; the fix is a
scheduler loop that re-asks a schedule when it signals a change, raced
against the sleep the way a cash-session wake is. That change is to the loop
every job shares, so it is deferred, not made here.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Protocol

from sqlalchemy.orm import Session

from corollary.calendars import nyse_session_open
from corollary.data.econ_store import (
    EconomicRelease,
    read_economic_releases,
    record_attempt,
    store_first_print,
)
from corollary.data.econ_values import (
    Attempt,
    Figure,
    FirstPrint,
    attempt_of,
    attempt_of_error,
    first_print,
)
from corollary.data.providers.fred import FredRefusal, FredVintageWindow, VintageFrequency
from corollary.data.seeds.calendar_seeds import ET_ZONE, EconReleaseSeeds, EconReleaseSeries

logger = logging.getLogger(__name__)

__all__ = [
    "ATTEMPT_DELAY",
    "ATTEMPT_EVERY",
    "CATCH_UP_AT",
    "RELEASE_DAY_UNTIL",
    "FirstPrintsRun",
    "ReleaseAttempt",
    "VintageSource",
    "catch_up_day",
    "fetch_first_print",
    "fill_first_prints",
    "is_due",
    "latest_due",
    "next_release_day_slot",
    "release_day_slots",
]

#: R8: the first attempt is the release time plus this.
ATTEMPT_DELAY: Final = timedelta(minutes=10)
#: R8: then every this, on the release day.
ATTEMPT_EVERY: Final = timedelta(minutes=30)
#: R8: the release day's attempts stop here, ET, on the release's ET date.
#: Inclusive: a slot landing exactly on it is still made.
RELEASE_DAY_UNTIL: Final = time(18, 0)
#: R8: the daily catch-up, ET, on NYSE trading days.
CATCH_UP_AT: Final = time(10, 0)

#: How far back :func:`latest_catch_up_slot` looks for a trading day. NYSE
#: never closes this long; past the published calendar there is no answer.
_CATCH_UP_LOOKBACK_DAYS: Final = 15

#: Every stored row: the calendar keeps no retention (module docstring).
_STORED_FROM: Final = date.min
_STORED_TO: Final = date.max


def _utc(moment: datetime, name: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got {moment!r}")
    return moment.astimezone(UTC)


# --------------------------------------------------------------------------
# When a release is due
# --------------------------------------------------------------------------


def catch_up_day(day: date) -> bool:
    """The catch-up runs on ``day``: NYSE holds a session, per the calendar. Half-days count."""
    return nyse_session_open(day) is not None


def _release_day_until(release_at: datetime) -> datetime:
    """:data:`RELEASE_DAY_UNTIL` on the release's ET date, as UTC."""
    day = release_at.astimezone(ET_ZONE).date()
    return datetime.combine(day, RELEASE_DAY_UNTIL, tzinfo=ET_ZONE).astimezone(UTC)


def release_day_slots(release_at: datetime) -> tuple[datetime, ...]:
    """Every release-day attempt instant, UTC, in order. Empty for a release after 17:50 ET.

    Arithmetic in UTC is ET wall-clock arithmetic here: a release day's slots
    lie between 08:40 and 18:00 ET, and no clock change falls in that span.
    """
    release_at = _utc(release_at, "release_at")
    first, until = release_at + ATTEMPT_DELAY, _release_day_until(release_at)
    slots: list[datetime] = []
    slot = first
    while slot <= until:
        slots.append(slot)
        slot += ATTEMPT_EVERY
    return tuple(slots)


def next_release_day_slot(release_at: datetime, after: datetime) -> datetime | None:
    """The first release-day slot **strictly after** ``after``, or ``None`` once the day is done."""
    release_at, after = _utc(release_at, "release_at"), _utc(after, "after")
    first, until = release_at + ATTEMPT_DELAY, _release_day_until(release_at)
    if after < first:
        candidate = first
    else:
        candidate = first + ATTEMPT_EVERY * ((after - first) // ATTEMPT_EVERY + 1)
    return candidate if candidate <= until else None


def _latest_release_day_slot(release_at: datetime, now: datetime) -> datetime | None:
    first, until = release_at + ATTEMPT_DELAY, _release_day_until(release_at)
    if now < first or first > until:
        return None
    return first + ATTEMPT_EVERY * ((min(now, until) - first) // ATTEMPT_EVERY)


def latest_catch_up_slot(now: datetime) -> datetime | None:
    """The latest catch-up instant at or before ``now``, UTC; ``None`` past the calendar."""
    now = _utc(now, "now")
    today = now.astimezone(ET_ZONE).date()
    for offset in range(_CATCH_UP_LOOKBACK_DAYS):
        day = today - timedelta(days=offset)
        if not catch_up_day(day):
            continue
        slot = datetime.combine(day, CATCH_UP_AT, tzinfo=ET_ZONE).astimezone(UTC)
        if slot <= now:
            return slot
    return None


def latest_due(release_at: datetime, now: datetime) -> datetime | None:
    """The release's latest due instant at or before ``now``; ``None`` before its first.

    The later of its latest release-day slot and the latest catch-up that
    came at or after its first attempt instant. See the module docstring.
    """
    release_at, now = _utc(release_at, "release_at"), _utc(now, "now")
    first = release_at + ATTEMPT_DELAY
    if now < first:
        return None
    candidates = [_latest_release_day_slot(release_at, now)]
    catch_up = latest_catch_up_slot(now)
    if catch_up is not None and catch_up >= first:
        candidates.append(catch_up)
    found = [slot for slot in candidates if slot is not None]
    return max(found) if found else None


def is_due(release_at: datetime, last_attempted_at: datetime | None, now: datetime) -> bool:
    """A release with no value is due an attempt at ``now``. See the module docstring."""
    due = latest_due(release_at, now)
    if due is None:
        return False
    return last_attempted_at is None or _utc(last_attempted_at, "last_attempted_at") < due


# --------------------------------------------------------------------------
# One attempt
# --------------------------------------------------------------------------


class VintageSource(Protocol):
    """What the job needs from FRED. :class:`~corollary.data.providers.fred.FredProvider` is one."""

    @property
    def secrets(self) -> Sequence[str]: ...

    async def vintage_window(
        self, series_id: str, release_day: date, *, frequency: VintageFrequency
    ) -> FredVintageWindow | FredRefusal: ...


async def fetch_first_print(
    source: VintageSource, series: EconReleaseSeries, release_day: date
) -> FirstPrint:
    """The release's figure: **one** request, at the seed row's frequency, read with that row.

    The only place the job calls ``vintage_window``. The frequency the
    window is fetched at and the row ``first_print`` reads it against are the
    same object's, so a window fetched for one frequency cannot be read
    against another row. Raises whatever the fetch raises, and
    ``first_print``'s ``ValueError`` for a mismatch the source introduced.
    """
    answer = await source.vintage_window(
        series.series_id, release_day, frequency=series.frequency
    )
    return first_print(answer, series)


@dataclass(frozen=True, slots=True)
class ReleaseAttempt:
    """What one release's attempt in a run came to, as recorded."""

    release_id: int
    release_date: date
    series_id: str
    attempt: Attempt
    #: The ``error`` came from a caller's bug (``first_print``'s ``ValueError``).
    bug: bool = False
    #: The attempt itself could not be recorded; the log has why.
    unrecorded: bool = False


@dataclass(frozen=True, slots=True)
class FirstPrintsRun:
    """One :func:`fill_first_prints` run."""

    #: The releases attempted, in calendar order.
    attempted: tuple[ReleaseAttempt, ...]
    #: Release instants, UTC and sorted, of the unfilled rows whose release
    #: day still has a slot after the run's ``now`` -- what the schedule
    #: wakes for between catch-ups.
    pending: tuple[datetime, ...]
    #: Stored rows whose release has left the seed: never attempted.
    unseeded: tuple[str, ...]

    @property
    def errors(self) -> tuple[ReleaseAttempt, ...]:
        return tuple(r for r in self.attempted if r.attempt.outcome == "error" or r.unrecorded)


def _read_blocking(
    session_factory: Callable[[], Session], seeds: EconReleaseSeeds
) -> tuple[EconomicRelease, ...]:
    with session_factory() as session:
        return read_economic_releases(session, _STORED_FROM, _STORED_TO, seeds=seeds)


def _record_blocking(
    session_factory: Callable[[], Session],
    release: EconomicRelease,
    series: EconReleaseSeries,
    result: FirstPrint | None,
    attempt: Attempt,
    *,
    started: datetime,
    fetched_at: datetime,
) -> None:
    """The figure, if any, and the attempt: one transaction. **Blocks.**"""
    with session_factory() as session:
        if isinstance(result, Figure):
            store_first_print(
                session,
                release.release_id,
                release.release_date,
                series,
                result,
                fetched_at=fetched_at,
            )
        record_attempt(session, release.release_id, release.release_date, attempt, at=started)
        session.commit()


def _log_attempt(
    release: EconomicRelease,
    series: EconReleaseSeries,
    attempt: Attempt,
    *,
    started: datetime,
    correlation_id: str,
    level: int = logging.INFO,
    event: str = "econ_first_print_attempt",
    message: str = "economic first-print attempt",
) -> None:
    logger.log(
        level,
        message,
        extra={
            "event": event,
            "correlation_id": correlation_id,
            "rule": "spec R8: a release's first print, from FRED's release-day vintage",
            "release_id": release.release_id,
            "release_date": release.release_date.isoformat(),
            "series_id": series.series_id,
            "frequency": series.frequency,
            "outcome": attempt.outcome,
            # Already scrubbed: attempt_of_error, or FRED's own refusal text.
            "detail": attempt.detail,
            "at": started.isoformat(),
        },
    )


async def _attempt(
    source: VintageSource,
    session_factory: Callable[[], Session],
    release: EconomicRelease,
    series: EconReleaseSeries,
    clock: Callable[[], datetime],
) -> ReleaseAttempt:
    """One release: fetch, record, and never raise (bar cancellation)."""
    correlation_id = uuid.uuid4().hex
    secrets = tuple(source.secrets)
    started = _utc(clock(), "clock()")
    bug = False
    result: FirstPrint | None = None
    try:
        result = await fetch_first_print(source, series, release.release_date)
        attempt = attempt_of(result)
    except ValueError as exc:
        # first_print's caller-bug signal -- or a fetch's ValueError, which is
        # as much a bug: the provider raises FredError (a RuntimeError).
        bug = True
        attempt = attempt_of_error(exc, secrets=secrets)
        _log_attempt(
            release,
            series,
            attempt,
            started=started,
            correlation_id=correlation_id,
            level=logging.ERROR,
            event="econ_first_print_bug",
            message="economic first print raised ValueError: a caller bug, recorded as an error",
        )
    except Exception as exc:
        attempt = attempt_of_error(exc, secrets=secrets)
    fetched_at = _utc(clock(), "clock()")

    try:
        await asyncio.to_thread(
            _record_blocking,
            session_factory,
            release,
            series,
            result,
            attempt,
            started=started,
            fetched_at=fetched_at,
        )
    except Exception as exc:
        # The store refused (or the database failed). Record that as this
        # release's error, in a fresh transaction, and move on.
        attempt = attempt_of_error(exc, secrets=secrets)
        bug = bug or isinstance(exc, (ValueError, TypeError))
        try:
            await asyncio.to_thread(
                _record_blocking,
                session_factory,
                release,
                series,
                None,
                attempt,
                started=started,
                fetched_at=fetched_at,
            )
        except Exception as again:
            _log_attempt(
                release,
                series,
                attempt_of_error(again, secrets=secrets),
                started=started,
                correlation_id=correlation_id,
                level=logging.ERROR,
                event="econ_first_print_unrecorded",
                message="an economic first-print attempt could not be recorded",
            )
            return ReleaseAttempt(
                release.release_id,
                release.release_date,
                series.series_id,
                attempt,
                bug=bug,
                unrecorded=True,
            )

    _log_attempt(
        release,
        series,
        attempt,
        started=started,
        correlation_id=correlation_id,
        level=logging.WARNING if attempt.outcome == "error" else logging.INFO,
    )
    return ReleaseAttempt(
        release.release_id, release.release_date, series.series_id, attempt, bug=bug
    )


async def fill_first_prints(
    source: VintageSource,
    session_factory: Callable[[], Session],
    seeds: EconReleaseSeeds,
    *,
    clock: Callable[[], datetime],
) -> FirstPrintsRun:
    """Attempt every stored economic release that is due at ``clock()``, one at a time.

    Each attempt is its own transaction and its own failure; see the module
    docstring. Raises only for what is not one release's: the calendar read
    (a corrupted economic row raises ``ValueError`` there, by design).
    """
    now = _utc(clock(), "clock()")
    releases = await asyncio.to_thread(_read_blocking, session_factory, seeds)

    attempted: list[ReleaseAttempt] = []
    unseeded: list[str] = []
    filled: set[tuple[int, date]] = set()
    for release in releases:
        if release.value is not None:
            continue
        series = release.series
        if series is None:
            unseeded.append(f"{release.release_id}:{release.release_date.isoformat()}")
            continue
        last = None if release.last_attempt is None else release.last_attempt.attempted_at
        if not is_due(release.release_at, last, now):
            continue
        outcome = await _attempt(source, session_factory, release, series, clock)
        attempted.append(outcome)
        if outcome.attempt.outcome == "figure" and not outcome.unrecorded:
            filled.add((release.release_id, release.release_date))

    if unseeded:
        logger.warning(
            "stored economic releases have no seed row and were not fetched",
            extra={
                "event": "econ_first_print_unseeded",
                "rule": "spec R7: a release's series comes from econ_release_series.csv",
                "releases": unseeded,
                "at": now.isoformat(),
            },
        )

    pending = sorted(
        {
            release.release_at
            for release in releases
            if release.value is None
            and release.series is not None
            and (release.release_id, release.release_date) not in filled
            and next_release_day_slot(release.release_at, now) is not None
        }
    )
    return FirstPrintsRun(tuple(attempted), tuple(pending), tuple(unseeded))
