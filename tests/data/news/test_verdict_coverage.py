"""Owner decision Q25 (2026-11-16): decision 27's need, read and filled outside the audit run.

The weekly audit backfills at most ``VERDICT_BACKFILL_CAP`` (100) missing
verdicts a run, and live run 5 needed 2117. Q25 has the daily tradeability
refresh fill the same need, and a one-off script drain it once. Both go
through the two public pieces pinned here:

* :func:`audit_runner.load_verdicts_needed` -- exactly
  :attr:`AuditPlan.verdicts_needed`, from the runner's own loaders, so
  "off-watch" and "reference session" are never re-derived;
* :func:`audit_runner.backfill_verdicts` -- the runner's backfill with the
  bound as an argument, oldest session first.

All data here is SYNTHETIC; no test makes a network request.
"""

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from corollary.data.news import audit_runner
from corollary.data.news.audit_runner import (
    VerdictBackfill,
    backfill_verdicts,
    load_verdicts_needed,
)
from corollary.db.models import AuditRun, LabelOutcome
from tests.data.news.test_audit_backfill import (
    FakeInputs,
    _flips_on_monday,
    _monday_label,
    _stored,
)
from tests.data.news.test_audit_runner import (
    MONDAY,
    MONDAY_10,
    SATURDAY_10,
    TUESDAY,
    _label,
    _sessions,
    _verdict,
    db_engine,
)
from tests.data.news.test_tradeability_cache import directory

__all__ = ["db_engine"]  # the fixture, re-exported so pytest finds it here

WATCH = frozenset({"AAPL", "MSFT"})


def _backfill(inputs: FakeInputs) -> VerdictBackfill:
    held = directory("XA", "XB", "XBACK", "XOLD")
    return VerdictBackfill(inputs=inputs, assets=lambda: held, ipo_dates=None)


def _three_labels(engine: Engine) -> None:
    """Tuesday's XA, Monday's XB, Monday's XA -- inserted out of order."""
    with Session(engine) as session:
        _label(
            session, ticker="XA", source="rules", direction="bullish",
            at=MONDAY_10 + timedelta(days=1),
        )
        _label(session, ticker="XB", source="rules", direction="bullish", at=MONDAY_10)
        _label(session, ticker="XA", source="rules", direction="bullish", at=MONDAY_10)
        session.commit()


def _fill(engine: Engine, inputs: FakeInputs, *, max_pairs: int) -> audit_runner.VerdictBackfillOutcome:
    needed = load_verdicts_needed(_sessions(engine), WATCH, SATURDAY_10, "test")
    return asyncio.run(
        backfill_verdicts(
            _backfill(inputs),
            needed,
            session_factory=_sessions(engine),
            clock=lambda: SATURDAY_10,
            max_pairs=max_pairs,
            caller="test",
            correlation_id="test",
        )
    )


# ------------------------------------------------------------------ the need


def test_the_need_is_the_plans_own_oldest_session_first(db_engine: Engine) -> None:
    _three_labels(db_engine)
    with Session(db_engine) as session:
        _label(session, ticker="AAPL", source="rules", direction="bullish", at=MONDAY_10)
        session.commit()
    needed = load_verdicts_needed(_sessions(db_engine), WATCH, SATURDAY_10, "test")
    # A watch ticker needs no verdict; (session, ticker) order.
    assert needed == (("XA", MONDAY), ("XB", MONDAY), ("XA", TUESDAY))


def test_a_stale_but_existing_verdict_is_no_need(db_engine: Engine) -> None:
    """Decision 25 grades on it, so decision 27 never replaces it."""
    _monday_label(db_engine, ticker="XOLD")
    with Session(db_engine) as session:
        _verdict(session, "XOLD", date(2026, 11, 10), True)
        session.commit()
    assert load_verdicts_needed(_sessions(db_engine), WATCH, SATURDAY_10, "test") == ()


def test_reading_the_need_writes_nothing(db_engine: Engine) -> None:
    _three_labels(db_engine)
    load_verdicts_needed(_sessions(db_engine), WATCH, SATURDAY_10, "test")
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditRun)) == 0
        assert session.scalar(select(func.count()).select_from(LabelOutcome)) == 0


# -------------------------------------------------------------- the backfill


def test_the_backfill_takes_its_bound_oldest_session_first(db_engine: Engine) -> None:
    _three_labels(db_engine)
    inputs = FakeInputs()
    outcome = _fill(db_engine, inputs, max_pairs=2)

    assert inputs.bar_calls == [(("XA", "XB"), MONDAY)]
    assert (outcome.needed, outcome.attempted, len(outcome.stored)) == (3, 2, 2)
    assert outcome.capped == (("XA", TUESDAY),)
    assert outcome.held == frozenset({("XA", TUESDAY)})
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XA", MONDAY), ("XB", MONDAY)]
    # Verdicts only: no run row and no grade.
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditRun)) == 0
        assert session.scalar(select(func.count()).select_from(LabelOutcome)) == 0


def test_a_tickers_root_answer_is_asked_once_across_its_sessions(db_engine: Engine) -> None:
    """The standard-root check is the vendor's current answer (decision 27's
    named residual), the same for every session, so one invocation asks it
    once per ticker. Each session's bars are still asked for that session."""
    _three_labels(db_engine)
    inputs = FakeInputs()
    outcome = _fill(db_engine, inputs, max_pairs=10)

    assert inputs.bar_calls == [(("XA", "XB"), MONDAY), (("XA",), TUESDAY)]
    assert sorted(inputs.root_calls) == ["XA", "XB"]
    assert len(outcome.stored) == 3


def test_the_backfill_reads_no_session_on_or_after_the_labels(db_engine: Engine) -> None:
    """The fake serves Monday's $50 bars whatever it is asked; judged for
    Monday they are ignored, so the verdict fails on Friday's $4 close."""
    _monday_label(db_engine)
    inputs = FakeInputs(bars=_flips_on_monday)
    _fill(db_engine, inputs, max_pairs=10)

    assert [day for _, day in inputs.bar_calls] == [MONDAY]
    assert _stored(db_engine) == [("XBACK", MONDAY, False, "low_close", Decimal("4"))]


def test_a_bound_below_one_attempts_nothing(db_engine: Engine) -> None:
    _three_labels(db_engine)
    inputs = FakeInputs()
    outcome = _fill(db_engine, inputs, max_pairs=0)
    assert inputs.bar_calls == [] and inputs.root_calls == []
    assert (outcome.attempted, len(outcome.held)) == (0, 3)


class _CountingIpo:
    """An IPO-date source that counts what it was asked and knows no date."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ipo_date(self, symbol: str) -> date | None:
        self.calls.append(symbol)
        return None


def test_the_ipo_bound_holds_when_a_sessions_refresh_raises(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit L2: lookups are counted at the source, so a raise still spends them.

    Monday's refresh asks 15 IPO dates and then raises, returning no
    ``RefreshResult``; Tuesday may ask only the 5 left of 20, not 20 again.
    """
    _three_labels(db_engine)
    ipo = _CountingIpo()
    granted: dict[date, int] = {}

    async def refresh(**kwargs: Any) -> Any:
        day: date = kwargs["session_date"]
        granted[day] = kwargs["max_ipo_lookups"]
        if day == MONDAY:
            for n in range(15):
                await kwargs["ipo_dates"].ipo_date(f"X{n}")
            raise RuntimeError("the cache write failed after the lookups")
        raise RuntimeError("Tuesday fails too; only its grant is read")

    monkeypatch.setattr(audit_runner, "refresh_tradeability", refresh)
    needed = load_verdicts_needed(_sessions(db_engine), WATCH, SATURDAY_10, "test")
    held = directory("XA", "XB")
    asyncio.run(
        backfill_verdicts(
            VerdictBackfill(inputs=FakeInputs(), assets=lambda: held, ipo_dates=ipo),
            needed,
            session_factory=_sessions(db_engine),
            clock=lambda: SATURDAY_10,
            max_pairs=10,
            max_ipo_lookups=20,
            caller="test",
            correlation_id="test",
        )
    )
    assert len(ipo.calls) == 15
    assert granted == {MONDAY: 20, TUESDAY: 5}


def test_a_stop_check_ends_the_call_before_the_next_session(db_engine: Engine) -> None:
    """Audit M1's hook: asked before each session; an answer stops the call there.

    Monday's pairs were refreshed and are stored; Tuesday's were never asked,
    are not counted as attempted, and stay held -- pending, never untradeable.
    """
    _three_labels(db_engine)
    inputs = FakeInputs()
    asked: list[int] = []

    def stop() -> str | None:
        asked.append(len(inputs.bar_calls))
        return "the regular session of 2026-11-23 opened" if inputs.bar_calls else None

    needed = load_verdicts_needed(_sessions(db_engine), WATCH, SATURDAY_10, "test")
    outcome = asyncio.run(
        backfill_verdicts(
            _backfill(inputs),
            needed,
            session_factory=_sessions(db_engine),
            clock=lambda: SATURDAY_10,
            max_pairs=10,
            caller="test",
            correlation_id="test",
            stop=stop,
        )
    )
    assert [day for _, day in inputs.bar_calls] == [MONDAY]
    assert asked == [0, 1]
    assert outcome.stopped == "the regular session of 2026-11-23 opened"
    assert outcome.attempted == 2
    assert sorted(outcome.stored) == [("XA", MONDAY), ("XB", MONDAY)]
    assert ("XA", TUESDAY) in outcome.held
    assert [(t, d) for t, d, *_ in _stored(db_engine)] == [("XA", MONDAY), ("XB", MONDAY)]


# ------------------------------------ Q25 re-audit 2: the run lock's holder


def test_a_run_refused_by_the_backfills_lock_names_the_holder() -> None:
    """Not "a sentiment audit run is already in progress": the daily job grades nothing."""
    with audit_runner.try_run_lock("tradeability_cache") as taken:
        assert taken
        # A second taker is refused and does not overwrite the holder.
        with audit_runner.try_run_lock("someone_else") as second:
            assert not second
        with pytest.raises(audit_runner.AuditAlreadyRunning) as refused:
            asyncio.run(
                audit_runner.run_sentiment_audit(None, trigger="scheduled")  # type: ignore[arg-type]
            )
    assert refused.value.holder == "tradeability_cache"
    assert not refused.value.held_by_audit_run
    assert "tradeability_cache" in str(refused.value)
    assert "already in progress" not in str(refused.value)
    assert not audit_runner._RUN_LOCK.locked()


def test_an_unrecorded_holder_reads_as_an_audit_run() -> None:
    """A bare ``_RUN_LOCK`` holder -- only a test takes it so -- keeps the old wording."""
    assert audit_runner._RUN_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(audit_runner.AuditAlreadyRunning) as refused:
            asyncio.run(
                audit_runner.run_sentiment_audit(None, trigger="scheduled")  # type: ignore[arg-type]
            )
    finally:
        audit_runner._RUN_LOCK.release()
    assert refused.value.holder is None and refused.value.held_by_audit_run
    assert "in progress" in str(refused.value)


def test_the_holder_is_cleared_on_release_and_on_a_raise() -> None:
    with pytest.raises(RuntimeError):
        with audit_runner.try_run_lock("tradeability_cache") as taken:
            assert taken
            raise RuntimeError("SYNTHETIC")
    assert not audit_runner._RUN_LOCK.locked()
    assert audit_runner._RUN_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(audit_runner.AuditAlreadyRunning) as refused:
            asyncio.run(
                audit_runner.run_sentiment_audit(None, trigger="scheduled")  # type: ignore[arg-type]
            )
    finally:
        audit_runner._RUN_LOCK.release()
    assert refused.value.holder is None  # not the stale "tradeability_cache"
