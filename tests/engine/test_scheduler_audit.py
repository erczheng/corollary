"""The ``sentiment_audit`` context job (unit 6.4a): its slot, its body, its entry point.

Rule 9's isolation for this job is ``test_scheduler.py``'s
``test_the_shipped_context_jobs_never_move_the_halt_state``, whose window
runs to Saturday 10:00 ET so this job's real body runs there with the rest.
"""

import asyncio
from collections.abc import Callable, Iterator
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.deps import POSITION_UNDERLYINGS_TTL
from corollary.data.news import audit_runner
from corollary.data.seeds import SeedError
from corollary.db.models import AuditRun, Base, LabelOutcome
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine import scheduler
from corollary.engine.scheduler import (
    SENTIMENT_AUDIT_AT,
    SENTIMENT_AUDIT_DAYS,
    SENTIMENT_AUDIT_JOB,
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


def _audit_job(
    services: ContextServices, clock: Callable[[], datetime] | None = None
) -> ScheduledJob:
    jobs = {
        job.name: job
        for job in context_jobs(services, clock=clock or TickingClock(SATURDAY_10))
    }
    return jobs[SENTIMENT_AUDIT_JOB]


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


def _held(refreshed: bool, *, at: datetime = SATURDAY_10) -> HeldPositionUnderlyings:
    holder = PositionUnderlyings()
    if refreshed:
        holder.replace({"TSLA"}, at=at)
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


# ------------------------------- follow-up F3: stale positions stop the run too


def _frozen() -> datetime:
    """Every reading the same instant -- the runner bumps ``finished_at`` itself."""
    return SATURDAY_10


def test_the_staleness_bound_is_three_of_the_refreshers_intervals() -> None:
    """The scheduler may not import ``corollary.api`` (the context jobs' import
    scan), so it mirrors the refresher's interval; this pins the mirror."""
    assert scheduler._SENTIMENT_AUDIT_POSITIONS_REFRESH == POSITION_UNDERLYINGS_TTL
    assert scheduler._SENTIMENT_AUDIT_POSITIONS_STALE_AFTER == 3 * POSITION_UNDERLYINGS_TTL


def test_positions_exactly_at_the_staleness_bound_still_run(db_engine: Engine) -> None:
    _the_week(db_engine)
    as_of = SATURDAY_10 - 3 * POSITION_UNDERLYINGS_TTL
    services = _services(db_engine, FakeBars(), position_underlyings=_held(True, at=as_of))
    assert asyncio.run(_audit_job(services, _frozen).run()) is None
    with Session(db_engine) as session:
        runs = [(r.trigger, r.status) for r in session.scalars(select(AuditRun))]
    assert runs == [("scheduled", "succeeded")]


def test_positions_past_the_staleness_bound_skip_and_write_no_run(db_engine: Engine) -> None:
    """A holder whose refresher has failed since Tuesday would classify
    Saturday's labels against Tuesday's positions, permanently."""
    _the_week(db_engine)
    as_of = SATURDAY_10 - 3 * POSITION_UNDERLYINGS_TTL - timedelta(microseconds=1)
    services = _services(db_engine, FakeBars(), position_underlyings=_held(True, at=as_of))
    bars = services.alpaca
    assert isinstance(bars, FakeBars)
    reason = "the paper positions were last read more than"

    outcome = asyncio.run(_audit_job(services, _frozen).run())
    assert isinstance(outcome, JobSkipped) and reason in outcome.reason
    with pytest.raises(SentimentAuditUnavailable, match=reason):
        asyncio.run(run_sentiment_audit_now(services, trigger="manual", clock=_frozen))
    with Session(db_engine) as session:
        assert session.scalars(select(AuditRun)).all() == []
        assert session.scalars(select(LabelOutcome)).all() == []
    assert bars.calls == []


# ------------------------------ unit 6.4b fix: on_opened, and one job name


def test_run_now_threads_on_opened_and_calls_it_with_the_run_id(db_engine: Engine) -> None:
    _the_week(db_engine)
    services = _services(db_engine, FakeBars())
    seen: list[tuple[int, str]] = []

    def on_opened(run_id: int) -> None:
        with Session(db_engine) as session:
            row = session.get(AuditRun, run_id)
            assert row is not None
            seen.append((run_id, row.status))

    result = asyncio.run(
        run_sentiment_audit_now(
            services, trigger="manual", clock=TickingClock(SATURDAY_10), on_opened=on_opened
        )
    )
    assert seen == [(result.run_id, "running")]


def test_run_now_never_calls_on_opened_for_a_run_that_never_opened(db_engine: Engine) -> None:
    seen: list[int] = []
    # No provider: refused before the runner.
    with pytest.raises(SentimentAuditUnavailable):
        asyncio.run(
            run_sentiment_audit_now(
                _services(db_engine, None), trigger="manual", on_opened=seen.append
            )
        )
    # A degraded universe: refused before any row.
    degraded = _services(db_engine, FakeBars(), seed_loader=_no_seed)
    with pytest.raises(SentimentAuditUnavailable):
        asyncio.run(run_sentiment_audit_now(degraded, trigger="manual", on_opened=seen.append))
    assert seen == []


def test_the_sentiment_audit_job_name_is_one_shipped_constant(db_engine: Engine) -> None:
    """The route serves this job's next slot by name; a rename here must reach it."""
    from corollary.api.routes import sentiment as sentiment_routes

    names = [job.name for job in context_jobs(_services(db_engine, None))]
    assert names.count(SENTIMENT_AUDIT_JOB) == 1
    assert sentiment_routes.SENTIMENT_AUDIT_JOB is SENTIMENT_AUDIT_JOB
    # ``is`` alone proves little -- CPython interns identifier-like literals --
    # so the route must hold no copy of the name of its own.
    import inspect
    import re

    source = inspect.getsource(sentiment_routes)
    assert not re.search(rf"[\"']{SENTIMENT_AUDIT_JOB}[\"']", source)
