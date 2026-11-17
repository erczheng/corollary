"""``ScheduledJob.replan``: a sleeping job re-asks its schedule when told it changed (handoff item D).

The shipped case -- ``calendar_releases`` storing a release while
``econ_first_prints`` sleeps -- is ``tests/engine/test_econ_first_prints_job.py``.
This file is the scheduler's guarantees about a re-plan, on a schedule the
test edits by hand:

* an earlier answer replaces the slot the job was sleeping toward;
* a later one changes nothing -- a planned slot is never delayed or skipped;
* a re-plan runs nothing by itself;
* a schedule that cannot answer keeps the slot, and says so;
* a re-plan and a Cash wake on the same job each do their own thing.

Every run is driven by ``tests/engine/test_scheduler.py``'s ``FakeClock``
through ``run_until_parked``, so a hang fails with the stuck job named.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from corollary.engine.scheduler import (
    JobBody,
    JobSkipped,
    JobWake,
    ReleaseAttemptPlan,
    ScheduledJob,
    Scheduler,
)
from tests.engine.test_scheduler import (
    WED_1500_ET,
    FakeClock,
    Recorder,
    no_secrets,
    run_until_parked,
)

T0 = WED_1500_ET


class Slots:
    """A schedule answering from a list the test edits: the first listed slot after ``after``."""

    def __init__(self, *slots: datetime) -> None:
        self.slots = sorted(slots)
        self.raises = False

    def next_run(self, after: datetime) -> datetime | None:
        if self.raises:
            raise RuntimeError("the schedule cannot answer")
        return next((slot for slot in self.slots if slot > after), None)

    def describe(self) -> str:
        return "a test's hand-edited slots"


def _doing(name: str, at: datetime, action: Callable[[], None]) -> ScheduledJob:
    """A job that runs ``action`` once, at ``at``: how a test edits a schedule mid-run."""

    async def body() -> JobSkipped | None:
        action()
        return None

    run: JobBody = body
    return ScheduledJob(name=name, schedule=Slots(at), run=run, rule="test")


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == event]


@pytest.mark.asyncio
async def test_an_earlier_answer_replaces_the_slot_and_runs_nothing_extra(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(T0, stop_at=T0 + timedelta(hours=3))
    slots = Slots(T0 + timedelta(hours=2))
    replan = asyncio.Event()
    late = Recorder(clock)

    def add_an_earlier_slot() -> None:
        slots.slots = sorted([*slots.slots, T0 + timedelta(hours=1)])
        replan.set()

    scheduler = Scheduler(
        [
            ScheduledJob(name="late", schedule=slots, run=late, rule="test", replan=replan),
            _doing("editor", T0 + timedelta(minutes=30), add_an_earlier_slot),
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        await run_until_parked(scheduler, clock)

    # The new, earlier slot, then the one already planned: each runs once,
    # and nothing runs at the moment of the re-plan itself.
    assert late.ran == [T0 + timedelta(hours=1), T0 + timedelta(hours=2)]
    (record,) = _events(caplog, "context_job_replanned")
    assert record.__dict__["job"] == "late"
    assert record.__dict__["was"] == (T0 + timedelta(hours=2)).isoformat()
    assert record.__dict__["now_due"] == (T0 + timedelta(hours=1)).isoformat()
    assert record.__dict__["at"] == (T0 + timedelta(minutes=30)).isoformat()


@pytest.mark.asyncio
async def test_a_later_answer_never_delays_or_skips_the_planned_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The planned slot is taken out of the list and a later one put in: it still runs."""
    clock = FakeClock(T0, stop_at=T0 + timedelta(hours=3))
    planned, later = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    slots = Slots(planned)
    replan = asyncio.Event()
    job = Recorder(clock)

    def move_it_later() -> None:
        slots.slots = [later]
        replan.set()

    scheduler = Scheduler(
        [
            ScheduledJob(name="job", schedule=slots, run=job, rule="test", replan=replan),
            _doing("editor", T0 + timedelta(minutes=30), move_it_later),
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        await run_until_parked(scheduler, clock)

    assert job.ran == [planned, later]
    assert _events(caplog, "context_job_replanned") == []


@pytest.mark.asyncio
async def test_a_schedule_that_cannot_answer_a_replan_keeps_its_slot_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(T0, stop_at=T0 + timedelta(hours=1, minutes=30))
    planned = T0 + timedelta(hours=1)
    slots = Slots(planned)
    replan = asyncio.Event()
    job = Recorder(clock)

    def break_it() -> None:
        slots.raises = True
        replan.set()

    def mend_it() -> None:
        slots.raises = False

    scheduler = Scheduler(
        [
            ScheduledJob(name="job", schedule=slots, run=job, rule="test", replan=replan),
            _doing("breaker", T0 + timedelta(minutes=30), break_it),
            _doing("mender", T0 + timedelta(minutes=45), mend_it),
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    with caplog.at_level(logging.WARNING, logger="corollary.engine.scheduler"):
        await run_until_parked(scheduler, clock)

    assert job.ran == [planned]
    (record,) = _events(caplog, "context_job_replan_failed")
    assert record.__dict__["job"] == "job"
    assert record.__dict__["kept"] == planned.isoformat()
    assert record.__dict__["error_type"] == "RuntimeError"
    assert scheduler.status()["job"].failures == 0  # a kept slot is not a failed run


@pytest.mark.asyncio
async def test_a_wake_and_a_replan_on_one_job_each_do_their_own_thing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At the same instant: the wake runs the woken body once, the re-plan moves the slot."""
    clock = FakeClock(T0, stop_at=T0 + timedelta(hours=3))
    slots = Slots(T0 + timedelta(hours=2))
    replan = asyncio.Event()
    scheduled = Recorder(clock)
    woken = Recorder(clock)
    wakes: list[JobWake] = []

    def add_a_slot_and_wake() -> None:
        slots.slots = sorted([*slots.slots, T0 + timedelta(hours=1)])
        replan.set()
        wakes[0]()

    scheduler = Scheduler(
        [
            ScheduledJob(
                name="both",
                schedule=slots,
                run=scheduled,
                rule="test",
                woken=woken,
                replan=replan,
            ),
            _doing("editor", T0 + timedelta(minutes=30), add_a_slot_and_wake),
        ],
        secrets=no_secrets,
        clock=clock,
        sleep=clock.sleep,
    )
    wakes.append(scheduler.waker(["both"]))
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        await run_until_parked(scheduler, clock)

    assert woken.ran == [T0 + timedelta(minutes=30)]
    assert scheduled.ran == [T0 + timedelta(hours=1), T0 + timedelta(hours=2)]
    assert len(_events(caplog, "context_job_replanned")) == 1
    assert scheduler.status()["both"].woken_runs == 1


def test_the_plan_flags_only_an_instant_it_did_not_already_hold() -> None:
    t1 = datetime(2026, 11, 18, 13, 30, tzinfo=UTC)
    t2 = datetime(2026, 11, 19, 13, 30, tzinfo=UTC)
    plan = ReleaseAttemptPlan()

    plan.replace([t1])
    assert not plan.changed.is_set()  # the job's own run never flags a change
    plan.add([t1])
    assert not plan.changed.is_set()  # nothing new
    plan.add([t2, t1])
    assert plan.changed.is_set()
    assert plan.pending == (t1, t2)
    with pytest.raises(ValueError):
        plan.add([datetime(2026, 11, 20, 8, 30)])  # naive: refused, as replace refuses it


def test_an_instant_added_during_a_run_survives_that_runs_replace() -> None:
    """The run's answer was read before the addition, so it cannot be the whole truth."""
    t1 = datetime(2026, 11, 18, 13, 30, tzinfo=UTC)
    t2 = datetime(2026, 11, 19, 13, 30, tzinfo=UTC)
    plan = ReleaseAttemptPlan()

    plan.begin()
    plan.add([t2])  # the refresh, while the run fetches
    plan.replace([t1])  # the run's answer, read before it
    assert plan.pending == (t1, t2)

    # Outside a run, the run's answer is the whole plan again.
    plan.replace([t1])
    assert plan.pending == (t1,)
