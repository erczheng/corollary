"""The ``sentiment_audit`` start-up catch-up: a missed Saturday runs once, from the rows.

The catch-up decides from ``audit_run``, never process memory, so a restart
cannot double-run it; it defers to the session's close (the calendar's, so a
half-day is 13:00 ET) when it would otherwise fire in session; it waits,
bounded, for the paper positions' first read; and it runs the scheduled
body, so its run is ``trigger = 'scheduled'`` and never the manual kind.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.calendars import nyse_session_close
from corollary.data.news.audit_runner import ABANDONED_RUN_AFTER
from corollary.db.models import AuditRun, Base
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine.scheduler import (
    SENTIMENT_AUDIT_CATCH_UP_IMMINENT,
    SENTIMENT_AUDIT_CATCH_UP_POSITIONS_WAIT,
    SENTIMENT_AUDIT_JOB,
    ContextServices,
    HeldPositionUnderlyings,
    JobSkipped,
    PositionUnderlyings,
    Scheduler,
    ScheduledJob,
    context_jobs,
    latest_sentiment_audit_slot,
)
from tests.api.news_support import make_seed
from tests.data.news.test_audit_runner import FakeBars, _the_week
from tests.engine.test_scheduler import (
    FakeClock,
    _state_snapshot,
    _WatchedRuntime,
    no_secrets,
    run_until_parked,
)

UTC = timezone.utc

#: Saturday 21 Nov 2026, 10:00 ET (EST, so 15:00 UTC): the slot every case
#: below catches up on. ``_the_week``'s labels are the Monday before it.
SLOT = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)
#: Sunday 22 Nov 2026, 07:00 ET: no session, so the catch-up runs at once.
SUNDAY = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
#: Monday 23 Nov 2026, 11:00 ET: in a full session, which closes 16:00 ET.
MONDAY_IN_SESSION = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)
#: Friday 27 Nov 2026, 11:00 ET: the day after Thanksgiving, a half-day.
HALF_DAY_IN_SESSION = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "audit_catch_up.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
    yield engine
    engine.dispose()


class SteppedClock:
    """A clock that moves only when slept on. Records every sleep.

    ``on_sleep`` runs after each sleep has moved the clock -- the place a
    test makes something happen "while the catch-up waited".
    """

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []
        self.on_sleep: Callable[[], None] | None = None

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.on_sleep is not None:
            self.on_sleep()


def _services(engine: Engine, bars: object | None, **overrides: object) -> ContextServices:
    fields: dict[str, object] = {
        "funds": (),
        "session_factory": lambda: Session(engine),
        "alpaca": bars,
        "markets": ("AAPL", "MSFT"),
        "seed_loader": make_seed,
    }
    fields.update(overrides)
    return ContextServices(**fields)  # type: ignore[arg-type]


def _job(services: ContextServices, clock: SteppedClock) -> ScheduledJob:
    jobs = {job.name: job for job in context_jobs(services, clock=clock, sleep=clock.sleep)}
    return jobs[SENTIMENT_AUDIT_JOB]


def _catch_up(services: ContextServices, clock: SteppedClock) -> JobSkipped | None:
    catch_up = _job(services, clock).catch_up
    assert catch_up is not None
    return asyncio.run(catch_up())


def _runs(engine: Engine) -> list[tuple[str, str, datetime]]:
    with Session(engine) as session:
        return [
            (r.trigger, r.status, r.started_at)
            for r in session.scalars(select(AuditRun).order_by(AuditRun.id))
        ]


def _insert_run(engine: Engine, *, started_at: datetime, status: str) -> None:
    with Session(engine) as session:
        session.add(
            AuditRun(
                started_at=started_at,
                finished_at=None if status == "running" else started_at + timedelta(minutes=3),
                trigger="scheduled",
                status=status,
                detail=None,
            )
        )
        session.commit()


def _decisions(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == "sentiment_audit_catch_up"]


# ------------------------------------------------------------- the slot itself


def test_the_latest_slot_is_the_newest_saturday_1000_eastern_at_or_before_now() -> None:
    # A Monday's latest slot is the Saturday before it.
    assert latest_sentiment_audit_slot(MONDAY_IN_SESSION) == SLOT
    # At the slot exactly, the slot has passed: the scheduled loop's
    # ``next_run`` is strictly after, so it would otherwise be missed.
    assert latest_sentiment_audit_slot(SLOT) == SLOT
    # A moment before it, last week's.
    assert latest_sentiment_audit_slot(SLOT - timedelta(microseconds=1)) == SLOT - timedelta(
        days=7
    )
    # Later the same Saturday, the same slot.
    assert latest_sentiment_audit_slot(SLOT + timedelta(hours=8)) == SLOT
    # July is EDT: 10:00 ET is 14:00 UTC, and Friday 3 July 2026 being a
    # holiday moves nothing.
    assert latest_sentiment_audit_slot(datetime(2026, 7, 6, 12, 0, tzinfo=UTC)) == datetime(
        2026, 7, 4, 14, 0, tzinfo=UTC
    )


def test_the_latest_slot_refuses_a_naive_instant() -> None:
    with pytest.raises(ValueError):
        latest_sentiment_audit_slot(datetime(2026, 11, 23, 12, 0))


# ------------------------------------------------------------ run, or not


def test_the_job_has_a_catch_up(db_engine: Engine) -> None:
    assert _job(_services(db_engine, None), SteppedClock(SUNDAY)).catch_up is not None


def test_a_missed_saturday_runs_once_at_start_and_a_restart_does_not_run_it_again(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    _the_week(db_engine)
    services = _services(db_engine, FakeBars())
    clock = SteppedClock(SUNDAY)
    caplog.set_level(logging.INFO, logger="corollary.engine.scheduler")

    assert _catch_up(services, clock) is None
    # Out of session: at once, no sleep, the scheduled kind.
    assert clock.slept == []
    assert _runs(db_engine) == [("scheduled", "succeeded", SUNDAY)]
    # Its notice is the scheduled run's (the demotion), queued on the context
    # outbox; nothing announces an operator request -- that is the manual
    # POST's, and this is not one.
    assert [n.event for n in services.notices.drain()] == ["sentiment_demoted"]

    # A restart: a fresh job set over the same rows. Nothing missing, nothing run.
    bars = FakeBars()
    restarted = _services(db_engine, bars)
    outcome = _catch_up(restarted, SteppedClock(SUNDAY + timedelta(hours=1)))
    assert isinstance(outcome, JobSkipped) and "already ran" in outcome.reason
    assert len(_runs(db_engine)) == 1
    assert bars.calls == []

    decisions = _decisions(caplog)
    assert [r.decision for r in decisions] == [  # type: ignore[attr-defined]
        "run_now",
        "ran",
        "not_needed",
    ]
    first, ran, second = decisions
    assert ran.levelno == logging.INFO
    assert first.slot == SLOT.isoformat()  # type: ignore[attr-defined]
    assert first.last_run_started is None  # type: ignore[attr-defined]
    assert first.at == SUNDAY.isoformat()  # type: ignore[attr-defined]
    assert second.last_run_started == SUNDAY.isoformat()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("started_at", "status"),
    [
        (SLOT, "succeeded"),
        (SLOT + timedelta(minutes=1), "failed"),
        # Another process's live run: younger than the abandonment bound.
        (SUNDAY - ABANDONED_RUN_AFTER, "running"),
    ],
    ids=["succeeded_at_the_slot", "failed_after_it", "running_young"],
)
def test_a_run_at_or_after_the_slot_means_nothing_is_missing(
    db_engine: Engine, started_at: datetime, status: str
) -> None:
    _the_week(db_engine)
    _insert_run(db_engine, started_at=started_at, status=status)
    bars = FakeBars()
    outcome = _catch_up(_services(db_engine, bars), SteppedClock(SUNDAY))
    assert isinstance(outcome, JobSkipped) and "already ran" in outcome.reason
    assert len(_runs(db_engine)) == 1
    assert bars.calls == []


@pytest.mark.parametrize(
    ("started_at", "status"),
    [
        # Before the slot: last week's run is not this week's.
        (SLOT - timedelta(microseconds=1), "succeeded"),
        # A ``running`` row past the abandonment bound is a dead process's;
        # the runner closes it ``failed`` and runs.
        (SUNDAY - ABANDONED_RUN_AFTER - timedelta(seconds=1), "running"),
    ],
    ids=["succeeded_before_the_slot", "running_abandoned"],
)
def test_a_run_that_does_not_count_leaves_the_slot_missing(
    db_engine: Engine, started_at: datetime, status: str
) -> None:
    _the_week(db_engine)
    _insert_run(db_engine, started_at=started_at, status=status)
    assert _catch_up(_services(db_engine, FakeBars()), SteppedClock(SUNDAY)) is None
    runs = _runs(db_engine)
    assert runs[-1] == ("scheduled", "succeeded", SUNDAY)
    if status == "running":
        assert runs[0][1] == "failed"  # closed as abandoned by the runner


# ------------------------------------------------------- deferral to the close


@pytest.mark.parametrize(
    ("now", "session_day", "close_utc"),
    [
        (MONDAY_IN_SESSION, date(2026, 11, 23), datetime(2026, 11, 23, 21, 0, tzinfo=UTC)),
        # A half-day closes at 13:00 ET: the calendar's close, not 16:00.
        (HALF_DAY_IN_SESSION, date(2026, 11, 27), datetime(2026, 11, 27, 18, 0, tzinfo=UTC)),
    ],
    ids=["full_day", "half_day"],
)
def test_in_session_the_catch_up_defers_to_the_calendars_close(
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    now: datetime,
    session_day: date,
    close_utc: datetime,
) -> None:
    assert nyse_session_close(session_day) == close_utc
    _the_week(db_engine)
    clock = SteppedClock(now)
    bars = FakeBars()

    def no_bars_before_the_close() -> None:
        assert bars.calls == [], "the audit fetched bars during the session"

    clock.on_sleep = no_bars_before_the_close
    caplog.set_level(logging.INFO, logger="corollary.engine.scheduler")

    assert _catch_up(_services(db_engine, bars), clock) is None
    runs = _runs(db_engine)
    assert len(runs) == 1
    trigger, status, started = runs[0]
    assert (trigger, status) == ("scheduled", "succeeded")
    assert started == close_utc
    assert bars.calls, "the deferred run never fetched"

    decisions = _decisions(caplog)
    assert [r.decision for r in decisions] == [  # type: ignore[attr-defined]
        "deferred",
        "run_now",
        "ran",
    ]
    assert decisions[0].until == close_utc.isoformat()  # type: ignore[attr-defined]
    assert decisions[0].at == now.isoformat()  # type: ignore[attr-defined]
    # ``run_now`` is stated when the run starts -- after the wait, not before it.
    assert decisions[1].at == close_utc.isoformat()  # type: ignore[attr-defined]


def test_a_run_during_the_deferral_makes_the_close_a_no_op(db_engine: Engine) -> None:
    _the_week(db_engine)
    clock = SteppedClock(MONDAY_IN_SESSION)
    inserted: list[datetime] = []

    def a_manual_run_lands() -> None:
        if not inserted:
            _insert_run(db_engine, started_at=clock.now, status="succeeded")
            inserted.append(clock.now)

    clock.on_sleep = a_manual_run_lands
    bars = FakeBars()
    outcome = _catch_up(_services(db_engine, bars), clock)
    assert isinstance(outcome, JobSkipped) and "already ran" in outcome.reason
    assert len(_runs(db_engine)) == 1
    assert bars.calls == []


def test_without_a_provider_the_catch_up_skips_without_waiting_for_the_close(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="corollary.engine.scheduler")
    clock = SteppedClock(MONDAY_IN_SESSION)
    outcome = _catch_up(_services(db_engine, None), clock)
    assert isinstance(outcome, JobSkipped) and "SIP minute bars" in outcome.reason
    assert clock.slept == []
    assert _runs(db_engine) == []
    # Never ``run_now``: nothing ran. Said at WARNING, the reason in the
    # message text itself -- lastResort renders the message and drops ``extra``.
    decisions = _decisions(caplog)
    assert [r.decision for r in decisions] == ["skipped_no_provider"]  # type: ignore[attr-defined]
    assert decisions[0].levelno == logging.WARNING
    assert outcome.reason in decisions[0].getMessage()
    assert decisions[0].reason == outcome.reason  # type: ignore[attr-defined]


# ------------------------------------------------------- the imminent slot

#: Saturday 28 Nov 2026, 10:00 ET: the slot after :data:`SLOT`.
NEXT_SLOT = SLOT + timedelta(days=7)


@pytest.mark.parametrize(
    "now",
    [
        # The Task Scheduler launch: 09:45 ET on the Saturday, last week missed.
        NEXT_SLOT - timedelta(minutes=15),
        # The boundary: exactly the window before the slot is still imminent.
        NEXT_SLOT - timedelta(minutes=30),
    ],
    ids=["saturday_0945", "at_the_boundary"],
)
def test_the_catch_up_does_nothing_when_the_next_slot_is_imminent(
    db_engine: Engine, caplog: pytest.LogCaptureFixture, now: datetime
) -> None:
    """Run last week's slot at 09:45 and, still running at 10:00, the loop's
    ``next_run`` is next Saturday: today's slot is lost to a later catch-up.
    The slot's own run grades every matured label anyway, last week's included."""
    assert SENTIMENT_AUDIT_CATCH_UP_IMMINENT == timedelta(minutes=30)
    _the_week(db_engine)
    caplog.set_level(logging.INFO, logger="corollary.engine.scheduler")
    bars = FakeBars()
    clock = SteppedClock(now)
    outcome = _catch_up(_services(db_engine, bars), clock)
    assert isinstance(outcome, JobSkipped) and "imminent" in outcome.reason
    assert _runs(db_engine) == []
    assert bars.calls == [] and clock.slept == []
    decisions = _decisions(caplog)
    assert [r.decision for r in decisions] == [  # type: ignore[attr-defined]
        "not_needed_slot_imminent"
    ]
    # The slot it declined is last Saturday's; ``until`` is the one that covers it.
    assert decisions[0].slot == SLOT.isoformat()  # type: ignore[attr-defined]
    assert decisions[0].until == NEXT_SLOT.isoformat()  # type: ignore[attr-defined]
    assert decisions[0].levelno == logging.INFO


def test_just_outside_the_imminent_window_the_missed_slot_runs(db_engine: Engine) -> None:
    _the_week(db_engine)
    now = NEXT_SLOT - timedelta(minutes=30, microseconds=1)
    assert _catch_up(_services(db_engine, FakeBars()), SteppedClock(now)) is None
    assert _runs(db_engine) == [("scheduled", "succeeded", now)]


# ------------------------------------------------ the existing preconditions


def test_the_catch_up_waits_for_the_paper_positions_first_read(db_engine: Engine) -> None:
    _the_week(db_engine)
    holder = PositionUnderlyings()
    clock = SteppedClock(SUNDAY)

    def the_refresher_reads() -> None:
        if holder.as_of is None:
            holder.replace({"TSLA"}, at=clock.now)

    clock.on_sleep = the_refresher_reads
    services = _services(
        db_engine, FakeBars(), position_underlyings=HeldPositionUnderlyings(holder)
    )
    assert _catch_up(services, clock) is None
    assert len(clock.slept) == 1
    assert [(t, s) for t, s, _ in _runs(db_engine)] == [("scheduled", "succeeded")]


def test_a_holder_never_read_within_the_bound_skips_and_writes_no_run(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="corollary.engine.scheduler")
    _the_week(db_engine)
    clock = SteppedClock(SUNDAY)
    bars = FakeBars()
    services = _services(
        db_engine, bars, position_underlyings=HeldPositionUnderlyings(PositionUnderlyings())
    )
    outcome = _catch_up(services, clock)
    assert isinstance(outcome, JobSkipped)
    assert "have not been read since start-up" in outcome.reason
    # Bounded: it slept the bound and no more.
    assert timedelta(seconds=sum(clock.slept)) == SENTIMENT_AUDIT_CATCH_UP_POSITIONS_WAIT
    assert _runs(db_engine) == []
    assert bars.calls == []
    # The final outcome is stated as well as ``run_now``, at WARNING, the
    # reason in the message itself.
    decisions = _decisions(caplog)
    assert [r.decision for r in decisions] == ["run_now", "skipped"]  # type: ignore[attr-defined]
    assert decisions[1].levelno == logging.WARNING
    assert outcome.reason in decisions[1].getMessage()


def _no_seed() -> None:
    return None


def test_a_degraded_watch_universe_skips_and_the_next_start_retries(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="corollary.engine.scheduler")
    _the_week(db_engine)
    outcome = _catch_up(
        _services(db_engine, FakeBars(), seed_loader=_no_seed), SteppedClock(SUNDAY)
    )
    assert isinstance(outcome, JobSkipped) and "SPDR seed has never been built" in outcome.reason
    skipped = [r for r in _decisions(caplog) if r.decision == "skipped"]  # type: ignore[attr-defined]
    assert len(skipped) == 1 and skipped[0].levelno == logging.WARNING
    assert outcome.reason in skipped[0].getMessage()
    assert _runs(db_engine) == []
    # The next start, with the seed built, finds the slot still missing.
    assert _catch_up(_services(db_engine, FakeBars()), SteppedClock(SUNDAY)) is None
    assert len(_runs(db_engine)) == 1


# --------------------------------------------------------------------- rule 9


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["runs", "defers_then_runs", "not_needed", "imminent"])
@pytest.mark.parametrize("halted_before", [False, True])
async def test_the_audit_catch_up_never_moves_the_halt_state(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    halted_before: bool,
    case: str,
) -> None:
    """Rule 9: the catch-up, in each of its three outcomes, through the real scheduler.

    On a writable database holding the real ``engine_state`` row, watched by
    a real :class:`EngineRuntime` whose every watchdog input is spied on. A
    resume would flip the halted parametrisation, a halt the resumed one.
    """
    watched = _WatchedRuntime(db_engine, monkeypatch, halted=halted_before)
    state_before = _state_snapshot(db_engine)
    _the_week(db_engine)
    start = {
        "defers_then_runs": MONDAY_IN_SESSION,
        # 09:45 ET Saturday, last week missed: the catch-up declines and the
        # 10:00 slot's own run is the one that happens.
        "imminent": NEXT_SLOT - timedelta(minutes=15),
    }.get(case, SUNDAY)
    if case == "not_needed":
        _insert_run(db_engine, started_at=SLOT, status="succeeded")
    clock = FakeClock(start, stop_at=start + timedelta(hours=8), wait_for_threads=True)
    services = _services(db_engine, FakeBars())
    jobs = [j for j in context_jobs(services, clock=clock, sleep=clock.sleep)]
    audit = [j for j in jobs if j.name == SENTIMENT_AUDIT_JOB]
    scheduler = Scheduler(audit, secrets=no_secrets, clock=clock, sleep=clock.sleep)
    await run_until_parked(scheduler, clock, timeout=30)

    status = scheduler.status()[SENTIMENT_AUDIT_JOB]
    assert status.failures == 0, status
    runs = _runs(db_engine)
    if case == "not_needed":
        assert status.skips == 1 and len(runs) == 1
    elif case == "imminent":
        assert status.skips == 1 and status.runs == 1
        assert runs == [("scheduled", "succeeded", NEXT_SLOT)]
    else:
        assert status.runs == 1 and runs[-1][:2] == ("scheduled", "succeeded")
    if case == "defers_then_runs":
        assert runs[-1][2] == datetime(2026, 11, 23, 21, 0, tzinfo=UTC)

    assert _state_snapshot(db_engine) == state_before
    watched.assert_untouched()
