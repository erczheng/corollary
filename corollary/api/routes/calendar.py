"""The News calendar: the range read and the manual geopolitical rows (Phase 3 step 7).

The spec's *API* section, verbatim: *"All read routes are plain ``GET``s
depending on no broker"*, and *"``GET /api/calendar?from&to``;
``POST``/``PUT``/``DELETE /api/calendar/manual/{id}`` for geopolitical rows
only -- a vendor or seeded row is not editable."* No route here depends on a
broker or a provider: the rows are what the scheduler's five calendar jobs
stored, and the job freshness is the scheduler's in-memory record, read off
``app.state``. ``tests/api/test_calendar_routes.py`` walks each route's
dependency tree to keep it so.

``GET /api/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD``
---------------------------------------------------

* **Both bounds are required, inclusive, and Eastern dates.** ``from`` after
  ``to``, a malformed or impossible date, or a range wider than
  :data:`CALENDAR_MAX_SPAN_DAYS` dates is a 422 (rule 4: the bound is the
  server's, never the client's).
* **Grouped by ``date``, never by ``at``'s UTC day**
  (:func:`~corollary.data.calendar.group_by_date`): a release at 20:30 ET on
  11 Aug is ``00:30Z`` on the 12th and is served under the 11th.
* **Decimal figures are exact strings** (:data:`~corollary.api.schemas.DecimalString`),
  never JSON numbers.
* **Consensus is stated, not null.** Every ``economic`` row carries
  ``consensus: "unavailable"`` (Q3); every other kind carries ``null``.
* **Notices** say aloud what the rows cannot: central-bank seed gaps for each
  year the range touches (decision 8 -- no file, unpublished, partial, or a
  seed file that fails validation); each calendar job's last success,
  failure and skip with its reason, a Finnhub premium refusal
  (``CalendarAccessDenied``, Q15) distinguished from an outage, and a
  start-up catch-up that found the rows fresh (``fresh_at_start``) kept
  apart from a real skip; and that an economic row's ``prior``/``actual``
  are pending an owner decision. Every instant in a notice's ``message``
  is in ``America/New_York`` and every span is in words.
* **"None in this range" is said only of dates a fetch asked about.** Each
  job fetches a window from the Eastern date it ran on (:data:`CALENDAR_JOBS`,
  built from the fetchers' own horizon constants), and a range may reach
  past it. ``coveredThrough`` is that window's last date; the part of the
  range after it is stated as *not covered*, never as empty.

The manual routes
-----------------

Decision 9: a geopolitical row, ``source = 'manual'``, removal a soft
delete, and **not** in the configuration audit log.

* **Every field is validated server-side** -- by
  :class:`~corollary.api.schemas.ManualCalendarEventRequest` (a trimmed,
  non-blank, one-line title of at most ``TITLE_MAX``; ``date`` as
  ``YYYY-MM-DD`` text; ``at`` with an offset or null), by the year bound
  here (:data:`MANUAL_YEAR_MIN`..:data:`MANUAL_YEAR_MAX`), and again by the
  store (``at``'s Eastern date must be ``date``). A refusal is a 422.
* **Status codes.** Created: 201. Edited: 200. Removed: 204. Unknown or
  already removed: 404. A vendor or seed row: **409** -- the row exists and
  the request conflicts with what it is; 403 would read as a permissions
  problem a different user could get past, and there is only one user.
* **Notifies** ``calendar_changed`` (decision 20, as
  :mod:`corollary.api.operator` records the reading): ``info``, bell off,
  Discord on, after the commit, built from the stored row. A request that
  changed nothing (an edit to identical values) and a refused request emit
  nothing.
* **Every refusal is logged** with its rule, inputs and time (rule 8's
  standard, kept for every refusal in this API), by one of two paths. A
  refusal this module raises goes through :func:`_refuse`, whose
  ``inputs`` name a body by its date, instant and title *length*. A refusal
  FastAPI raises before the handler runs -- the body schema (a blank or
  control-character title, an extra ``source`` or ``kind``, a malformed
  ``date`` or ``at``), a missing ``from``/``to``, an out-of-range path
  id -- is logged by ``corollary.api.app``'s shared validation handler as
  ``api_request_refused``, with the field locations and error types and
  never a value. Neither path ever logs a title's text.
"""

import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path as FilePath
from typing import Annotated, Final, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, Request, Response
from sqlalchemy.orm import Session

from corollary.api.deps import ApiError, SessionDep
from corollary.api.operator import calendar_notice, notify_after_response
from corollary.api.schemas import (
    CalendarDay,
    CalendarEventItem,
    CalendarJobName,
    CalendarJobNotice,
    CalendarJobState,
    CalendarNotices,
    CalendarRange,
    CalendarReleaseFiguresNotice,
    CalendarSeedGapNotice,
    ManualCalendarEventRequest,
)
from corollary.data.calendar import (
    CalendarEventNotEditableError,
    CalendarEventNotFoundError,
    StoredCalendarEvent,
    create_manual,
    delete_manual,
    group_by_date,
    read_range,
    seed_gaps_between,
    update_manual,
)
from corollary.data.calendar_dividends import DIVIDEND_EX_DATE_HORIZON
from corollary.data.calendar_event import CalendarKind
from corollary.data.calendar_finnhub import EARNINGS_WINDOW, IPO_WINDOW
from corollary.data.calendar_releases import FRED_RELEASES_HORIZON_DAYS
from corollary.data.seeds import SeedError
from corollary.db.models import CalendarEvent
from corollary.engine.scheduler import (
    CALENDAR_CENTRAL_BANK_YEARS_AHEAD,
    JobStatus,
    days_in_words,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

EASTERN: Final = ZoneInfo("America/New_York")

#: The widest range ``GET /api/calendar`` serves, counted in dates
#: inclusively: ``from`` and ``to`` may be at most 91 days apart. About a
#: quarter -- wider than any job's forward window (dividends reach +90
#: days), and narrow enough that one read stays one small query.
CALENDAR_MAX_SPAN_DAYS: Final = 92

#: The years a manual entry may fall in. Not a calendar policy: a bound so a
#: year-1 or year-9999 instant cannot overflow the Eastern conversion the
#: store performs and turn a bad request into a 500.
MANUAL_YEAR_MIN: Final = 2000
MANUAL_YEAR_MAX: Final = 2099

#: Row ids are SQLite INTEGERs: a larger path id would overflow at bind time.
_ID_MAX: Final = 2**63 - 1

def _forward(horizon: timedelta) -> Callable[[date], tuple[date, date]]:
    """A job whose fetch on ``day`` asks about ``day .. day + horizon``, inclusive."""

    def window(day: date) -> tuple[date, date]:
        return (day, day + horizon)

    return window


def _central_bank_years(day: date) -> tuple[date, date]:
    """The seed years the central-bank job imports when it runs on ``day``."""
    return (date(day.year, 1, 1), date(day.year + CALENDAR_CENTRAL_BANK_YEARS_AHEAD, 12, 31))


@dataclass(frozen=True, slots=True)
class CalendarJobSpec:
    """What the route knows about one calendar job.

    ``window`` is the Eastern dates a fetch run on a given day asked about,
    built from the **same** constant the fetcher uses (``EARNINGS_WINDOW``,
    ``IPO_WINDOW``, ``DIVIDEND_EX_DATE_HORIZON``, ``FRED_RELEASES_HORIZON_DAYS``,
    ``CALENDAR_CENTRAL_BANK_YEARS_AHEAD``) -- never a second copy of the
    number. ``reach`` says how far that is, in words.
    """

    kinds: tuple[CalendarKind, ...]
    label: str
    window: Callable[[date], tuple[date, date]]
    reach: str


#: The five calendar jobs, in panel order. The names are repeated from
#: ``engine/scheduler.py``'s ``_calendar_jobs`` on purpose -- a test pins
#: them against the real scheduler's job list, so a renamed job cannot
#: silently read as ``not_scheduled``.
CALENDAR_JOBS: Final[Mapping[CalendarJobName, CalendarJobSpec]] = {
    "calendar_earnings": CalendarJobSpec(
        kinds=(CalendarKind.EARNINGS,),
        label="Earnings",
        window=_forward(EARNINGS_WINDOW),
        reach=f"looks {days_in_words(EARNINGS_WINDOW)} ahead",
    ),
    "calendar_ipo": CalendarJobSpec(
        kinds=(CalendarKind.IPO,),
        label="IPOs",
        window=_forward(IPO_WINDOW),
        reach=f"looks {days_in_words(IPO_WINDOW)} ahead",
    ),
    "calendar_dividends": CalendarJobSpec(
        kinds=(CalendarKind.DIVIDEND,),
        label="Dividends",
        window=_forward(DIVIDEND_EX_DATE_HORIZON),
        reach=f"looks {days_in_words(DIVIDEND_EX_DATE_HORIZON)} ahead",
    ),
    "calendar_releases": CalendarJobSpec(
        kinds=(CalendarKind.ECONOMIC,),
        label="Economic releases",
        window=_forward(timedelta(days=FRED_RELEASES_HORIZON_DAYS)),
        reach=f"looks {days_in_words(timedelta(days=FRED_RELEASES_HORIZON_DAYS))} ahead",
    ),
    "calendar_central_banks": CalendarJobSpec(
        kinds=(CalendarKind.CENTRAL_BANK,),
        label="Central-bank decisions",
        window=_central_bank_years,
        reach=(
            "imports the seed for the year it runs in and "
            + (
                "the next"
                if CALENDAR_CENTRAL_BANK_YEARS_AHEAD == 1
                else f"the {CALENDAR_CENTRAL_BANK_YEARS_AHEAD} years after it"
            )
        ),
    ),
}

#: The error type the scheduler records for Q15's premium refusal.
ACCESS_DENIED_ERROR: Final = "CalendarAccessDenied"

#: Unit 7.2b-R's open question, stated on every read.
RELEASE_FIGURES_PENDING: Final = (
    "Prior and actual for economic releases are not filled yet: Q3 says they "
    "come from FRED after release, but which FRED series and transform is each "
    "release's headline figure (CPI's index level, or its monthly or yearly "
    "change) is an open owner decision, and no number is guessed in the meantime."
)

_RANGE_RULE: Final = (
    "GET /api/calendar: from and to are YYYY-MM-DD Eastern dates, from <= to, "
    f"at most {CALENDAR_MAX_SPAN_DAYS} dates inclusive (rule 4: the server bounds it)"
)
_MANUAL_RULE: Final = (
    "decision 9: only manual geopolitical rows are created, edited or removed "
    "here; every field is validated server-side (rule 4)"
)

#: ``YYYY-MM-DD`` in ASCII digits; ``date.fromisoformat`` alone would also
#: take ``20261014`` and other ISO spellings.
_ISO_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
IdPath = Annotated[int, Path(ge=1, le=_ID_MAX)]


def request_now() -> datetime:
    """The request's clock, aware UTC. A dependency so a test can fix it."""
    return datetime.now(timezone.utc)


def central_bank_seed_dir() -> FilePath | None:
    """Where the central-bank seed files are read from; ``None`` is the committed set."""
    return None


NowDep = Annotated[datetime, Depends(request_now)]
SeedDirDep = Annotated[FilePath | None, Depends(central_bank_seed_dir)]

#: What the route reads from the scheduler: ``Scheduler.status``.
StatusReader = Callable[[], Mapping[str, JobStatus]]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def _eastern(moment: datetime) -> str:
    """``2026-10-14 07:00 EDT`` -- stored UTC, displayed in New York."""
    return moment.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M %Z")


def _eastern_date(moment: datetime) -> date:
    return moment.astimezone(EASTERN).date()


def event_item(event: StoredCalendarEvent) -> CalendarEventItem:
    """One stored row on the wire. ``consensus`` is stated on economic rows (Q3)."""
    return CalendarEventItem(
        id=str(event.id),
        date=event.date,
        at=event.at,
        type=event.kind.value,
        title=event.title,
        ticker=event.ticker,
        source=event.source.value,
        editable=event.editable,
        session=None if event.session is None else event.session.value,
        estimate=event.estimate,
        prior=event.prior,
        actual=event.actual,
        consensus="unavailable" if event.kind is CalendarKind.ECONOMIC else None,
        unit=event.unit,
        exchange=event.exchange,
        shares=event.shares,
        price_low=event.price_low,
        price_high=event.price_high,
        ipo_status=None if event.ipo_status is None else event.ipo_status.value,
    )


def _latest_outcome(status: JobStatus) -> Literal["ok", "failed", "skipped"] | None:
    """The newest of the three outcomes; a tie reads pessimistically."""
    candidates: list[tuple[datetime, int, Literal["ok", "failed", "skipped"]]] = []
    if status.last_success is not None:
        candidates.append((status.last_success, 0, "ok"))
    if status.last_skipped is not None:
        candidates.append((status.last_skipped, 1, "skipped"))
    if status.last_failure is not None:
        candidates.append((status.last_failure, 2, "failed"))
    if not candidates:
        return None
    return max(candidates)[2]


def _stale_since(status: JobStatus) -> str:
    if status.last_success is None:
        return "never fetched successfully in this process"
    return f"stale since {_eastern(status.last_success)}"


def _sentence(text: str) -> str:
    """Upper-case the first letter only. ``str.capitalize`` would lower ``EDT``."""
    return text[:1].upper() + text[1:]


def job_state(status: JobStatus | None, *, scheduler_running: bool) -> CalendarJobState:
    """Which of :data:`~corollary.api.schemas.CalendarJobState` a job is in."""
    if not scheduler_running:
        return "scheduler_not_running"
    if status is None:
        return "not_scheduled"
    outcome = _latest_outcome(status)
    if outcome is None:
        return "never_run"
    if outcome == "failed":
        if status.last_error_type == ACCESS_DENIED_ERROR:
            return "access_denied"
        return "failing"
    if outcome == "skipped":
        if status.last_skip_fresh_as_of is not None:
            return "fresh_at_start"
        return "skipped"
    return "ok"


def fetched_on(status: JobStatus | None) -> date | None:
    """The Eastern date the newest fetch known to have succeeded ran on.

    A success in this process is dated by its **start**, since the fetchers
    take "today" as the run begins: a run that began at 23:59 ET and finished
    after midnight asked about the earlier day. The start is
    ``last_success_started``, which the scheduler records at the moment of
    success -- never ``last_started``, which every later run overwrites as it
    begins, and never the finish, which can be a day later than the fetch.
    A success with no recorded start contributes nothing: undated, it claims
    no coverage at all rather than a guessed one.

    A ``fresh_at_start`` skip contributes the newest stored row's write
    date -- the only evidence of an earlier process's fetch. A fetch stamps
    its rows' ``updated_at`` with the same pre-fetch clock reading its window
    was dated from (the job's ``now``, passed through to the store), so that
    date *is* the fetch's day: it overstates nothing, even for a fetch that
    straddled midnight ET. An unchanged row keeps its older stamp, which can
    only date the evidence earlier -- the safe direction.
    """
    if status is None:
        return None
    days: list[date] = []
    if status.last_success is not None and status.last_success_started is not None:
        days.append(_eastern_date(status.last_success_started))
    if status.last_skip_fresh_as_of is not None:
        days.append(_eastern_date(status.last_skip_fresh_as_of))
    return max(days, default=None)


@dataclass(frozen=True, slots=True)
class Coverage:
    """Which part of a requested range the newest known fetch asked about."""

    #: The fetch's window, inclusive.
    first: date
    through: date
    #: The requested range intersected with the window; ``None`` if disjoint.
    covered: tuple[date, date] | None
    #: Rows of the job's kinds inside ``covered``.
    rows_covered: int


def coverage(
    spec: CalendarJobSpec,
    day: date,
    start: date,
    end: date,
    events: Sequence[StoredCalendarEvent],
) -> Coverage:
    """The part of ``[start, end]`` a fetch run on ``day`` covered."""
    first, through = spec.window(day)
    low, high = max(start, first), min(end, through)
    covered = (low, high) if low <= high else None
    rows = (
        0
        if covered is None
        else sum(
            1 for event in events if event.kind in spec.kinds and low <= event.date <= high
        )
    )
    return Coverage(first=first, through=through, covered=covered, rows_covered=rows)


def _none_clause(cover: Coverage | None, start: date, end: date) -> str | None:
    """``none in this range`` -- said only of dates the fetch asked about."""
    if cover is None or cover.covered is None or cover.rows_covered:
        return None
    low, high = cover.covered
    if (low, high) == (start, end):
        return "none in this range"
    return f"none from {low.isoformat()} through {high.isoformat()}"


def _coverage_sentences(
    spec: CalendarJobSpec, cover: Coverage | None, start: date, end: date
) -> str:
    """What the fetch did not ask about, before and after its window."""
    if cover is None:
        return ""
    sentences: list[str] = []
    if start < cover.first:
        sentences.append(
            "Only what earlier fetches stored is shown for dates before "
            f"{cover.first.isoformat()}."
        )
    if end > cover.through:
        sentences.append(
            f"Not covered: the job {spec.reach}, so dates after "
            f"{cover.through.isoformat()} have not been fetched."
        )
    return "".join(f" {sentence}" for sentence in sentences)


def job_message(
    spec: CalendarJobSpec,
    state: CalendarJobState,
    status: JobStatus | None,
    cover: Coverage | None,
    start: date,
    end: date,
) -> str:
    """The panel's sentence for one job. Every state is written out.

    Every instant is rendered in ``America/New_York`` and every span in
    words; nothing the scheduler wrote for its own log is quoted.
    """
    label = spec.label
    tail = _coverage_sentences(spec, cover, start, end)
    if state == "scheduler_not_running":
        return (
            f"{label}: no context scheduler is running in this process, so nothing "
            "is being refreshed; the rows shown are as last stored."
        )
    if state == "not_scheduled" or status is None:
        return f"{label}: the running scheduler has no job for this calendar."
    if state == "never_run":
        upcoming = (
            f"; the first run is due {_eastern(status.next_run)}"
            if status.next_run is not None
            else ""
        )
        return f"{label}: not fetched yet in this process{upcoming}."
    if state == "access_denied":
        assert status.last_failure is not None
        return (
            f"{label}: Finnhub refused this calendar on this key at "
            f"{_eastern(status.last_failure)} (a premium endpoint, or a key it does "
            f"not accept); it goes back to the owner (Q15) and is not retried "
            f"around. {_sentence(_stale_since(status))}.{tail}"
        )
    if state == "failing":
        assert status.last_failure is not None
        return (
            f"{label}: {_stale_since(status)}; the last run failed at "
            f"{_eastern(status.last_failure)} ({status.last_error_type}) and runs "
            f"again at its next slot.{tail}"
        )
    if state == "skipped":
        assert status.last_skipped is not None
        return (
            f"{label}: nothing fetched at {_eastern(status.last_skipped)} -- "
            f"{status.last_skip_reason}. {_sentence(_stale_since(status))}.{tail}"
        )
    none = _none_clause(cover, start, end)
    lead = f"{label}: {none}; " if none is not None else f"{label}: "
    if state == "fresh_at_start":
        assert status.last_skip_fresh_as_of is not None
        upcoming = (
            f"next fetch {_eastern(status.next_run)} at its daily slot"
            if status.next_run is not None
            else "next fetch at its daily slot"
        )
        return (
            f"{lead}up to date as of {_eastern(status.last_skip_fresh_as_of)} "
            f"(the newest stored row, written before this process started); "
            f"{upcoming}.{tail}"
        )
    assert status.last_success is not None
    return f"{lead}last fetched {_eastern(status.last_success)}.{tail}"


def job_notices(
    statuses: Mapping[str, JobStatus] | None,
    events: Sequence[StoredCalendarEvent],
    start: date,
    end: date,
) -> list[CalendarJobNotice]:
    """One notice per calendar job, in panel order."""
    notices: list[CalendarJobNotice] = []
    for name, spec in CALENDAR_JOBS.items():
        status = None if statuses is None else statuses.get(name)
        rows = sum(1 for event in events if event.kind in spec.kinds)
        state = job_state(status, scheduler_running=statuses is not None)
        day = fetched_on(status)
        cover = None if day is None else coverage(spec, day, start, end, events)
        # The scheduler's "already fresh" reason carries a UTC instant for its
        # log; the notice says it in Eastern time instead and quotes nothing.
        fresh = status is not None and status.last_skip_fresh_as_of is not None
        notices.append(
            CalendarJobNotice(
                job=name,
                kinds=[kind.value for kind in spec.kinds],
                state=state,
                access_denied=state == "access_denied",
                last_success=None if status is None else status.last_success,
                last_failure=None if status is None else status.last_failure,
                last_error_type=None if status is None else status.last_error_type,
                last_skipped=None if status is None else status.last_skipped,
                last_skip_reason=(
                    None if status is None or fresh else status.last_skip_reason
                ),
                fresh_as_of=(
                    status.last_skip_fresh_as_of
                    if status is not None and state == "fresh_at_start"
                    else None
                ),
                next_run=None if status is None else status.next_run,
                covered_through=None if cover is None else cover.through,
                rows_in_range=rows,
                message=job_message(spec, state, status, cover, start, end),
            )
        )
    return notices


def seed_gap_notices(
    start: date, end: date, directory: FilePath | None, correlation_id: str
) -> list[CalendarSeedGapNotice]:
    """Decision 8's gaps for each year ``[start, end]`` touches.

    Year by year, so a malformed file for one year is that year's
    ``unreadable`` notice and does not hide the other year's gaps.
    """
    notices: list[CalendarSeedGapNotice] = []
    for year in range(start.year, end.year + 1):
        try:
            gaps = seed_gaps_between(
                max(start, date(year, 1, 1)), min(end, date(year, 12, 31)), directory
            )
        except SeedError as exc:
            logger.error(
                "a central-bank seed file failed validation; the calendar says so "
                "rather than reading half of it",
                extra={
                    "event": "calendar_seed_unreadable",
                    "rule": "decision 8: a malformed seed is stated, never half-read",
                    "year": year,
                    "error": str(exc),
                    "correlation_id": correlation_id,
                },
            )
            notices.append(
                CalendarSeedGapNotice(
                    bank=None,
                    year=year,
                    kind="unreadable",
                    reason=(
                        f"the central-bank seed for {year} is committed but failed "
                        "validation, so no central-bank decision for that year is "
                        "shown; the server log names the line"
                    ),
                    covers_from=None,
                )
            )
            continue
        notices.extend(
            CalendarSeedGapNotice(
                bank=gap.bank.value,
                year=gap.year,
                kind=gap.kind.value,
                reason=gap.reason,
                covers_from=gap.covers_from,
            )
            for gap in gaps
        )
    return notices


def _scheduler_statuses(request: Request) -> Mapping[str, JobStatus] | None:
    """The running scheduler's records, or ``None`` when no scheduler runs."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return None
    reader: StatusReader = scheduler.status
    return reader()


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def _refuse(
    *,
    rule: str,
    action: str,
    status: int,
    code: str,
    message: str,
    inputs: Mapping[str, object],
    at: datetime,
    correlation_id: str,
) -> ApiError:
    """Log a refusal (rule, inputs, time) and build its error. Never quotes a title."""
    logger.warning(
        "calendar %s refused: %s",
        action,
        message,
        extra={
            "event": "calendar_request_refused",
            "rule": rule,
            "action": action,
            "inputs": dict(inputs),
            "status": status,
            "code": code,
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return ApiError(status_code=status, code=code, message=message)


def _parse_day(raw: str, name: str, *, now: datetime, correlation_id: str) -> date:
    try:
        if not _ISO_DATE.fullmatch(raw):
            raise ValueError(raw)
        return date.fromisoformat(raw)
    except ValueError:
        raise _refuse(
            rule=_RANGE_RULE,
            action="read",
            status=422,
            code="invalid_calendar_range",
            message=f"{name} must be a YYYY-MM-DD calendar date",
            inputs={name: raw[:16]},
            at=now,
            correlation_id=correlation_id,
        ) from None


def _check_manual_years(
    body: ManualCalendarEventRequest, *, action: str, now: datetime, correlation_id: str
) -> None:
    years = [body.date.year] + ([] if body.at is None else [body.at.year])
    if all(MANUAL_YEAR_MIN <= year <= MANUAL_YEAR_MAX for year in years):
        return
    raise _refuse(
        rule=_MANUAL_RULE,
        action=action,
        status=422,
        code="invalid_calendar_entry",
        message=(
            f"a calendar entry's date and time must fall in {MANUAL_YEAR_MIN}-"
            f"{MANUAL_YEAR_MAX}"
        ),
        inputs=_entry_inputs(body),
        at=now,
        correlation_id=correlation_id,
    )


def _entry_inputs(body: ManualCalendarEventRequest) -> dict[str, object]:
    """What a refusal log records of a body: its date, instant and title *length*.

    Never the title: it is free text bound for Discord.
    """
    return {
        "date": body.date.isoformat(),
        "at": None if body.at is None else body.at.isoformat(),
        "title_length": len(body.title),
    }


def _store_refusal(
    exc: Exception,
    *,
    action: str,
    event_id: int | None,
    body: ManualCalendarEventRequest | None,
    now: datetime,
    correlation_id: str,
) -> ApiError:
    """Map a store refusal to its status. The messages are the store's own words.

    ``inputs`` carries the path id when there is one (not on ``POST``) and
    the body's shape when there is one (not on ``DELETE``).
    """
    inputs: dict[str, object] = {} if event_id is None else {"id": event_id}
    if body is not None:
        inputs |= _entry_inputs(body)
    if isinstance(exc, CalendarEventNotFoundError):
        return _refuse(
            rule=_MANUAL_RULE,
            action=action,
            status=404,
            code="calendar_event_not_found",
            message=f"calendar event {event_id} does not exist or was removed",
            inputs=inputs,
            at=now,
            correlation_id=correlation_id,
        )
    if isinstance(exc, CalendarEventNotEditableError):
        inputs |= {"source": exc.source, "kind": exc.kind}
        return _refuse(
            rule=_MANUAL_RULE,
            action=action,
            status=409,
            code="calendar_event_not_editable",
            message=(
                f"calendar event {event_id} is a {exc.source} {exc.kind} row; only "
                "manual geopolitical rows can be edited or removed"
            ),
            inputs=inputs,
            at=now,
            correlation_id=correlation_id,
        )
    # A ``ValueError`` from ``check_title``/``check_when``: the store's
    # second look at the body. Its text names no secret, but it can quote the
    # instant the client sent; that is the client's own value.
    return _refuse(
        rule=_MANUAL_RULE,
        action=action,
        status=422,
        code="invalid_calendar_entry",
        message=str(exc),
        inputs=inputs,
        at=now,
        correlation_id=correlation_id,
    )


# --------------------------------------------------------------------------
# Notices of a change -- built from the stored row
# --------------------------------------------------------------------------


def _when(day: date, at: datetime | None) -> str:
    if at is None:
        return f"{day.isoformat()} (all day)"
    return _eastern(at)


def _line(title: str, day: date, at: datetime | None) -> str:
    return f"{title} -- {_when(day, at)}"


def _announce(
    request: Request,
    background: BackgroundTasks,
    *,
    action: str,
    lines: list[str],
    event_id: int,
    at: datetime,
    correlation_id: str,
) -> None:
    """After the commit: log the change and schedule its one notice."""
    logger.info(
        "manual calendar entry %s",
        action,
        extra={
            "event": "calendar_manual_changed",
            "action": action,
            "id": event_id,
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    notify_after_response(
        request,
        background,
        calendar_notice(action, lines, at=at, correlation_id=correlation_id),
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("", summary="Calendar events in an Eastern date range, grouped by date")
def read_calendar(
    session: SessionDep,
    request: Request,
    now: NowDep,
    seed_dir: SeedDirDep,
    from_: Annotated[str, Query(alias="from", min_length=1, max_length=16)],
    to: Annotated[str, Query(min_length=1, max_length=16)],
) -> CalendarRange:
    correlation_id = str(uuid.uuid4())
    start = _parse_day(from_, "from", now=now, correlation_id=correlation_id)
    end = _parse_day(to, "to", now=now, correlation_id=correlation_id)
    if end < start:
        raise _refuse(
            rule=_RANGE_RULE,
            action="read",
            status=422,
            code="invalid_calendar_range",
            message=f"from ({start.isoformat()}) is after to ({end.isoformat()})",
            inputs={"from": start.isoformat(), "to": end.isoformat()},
            at=now,
            correlation_id=correlation_id,
        )
    if end - start > timedelta(days=CALENDAR_MAX_SPAN_DAYS - 1):
        raise _refuse(
            rule=_RANGE_RULE,
            action="read",
            status=422,
            code="invalid_calendar_range",
            message=(
                f"the range covers {(end - start).days + 1} dates; at most "
                f"{CALENDAR_MAX_SPAN_DAYS} are served"
            ),
            inputs={"from": start.isoformat(), "to": end.isoformat()},
            at=now,
            correlation_id=correlation_id,
        )
    events = read_range(session, start, end)
    days = [
        CalendarDay(date=day, events=[event_item(event) for event in day_events])
        for day, day_events in group_by_date(events).items()
    ]
    return CalendarRange(
        start=start,
        end=end,
        max_span_days=CALENDAR_MAX_SPAN_DAYS,
        days=days,
        total=len(events),
        notices=CalendarNotices(
            seed_gaps=seed_gap_notices(start, end, seed_dir, correlation_id),
            jobs=job_notices(_scheduler_statuses(request), events, start, end),
            release_figures=CalendarReleaseFiguresNotice(
                state="pending_owner_decision", reason=RELEASE_FIGURES_PENDING
            ),
        ),
    )


@router.post(
    "/manual",
    status_code=201,
    summary="Add a manual geopolitical entry. Not audit-logged (decision 9).",
)
def add_manual(
    body: ManualCalendarEventRequest,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
    now: NowDep,
) -> CalendarEventItem:
    correlation_id = str(uuid.uuid4())
    _check_manual_years(body, action="add", now=now, correlation_id=correlation_id)
    try:
        stored = create_manual(session, title=body.title, date=body.date, at=body.at, now=now)
    except ValueError as exc:
        raise _store_refusal(
            exc, action="add", event_id=None, body=body, now=now, correlation_id=correlation_id
        ) from None
    session.commit()
    _announce(
        request,
        background,
        action="added",
        lines=[_line(stored.title, stored.date, stored.at)],
        event_id=stored.id,
        at=now,
        correlation_id=correlation_id,
    )
    return event_item(stored)


def _snapshot(session: Session, event_id: int) -> tuple[str, date, datetime | None] | None:
    """A manual row's title, date and instant before a change, read back as stored."""
    row = session.get(CalendarEvent, event_id)
    if row is None:
        return None
    at = row.at
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (row.title, row.date, at)


@router.put("/manual/{event_id}", summary="Edit a manual geopolitical entry.")
def edit_manual(
    event_id: IdPath,
    body: ManualCalendarEventRequest,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
    now: NowDep,
) -> CalendarEventItem:
    correlation_id = str(uuid.uuid4())
    _check_manual_years(body, action="edit", now=now, correlation_id=correlation_id)
    before = _snapshot(session, event_id)
    try:
        stored = update_manual(
            session, event_id, title=body.title, date=body.date, at=body.at, now=now
        )
    except (CalendarEventNotFoundError, CalendarEventNotEditableError, ValueError) as exc:
        raise _store_refusal(
            exc,
            action="edit",
            event_id=event_id,
            body=body,
            now=now,
            correlation_id=correlation_id,
        ) from None
    session.commit()
    after = (stored.title, stored.date, stored.at)
    lines = (
        []
        if before == after or before is None
        else [f"{_line(*before)} → {_line(*after)}"]
    )
    _announce(
        request,
        background,
        action="edited",
        lines=lines,
        event_id=event_id,
        at=now,
        correlation_id=correlation_id,
    )
    return event_item(stored)


@router.delete(
    "/manual/{event_id}",
    status_code=204,
    summary="Remove a manual geopolitical entry (a soft delete).",
)
def remove_manual(
    event_id: IdPath,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
    now: NowDep,
) -> Response:
    correlation_id = str(uuid.uuid4())
    try:
        delete_manual(session, event_id, now=now)
    except (CalendarEventNotFoundError, CalendarEventNotEditableError) as exc:
        raise _store_refusal(
            exc,
            action="remove",
            event_id=event_id,
            body=None,
            now=now,
            correlation_id=correlation_id,
        ) from None
    removed = _snapshot(session, event_id)
    session.commit()
    _announce(
        request,
        background,
        action="removed",
        lines=[] if removed is None else [_line(*removed)],
        event_id=event_id,
        at=now,
        correlation_id=correlation_id,
    )
    return Response(status_code=204)
