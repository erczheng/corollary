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
from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta, timezone
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
    JobStatus,
    ScheduledJob,
    Scheduler,
    build_context_scheduler,
    context_jobs,
    on_weekdays,
    trading_days,
)
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
    """

    def __init__(self, start: datetime, *, stop_at: datetime) -> None:
        self.now = start
        self.stop_at = stop_at
        self.parked = asyncio.Event()
        self._waiting: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._sequence = 0

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

    async def drive(self) -> None:
        while True:
            for _ in range(50):
                await asyncio.sleep(0)
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


def test_the_shipped_job_set_is_one_calendar_clocked_probe() -> None:
    """Step 2 ships a no-op; the feeds' jobs arrive in their own steps."""
    jobs = context_jobs(ContextServices(session_factory=lambda: None))  # type: ignore[arg-type, return-value]
    assert [job.name for job in jobs] == ["calendar_probe"]
    assert isinstance(jobs[0].schedule, EveryWhileOpen)


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

    services = ContextServices(session_factory=lambda: Session(db_engine))
    shipped = context_jobs(services)
    assert shipped, "no shipped jobs: this test would prove nothing"

    def raising(job: ScheduledJob) -> ScheduledJob:
        async def run() -> None:
            await job.run()
            raise _alpaca_news_failure()

        return dataclasses.replace(job, run=run)

    jobs = shipped if mode == "as_shipped" else [raising(job) for job in shipped]

    # Wednesday afternoon, across Thanksgiving, into Friday's half-day: every
    # in-session and pre-market slot a shipped job could have fires here.
    clock = FakeClock(WED_1500_ET, stop_at=HALF_DAY_CLOSE + timedelta(hours=1))
    scheduler = Scheduler(jobs, secrets=no_secrets, clock=clock, sleep=clock.sleep)
    await run_until_parked(scheduler, clock)

    status = scheduler.status()
    for job in shipped:
        attempts = status[job.name].runs + status[job.name].failures
        assert attempts >= 1, f"{job.name} never ran; widen the window"
        if mode == "each_raises":
            assert status[job.name].last_error_type == "ConnectError"

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
