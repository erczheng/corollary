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
restart decides so itself, from the rows it finds. That decision is a job's
optional :attr:`ScheduledJob.catch_up`, run once as the scheduler starts and
before the first slot, under the same failure handling as a scheduled run --
and it must be a no-op when the rows say nothing is missing, or it is the
re-fire-on-every-save this rule exists to prevent. FRED's ``DGS3MO`` job
fetches at start only while no observation has ever been stored, or the newest
one is more than two sessions old (a long downtime). The slots themselves are
**anchored to the session open** (``open + k * interval``), not to the instant
the process started: anchored to the start, every save would push the first
run a full interval out, and a developer saving more often than the interval
would starve the job for the whole session.

A run that fetched nothing is *skipped*, not a success
------------------------------------------------------

A body returns ``None`` when it did its work, raises when it failed, and
returns :class:`JobSkipped` with a reason when it completed without fault but
had nothing to do -- no key configured, or a catch-up that found the rows
current. A skip moves ``last_skipped`` and ``skips``, never ``last_success``
or ``runs``: ``last_success`` is what a *stale since* display reads as the
last time the feed's data was known good, and a slot that fetched nothing
does not make it so. A skip is not a fault either -- ``failing`` is
unaffected -- and like every outcome here it never reaches rule 9.

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
import collections
import functools
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Final, Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ, nyse_session_close, nyse_session_open
from corollary.data.macro.risk_free import (
    NotRefreshed,
    ObservationSource,
    catch_up_dgs3mo,
    refresh_dgs3mo,
)
from corollary.data.news.assets import AssetDirectoryHolder, AssetSource
from corollary.data.news.pollers import (
    AlpacaNewsPoller,
    AlpacaNewsSource,
    CompanyNewsSource,
    FinnhubMarketNewsPoller,
    MarketNewsSource,
    MassiveNewsPoller,
    MassiveNewsSource,
    NewsStore,
    PollSkipped,
    UniverseSource,
    WatchTierPoller,
    build_watch_universe,
    news_name_books,
    prune_news,
    refresh_assets,
    refresh_tradeability_cache,
    watch_interval,
)
from corollary.data.news.tradeability import IpoDateSource, TradeabilityInputs
from corollary.data.seeds import SpdrSeed
from corollary.data.seeds.isin import IsinMappingSource, OpenFigiIsinResolver
from corollary.data.seeds.nport import (
    CusipResolver,
    IsinResolver,
    NportSource,
    SnapshotRule,
    UnavailableIsinResolver,
    build_spdr_snapshot,
    latest_snapshot_attempt,
    load_spdr_seed_from_db,
    stored_snapshot_amendments,
)
from corollary.pricing.rates import DGS3MO_SERIES, RiskFreeRateSource
from corollary.ratelimit import (
    ALPACA_DATA_HOST,
    ALPACA_PAPER_TRADING_HOST,
    FINNHUB_HOST,
    FRED_HOST,
    MASSIVE_HOST,
    SEC_DATA_HOST,
    SEC_WWW_HOST,
)
from corollary.wire import vendor_detail

__all__ = [
    "ALPACA_NEWS_IN_SESSION",
    "ALPACA_NEWS_OTHERWISE",
    "ASSET_DIRECTORY_AT",
    "FINNHUB_MARKET_NEWS_EVERY",
    "MASSIVE_NEWS_EVERY",
    "NEWS_PRUNE_AT",
    "SPDR_HOLDINGS_AT",
    "SPDR_HOLDINGS_CATCH_UP_AFTER",
    "SPDR_HOLDINGS_DAYS",
    "TRADEABILITY_EVERY",
    "AlpacaContextSource",
    "AtTime",
    "CONTEXT_SESSIONS_DRAIN_TIMEOUT",
    "ContextServices",
    "ContextSessions",
    "DayRule",
    "EveryInterval",
    "EveryWhileOpen",
    "FinnhubNewsSource",
    "JobSkipped",
    "JobStatus",
    "HeldPositionUnderlyings",
    "PositionUnderlyings",
    "PositionUnderlyingsSource",
    "Schedule",
    "ScheduledJob",
    "Scheduler",
    "SchedulerFactory",
    "SpdrSnapshotAborted",
    "TwoRate",
    "WatchTierCadence",
    "build_context_scheduler",
    "context_jobs",
    "every_day",
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


def every_day(day: date) -> bool:
    """Every calendar day, whatever the market does -- weekends and holidays included.

    For jobs whose clock is the day rather than the session: the retention
    prune, and the asset directory (whose 26-hour staleness bound would lapse
    over every weekend if it refreshed on trading days only).
    """
    return True


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


#: Where :class:`EveryInterval`'s grid is anchored. Any fixed UTC instant on a
#: whole hour would do; what matters is that it never moves, so a restarted
#: process lands on the same slots. On a whole hour, a five- or fifteen-minute
#: grid is on the five- or fifteen-minute marks in ET as well as UTC.
_GRID_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _in_regular_session(moment: datetime) -> bool:
    """``moment`` is inside the calendar's ``[open, close)`` for its ET date.

    A session never spans midnight ET, so the ET date names the only session
    ``moment`` could be in. A day the calendar has no session for -- weekend,
    holiday, or past the published schedule -- is never in session.
    """
    day = moment.astimezone(NYSE_TZ).date()
    opens = nyse_session_open(day)
    closes = nyse_session_close(day)
    return opens is not None and closes is not None and opens <= moment < closes


@dataclass(frozen=True, slots=True)
class EveryInterval:
    """Every ``interval``, day and night, whatever the calendar says.

    For a feed whose news does not stop when the market does (Finnhub market
    news, Massive). The slots are ``epoch + k * interval`` on a fixed UTC grid,
    so -- like :class:`EveryWhileOpen` -- the answer does not depend on when the
    question was asked, and a restart lands back on the same grid.
    """

    interval: timedelta

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError(f"interval must be positive, got {self.interval!r}")

    def next_run(self, after: datetime) -> datetime | None:
        moment = _require_utc(after)
        steps = (moment - _GRID_EPOCH) // self.interval + 1
        return _GRID_EPOCH + self.interval * steps

    def at_or_after(self, moment: datetime) -> datetime:
        """The first slot at or after ``moment`` (``next_run`` is strictly after)."""
        moment = _require_utc(moment)
        # ceil(x) = -floor(-x), in whole intervals: no float anywhere.
        steps = -((_GRID_EPOCH - moment) // self.interval)
        return _GRID_EPOCH + self.interval * steps

    def describe(self) -> str:
        return f"every {self.interval}, day and night"


@dataclass(frozen=True, slots=True)
class TwoRate:
    """Every ``in_session`` while the NYSE session is open, every ``otherwise`` outside it.

    The job's slots are the union of two grids: :class:`EveryWhileOpen`'s
    ``in_session`` grid (anchored at each open, inside ``[open, close)``), and
    :class:`EveryInterval`'s ``otherwise`` grid with every slot that falls
    inside a session removed. So a half-day goes slow at its 13:00 close, not
    at a hardcoded 16:00, and a holiday is slow all day. Past the published
    calendar there is no session to be in, and the slow grid carries on.
    """

    in_session: timedelta
    otherwise: timedelta

    def __post_init__(self) -> None:
        for name, value in (("in_session", self.in_session), ("otherwise", self.otherwise)):
            if value <= timedelta(0):
                raise ValueError(f"{name} must be positive, got {value!r}")

    def _next_out_of_session(self, moment: datetime) -> datetime | None:
        grid = EveryInterval(self.otherwise)
        candidate = grid.next_run(moment)
        # Each pass either answers or jumps past one session's close, and the
        # next slow slot after a close is outside that session; bounded anyway.
        for _ in range(_HORIZON_DAYS * 2):
            if candidate is None or not _in_regular_session(candidate):
                return candidate
            day = candidate.astimezone(NYSE_TZ).date()
            closes = nyse_session_close(day)
            if closes is None:  # pragma: no cover - _in_regular_session saw one
                return candidate
            candidate = grid.at_or_after(closes)
        return None

    def next_run(self, after: datetime) -> datetime | None:
        moment = _require_utc(after)
        fast = EveryWhileOpen(self.in_session).next_run(moment)
        slow = self._next_out_of_session(moment)
        candidates = [slot for slot in (fast, slow) if slot is not None]
        return min(candidates) if candidates else None

    def describe(self) -> str:
        return (
            f"every {self.in_session} while the NYSE session is open, "
            f"every {self.otherwise} otherwise"
        )


@dataclass(frozen=True, slots=True, eq=False)
class WatchTierCadence:
    """Decision 21's watch tier: one request every ``watch_interval(W, after)``.

    ``universe_size`` is read on every question, because W changes whenever
    the universe does -- a manual add, a position opened. It is the size the
    poller saw on its latest call; ``0`` (nothing polled yet, or an empty
    universe) is taken as 1, so the job is asked again after a whole cycle
    rather than never. The window and its two rates are
    :func:`~corollary.data.news.pollers.watch_interval`'s, which reads the
    calendar's close -- never hardcoded hours.

    Not a grid: each slot is the previous one plus the interval at that
    instant, so a cycle's pace follows W as it changes.
    """

    universe_size: Callable[[], int]

    def next_run(self, after: datetime) -> datetime | None:
        moment = _require_utc(after)
        size = max(self.universe_size(), 1)
        return moment + watch_interval(size, moment)

    def describe(self) -> str:
        return (
            "one watch-tier symbol every 15 min / W in the watch window "
            "(06:00 ET to the calendar's close + 1h), every 1 h / W otherwise"
        )


# --------------------------------------------------------------------------
# Jobs and their status
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobSkipped:
    """What a body returns when it completed without fault and fetched nothing.

    Recorded as a skip with ``reason`` -- never as a success. See the module
    docstring's *A run that fetched nothing is skipped*.
    """

    reason: str


#: A job body: ``None`` for work done, :class:`JobSkipped` for nothing to do,
#: an exception for a failure.
JobBody = Callable[[], Awaitable[JobSkipped | None]]


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
    run: JobBody
    rule: str
    inputs: Mapping[str, str] = field(default_factory=dict)
    #: Run once when the scheduler starts, before the first slot, with the
    #: same failure handling as :attr:`run`. It must decide from the rows it
    #: finds whether anything is missing, and do nothing when not -- see the
    #: module docstring's *Clocks*. The one exception is the watch tier's,
    #: which always makes one request: its cadence cannot be known until it
    #: has (see ``_news_watch_tier_catch_up``).
    catch_up: JobBody | None = None


@dataclass(frozen=True, slots=True)
class JobStatus:
    """A snapshot of one job's record. All instants UTC; ``None`` is "never"."""

    name: str
    rule: str
    schedule: str
    #: The job definition's own schedule -- what a reader measures "how many
    #: runs should have happened since" against, never a re-typed interval.
    cadence: Schedule
    #: ``None`` before the job's loop first asks its schedule, and for good
    #: once :attr:`stopped`.
    next_run: datetime | None
    last_started: datetime | None
    last_success: datetime | None
    last_failure: datetime | None
    last_error_type: str | None
    #: Runs that did their work. A skip is not counted here.
    runs: int
    failures: int
    #: Runs (and catch-ups) that completed without fault and fetched nothing.
    skips: int = 0
    last_skipped: datetime | None = None
    last_skip_reason: str | None = None
    #: The job's task has ended and the job will not run again in this
    #: process: it died (``context_job_task_died``) or its calendar ran out
    #: (``context_job_unscheduled``). ``next_run`` is then ``None``.
    stopped: bool = False

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
    skips: int = 0
    last_skipped: datetime | None = None
    last_skip_reason: str | None = None
    stopped: bool = False


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
            # A page reading this job's status must see that it will not run
            # again, not the slot it was sleeping toward when it died.
            record = self._records.get(task.get_name().removeprefix("context-job:"))
            if record is not None:
                record.next_run = None
                record.stopped = True
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
                cadence=job.schedule,
                next_run=record.next_run,
                last_started=record.last_started,
                last_success=record.last_success,
                last_failure=record.last_failure,
                last_error_type=record.last_error_type,
                runs=record.runs,
                failures=record.failures,
                skips=record.skips,
                last_skipped=record.last_skipped,
                last_skip_reason=record.last_skip_reason,
                stopped=record.stopped,
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
        if job.catch_up is not None:
            await self._run_catch_up(job, job.catch_up, record)
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
                record.stopped = True
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
                outcome = await job.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure(job, record, exc, scheduled_for=due)
            else:
                if isinstance(outcome, JobSkipped):
                    self._record_skip(job, record, outcome, phase="scheduled")
                    continue
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

    async def _run_catch_up(
        self,
        job: ScheduledJob,
        catch_up: JobBody,
        record: _JobRecord,
    ) -> None:
        """The job's start-up catch-up, once. A failure is stated, never fatal."""
        record.last_started = self._now()
        try:
            outcome = await catch_up()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure(
                job, record, exc, scheduled_for=None, phase="catch_up"
            )
        else:
            if isinstance(outcome, JobSkipped):
                self._record_skip(job, record, outcome, phase="catch_up")
                return
            record.runs += 1
            record.last_success = self._now()
            logger.debug(
                "context job caught up at start",
                extra={
                    "event": "context_job_caught_up",
                    "job": job.name,
                    "finished_at": record.last_success.isoformat(),
                },
            )

    def _record_skip(
        self,
        job: ScheduledJob,
        record: _JobRecord,
        skipped: JobSkipped,
        *,
        phase: str,
    ) -> None:
        """State one skip. **Never raises**, for :meth:`_record_failure`'s reason.

        ``last_success`` and ``runs`` are left alone: nothing was fetched.
        """
        try:
            skipped_at = self._now()
        except Exception:
            skipped_at = _utc_now()
        record.skips += 1
        record.last_skipped = skipped_at
        try:
            reason = str(skipped.reason)
        except Exception:
            reason = "(the skip reason could not be rendered)"
        record.last_skip_reason = reason
        try:
            logger.info(
                "a context job had nothing to fetch; recorded as skipped, not "
                "as a success",
                extra={
                    "event": "context_job_skipped",
                    "job": job.name,
                    "phase": phase,
                    "rule": job.rule,
                    "reason": reason,
                    "skipped_at": skipped_at.isoformat(),
                },
            )
        except Exception:
            pass

    def _record_failure(
        self,
        job: ScheduledJob,
        record: _JobRecord,
        exc: Exception,
        *,
        scheduled_for: datetime | None,
        phase: str = "scheduled",
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
                    "phase": phase,
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


class AlpacaContextSource(AlpacaNewsSource, AssetSource, TradeabilityInputs, Protocol):
    """Alpaca as the context jobs use it. ``AlpacaProvider`` satisfies it.

    Benzinga news (``data.``), the day's optionable asset list (``paper-api.``)
    and the tradeability inputs (daily bars, the standard-root lookup). Market
    and reference data only -- **not** the broker: nothing here reads an
    account, and nothing here can place an order.
    """


class FinnhubNewsSource(CompanyNewsSource, MarketNewsSource, IpoDateSource, Protocol):
    """Finnhub as the context jobs use it. ``FinnhubProvider`` satisfies it.

    Company news (the watch tier), market news (discovery) and ``ipo_date``
    (owner decision Q12, for a partial ADV window) -- one instance, the same
    one the market-cap column reads, so Finnhub's 60/min is counted once.
    """


#: The open-position underlyings, as the watch universe's position members.
#: A narrow async callable rather than a broker: the API builds it over the
#: paper account's read-only positions and hands only the callable here. A
#: raise is logged by the universe build and leaves positions out of one cycle.
PositionUnderlyingsSource = Callable[[], Awaitable[Iterable[str]]]


async def _no_position_underlyings() -> frozenset[str]:
    """No positions: what :class:`ContextServices` built without a source reads."""
    return frozenset()


class PositionUnderlyings:
    """The last-known open-position underlyings, for the watch universe.

    **In memory, and nothing else.** The holder has a frozen set and the
    instant it was last replaced; it holds no broker, no registry, no
    callable. It lives here rather than in ``corollary.api`` so that the
    object the context jobs are handed a view of is one whose module the
    jobs' import scan may walk (unit 4B2 audit: the jobs used to hold a
    callable that closed over the whole service registry).

    Two readers, one writer. ``GET /api/news/watch`` reads it; the jobs read
    it through :class:`HeldPositionUnderlyings`; the lifespan's
    ``PaperPositionsRefresher`` (``corollary.api.deps``) is the one thing that
    calls :meth:`replace`, from the **paper** account's positions. Empty, with
    :attr:`as_of` ``None``, until the first replace -- stated by the route as
    "never read", not "none held".

    The symbols are kept exactly as they arrived: ``watch_universe`` is the
    one place that normalises them and skips a non-equity deliverable
    (``GME.WS``) with its reason. Single event loop, one assignment per
    replace, so a reader sees the old set or the new one, never half of each.
    """

    def __init__(self) -> None:
        self._symbols: frozenset[str] = frozenset()
        self._as_of: datetime | None = None

    def current(self) -> frozenset[str]:
        return self._symbols

    @property
    def as_of(self) -> datetime | None:
        """When :meth:`replace` last ran (UTC), or ``None`` if it never has."""
        return self._as_of

    def replace(self, symbols: Iterable[str], *, at: datetime) -> None:
        self._symbols = frozenset(symbols)
        self._as_of = _require_utc(at)


@dataclass(frozen=True, slots=True, eq=False)
class HeldPositionUnderlyings:
    """What :attr:`ContextServices.position_underlyings` is in production.

    An async read of a :class:`PositionUnderlyings` holder and **nothing
    more**: it never fetches, so it can never be the thing that reaches an
    account. The broker read that fills the holder is the lifespan's, outside
    everything a context job is built from -- ``tests/engine/test_scheduler.py``
    walks the object graph of the services the real lifespan builds and
    fails if a broker, the registry or the runtime is reachable from it.
    """

    holder: PositionUnderlyings

    async def __call__(self) -> frozenset[str]:
        return self.holder.current()


#: How long shutdown waits for a context job's in-flight database work. A job
#: task cancelled while awaiting ``asyncio.to_thread`` stops waiting; the
#: thread does not stop, and its commit would otherwise land after the
#: lifespan returned.
CONTEXT_SESSIONS_DRAIN_TIMEOUT: Final = 10.0


class _CountedSession(Session):
    """A :class:`Session` that tells its factory when it closes -- once."""

    def __init__(self, bind: Engine, *, on_close: Callable[[], None]) -> None:
        super().__init__(bind)
        self._corollary_on_close: Callable[[], None] | None = on_close

    def close(self) -> None:
        try:
            super().close()
        finally:
            on_close, self._corollary_on_close = self._corollary_on_close, None
            if on_close is not None:
                on_close()


class ContextSessions:
    """The context jobs' session factory, which shutdown can drain.

    ``aclose`` on the scheduler cancels job *tasks*; a task awaiting
    ``asyncio.to_thread`` is cancelled while the worker thread runs on and
    commits. So every session this factory opens is counted until it closes,
    and :meth:`drain` -- called by the lifespan straight after the
    scheduler's ``aclose`` -- first refuses any new session (a thread that
    had not yet opened one fails rather than writing after shutdown) and then
    waits, bounded, for the open ones to close. Every job opens its session
    with ``with``, so closing is not optional.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._condition = threading.Condition()
        self._open = 0
        self._closed = False

    def __call__(self) -> Session:
        with self._condition:
            if self._closed:
                raise RuntimeError(
                    "the context scheduler has shut down; no new database session"
                )
            self._open += 1
        return _CountedSession(self._engine, on_close=self._closed_one)

    def _closed_one(self) -> None:
        with self._condition:
            self._open -= 1
            self._condition.notify_all()

    @property
    def open_sessions(self) -> int:
        with self._condition:
            return self._open

    def _wait_closed(self, timeout: float) -> bool:
        with self._condition:
            self._closed = True
            return self._condition.wait_for(lambda: self._open == 0, timeout=timeout)

    async def drain(self, timeout: float = CONTEXT_SESSIONS_DRAIN_TIMEOUT) -> bool:
        """Refuse new sessions, then wait up to ``timeout`` s for open ones. Never raises.

        ``True`` when every session closed in time. ``False`` is logged --
        the count only, since a session carries no secret but its work
        might -- and shutdown continues: a stuck write is not a reason to
        leave the rule 9 alerts behind it undelivered.
        """
        drained = await asyncio.to_thread(self._wait_closed, timeout)
        if not drained:
            logger.error(
                "context job database work was still running at shutdown",
                extra={
                    "event": "context_sessions_not_drained",
                    "open_sessions": self.open_sessions,
                    "timeout_seconds": timeout,
                },
            )
        return drained



#: The event an adopted NPORT-P/A raises (owner decision 2026-09-30). Routed by
#: ``notification_route`` (migration 0011), which a human may change.
SPDR_SEED_AMENDED: Final = "spdr_seed_amended"

#: How many undelivered notices :class:`ContextNotices` holds before it drops
#: (and logs) the oldest. The only producer today is a weekly job; the bound
#: exists so an app whose deliverer never started cannot grow without limit.
CONTEXT_NOTICE_BACKLOG: Final = 64


@dataclass(frozen=True)
class ContextNotice:
    """One notification a context job asks for: words only.

    No channels (``notification_route`` decides those at emission, never the
    caller) and no sink -- the job hands this to :class:`ContextNotices` and
    is done. ``account`` is ``None``: a context feed belongs to no book.
    """

    event: str
    severity: str
    title: str
    body: str
    at: datetime
    correlation_id: str
    account: str | None = None


class ContextNotices:
    """The context jobs' outbox -- how a job notifies **without** holding the runtime.

    Decision 1 keeps the engine runtime out of :class:`ContextServices`, and
    the one notification path (decision 14) is the runtime's
    ``notify_operator_action``. So a job never calls a notifier: it ``put``s
    a :class:`ContextNotice` here, and the lifespan -- which owns the runtime
    and is not reachable from any job -- drains this and hands each notice
    to that path (``corollary.api.app``). This object holds a deque and a
    lock and nothing else, so the isolation walk finds no broker, registry,
    runtime or supervisor behind it.

    Decision 20: ``put`` **never raises and never blocks** on delivery, so a
    notice can neither delay nor fail the job that asked for it. Thread-safe,
    since a job body may run work in a worker thread.
    """

    def __init__(self, *, limit: int = CONTEXT_NOTICE_BACKLOG) -> None:
        self._limit = limit
        self._pending: collections.deque[ContextNotice] = collections.deque()
        self._lock = threading.Lock()

    def put(self, notice: ContextNotice) -> None:
        dropped: ContextNotice | None = None
        with self._lock:
            if len(self._pending) >= self._limit:
                dropped = self._pending.popleft()
            self._pending.append(notice)
        if dropped is not None:
            logger.error(
                "the context notice outbox is full; the oldest undelivered notice was dropped",
                extra={
                    "event": "context_notice_dropped",
                    "notification_event": dropped.event,
                    "title": dropped.title,
                    "at": dropped.at.isoformat(),
                    "correlation_id": dropped.correlation_id,
                    "limit": self._limit,
                },
            )

    def drain(self) -> list[ContextNotice]:
        """Every pending notice, oldest first, removed from the outbox."""
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        return pending

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


def spdr_amended_notice(
    session_factory: Callable[[], Session],
    snapshot_id: int,
    adopted: Mapping[str, str],
    at: datetime,
) -> ContextNotice | None:
    """The ``spdr_seed_amended`` notice for an accepted snapshot, from its **stored** rows.

    Decision 20: every value quoted is read back from what was written.
    ``adopted`` (the build's ``etf -> accession``) only selects which funds to
    name, and must agree with the stored rows -- ``None`` if the snapshot is
    missing, not accepted, or any adopted fund's stored accession differs,
    so the notice and the table cannot disagree.
    Names the report date, the filed date, and each amended fund with its
    accession -- public SEC identifiers; nothing here reads the environment,
    so no key or URL can reach the words (rule 6).
    """
    if not adopted:
        return None
    stored = stored_snapshot_amendments(session_factory, snapshot_id, adopted)
    if stored is None or dict(stored.accessions) != dict(adopted):
        return None
    funds = ", ".join(f"{etf} {accession}" for etf, accession in sorted(stored.accessions.items()))
    return ContextNotice(
        event=SPDR_SEED_AMENDED,
        severity="info",
        title="SPDR seed amended",
        body=(
            f"Adopted NPORT-P/A for the {stored.report_date.isoformat()} report date "
            f"(filed through {stored.filed_date.isoformat()}): {funds}. The sector "
            "seed now serves the amended holdings."
        ),
        at=at,
        correlation_id=uuid.uuid4().hex,
    )


@dataclass(frozen=True, slots=True)
class ContextServices:
    """Everything a context job may be built from -- and, by omission, what it may not.

    **No engine runtime, no watchdog, no socket supervisor, no broker.** Rule
    9's switch is fed by the three vendor sockets and nothing else, and a job
    cannot call what it was never given. Adding the runtime here is the change
    the isolation test exists to refuse. The positions the watch universe
    needs arrive as :data:`PositionUnderlyingsSource` -- in production a
    :class:`HeldPositionUnderlyings`, an in-memory read of a holder -- never as
    a broker object, and never as a callable that *could* reach one: the
    lifespan's refresher reads the paper account and writes the holder, and
    is not in here. ``tests/engine/test_scheduler.py`` walks every object
    reachable from the services the real lifespan builds (fields, partials,
    bound methods, closure cells, instance attributes) and fails on a broker,
    the service registry, the runtime or a socket supervisor.

    Every vendor is ``None`` when it is not configured; its jobs then return
    :class:`JobSkipped` with the reason, and the app boots regardless.
    """

    session_factory: Callable[[], Session]
    #: FRED, or ``None`` when ``FRED_API_KEY`` is unset -- the lifespan says so
    #: once at startup, and the FRED job's body then does nothing.
    fred: ObservationSource | None = None
    #: The process's risk-free rate, which the FRED job updates and the
    #: market-data provider reads. A private default is a rate nobody reads.
    rates: RiskFreeRateSource = field(default_factory=RiskFreeRateSource)
    #: The day's asset directory. The lifespan passes ``app.state.asset_directory``
    #: -- **the same object** -- so the news routes read what the
    #: ``asset_directory`` job refreshed. A private default is a directory no
    #: route ever sees.
    assets: AssetDirectoryHolder = field(default_factory=AssetDirectoryHolder)
    #: The one write path into the news tables: its lock is what serialises
    #: every ingest and the prune. ``None`` (a test's services) and
    #: :func:`context_jobs` builds exactly one for its job set. It must hold
    #: :attr:`assets`, or ingest would filter tags against a directory nobody
    #: refreshes -- refused in ``__post_init__``.
    news_store: NewsStore | None = None
    alpaca: AlpacaContextSource | None = None
    finnhub: FinnhubNewsSource | None = None
    #: Massive, or ``None`` when ``MASSIVE_API_KEY`` is unset.
    massive: MassiveNewsSource | None = None
    #: The Markets page's universe -- the watch universe's ``markets`` members.
    #: Passed in, because this module may not import ``corollary.api``.
    markets: tuple[str, ...] = ()
    #: The curated universe's funds (its ``fund`` flag), which the rules
    #: tier's single-tag rule never lands on (AUDIT3-M1). Passed in, like
    #: :attr:`markets`. Read only when the services build their own news
    #: store; a store passed in brings its own name-book source. Required,
    #: keyword-only, with no default: an empty default would quietly reopen
    #: AUDIT3-M1 for a caller that forgot it -- the reason ``NameBook``
    #: refuses to default it too. ``()`` must be said, not implied.
    funds: tuple[str, ...] = field(kw_only=True)
    position_underlyings: PositionUnderlyingsSource = _no_position_underlyings
    #: The SPDR seed, for the sector leaders. The lifespan passes
    #: ``app.state.spdr_seed_loader`` so the jobs and the watch routes agree.
    #: ``None`` means the accepted N-PORT snapshot in *this* services'
    #: database (:func:`~corollary.data.seeds.nport.load_spdr_seed_from_db`
    #: over :attr:`session_factory`), filled in by ``__post_init__`` -- never
    #: a file in the source tree (unit 4SEC-B2 retired the SSGA CSV).
    seed_loader: Callable[[], SpdrSeed | None] | None = None
    #: SEC EDGAR, for the ``spdr_holdings`` job -- or ``None`` when
    #: ``SEC_USER_AGENT`` is unset (SEC requires a declared User-Agent and
    #: none is invented); the job then skips.
    sec: NportSource | None = None
    #: The market-data provider's CUSIP -> asset lookup (Alpaca's
    #: ``/v2/assets/{cusip}``) -- or ``None``, and the ``spdr_holdings`` job
    #: skips. A reference-data read, never the broker.
    cusips: CusipResolver | None = None
    #: OpenFIGI, for the ``spdr_holdings`` job's ISIN-only lines (spec Q17)
    #: -- or ``None``, and the job passes ``UnavailableIsinResolver``: a
    #: quarter with ISIN-only lines aborts before any CUSIP lookup, stores
    #: nothing, and the job reports a skip (said at INFO), never a refusal.
    #: Optional like SEC; ``OPENFIGI_API_KEY`` unset is still
    #: a provider (keyless), so ``None`` means the app was built without one.
    #: A reference-data lookup, never the broker.
    openfigi: IsinMappingSource | None = None
    #: Where a job puts a notification (:class:`ContextNotices`). The lifespan
    #: passes the one it drains into the runtime's notification path; a
    #: private default is an outbox nobody delivers, which is what a test's
    #: services want.
    notices: ContextNotices = field(default_factory=ContextNotices)

    def __post_init__(self) -> None:
        if self.seed_loader is None:
            object.__setattr__(
                self,
                "seed_loader",
                functools.partial(load_spdr_seed_from_db, self.session_factory),
            )
        if self.news_store is not None and self.news_store.assets is not self.assets:
            raise ValueError(
                "the news store holds a different asset directory from the one "
                "these services refresh; ingest would filter tags against a "
                "directory nobody refreshes"
            )


async def _calendar_probe() -> None:
    """The no-op job. Its whole output is the scheduler's record that it ran."""
    return None


#: FRED publishes a daily close the next morning (the 2026-09-22 ``VIXCLS``
#: close appeared at 08:37 CT on the 23rd); 10:00 ET is after that on an
#: ordinary day. The spec's *Feeds and budgets* FRED row.
FRED_DAILY_AT = time(10, 0)


def _skipped(outcome: object) -> JobSkipped | None:
    """A refresh or poll's outcome as a job outcome.

    ``NotRefreshed`` (FRED) and ``PollSkipped`` (news) skip, with their
    reason; anything else returned is work done. A failure is a raise, and
    never reaches here.
    """
    if isinstance(outcome, (NotRefreshed, PollSkipped)):
        return JobSkipped(outcome.reason)
    return None


async def _fred_dgs3mo_refresh(services: ContextServices) -> JobSkipped | None:
    """The daily ``fred_dgs3mo`` body."""
    return _skipped(
        await refresh_dgs3mo(
            fred=services.fred,
            session_factory=services.session_factory,
            rates=services.rates,
        )
    )


async def _fred_dgs3mo_catch_up(
    services: ContextServices, clock: UtcClock
) -> JobSkipped | None:
    """The start-up ``fred_dgs3mo`` catch-up, judging staleness on ``clock``."""
    return _skipped(
        await catch_up_dgs3mo(
            fred=services.fred,
            session_factory=services.session_factory,
            rates=services.rates,
            now=clock,
        )
    )


# -- the news jobs (Phase 3 step 4, decision 21) ----------------------------

#: *Feeds and budgets*: Benzinga via Alpaca, 60 s in session, 5 min otherwise.
ALPACA_NEWS_IN_SESSION = timedelta(seconds=60)
ALPACA_NEWS_OTHERWISE = timedelta(minutes=5)
#: Finnhub ``/news?category=general``: 5 min, day and night.
FINNHUB_MARKET_NEWS_EVERY = timedelta(minutes=5)
#: Massive ``/v2/reference/news``: 15 min, day and night (a 5/min bucket).
MASSIVE_NEWS_EVERY = timedelta(minutes=15)
#: The optionable asset list: daily at 07:30 ET -- every day, not trading
#: days only, because ``MAX_DIRECTORY_AGE`` is 26 h and a weekend would lapse
#: it, and the tradeability job warns on a stale directory every run.
ASSET_DIRECTORY_AT = time(7, 30)
#: ADV / tradeability for off-watch tickers carrying a qualifying news signal: every 15 min.
TRADEABILITY_EVERY = timedelta(minutes=15)
#: Retention: nightly at 03:00 ET. 03:00 exists exactly once on both DST
#: change days -- spring forward skips 02:00-03:00 and lands *on* 03:00 EDT,
#: fall back repeats 01:00-02:00 -- so the prune neither doubles nor vanishes.
NEWS_PRUNE_AT = time(3, 0)


class _DiscoveryPoller(Protocol):
    async def poll(self, now: datetime) -> object: ...


async def _news_watch_tier(poller: WatchTierPoller, clock: UtcClock) -> JobSkipped | None:
    """One watch-tier request: the next symbol in the round robin."""
    return _skipped(await poller.poll_next(clock()))


async def _news_discovery(poller: _DiscoveryPoller, clock: UtcClock) -> JobSkipped | None:
    """One discovery-feed call (Alpaca, Finnhub market, Massive) from its cursor."""
    return _skipped(await poller.poll(clock()))


async def _news_watch_tier_catch_up(
    poller: WatchTierPoller, clock: UtcClock
) -> JobSkipped | None:
    """One watch-tier request at start, so W is known before the first slot.

    Until the poller has built the universe once, W is unknown and taken as
    1 -- which would put the first request a whole window interval (up to an
    hour overnight) after a restart. This asks once, straight away: one
    Finnhub request per process start, against 60/min. The rotation is in
    memory, so after a restart it resumes at the first symbol.
    """
    return await _news_watch_tier(poller, clock)


async def _asset_directory_refresh(services: ContextServices) -> JobSkipped | None:
    """Fetch and hold the day's asset directory, in the holder the routes read."""
    return _skipped(await refresh_assets(services.assets, services.alpaca))


async def _asset_directory_catch_up(services: ContextServices) -> JobSkipped | None:
    """At start, only while nothing is held: the watch routes 503 until then.

    A process starts with an empty holder, so in practice this fetches once
    per start -- one ``paper-api.`` request against 200/min, which a
    ``--reload`` loop can afford -- and never while a directory is held.
    """
    if services.assets.current() is not None:
        return JobSkipped(
            "an asset directory is already held; the start-up catch-up had nothing to fill"
        )
    return await _asset_directory_refresh(services)


async def _tradeability(
    services: ContextServices, universe: UniverseSource, clock: UtcClock
) -> JobSkipped | None:
    """Check today's off-watch tickers. Finnhub settles a partial ADV window (Q12)."""
    return _skipped(
        await refresh_tradeability_cache(
            holder=services.assets,
            provider=services.alpaca,
            universe=universe,
            session_factory=services.session_factory,
            now=clock(),
            ipo_dates=services.finnhub,
        )
    )


async def _news_prune(store: NewsStore, clock: UtcClock) -> JobSkipped | None:
    """Decision 21's retention, under the store's lock so it never races an ingest."""
    await prune_news(store, clock())
    return None


def _news_jobs(services: ContextServices, clock: UtcClock) -> list[ScheduledJob]:
    """The step 4 news jobs, over one store and one poller per feed.

    Called once per job set, so a process holds exactly one
    :class:`NewsStore` (unless the services brought theirs) and one poller
    per feed. The cursors live in the pollers, in memory, recovered from the
    stored rows on each first run.
    """
    seed_loader = services.seed_loader
    assert seed_loader is not None  # filled by ContextServices.__post_init__
    store = (
        services.news_store
        if services.news_store is not None
        else NewsStore(
            session_factory=services.session_factory,
            assets=services.assets,
            books=news_name_books(
                markets=services.markets,
                position_underlyings=services.position_underlyings,
                session_factory=services.session_factory,
                seed_loader=seed_loader,
                assets=services.assets,
                funds=services.funds,
            ),
        )
    )
    universe: UniverseSource = functools.partial(
        build_watch_universe,
        markets=services.markets,
        position_underlyings=services.position_underlyings,
        session_factory=services.session_factory,
        seed_loader=seed_loader,
    )
    watch = WatchTierPoller(provider=services.finnhub, universe=universe, store=store)
    alpaca_news = AlpacaNewsPoller(provider=services.alpaca, store=store)
    finnhub_market = FinnhubMarketNewsPoller(provider=services.finnhub, store=store)
    massive = MassiveNewsPoller(provider=services.massive, store=store)
    return [
        ScheduledJob(
            name="news_watch_tier",
            schedule=WatchTierCadence(universe_size=lambda: watch.last_universe_size),
            run=functools.partial(_news_watch_tier, watch, clock),
            catch_up=functools.partial(_news_watch_tier_catch_up, watch, clock),
            rule=(
                "decision 21's watch tier: company news for the watch universe "
                "(Markets, sector leaders, positions, manual), one symbol per "
                "request; when this fails that symbol waits for its next turn "
                "and its news goes stale"
            ),
            inputs={"host": FINNHUB_HOST, "endpoint": "/company-news"},
        ),
        ScheduledJob(
            name="news_alpaca",
            schedule=TwoRate(
                in_session=ALPACA_NEWS_IN_SESSION, otherwise=ALPACA_NEWS_OTHERWISE
            ),
            run=functools.partial(_news_discovery, alpaca_news, clock),
            rule=(
                "decision 21's discovery tier: every Benzinga headline, "
                "untickered; when this fails the cursor stays put and the next "
                "run re-reads. Never a rule 9 input: REST news, not the Alpaca "
                "socket the watchdog judges"
            ),
            inputs={"host": ALPACA_DATA_HOST, "endpoint": "/v1beta1/news"},
        ),
        ScheduledJob(
            name="news_finnhub_market",
            schedule=EveryInterval(FINNHUB_MARKET_NEWS_EVERY),
            run=functools.partial(_news_discovery, finnhub_market, clock),
            rule=(
                "decision 21's discovery tier: Finnhub general market news from "
                "the minId cursor; when this fails the cursor stays put"
            ),
            inputs={"host": FINNHUB_HOST, "endpoint": "/news", "category": "general"},
        ),
        ScheduledJob(
            name="news_massive",
            schedule=EveryInterval(MASSIVE_NEWS_EVERY),
            run=functools.partial(_news_discovery, massive, clock),
            rule=(
                "decision 21's discovery tier: Massive's vendor-scored news, "
                "untickered, from the last published_utc seen; when this fails "
                "the cursor stays put"
            ),
            inputs={"host": MASSIVE_HOST, "endpoint": "/v2/reference/news"},
        ),
        ScheduledJob(
            name="asset_directory",
            schedule=AtTime(ASSET_DIRECTORY_AT, every_day),
            run=functools.partial(_asset_directory_refresh, services),
            catch_up=functools.partial(_asset_directory_catch_up, services),
            rule=(
                "decision 21's optionable list: ingest's tag filter, has_options "
                "for tradeability, and the watch routes' ticker check, which "
                "answer 503 until one is held; when this fails the last "
                "directory stays in use, with its age"
            ),
            inputs={
                "host": ALPACA_PAPER_TRADING_HOST,
                "endpoint": "/v2/assets",
                "attributes": "has_options",
            },
        ),
        ScheduledJob(
            name="tradeability_cache",
            schedule=EveryInterval(TRADEABILITY_EVERY),
            run=functools.partial(_tradeability, services, universe, clock),
            rule=(
                "decision 21's tradeability cache for off-watch tickers carrying "
                "a qualifying news signal (ADV, last close, has_options, "
                "standard root, IPO date); "
                "when this fails those tickers stay unchecked and are asked "
                "again next run"
            ),
            inputs={
                "host": ALPACA_DATA_HOST,
                "endpoint": "/v2/stocks/bars",
                "also": (
                    f"{ALPACA_PAPER_TRADING_HOST} /v2/options/contracts, "
                    f"{FINNHUB_HOST} /stock/profile2"
                ),
            },
        ),
        ScheduledJob(
            name="news_prune",
            schedule=AtTime(NEWS_PRUNE_AT, every_day),
            run=functools.partial(_news_prune, store, clock),
            rule=(
                "decision 21's retention: null old summaries and delete old "
                "unlabelled groups; when this fails the tables grow a night "
                "longer and nothing else is affected"
            ),
            inputs={"host": "local"},
        ),
    ]


# -- the SPDR sector seed from SEC N-PORT (Phase 3 step 4, unit 4SEC-B2) ----

#: Weekly, Monday 09:00 ET: the eleven funds file NPORT-P quarterly, public
#: about 60 days after the quarter ends, so a weekly look finds a new quarter
#: within seven days of SEC posting it. The week is the clock here, not the
#: session -- EDGAR answers on a market holiday -- so the day rule is
#: :func:`on_weekdays`, not :func:`trading_days`.
SPDR_HOLDINGS_AT = time(9, 0)
SPDR_HOLDINGS_DAYS: DayRule = on_weekdays(0)
#: The start-up catch-up builds only when no attempt was ever recorded, or the
#: latest recorded attempt is older than this. Only two things are ever
#: recorded: an accepted snapshot, and a refusal that is a *fact about the
#: filing* (a fund outside 90-110 with every line answered, an unreadable or
#: ambiguous amendment, a missing fund, ...) -- so this gate is what stops a
#: real data problem costing ~474 CUSIP lookups on every ``--reload`` start.
#: Nothing else is recorded, so nothing else suppresses it: an "unchanged"
#: run (a start past this age asks SEC's index once and skips), an abort (a
#: vendor outage: SEC, the CUSIP lookup, OpenFIGI), and a skip for a missing
#: precondition (no asset directory, no ISIN source -- 2026-10-07) all leave
#: the table as it was, so the next start retries.
SPDR_HOLDINGS_CATCH_UP_AFTER = timedelta(days=7)
#: How long the start-up catch-up waits for the day's asset directory before
#: skipping. Every job's catch-up starts at once in its own task, so the SPDR
#: catch-up can reach its build before the ``asset_directory`` catch-up has
#: filled the holder (one ``paper-api.`` request, normally a second or two).
#: Without the directory no ISIN answer can be checked, so the job would skip;
#: this waits instead, bounded. A directory that has not arrived by then is a
#: directory problem with its own failure record -- the catch-up skips,
#: stores nothing, and the next start retries.
SPDR_DIRECTORY_WAIT = timedelta(minutes=5)
#: How often that wait looks at the holder, which has no event to await. The
#: wait is counted in polls slept, not on the wall clock, so it is exact under
#: a test's sleeper and never shorter than the bound in production.
SPDR_DIRECTORY_POLL = timedelta(seconds=5)


class SpdrSnapshotAborted(Exception):
    """The SPDR build stored nothing because a vendor failed; the job's failure.

    ``rule`` is the builder's :class:`~corollary.data.seeds.nport.SnapshotRule`.
    Raised, so the scheduler records a failure with the job's rule and inputs;
    the job then waits for its next weekly slot (a failure is never retried
    on a tighter loop). The builder has already logged it at ERROR.
    """

    def __init__(self, rule: SnapshotRule, reason: str) -> None:
        super().__init__(f"{rule.value}: {reason}")
        self.rule = rule
        self.reason = reason


#: Why the SPDR job does nothing without the day's asset directory.
_SPDR_NO_DIRECTORY = (
    "no asset directory is held yet, so an N-PORT line identified by ISIN alone "
    "cannot be checked against the broker's listing; a missing precondition is "
    "not a refusal, so nothing is stored and the loaded SPDR snapshot, if any, stays"
)


def _spdr_unavailable(services: ContextServices) -> JobSkipped | None:
    if services.sec is None:
        return JobSkipped(
            "SEC_USER_AGENT is unset (or unusable), so the SEC N-PORT source is "
            "unavailable; the loaded SPDR snapshot, if any, stays"
        )
    if services.cusips is None:
        return JobSkipped(
            "the market-data provider offers no CUSIP lookup, so N-PORT holdings "
            "cannot be resolved to tickers; the loaded SPDR snapshot, if any, stays"
        )
    return None


async def _spdr_holdings(services: ContextServices, clock: UtcClock) -> JobSkipped | None:
    """Build the SPDR sector snapshot from the latest N-PORT quarter.

    * ``accepted`` -- stored and now current: work done.
    * ``refused`` -- **work done too**: the job did what it is for, and the
      builder recorded the attempt (rule and reason) and logged it with its
      rule at WARNING; the previous accepted snapshot stays current. A skip
      would say "nothing to do", which is not what happened.
    * ``unchanged`` -- :class:`JobSkipped`: no newer quarter than the loaded.
    * ``aborted`` -- :class:`SpdrSnapshotAborted`, a failure: nothing stored.
      :attr:`SnapshotRule.SEC_ACCESS_REFUSED` is also logged here at ERROR
      under its own event, because SEC refusing the declared User-Agent is an
      operator problem no retry fixes.

    The ISIN seam is :class:`~corollary.data.seeds.isin.OpenFigiIsinResolver`
    over :attr:`ContextServices.openfigi` (spec Q17), checked against the
    shared day's asset directory -- the same directory the builder is given.

    **A missing precondition is a skip, never a stored refusal** (2026-10-07).
    A stored refusal is a fact about the filing and suppresses the start-up
    catch-up for :data:`SPDR_HOLDINGS_CATCH_UP_AFTER`; "the directory had not
    loaded yet" is not one. So before the directory's first fetch the job
    skips without asking SEC anything, and with no OpenFIGI provider (said at
    INFO) the seam is :class:`UnavailableIsinResolver`: a quarter with
    ISIN-only lines aborts under
    :attr:`SnapshotRule.ISIN_SOURCE_UNAVAILABLE` before any CUSIP lookup, and
    that abort is returned as a skip -- configuration, like an unset
    ``SEC_USER_AGENT``, not a vendor failing. Either way nothing is stored
    and the band is never lowered.
    """
    unavailable = _spdr_unavailable(services)
    if unavailable is not None:
        return unavailable
    assert services.sec is not None and services.cusips is not None
    directory = services.assets.current()
    if directory is None:
        return JobSkipped(_SPDR_NO_DIRECTORY)
    isin_resolver: IsinResolver
    if services.openfigi is not None:
        isin_resolver = OpenFigiIsinResolver(
            services.openfigi, services.session_factory, directory=directory, clock=clock
        )
    else:
        logger.info(
            "no OpenFIGI provider is configured; a quarter with N-PORT lines "
            "identified by ISIN alone cannot be built, and nothing is stored",
            extra={"event": "spdr_isin_source_unavailable", "at": clock().isoformat()},
        )
        isin_resolver = UnavailableIsinResolver("no OpenFIGI provider is configured")
    outcome = await build_spdr_snapshot(
        services.sec,
        services.cusips,
        services.session_factory,
        isin_resolver=isin_resolver,
        directory=directory,
        clock=clock,
    )
    if outcome.status == "unchanged":
        loaded = outcome.report_date.isoformat() if outcome.report_date else "unknown"
        return JobSkipped(
            f"no NPORT-P quarter newer than the loaded {loaded}; nothing rebuilt"
        )
    if outcome.status == "aborted":
        rule = outcome.rule if outcome.rule is not None else SnapshotRule.SEC_UNAVAILABLE
        reason = outcome.reason or ""
        if rule is SnapshotRule.ISIN_SOURCE_UNAVAILABLE:
            # Configuration, not a vendor failing: a skip, as an unset
            # SEC_USER_AGENT is. The directory was checked above, so in the
            # job this is "no OpenFIGI provider". Nothing was stored.
            return JobSkipped(
                f"the N-PORT lines identified by ISIN alone cannot be resolved "
                f"({reason}); OpenFIGI is the ISIN source (spec Q17). Nothing is "
                f"stored and the loaded SPDR snapshot, if any, stays"
            )
        if rule is SnapshotRule.SEC_ACCESS_REFUSED:
            logger.error(
                "SEC refused access to the N-PORT source; check the declared "
                "SEC_USER_AGENT. The SPDR snapshot is not updated and the job "
                "waits for its next weekly slot",
                extra={
                    "event": "spdr_snapshot_sec_access_refused",
                    "rule": rule.value,
                    "variable": "SEC_USER_AGENT",
                    "at": clock().isoformat(),
                },
            )
        raise SpdrSnapshotAborted(rule, reason)
    if outcome.status == "accepted" and outcome.adopted_amendments:
        _notify_spdr_amended(services, outcome.snapshot_id, outcome.adopted_amendments, clock)
    return None


def _notify_spdr_amended(
    services: ContextServices,
    snapshot_id: int | None,
    adopted: Mapping[str, str],
    clock: UtcClock,
) -> None:
    """Put one ``spdr_seed_amended`` notice. **Never raises** (decision 20).

    The snapshot has already committed; a notice that cannot be built is a
    logged error and the job still succeeded. Exactly one per adoption: the
    builder only reports ``adopted_amendments`` on the run that stored them,
    and answers ``unchanged`` once they are carried.
    """
    try:
        if snapshot_id is None:
            raise ValueError("an accepted snapshot without an id")
        notice = spdr_amended_notice(services.session_factory, snapshot_id, adopted, clock())
        if notice is None:
            raise ValueError(
                f"stored snapshot {snapshot_id} does not carry the adopted amendments"
            )
        services.notices.put(notice)
    except Exception as exc:
        logger.error(
            "the spdr_seed_amended notice could not be built; the amended snapshot stands",
            extra={
                "event": "spdr_seed_amended_not_notified",
                "snapshot_id": snapshot_id,
                "error_type": type(exc).__name__,
            },
        )


async def _await_asset_directory(
    holder: AssetDirectoryHolder,
    sleep: Sleeper,
    *,
    bound: timedelta = SPDR_DIRECTORY_WAIT,
    poll: timedelta = SPDR_DIRECTORY_POLL,
) -> bool:
    """Wait, bounded, until ``holder`` holds a directory. True once it does.

    Polls, because the holder has no event and growing one is a change to the
    news side for one caller. The bound is counted in polls slept rather than
    read off a clock, so a test's sleeper makes it exact and a real
    ``asyncio.sleep`` can only make it longer, never shorter.
    """
    waited = timedelta(0)
    while holder.current() is None:
        if waited >= bound:
            return False
        await sleep(poll.total_seconds())
        waited += poll
    return True


async def _spdr_holdings_catch_up(
    services: ContextServices, clock: UtcClock, sleep: Sleeper
) -> JobSkipped | None:
    """At start: build only when nothing was ever recorded, or the last record is old.

    The seven-day gate is asked first -- a database read -- so a suppressed
    catch-up never waits. Then, because every catch-up starts at once, it
    waits up to :data:`SPDR_DIRECTORY_WAIT` for the ``asset_directory``
    catch-up to fill the shared holder. If it does not, this skips and stores
    nothing, so the next start tries again.
    """
    unavailable = _spdr_unavailable(services)
    if unavailable is not None:
        return unavailable
    attempt = latest_snapshot_attempt(services.session_factory)
    if attempt is not None and clock() - attempt.built_at <= SPDR_HOLDINGS_CATCH_UP_AFTER:
        return JobSkipped(
            f"the latest SPDR snapshot attempt ({attempt.status}, report date "
            f"{attempt.report_date.isoformat()}) was recorded at "
            f"{attempt.built_at.isoformat()}, within "
            f"{SPDR_HOLDINGS_CATCH_UP_AFTER.days} days; the weekly slot will look"
        )
    if services.assets.current() is None:
        bound_seconds = int(SPDR_DIRECTORY_WAIT.total_seconds())
        logger.info(
            "the SPDR catch-up is waiting up to %d s for the asset directory",
            bound_seconds,
            extra={
                "event": "spdr_catch_up_awaiting_directory",
                "bound_seconds": bound_seconds,
                "at": clock().isoformat(),
            },
        )
        if not await _await_asset_directory(services.assets, sleep):
            return JobSkipped(
                f"no asset directory arrived within {bound_seconds // 60} minutes of "
                f"start; {_SPDR_NO_DIRECTORY}. The next start retries"
            )
    return await _spdr_holdings(services, clock)


def _spdr_job(services: ContextServices, clock: UtcClock, sleep: Sleeper) -> ScheduledJob:
    return ScheduledJob(
        name="spdr_holdings",
        schedule=AtTime(SPDR_HOLDINGS_AT, SPDR_HOLDINGS_DAYS),
        run=functools.partial(_spdr_holdings, services, clock),
        catch_up=functools.partial(_spdr_holdings_catch_up, services, clock, sleep),
        rule=(
            "the SPDR sector seed (ticker -> sector, the sector leaders) from "
            "the Select Sector SPDRs' NPORT-P, CUSIPs resolved by the asset "
            "lookup; a refused snapshot (a fact about the filing) is recorded "
            "with its rule and the previous accepted one stays current; when "
            "this fails, or the asset directory or ISIN source is missing, "
            "nothing is stored and the loaded seed stays in use, with its dates"
        ),
        inputs={
            "host": SEC_DATA_HOST,
            "endpoint": "/submissions/CIK0001064641.json",
            "also": (
                f"{SEC_WWW_HOST} /files/company_tickers_mf.json and "
                f"/Archives/edgar/data/1064641/.../primary_doc.xml, "
                f"{ALPACA_PAPER_TRADING_HOST} /v2/assets/{{cusip}}"
            ),
        },
    )


def context_jobs(
    services: ContextServices,
    *,
    clock: UtcClock = _utc_now,
    sleep: Sleeper = asyncio.sleep,
) -> list[ScheduledJob]:
    """The Phase 3 job set. Each feed's step adds its own.

    Every job ships whatever the configuration, so the rule 9 isolation
    tests always run all of them; a job whose vendor is not configured
    returns :class:`JobSkipped` from its body. ``clock`` is the scheduler's,
    so a catch-up judges "stale" on the same clock the slots run on, and
    ``sleep`` is too, so the SPDR catch-up's bounded wait for the asset
    directory never sleeps on the wall clock under a test.
    """
    return [
        ScheduledJob(
            name="calendar_probe",
            schedule=EveryWhileOpen(timedelta(minutes=15)),
            run=_calendar_probe,
            rule=(
                "proves the context scheduler runs on the market calendar in "
                "the lifespan; feeds no page"
            ),
        ),
        ScheduledJob(
            name="fred_dgs3mo",
            schedule=AtTime(FRED_DAILY_AT, trading_days),
            run=functools.partial(_fred_dgs3mo_refresh, services),
            catch_up=functools.partial(_fred_dgs3mo_catch_up, services, clock),
            rule=(
                "the risk-free rate derived greeks are computed at (decision "
                "19); when this fails the last stored DGS3MO observation stays "
                "in use, with its date"
            ),
            inputs={
                "host": FRED_HOST,
                "endpoint": "/fred/series/observations",
                "series_id": DGS3MO_SERIES,
            },
        ),
        *_news_jobs(services, clock),
        _spdr_job(services, clock, sleep),
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
    return Scheduler(
        context_jobs(services, clock=clock, sleep=sleep),
        secrets=secrets,
        clock=clock,
        sleep=sleep,
    )


def no_scheduler(
    services: ContextServices, secrets: Callable[[], Sequence[str]]
) -> None:
    """No context jobs at all. What a route test's app is built with.

    Explicit, and the default, for the reason ``no_socket_supervisor`` is:
    later jobs call real vendors with keys from the environment, and a test
    app must not do that by forgetting an argument.
    """
    return None
