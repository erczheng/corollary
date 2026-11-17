"""``tests/api/scheduler_drive.py``: a stuck job fails loudly and by name; a slow run does not fail.

The API tests that drive a lifespan's scheduler used to give the whole drive
10 to 30 s of wall time and race ``clock.parked`` against it, cancelling the
driver afterwards. On a loaded machine that timed out with no assertion
failing, and when a job really was stuck it said so as an anonymous
``TimeoutError``. These pin the replacement's two halves, as
``tests/engine/test_scheduler.py`` pins its driver's.
"""

import asyncio
import threading
from collections.abc import Callable, Sequence
from datetime import timedelta

import pytest
from sqlalchemy import Engine

from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.engine.scheduler import (
    ContextServices,
    EveryWhileOpen,
    JobBody,
    ScheduledJob,
    Scheduler,
)
from tests.api.scheduler_drive import drive_until_parked, until
from tests.engine.test_scheduler import WED_1500_ET, FakeClock

#: 15:15 ET on WED_1500_ET's day: a fifteen-minute job's first slot.
FIRST_SLOT = "2026-11-25T20:15:00+00:00"


def _one_job(clock: FakeClock, name: str, body: JobBody, every: timedelta) -> Scheduler:
    return Scheduler(
        [ScheduledJob(name=name, schedule=EveryWhileOpen(every), run=body, rule="test")],
        secrets=lambda: (),
        clock=clock,
        sleep=clock.sleep,
    )


def _factory(
    clock: FakeClock, name: str, body: JobBody, every: timedelta
) -> Callable[[ContextServices, Callable[[], Sequence[str]]], Scheduler]:
    def factory(services: ContextServices, secrets: Callable[[], Sequence[str]]) -> Scheduler:
        return _one_job(clock, name, body, every)

    return factory


@pytest.mark.asyncio
async def test_a_job_stuck_under_the_lifespan_fails_the_drive_naming_itself_and_the_instant(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    clock = FakeClock(
        WED_1500_ET,
        stop_at=WED_1500_ET + timedelta(minutes=30),
        wait_for_threads=True,
        hang_guard=0.5,
    )
    release = threading.Event()

    async def stuck() -> None:
        await asyncio.to_thread(release.wait, 30)

    app = create_app(
        registry=registry,
        db_engine=db_engine,
        scheduler=_factory(clock, "stuck", stuck, timedelta(minutes=15)),
    )
    async with app.router.lifespan_context(app):
        try:
            with pytest.raises(AssertionError) as raised:
                await drive_until_parked(app.state.scheduler, clock)
        finally:
            release.set()

    message = str(raised.value)
    assert "context-job:stuck" in message, message
    assert "stuck (test_scheduler_drive.py:" in message, message
    assert FIRST_SLOT in message, message
    assert "0.5 s" in message, message


@pytest.mark.asyncio
async def test_a_slow_drive_under_the_lifespan_parks_however_long_it_takes_in_total(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Every step spends 50 ms on a worker thread; the run outlasts the 2 s guard and parks."""
    clock = FakeClock(
        WED_1500_ET,
        stop_at=WED_1500_ET + timedelta(minutes=61),
        wait_for_threads=True,
        hang_guard=2.0,
    )
    ran: list[object] = []

    async def slow() -> None:
        await asyncio.to_thread(threading.Event().wait, 0.05)
        ran.append(clock())

    app = create_app(
        registry=registry,
        db_engine=db_engine,
        scheduler=_factory(clock, "slow", slow, timedelta(minutes=1)),
    )
    loop = asyncio.get_running_loop()
    async with app.router.lifespan_context(app):
        began = loop.time()
        await drive_until_parked(app.state.scheduler, clock)
        elapsed = loop.time() - began
        assert app.state.scheduler.status()["slow"].failures == 0

    # The premise: the drive as a whole outlasted the per-step guard.
    assert elapsed > clock.hang_guard
    assert len(ran) >= 59, ran


@pytest.mark.asyncio
async def test_until_names_what_it_waited_for_and_where_each_live_job_is() -> None:
    clock = FakeClock(WED_1500_ET, stop_at=WED_1500_ET + timedelta(minutes=15))
    release = threading.Event()
    entered = threading.Event()

    async def stuck() -> None:
        await asyncio.to_thread(lambda: (entered.set(), release.wait(30)))

    scheduler = _one_job(clock, "stuck", stuck, timedelta(minutes=15))
    scheduler.start()
    try:
        # The clock does not wait for threads, so it parks with the job
        # inside its worker thread at 15:15.
        await drive_until_parked(scheduler, clock)
        await until(entered.is_set, "the stuck job entering its worker thread")
        with pytest.raises(AssertionError) as raised:
            await until(release.is_set, "the stuck job's run finishing", hang_guard=0.3)
    finally:
        release.set()
        await scheduler.aclose()

    message = str(raised.value)
    assert message.startswith("the stuck job's run finishing: not reached within 0.3 s"), message
    assert "context-job:stuck" in message, message
    assert "stuck (test_scheduler_drive.py:" in message, message
