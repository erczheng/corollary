"""Waking a job's own task early: :meth:`Scheduler.waker` and :class:`JobWake`.

Cash-session unit 2b. A Cash session confirmed after the calendar's morning
slots would otherwise join the calendar only the next morning. The Cash
positions refresher wakes the two ticker-scoped calendar jobs once it has read
the Cash book -- through a :class:`JobWake`, a capability bound to named jobs
that holds their wake events and nothing else, never the :class:`Scheduler`.

What is pinned here, with plain job bodies (the calendar bodies are pinned in
``test_calendar_cash_scope.py``):

* a wake runs the job's ``woken`` body on the job's **own** task -- never a
  second, concurrent run -- and then the schedule resumes: the woken run does
  not consume the next slot, even when it overruns it;
* at most one woken run per job per :data:`WOKEN_RUN_MIN_INTERVAL`;
* a wake pending when a slot arrives is served by that scheduled run;
* a woken run's success never stands in for a scheduled one in the status
  (it asked about a subset), while its failure is a failure;
* shutdown during a woken run cancels it like any run.
"""

import asyncio
import logging
import threading
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine.scheduler import (
    WOKEN_RUN_MIN_INTERVAL,
    AtTime,
    ContextSessions,
    JobSkipped,
    JobWake,
    ScheduledJob,
    Scheduler,
    every_day,
)
from tests.engine.test_scheduler import FakeClock, no_secrets, run_until_parked

#: 10:00 ET on a Wednesday; the job's 07:00 ET slot is next at 12:00 UTC on 26th.
START = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
NEXT_SLOT = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
STOP = datetime(2026, 11, 26, 13, 0, tzinfo=UTC)


class Bodies:
    """A scheduled and a woken body that note when they ran and catch overlap."""

    def __init__(self, clock: FakeClock, *, woken_takes: timedelta = timedelta(0)) -> None:
        self.clock = clock
        self.scheduled: list[datetime] = []
        self.woken: list[datetime] = []
        self.active = 0
        self.max_active = 0
        self.woken_takes = woken_takes
        self.woken_raises: Exception | None = None
        self.scheduled_raises: Exception | None = None
        self.woken_skips = False
        #: Skip this many woken runs, then run: a skip that is followed by work.
        self.woken_skips_first = 0

    async def run(self) -> None:
        self.scheduled.append(self.clock())
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        if self.scheduled_raises is not None:
            raise self.scheduled_raises

    async def run_woken(self) -> JobSkipped | None:
        self.woken.append(self.clock())
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.woken_takes:
                await self.clock.sleep(self.woken_takes.total_seconds())
            if self.woken_raises is not None:
                raise self.woken_raises
            if self.woken_skips:
                return JobSkipped("nothing Cash-only")
            if self.woken_skips_first:
                self.woken_skips_first -= 1
                return JobSkipped("nothing Cash-only")
            return None
        finally:
            self.active -= 1


def _job(bodies: Bodies, name: str = "calendar_earnings", *, wakeable: bool = True) -> ScheduledJob:
    return ScheduledJob(
        name=name,
        schedule=AtTime(time(7, 0), every_day),
        run=bodies.run,
        woken=bodies.run_woken if wakeable else None,
        rule="test",
    )


def _scheduler(clock: FakeClock, *jobs: ScheduledJob) -> Scheduler:
    return Scheduler(list(jobs), secrets=no_secrets, clock=clock, sleep=clock.sleep)


async def _wake_at(clock: FakeClock, wake: JobWake, *instants: datetime) -> None:
    """Call ``wake`` at each instant, on the fake clock, like the refresher would."""
    for instant in instants:
        await clock.sleep((instant - clock()).total_seconds())
        wake()


async def _drive(scheduler: Scheduler, clock: FakeClock, wake: JobWake, *at: datetime) -> None:
    trigger = asyncio.get_running_loop().create_task(_wake_at(clock, wake, *at))
    try:
        await run_until_parked(scheduler, clock)
    finally:
        trigger.cancel()
        await asyncio.gather(trigger, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_wake_runs_the_woken_body_once_and_the_next_slot_still_runs() -> None:
    clock = FakeClock(START, stop_at=STOP)
    bodies = Bodies(clock)
    scheduler = _scheduler(clock, _job(bodies))
    wake = scheduler.waker(["calendar_earnings"])
    midday = START + timedelta(hours=2)
    await _drive(scheduler, clock, wake, midday)
    assert bodies.woken == [midday]
    # Not consumed: the 07:00 ET slot still ran, at its instant.
    assert bodies.scheduled == [NEXT_SLOT]
    assert bodies.max_active == 1
    status = scheduler.status()["calendar_earnings"]
    assert status.woken_runs == 1
    assert status.last_woken == midday


@pytest.mark.asyncio
async def test_a_woken_run_that_overruns_the_slot_is_followed_by_that_slot() -> None:
    clock = FakeClock(START, stop_at=NEXT_SLOT + timedelta(hours=3))
    bodies = Bodies(clock, woken_takes=timedelta(hours=2))
    scheduler = _scheduler(clock, _job(bodies))
    wake = scheduler.waker(["calendar_earnings"])
    just_before = NEXT_SLOT - timedelta(minutes=30)
    await _drive(scheduler, clock, wake, just_before)
    assert bodies.woken == [just_before]
    # The slot fell inside the woken run; it runs as soon as that ends, never
    # alongside it, and is not skipped to the next morning.
    assert bodies.scheduled == [just_before + timedelta(hours=2)]
    assert bodies.max_active == 1


@pytest.mark.asyncio
async def test_a_second_wake_within_the_hour_runs_nothing_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(START, stop_at=STOP)
    bodies = Bodies(clock)
    scheduler = _scheduler(clock, _job(bodies))
    wake = scheduler.waker(["calendar_earnings"])
    first = START + timedelta(hours=1)
    again = first + WOKEN_RUN_MIN_INTERVAL - timedelta(seconds=1)
    later = first + WOKEN_RUN_MIN_INTERVAL
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        await _drive(scheduler, clock, wake, first, again, later)
    # Refused inside the hour; allowed again at the boundary.
    assert bodies.woken == [first, later]
    capped = [r for r in caplog.records if getattr(r, "event", None) == "context_job_wake_capped"]
    assert len(capped) == 1
    assert capped[0].job == "calendar_earnings"  # type: ignore[attr-defined]


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_woken_skip_does_not_cap_the_next_wake() -> None:
    """Unit 2b audit, finding 2. A skip made no request (every woken skip
    returns before its first one), so it must not charge the hourly cap: a
    wake that found nothing Cash-only, or landed after the session ended,
    would otherwise refuse the next, legitimate wake for an hour."""
    clock = FakeClock(START, stop_at=NEXT_SLOT - timedelta(minutes=1))
    bodies = Bodies(clock)
    bodies.woken_skips_first = 1
    scheduler = _scheduler(clock, _job(bodies))
    skipped_at = START + timedelta(hours=1)
    ran_at = skipped_at + timedelta(minutes=10)
    await _drive(scheduler, clock, scheduler.waker(["calendar_earnings"]), skipped_at, ran_at)
    assert bodies.woken == [skipped_at, ran_at]
    status = scheduler.status()["calendar_earnings"]
    # Both attempts are counted; only the one that did work set the cap's clock.
    assert status.woken_runs == 2
    assert status.last_woken == ran_at


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ran", "failed"])
async def test_a_woken_run_that_made_requests_caps_the_next_wake(outcome: str) -> None:
    """A success, and a failure (which may have spent requests before it
    raised; a failing vendor is not asked again inside the hour), both charge
    the cap. Only the skip is free."""
    clock = FakeClock(START, stop_at=NEXT_SLOT - timedelta(minutes=1))
    bodies = Bodies(clock)
    if outcome == "failed":
        bodies.woken_raises = RuntimeError("finnhub 502")
    scheduler = _scheduler(clock, _job(bodies))
    first = START + timedelta(hours=1)
    second = first + WOKEN_RUN_MIN_INTERVAL - timedelta(seconds=1)
    await _drive(scheduler, clock, scheduler.waker(["calendar_earnings"]), first, second)
    assert bodies.woken == [first]
    status = scheduler.status()["calendar_earnings"]
    assert status.woken_runs == 1
    assert status.last_woken == first


@pytest.mark.asyncio
async def test_only_skips_leave_the_cap_clock_unset() -> None:
    clock = FakeClock(START, stop_at=NEXT_SLOT - timedelta(minutes=1))
    bodies = Bodies(clock)
    bodies.woken_skips = True
    scheduler = _scheduler(clock, _job(bodies))
    first = START + timedelta(hours=1)
    await _drive(
        scheduler, clock, scheduler.waker(["calendar_earnings"]), first, first + timedelta(minutes=5)
    )
    assert bodies.woken == [first, first + timedelta(minutes=5)]
    status = scheduler.status()["calendar_earnings"]
    assert status.woken_runs == 2
    assert status.last_woken is None


@pytest.mark.asyncio
async def test_two_wakes_before_the_task_wakes_coalesce_into_one_run() -> None:
    clock = FakeClock(START, stop_at=STOP)
    bodies = Bodies(clock)
    scheduler = _scheduler(clock, _job(bodies))
    wake = scheduler.waker(["calendar_earnings"])
    midday = START + timedelta(hours=2)
    await _drive(scheduler, clock, wake, midday, midday)
    assert bodies.woken == [midday]


@pytest.mark.asyncio
async def test_a_wake_pending_at_the_slot_is_served_by_the_scheduled_run() -> None:
    """A wake that lands while the scheduled run is due runs nothing extra."""
    clock = FakeClock(START, stop_at=STOP)
    bodies = Bodies(clock)
    scheduler = _scheduler(clock, _job(bodies))
    # The trigger's sleep is registered before the job's (the job waits for
    # the calendar build first), so at NEXT_SLOT the wake lands first and the
    # job finds both "due" and "woken" true at once: the slot wins, and its
    # run clears the wake, because it reads the same Cash view.
    await _drive(scheduler, clock, scheduler.waker(["calendar_earnings"]), NEXT_SLOT)
    assert bodies.scheduled == [NEXT_SLOT]
    assert bodies.woken == []
    assert bodies.max_active == 1


@pytest.mark.asyncio
async def test_a_woken_success_never_stands_in_for_a_scheduled_one() -> None:
    clock = FakeClock(START, stop_at=NEXT_SLOT - timedelta(minutes=1))
    bodies = Bodies(clock)
    scheduler = _scheduler(clock, _job(bodies))
    await _drive(scheduler, clock, scheduler.waker(["calendar_earnings"]), START + timedelta(hours=1))
    status = scheduler.status()["calendar_earnings"]
    assert bodies.woken and not bodies.scheduled
    # It asked about the Cash-only tickers; the panel must not date the whole
    # window's coverage from it.
    assert status.runs == 0
    assert status.last_success is None
    assert status.last_success_started is None
    assert status.last_started is None
    assert status.skips == 0
    assert status.woken_runs == 1


@pytest.mark.asyncio
async def test_a_woken_skip_is_logged_and_never_recorded_as_the_jobs_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(START, stop_at=NEXT_SLOT - timedelta(minutes=1))
    bodies = Bodies(clock)
    bodies.woken_skips = True
    scheduler = _scheduler(clock, _job(bodies))
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        await _drive(scheduler, clock, scheduler.waker(["calendar_earnings"]), START + timedelta(hours=1))
    status = scheduler.status()["calendar_earnings"]
    assert status.skips == 0 and status.last_skip_reason is None
    assert status.woken_runs == 1
    assert [r for r in caplog.records if getattr(r, "event", None) == "context_job_woken_skipped"]


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_woken_failure_is_a_recorded_failure_and_the_schedule_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(START, stop_at=STOP)
    bodies = Bodies(clock)
    bodies.woken_raises = RuntimeError("finnhub 502")
    scheduler = _scheduler(clock, _job(bodies))
    midday = START + timedelta(hours=1)
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        await _drive(scheduler, clock, scheduler.waker(["calendar_earnings"]), midday)
    status = scheduler.status()["calendar_earnings"]
    assert status.failures == 1
    assert status.last_failure == midday
    assert status.last_error_type == "RuntimeError"
    assert bodies.scheduled == [NEXT_SLOT]
    failed = [r for r in caplog.records if getattr(r, "event", None) == "context_job_failed"]
    assert [getattr(r, "phase", None) for r in failed] == ["woken"]
    # The phase rides on the status, so the panel can say which run failed;
    # no full run (scheduled or catch-up) has failed.
    assert status.last_failure_phase == "woken"
    assert status.last_full_failure is None


@pytest.mark.asyncio
async def test_a_woken_failure_after_a_failed_slot_keeps_the_slots_failure_on_record() -> None:
    """``last_failure`` is the newest failure of any run, so the job still
    reads failing; ``last_full_failure`` keeps the slot's, so a reader can
    tell an early Cash run failing beside a good sweep from both failing."""
    clock = FakeClock(START, stop_at=NEXT_SLOT + timedelta(hours=2))
    bodies = Bodies(clock)
    bodies.scheduled_raises = RuntimeError("finnhub 502")
    bodies.woken_raises = RuntimeError("finnhub 502")
    scheduler = _scheduler(clock, _job(bodies))
    woken_at = NEXT_SLOT + timedelta(hours=1)
    await _drive(scheduler, clock, scheduler.waker(["calendar_earnings"]), woken_at)
    status = scheduler.status()["calendar_earnings"]
    assert bodies.scheduled == [NEXT_SLOT] and bodies.woken == [woken_at]
    assert status.failures == 2
    assert status.last_failure == woken_at
    assert status.last_failure_phase == "woken"
    assert status.last_full_failure == NEXT_SLOT
    assert status.failing


def test_a_waker_binds_only_wakeable_jobs_and_holds_no_scheduler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(START, stop_at=STOP)
    bodies = Bodies(clock)
    scheduler = _scheduler(
        clock, _job(bodies), _job(bodies, "calendar_ipo", wakeable=False)
    )
    with caplog.at_level(logging.WARNING, logger="corollary.engine.scheduler"):
        wake = scheduler.waker(["calendar_earnings", "calendar_ipo", "nonexistent"])
    assert wake.jobs == ("calendar_earnings",)
    unbound = [r for r in caplog.records if getattr(r, "event", None) == "context_job_wake_unbound"]
    assert len(unbound) == 1
    assert unbound[0].jobs == ["calendar_ipo", "nonexistent"]  # type: ignore[attr-defined]
    # The capability is its events, and nothing it could run or stop a job with.
    held = [getattr(wake, name) for name in type(wake).__slots__]
    assert not any(isinstance(value, Scheduler) for value in held)
    (pairs,) = held
    assert all(isinstance(event, asyncio.Event) for _, event in pairs)


@pytest.mark.asyncio
async def test_a_wake_before_start_and_one_with_no_jobs_never_raise() -> None:
    clock = FakeClock(START, stop_at=START + timedelta(hours=1))
    bodies = Bodies(clock)
    scheduler = _scheduler(clock, _job(bodies))
    assert scheduler.waker([])() == ()
    wake = scheduler.waker(["calendar_earnings"])
    assert wake() == ("calendar_earnings",)  # before start: kept, served once started
    await run_until_parked(scheduler, clock)
    assert bodies.woken == [START]


@pytest.mark.asyncio
async def test_shutdown_during_a_woken_run_cancels_it_and_aclose_returns() -> None:
    clock = FakeClock(START, stop_at=STOP)
    started = asyncio.Event()
    release = asyncio.Event()

    async def hangs() -> None:
        started.set()
        await release.wait()

    async def scheduled() -> None:
        return None

    scheduler = Scheduler(
        [
            ScheduledJob(
                name="calendar_earnings",
                schedule=AtTime(time(7, 0), every_day),
                run=scheduled,
                woken=hangs,
                rule="test",
            )
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    scheduler.waker(["calendar_earnings"])()
    scheduler.start()
    await asyncio.wait_for(started.wait(), timeout=10)
    await asyncio.wait_for(scheduler.aclose(), timeout=10)
    assert not release.is_set()
    assert scheduler.status()["calendar_earnings"].woken_runs == 1


@pytest.mark.risk
@pytest.mark.asyncio
async def test_shutdown_during_a_woken_runs_database_write_drains_it(tmp_path: Path) -> None:
    """The lifespan's order: ``aclose`` cancels the woken run, whose worker
    thread keeps its session; ``drain`` waits for it, so no commit lands after
    the lifespan returned -- exactly as for a scheduled run."""
    engine = create_db_engine(sqlite_url(tmp_path / "wake_drain.db"))
    sessions = ContextSessions(engine)
    in_thread = threading.Event()
    release = threading.Event()

    def blocking_write() -> None:
        with sessions() as session:
            in_thread.set()
            release.wait(timeout=5)
            session.execute(text("SELECT 1"))

    async def woken() -> None:
        await asyncio.to_thread(blocking_write)

    async def scheduled() -> None:
        return None

    clock = FakeClock(START, stop_at=STOP)
    scheduler = Scheduler(
        [
            ScheduledJob(
                name="calendar_dividends",
                schedule=AtTime(time(7, 10), every_day),
                run=scheduled,
                woken=woken,
                rule="test",
            )
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    scheduler.waker(["calendar_dividends"])()
    scheduler.start()
    try:
        await asyncio.wait_for(asyncio.to_thread(in_thread.wait, 10), timeout=10)
        await asyncio.wait_for(scheduler.aclose(), timeout=10)
        assert sessions.open_sessions == 1  # the thread still holds it
        release.set()
        assert await sessions.drain(timeout=5) is True
        assert sessions.open_sessions == 0
    finally:
        release.set()
        engine.dispose()
