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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
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
    Sleeper,
    TwoRate,
    WatchTierCadence,
    build_context_scheduler,
    context_jobs,
    every_day,
    on_weekdays,
    trading_days,
)
from corollary.data.macro.risk_free import store_observations
from corollary.data.calendar_dividends import CashDividendRead
from corollary.db.models import CalendarEvent
from sqlalchemy import select
from tests.engine.test_calendar_jobs import (
    FakeDividends,
    FakeFinnhubCalendar,
    FakeReleases,
    _cpi,
    _dividend,
    _earning,
    _ipo,
)
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
from tests.data.seeds.test_nport_snapshot import FakeResolver, FakeSec, sec_provider
from corollary.data.seeds import nport
from corollary.db.models import SpdrHoldingsSnapshot
from corollary.ratelimit import SEC_DATA_HOST

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
    "spdr_holdings",
    "calendar_earnings",
    "calendar_ipo",
    "calendar_dividends",
    "calendar_releases",
    "calendar_central_banks",
]


#: Step 7's calendar jobs (unit 7.2c-2), and the slot each runs at.
_CALENDAR_JOBS = (
    "calendar_earnings",
    "calendar_ipo",
    "calendar_dividends",
    "calendar_releases",
    "calendar_central_banks",
)


@pytest.mark.risk
def test_the_shipped_job_set_carries_the_five_calendar_jobs_at_their_cadences() -> None:
    """The rule 9 isolation tests below run whatever ``context_jobs`` ships.

    So the calendar jobs being *in* that set is what puts them under the
    isolation tests at all -- pinned here, with the clocks the feeds table
    gives them: earnings 07:00 ET daily, the IPO and dividends reads on the
    next two five-minute slots, FRED's release dates with the FRED row at
    10:00 ET on trading days, the central-bank seed at 06:45 ET. Every one
    catches up at start.
    """
    jobs = context_jobs(ContextServices(session_factory=lambda: None))  # type: ignore[arg-type, return-value]
    by_name = {job.name: job for job in jobs}
    assert set(_CALENDAR_JOBS) <= set(by_name)
    assert {name: by_name[name].schedule for name in _CALENDAR_JOBS} == {
        "calendar_earnings": AtTime(time(7, 0), every_day),
        "calendar_ipo": AtTime(time(7, 5), every_day),
        "calendar_dividends": AtTime(time(7, 10), every_day),
        "calendar_releases": AtTime(time(10, 0), trading_days),
        "calendar_central_banks": AtTime(time(6, 45), every_day),
    }
    for name in _CALENDAR_JOBS:
        assert by_name[name].catch_up is not None, name
        assert by_name[name].rule.strip(), name


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
        if name not in ("asset_directory", "news_watch_tier", "spdr_holdings", *_CALENDAR_JOBS):
            assert by_name[name].catch_up is None, name
    # The SPDR sector seed (unit 4SEC-B2): weekly, Monday 09:00 ET, plus a
    # start-up catch-up when no attempt was ever recorded or the last is old.
    spdr = by_name["spdr_holdings"]
    assert spdr.schedule == AtTime(scheduler_module.SPDR_HOLDINGS_AT, scheduler_module.SPDR_HOLDINGS_DAYS)
    assert scheduler_module.SPDR_HOLDINGS_AT == time(9, 0)
    assert spdr.catch_up is not None
    # From a Tuesday, the next slot is the following Monday at 09:00 EDT.
    assert spdr.schedule.next_run(datetime(2026, 9, 29, 12, 0, tzinfo=UTC)) == datetime(
        2026, 10, 5, 13, 0, tzinfo=UTC
    )

    hosts = {name: by_name[name].inputs.get("host") for name in _SHIPPED_NAMES[2:]}
    assert hosts == {
        "news_watch_tier": FINNHUB_HOST,
        "news_alpaca": ALPACA_DATA_HOST,
        "news_finnhub_market": FINNHUB_HOST,
        "news_massive": MASSIVE_HOST,
        "asset_directory": ALPACA_PAPER_TRADING_HOST,
        "tradeability_cache": ALPACA_DATA_HOST,
        "news_prune": "local",
        "spdr_holdings": SEC_DATA_HOST,
        "calendar_earnings": FINNHUB_HOST,
        "calendar_ipo": FINNHUB_HOST,
        "calendar_dividends": ALPACA_DATA_HOST,
        "calendar_releases": FRED_HOST,
        "calendar_central_banks": "local",
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
    # here rather than its no-key no-op. OpenFIGI too, from its recording, so
    # the SPDR body builds rather than skipping for want of an ISIN source.
    from tests.data.seeds.test_isin_resolver import LiveOpenFigi, provider_for

    openfigi_fake = LiveOpenFigi()
    async with _recorded_fred() as fred, _recorded_sec() as sec:
        openfigi = provider_for(openfigi_fake)
        rates = RiskFreeRateSource()
        alpaca = FailingAlpacaNews() if mode == "alpaca_news_fails" else FakeAlpacaContext()
        finnhub, massive = FakeFinnhubNews(), FakeMassive()
        cusips = FakeResolver()
        # Step 7's calendar vendors, answering rows inside each window, so
        # every calendar body fetches and writes. The held-positions holder
        # here is older than HELD_POSITIONS_STALE_AFTER at this clock, so
        # earnings and dividends take the upsert-only path; IPO, releases and
        # central banks write through ``replace_window`` / the seed import.
        calendar_finnhub, dividends, releases = _calendar_fakes()
        services = _news_services(
            db_engine,
            alpaca=alpaca,
            finnhub=finnhub,
            massive=massive,
            fred=fred,
            rates=rates,
            sec=sec,
            cusips=cusips,
            openfigi=openfigi,
            finnhub_calendar=calendar_finnhub,
            dividends=dividends,
            fred_releases=releases,
        )
        assert isinstance(services.position_underlyings, HeldPositionUnderlyings)
        # The scheduler's own sleeper: the SPDR catch-up waits on the fake
        # clock for the asset_directory catch-up, never on the wall clock.
        shipped = context_jobs(services, clock=clock, sleep=clock.sleep)
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
        try:
            await run_until_parked(scheduler, clock, timeout=60)
        finally:
            await openfigi.aclose()

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
    # The SPDR body really ran: it waited for the directory, SEC, OpenFIGI and
    # the CUSIP lookup were asked, and a refusal was recorded -- a genuine one:
    # the test directory lists none of the real holdings' tickers, so the
    # ISIN answers are not listed and XLB falls below the band.
    assert cusips.calls and openfigi_fake.requests
    attempt = nport.latest_snapshot_attempt(lambda: Session(db_engine))
    assert attempt is not None and (attempt.status, attempt.rule) == ("refused", "weight_band")
    # The calendar bodies really ran (step 7): every vendor was asked, and
    # each kind landed through the writable session.
    assert {call[0] for call in calendar_finnhub.calls} == {"earnings", "ipo"}
    assert dividends.calls and releases.calls
    with Session(db_engine) as session:
        kinds = set(session.scalars(select(CalendarEvent.kind).distinct()))
    assert kinds == {"earnings", "ipo", "dividend", "economic", "central-bank"}

    assert _state_snapshot(db_engine) == state_before
    watched.assert_untouched()


def _calendar_fakes() -> tuple[FakeFinnhubCalendar, FakeDividends, FakeReleases]:
    """The step 7 calendar vendors, with one row each inside a late-November window."""
    finnhub = FakeFinnhubCalendar(
        earnings=[_earning(symbol="AAPL", date="2026-12-10")],
        ipos=[_ipo(date="2026-12-01")],
    )
    dividends = FakeDividends(
        CashDividendRead(dividends=(_dividend(ex_date=date(2026, 12, 15)),), skipped=())
    )
    releases = FakeReleases([_cpi(date(2026, 12, 10))])
    return finnhub, dividends, releases


#: How often the stand-in risk manager heartbeats in the hang test: well
#: inside the 90 s watchdog window, so one missed beat is not yet a halt.
_HEARTBEAT_EVERY = timedelta(seconds=30)
#: Fri 27 Nov 2026 02:55 ET (the half-day): every shipped job reaches its
#: body inside the hang test's window -- the catch-ups at once, the 03:00
#: prune, the slow grids, the 07:30 directory, the 09:30 open's probe.
_HANG_FROM = datetime(2026, 11, 27, 7, 55, tzinfo=UTC)
_HANG_UNTIL = datetime(2026, 11, 27, 14, 40, tzinfo=UTC)


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("hang", ["awaiting", "in_a_worker_thread"])
async def test_shipped_context_jobs_hanging_past_the_watchdog_window_never_halt_or_delay_the_heartbeat(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, hang: str
) -> None:
    """Decision 1 for a job that never comes back, not only one that raises.

    Every shipped job -- the calendar jobs included -- has its run and its
    catch-up replaced by a body that never returns: ``awaiting`` a vendor
    that never answers, or ``in_a_worker_thread``, a blocking call stuck off
    the loop. The schedules, names, rules and inputs are the shipped ones.
    Beside them runs a stand-in for the risk manager on the same loop and
    the same clock, heartbeating an **armed** watchdog every 30 s and asking
    it after every beat, for over six hours of calendar -- far past the 90 s
    window -- with every job hung.

    It must come out un-halted, with the halt row as it went in, no watchdog
    input touched by anything but the stand-in, every heartbeat recorded at
    the instant it was due, and the loop never stalled in real time. The
    control: the watchdog really was armed, and ninety seconds past the last
    beat it would have halted.
    """
    clock = FakeClock(_HANG_FROM, stop_at=_HANG_UNTIL)
    notifier = SpyNotifier()
    runtime = EngineRuntime(
        session_factory=lambda: Session(db_engine),
        notifier=notifier,
        now=clock,
        heartbeat_armed=True,
    )
    runtime.start()
    resume_engine(db_engine)
    state_before = _state_snapshot(db_engine)
    assert state_before[0] is False
    # The stand-in's own handles, taken before the spies go on: anything
    # else that reaches a watchdog input is recorded as touching it.
    beat, check = runtime.record_heartbeat, runtime.check_watchdog
    touched: list[str] = []
    for name in _WATCHDOG_INPUTS:
        original = getattr(runtime, name)

        def spy(*args: object, _name: str = name, _orig: object = original,
                **kwargs: object) -> object:
            touched.append(_name)
            return _orig(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(runtime, name, spy)

    calendar_finnhub, dividends, releases = _calendar_fakes()
    services = _news_services(
        db_engine,
        alpaca=FakeAlpacaContext(),
        finnhub=FakeFinnhubNews(),
        massive=FakeMassive(),
        finnhub_calendar=calendar_finnhub,
        dividends=dividends,
        fred_releases=releases,
    )
    shipped = context_jobs(services, clock=clock, sleep=clock.sleep)
    assert set(_CALENDAR_JOBS) <= {job.name for job in shipped}

    entered: list[str] = []
    never = asyncio.Event()
    release = threading.Event()

    def hung(job_name: str) -> Callable[[], Awaitable[None]]:
        async def body() -> None:
            entered.append(job_name)
            if hang == "awaiting":
                await never.wait()
            else:
                await asyncio.to_thread(release.wait)

        return body

    jobs = [
        dataclasses.replace(
            job,
            run=hung(job.name),
            catch_up=None if job.catch_up is None else hung(job.name),
        )
        for job in shipped
    ]
    scheduler = Scheduler(jobs, secrets=no_secrets, clock=clock, sleep=clock.sleep)

    beats: list[tuple[datetime, datetime]] = []
    decisions: list[object] = []

    async def risk_manager() -> None:
        due = clock()
        while True:
            due = due + _HEARTBEAT_EVERY
            await clock.sleep((due - clock()).total_seconds())
            # Asked *before* the beat, so the watchdog judges the gap since
            # the previous one -- a late beat would be a halt here.
            decision = check()
            if decision is not None:
                decisions.append(decision)
            beats.append((due, clock()))
            beat(clock())

    loop = asyncio.get_running_loop()
    stalls: list[float] = []

    async def loop_monitor() -> None:
        while True:
            started = loop.time()
            await asyncio.sleep(0.005)
            stalls.append(loop.time() - started)

    heart = loop.create_task(risk_manager(), name="risk-manager-stand-in")
    monitor = loop.create_task(loop_monitor(), name="loop-monitor")
    try:
        await run_until_parked(scheduler, clock, timeout=60)
    finally:
        release.set()
        heart.cancel()
        monitor.cancel()
        await asyncio.gather(heart, monitor, return_exceptions=True)

    # Every shipped job really hung -- the calendar five among them.
    assert set(entered) == {job.name for job in shipped}
    status = scheduler.status()
    for job in shipped:
        assert status[job.name].runs == 0 and status[job.name].failures == 0, job.name

    # The heartbeat was never late: one beat per 30 s for the whole window,
    # each recorded at the instant it was due, and the armed watchdog never
    # answered.
    assert len(beats) == (_HANG_UNTIL - _HANG_FROM) // _HEARTBEAT_EVERY
    assert all(due == at for due, at in beats)
    assert decisions == []
    # Nor did the loop stall in real time while the jobs hung.
    assert stalls and max(stalls) < 1.0, max(stalls)

    assert _state_snapshot(db_engine) == state_before
    assert touched == []
    assert notifier.sent == []
    # The control: armed, ninety seconds after the last beat, it would halt.
    assert runtime.watchdog.evaluate(beats[-1][1] + timedelta(seconds=90)) is not None


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


# --------------------------------------------------------------------------
# The SPDR sector seed from SEC N-PORT (unit 4SEC-B2)
# --------------------------------------------------------------------------


@asynccontextmanager
async def _recorded_sec(fake: FakeSec | None = None) -> AsyncIterator[object]:
    """SEC served from the recorded 2026-06-30 quarter, offline."""
    provider = sec_provider(fake if fake is not None else FakeSec())
    try:
        yield provider
    finally:
        await provider.aclose()


SPDR_NOW = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)


class _FixedAssets:
    """An :class:`AssetSource` answering one fixed directory."""

    def __init__(self, directory: object) -> None:
        self.directory = directory

    async def active_equities(self) -> object:
        return self.directory


def _spdr_directory(*, without: frozenset[str] = frozenset()) -> AssetDirectory:
    """Every ticker a test build can resolve to: the CUSIP survey, the seam's, OpenFIGI's."""
    from tests.data.seeds.test_isin_resolver import LIVE_TICKERS, directory_of
    from tests.data.seeds.test_nport_snapshot import SURVEY, XLB_ISINS

    return directory_of(
        (set(SURVEY.values()) | set(XLB_ISINS.values()) | set(LIVE_TICKERS.values())) - without
    )


async def _never_sleeps(seconds: float) -> None:
    raise AssertionError(f"the SPDR job slept {seconds}s; this test gave it no sleeper")


_FULL = object()


async def _spdr_jobs(
    db_engine: Engine,
    *,
    directory: object = _FULL,
    sleep: Sleeper = _never_sleeps,
    **extra: object,
) -> tuple[ContextServices, ScheduledJob]:
    """The SPDR job over fresh services, with the day's directory held unless ``directory=None``."""
    services = _news_services(db_engine, **extra)
    held = _spdr_directory() if directory is _FULL else directory
    if held is not None:
        await services.assets.refresh(_FixedAssets(held))  # type: ignore[arg-type]
    jobs = {job.name: job for job in context_jobs(services, clock=lambda: SPDR_NOW, sleep=sleep)}
    return services, jobs["spdr_holdings"]


def _attempts(db_engine: Engine) -> list[SpdrHoldingsSnapshot]:
    with Session(db_engine) as session:
        return list(session.query(SpdrHoldingsSnapshot).order_by(SpdrHoldingsSnapshot.id))


@pytest.mark.asyncio
async def test_without_sec_user_agent_the_spdr_job_skips_and_never_succeeds(db_engine: Engine) -> None:
    _, job = await _spdr_jobs(db_engine, sec=None, cusips=FakeResolver())
    assert job.catch_up is not None
    for body in (job.run, job.catch_up):
        outcome = await body()
        assert isinstance(outcome, JobSkipped)
        assert "SEC_USER_AGENT" in outcome.reason
    assert _attempts(db_engine) == []


@pytest.mark.asyncio
async def test_without_a_cusip_lookup_the_spdr_job_skips(db_engine: Engine) -> None:
    async with _recorded_sec() as sec:
        _, job = await _spdr_jobs(db_engine, sec=sec, cusips=None)
        outcome = await job.run()
    assert isinstance(outcome, JobSkipped)
    assert _attempts(db_engine) == []


@pytest.mark.risk
@pytest.mark.asyncio
async def test_with_no_asset_directory_the_scheduled_spdr_run_skips_and_stores_nothing(
    db_engine: Engine,
) -> None:
    """A missing precondition is not a refusal (2026-10-07): nothing fetched, nothing stored.

    The weekly slot does not wait -- the directory job runs at 07:30 ET, so a
    09:00 run without one is a directory problem with its own failure record.
    """
    fake = FakeSec()
    cusips = FakeResolver()
    async with _recorded_sec(fake) as sec:
        _, job = await _spdr_jobs(db_engine, directory=None, sec=sec, cusips=cusips)
        outcome = await job.run()
    assert isinstance(outcome, JobSkipped)
    assert "asset directory" in outcome.reason
    assert _attempts(db_engine) == []
    assert fake.archive_requests == [] and cusips.calls == []


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_genuinely_refused_snapshot_is_the_job_doing_its_work_and_is_recorded(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Directory held, OpenFIGI answering, LIN not listed: XLB below 90, a stored refusal."""
    from tests.data.seeds.test_isin_resolver import LiveOpenFigi, provider_for

    openfigi = provider_for(LiveOpenFigi())
    try:
        async with _recorded_sec() as sec:
            _, job = await _spdr_jobs(
                db_engine,
                directory=_spdr_directory(without=frozenset({"LIN"})),
                sec=sec, cusips=FakeResolver(), openfigi=openfigi,
            )
            outcome = await job.run()
    finally:
        await openfigi.aclose()
    assert outcome is None
    [attempt] = _attempts(db_engine)
    assert (attempt.status, attempt.rule) == ("refused", "weight_band")
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "spdr_snapshot_refused"]
    assert record.rule == "weight_band"


@pytest.mark.asyncio
async def test_an_already_loaded_quarter_is_a_skip_not_a_success(db_engine: Engine) -> None:
    from tests.data.seeds.test_nport_snapshot import FakeIsinResolver

    async with _recorded_sec() as sec:
        accepted = await nport.build_spdr_snapshot(
            sec, FakeResolver(), lambda: Session(db_engine), isin_resolver=FakeIsinResolver()
        )
        assert accepted.status == "accepted"
        _, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        outcome = await job.run()
    assert isinstance(outcome, JobSkipped)
    assert "2026-06-30" in outcome.reason
    assert len(_attempts(db_engine)) == 1


@pytest.mark.asyncio
async def test_sec_refusing_access_is_a_failure_logged_at_error(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeSec()
    fake.status = 403
    async with _recorded_sec(fake) as sec:
        _, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        with pytest.raises(scheduler_module.SpdrSnapshotAborted) as raised:
            await job.run()
    assert raised.value.rule is nport.SnapshotRule.SEC_ACCESS_REFUSED
    assert _attempts(db_engine) == []
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "spdr_snapshot_sec_access_refused"]
    assert record.levelno == logging.ERROR
    assert "synthetic.tester" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_an_aborted_build_is_a_failure_under_the_scheduler_not_a_retry_storm(
    db_engine: Engine,
) -> None:
    """An abort raises; the scheduler records one failure and waits for the next weekly slot."""
    fake = FakeSec()
    fake.status = 403
    async with _recorded_sec(fake) as sec:
        services = _news_services(db_engine, sec=sec, cusips=FakeResolver())
        await services.assets.refresh(_FixedAssets(_spdr_directory()))  # type: ignore[arg-type]
        clock = FakeClock(SPDR_NOW, stop_at=SPDR_NOW + timedelta(days=2))
        job = _jobs_by_name(services, clock)["spdr_holdings"]
        scheduler = Scheduler([job], secrets=no_secrets, clock=clock, sleep=clock.sleep)
        await run_until_parked(scheduler, clock, timeout=30)
    status = scheduler.status()["spdr_holdings"]
    assert status.failures == 1  # the catch-up; Monday's slot is outside the window
    assert status.runs == 0
    assert status.last_error_type == "SpdrSnapshotAborted"


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_cusip_lookup_outage_aborts_the_spdr_job_and_stores_nothing(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_isin_seam(monkeypatch)
    async with _recorded_sec() as sec:
        _, job = await _spdr_jobs(
            db_engine, sec=sec, cusips=FakeResolver(broken=frozenset({"037833100"}))
        )
        with pytest.raises(scheduler_module.SpdrSnapshotAborted) as raised:
            await job.run()
    assert raised.value.rule is nport.SnapshotRule.CUSIP_LOOKUP_FAILED
    assert _attempts(db_engine) == []


# -- the start-up catch-up: a seven-day gate on facts, a bounded wait on the directory


@pytest.mark.risk
@pytest.mark.asyncio
async def test_the_spdr_catch_up_is_suppressed_for_seven_days_by_a_stored_genuine_refusal(
    db_engine: Engine,
) -> None:
    """A real data problem must not cost ~474 CUSIP lookups on every ``--reload`` start."""
    from tests.data.seeds.test_isin_resolver import LiveOpenFigi, provider_for

    fake = FakeSec()
    openfigi = provider_for(LiveOpenFigi())
    try:
        async with _recorded_sec(fake) as sec:
            _, job = await _spdr_jobs(
                db_engine,
                directory=_spdr_directory(without=frozenset({"LIN"})),
                sec=sec, cusips=FakeResolver(), openfigi=openfigi,
            )
            assert job.catch_up is not None
            assert await job.catch_up() is None  # nothing ever attempted: builds, refused
            assert [(a.status, a.rule) for a in _attempts(db_engine)] == [("refused", "weight_band")]
            fetched = len(fake.archive_requests)
            # Stamped on the job's clock; moved explicitly to either side of 7 days.
            with Session(db_engine) as session, session.begin():
                session.query(SpdrHoldingsSnapshot).one().built_at = (
                    SPDR_NOW - timedelta(days=6, hours=23)
                )
            skipped = await job.catch_up()
            assert isinstance(skipped, JobSkipped) and "refused" in skipped.reason
            assert len(fake.archive_requests) == fetched  # SEC not asked again
            assert len(_attempts(db_engine)) == 1
            with Session(db_engine) as session, session.begin():
                session.query(SpdrHoldingsSnapshot).one().built_at = (
                    SPDR_NOW - timedelta(days=7, seconds=1)
                )
            assert await job.catch_up() is None  # older than seven days: builds again
            assert len(_attempts(db_engine)) == 2
    finally:
        await openfigi.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_the_spdr_catch_up_is_suppressed_for_seven_days_by_an_accepted_snapshot(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_isin_seam(monkeypatch)
    fake = FakeSec()
    async with _recorded_sec(fake) as sec:
        _, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        assert job.catch_up is not None
        assert await job.catch_up() is None
        assert [a.status for a in _attempts(db_engine)] == ["accepted"]
        fetched = len(fake.archive_requests)
        skipped = await job.catch_up()
        assert isinstance(skipped, JobSkipped) and "accepted" in skipped.reason
        assert len(fake.archive_requests) == fetched
    assert len(_attempts(db_engine)) == 1


class _DirectoryArrives:
    """A :data:`Sleeper` that records every wait and loads the directory on call ``on_call``."""

    def __init__(self, holder: AssetDirectoryHolder, on_call: int | None) -> None:
        self.holder = holder
        self.on_call = on_call
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.on_call is not None and len(self.slept) == self.on_call:
            await self.holder.refresh(_FixedAssets(_spdr_directory()))  # type: ignore[arg-type]


def _catch_up_waiting_on(
    db_engine: Engine, sec: object, on_call: int | None
) -> tuple[ContextServices, ScheduledJob, _DirectoryArrives]:
    services = _news_services(db_engine, sec=sec, cusips=FakeResolver())
    sleeper = _DirectoryArrives(services.assets, on_call)
    jobs = {job.name: job for job in context_jobs(services, clock=lambda: SPDR_NOW, sleep=sleeper)}
    return services, jobs["spdr_holdings"], sleeper


def test_the_spdr_directory_wait_is_bounded_and_polls_well_inside_it() -> None:
    assert scheduler_module.SPDR_DIRECTORY_WAIT == timedelta(minutes=5)
    assert scheduler_module.SPDR_DIRECTORY_POLL == timedelta(seconds=5)


@pytest.mark.risk
@pytest.mark.asyncio
async def test_the_spdr_catch_up_waits_for_the_asset_directory_then_builds(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch-ups start together; the directory lands a few polls after SPDR's starts waiting."""
    _with_isin_seam(monkeypatch)
    async with _recorded_sec() as sec:
        _, job, sleeper = _catch_up_waiting_on(db_engine, sec, on_call=3)
        assert job.catch_up is not None
        assert await job.catch_up() is None
    poll = scheduler_module.SPDR_DIRECTORY_POLL.total_seconds()
    assert sleeper.slept == [poll, poll, poll]
    assert [a.status for a in _attempts(db_engine)] == ["accepted"]


@pytest.mark.risk
@pytest.mark.asyncio
async def test_the_spdr_catch_up_gives_up_at_the_bound_stores_nothing_and_the_next_start_retries(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_isin_seam(monkeypatch)
    fake = FakeSec()
    async with _recorded_sec(fake) as sec:
        services, job, sleeper = _catch_up_waiting_on(db_engine, sec, on_call=None)
        assert job.catch_up is not None
        outcome = await job.catch_up()
        assert isinstance(outcome, JobSkipped)
        assert "asset directory" in outcome.reason and "5 minutes" in outcome.reason
        assert sum(sleeper.slept) == scheduler_module.SPDR_DIRECTORY_WAIT.total_seconds()
        assert _attempts(db_engine) == []
        assert fake.archive_requests == []
        # Nothing was stored, so nothing suppresses the next start's catch-up.
        await services.assets.refresh(_FixedAssets(_spdr_directory()))  # type: ignore[arg-type]
        assert await job.catch_up() is None
    assert [a.status for a in _attempts(db_engine)] == ["accepted"]


@pytest.mark.asyncio
async def test_with_a_directory_held_the_spdr_catch_up_does_not_wait(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_isin_seam(monkeypatch)
    async with _recorded_sec() as sec:
        _, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())  # _never_sleeps
        assert job.catch_up is not None
        assert await job.catch_up() is None
    assert [a.status for a in _attempts(db_engine)] == ["accepted"]


@pytest.mark.asyncio
async def test_a_suppressed_spdr_catch_up_does_not_wait_for_the_directory(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seven-day gate is a database read, so it is asked before any waiting."""
    _with_isin_seam(monkeypatch)
    async with _recorded_sec() as sec:
        _, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        assert job.catch_up is not None
        assert await job.catch_up() is None
        _, job = await _spdr_jobs(db_engine, directory=None, sec=sec, cusips=FakeResolver())
        assert job.catch_up is not None
        assert isinstance(await job.catch_up(), JobSkipped)  # _never_sleeps was not called


# -- the ISIN seam


@pytest.mark.risk
@pytest.mark.asyncio
async def test_without_openfigi_the_spdr_job_skips_stores_nothing_and_says_so(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """No ISIN source is configuration, not a fact about the filing: a skip, nothing stored.

    It used to fall back to ``NoIsinResolver`` and store a ``weight_band``
    refusal for the real quarter, which suppressed the catch-up for a week.
    The prefetch fails before any CUSIP is looked up.
    """
    caplog.set_level(logging.INFO)
    cusips = FakeResolver()
    async with _recorded_sec() as sec:
        services, job = await _spdr_jobs(db_engine, sec=sec, cusips=cusips)
        assert services.openfigi is None
        outcome = await job.run()
    assert isinstance(outcome, JobSkipped)
    assert "OpenFIGI" in outcome.reason
    assert _attempts(db_engine) == []
    assert cusips.calls == []
    said = [r for r in caplog.records if getattr(r, "event", "") == "spdr_isin_source_unavailable"]
    assert len(said) == 1 and said[0].levelno == logging.INFO


@pytest.mark.asyncio
async def test_with_openfigi_the_spdr_job_resolves_the_isin_lines_against_the_shared_directory(
    db_engine: Engine,
) -> None:
    """The live OpenFIGI recording, three batched requests, and the day's directory."""
    from tests.data.seeds.test_isin_resolver import LIVE_TICKERS, LiveOpenFigi, provider_for

    from corollary.db.models import IsinTicker

    fake = LiveOpenFigi()
    openfigi = provider_for(fake)
    try:
        async with _recorded_sec() as sec:
            _, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver(), openfigi=openfigi)
            assert await job.run() is None
    finally:
        await openfigi.aclose()
    [attempt] = _attempts(db_engine)
    assert attempt.status == "accepted", attempt.reason
    assert [len(r) for r in fake.requests] == [10, 10, 9]
    with Session(db_engine) as session:
        cached = {row.isin: row.ticker for row in session.query(IsinTicker)}
    assert cached == LIVE_TICKERS


@pytest.mark.risk
@pytest.mark.asyncio
async def test_with_openfigi_but_no_directory_yet_the_spdr_job_skips_and_asks_nothing(
    db_engine: Engine,
) -> None:
    """Fail closed before the asset directory's first fetch: nothing asked, nothing stored."""
    from tests.data.seeds.test_isin_resolver import LiveOpenFigi, provider_for

    fake = LiveOpenFigi()
    openfigi = provider_for(fake)
    try:
        async with _recorded_sec() as sec:
            services, job = await _spdr_jobs(
                db_engine, directory=None, sec=sec, cusips=FakeResolver(), openfigi=openfigi
            )
            assert services.assets.current() is None
            outcome = await job.run()
    finally:
        await openfigi.aclose()
    assert isinstance(outcome, JobSkipped)
    assert _attempts(db_engine) == []
    assert fake.requests == []


@pytest.mark.risk
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.asyncio
async def test_an_openfigi_outage_aborts_the_spdr_job_and_stores_nothing(
    db_engine: Engine, status: int
) -> None:
    from tests.data.seeds.test_isin_resolver import LiveOpenFigi, provider_for

    fake = LiveOpenFigi()
    fake.status = status
    openfigi = provider_for(fake)
    try:
        async with _recorded_sec() as sec:
            _, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver(), openfigi=openfigi)
            with pytest.raises(scheduler_module.SpdrSnapshotAborted) as raised:
                await job.run()
    finally:
        await openfigi.aclose()
    assert raised.value.rule is nport.SnapshotRule.ISIN_LOOKUP_FAILED
    assert _attempts(db_engine) == []


def test_the_services_default_seed_loader_reads_the_database(db_engine: Engine) -> None:
    services = ContextServices(session_factory=lambda: Session(db_engine))
    assert services.seed_loader is not None
    assert services.seed_loader() is None  # no accepted snapshot: no seed, no file read


# --------------------------------------------------------------------------
# spdr_seed_amended: an adopted NPORT-P/A notifies once (owner decision 2026-09-30)
# --------------------------------------------------------------------------


def _with_isin_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job's builder, with XLB's ISIN seam filled so the real quarter can accept.

    A test's services carry no OpenFIGI provider, so the job passes
    ``UnavailableIsinResolver``, under which the recorded quarter (29
    ISIN-only lines) aborts and stores nothing; the notice path needs an
    accept to be reachable at all.
    """
    from tests.data.seeds.test_nport_snapshot import FakeIsinResolver

    real = nport.build_spdr_snapshot

    async def seeded(*args: object, **kwargs: object) -> nport.SnapshotOutcome:
        kwargs["isin_resolver"] = FakeIsinResolver()
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scheduler_module, "build_spdr_snapshot", seeded)


def _amended_fake(pct_from: str = "0.461687304177", pct_to: str = "0.561687304177") -> FakeSec:
    from tests.data.seeds.test_nport_snapshot import AMENDMENT, with_amendments, xlk_doc

    return with_amendments(
        FakeSec(), {AMENDMENT: ("2026-09-15", xlk_doc(pct_from=pct_from, pct_to=pct_to))}
    )


@pytest.mark.asyncio
async def test_an_adopted_amendment_notifies_once_and_a_rerun_notifies_nothing(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.data.seeds.test_nport_snapshot import AMENDMENT

    _with_isin_seam(monkeypatch)
    async with _recorded_sec() as sec:
        services, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        assert await job.run() is None
    # A normal quarter accepted from its originals announces nothing new.
    assert [a.status for a in _attempts(db_engine)] == ["accepted"]
    assert services.notices.drain() == []

    async with _recorded_sec(_amended_fake()) as sec:
        services2, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        assert await job.run() is None
        [notice] = services2.notices.drain()
        assert (notice.event, notice.severity, notice.account) == ("spdr_seed_amended", "info", None)
        assert notice.title == "SPDR seed amended"
        assert "2026-06-30" in notice.body and f"XLK {AMENDMENT}" in notice.body
        assert "2026-09-15" in notice.body
        assert notice.at == SPDR_NOW and notice.correlation_id
        assert "synthetic.tester" not in notice.body.lower()  # never the User-Agent

        # The same amendment again: unchanged, a skip, and no second notice.
        outcome = await job.run()
        assert isinstance(outcome, JobSkipped)
        assert services2.notices.drain() == []
    assert [a.status for a in _attempts(db_engine)] == ["accepted", "accepted"]


@pytest.mark.asyncio
async def test_a_refused_amendment_notifies_nothing_and_keeps_the_previous(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_isin_seam(monkeypatch)
    async with _recorded_sec() as sec:
        _, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        assert await job.run() is None
    bad = _amended_fake(pct_from="5.277342107815", pct_to="25.277342107815")
    async with _recorded_sec(bad) as sec:
        services, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        assert await job.run() is None  # a refusal is the job doing its work
    assert services.notices.drain() == []
    assert [(a.status, a.rule) for a in _attempts(db_engine)] == [
        ("accepted", None), ("refused", "weight_band")
    ]
    seed = nport.load_spdr_seed_from_db(lambda: Session(db_engine))
    assert seed is not None and seed.filed_date == date(2026, 8, 28)


@pytest.mark.asyncio
async def test_a_notice_that_cannot_be_built_never_fails_the_job(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Decision 20: the amended snapshot stands; the failure is a logged error."""
    _with_isin_seam(monkeypatch)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the notice builder broke")

    monkeypatch.setattr(scheduler_module, "spdr_amended_notice", broken)
    async with _recorded_sec() as sec:
        _, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        assert await job.run() is None
    async with _recorded_sec(_amended_fake()) as sec:
        services, job = await _spdr_jobs(db_engine, sec=sec, cusips=FakeResolver())
        assert await job.run() is None
    assert services.notices.drain() == []
    assert [a.status for a in _attempts(db_engine)] == ["accepted", "accepted"]
    [record] = [
        r for r in caplog.records if getattr(r, "event", "") == "spdr_seed_amended_not_notified"
    ]
    assert record.levelno == logging.ERROR and record.error_type == "RuntimeError"


def test_the_notice_is_built_from_the_stored_rows_or_not_at_all(db_engine: Engine) -> None:
    """An ``adopted`` map the stored rows do not carry builds no notice."""
    from tests.data.seeds.test_nport_snapshot import insert_accepted

    sessions = lambda: Session(db_engine)  # noqa: E731
    insert_accepted(sessions, date(2026, 3, 31), date(2026, 5, 29))
    with Session(db_engine) as session:
        snapshot_id = session.query(SpdrHoldingsSnapshot).one().id
    at = SPDR_NOW
    assert scheduler_module.spdr_amended_notice(sessions, snapshot_id, {}, at) is None
    assert scheduler_module.spdr_amended_notice(
        sessions, snapshot_id, {"XLK": "0001410368-26-099999"}, at
    ) is None  # stored rows carry 0000000000-26-000000
    notice = scheduler_module.spdr_amended_notice(
        sessions, snapshot_id, {"XLK": "0000000000-26-000000"}, at
    )
    assert notice is not None and "XLK 0000000000-26-000000" in notice.body
    assert "2026-03-31" in notice.body
    assert scheduler_module.spdr_amended_notice(
        sessions, snapshot_id + 99, {"XLK": "0000000000-26-000000"}, at
    ) is None


def test_the_outbox_drains_oldest_first_and_drops_loudly_when_full(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outbox = scheduler_module.ContextNotices(limit=2)

    def notice(n: int) -> scheduler_module.ContextNotice:
        return scheduler_module.ContextNotice(
            event="spdr_seed_amended", severity="info", title=f"t{n}", body="b",
            at=SPDR_NOW, correlation_id=f"c{n}",
        )

    for n in range(3):
        outbox.put(notice(n))
    assert len(outbox) == 2
    assert [n.title for n in outbox.drain()] == ["t1", "t2"]
    assert outbox.drain() == [] and len(outbox) == 0
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "context_notice_dropped"]
    assert record.levelno == logging.ERROR and record.correlation_id == "c0"
