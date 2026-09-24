"""The engine's scheduler: every job that runs on a clock rather than a request.

Phase 3 decision 1 made this real. It is an asyncio task set, started in the
FastAPI lifespan beside the sockets and the watchdog, that runs each context
job -- news, calendar, FRED, the composite, the audit -- on a **market-calendar
clock**. Schedules resolve through :mod:`corollary.calendars`, never a
hardcoded 09:30-16:00: the day after Thanksgiving closes at 13:00 ET, and an
in-session job that ran until 16:00 would poll three hours of closed market.

Rule 9: a context job is never a watchdog producer
--------------------------------------------------

**No context job calls ``EngineRuntime.record_message`` or any watchdog input,
and no context failure halts the engine.** A news vendor being down is not an
Alpaca connection loss, and a halt log full of *"Finnhub 502"* is the log
nobody reads the morning it matters. That holds even for the Alpaca news and
corporate-actions calls, which reach the same vendor: the watchdog's producers
are the three vendor sockets and nothing else.

It is structural here rather than disciplinary. This module does not import
``corollary.engine.runtime``; :class:`Scheduler` is not handed the runtime,
and :class:`ContextServices` -- the one bundle the job factory receives -- has
no field that carries it. A job that wanted to feed the watchdog would first
have to widen one of those types, and
``tests/engine/test_scheduler.py::test_the_scheduler_cannot_reach_the_runtime_by_construction``
fails when anyone does.

Not being *handed* the runtime is not the whole of it, because
``ContextServices.session_factory`` gives every job a **writable** session --
the same type ``api/routes/engine.py``'s ``resume`` takes -- and a module can
import what it was not given. So two more things hold, and are tested:

* **No module that contributes a context job imports** the runtime,
  ``corollary.engine.state``, the sockets, or anything under
  ``corollary.api`` (``routes.engine.resume``; ``app.state.engine_runtime``)
  -- directly, inside a function, or through another ``corollary`` module.
  The set of contributing modules is derived from :func:`context_jobs`'s own
  output, so a job module a later step adds is scanned without anyone
  remembering to list it.
* **Running the shipped job set never moves the halt.** The shipped jobs run
  against a real database in both halt states, as shipped and with each one
  forced to fail, and ``halted``/``halted_reason``/``halted_at`` must come out
  as they went in. A job that resumed the engine through its session fails
  that test even though it never raised.

A failure is stated, never silent
---------------------------------

A job that raises is caught, logged at ERROR with its rule, its inputs, the
instant it was due, the instant it failed (both UTC) and the exception class,
with the message redacted through :func:`corollary.wire.vendor_detail` --
httpx errors carry the request URL, and Finnhub's key travels in the query
string. The scheduler then carries on: other jobs are separate tasks and never
wait on it, and its own next slot comes round as usual. Each job's last
success and last failure are kept in memory and read through
:meth:`Scheduler.status`, which is what a later step's routes turn into
*stale since HH:MM ET* on the page the job feeds.

In memory only, on purpose, for now: the durable answer to "how fresh is this
feed" is the newest row the feed wrote, which survives a restart where a
status dict does not.

Clocks
------

Every instant here is an aware UTC ``datetime``. Times of day are *specified*
in ``America/New_York`` (:class:`AtTime`) because that is how the market and
the spec state them -- a job pinned to 11:00 UTC would run at 06:00 ET for four
months of the year -- and converted on the day they apply, so the DST change
moves the UTC instant and not the ET one.

A job's first run is its first slot **after** the scheduler starts, never an
immediate run at startup. That is what keeps ``dev_app``'s ``--reload`` loop
from re-firing every job on every save; a job that must catch up after a
restart decides so itself, from the rows it finds. The slots themselves are
**anchored to the session open** (``open + k * interval``), not to the instant
the process started: anchored to the start, every save would push the first
run a full interval out, and a developer saving more often than the interval
would starve the job for the whole session.

Jobs share the event loop with rule 9's producers
------------------------------------------------

The scheduler runs on the same event loop as the three vendor sockets and the
watchdog. A job that blocks the loop -- a long SQLite write, a CPU-bound parse,
a synchronous HTTP call -- stalls every socket read for as long as it blocks,
and a stalled read is exactly what the watchdog is built to judge as a dead
connection. **Blocking synchronous work in a job belongs in
``asyncio.to_thread``.** The scheduler follows its own rule: the market
calendar's first build (``exchange_calendars`` and pandas, about half a
second) runs once on a worker thread from :meth:`Scheduler.start`, and no job
asks its schedule anything until that has finished.

Phase 4's work, still to come
-----------------------------

See PRD.md §6.5 for the schedule. **Two things belong here and are
deliberately not elsewhere.**

*Re-planning the stream subscription when the book changes.*
``engine/sockets.py`` plans once at the session open and says so: nothing in
Phase 2 places an order, so the book cannot change under it, and re-asking
the broker on a five-second socket tick would spend the 200/min budget on a
question whose answer cannot have moved. The refresh that *would* notice a
change is this module's, beside the position poll -- and the re-subscribe it
implies (unsubscribe, replace the plan, reconcile the acknowledgement
against ``reconcile_acknowledgement``) is one decision, not two.

*The opening snapshot.* ``EngineRuntime.record_opening_snapshot`` has no
caller yet. It is readiness rather than permission -- it does not clear the
cold-start halt -- and the pre-market build is what will take it. **It is
also a watchdog input**, which is why it cannot simply become a job in the
set below: the pre-market build is an engine job, not a context job, and it
arrives with its own, explicit handle on the runtime when Phase 4 lands.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol

from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ, nyse_session_close, nyse_session_open
from corollary.wire import vendor_detail

__all__ = [
    "AtTime",
    "ContextServices",
    "DayRule",
    "EveryWhileOpen",
    "JobStatus",
    "Schedule",
    "ScheduledJob",
    "Scheduler",
    "SchedulerFactory",
    "build_context_scheduler",
    "context_jobs",
    "no_scheduler",
    "on_weekdays",
    "trading_days",
]

logger = logging.getLogger(__name__)

UtcClock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]

#: How many days ahead a schedule looks for its next slot. The longest run of
#: consecutive non-session days NYSE has published is four weekdays plus a
#: weekend (September 2001); fifteen covers that with room, and a weekly job
#: needs seven. Past it -- which in practice means past the end of the
#: published calendar -- a schedule answers ``None`` rather than guessing.
_HORIZON_DAYS = 15

#: The longest single sleep. A laptop that hibernates for an hour wakes with
#: ``asyncio.sleep`` still owing the balance on the *monotonic* clock, while
#: the wall clock -- which the calendar speaks -- has moved on. Re-reading the
#: clock at least this often bounds how late a job can be after a resume.
_MAX_SLEEP_SECONDS = 300.0

#: A job that overran its slot skips to the next slot after it finished,
#: walking the grid forward. Bounded so a schedule that somehow never
#: advances cannot spin; past it the scheduler asks from "now" instead.
_MAX_SLOTS_SKIPPED = 10_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"expected an aware datetime, got naive {moment!r}")
    return moment.astimezone(timezone.utc)


def _warm_calendar_blocking(moment: datetime) -> None:
    """Build the market calendar by asking it one question. **Blocks** ~0.5s.

    Only ever called through ``asyncio.to_thread`` -- see
    :meth:`Scheduler._warm_calendar`.
    """
    nyse_session_open(_require_utc(moment).astimezone(NYSE_TZ).date())


def _describe_exception(exc: BaseException, secrets: Sequence[str]) -> str:
    """The exception's message, redacted -- and never an exception itself.

    ``str(exc)`` runs arbitrary code, and a failure *here* would kill the
    job's task inside the very handler meant to state its failure. Should
    redaction itself fail, the message is withheld rather than logged raw.
    """
    try:
        text = str(exc)
    except Exception:
        return f"<{type(exc).__name__}: its message could not be rendered>"
    try:
        return vendor_detail(text, secrets=secrets)
    except Exception:
        return f"<{type(exc).__name__}: message withheld, redaction failed>"


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------


class Schedule(Protocol):
    """When a job is next due.

    ``next_run(after)`` is the first instant **strictly after** ``after`` at
    which the job should run, as an aware UTC datetime -- or ``None`` when the
    calendar cannot say (a date past the published schedule).
    """

    def next_run(self, after: datetime) -> datetime | None: ...

    def describe(self) -> str: ...


#: Which calendar days an :class:`AtTime` job runs on.
DayRule = Callable[[date], bool]


def trading_days(day: date) -> bool:
    """NYSE holds a session on ``day``, per the calendar. Half-days count."""
    return nyse_session_open(day) is not None


def on_weekdays(*weekdays: int) -> DayRule:
    """Run on these weekdays (Monday is 0, Saturday 5), whatever the market does.

    For jobs whose clock is the week rather than the session -- the Saturday
    audit runs on a Saturday whether or not Friday was a holiday.
    """
    chosen = frozenset(weekdays)
    if not chosen or not chosen <= frozenset(range(7)):
        raise ValueError(f"weekdays must be a non-empty subset of 0-6, got {weekdays!r}")

    def rule(day: date) -> bool:
        return day.weekday() in chosen

    return rule


@dataclass(frozen=True, slots=True)
class EveryWhileOpen:
    """Every ``interval`` while the regular NYSE session is open.

    The session is ``[open, close)`` as the calendar publishes it, so a
    half-day's last run falls before 13:00 ET and a holiday has none. The
    slots are ``open + k * interval`` for ``k = 0, 1, 2, ...`` -- a grid
    anchored at the open, so the answer does not depend on when the question
    was asked, and a restarted process lands back on the same grid.
    """

    interval: timedelta

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError(f"interval must be positive, got {self.interval!r}")

    def next_run(self, after: datetime) -> datetime | None:
        moment = _require_utc(after)
        first_day = moment.astimezone(NYSE_TZ).date()
        for offset in range(_HORIZON_DAYS):
            day = first_day + timedelta(days=offset)
            opens = nyse_session_open(day)
            closes = nyse_session_close(day)
            if opens is None or closes is None:
                continue
            if moment < opens:
                return opens
            steps = (moment - opens) // self.interval + 1
            candidate = opens + self.interval * steps
            if candidate < closes:
                return candidate
        return None

    def describe(self) -> str:
        return f"every {self.interval} while the NYSE session is open"


@dataclass(frozen=True, slots=True)
class AtTime:
    """At ``at`` (a wall-clock time in ``America/New_York``) on the days ``days`` allows.

    ``at`` is naive on purpose: it names an Eastern time of day, and the UTC
    instant it becomes is worked out per day, so it follows the DST change.
    Avoid 02:00-03:00 ET, which happens twice or not at all on the two change
    days.
    """

    at: time
    days: DayRule

    def __post_init__(self) -> None:
        if self.at.tzinfo is not None:
            raise ValueError(
                "AtTime takes a naive time of day, read as America/New_York; "
                f"got {self.at!r}"
            )

    def next_run(self, after: datetime) -> datetime | None:
        moment = _require_utc(after)
        first_day = moment.astimezone(NYSE_TZ).date()
        for offset in range(_HORIZON_DAYS):
            day = first_day + timedelta(days=offset)
            if not self.days(day):
                continue
            candidate = datetime.combine(day, self.at, tzinfo=NYSE_TZ).astimezone(
                timezone.utc
            )
            if candidate > moment:
                return candidate
        return None

    def describe(self) -> str:
        name = getattr(self.days, "__name__", "selected days")
        return f"at {self.at.isoformat(timespec='minutes')} ET on {name}"


# --------------------------------------------------------------------------
# Jobs and their status
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One job: a name, when it runs, what it runs, and why it matters.

    ``rule`` is the sentence a failure log leads with -- what this job feeds
    and what goes stale when it fails -- and ``inputs`` the static facts that
    identify its call (host, endpoint, series). Neither may carry a secret:
    both are logged verbatim.
    """

    name: str
    schedule: Schedule
    run: Callable[[], Awaitable[None]]
    rule: str
    inputs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JobStatus:
    """A snapshot of one job's record. All instants UTC; ``None`` is "never"."""

    name: str
    rule: str
    schedule: str
    next_run: datetime | None
    last_started: datetime | None
    last_success: datetime | None
    last_failure: datetime | None
    last_error_type: str | None
    runs: int
    failures: int

    @property
    def failing(self) -> bool:
        """The most recent completed run failed.

        What a page reads to say *stale since* -- against
        :attr:`last_success`, the last time its data was known good.
        """
        if self.last_failure is None:
            return False
        return self.last_success is None or self.last_failure > self.last_success


@dataclass(slots=True)
class _JobRecord:
    """The mutable record behind :class:`JobStatus`. Private to the scheduler."""

    next_run: datetime | None = None
    last_started: datetime | None = None
    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_error_type: str | None = None
    runs: int = 0
    failures: int = 0


# --------------------------------------------------------------------------
# The scheduler
# --------------------------------------------------------------------------


class Scheduler:
    """Runs each :class:`ScheduledJob` on its own asyncio task.

    One task per job, so a slow or hung job delays nobody else. Not handed
    the engine runtime -- see the module docstring -- and nothing it does can
    halt, resume or feed rule 9's switch.

    ``secrets`` is called on every failure for the values to redact from the
    logged exception text; the app passes the same callable its error
    envelope uses, so a key added to a reloaded process is redacted at once.
    ``clock`` and ``sleep`` are injected so tests run a week of calendar in
    milliseconds.
    """

    def __init__(
        self,
        jobs: Iterable[ScheduledJob],
        *,
        secrets: Callable[[], Sequence[str]],
        clock: UtcClock = _utc_now,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._jobs: tuple[ScheduledJob, ...] = tuple(jobs)
        names = [job.name for job in self._jobs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"two jobs cannot share a name: {duplicates}")
        self._secrets = secrets
        self._clock = clock
        self._sleep = sleep
        self._records: dict[str, _JobRecord] = {job.name: _JobRecord() for job in self._jobs}
        self._tasks: list[asyncio.Task[None]] = []
        self._warm_task: asyncio.Task[None] | None = None
        self._calendar_ready = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Put every job on its own task. Idempotent; needs a running loop.

        Also starts the one calendar build, on a worker thread. Every job
        task waits for it before asking its schedule anything, so the build
        runs once and never on the loop -- see the module docstring.
        """
        if self._warm_task is not None:
            return
        loop = asyncio.get_running_loop()
        self._warm_task = loop.create_task(
            self._warm_calendar(), name="context-scheduler:calendar"
        )
        self._tasks = [
            loop.create_task(self._run_job(job), name=f"context-job:{job.name}")
            for job in self._jobs
        ]
        for task in self._tasks:
            task.add_done_callback(self._task_ended)
        logger.info(
            "context scheduler started",
            extra={
                "event": "context_scheduler_started",
                "jobs": [job.name for job in self._jobs],
                "at": self._now().isoformat(),
            },
        )

    async def ready(self) -> None:
        """Wait until the calendar build :meth:`start` began has finished.

        Finished, not succeeded: a failed build is logged, and the failure
        resurfaces per job on its first schedule lookup. Call after
        :meth:`start`; before it, this waits for a build nobody began.
        """
        await self._calendar_ready.wait()

    async def aclose(self) -> None:
        """Cancel every task and wait for it. Safe to call more than once. Never raises.

        The lifespan calls this **first** in its shutdown, ahead of the socket
        supervisor, the runtime and the Discord sink. An exception out of
        here would skip all of those, and queued rule 9 alerts would get no
        grace period and no ``dropped`` row -- so a task that died with an
        exception is suppressed here. It is not silent: :meth:`_task_ended`
        logged it at the moment it died. A cancellation of ``aclose`` itself
        still propagates.
        """
        tasks = list(self._tasks)
        if self._warm_task is not None:
            tasks.append(self._warm_task)
        self._tasks, self._warm_task = [], None
        for task in tasks:
            task.cancel()
        # return_exceptions: every outcome -- cancelled, died, returned --
        # comes back as a value rather than being raised here.
        await asyncio.gather(*tasks, return_exceptions=True)

    def _task_ended(self, task: "asyncio.Task[None]") -> None:
        """Say so, at once, when a job's task ends in anything but a cancellation.

        ``_run_job`` catches every job failure, so this is the scheduler's
        own machinery failing (a broken clock or sleeper). The job will not
        run again in this process, and a page reading *stale* with nothing in
        the log to say why is the silence rule 8 forbids.
        """
        try:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is None:
                return  # ran out of calendar; context_job_unscheduled said so
            logger.error(
                "a context job's task died; that job will not run again until "
                "the process restarts. The engine is not halted",
                extra={
                    "event": "context_job_task_died",
                    "job": task.get_name().removeprefix("context-job:"),
                    "error_type": type(exc).__name__,
                    "detail": _describe_exception(exc, self._safe_secrets()),
                    "at": _utc_now().isoformat(),
                },
            )
        except Exception:  # pragma: no cover - a done callback must not raise
            logger.exception(
                "could not record a context job task's death",
                extra={"event": "context_job_task_died"},
            )

    # -- what a route reads ------------------------------------------------

    def status(self) -> dict[str, JobStatus]:
        """Every job's record, as frozen snapshots keyed by job name."""
        return {
            job.name: JobStatus(
                name=job.name,
                rule=job.rule,
                schedule=job.schedule.describe(),
                next_run=record.next_run,
                last_started=record.last_started,
                last_success=record.last_success,
                last_failure=record.last_failure,
                last_error_type=record.last_error_type,
                runs=record.runs,
                failures=record.failures,
            )
            for job in self._jobs
            for record in (self._records[job.name],)
        }

    # -- the loop ----------------------------------------------------------

    def _now(self) -> datetime:
        return _require_utc(self._clock())

    def _following(self, job: ScheduledJob, due: datetime) -> datetime | None:
        """The first slot after ``due`` that is still in the future.

        Walks the schedule's own grid forward past any slot a long run
        overran, so the job neither bursts to catch up nor drifts off its
        grid by the length of the run.
        """
        now = self._now()
        following = job.schedule.next_run(due)
        for _ in range(_MAX_SLOTS_SKIPPED):
            if following is None or following > now:
                return following
            following = job.schedule.next_run(following)
        return job.schedule.next_run(now)

    async def _sleep_until(self, due: datetime) -> None:
        while True:
            remaining = (due - self._now()).total_seconds()
            if remaining <= 0:
                return
            await self._sleep(min(remaining, _MAX_SLEEP_SECONDS))

    async def _warm_calendar(self) -> None:
        """Build the market calendar on a worker thread, once, before any job runs.

        The same move ``engine/sockets.py`` makes, for the same reason: on
        the loop, the build is half a second in which no socket frame is
        read. One build per scheduler however many jobs it has -- each job
        task waits on :attr:`_calendar_ready` rather than triggering its own.
        A failure is logged and otherwise ignored: this is a cache warm, and
        each job's own schedule lookup is where a broken calendar surfaces,
        with the job's name on it.
        """
        try:
            await asyncio.to_thread(_warm_calendar_blocking, self._now())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "the market calendar could not be built ahead of the context "
                "jobs; each job will report it on its own schedule lookup",
                extra={
                    "event": "context_calendar_warm_failed",
                    "error_type": type(exc).__name__,
                    "detail": _describe_exception(exc, self._safe_secrets()),
                },
            )
        finally:
            self._calendar_ready.set()

    def _safe_secrets(self) -> Sequence[str]:
        try:
            return tuple(self._secrets())
        except Exception:
            return ()

    async def _run_job(self, job: ScheduledJob) -> None:
        record = self._records[job.name]
        due: datetime | None = None
        await self._calendar_ready.wait()
        while True:
            try:
                upcoming = (
                    job.schedule.next_run(self._now())
                    if due is None
                    else self._following(job, due)
                )
            except Exception as exc:
                # A schedule that cannot answer (a calendar build that
                # failed, say) is stated and retried, never a dead task.
                self._record_failure(job, record, exc, scheduled_for=None)
                await self._sleep(_MAX_SLEEP_SECONDS)
                due = None
                continue
            record.next_run = upcoming
            if upcoming is None:
                logger.error(
                    "a context job has no next run; the calendar has nothing "
                    "to say past its published range",
                    extra={
                        "event": "context_job_unscheduled",
                        "job": job.name,
                        "rule": job.rule,
                        "schedule": job.schedule.describe(),
                        "at": self._now().isoformat(),
                    },
                )
                return
            await self._sleep_until(upcoming)
            due = upcoming
            record.last_started = self._now()
            try:
                await job.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure(job, record, exc, scheduled_for=due)
            else:
                record.runs += 1
                record.last_success = self._now()
                logger.debug(
                    "context job ran",
                    extra={
                        "event": "context_job_ran",
                        "job": job.name,
                        "scheduled_for": due.isoformat(),
                        "finished_at": record.last_success.isoformat(),
                    },
                )

    def _record_failure(
        self,
        job: ScheduledJob,
        record: _JobRecord,
        exc: Exception,
        *,
        scheduled_for: datetime | None,
    ) -> None:
        """State one failure. **Never raises.**

        It runs inside the job's task, and an exception here would end that
        task -- stopping the job for the rest of the process, from inside the
        handler meant to say it failed.
        """
        error_type = type(exc).__name__
        try:
            failed_at = self._now()
        except Exception:
            failed_at = _utc_now()
        record.failures += 1
        record.last_failure = failed_at
        record.last_error_type = error_type
        try:
            logger.error(
                "a context job failed; the engine is not halted and the job runs "
                "again at its next slot",
                extra={
                    "event": "context_job_failed",
                    "job": job.name,
                    "rule": job.rule,
                    "inputs": dict(job.inputs),
                    "schedule": job.schedule.describe(),
                    "scheduled_for": (
                        None if scheduled_for is None else scheduled_for.isoformat()
                    ),
                    "failed_at": failed_at.isoformat(),
                    "error_type": error_type,
                    "detail": _describe_exception(exc, self._safe_secrets()),
                },
            )
        except Exception:
            # Something in the job's own description (its schedule's
            # ``describe``, its inputs) broke. The fact of the failure is
            # still stated, with nothing in it that could break again.
            logger.error(
                "a context job failed, and its failure could not be fully described",
                extra={
                    "event": "context_job_failed",
                    "job": job.name,
                    "failed_at": failed_at.isoformat(),
                    "error_type": error_type,
                },
            )


# --------------------------------------------------------------------------
# The job set the lifespan runs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextServices:
    """Everything a context job may be built from -- and, by omission, what it may not.

    **No engine runtime, no watchdog, no socket supervisor, no broker.** Rule
    9's switch is fed by the three vendor sockets and nothing else, and a job
    cannot call what it was never given. Later steps add the providers their
    jobs need (FRED, news, StockTwits); adding the runtime here is the change
    the isolation test exists to refuse.
    """

    session_factory: Callable[[], Session]


async def _calendar_probe() -> None:
    """The no-op job. Its whole output is the scheduler's record that it ran."""
    return None


def context_jobs(services: ContextServices) -> list[ScheduledJob]:
    """The Phase 3 job set. Today one no-op; each feed's step adds its own."""
    return [
        ScheduledJob(
            name="calendar_probe",
            schedule=EveryWhileOpen(timedelta(minutes=15)),
            run=_calendar_probe,
            rule=(
                "proves the context scheduler runs on the market calendar in "
                "the lifespan; feeds no page"
            ),
        )
    ]


#: How the lifespan gets its scheduler. A factory for the same reason the
#: socket supervisor has one: a test hands in a wound clock.
#: Its second argument is the secrets callable failures are redacted with.
SchedulerFactory = Callable[
    [ContextServices, Callable[[], Sequence[str]]], Scheduler | None
]


def build_context_scheduler(
    services: ContextServices,
    secrets: Callable[[], Sequence[str]],
    *,
    clock: UtcClock = _utc_now,
    sleep: Sleeper = asyncio.sleep,
) -> Scheduler:
    """The shipped scheduler: :func:`context_jobs` on the real clock."""
    return Scheduler(context_jobs(services), secrets=secrets, clock=clock, sleep=sleep)


def no_scheduler(
    services: ContextServices, secrets: Callable[[], Sequence[str]]
) -> None:
    """No context jobs at all. What a route test's app is built with.

    Explicit, and the default, for the reason ``no_socket_supervisor`` is:
    later jobs call real vendors with keys from the environment, and a test
    app must not do that by forgetting an argument.
    """
    return None
