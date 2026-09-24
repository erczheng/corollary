"""The context scheduler in the lifespan (Phase 3 decision 1, order-of-work step 2).

Done when: *a calendar-clocked no-op job runs in the lifespan across a
holiday fixture.* The shipped job set -- today one no-op, ``calendar_probe``
-- runs under ``create_app``'s lifespan on a wound clock from the afternoon
before Thanksgiving 2026 to the afternoon of the half-day after it. No vendor
socket is opened: every app here is built with the default
``streams=no_socket_supervisor``.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.db.models import ENGINE_STATE_ID, EngineState
from corollary.engine.runtime import EngineRuntime
from corollary.engine.scheduler import (
    ContextServices,
    EveryWhileOpen,
    ScheduledJob,
    Scheduler,
    build_context_scheduler,
    no_scheduler,
)
from tests.engine.test_scheduler import (
    HALF_DAY_CLOSE,
    HALF_DAY_OPEN,
    MONDAY_OPEN,
    THANKSGIVING,
    WED_1500_ET,
    FakeClock,
)

UTC = timezone.utc


def _context_tasks() -> list[asyncio.Task[object]]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("context-job:") and not task.done()
    ]


@pytest.mark.asyncio
async def test_the_no_op_job_runs_in_the_lifespan_across_a_holiday_and_a_half_day(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    clock = FakeClock(WED_1500_ET, stop_at=datetime(2026, 11, 27, 19, 0, tzinfo=UTC))
    app = create_app(
        registry=registry,
        db_engine=db_engine,
        scheduler=partial(build_context_scheduler, clock=clock, sleep=clock.sleep),
    )
    async with app.router.lifespan_context(app):
        assert app.state.engine_runtime is not None
        assert app.state.socket_supervisor is None  # no vendor socket, ever
        # The calendar builds on a worker thread first; the driver parks when
        # nothing sleeps on the fake clock, so it waits for that build.
        await asyncio.wait_for(app.state.scheduler.ready(), timeout=10)
        driver = asyncio.get_running_loop().create_task(clock.drive())
        try:
            await asyncio.wait_for(clock.parked.wait(), timeout=10)
        finally:
            driver.cancel()
        probe = app.state.scheduler.status()["calendar_probe"]

    # Wednesday 15:15, 15:30, 15:45 ET; nothing on Thanksgiving; Friday every
    # fifteen minutes from the 09:30 open to the last slot before 13:00.
    assert probe.runs == 3 + 14
    assert probe.failures == 0
    assert probe.last_success is not None
    assert probe.last_success.date() != THANKSGIVING
    assert HALF_DAY_OPEN <= probe.last_success < HALF_DAY_CLOSE
    assert probe.last_success == HALF_DAY_CLOSE - timedelta(minutes=15)
    assert probe.next_run == MONDAY_OPEN
    assert _context_tasks() == []  # shutdown cancelled and awaited every job


@pytest.mark.asyncio
async def test_an_app_built_for_a_test_runs_no_scheduler(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """The default is none, for the reason ``streams`` defaults to none.

    Later jobs call real vendors with keys from the environment; a route
    test's app must not do that by forgetting an argument.
    """
    app = create_app(registry=registry, db_engine=db_engine)
    assert app.state.scheduler_factory is no_scheduler
    assert app.state.scheduler is None
    async with app.router.lifespan_context(app):
        assert app.state.scheduler is None
        assert _context_tasks() == []


def test_both_shipped_apps_run_the_context_scheduler() -> None:
    """``app`` and ``dev_app`` both opt in -- see ``dev_app``'s docstring for why.

    Recorded so the shipped choice is assertable without starting anything:
    the opt-in is the line that can go missing, and an app whose feeds all
    read *stale* because nothing ever polled them is the failure this catches.
    """
    from corollary.api.app import app as shipped
    from corollary.api.app import dev_app as dev

    assert shipped.state.scheduler_factory is build_context_scheduler
    assert dev.state.scheduler_factory is build_context_scheduler


@pytest.mark.asyncio
async def test_a_scheduler_that_cannot_be_built_leaves_the_app_serving(
    registry: ServiceRegistry, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Stated, never fatal -- the standing the sockets and the database have.

    And never a halt: a context scheduler that failed to start is not rule 9's
    business, so ``engine_state`` is exactly what it would have been.
    """

    # The control: the same database through a lifespan with no scheduler.
    # What a cold start leaves in ``engine_state`` is exactly this.
    control = create_app(registry=registry, db_engine=db_engine)
    async with control.router.lifespan_context(control):
        expected = _halt_state(db_engine)
    assert expected[0] is True  # every cold start comes up halted

    def broken(services: ContextServices, secrets: object) -> None:
        raise RuntimeError("no calendar today")

    app = create_app(registry=registry, db_engine=db_engine, scheduler=broken)  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="corollary.api.app"):
        async with app.router.lifespan_context(app):
            assert app.state.scheduler is None
            assert app.state.engine_runtime is not None
            assert _halt_state(db_engine) == expected

    not_started = [
        r for r in caplog.records
        if getattr(r, "event", None) == "context_scheduler_not_started"
    ]
    assert len(not_started) == 1
    assert not_started[0].levelno == logging.ERROR
    assert getattr(not_started[0], "error_type") == "RuntimeError"


@pytest.mark.asyncio
async def test_a_scheduler_task_that_died_does_not_skip_the_rest_of_shutdown(
    registry: ServiceRegistry,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The scheduler closes first; nothing it does may cut the shutdown short.

    A job task that ended in a non-cancellation exception used to be
    re-raised out of ``aclose``, skipping the supervisor, the runtime, the
    Discord sink and the registry -- so queued rule 9 alerts got no grace and
    no ``dropped`` row. The registry closes last, so its ``aclose`` running
    proves every step before it ran too.
    """
    closed: list[str] = []
    original_runtime_aclose = EngineRuntime.aclose

    async def runtime_aclose(self: EngineRuntime) -> None:
        closed.append("runtime")
        await original_runtime_aclose(self)

    original_registry_aclose = registry.aclose

    async def registry_aclose() -> None:
        closed.append("registry")
        await original_registry_aclose()

    monkeypatch.setattr(EngineRuntime, "aclose", runtime_aclose)
    monkeypatch.setattr(registry, "aclose", registry_aclose)

    async def body() -> None:
        return None

    async def broken_sleep(seconds: float) -> None:
        raise RuntimeError("the sleeper broke")

    def doomed(
        services: ContextServices, secrets: Callable[[], Sequence[str]]
    ) -> Scheduler:
        return Scheduler(
            [
                ScheduledJob(
                    name="doomed",
                    schedule=EveryWhileOpen(timedelta(minutes=15)),
                    run=body,
                    rule="test",
                )
            ],
            secrets=secrets,
            clock=lambda: WED_1500_ET,
            sleep=broken_sleep,
        )

    app = create_app(registry=registry, db_engine=db_engine, scheduler=doomed)
    with caplog.at_level(logging.ERROR, logger="corollary.engine.scheduler"):
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app.state.scheduler.ready(), timeout=10)
            for _ in range(50):
                await asyncio.sleep(0)
            assert [
                r for r in caplog.records
                if getattr(r, "event", None) == "context_job_task_died"
            ]
    assert closed == ["runtime", "registry"]
    assert _context_tasks() == []


def _halt_state(engine: Engine) -> tuple[bool, str | None, datetime | None]:
    with Session(engine) as session:
        state = session.get(EngineState, ENGINE_STATE_ID)
        assert state is not None
        return (state.halted, state.halted_reason, state.halted_at)
