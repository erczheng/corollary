"""Driving a lifespan's context scheduler on a ``FakeClock``: hang guards per step, never per run.

The pattern is ``tests/engine/test_scheduler.py``'s -- ``FakeClock``,
``HANG_GUARD_SECONDS``, ``MAX_DRIVER_STEPS`` and ``run_until_parked`` --
copied here rather than reinvented, and that file is where the reasons are
written down. In short:

* **A hang guard bounds one unit of work, never a run.** ``FakeClock.drive``
  bounds each step by its ``hang_guard`` and the run by a step *count*. A
  wall-clock budget on the whole run fails a correct run on a loaded machine
  with no assertion failing -- a flake, not a finding. These API tests had
  exactly that: 10 to 30 s for the whole drive, ``ready()`` included.
* **The driver is awaited, never raced.** Its own ``AssertionError`` names
  the instant and every live job with where it is suspended. Racing
  ``clock.parked`` against a timeout and then cancelling the driver reported
  the same hang as an anonymous ``TimeoutError`` and threw that message away.
* **``scheduler.ready()`` -- the calendar build, once per process -- is
  bounded by the default :data:`HANG_GUARD_SECONDS`,** never by a test's
  tighter guard: the first test to drive pays ~1-2 s for the build.

These are for a scheduler something else starts and closes -- usually the
app's lifespan -- so unlike ``run_until_parked`` nothing here starts or
closes it. Not a test module (no ``test_`` prefix), so pytest does not
collect it; ``tests/api/test_scheduler_drive.py`` tests it.
"""

import asyncio
from collections.abc import Callable

from corollary.engine.scheduler import Scheduler
from tests.engine.test_scheduler import HANG_GUARD_SECONDS, FakeClock, _suspended_at

__all__ = ["HANG_GUARD_SECONDS", "await_ready", "drive_until_parked", "live_jobs", "until"]


def live_jobs() -> str:
    """Every live ``context-job:`` task and where it is suspended, one per line. For a hang report."""
    tasks = FakeClock._live_jobs()
    if not tasks:
        return "  (none)"
    return "\n".join(f"  {task.get_name()}: {_suspended_at(task)}" for task in tasks)


async def await_ready(scheduler: Scheduler) -> None:
    """``scheduler.ready()`` within :data:`HANG_GUARD_SECONDS`, or a named ``AssertionError``."""
    try:
        await asyncio.wait_for(scheduler.ready(), timeout=HANG_GUARD_SECONDS)
    except TimeoutError:
        raise AssertionError(
            f"the scheduler's calendar build did not finish within "
            f"{HANG_GUARD_SECONDS:.0f}s of wall-clock time (scheduler.ready()). "
            f"Live context jobs, and where each is suspended:\n{live_jobs()}"
        ) from None


async def drive_until_parked(scheduler: Scheduler, clock: FakeClock) -> None:
    """Wait for ``ready()``, then drive ``clock`` until it parks -- the driver awaited directly.

    No budget on the drive as a whole: each step is bounded by
    ``clock.hang_guard`` (when the clock waits for threads) and the run by
    ``clock.max_steps``. A hang raises the driver's own ``AssertionError``,
    naming the instant, the stuck job and where it is suspended.
    """
    await await_ready(scheduler)
    await clock.drive()


async def until(
    condition: Callable[[], bool],
    what: str,
    *,
    hang_guard: float = HANG_GUARD_SECONDS,
) -> None:
    """Poll ``condition`` until it holds, failing past ``hang_guard`` with the live jobs named.

    For a wait the fake clock does not see -- a woken run, a failed
    catch-up -- whose work runs on worker threads. Each call waits for one
    unit of work, so ``hang_guard`` is a hang guard, not a timing budget:
    exceeding it means a job is stuck, and the ``AssertionError`` says
    ``what`` was awaited and where every live context job is suspended.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + hang_guard
    while not condition():
        if loop.time() >= deadline:
            raise AssertionError(
                f"{what}: not reached within {hang_guard:g} s of wall time. One wait is "
                "one unit of work, so this is a stuck job, not a slow machine. Live "
                f"context jobs, and where each is suspended:\n{live_jobs()}"
            )
        await asyncio.sleep(0.005)
