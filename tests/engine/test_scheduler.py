"""The context scheduler: a market-calendar clock, and never a rule-9 producer.

Phase 3 decision 1. Three things are pinned here:

* **The clock is the calendar.** Schedules resolve against
  ``corollary.calendars`` -- Thanksgiving 2026 (Thu 26 Nov) is a holiday and
  the day after is a 13:00 ET half-day, so an in-session job fires on neither
  side of those wrongly, and an ET wall-clock job lands on the right UTC
  instant either side of the 1 Nov 2026 DST change.
* **A failing job is contained.** It is logged with its rule, inputs,
  timestamp and exception class -- redacted -- and the scheduler keeps
  running it and every other job.
* **Rule 9 isolation** (marked ``risk``). A context job raising, including an
  Alpaca news failure from ``data.alpaca.markets``, leaves
  ``engine_state.halted`` unchanged and never touches the watchdog. The
  scheduler is never handed the runtime, and a test pins that too.
"""

import ast
import asyncio
import dataclasses
import functools
import heapq
import importlib.util
import inspect
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import get_type_hints

import httpx
import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session

from corollary.db.models import Base
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine import scheduler as scheduler_module
from corollary.engine.runtime import EngineRuntime, Notification
from corollary.engine.scheduler import (
    ALPACA_NEWS_IN_SESSION,
    ALPACA_NEWS_OTHERWISE,
    ASSET_DIRECTORY_AT,
    FINNHUB_MARKET_NEWS_EVERY,
    MASSIVE_NEWS_EVERY,
    NEWS_PRUNE_AT,
    TRADEABILITY_EVERY,
    AtTime,
    ContextServices,
    ContextSessions,
    EveryInterval,
    HeldPositionUnderlyings,
    EveryWhileOpen,
    JobSkipped,
    JobStatus,
    PositionUnderlyings,
    ScheduledJob,
    Scheduler,
    TwoRate,
    WatchTierCadence,
    build_context_scheduler,
    context_jobs,
    every_day,
    on_weekdays,
    trading_days,
)
from corollary.data.macro.risk_free import store_observations
from corollary.data.news.assets import AssetDirectoryHolder
from corollary.data.news.pollers import NewsStore, watch_interval
from corollary.data.providers.interface import AssetDirectory
from corollary.ratelimit import (
    ALPACA_DATA_HOST,
    ALPACA_PAPER_TRADING_HOST,
    FINNHUB_HOST,
    MASSIVE_HOST,
)
from tests.data.news.test_pollers import (
    DIRECTORY,
    FakeAlpaca,
    FakeCompanyNews,
    FakeMarketNews,
    FakeMassive,
)
from corollary.data.providers.fred import (
    FredCredentials,
    FredObservation,
    FredProvider,
)
from corollary.pricing.rates import RateProvenance, RiskFreeRateSource
from corollary.ratelimit import FRED_HOST, HostRateLimiter
from tests.data.providers.test_fred_provider import FAKE_KEY, fixture_body_text
from tests.engine.test_runtime import read_state, resume_engine

UTC = timezone.utc

#: Wednesday 25 Nov 2026, 20:00 UTC = 15:00 ET: the last hour before the
#: Thanksgiving holiday.
WED_1500_ET = datetime(2026, 11, 25, 20, 0, tzinfo=UTC)
THANKSGIVING = date(2026, 11, 26)
#: Friday 27 Nov 2026: opens 09:30 ET (14:30 UTC), closes early at 13:00 ET
#: (18:00 UTC).
HALF_DAY_OPEN = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
HALF_DAY_CLOSE = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
MONDAY_OPEN = datetime(2026, 11, 30, 14, 30, tzinfo=UTC)
#: Thursday 26 Nov 2026, 20:00 ET (Friday 01:00 UTC): the holiday's evening.
THANKSGIVING_EVENING = datetime(2026, 11, 27, 1, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class FakeClock:
    """A UTC clock driven as a discrete-event simulation.

    Each job is its own task, so a clock that simply advanced on every
    ``sleep`` would move once per *task* and hand one job another's time.
    Instead ``sleep`` registers a wake-up instant and waits; :meth:`drive`
    lets every runnable task settle, then moves the clock to the earliest
    pending wake-up and releases exactly that sleeper. A week of calendar
    runs in milliseconds, in the order a real clock would produce.

    Past ``stop_at`` the driver stops and sets :attr:`parked`: the scheduler
    is quiescent and its status can be read without racing it.

    ``wait_for_threads``: a job body that offloads blocking work with
    ``asyncio.to_thread`` -- as the scheduler's docstring requires -- is
    neither runnable on the loop nor asleep on this clock while the thread
    runs, so fifty ``sleep(0)`` yields do not settle it and the driver could
    park, or advance time, under it. With the flag set, the driver also waits
    (in real time, bounded) until every live ``context-job:`` task is asleep
    here before it moves the clock.
    """

    def __init__(
        self, start: datetime, *, stop_at: datetime, wait_for_threads: bool = False
    ) -> None:
        self.now = start
        self.stop_at = stop_at
        self.parked = asyncio.Event()
        self._waiting: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._sequence = 0
        self._wait_for_threads = wait_for_threads

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        wake: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sequence += 1
        heapq.heappush(
            self._waiting,
            (self.now + timedelta(seconds=seconds), self._sequence, wake),
        )
        await wake

    def _every_job_is_asleep(self) -> bool:
        live = [
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith("context-job:") and not task.done()
        ]
        return len(self._waiting) >= len(live)

    async def drive(self) -> None:
        while True:
            for _ in range(50):
                await asyncio.sleep(0)
            if self._wait_for_threads:
                deadline = asyncio.get_running_loop().time() + 10.0
                while not self._every_job_is_asleep():
                    assert asyncio.get_running_loop().time() < deadline, (
                        "a context job neither finished nor slept on the clock "
                        "within 10s"
                    )
                    await asyncio.sleep(0.001)
            if not self._waiting or self._waiting[0][0] > self.stop_at:
                self.now = max(self.now, self.stop_at)
                self.parked.set()
                return
            target, _, wake = heapq.heappop(self._waiting)
            self.now = max(self.now, target)
            wake.set_result(None)


class Recorder:
    """A job body that notes the instant it ran."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.ran: list[datetime] = []

    async def __call__(self) -> None:
        self.ran.append(self.clock())


def no_secrets() -> tuple[str, ...]:
    return ()


async def run_until_parked(
    scheduler: Scheduler, clock: FakeClock, *, timeout: float = 10
) -> None:
    scheduler.start()
    # The calendar is built on a worker thread before any job asks a
    # schedule anything. The driver parks when nothing is sleeping on the
    # fake clock, so it must not start until that build has finished.
    await asyncio.wait_for(scheduler.ready(), timeout=10)
    driver = asyncio.get_running_loop().create_task(clock.drive())
    try:
        await asyncio.wait_for(clock.parked.wait(), timeout=timeout)
    finally:
        driver.cancel()
        await scheduler.aclose()


# --------------------------------------------------------------------------
# The calendar clock -- pure schedules
# --------------------------------------------------------------------------


def test_an_in_session_job_starts_at_the_open() -> None:
    schedule = EveryWhileOpen(timedelta(minutes=15))
    before_open = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)  # 08:00 ET
    assert schedule.next_run(before_open) == datetime(
        2026, 11, 25, 14, 30, tzinfo=UTC
    )


def test_an_in_session_job_steps_by_its_interval_inside_the_session() -> None:
    schedule = EveryWhileOpen(timedelta(minutes=15))
    assert schedule.next_run(WED_1500_ET) == WED_1500_ET + timedelta(minutes=15)


def test_an_in_session_grid_is_anchored_at_the_open_not_at_the_instant_asked() -> None:
    """Slots are ``open + k * interval``, whatever instant the question is asked at.

    Anchored at the instant asked, a scheduler that restarts -- ``dev_app``
    under ``--reload``, once per save -- pushes the first run a full interval
    out every time, and a developer saving every ten minutes starves a
    fifteen-minute job for the whole session.
    """
    wednesday_open = datetime(2026, 11, 25, 14, 30, tzinfo=UTC)
    fifteen = EveryWhileOpen(timedelta(minutes=15))
    assert fifteen.next_run(wednesday_open) == wednesday_open + timedelta(minutes=15)
    assert fifteen.next_run(wednesday_open + timedelta(minutes=7)) == (
        wednesday_open + timedelta(minutes=15)
    )
    assert fifteen.next_run(wednesday_open + timedelta(minutes=14, seconds=59)) == (
        wednesday_open + timedelta(minutes=15)
    )
    # An interval that does not divide an hour stays on its own grid too.
    twenty_five = EveryWhileOpen(timedelta(minutes=25))
    assert twenty_five.next_run(wednesday_open + timedelta(minutes=26)) == (
        wednesday_open + timedelta(minutes=50)
    )
    # 16:00 ET is off a 25-minute grid from 09:30; the last slot is 15:50.
    assert twenty_five.next_run(datetime(2026, 11, 25, 20, 50, tzinfo=UTC)) == (
        HALF_DAY_OPEN
    )


@pytest.mark.asyncio
async def test_a_scheduler_restarted_mid_interval_keeps_the_sessions_grid() -> None:
    """Started at 10:07 ET -- a reload -- the first run is 10:15 ET, not 10:22."""
    started = datetime(2026, 11, 25, 15, 7, tzinfo=UTC)  # 10:07 ET
    clock = FakeClock(started, stop_at=started + timedelta(minutes=30))
    recorder = Recorder(clock)
    scheduler = Scheduler(
        [
            ScheduledJob(
                name="probe",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=recorder,
                rule="test",
            )
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    await run_until_parked(scheduler, clock)
    assert recorder.ran == [
        datetime(2026, 11, 25, 15, 15, tzinfo=UTC),
        datetime(2026, 11, 25, 15, 30, tzinfo=UTC),
    ]


def test_an_in_session_job_skips_the_holiday_and_the_weekend() -> None:
    """Wednesday's last slot is 15:45 ET; the next is Friday's open, not Thursday."""
    schedule = EveryWhileOpen(timedelta(minutes=15))
    last_wednesday = datetime(2026, 11, 25, 20, 45, tzinfo=UTC)
    assert schedule.next_run(last_wednesday) == HALF_DAY_OPEN
    during_holiday = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)
    assert schedule.next_run(during_holiday) == HALF_DAY_OPEN


def test_an_in_session_job_stops_at_a_half_days_early_close() -> None:
    """13:00 ET on the day after Thanksgiving, not 16:00: the next run is Monday."""
    schedule = EveryWhileOpen(timedelta(minutes=15))
    last_slot = HALF_DAY_CLOSE - timedelta(minutes=15)
    assert schedule.next_run(last_slot) == MONDAY_OPEN
    assert schedule.next_run(HALF_DAY_CLOSE) == MONDAY_OPEN
    assert schedule.next_run(datetime(2026, 11, 27, 19, 0, tzinfo=UTC)) == (
        MONDAY_OPEN
    )


def test_the_close_itself_is_not_in_session() -> None:
    schedule = EveryWhileOpen(timedelta(minutes=30))
    assert schedule.next_run(HALF_DAY_CLOSE - timedelta(minutes=30)) == MONDAY_OPEN


def test_an_at_time_job_is_specified_in_eastern_and_lands_in_utc_across_dst() -> None:
    """07:00 ET is 11:00 UTC on Friday 30 Oct and 12:00 UTC on Monday 2 Nov.

    The clocks go back on Sunday 1 Nov 2026. A job pinned to 11:00 UTC would
    run at 06:00 ET for four months of the year.
    """
    schedule = AtTime(time(7, 0), days=trading_days)
    friday = schedule.next_run(datetime(2026, 10, 30, 10, 0, tzinfo=UTC))
    assert friday == datetime(2026, 10, 30, 11, 0, tzinfo=UTC)
    monday = schedule.next_run(friday)
    assert monday == datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def test_an_at_time_job_on_trading_days_skips_the_holiday_but_not_the_half_day() -> (
    None
):
    schedule = AtTime(time(7, 0), days=trading_days)
    after_wednesday = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
    assert schedule.next_run(after_wednesday) == datetime(
        2026, 11, 27, 12, 0, tzinfo=UTC
    )


def test_an_at_time_job_on_a_weekday_set_ignores_the_market_calendar() -> None:
    """A Saturday job runs on Saturday; there is never a session to consult."""
    saturday_0900 = AtTime(time(9, 0), days=on_weekdays(5))
    assert saturday_0900.next_run(WED_1500_ET) == datetime(
        2026, 11, 28, 14, 0, tzinfo=UTC
    )
    thursday = AtTime(time(9, 0), days=on_weekdays(3))
    assert thursday.next_run(datetime(2026, 11, 25, 0, 0, tzinfo=UTC)) == (
        datetime(2026, 11, 26, 14, 0, tzinfo=UTC)
    )


def test_an_at_time_job_already_past_today_runs_tomorrow() -> None:
    schedule = AtTime(time(10, 0), days=on_weekdays(0, 1, 2, 3, 4))
    assert schedule.next_run(datetime(2026, 11, 24, 15, 0, tzinfo=UTC)) == (
        datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    )


def test_a_schedule_past_the_published_calendar_has_no_next_run() -> None:
    """``None``, not a guessed 09:30: the calendar has nothing to say."""
    far = datetime(2100, 1, 4, 12, 0, tzinfo=UTC)
    assert EveryWhileOpen(timedelta(minutes=15)).next_run(far) is None
    assert AtTime(time(7, 0), days=trading_days).next_run(far) is None


def test_schedules_refuse_naive_instants_and_nonsense_intervals() -> None:
    with pytest.raises(ValueError):
        EveryWhileOpen(timedelta(minutes=15)).next_run(datetime(2026, 11, 25, 12))
    with pytest.raises(ValueError):
        EveryWhileOpen(timedelta(0))
    with pytest.raises(ValueError):
        AtTime(time(7, 0, tzinfo=UTC), days=trading_days)
    with pytest.raises(ValueError):
        on_weekdays(7)


# --------------------------------------------------------------------------
# The scheduler loop
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_loop_fires_on_the_calendar_across_a_holiday_and_a_half_day() -> (
    None
):
    clock = FakeClock(WED_1500_ET, stop_at=datetime(2026, 11, 27, 19, 0, tzinfo=UTC))
    recorder = Recorder(clock)
    scheduler = Scheduler(
        [
            ScheduledJob(
                name="probe",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=recorder,
                rule="test",
            )
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    await run_until_parked(scheduler, clock)

    wednesday = [WED_1500_ET + timedelta(minutes=15 * k) for k in (1, 2, 3)]
    friday = [HALF_DAY_OPEN + timedelta(minutes=15 * k) for k in range(14)]
    assert recorder.ran == wednesday + friday
    assert not any(moment.date() == THANKSGIVING for moment in recorder.ran)
    assert max(recorder.ran) < HALF_DAY_CLOSE

    status = scheduler.status()["probe"]
    assert status.runs == 17
    assert status.failures == 0
    assert status.last_success == friday[-1]
    assert status.next_run == MONDAY_OPEN


@pytest.mark.asyncio
async def test_a_job_that_overruns_its_slot_skips_rather_than_bursts() -> None:
    """A 40-minute run on a 15-minute schedule resumes on the grid, once.

    The slots it overran (15:30, 15:45) are skipped, not replayed in a burst,
    and the next run is the first slot after it finished -- 16:00, on the
    same grid, rather than 15:55 + 15 minutes drifting off it.
    """
    start = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    clock = FakeClock(start, stop_at=start + timedelta(hours=2))
    ran: list[datetime] = []

    async def slow() -> None:
        ran.append(clock())
        if len(ran) == 1:
            clock.now = clock.now + timedelta(minutes=40)

    scheduler = Scheduler(
        [
            ScheduledJob(
                name="slow",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=slow,
                rule="test",
            )
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    await run_until_parked(scheduler, clock)
    assert ran[0] == start + timedelta(minutes=15)
    assert ran[1] == start + timedelta(minutes=60)
    assert ran[2] == start + timedelta(minutes=75)
    assert len(ran) == len(set(ran))


@pytest.mark.asyncio
async def test_a_failing_job_is_logged_and_the_scheduler_keeps_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(WED_1500_ET, stop_at=datetime(2026, 11, 25, 21, 0, tzinfo=UTC))
    healthy = Recorder(clock)
    attempts: list[datetime] = []

    async def broken() -> None:
        attempts.append(clock())
        raise RuntimeError("vendor said no to token=sekrit-value")

    scheduler = Scheduler(
        [
            ScheduledJob(
                name="broken",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=broken,
                rule="headlines feed the News page; stale when this fails",
                inputs={"host": "finnhub.io", "endpoint": "/news"},
            ),
            ScheduledJob(
                name="healthy",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=healthy,
                rule="test",
            ),
        ],
        secrets=lambda: ("sekrit-value",),
        clock=clock,
        sleep=clock.sleep,
    )
    with caplog.at_level(logging.DEBUG, logger=scheduler_module.__name__):
        await run_until_parked(scheduler, clock)

    assert len(attempts) >= 2  # the next run of the failing job still happened
    assert healthy.ran  # and the other job was never held up by it

    status = scheduler.status()
    assert status["broken"].failures == len(attempts)
    assert status["broken"].last_success is None
    assert status["broken"].last_error_type == "RuntimeError"
    assert status["broken"].failing is True
    assert status["healthy"].failing is False

    failures = [r for r in caplog.records if getattr(r, "event", None) == (
        "context_job_failed"
    )]
    assert len(failures) == len(attempts)
    record = failures[0]
    assert record.levelno == logging.ERROR
    assert getattr(record, "job") == "broken"
    assert getattr(record, "rule") == (
        "headlines feed the News page; stale when this fails"
    )
    assert getattr(record, "inputs") == {"host": "finnhub.io", "endpoint": "/news"}
    assert getattr(record, "error_type") == "RuntimeError"
    failed_at = datetime.fromisoformat(getattr(record, "failed_at"))
    assert failed_at.utcoffset() == timedelta(0)
    assert datetime.fromisoformat(getattr(record, "scheduled_for")).utcoffset() == (
        timedelta(0)
    )
    assert "sekrit-value" not in caplog.text
    assert "<redacted>" in getattr(record, "detail")


class _Unprintable(Exception):
    def __str__(self) -> str:
        raise RuntimeError("__str__ itself is broken")


@pytest.mark.asyncio
async def test_a_failure_whose_message_cannot_be_rendered_is_still_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recording a failure can never itself fail -- or the job's task dies there.

    A task that died inside the failure handler stops that job for the rest
    of the process with nothing to say so, which is precisely the silence
    the handler exists to prevent.
    """
    clock = FakeClock(WED_1500_ET, stop_at=datetime(2026, 11, 25, 21, 0, tzinfo=UTC))
    attempts: list[datetime] = []

    async def unprintable() -> None:
        attempts.append(clock())
        raise _Unprintable()

    scheduler = Scheduler(
        [
            ScheduledJob(
                name="unprintable",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=unprintable,
                rule="test",
            )
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    with caplog.at_level(logging.ERROR, logger=scheduler_module.__name__):
        await run_until_parked(scheduler, clock)

    assert len(attempts) == 3  # 15:15, 15:30, 15:45 ET: the job kept running
    status = scheduler.status()["unprintable"]
    assert status.failures == 3
    assert status.last_error_type == "_Unprintable"
    failures = [r for r in caplog.records if getattr(r, "event", None) == (
        "context_job_failed"
    )]
    assert len(failures) == 3
    assert "_Unprintable" in getattr(failures[0], "detail")
    assert not [r for r in caplog.records if getattr(r, "event", None) == (
        "context_job_task_died"
    )]


@pytest.mark.asyncio
async def test_a_job_task_that_dies_is_logged_when_it_dies_and_aclose_survives_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task ending in a non-cancellation exception never escapes ``aclose``.

    ``aclose`` runs first in the lifespan's shutdown. Re-raised there, it
    would skip the supervisor, the runtime and the Discord sink behind it,
    and queued rule 9 alerts would get no grace and no ``dropped`` row.
    The death is logged at the moment it happens, not only at shutdown.
    """

    async def body() -> None:
        return None

    async def broken_sleep(seconds: float) -> None:
        raise RuntimeError("the sleeper broke")

    scheduler = Scheduler(
        [
            ScheduledJob(
                name="doomed",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=body,
                rule="test",
            )
        ],
        secrets=no_secrets,
        clock=lambda: WED_1500_ET,
        sleep=broken_sleep,
    )
    with caplog.at_level(logging.ERROR, logger=scheduler_module.__name__):
        scheduler.start()
        await asyncio.wait_for(scheduler.ready(), timeout=10)
        for _ in range(50):
            await asyncio.sleep(0)
        died = [r for r in caplog.records if getattr(r, "event", None) == (
            "context_job_task_died"
        )]
        assert len(died) == 1
        assert getattr(died[0], "job") == "doomed"
        assert getattr(died[0], "error_type") == "RuntimeError"
        await scheduler.aclose()  # does not raise
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_the_calendar_is_built_off_the_loop_once_before_any_schedule_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``exchange_calendars`` costs ~0.5s on first use; the loop must not pay it.

    Jobs share the event loop with rule 9's producers -- the sockets and the
    watchdog -- so a half-second stall there is a half-second of unread
    socket frames. The build runs once, on a worker thread, and every
    schedule is asked only after it finished.
    """
    events: list[str] = []
    loop_thread = threading.current_thread()
    original = scheduler_module._warm_calendar_blocking

    def recording_warm(moment: datetime) -> None:
        assert threading.current_thread() is not loop_thread
        events.append("warm")
        original(moment)

    monkeypatch.setattr(scheduler_module, "_warm_calendar_blocking", recording_warm)

    class RecordingSchedule:
        def next_run(self, after: datetime) -> datetime | None:
            events.append("next_run")
            return None

        def describe(self) -> str:
            return "test"

    async def body() -> None:
        return None

    scheduler = Scheduler(
        [
            ScheduledJob(name=f"job{i}", schedule=RecordingSchedule(), run=body,
                         rule="test")
            for i in range(3)
        ],
        secrets=no_secrets,
        clock=lambda: WED_1500_ET,
    )
    scheduler.start()
    await asyncio.wait_for(scheduler.ready(), timeout=10)
    for _ in range(50):
        await asyncio.sleep(0)
    await scheduler.aclose()
    assert events == ["warm", "next_run", "next_run", "next_run"]


def test_two_jobs_cannot_share_a_name() -> None:
    async def body() -> None:
        return None

    job = ScheduledJob(
        name="dup", schedule=EveryWhileOpen(timedelta(minutes=1)), run=body, rule="x"
    )
    with pytest.raises(ValueError):
        Scheduler([job, job], secrets=no_secrets)


def test_a_status_is_a_snapshot_not_a_live_view() -> None:
    assert JobStatus.__dataclass_params__.frozen  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_aclose_is_safe_before_and_after_start() -> None:
    scheduler = Scheduler([], secrets=no_secrets)
    await scheduler.aclose()
    scheduler.start()
    scheduler.start()
    await scheduler.aclose()
    await scheduler.aclose()


# --------------------------------------------------------------------------
# The shipped job set
# --------------------------------------------------------------------------


_SHIPPED_NAMES = [
    "calendar_probe",
    "fred_dgs3mo",
    "news_watch_tier",
    "news_alpaca",
    "news_finnhub_market",
    "news_massive",
    "asset_directory",
    "tradeability_cache",
    "news_prune",
]


def test_the_shipped_job_set_is_the_probe_fred_and_the_news_jobs() -> None:
    """Step 2 shipped the no-op; step 3 FRED ``DGS3MO``; step 4 the news jobs.

    Every job ships whether or not its vendor is configured -- without one
    its body returns ``JobSkipped`` -- so the isolation tests below always
    run all of them. Each carries a rule and the host it calls.
    """
    jobs = context_jobs(ContextServices(session_factory=lambda: None))  # type: ignore[arg-type, return-value]
    assert [job.name for job in jobs] == _SHIPPED_NAMES
    by_name = {job.name: job for job in jobs}
    probe, fred = by_name["calendar_probe"], by_name["fred_dgs3mo"]
    assert isinstance(probe.schedule, EveryWhileOpen)
    assert probe.catch_up is None
    assert fred.schedule == AtTime(time(10, 0), trading_days)
    assert fred.catch_up is not None
    assert fred.inputs["host"] == FRED_HOST
    assert fred.inputs["series_id"] == "DGS3MO"

    assert isinstance(by_name["news_watch_tier"].schedule, WatchTierCadence)
    assert by_name["news_alpaca"].schedule == TwoRate(
        in_session=timedelta(seconds=60), otherwise=timedelta(minutes=5)
    )
    assert by_name["news_finnhub_market"].schedule == EveryInterval(timedelta(minutes=5))
    assert by_name["news_massive"].schedule == EveryInterval(timedelta(minutes=15))
    assert by_name["asset_directory"].schedule == AtTime(time(7, 30), every_day)
    assert by_name["tradeability_cache"].schedule == EveryInterval(timedelta(minutes=15))
    assert by_name["news_prune"].schedule == AtTime(time(3, 0), every_day)
    # The spec's *Feeds and budgets* figures, as the named constants.
    assert (ALPACA_NEWS_IN_SESSION, ALPACA_NEWS_OTHERWISE) == (
        timedelta(seconds=60),
        timedelta(minutes=5),
    )
    assert FINNHUB_MARKET_NEWS_EVERY == timedelta(minutes=5)
    assert MASSIVE_NEWS_EVERY == TRADEABILITY_EVERY == timedelta(minutes=15)
    assert (ASSET_DIRECTORY_AT, NEWS_PRUNE_AT) == (time(7, 30), time(3, 0))

    # Two news jobs catch up at start: the asset directory, because the
    # routes 503 until it is held, and the watch tier, whose cadence is
    # unknown until it has polled once (W taken as 1 would put its first
    # request up to an hour after a restart). Every other waits for its slot.
    assert by_name["asset_directory"].catch_up is not None
    assert by_name["news_watch_tier"].catch_up is not None
    for name in _SHIPPED_NAMES[2:]:
        if name not in ("asset_directory", "news_watch_tier"):
            assert by_name[name].catch_up is None, name

    hosts = {name: by_name[name].inputs.get("host") for name in _SHIPPED_NAMES[2:]}
    assert hosts == {
        "news_watch_tier": FINNHUB_HOST,
        "news_alpaca": ALPACA_DATA_HOST,
        "news_finnhub_market": FINNHUB_HOST,
        "news_massive": MASSIVE_HOST,
        "asset_directory": ALPACA_PAPER_TRADING_HOST,
        "tradeability_cache": ALPACA_DATA_HOST,
        "news_prune": "local",
    }
    for job in jobs:
        assert job.rule.strip(), job.name


# --------------------------------------------------------------------------
# The news schedules (Phase 3 step 4) -- pure arithmetic on the calendar
# --------------------------------------------------------------------------

#: Wednesday 25 Nov 2026: a full session, 09:30-16:00 ET = 14:30-21:00 UTC.
WED_OPEN = datetime(2026, 11, 25, 14, 30, tzinfo=UTC)
WED_CLOSE = datetime(2026, 11, 25, 21, 0, tzinfo=UTC)


def _et(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    from corollary.calendars import NYSE_TZ

    return datetime(y, mo, d, h, mi, s, tzinfo=NYSE_TZ).astimezone(UTC)


def test_every_interval_is_a_fixed_utc_grid_whatever_the_calendar() -> None:
    schedule = EveryInterval(timedelta(minutes=5))
    # On the grid, and strictly after: a slot asked from itself is the next.
    assert schedule.next_run(datetime(2026, 11, 25, 15, 7, 13, tzinfo=UTC)) == datetime(
        2026, 11, 25, 15, 10, tzinfo=UTC
    )
    assert schedule.next_run(datetime(2026, 11, 25, 15, 10, tzinfo=UTC)) == datetime(
        2026, 11, 25, 15, 15, tzinfo=UTC
    )
    # A restart anywhere inside a slot lands back on the same grid.
    for second in (0, 1, 150, 299):
        asked = datetime(2026, 11, 25, 15, 5, tzinfo=UTC) + timedelta(seconds=second)
        assert schedule.next_run(asked) == datetime(2026, 11, 25, 15, 10, tzinfo=UTC)
    # A holiday, a weekend night: the calendar does not enter into it.
    assert schedule.next_run(_et(2026, 11, 26, 10, 1)) == _et(2026, 11, 26, 10, 5)
    assert schedule.next_run(_et(2026, 11, 28, 23, 58)) == _et(2026, 11, 29, 0, 0)
    # A fifteen-minute grid is on the quarter hour in ET too.
    fifteen = EveryInterval(timedelta(minutes=15))
    assert fifteen.next_run(_et(2026, 11, 25, 10, 1)) == _et(2026, 11, 25, 10, 15)
    assert "every 0:05:00" in schedule.describe()


def test_every_interval_refuses_nonsense() -> None:
    with pytest.raises(ValueError):
        EveryInterval(timedelta(0))
    with pytest.raises(ValueError):
        EveryInterval(timedelta(minutes=5)).next_run(datetime(2026, 11, 25, 15, 0))


def test_two_rate_steps_by_the_minute_in_session_and_five_otherwise() -> None:
    schedule = TwoRate(in_session=timedelta(seconds=60), otherwise=timedelta(minutes=5))
    # In session: the 60 s grid anchored at the open.
    assert schedule.next_run(_et(2026, 11, 25, 10, 0)) == _et(2026, 11, 25, 10, 1)
    assert schedule.next_run(_et(2026, 11, 25, 10, 0, 30)) == _et(2026, 11, 25, 10, 1)
    # Pre-market: five minutes, until the open itself, which is in session.
    assert schedule.next_run(_et(2026, 11, 25, 9, 21)) == _et(2026, 11, 25, 9, 25)
    assert schedule.next_run(_et(2026, 11, 25, 9, 28, 30)) == WED_OPEN
    # Overnight, across midnight ET.
    assert schedule.next_run(_et(2026, 11, 25, 23, 58)) == _et(2026, 11, 26, 0, 0)
    # A full day's last in-session slot, then the close on the slow grid.
    assert schedule.next_run(_et(2026, 11, 25, 15, 58, 30)) == _et(2026, 11, 25, 15, 59)
    assert schedule.next_run(_et(2026, 11, 25, 15, 59)) == WED_CLOSE
    assert schedule.next_run(WED_CLOSE) == _et(2026, 11, 25, 16, 5)


def test_two_rate_follows_a_holiday_and_a_half_days_early_close() -> None:
    schedule = TwoRate(in_session=timedelta(seconds=60), otherwise=timedelta(minutes=5))
    # Thanksgiving: no session, so five minutes through the day.
    assert schedule.next_run(_et(2026, 11, 26, 10, 0)) == _et(2026, 11, 26, 10, 5)
    assert schedule.next_run(_et(2026, 11, 26, 12, 59)) == _et(2026, 11, 26, 13, 0)
    # The half-day: every minute to 12:59, then 13:00 is closed -- slow grid.
    assert schedule.next_run(_et(2026, 11, 27, 12, 58)) == _et(2026, 11, 27, 12, 59)
    assert schedule.next_run(_et(2026, 11, 27, 12, 59)) == HALF_DAY_CLOSE
    assert schedule.next_run(HALF_DAY_CLOSE) == _et(2026, 11, 27, 13, 5)
    # Not the 16:00 a hardcoded session would have kept polling to.
    assert schedule.next_run(_et(2026, 11, 27, 14, 0)) == _et(2026, 11, 27, 14, 5)
    # The weekend, then Monday's open on the fast grid.
    assert schedule.next_run(_et(2026, 11, 30, 9, 28)) == MONDAY_OPEN
    assert schedule.next_run(MONDAY_OPEN) == _et(2026, 11, 30, 9, 31)
    assert "60" in schedule.describe() or "0:01:00" in schedule.describe()


def test_two_rate_never_fires_its_slow_grid_inside_a_session() -> None:
    """The slow grid's in-session slots are removed, not merely outpaced.

    At the shipped 60 s / 5 min every slow slot inside a session is also a
    fast slot, so dropping them is invisible there. On grids that do not nest
    -- 7 min in session, 5 min otherwise -- a slow slot at 10:00 ET falls
    between the fast 09:58 and 10:05, and firing it would be a request the
    in-session rate never budgeted.
    """
    schedule = TwoRate(in_session=timedelta(minutes=7), otherwise=timedelta(minutes=5))
    assert schedule.next_run(_et(2026, 11, 25, 9, 58)) == _et(2026, 11, 25, 10, 5)
    # Outside the session the slow grid is all there is.
    assert schedule.next_run(_et(2026, 11, 25, 16, 1)) == _et(2026, 11, 25, 16, 5)


def test_two_rate_refuses_nonsense() -> None:
    with pytest.raises(ValueError):
        TwoRate(in_session=timedelta(0), otherwise=timedelta(minutes=5))
    with pytest.raises(ValueError):
        TwoRate(in_session=timedelta(seconds=60), otherwise=timedelta(seconds=-1))


def test_the_watch_cadence_is_watch_interval_at_the_polled_universe_size() -> None:
    sizes = iter([66, 66, 0, 66, 66, 10, 10])
    schedule = WatchTierCadence(universe_size=lambda: next(sizes))
    in_window = _et(2026, 11, 25, 10, 0)
    assert schedule.next_run(in_window) == in_window + timedelta(minutes=15) / 66
    assert schedule.next_run(in_window) == in_window + watch_interval(66, in_window)
    # Nothing polled yet (or an empty universe): W is taken as 1.
    assert schedule.next_run(in_window) == in_window + timedelta(minutes=15)
    # Outside the window -- 05:59 ET, and a holiday at noon -- it is hourly.
    early = _et(2026, 11, 25, 5, 59)
    assert schedule.next_run(early) == early + timedelta(hours=1) / 66
    holiday = _et(2026, 11, 26, 12, 0)
    assert schedule.next_run(holiday) == holiday + timedelta(hours=1) / 66
    # The half-day's window closes an hour after the calendar's 13:00 close.
    last = _et(2026, 11, 27, 13, 59)
    assert schedule.next_run(last) == last + timedelta(minutes=15) / 10
    past = _et(2026, 11, 27, 14, 0)
    assert schedule.next_run(past) == past + timedelta(hours=1) / 10
    with pytest.raises(ValueError):
        schedule.next_run(datetime(2026, 11, 25, 15, 0))
    assert "watch" in schedule.describe()


def test_the_prune_runs_at_0300_et_every_day_including_both_dst_changes() -> None:
    """03:00 ET exists exactly once on both change days, which 02:xx does not."""
    prune = AtTime(time(3, 0), every_day)
    # 8 Mar 2026: 02:00 EST jumps to 03:00 EDT -- 03:00 is 07:00 UTC.
    assert prune.next_run(datetime(2026, 3, 8, 5, 0, tzinfo=UTC)) == datetime(
        2026, 3, 8, 7, 0, tzinfo=UTC
    )
    # 1 Nov 2026: 01:00-02:00 happens twice; 03:00 is once, EST, 08:00 UTC.
    assert prune.next_run(datetime(2026, 11, 1, 4, 0, tzinfo=UTC)) == datetime(
        2026, 11, 1, 8, 0, tzinfo=UTC
    )
    # A holiday and a Saturday are days too.
    assert prune.next_run(_et(2026, 11, 26, 0, 0)) == _et(2026, 11, 26, 3, 0)
    assert prune.next_run(_et(2026, 11, 28, 4, 0)) == _et(2026, 11, 29, 3, 0)
    assert prune.describe() == "at 03:00 ET on every_day"


def test_the_asset_directory_refreshes_at_0730_et_every_day() -> None:
    """Every day, not trading days: ``MAX_DIRECTORY_AGE`` is 26 hours.

    Refreshed on trading days only, the directory would be stale from
    Saturday 09:30 ET to Monday 07:30 -- and the tradeability job warns about
    a stale directory on every one of its fifteen-minute runs.
    """
    schedule = AtTime(time(7, 30), every_day)
    assert schedule.next_run(_et(2026, 11, 26, 7, 0)) == _et(2026, 11, 26, 7, 30)
    assert schedule.next_run(_et(2026, 11, 28, 7, 30)) == _et(2026, 11, 29, 7, 30)


# --------------------------------------------------------------------------
# The news job bodies (Phase 3 step 4) -- fakes for every vendor
# --------------------------------------------------------------------------


@dataclasses.dataclass
class FakeAlpacaContext(FakeAlpaca):
    """Alpaca as the context jobs see it: news, the asset list, tradeability."""

    directory: AssetDirectory = DIRECTORY
    directory_calls: int = 0

    async def active_equities(self) -> AssetDirectory:
        self.directory_calls += 1
        return self.directory

    async def has_standard_root(self, ticker: str) -> bool:
        return True

    async def adv_daily_bars(
        self, symbols: object, *, session_date: date
    ) -> dict[str, list[object]]:
        return {}


class FailingAlpacaNews(FakeAlpacaContext):
    """Every other Alpaca call answers; ``/v1beta1/news`` cannot be reached."""

    async def news(  # type: ignore[override]
        self, *, start: datetime, end: datetime | None = None, max_pages: int = 5
    ) -> object:
        self.calls.append(start)
        raise _alpaca_news_failure()


class FakeFinnhubNews:
    """Finnhub as the context jobs see it: company news, market news, IPO dates."""

    def __init__(self) -> None:
        self.company = FakeCompanyNews()
        self.market = FakeMarketNews()
        self.ipo_calls: list[str] = []

    async def company_news(self, symbol: str, from_date: date, to_date: date) -> object:
        return await self.company.company_news(symbol, from_date, to_date)

    async def market_news(self, min_id: int | None) -> object:
        return await self.market.market_news(min_id)

    async def ipo_date(self, symbol: str) -> date | None:
        self.ipo_calls.append(symbol)
        return None


def _held_positions() -> HeldPositionUnderlyings:
    """Production's job-facing positions reader, over a holder that holds TSLA.

    The real :class:`HeldPositionUnderlyings`, not a test coroutine, so every
    job test here -- the rule 9 isolation test included -- runs the reader
    the lifespan hands the jobs.
    """
    holder = PositionUnderlyings()
    holder.replace({"TSLA"}, at=datetime(2026, 11, 25, 15, 0, tzinfo=UTC))
    return HeldPositionUnderlyings(holder)


def _news_services(
    db_engine: Engine,
    *,
    alpaca: object | None = None,
    finnhub: object | None = None,
    massive: object | None = None,
    **extra: object,
) -> ContextServices:
    holder = AssetDirectoryHolder()
    sessions = lambda: Session(db_engine)  # noqa: E731
    return ContextServices(
        session_factory=sessions,
        assets=holder,
        news_store=NewsStore(session_factory=sessions, assets=holder),
        alpaca=alpaca,  # type: ignore[arg-type]
        finnhub=finnhub,  # type: ignore[arg-type]
        massive=massive,  # type: ignore[arg-type]
        markets=("AAPL",),
        position_underlyings=_held_positions(),
        seed_loader=lambda: None,
        **extra,  # type: ignore[arg-type]
    )


def _jobs_by_name(services: ContextServices, clock: Callable[[], datetime]) -> dict[str, ScheduledJob]:
    return {job.name: job for job in context_jobs(services, clock=clock)}


@pytest.mark.asyncio
async def test_with_no_news_vendor_every_news_job_skips_and_none_succeeds(
    db_engine: Engine,
) -> None:
    """An unconfigured provider is a skip with a reason: never a failure, never a success."""
    jobs = _jobs_by_name(_news_services(db_engine), lambda: _et(2026, 11, 25, 10, 0))
    for name in (
        "news_watch_tier",
        "news_alpaca",
        "news_finnhub_market",
        "news_massive",
        "asset_directory",
        "tradeability_cache",
    ):
        outcome = await jobs[name].run()
        assert isinstance(outcome, JobSkipped), name
        assert outcome.reason.strip(), name
    catch_up = jobs["asset_directory"].catch_up
    assert catch_up is not None
    assert isinstance(await catch_up(), JobSkipped)
    # The prune is local: it has no vendor to be missing, and it runs.
    assert await jobs["news_prune"].run() is None


@pytest.mark.asyncio
async def test_the_asset_directory_catches_up_only_while_the_holder_is_empty(
    db_engine: Engine,
) -> None:
    """The routes 503 until the directory is held, so a start fills it -- once."""
    alpaca = FakeAlpacaContext()
    services = _news_services(db_engine, alpaca=alpaca)
    job = _jobs_by_name(services, lambda: _et(2026, 11, 25, 10, 0))["asset_directory"]
    assert job.catch_up is not None
    assert await job.catch_up() is None
    assert services.assets.current() is not None
    assert alpaca.directory_calls == 1
    skipped = await job.catch_up()
    assert isinstance(skipped, JobSkipped)
    assert alpaca.directory_calls == 1
    # The daily slot always refreshes, held or not.
    assert await job.run() is None
    assert alpaca.directory_calls == 2


@pytest.mark.asyncio
async def test_the_watch_tier_polls_position_underlyings_and_paces_on_the_universe(
    db_engine: Engine,
) -> None:
    """Positions come from the injected callable; W is what the poller last saw."""
    finnhub = FakeFinnhubNews()
    services = _news_services(db_engine, finnhub=finnhub)
    now = _et(2026, 11, 25, 10, 0)
    job = _jobs_by_name(services, lambda: now)["news_watch_tier"]
    # Before any poll W is unknown and taken as 1.
    assert job.schedule.next_run(now) == now + timedelta(minutes=15)
    assert await job.run() is None
    assert await job.run() is None
    assert [call[0] for call in finnhub.company.calls] == ["AAPL", "TSLA"]
    # Two polled symbols: one request every 450 s in the window.
    assert job.schedule.next_run(now) == now + timedelta(minutes=15) / 2


@pytest.mark.asyncio
async def test_the_watch_tier_learns_its_universe_size_at_start(db_engine: Engine) -> None:
    """A restart must not leave the first watch request a whole cycle away.

    Before any poll W is unknown and taken as 1 -- up to an hour overnight.
    The start-up catch-up makes one request straight away, so the first slot
    is already paced on the real W.
    """
    finnhub = FakeFinnhubNews()
    services = _news_services(db_engine, finnhub=finnhub)
    overnight = _et(2026, 11, 25, 23, 0)
    job = _jobs_by_name(services, lambda: overnight)["news_watch_tier"]
    assert job.schedule.next_run(overnight) == overnight + timedelta(hours=1)
    assert job.catch_up is not None
    assert await job.catch_up() is None
    assert [call[0] for call in finnhub.company.calls] == ["AAPL"]
    # W = 2 (AAPL from Markets, TSLA held): the next request is 30 min away.
    assert job.schedule.next_run(overnight) == overnight + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_the_watch_tier_asks_at_start_under_the_scheduler(db_engine: Engine) -> None:
    """End to end through :class:`Scheduler`: the first Finnhub call is at start."""
    finnhub = FakeFinnhubNews()
    services = _news_services(db_engine, finnhub=finnhub)
    start = _et(2026, 11, 25, 23, 0)
    clock = FakeClock(start, stop_at=start + timedelta(minutes=1), wait_for_threads=True)
    watch = _jobs_by_name(services, clock)["news_watch_tier"]
    scheduler = Scheduler([watch], secrets=no_secrets, clock=clock, sleep=clock.sleep)
    await run_until_parked(scheduler, clock)
    assert len(finnhub.company.calls) == 1
    assert scheduler.status()["news_watch_tier"].runs == 1
    assert scheduler.status()["news_watch_tier"].next_run == start + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_the_discovery_jobs_poll_their_vendor_and_store(db_engine: Engine) -> None:
    alpaca, finnhub, massive = FakeAlpacaContext(), FakeFinnhubNews(), FakeMassive()
    services = _news_services(db_engine, alpaca=alpaca, finnhub=finnhub, massive=massive)
    jobs = _jobs_by_name(services, lambda: _et(2026, 11, 25, 10, 0))
    assert await jobs["news_alpaca"].run() is None
    assert await jobs["news_finnhub_market"].run() is None
    assert await jobs["news_massive"].run() is None
    assert len(alpaca.calls) == 1
    assert finnhub.market.calls == [None]
    assert len(massive.calls) == 1


@pytest.mark.asyncio
async def test_tradeability_is_handed_finnhub_as_its_ipo_date_source(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision Q12: without it every partial-window ticker fails closed."""
    seen: dict[str, object] = {}

    async def spy(**kwargs: object) -> object:
        seen.update(kwargs)
        from corollary.data.news.pollers import PollSkipped

        return PollSkipped("spied")

    monkeypatch.setattr(scheduler_module, "refresh_tradeability_cache", spy)
    alpaca, finnhub = FakeAlpacaContext(), FakeFinnhubNews()
    services = _news_services(db_engine, alpaca=alpaca, finnhub=finnhub)
    now = _et(2026, 11, 25, 10, 0)
    outcome = await _jobs_by_name(services, lambda: now)["tradeability_cache"].run()
    assert outcome == JobSkipped("spied")
    assert seen["ipo_dates"] is finnhub
    assert seen["provider"] is alpaca
    assert seen["holder"] is services.assets
    assert seen["now"] == now


def test_a_news_store_on_another_asset_holder_is_refused(db_engine: Engine) -> None:
    """Two holders would filter ingested tags against a directory nobody refreshes."""
    sessions = lambda: Session(db_engine)  # noqa: E731
    with pytest.raises(ValueError, match="asset"):
        ContextServices(
            session_factory=sessions,
            assets=AssetDirectoryHolder(),
            news_store=NewsStore(session_factory=sessions, assets=AssetDirectoryHolder()),
        )


# --------------------------------------------------------------------------
# Catch-up at start
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_catch_up_runs_once_at_start_before_the_first_slot() -> None:
    clock = FakeClock(WED_1500_ET, stop_at=datetime(2026, 11, 25, 21, 0, tzinfo=UTC))
    body = Recorder(clock)
    caught_up = Recorder(clock)
    scheduler = Scheduler(
        [
            ScheduledJob(
                name="with_catch_up",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=body,
                rule="test",
                catch_up=caught_up,
            )
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    await run_until_parked(scheduler, clock)
    assert caught_up.ran == [WED_1500_ET]
    assert body.ran and body.ran[0] == WED_1500_ET + timedelta(minutes=15)
    status = scheduler.status()["with_catch_up"]
    assert status.runs == 1 + len(body.ran)
    assert status.failures == 0


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_failing_catch_up_is_logged_and_the_schedule_still_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(WED_1500_ET, stop_at=datetime(2026, 11, 25, 21, 0, tzinfo=UTC))
    body = Recorder(clock)

    async def broken_catch_up() -> None:
        raise RuntimeError("FRED said no to api_key=sekrit-value")

    scheduler = Scheduler(
        [
            ScheduledJob(
                name="fred_like",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=body,
                rule="the risk-free rate; stale when this fails",
                inputs={"host": FRED_HOST},
                catch_up=broken_catch_up,
            )
        ],
        secrets=lambda: ("sekrit-value",),
        clock=clock,
        sleep=clock.sleep,
    )
    with caplog.at_level(logging.DEBUG, logger=scheduler_module.__name__):
        await run_until_parked(scheduler, clock)

    assert body.ran  # the schedule was not abandoned
    status = scheduler.status()["fred_like"]
    assert status.failures == 1
    assert status.last_error_type == "RuntimeError"
    [failure] = [
        r for r in caplog.records if getattr(r, "event", None) == "context_job_failed"
    ]
    assert getattr(failure, "phase") == "catch_up"
    assert getattr(failure, "scheduled_for") is None
    assert "sekrit-value" not in caplog.text


# --------------------------------------------------------------------------
# A run that fetched nothing is skipped, never a success
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_skipped_run_is_recorded_as_skipped_and_never_as_a_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``last_success`` is when the data was last known good; a skip is not that.

    Recorded as a success, a feed with no key -- or a catch-up that correctly
    did nothing -- would read as refreshed at every slot, and a *stale since*
    display would call it fresh when nothing was ever fetched.
    """
    clock = FakeClock(WED_1500_ET, stop_at=datetime(2026, 11, 25, 21, 0, tzinfo=UTC))
    ran: list[datetime] = []

    async def body() -> JobSkipped:
        ran.append(clock())
        return JobSkipped("nothing configured to fetch from")

    async def catch_up() -> JobSkipped:
        return JobSkipped("the stored rows are current")

    scheduler = Scheduler(
        [
            ScheduledJob(
                name="skips",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=body,
                rule="test",
                catch_up=catch_up,
            )
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    with caplog.at_level(logging.INFO, logger=scheduler_module.__name__):
        await run_until_parked(scheduler, clock)

    status = scheduler.status()["skips"]
    assert ran, "the body never ran; widen the window"
    assert status.last_success is None
    assert status.runs == 0
    assert status.failures == 0
    assert not status.failing
    assert status.skips == 1 + len(ran)
    assert status.last_skipped == ran[-1]
    assert status.last_skip_reason == "nothing configured to fetch from"
    skipped = [
        r for r in caplog.records if getattr(r, "event", None) == "context_job_skipped"
    ]
    assert [getattr(r, "phase") for r in skipped] == ["catch_up"] + ["scheduled"] * len(ran)
    assert getattr(skipped[0], "reason") == "the stored rows are current"


@pytest.mark.asyncio
async def test_without_a_fred_key_the_shipped_fred_job_never_reports_a_success(
    db_engine: Engine,
) -> None:
    """Case A: ``FRED_API_KEY`` unset. Catch-up and the 10:00 ET run both skip."""
    services = ContextServices(session_factory=lambda: Session(db_engine), fred=None)
    clock = FakeClock(WED_1500_ET, stop_at=HALF_DAY_CLOSE)
    scheduler = build_context_scheduler(
        services, no_secrets, clock=clock, sleep=clock.sleep
    )
    await run_until_parked(scheduler, clock)

    status = scheduler.status()["fred_dgs3mo"]
    assert status.last_success is None
    assert status.runs == 0
    assert status.failures == 0
    # Start-up, then Friday 10:00 ET (Thanksgiving has no slot).
    assert status.skips == 2
    assert status.last_skipped == datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    assert status.last_skip_reason is not None
    assert "FRED_API_KEY" in status.last_skip_reason


class _CountingFred:
    def __init__(self) -> None:
        self.calls = 0

    async def observations(
        self, series_id: str, *, limit: int = 10
    ) -> list[FredObservation]:
        self.calls += 1
        return []


@pytest.mark.asyncio
async def test_a_catch_up_on_a_current_table_is_skipped_not_counted_a_success(
    db_engine: Engine,
) -> None:
    """Case B: the table already holds a current row, so boot fetches nothing.

    The catch-up reads the scheduler's own clock, so "current" is judged
    against Wed 25 Nov here: two sessions back is Mon 23 Nov, and the stored
    Tue 24 Nov row is newer.
    """
    with Session(db_engine) as session:
        store_observations(
            session,
            [FredObservation("DGS3MO", date(2026, 11, 24), value=None)],
            fetched_at=WED_1500_ET,
        )
        store_observations(
            session,
            [FredObservation("DGS3MO", date(2026, 11, 23), value=Decimal("3.91"))],
            fetched_at=WED_1500_ET,
        )
        session.commit()
    fred = _CountingFred()
    services = ContextServices(session_factory=lambda: Session(db_engine), fred=fred)
    clock = FakeClock(
        WED_1500_ET,
        stop_at=datetime(2026, 11, 25, 21, 0, tzinfo=UTC),
        wait_for_threads=True,
    )
    scheduler = build_context_scheduler(
        services, no_secrets, clock=clock, sleep=clock.sleep
    )
    await run_until_parked(scheduler, clock)

    assert fred.calls == 0
    status = scheduler.status()["fred_dgs3mo"]
    assert status.last_success is None
    assert status.runs == 0
    assert status.skips == 1
    assert status.last_skip_reason is not None
    assert "2026-11-24" in status.last_skip_reason


# --------------------------------------------------------------------------
# Rule 9 isolation
# --------------------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "scheduler.db"))

    # Test-only durability trade. The shipped news jobs commit once per poll
    # -- the isolation run below drives ~1,000 of them across a holiday
    # weekend -- and a WAL fsync per commit on Windows put each run at ~7 s,
    # inside ``run_until_parked``'s 10 s timeout. A throwaway database needs
    # no fsync; nothing asserted here depends on one.
    @event.listens_for(engine, "connect")
    def _no_fsync(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA synchronous=OFF")
        finally:
            cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
    yield engine
    engine.dispose()


class SpyNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def emit(self, notification: Notification) -> None:
        self.sent.append(notification)


#: Every EngineRuntime method that feeds or moves rule 9's switch.
_WATCHDOG_INPUTS = (
    "record_message",
    "record_poll",
    "record_heartbeat",
    "record_stream_open",
    "record_stream_closed",
    "expect_feed",
    "stop_expecting_feed",
    "expect_handshake",
    "stop_expecting_handshake",
    "record_opening_snapshot",
    "check_watchdog",
    "halt",
)


def _state_snapshot(engine: Engine) -> tuple[bool, str | None, datetime | None]:
    """Everything rule 9's halt consists of: whether, why, and since when."""
    state = read_state(engine)
    return (state.halted, state.halted_reason, state.halted_at)


class _WatchedRuntime:
    """A real :class:`EngineRuntime` on the real ``engine_state`` row, spied on.

    Every watchdog input is wrapped to record its name before delegating, so
    a context job that reached one -- by any route -- shows up in
    :attr:`touched`.
    """

    def __init__(
        self, db_engine: Engine, monkeypatch: pytest.MonkeyPatch, *, halted: bool
    ) -> None:
        self.notifier = SpyNotifier()
        self.runtime = EngineRuntime(
            session_factory=lambda: Session(db_engine), notifier=self.notifier
        )
        self.runtime.start()
        if not halted:
            resume_engine(db_engine)
        assert read_state(db_engine).halted is halted
        self.activity_before = self.runtime.watchdog.last_activity_at
        self.armed_before = self.runtime.watchdog.heartbeat_armed
        self.touched: list[str] = []
        for name in _WATCHDOG_INPUTS:
            original = getattr(self.runtime, name)

            def spy(*args: object, _name: str = name, _orig: object = original,
                    **kwargs: object) -> object:
                self.touched.append(_name)
                return _orig(*args, **kwargs)  # type: ignore[operator]

            monkeypatch.setattr(self.runtime, name, spy)

    def assert_untouched(self) -> None:
        assert self.touched == []
        assert self.notifier.sent == []
        assert self.runtime.watchdog.last_activity_at == self.activity_before
        assert self.runtime.watchdog.heartbeat_armed is self.armed_before


@asynccontextmanager
async def _recorded_fred() -> AsyncIterator[FredProvider]:
    """A FRED provider that serves the recorded DGS3MO response and nothing else.

    The provider does not close a client it was handed, so this closes both.
    """
    body = fixture_body_text("p3_observations_dgs3mo").encode("utf-8")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == FRED_HOST, request.url.host
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = FredProvider(
            credentials=FredCredentials(api_key=FAKE_KEY),
            client=client,
            limiter=HostRateLimiter(per_host={}),
        )
        try:
            yield provider
        finally:
            await provider.aclose()


def _alpaca_news_failure() -> httpx.ConnectError:
    request = httpx.Request("GET", "https://data.alpaca.markets/v1beta1/news")
    return httpx.ConnectError("connection refused", request=request)


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("halted_before", [False, True])
async def test_a_failing_context_job_never_halts_and_never_feeds_the_watchdog(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, halted_before: bool
) -> None:
    """Decision 1: a news vendor down is not an Alpaca connection loss.

    The scheduler's own failure path, over hand-built jobs: one stands in for
    an Alpaca news failure -- an httpx error from ``data.alpaca.markets``,
    the same vendor the watchdog judges -- one for any other exception, and a
    healthy one proves the loop kept going. In both halt states: resumed (a
    halt would flip it) and halted (a resume would). The shipped job set has
    its own test below.
    """
    watched = _WatchedRuntime(db_engine, monkeypatch, halted=halted_before)
    state_before = _state_snapshot(db_engine)

    async def alpaca_news() -> None:
        raise _alpaca_news_failure()

    async def anything_else() -> None:
        raise ValueError("a parser choked")

    clock = FakeClock(WED_1500_ET, stop_at=datetime(2026, 11, 27, 16, 0, tzinfo=UTC))
    healthy = Recorder(clock)
    scheduler = Scheduler(
        [
            ScheduledJob(
                name="alpaca_news",
                schedule=EveryWhileOpen(timedelta(minutes=1)),
                run=alpaca_news,
                rule="Benzinga headlines",
                inputs={"host": "data.alpaca.markets"},
            ),
            ScheduledJob(
                name="other",
                schedule=AtTime(time(7, 0), days=trading_days),
                run=anything_else,
                rule="test",
            ),
            ScheduledJob(
                name="healthy",
                schedule=EveryWhileOpen(timedelta(minutes=15)),
                run=healthy,
                rule="test",
            ),
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    await run_until_parked(scheduler, clock)

    status = scheduler.status()
    assert status["alpaca_news"].failures >= 2
    assert status["alpaca_news"].last_error_type == "ConnectError"
    assert status["other"].failures >= 1
    assert status["healthy"].runs >= 2

    assert _state_snapshot(db_engine) == state_before
    watched.assert_untouched()


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["as_shipped", "each_raises", "alpaca_news_fails"])
@pytest.mark.parametrize("halted_before", [False, True])
async def test_the_shipped_context_jobs_never_move_the_halt_state(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    halted_before: bool,
    mode: str,
) -> None:
    """The job set ``context_jobs`` actually returns, on a real writable database.

    This is the test that grows with the job set: every job a later step adds
    runs here, handed the same :class:`ContextServices` the lifespan builds,
    whose ``session_factory`` is a *writable* session on the ``engine_state``
    row. A job that resumed through ``routes.engine.resume(session)``, or set
    ``engine_state(session).halted``, changes the snapshot and fails here --
    in the halted parametrisation for a resume, the resumed one for a halt.

    Three modes, because each catches what the others cannot:

    * ``as_shipped`` runs every job exactly as shipped. A job that
      *succeeds* by resuming the engine never raises, so a forced-failure
      run alone would never exercise the line that does it.
    * ``each_raises`` runs every job's real body and then raises an Alpaca
      news failure out of it, so the scheduler's failure path is exercised
      with every shipped job's own rule and inputs.
    * ``alpaca_news_fails`` is the spec's named case, end to end: every job
      as shipped, with Alpaca's ``/v1beta1/news`` unreachable -- a real
      httpx ``ConnectError`` from ``data.alpaca.markets``, the vendor whose
      *socket* the watchdog judges -- raised through the real
      :class:`~corollary.data.news.pollers.AlpacaNewsPoller`.

    The positions reader is production's own :class:`HeldPositionUnderlyings`
    (asserted below), so the watch-tier and tradeability jobs run the very
    reader the lifespan hands them. Every vendor is a fake (step 4's news
    jobs call Alpaca, Finnhub and Massive), so each news job's *real* body runs -- polls, stores through the
    writable session, refreshes the asset directory -- rather than its
    no-vendor skip.
    """
    watched = _WatchedRuntime(db_engine, monkeypatch, halted=halted_before)
    state_before = _state_snapshot(db_engine)

    # Thanksgiving evening into Friday's half-day: every kind of slot a
    # shipped job has fires here -- the overnight slow grids, the 03:00 prune,
    # the 07:30 directory, the watch window opening at 06:00, the open's 60 s
    # grid, 10:00 FRED, and the half-day's 13:00 switch back to the slow grid.
    # Not from Wednesday: at the Alpaca job's cadence that is ~2,500 SQLite
    # round trips on worker threads, ~7 s a run, six runs on every ``-m risk``
    # -- and the Wednesday session and the holiday itself are pinned by the
    # pure schedule tests above. See ``wait_for_threads`` for the threads.
    clock = FakeClock(
        THANKSGIVING_EVENING,
        stop_at=HALF_DAY_CLOSE + timedelta(hours=1),
        wait_for_threads=True,
    )

    # FRED configured, served from the recorded fixture: the FRED job's real
    # body -- HTTP, parse, upsert through the writable session, adopt -- runs
    # here rather than its no-key no-op.
    async with _recorded_fred() as fred:
        rates = RiskFreeRateSource()
        alpaca = FailingAlpacaNews() if mode == "alpaca_news_fails" else FakeAlpacaContext()
        finnhub, massive = FakeFinnhubNews(), FakeMassive()
        services = _news_services(
            db_engine,
            alpaca=alpaca,
            finnhub=finnhub,
            massive=massive,
            fred=fred,
            rates=rates,
        )
        assert isinstance(services.position_underlyings, HeldPositionUnderlyings)
        shipped = context_jobs(services, clock=clock)
        assert shipped, "no shipped jobs: this test would prove nothing"

        def raising(job: ScheduledJob) -> ScheduledJob:
            async def run() -> None:
                await job.run()
                raise _alpaca_news_failure()

            # A start-up catch-up is a body too, and runs through its own failure
            # path (``phase="catch_up"``); it must raise here as well, or that
            # path would never be exercised with a shipped job's rule and inputs.
            original_catch_up = job.catch_up
            if original_catch_up is None:
                return dataclasses.replace(job, run=run)

            async def catch_up() -> None:
                await original_catch_up()
                raise _alpaca_news_failure()

            return dataclasses.replace(job, run=run, catch_up=catch_up)

        jobs = [raising(job) for job in shipped] if mode == "each_raises" else shipped
        scheduler = Scheduler(jobs, secrets=no_secrets, clock=clock, sleep=clock.sleep)
        await run_until_parked(scheduler, clock, timeout=60)

    status = scheduler.status()
    for job in shipped:
        attempts = (
            status[job.name].runs + status[job.name].failures + status[job.name].skips
        )
        assert attempts >= 1, f"{job.name} never ran; widen the window"
        if mode == "each_raises":
            assert status[job.name].last_error_type == "ConnectError"
        elif mode == "as_shipped" or job.name != "news_alpaca":
            # The fakes answer, so nothing fails: the success paths ran.
            assert status[job.name].failures == 0, (job.name, status[job.name])
    if mode == "alpaca_news_fails":
        alpaca_news = status["news_alpaca"]
        assert alpaca_news.failures >= 2
        assert alpaca_news.runs == 0
        assert alpaca_news.last_error_type == "ConnectError"
        assert len(alpaca.calls) == alpaca_news.failures
    # The news bodies really ran: the directory is held, every vendor was asked.
    assert services.assets.current() is not None
    assert finnhub.company.calls and finnhub.market.calls and massive.calls
    assert status["news_prune"].runs + status["news_prune"].failures >= 1
    # The FRED body really ran: the recorded 2026-09-22 close is in use.
    assert rates.current().provenance is RateProvenance.FRED_DGS3MO
    assert rates.current().observation_date == date(2026, 9, 22)

    assert _state_snapshot(db_engine) == state_before
    watched.assert_untouched()


#: What no context job's code may import, directly or through another
#: ``corollary`` module: the runtime and its switch, the ``engine_state`` row's
#: accessors, the sockets that feed the watchdog, and the API -- whose
#: ``routes.engine`` holds ``resume`` and whose ``app`` holds the runtime.
_FORBIDDEN_TO_JOBS = (
    "corollary.engine.runtime",
    "corollary.engine.state",
    "corollary.engine.sockets",
    "corollary.api",
    # The broker (step 4): the watch universe's positions reach the jobs as a
    # callable the API builds, never as a broker object the jobs could hold.
    "corollary.engine.execution",
)


def _module_of(obj: object) -> str:
    """The module that defines ``obj`` -- unwrapping partials and bound methods."""
    target = obj
    while isinstance(target, functools.partial):
        target = target.func
    target = getattr(target, "__func__", target)
    module = inspect.getmodule(target) or inspect.getmodule(type(target))
    assert module is not None, f"cannot tell where {obj!r} was defined"
    return module.__name__


def _contributing_modules(jobs: list[ScheduledJob]) -> set[str]:
    """Every module a shipped job's code or schedule comes from.

    Derived from the jobs themselves, so a job module a later step adds --
    ``corollary.data.news.jobs``, say -- is scanned without anyone
    remembering to list it here.
    """
    modules = {scheduler_module.__name__}
    for job in jobs:
        modules.add(_module_of(job.run))
        if job.catch_up is not None:
            modules.add(_module_of(job.catch_up))
        modules.add(_module_of(job.schedule))
        days = getattr(job.schedule, "days", None)
        if days is not None:
            modules.add(_module_of(days))
    return modules


#: Non-``corollary`` modules an object in :class:`ContextServices` may come
#: from. A test fake or a third-party object here would be code the import
#: scan cannot see into, so anything else fails the scan.
_SERVICE_MODULES_OUTSIDE_COROLLARY = frozenset({"builtins"})


def _service_modules(services: ContextServices) -> set[str]:
    """The module of every object :class:`ContextServices` holds (``None`` skipped).

    The default factory is not the only thing the jobs run: in production
    the lifespan hands in a session factory, a positions reader, providers
    and a seed loader, and each one's module is code a job executes.

    **Top-level fields only** -- a function nested inside a partial, a
    closure or a default is ``builtins`` here. The lifespan test in
    ``tests/api/test_news_wiring.py`` adds ``_code_modules``, the defining
    module of every function, method and class its object-graph walk reaches
    (unit 4B2 re-audit), so nested code is import-scanned too.
    """
    modules: set[str] = set()
    for spec in dataclasses.fields(services):
        value = getattr(services, spec.name)
        if value is None:
            continue
        modules.add(_module_of(value))
    return modules


def _outside_corollary(modules: set[str]) -> list[str]:
    return sorted(
        name
        for name in modules
        if not (name == "corollary" or name.startswith("corollary."))
        and name not in _SERVICE_MODULES_OUTSIDE_COROLLARY
    )


def _direct_corollary_imports(module_name: str) -> set[str]:
    spec = importlib.util.find_spec(module_name)
    assert spec is not None and spec.origin is not None, module_name
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    is_package = spec.submodule_search_locations is not None
    package = module_name if is_package else module_name.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join(anchor + ([base] if base else []))
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return {n for n in names if n == "corollary" or n.startswith("corollary.")}


def _is_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False  # ``from pkg import attribute`` -- a name, not a module


def _reachable_corollary_modules(start: set[str]) -> set[str]:
    """Every ``corollary`` name importable from ``start``, transitively.

    Includes every parent package, since importing ``a.b.c`` runs
    ``a/__init__`` and ``a/b/__init__`` first. Follows only real modules;
    ``from x import name`` also records ``x.name`` so that
    ``from corollary.engine import state`` is seen as the module it is.
    Imports inside function bodies count: ``ast.walk`` sees every node.
    """
    seen: set[str] = set()
    frontier = list(start)
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        if not _is_module(name):
            continue
        parts = name.split(".")
        frontier.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        frontier.extend(_direct_corollary_imports(name))
    return seen


def _forbidden(names: set[str]) -> list[str]:
    return sorted(
        name
        for name in names
        for banned in _FORBIDDEN_TO_JOBS
        if name == banned or name.startswith(banned + ".")
    )


@pytest.mark.risk
def test_the_import_scan_sees_what_it_is_meant_to_forbid() -> None:
    """The scan itself: it must catch each way of spelling the forbidden import."""
    assert _forbidden({"corollary.api.routes.engine"}) == ["corollary.api.routes.engine"]
    assert _forbidden({"corollary.engine.state"}) == ["corollary.engine.state"]
    assert _forbidden({"corollary.apiary", "corollary.engine.stateless"}) == []
    # ``tests.engine.test_runtime`` resumes through the route with an import
    # inside a function body -- the spelling a job module would hide it in.
    reachable = _reachable_corollary_modules({"tests.engine.test_runtime"})
    assert "corollary.api.routes.engine" in reachable
    assert "corollary.engine.runtime" in reachable


@pytest.mark.risk
def test_the_scheduler_cannot_reach_the_runtime_by_construction() -> None:
    """Nothing a context job is built from, or imports, carries a way to halt or resume.

    Two halves:

    * **Imports.** Every module that contributes a shipped job -- found from
      the jobs, not a hand-kept list -- plus the scheduler itself, and every
      ``corollary`` module those import in turn, is free of the runtime,
      ``engine_state``'s accessors, the sockets and the API. A job module
      that did ``from corollary.api.routes.engine import resume`` -- at the
      top or inside a function -- fails here.
    * **Types.** Neither the :class:`Scheduler` constructor nor
      :class:`ContextServices` nor the factory the lifespan calls takes an
      ``EngineRuntime``, a ``Watchdog`` or a broker.

    What an AST scan cannot see is a dynamic ``importlib.import_module`` of
    a string; the behavioural test above is what stands behind that.
    """
    services = ContextServices(session_factory=lambda: None)  # type: ignore[arg-type, return-value]
    jobs = context_jobs(services)
    contributing = _contributing_modules(jobs) | {
        name for name in _service_modules(services) if name.startswith("corollary.")
    }
    assert scheduler_module.__name__ in contributing
    reachable = _reachable_corollary_modules(contributing)
    assert "corollary.calendars" in reachable  # the scan does walk imports
    assert _forbidden(reachable) == []

    for hints in (
        get_type_hints(Scheduler.__init__),
        get_type_hints(ContextServices),
        get_type_hints(build_context_scheduler),
    ):
        for annotation in hints.values():
            text = repr(annotation)
            assert "EngineRuntime" not in text
            assert "Watchdog" not in text
            assert "Broker" not in text


# --------------------------------------------------------------------------
# The context jobs' session factory, which shutdown drains
# --------------------------------------------------------------------------


def test_context_sessions_count_every_open_session_until_it_closes(db_engine: Engine) -> None:
    sessions = ContextSessions(db_engine)
    first, second = sessions(), sessions()
    assert sessions.open_sessions == 2
    first.close()
    first.close()  # a second close is not a second decrement
    assert sessions.open_sessions == 1
    with second:
        pass
    assert sessions.open_sessions == 0


@pytest.mark.asyncio
async def test_a_drain_waits_for_a_worker_threads_session_then_refuses_new_ones(
    db_engine: Engine,
) -> None:
    """A job task cancelled mid-``to_thread`` leaves its thread writing; drain waits for it."""
    sessions = ContextSessions(db_engine)
    opened, release = threading.Event(), threading.Event()
    order: list[str] = []

    def worker() -> None:
        with sessions() as session:
            opened.set()
            release.wait(timeout=10)
            session.execute(text("SELECT 1"))
            order.append("thread closed its session")

    thread = threading.Thread(target=worker)
    thread.start()
    assert await asyncio.to_thread(opened.wait, 10)
    drain = asyncio.get_running_loop().create_task(sessions.drain(timeout=10))
    await asyncio.sleep(0.05)
    assert not drain.done(), "drain returned while a session was still open"
    release.set()
    assert await drain is True
    order.append("drain returned")
    thread.join(timeout=10)
    assert order == ["thread closed its session", "drain returned"]
    with pytest.raises(RuntimeError, match="shut down"):
        sessions()


@pytest.mark.asyncio
async def test_a_drain_that_times_out_is_logged_and_never_raises(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    sessions = ContextSessions(db_engine)
    stuck = sessions()
    try:
        with caplog.at_level(logging.ERROR):
            assert await sessions.drain(timeout=0.05) is False
        said = [r for r in caplog.records if getattr(r, "event", None) == "context_sessions_not_drained"]
        assert len(said) == 1
        assert getattr(said[0], "open_sessions") == 1
    finally:
        stuck.close()
