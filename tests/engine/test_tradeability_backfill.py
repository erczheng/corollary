"""Owner decision Q25 (d), 2026-11-16: the daily tradeability job fills decision 27's need.

The ``tradeability_cache`` job checks today's signalled off-watch tickers, as
before, and then backfills ``(ticker, S)`` for off-watch labels whose
reference session S has no verdict on or before S -- the need the weekly
audit computes (``audit_runner.load_verdicts_needed``), filled through the
same ``refresh_tradeability(session_date=S)`` call, oldest first.

**The bound** (``TRADEABILITY_BACKFILL_BOUND``): the job as a whole spends
no more ``paper-api.`` requests a run than the refresh's own per-run bound,
``MAX_TICKERS_PER_RUN`` -- the backfill gets what today's refresh left of it.

Rule 9: a failure here is logged with the rule, the inputs and the
timestamp, and never moves the halt state. All data is SYNTHETIC; every
vendor is a fake.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

import corollary.engine.scheduler as scheduler_module
from corollary.data.news import audit_runner
from corollary.data.news.audit import VERDICT_BACKFILL_CAP
from corollary.data.news.pollers import PollSkipped
from corollary.data.news.tradeability import (
    MAX_IPO_LOOKUPS_PER_RUN,
    MAX_TICKERS_PER_RUN,
    RefreshResult,
)
from corollary.data.providers.interface import Bar
from corollary.db.models import AuditRun, LabelOutcome
from corollary.engine.scheduler import (
    SENTIMENT_AUDIT_JOB,
    TRADEABILITY_BACKFILL_BOUND,
    ContextServices,
    EveryInterval,
    HeldPositionUnderlyings,
    JobSkipped,
    PositionUnderlyings,
    ScheduledJob,
    Scheduler,
    context_jobs,
)
from tests.api.news_support import make_seed
from tests.data.news.test_audit_backfill import _flips_on_monday, _liquid, _stored
from tests.data.news.test_audit_runner import MONDAY, MONDAY_10, SATURDAY_10, _label, _verdict
from tests.data.news.test_tradeability_cache import directory
from tests.engine.test_scheduler import (
    FakeAlpacaContext,
    FakeClock,
    _jobs_by_name,
    _news_services,
    _state_snapshot,
    db_engine,
    no_secrets,
    run_until_parked,
)

__all__ = ["db_engine"]  # the fixture, re-exported so pytest finds it here

TUESDAY_10 = datetime(2026, 11, 17, 15, 0, tzinfo=SATURDAY_10.tzinfo)
#: Saturday 21 Nov 12:00 ET: no session, and two hours clear of the 10:00 audit slot.
SATURDAY_NOON = SATURDAY_10 + timedelta(hours=2)
#: Friday 20 Nov 11:00 ET: a regular session is open.
FRIDAY_11 = datetime(2026, 11, 20, 16, 0, tzinfo=SATURDAY_10.tzinfo)
#: Friday 20 Nov 17:00 ET: after the close.
FRIDAY_EVENING = datetime(2026, 11, 20, 22, 0, tzinfo=SATURDAY_10.tzinfo)


@dataclasses.dataclass
class RecordingAlpaca(FakeAlpacaContext):
    """The context-job Alpaca fake, with the two tradeability requests recorded."""

    bar_calls: list[tuple[tuple[str, ...], date]] = dataclasses.field(default_factory=list)
    root_calls: list[str] = dataclasses.field(default_factory=list)
    flip: bool = False
    #: Awaited before each daily-bars answer: a test spends fake-clock time
    #: here, as a real request spends real time.
    latency: Callable[[], Awaitable[None]] | None = None
    #: Called after each daily-bars answer: a test moves its clock here.
    after_bars: Callable[[], None] | None = None

    async def adv_daily_bars(  # type: ignore[override]
        self, symbols: Any, *, session_date: date
    ) -> dict[str, list[Bar]]:
        self.bar_calls.append((tuple(symbols), session_date))
        if self.latency is not None:
            await self.latency()
        make = _flips_on_monday if self.flip else _liquid
        answer = {s: make(s, session_date) for s in symbols}
        if self.after_bars is not None:
            self.after_bars()
        return answer

    async def has_standard_root(self, ticker: str) -> bool:
        self.root_calls.append(ticker)
        return True


def _today(root_requests: int, ipo_requests: int = 0) -> RefreshResult:
    """Today's refresh as having spent ``root_requests`` of the bound."""
    return RefreshResult(
        session_date=SATURDAY_10.date(),
        skipped=None,
        results=(),
        errors=(),
        deferred=(),
        bar_requests=0,
        root_requests=root_requests,
        ipo_requests=ipo_requests,
    )


async def _run_job(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    today: RefreshResult | PollSkipped | None,
    flip: bool = False,
    at: datetime = SATURDAY_NOON,
    clock: Callable[[], datetime] | None = None,
    with_directory: bool = True,
    stale_directory: bool = False,
    prepare: Callable[[ContextServices, RecordingAlpaca], None] | None = None,
) -> tuple[RecordingAlpaca, JobSkipped | None]:
    """The shipped ``tradeability_cache`` job, once, at ``at`` (or on ``clock``).

    ``today`` stands in for today's check; ``None`` runs the real one.
    ``prepare`` sees the services and the fake before the job runs.
    """
    _stub_today(monkeypatch, today)
    alpaca = RecordingAlpaca(directory=directory("XA", "XB", "XC", "XOLD"), flip=flip)
    services = _news_services(db_engine, alpaca=alpaca, seed_loader=make_seed)
    if with_directory:
        await services.assets.refresh(alpaca)
    if stale_directory:
        monkeypatch.setattr(services.assets, "is_stale", lambda **_: True)
    if prepare is not None:
        prepare(services, alpaca)
    job_clock = clock if clock is not None else (lambda: at)
    outcome = await _jobs_by_name(services, job_clock)["tradeability_cache"].run()
    return alpaca, outcome


def _stub_today(
    monkeypatch: pytest.MonkeyPatch, today: RefreshResult | PollSkipped | None
) -> None:
    """Today's check answers ``today`` without asking anything; ``None`` keeps the real one."""
    if today is None:
        return
    answer = today

    async def todays_refresh(**_: object) -> RefreshResult | PollSkipped:
        return answer

    monkeypatch.setattr(scheduler_module, "refresh_tradeability_cache", todays_refresh)


def _labels(engine: Engine, *rows: tuple[str, datetime]) -> None:
    with Session(engine) as session:
        for ticker, at in rows:
            _label(session, ticker=ticker, source="rules", direction="bullish", at=at)
        session.commit()


def test_the_bound_is_the_refreshs_own() -> None:
    assert TRADEABILITY_BACKFILL_BOUND == MAX_TICKERS_PER_RUN == 100
    # The runner's own cap is untouched by Q25.
    assert VERDICT_BACKFILL_CAP == 100


@pytest.mark.asyncio
async def test_the_daily_job_backfills_oldest_first_within_what_the_refresh_left(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(db_engine, ("XC", TUESDAY_10), ("XB", MONDAY_10), ("XA", MONDAY_10))
    alpaca, outcome = await _run_job(
        db_engine, monkeypatch, today=_today(root_requests=MAX_TICKERS_PER_RUN - 2)
    )

    # Two pairs left of the bound: Monday's XA and XB; Tuesday's XC waits.
    assert alpaca.bar_calls == [(("XA", "XB"), MONDAY)]
    assert len(alpaca.root_calls) <= 2
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XA", MONDAY), ("XB", MONDAY)]
    assert outcome is None
    # Verdicts only: no run row, no grade.
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditRun)) == 0
        assert session.scalar(select(func.count()).select_from(LabelOutcome)) == 0


@pytest.mark.asyncio
async def test_a_skipped_refresh_leaves_the_whole_bound_to_the_backfill(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(db_engine, ("XC", TUESDAY_10), ("XB", MONDAY_10), ("XA", MONDAY_10))
    alpaca, outcome = await _run_job(
        db_engine, monkeypatch, today=PollSkipped("no off-watch ticker needs a check")
    )
    assert [day for _, day in alpaca.bar_calls] == [MONDAY, date(2026, 11, 17)]
    assert len(_stored(db_engine)) == 3
    assert outcome is None  # work was done, so not a skip


@pytest.mark.asyncio
async def test_nothing_is_spent_once_the_refresh_used_the_bound(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _labels(db_engine, ("XA", MONDAY_10))
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        alpaca, _ = await _run_job(
            db_engine, monkeypatch, today=_today(root_requests=MAX_TICKERS_PER_RUN)
        )
    assert alpaca.bar_calls == [] and alpaca.root_calls == []
    assert _stored(db_engine) == []
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "tradeability_backfill_skipped"]
    assert len(skipped) == 1 and getattr(skipped[0], "budget") == 0


@pytest.mark.asyncio
async def test_the_daily_backfill_reads_no_session_on_or_after_the_labels(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monday's $50 bars are served whatever is asked; judged for Monday they are ignored."""
    _labels(db_engine, ("XA", MONDAY_10))
    alpaca, _ = await _run_job(db_engine, monkeypatch, today=_today(0), flip=True)
    assert [day for _, day in alpaca.bar_calls] == [MONDAY]
    assert _stored(db_engine) == [("XA", MONDAY, False, "low_close", Decimal("4"))]


@pytest.mark.asyncio
async def test_a_stale_but_existing_verdict_is_never_replaced(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(db_engine, ("XOLD", MONDAY_10))
    with Session(db_engine) as session:
        _verdict(session, "XOLD", date(2026, 11, 10), True)
        session.commit()
    alpaca, outcome = await _run_job(
        db_engine, monkeypatch, today=PollSkipped("nothing today")
    )
    assert alpaca.bar_calls == [] and alpaca.root_calls == []
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XOLD", date(2026, 11, 10))]
    assert outcome == JobSkipped("nothing today")


@pytest.mark.asyncio
async def test_the_ipo_budget_is_what_the_refresh_left(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    real = audit_runner.backfill_verdicts

    async def spy(*args: Any, **kwargs: Any) -> audit_runner.VerdictBackfillOutcome:
        seen.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(scheduler_module, "backfill_verdicts", spy)
    _labels(db_engine, ("XA", MONDAY_10))
    await _run_job(db_engine, monkeypatch, today=_today(root_requests=30, ipo_requests=5))
    assert seen["max_pairs"] == MAX_TICKERS_PER_RUN - 30
    assert seen["max_ipo_lookups"] == MAX_IPO_LOOKUPS_PER_RUN - 5
    assert seen["caller"] == "tradeability_cache"


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_backfill_failure_is_logged_and_never_moves_the_halt_state(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_: object, **__: object) -> object:
        raise RuntimeError("the labels table is unreadable")

    monkeypatch.setattr(scheduler_module, "load_verdicts_needed", broken)
    state_before = _state_snapshot(db_engine)
    with caplog.at_level(logging.ERROR, logger="corollary.engine.scheduler"):
        alpaca, outcome = await _run_job(
            db_engine, monkeypatch, today=PollSkipped("nothing today")
        )
    # Today's outcome is reported as it was; the backfill's failure is logged.
    assert outcome == JobSkipped("nothing today")
    failed = [r for r in caplog.records if getattr(r, "event", None) == "tradeability_backfill_failed"]
    assert len(failed) == 1
    record = failed[0]
    assert "rule" in record.__dict__ and getattr(record, "rule")
    assert getattr(record, "error_type") == "RuntimeError"
    assert getattr(record, "at") == SATURDAY_NOON.isoformat()
    inputs = getattr(record, "inputs")
    assert inputs["max_pairs"] == MAX_TICKERS_PER_RUN
    assert alpaca.bar_calls == []
    assert _state_snapshot(db_engine) == state_before


@pytest.mark.asyncio
async def test_the_daily_backfill_stands_aside_while_an_audit_runs(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Audit L3: the run lock itself, held as a run holds it -- not an advisory flag."""
    _labels(db_engine, ("XA", MONDAY_10))
    needs: list[object] = []
    real = scheduler_module.load_verdicts_needed

    def counted(*args: Any) -> Any:
        needs.append(args)
        return real(*args)

    monkeypatch.setattr(scheduler_module, "load_verdicts_needed", counted)
    assert audit_runner._RUN_LOCK.acquire(blocking=False)
    try:
        with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
            alpaca, _ = await _run_job(db_engine, monkeypatch, today=_today(0))
    finally:
        audit_runner._RUN_LOCK.release()
    assert alpaca.bar_calls == [] and alpaca.root_calls == [] and _stored(db_engine) == []
    assert needs == []
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "tradeability_backfill_skipped"]
    assert len(skipped) == 1 and "audit run" in getattr(skipped[0], "reason")


def _counted_needs(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every call of ``load_verdicts_needed`` from the job, called through."""
    calls: list[object] = []
    real = scheduler_module.load_verdicts_needed

    def counted(*args: Any) -> Any:
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(scheduler_module, "load_verdicts_needed", counted)
    return calls


def _skips(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == "tradeability_backfill_skipped"]


@pytest.mark.asyncio
async def test_in_session_the_backfill_reads_no_need_and_asks_nothing(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ruling P1: the calendar's open session, checked first, logged with its date."""
    _labels(db_engine, ("XA", MONDAY_10))
    needs = _counted_needs(monkeypatch)
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        alpaca, outcome = await _run_job(
            db_engine, monkeypatch, today=PollSkipped("nothing today"), at=FRIDAY_11
        )
    assert needs == [] and alpaca.bar_calls == [] and alpaca.root_calls == []
    assert _stored(db_engine) == []
    assert outcome == JobSkipped("nothing today")
    (record,) = _skips(caplog)
    assert getattr(record, "session_date") == "2026-11-20"
    assert getattr(record, "at") == FRIDAY_11.isoformat()
    assert "session" in getattr(record, "reason") and getattr(record, "rule")


@pytest.mark.asyncio
async def test_after_the_close_the_backfill_runs(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(db_engine, ("XA", MONDAY_10))
    alpaca, outcome = await _run_job(db_engine, monkeypatch, today=_today(0), at=FRIDAY_EVENING)
    assert [day for _, day in alpaca.bar_calls] == [MONDAY]
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XA", MONDAY)]
    assert outcome is None


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_minutes", [-15, 0, 15])
async def test_the_backfill_stands_clear_of_the_scheduled_audit_slot(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    offset_minutes: int,
) -> None:
    """Holding the run lock at Saturday 10:00 would make the week's audit skip."""
    _labels(db_engine, ("XA", MONDAY_10))
    needs = _counted_needs(monkeypatch)
    at = SATURDAY_10 + timedelta(minutes=offset_minutes)
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        alpaca, _ = await _run_job(db_engine, monkeypatch, today=_today(0), at=at)
    assert needs == [] and alpaca.bar_calls == [] and _stored(db_engine) == []
    (record,) = _skips(caplog)
    assert "audit slot" in getattr(record, "reason")


@pytest.mark.asyncio
async def test_the_job_logs_a_stale_directory_once_a_run(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ruling P2: the check for today says so; the backfill on the job's path does not repeat it."""
    _labels(db_engine, ("XA", MONDAY_10))
    with caplog.at_level(logging.INFO):
        _, _ = await _run_job(db_engine, monkeypatch, today=None, stale_directory=True)
    stale = [r for r in caplog.records if getattr(r, "event", None) == "asset_directory_stale_used"]
    assert len(stale) == 1
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XA", MONDAY)]


@pytest.mark.asyncio
async def test_a_skip_stays_a_skip_when_the_backfill_stored_nothing(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit L1: no directory held, so every attempted pair faults ``refresh_skipped``."""
    _labels(db_engine, ("XA", MONDAY_10))
    _, outcome = await _run_job(
        db_engine, monkeypatch, today=PollSkipped("nothing today"), with_directory=False
    )
    assert _stored(db_engine) == []
    assert outcome == JobSkipped("nothing today")


def _blocking(
    monkeypatch: pytest.MonkeyPatch, target: object, name: str, *, block_on: date | None = None
) -> tuple[asyncio.Event, asyncio.Event]:
    """Replace ``target.name`` with a coroutine that parks until released, then raises.

    With ``block_on``, only that session parks; every other is the real call.
    """
    entered, release = asyncio.Event(), asyncio.Event()
    real = getattr(target, name)

    async def parked(*args: Any, **kwargs: Any) -> Any:
        if block_on is not None and kwargs.get("session_date") != block_on:
            return await real(*args, **kwargs)
        entered.set()
        await release.wait()
        raise RuntimeError(f"{name} failed inside the stage")

    monkeypatch.setattr(target, name, parked)
    return entered, release


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["refresh", "backfill"])
@pytest.mark.parametrize("halted_before", [False, True])
async def test_a_failure_inside_the_stage_never_moves_the_halt_state(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    where: str,
    halted_before: bool,
) -> None:
    """Audit M3: past every gate, under a real runtime, the stage fails -- harmlessly.

    ``refresh`` fails inside ``refresh_tradeability`` (a fault, held);
    ``backfill`` fails inside ``backfill_verdicts`` (the job's own except).
    While the stage is parked the run lock is held and the event loop is
    free -- this test's own code runs -- so the heartbeat is never waited on.
    """
    from tests.engine.test_scheduler import _WatchedRuntime

    watched = _WatchedRuntime(db_engine, monkeypatch, halted=halted_before)
    state_before = _state_snapshot(db_engine)
    _labels(db_engine, ("XA", MONDAY_10))
    if where == "refresh":
        entered, release = _blocking(monkeypatch, audit_runner, "refresh_tradeability")
    else:
        entered, release = _blocking(monkeypatch, scheduler_module, "backfill_verdicts")
    with caplog.at_level(logging.INFO):
        job = asyncio.create_task(_run_job(db_engine, monkeypatch, today=_today(0)))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert audit_runner._RUN_LOCK.locked()  # an audit run would be refused now
        release.set()
        _, outcome = await asyncio.wait_for(job, timeout=5)
    assert not audit_runner._RUN_LOCK.locked()
    assert outcome is None  # the check for today is reported as it was
    assert _stored(db_engine) == []
    events = {getattr(r, "event", None) for r in caplog.records}
    if where == "refresh":
        assert "sentiment_audit_verdict_backfill_failed" in events
    else:
        assert "tradeability_backfill_failed" in events
    assert _state_snapshot(db_engine) == state_before
    watched.assert_untouched()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_cancellation_during_the_stage_propagates_and_half_writes_nothing(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monday is stored whole; Tuesday, cancelled mid-call, is not begun."""
    state_before = _state_snapshot(db_engine)
    _labels(db_engine, ("XC", TUESDAY_10), ("XB", MONDAY_10), ("XA", MONDAY_10))
    entered, _ = _blocking(
        monkeypatch, audit_runner, "refresh_tradeability", block_on=date(2026, 11, 17)
    )
    job = asyncio.create_task(_run_job(db_engine, monkeypatch, today=_today(0)))
    await asyncio.wait_for(entered.wait(), timeout=5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert not audit_runner._RUN_LOCK.locked()
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XA", MONDAY), ("XB", MONDAY)]
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditRun)) == 0
    assert _state_snapshot(db_engine) == state_before


# ------------------------------------------- Q25 re-audit: who gets the run lock

UTC = timezone.utc


def _counted_locks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every ``try_run_lock`` the job attempts, by holder, called through."""
    attempts: list[str] = []
    real = scheduler_module.try_run_lock

    def counted(holder: str) -> Any:
        attempts.append(holder)
        return real(holder)

    monkeypatch.setattr(scheduler_module, "try_run_lock", counted)
    return attempts


@pytest.mark.asyncio
async def test_until_the_audit_catch_up_settles_the_backfill_stands_aside(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-audit 1: checked just before the lock is tried, so the lock is never tried."""
    _labels(db_engine, ("XA", MONDAY_10))
    needs = _counted_needs(monkeypatch)
    locks = _counted_locks(monkeypatch)

    def armed(services: ContextServices, _: RecordingAlpaca) -> None:
        services.audit_catch_up_settled.clear()  # as a started scheduler leaves it

    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        alpaca, outcome = await _run_job(db_engine, monkeypatch, today=_today(0), prepare=armed)
    assert locks == [] and needs == []
    assert alpaca.bar_calls == [] and alpaca.root_calls == [] and _stored(db_engine) == []
    assert not audit_runner._RUN_LOCK.locked()
    assert outcome is None  # today's check is reported as it was
    (record,) = _skips(caplog)
    assert "catch-up" in getattr(record, "reason")
    assert getattr(record, "rule") and getattr(record, "at") == SATURDAY_NOON.isoformat()
    assert getattr(record, "inputs")["max_pairs"] == MAX_TICKERS_PER_RUN


@pytest.mark.asyncio
async def test_without_an_asset_directory_the_backfill_stands_aside_before_the_lock(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-audit 1: no ``refresh_skipped`` fault per pair, and no lock held for nothing."""
    _labels(db_engine, ("XA", MONDAY_10))
    needs = _counted_needs(monkeypatch)
    locks = _counted_locks(monkeypatch)
    with caplog.at_level(logging.INFO):
        alpaca, outcome = await _run_job(
            db_engine, monkeypatch, today=PollSkipped("nothing today"), with_directory=False
        )
    assert locks == [] and needs == [] and alpaca.bar_calls == [] and alpaca.root_calls == []
    assert outcome == JobSkipped("nothing today")
    (record,) = _skips(caplog)
    assert "asset directory" in getattr(record, "reason")
    assert getattr(record, "rule") and getattr(record, "at") == SATURDAY_NOON.isoformat()
    events = [getattr(r, "event", None) for r in caplog.records]
    assert "sentiment_audit_verdict_backfill" not in events


@pytest.mark.asyncio
async def test_a_started_scheduler_arms_the_gate_only_when_it_holds_the_audit_job(
    db_engine: Engine,
) -> None:
    """Re-audit 1: an app or test with no audit job cannot leave the backfill blocked."""
    services = _news_services(db_engine)
    jobs = _jobs_by_name(services, lambda: SATURDAY_NOON)
    assert services.audit_catch_up_settled.is_set()  # built is not wired

    without = Scheduler([jobs["tradeability_cache"]], secrets=no_secrets)
    without.start()
    assert services.audit_catch_up_settled.is_set()
    await without.aclose()

    wired = Scheduler([jobs["tradeability_cache"], jobs[SENTIMENT_AUDIT_JOB]], secrets=no_secrets)
    wired.start()
    assert not services.audit_catch_up_settled.is_set()
    await wired.aclose()


def _positions_refresher(
    holder: PositionUnderlyings, clock: FakeClock, *, first_read_after: float | None
) -> ScheduledJob:
    """The lifespan's paper positions refresher, as a job on the fake clock.

    Its first read is ``first_read_after`` seconds in (``None``: the holder
    was read before start), then a read every five minutes, so the holder
    never goes stale under the watch universe's three-interval bound.
    """

    async def read() -> None:
        holder.replace({"TSLA"}, at=clock())

    async def first_read() -> None:
        assert first_read_after is not None
        await clock.sleep(first_read_after)
        await read()

    return ScheduledJob(
        name="positions_refresher_stand_in",
        schedule=EveryInterval(timedelta(minutes=5)),
        run=read,
        catch_up=None if first_read_after is None else first_read,
        rule="test stand-in for the lifespan's paper positions refresher",
    )


async def _run_race(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    start: datetime,
    stop_at: datetime,
    positions_read_at_start: bool,
) -> tuple[Scheduler, list[Any]]:
    """The shipped audit and tradeability jobs on one fake clock, from ``start``.

    Each daily-bars request takes five seconds of the fake clock, as a real
    request takes real time, so a backfill that took the run lock is still
    holding it when a catch-up that woke a few seconds later asks for it.
    Last Saturday's audit is missed (no ``audit_run`` row) and one
    off-watch label from Monday 16 Nov needs a verdict.
    """
    _labels(db_engine, ("XA", MONDAY_10))
    _stub_today(monkeypatch, PollSkipped("nothing today"))
    needs = _counted_needs(monkeypatch)
    clock = FakeClock(start, stop_at=stop_at, wait_for_threads=True)

    async def five_seconds() -> None:
        await clock.sleep(5)

    alpaca = RecordingAlpaca(directory=directory("XA", "XB", "XC", "XOLD"), latency=five_seconds)
    holder = PositionUnderlyings()
    if positions_read_at_start:
        holder.replace({"TSLA"}, at=start)
    services = dataclasses.replace(
        _news_services(db_engine, alpaca=alpaca, seed_loader=make_seed),
        position_underlyings=HeldPositionUnderlyings(holder),
    )
    await services.assets.refresh(alpaca)
    jobs = {j.name: j for j in context_jobs(services, clock=clock, sleep=clock.sleep)}
    # The tradeability job first, so at a tie its sleeper is released first.
    chosen = [
        jobs["tradeability_cache"],
        jobs[SENTIMENT_AUDIT_JOB],
        _positions_refresher(
            holder, clock, first_read_after=None if positions_read_at_start else 1.0
        ),
    ]
    scheduler = Scheduler(chosen, secrets=no_secrets, clock=clock, sleep=clock.sleep)
    await run_until_parked(scheduler, clock)
    return scheduler, needs


def _audit_runs(engine: Engine) -> list[tuple[str, str, datetime]]:
    with Session(engine) as session:
        return [
            (r.trigger, r.status, r.started_at.replace(tzinfo=UTC))
            for r in session.scalars(select(AuditRun).order_by(AuditRun.id))
        ]


@pytest.mark.asyncio
async def test_the_start_up_catch_up_is_never_refused_by_the_backfill(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-audit 1, reproduced: start 14:14:57, positions read 14:14:58, the grid at 14:15:00.

    Sunday 22 Nov 2026, so no session gate applies and the audit slot is
    days away. The catch-up's positions poll sleeps five seconds and wakes at
    14:15:02; the tradeability slot fires at 14:15:00, before it. Before the
    fix the backfill took the run lock there and held it across its
    five-second request, and the catch-up's run was refused -- that week's
    audit then waited for the next start or the next Saturday. Now the
    backfill stands aside until the catch-up has settled, and the next slot,
    14:30, proceeds.
    """
    start = datetime(2026, 11, 22, 19, 14, 57, tzinfo=UTC)  # 14:14:57 ET
    with caplog.at_level(logging.INFO):
        scheduler, needs = await _run_race(
            db_engine,
            monkeypatch,
            start=start,
            stop_at=datetime(2026, 11, 22, 19, 31, tzinfo=UTC),
            positions_read_at_start=False,
        )
    status = scheduler.status()
    assert status[SENTIMENT_AUDIT_JOB].skips == 0, status[SENTIMENT_AUDIT_JOB]
    assert status[SENTIMENT_AUDIT_JOB].failures == 0
    assert [(t, s) for t, s, _ in _audit_runs(db_engine)] == [("scheduled", "succeeded")]
    first_slot = datetime(2026, 11, 22, 19, 15, tzinfo=UTC)
    stood_aside = [r for r in _skips(caplog) if getattr(r, "at") == first_slot.isoformat()]
    assert len(stood_aside) == 1 and "catch-up" in getattr(stood_aside[0], "reason")
    # Not blocked for good: the 14:30 slot read the need.
    assert [args[2] for args in needs] == [datetime(2026, 11, 22, 19, 30, tzinfo=UTC)]


@pytest.mark.asyncio
async def test_a_close_instant_tie_leaves_the_run_lock_to_the_deferred_catch_up(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-audit 3: every close is on the 15-minute grid, so both wake at 16:00 ET.

    Monday 23 Nov 2026, started in session at 15:50 ET: the catch-up defers
    to the calendar's close and the tradeability slot is the same instant.
    The backfill at 16:00 must not take the lock before the deferred
    catch-up has settled; the catch-up's run succeeds at the close, and the
    16:15 slot proceeds.
    """
    close = datetime(2026, 11, 23, 21, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO):
        scheduler, needs = await _run_race(
            db_engine,
            monkeypatch,
            start=close - timedelta(minutes=10),
            stop_at=close + timedelta(minutes=16),
            positions_read_at_start=True,
        )
    status = scheduler.status()
    assert status[SENTIMENT_AUDIT_JOB].skips == 0, status[SENTIMENT_AUDIT_JOB]
    runs = _audit_runs(db_engine)
    assert [(t, s) for t, s, _ in runs] == [("scheduled", "succeeded")]
    assert runs[0][2] == close
    at_close = [r for r in _skips(caplog) if getattr(r, "at") == close.isoformat()]
    assert len(at_close) == 1 and "catch-up" in getattr(at_close[0], "reason")
    assert [args[2] for args in needs] == [close + timedelta(minutes=15)]


@pytest.mark.asyncio
async def test_while_the_backfill_holds_the_lock_a_refused_audit_names_it(
    db_engine: Engine,
) -> None:
    """Re-audit 2: not "another sentiment audit run", and no claim it grades labels.

    Since 2026-11-20 the slot waits out ``SENTIMENT_AUDIT_LOCK_WAIT`` for the
    backfill first; the sleeper here returns at once, so the wait expires
    instantly and the skip says how long it waited.
    """
    services = _news_services(
        db_engine, alpaca=RecordingAlpaca(directory=directory("XA")), seed_loader=make_seed
    )

    async def no_wait(_: float) -> None:
        return None

    jobs = {
        job.name: job
        for job in scheduler_module.context_jobs(
            services, clock=lambda: SATURDAY_10, sleep=no_wait
        )
    }
    with audit_runner.try_run_lock("tradeability_cache") as taken:
        assert taken
        outcome = await jobs[SENTIMENT_AUDIT_JOB].run()
    assert isinstance(outcome, JobSkipped)
    assert "tradeability_cache" in outcome.reason
    assert "after waiting 30 minutes" in outcome.reason
    assert "grades the same labels" not in outcome.reason
    assert "another sentiment audit run" not in outcome.reason
    assert not audit_runner._RUN_LOCK.locked()


#: Monday 23 Nov 2026 09:29 ET: before the open.
MONDAY_PRE_OPEN = datetime(2026, 11, 23, 14, 29, tzinfo=UTC)
#: Monday 23 Nov 2026 09:31 ET: the session is open.
MONDAY_OPENED = datetime(2026, 11, 23, 14, 31, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_run_that_reaches_the_open_stops_before_the_next_session(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-audit 4: the session rule is asked before each session's refresh, not once.

    Started at 09:29 ET, the clock passes 09:30 during Monday's refresh:
    Monday's pairs are kept and Tuesday's refresh is never asked. The stop is
    logged with the rule, the session and the instant.
    """
    _labels(db_engine, ("XC", TUESDAY_10), ("XB", MONDAY_10), ("XA", MONDAY_10))
    clock = [MONDAY_PRE_OPEN]

    def opens(_: ContextServices, alpaca: RecordingAlpaca) -> None:
        def moved() -> None:
            clock[0] = MONDAY_OPENED

        alpaca.after_bars = moved

    with caplog.at_level(logging.INFO):
        alpaca, outcome = await _run_job(
            db_engine, monkeypatch, today=_today(0), clock=lambda: clock[0], prepare=opens
        )
    assert alpaca.bar_calls == [(("XA", "XB"), MONDAY)]
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XA", MONDAY), ("XB", MONDAY)]
    assert outcome is None
    (stopped,) = [
        r for r in caplog.records if getattr(r, "event", None) == "tradeability_backfill_stopped"
    ]
    assert getattr(stopped, "rule") and getattr(stopped, "at") == MONDAY_OPENED.isoformat()
    assert getattr(stopped, "session_date") == "2026-11-23"
    assert getattr(stopped, "inputs")["stored"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_minutes", [-16, 16])
async def test_just_outside_the_clearance_the_backfill_proceeds(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    offset_minutes: int,
) -> None:
    _labels(db_engine, ("XA", MONDAY_10))
    needs = _counted_needs(monkeypatch)
    at = SATURDAY_10 + timedelta(minutes=offset_minutes)
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        _, _ = await _run_job(db_engine, monkeypatch, today=_today(0), at=at)
    assert len(needs) == 1
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XA", MONDAY)]
    assert not [r for r in _skips(caplog) if "audit slot" in getattr(r, "reason")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("at", "stands_aside"),
    [
        # Saturday 31 Oct 2026 is EDT (UTC-4): the slot is 14:00 UTC.
        (datetime(2026, 10, 31, 14, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 31, 15, 0, tzinfo=UTC), False),  # 11:00 EDT
        # Saturday 7 Nov 2026, after the 1 Nov fall-back, is EST (UTC-5): 15:00 UTC.
        (datetime(2026, 11, 7, 15, 0, tzinfo=UTC), True),
        (datetime(2026, 11, 7, 14, 0, tzinfo=UTC), False),  # 09:00 EST
        # Saturday 14 Mar 2026, after the 8 Mar spring-forward, is EDT again.
        (datetime(2026, 3, 14, 14, 0, tzinfo=UTC), True),
        (datetime(2026, 3, 14, 15, 0, tzinfo=UTC), False),
    ],
    ids=[
        "edt_slot",
        "edt_hour_after",
        "est_slot",
        "est_hour_before",
        "spring_slot",
        "spring_hour_after",
    ],
)
async def test_the_clearance_follows_the_slot_across_daylight_saving(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    at: datetime,
    stands_aside: bool,
) -> None:
    """The slot is 10:00 ET in EST and EDT alike; the clearance is measured from it."""
    needs = _counted_needs(monkeypatch)
    with caplog.at_level(logging.INFO, logger="corollary.engine.scheduler"):
        _, _ = await _run_job(db_engine, monkeypatch, today=_today(0), at=at)
    slot_skips = [r for r in _skips(caplog) if "audit slot" in getattr(r, "reason")]
    if stands_aside:
        assert needs == [] and len(slot_skips) == 1
    else:
        assert len(needs) == 1 and slot_skips == []
