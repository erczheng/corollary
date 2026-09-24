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
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.db.models import Base
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine import scheduler as scheduler_module
from corollary.engine.runtime import EngineRuntime, Notification
from corollary.engine.scheduler import (
    AtTime,
    ContextServices,
    EveryWhileOpen,
    JobSkipped,
    JobStatus,
    ScheduledJob,
    Scheduler,
    build_context_scheduler,
    context_jobs,
    on_weekdays,
    trading_days,
)
from corollary.data.macro.risk_free import store_observations
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


async def run_until_parked(scheduler: Scheduler, clock: FakeClock) -> None:
    scheduler.start()
    # The calendar is built on a worker thread before any job asks a
    # schedule anything. The driver parks when nothing is sleeping on the
    # fake clock, so it must not start until that build has finished.
    await asyncio.wait_for(scheduler.ready(), timeout=10)
    driver = asyncio.get_running_loop().create_task(clock.drive())
    try:
        await asyncio.wait_for(clock.parked.wait(), timeout=10)
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


def test_the_shipped_job_set_is_the_probe_and_the_daily_fred_refresh() -> None:
    """Step 2 shipped the no-op; step 3 adds FRED ``DGS3MO`` at 10:00 ET on trading days.

    The FRED job ships whether or not FRED is configured -- without a key its
    body does nothing -- so the isolation tests below always run it.
    """
    jobs = context_jobs(ContextServices(session_factory=lambda: None))  # type: ignore[arg-type, return-value]
    assert [job.name for job in jobs] == ["calendar_probe", "fred_dgs3mo"]
    probe, fred = jobs
    assert isinstance(probe.schedule, EveryWhileOpen)
    assert probe.catch_up is None
    assert fred.schedule == AtTime(time(10, 0), trading_days)
    assert fred.catch_up is not None
    assert fred.inputs["host"] == FRED_HOST
    assert fred.inputs["series_id"] == "DGS3MO"


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
@pytest.mark.parametrize("mode", ["as_shipped", "each_raises"])
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

    Two modes, because each catches what the other cannot:

    * ``as_shipped`` runs every job exactly as shipped. A job that
      *succeeds* by resuming the engine never raises, so a forced-failure
      run alone would never exercise the line that does it.
    * ``each_raises`` runs every job's real body and then raises an Alpaca
      news failure out of it, so the scheduler's failure path is exercised
      with every shipped job's own rule and inputs.
    """
    watched = _WatchedRuntime(db_engine, monkeypatch, halted=halted_before)
    state_before = _state_snapshot(db_engine)

    # FRED configured, served from the recorded fixture: the FRED job's real
    # body -- HTTP, parse, upsert through the writable session, adopt -- runs
    # here rather than its no-key no-op.
    async with _recorded_fred() as fred:
        rates = RiskFreeRateSource()
        services = ContextServices(
            session_factory=lambda: Session(db_engine), fred=fred, rates=rates
        )
        shipped = context_jobs(services)
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

        jobs = shipped if mode == "as_shipped" else [raising(job) for job in shipped]

        # Wednesday afternoon, across Thanksgiving, into Friday's half-day: every
        # in-session and pre-market slot a shipped job could have fires here.
        # The FRED body writes SQLite on a worker thread; see ``wait_for_threads``.
        clock = FakeClock(
            WED_1500_ET,
            stop_at=HALF_DAY_CLOSE + timedelta(hours=1),
            wait_for_threads=True,
        )
        scheduler = Scheduler(jobs, secrets=no_secrets, clock=clock, sleep=clock.sleep)
        await run_until_parked(scheduler, clock)

    status = scheduler.status()
    for job in shipped:
        attempts = (
            status[job.name].runs + status[job.name].failures + status[job.name].skips
        )
        assert attempts >= 1, f"{job.name} never ran; widen the window"
        if mode == "each_raises":
            assert status[job.name].last_error_type == "ConnectError"
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
      ``EngineRuntime`` or a ``Watchdog``.

    What an AST scan cannot see is a dynamic ``importlib.import_module`` of
    a string; the behavioural test above is what stands behind that.
    """
    jobs = context_jobs(ContextServices(session_factory=lambda: None))  # type: ignore[arg-type, return-value]
    contributing = _contributing_modules(jobs)
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
