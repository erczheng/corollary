"""The ``sentiment_audit`` context job (unit 6.4a): its slot, its body, its entry point.

Rule 9's isolation for this job is ``test_scheduler.py``'s
``test_the_shipped_context_jobs_never_move_the_halt_state``, whose window
runs to Saturday 10:00 ET so this job's real body runs there with the rest.
"""

import asyncio
from collections.abc import Iterator
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.data.news import audit_runner
from corollary.data.seeds import SeedError
from corollary.db.models import AuditRun, Base, LabelOutcome
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine.scheduler import (
    SENTIMENT_AUDIT_AT,
    SENTIMENT_AUDIT_DAYS,
    AtTime,
    ContextServices,
    HeldPositionUnderlyings,
    JobSkipped,
    PositionUnderlyings,
    ScheduledJob,
    SentimentAuditUnavailable,
    context_jobs,
    run_sentiment_audit_now,
)
from corollary.ratelimit import ALPACA_DATA_HOST
from tests.api.news_support import make_seed
from tests.data.news.test_audit_runner import SATURDAY_10, FakeBars, TickingClock, _the_week

UTC = timezone.utc


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "scheduler_audit.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
    yield engine
    engine.dispose()


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


def _audit_job(services: ContextServices, clock: TickingClock | None = None) -> ScheduledJob:
    jobs = {
        job.name: job
        for job in context_jobs(services, clock=clock or TickingClock(SATURDAY_10))
    }
    return jobs["sentiment_audit"]


def test_the_audit_runs_saturday_at_1000_eastern(db_engine: Engine) -> None:
    job = _audit_job(_services(db_engine, None))
    assert job.schedule == AtTime(SENTIMENT_AUDIT_AT, SENTIMENT_AUDIT_DAYS)
    assert SENTIMENT_AUDIT_AT == time(10, 0)
    assert job.catch_up is None
    assert job.inputs["host"] == ALPACA_DATA_HOST
    schedule = job.schedule
    # November (EST): 10:00 ET is 15:00 UTC.
    assert schedule.next_run(datetime(2026, 11, 20, 17, 0, tzinfo=UTC)) == SATURDAY_10
    # At the slot exactly, the next is a week on.
    assert schedule.next_run(SATURDAY_10) == datetime(2026, 11, 28, 15, 0, tzinfo=UTC)
    # July (EDT): 14:00 UTC -- and Friday 3 July 2026 is a market holiday,
    # which moves nothing: the week is the clock.
    assert schedule.next_run(datetime(2026, 7, 2, 12, 0, tzinfo=UTC)) == datetime(
        2026, 7, 4, 14, 0, tzinfo=UTC
    )
    assert schedule.describe().startswith("at 10:00 ET")


def test_without_a_market_data_provider_the_audit_skips(db_engine: Engine) -> None:
    services = _services(db_engine, None)
    outcome = asyncio.run(_audit_job(services).run())
    assert isinstance(outcome, JobSkipped) and "SIP minute bars" in outcome.reason
    with pytest.raises(SentimentAuditUnavailable):
        asyncio.run(run_sentiment_audit_now(services, trigger="manual"))
    with Session(db_engine) as session:
        assert session.scalars(select(AuditRun)).all() == []


def test_the_job_runs_scheduled_and_queues_its_notice_on_the_outbox(db_engine: Engine) -> None:
    _the_week(db_engine)
    services = _services(db_engine, FakeBars())
    clock = TickingClock(SATURDAY_10)
    assert asyncio.run(_audit_job(services, clock).run()) is None

    # The demotion's notice is on the context outbox, for the lifespan to
    # route at emission; the unaudited -> active promotion queued nothing.
    queued = services.notices.drain()
    assert [(n.event, n.severity, n.account) for n in queued] == [
        ("sentiment_demoted", "warning", None)
    ]
    with Session(db_engine) as session:
        runs = [(r.trigger, r.status) for r in session.scalars(select(AuditRun).order_by(AuditRun.id))]
    assert runs == [("scheduled", "succeeded")]

    # The manual entry point (unit 6.4b's POST) records its own trigger.
    result = asyncio.run(run_sentiment_audit_now(services, trigger="manual", clock=clock))
    assert result.transitions == ()
    with Session(db_engine) as session:
        run = session.get(AuditRun, result.run_id)
        assert run is not None and (run.trigger, run.status) == ("manual", "succeeded")


def test_a_run_in_progress_makes_the_scheduled_slot_a_skip(db_engine: Engine) -> None:
    services = _services(db_engine, FakeBars())
    assert audit_runner._RUN_LOCK.acquire(blocking=False)
    try:
        outcome = asyncio.run(_audit_job(services).run())
    finally:
        audit_runner._RUN_LOCK.release()
    assert isinstance(outcome, JobSkipped) and "in progress" in outcome.reason


# ------------------------------------- fix B: a degraded universe never runs


def _held(refreshed: bool) -> HeldPositionUnderlyings:
    holder = PositionUnderlyings()
    if refreshed:
        holder.replace({"TSLA"}, at=datetime(2026, 11, 20, 15, 0, tzinfo=UTC))
    return HeldPositionUnderlyings(holder)


def _bad_seed() -> None:
    raise SeedError("SYNTHETIC malformed seed")


def _no_seed() -> None:
    return None


async def _positions_fail() -> frozenset[str]:
    raise RuntimeError("SYNTHETIC broker read failed")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"position_underlyings": _held(refreshed=False)}, "have not been read since start-up"),
        ({"position_underlyings": _positions_fail}, "position underlyings could not be read"),
        ({"seed_loader": _bad_seed}, "SPDR seed did not parse"),
        ({"seed_loader": _no_seed}, "SPDR seed has never been built"),
    ],
    ids=["positions_never_read", "positions_error", "seed_error", "seed_missing"],
)
def test_a_degraded_watch_universe_skips_and_writes_no_run(
    db_engine: Engine, overrides: dict[str, object], reason: str
) -> None:
    _the_week(db_engine)
    services = _services(db_engine, FakeBars(), **overrides)
    bars = services.alpaca
    assert isinstance(bars, FakeBars)

    outcome = asyncio.run(_audit_job(services).run())
    assert isinstance(outcome, JobSkipped) and reason in outcome.reason
    assert "SYNTHETIC" not in outcome.reason

    with pytest.raises(SentimentAuditUnavailable, match=reason):
        asyncio.run(run_sentiment_audit_now(services, trigger="manual"))
    with Session(db_engine) as session:
        assert session.scalars(select(AuditRun)).all() == []
        assert session.scalars(select(LabelOutcome)).all() == []
    assert bars.calls == []


def test_a_refreshed_holder_runs(db_engine: Engine) -> None:
    _the_week(db_engine)
    services = _services(db_engine, FakeBars(), position_underlyings=_held(refreshed=True))
    assert asyncio.run(_audit_job(services).run()) is None
    with Session(db_engine) as session:
        runs = [(r.trigger, r.status) for r in session.scalars(select(AuditRun))]
    assert runs == [("scheduled", "succeeded")]
