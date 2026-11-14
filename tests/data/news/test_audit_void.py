"""R4: voiding a sentiment audit run (U4a), against a real SQLite database.

Two kinds of history are used. The restore tests write ``audit_run``,
``audit_log`` and ``sentiment_source_status`` rows directly, in the shapes
``audit_runner._close_run`` writes, so each scenario's earlier transition is
explicit. The re-grade tests run the **real** runner, from
``test_audit_runner``'s fakes: a void, then a run, grades the voided labels
again. All articles, labels and prices are SYNTHETIC.
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ
from corollary.data.news import audit_runner, audit_void
from corollary.data.news.audit import (
    AccuracyBasis,
    AccuracyScope,
    GradedOutcome,
    Scope,
    SourceStatus,
    compute_accuracy,
)
from corollary.data.news.audit_runner import run_sentiment_audit
from corollary.data.news.audit_void import (
    Refusal,
    RunFacts,
    SentimentLogRow,
    SourceRow,
    VoidRefused,
    VoidResult,
    VoidRule,
    log_voided,
    plan_restorations,
    read_back_void,
    restored_reason,
    void_audit_run,
    void_notice_text,
)
from corollary.db.models import (
    AuditLog,
    AuditRun,
    Base,
    LabelOutcome,
    LabelOutcomeVoided,
    NotificationDelivery,
    NotificationRecord,
    SentimentAccuracy,
    SentimentLabelRow,
    SentimentSourceStatus,
)
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine import scheduler
from tests.data.news.test_audit_runner import (
    FLOOR_SESSIONS,
    SATURDAY_10,
    TickingClock,
    _deps,
    _five_session_week,
    _label,
)
from tests.db.test_session import _lock_is_free

UTC = timezone.utc
REASON = "run 2 graded syndicated twins twice"
#: Two Saturdays of synthetic runs, a week apart, each five minutes long.
T1 = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
RUN_LENGTH = timedelta(minutes=5)
NOW = datetime(2026, 11, 9, 20, 0, tzinfo=UTC)

#: Every table a void reads or writes, for the "writes nothing" comparison.
_TOUCHED: tuple[type[Base], ...] = (
    AuditRun,
    AuditLog,
    LabelOutcome,
    LabelOutcomeVoided,
    SentimentAccuracy,
    SentimentSourceStatus,
    NotificationRecord,
    NotificationDelivery,
)


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "audit_void.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
    yield engine
    engine.dispose()


def _sessions(engine: Engine) -> Callable[[], Session]:
    return lambda: Session(engine)


def _void(engine: Engine, run_id: int, reason: str = REASON, now: datetime = NOW) -> VoidResult:
    return void_audit_run(
        _sessions(engine), run_id=run_id, reason=reason, clock=lambda: now, correlation_id="c0ffee"
    )


class SharedClock:
    """One clock read by two writers on two threads: each reading one second
    after the last, whoever asks. The order of the readings is then the order
    the writers asked in -- which, under the write lock, is the order they
    took the lock in."""

    def __init__(self, start: datetime) -> None:
        self._next = start
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            reading = self._next
            self._next += timedelta(seconds=1)
            return reading


# ------------------------------------------------------------ synthetic history


def _run(
    session: Session, started: datetime, *, status: str = "succeeded", trigger: str = "scheduled"
) -> int:
    run = AuditRun(
        started_at=started,
        finished_at=None if status == "running" else started + RUN_LENGTH,
        trigger=trigger,
        status=status,
    )
    session.add(run)
    session.flush()
    return run.id


def _move(
    session: Session, run_id: int, started: datetime, source: str, previous: str, new: str
) -> None:
    """One transition exactly as ``_close_run`` writes it: the audit_log row
    at the finishing reading, the status row at the run instant."""
    session.add(
        AuditLog(
            at=started + RUN_LENGTH,
            category="sentiment",
            field=source,
            previous_value=previous,
            new_value=new,
        )
    )
    row = session.get(SentimentSourceStatus, source)
    assert row is not None
    row.status = new
    row.changed_at = started
    row.reason = f"run {run_id}'s figures for {source}"
    row.audit_run_id = run_id
    session.flush()


def _outcome(session: Session, label_id: int, window: str, run_id: int) -> None:
    session.add(
        LabelOutcome(
            label_id=label_id,
            window=window,
            skip_reason="off_watch_untradeable",
            scope=None,
            audit_run_id=run_id,
        )
    )


def _history(engine: Engine, run2_moves: tuple[tuple[str, str, str], ...]) -> tuple[int, int]:
    """Run 1 moves ``massive`` unaudited -> active; run 2 makes ``run2_moves``.

    Each run also grades two labels, so the archive has rows of both runs to
    tell apart.
    """
    with Session(engine) as session:
        run1 = _run(session, T1)
        _move(session, run1, T1, "massive", "unaudited", "active")
        run2 = _run(session, T2)
        for source, previous, new in run2_moves:
            _move(session, run2, T2, source, previous, new)
        for run_id in (run1, run2):
            label = _label(session, ticker="ZZZZ", source="rules", direction="bullish", at=T1)
            _outcome(session, label, "1h", run_id)
            _outcome(session, label, "1d", run_id)
        session.add(
            SentimentAccuracy(
                audit_run_id=run2,
                source="massive",
                window="1h",
                basis="trailing_28d",
                scope="all",
                graded=30,
                correct=10,
                neutral=0,
                sessions=5,
            )
        )
        session.commit()
    return run1, run2


def _status(engine: Engine, source: str) -> tuple[object, ...]:
    with Session(engine) as session:
        row = session.get(SentimentSourceStatus, source)
        assert row is not None
        return (row.status, row.changed_at, row.reason, row.audit_run_id)


def _counts(engine: Engine) -> dict[str, int]:
    with Session(engine) as session:
        return {
            model.__tablename__: int(
                session.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in _TOUCHED
        }


def _snapshot(engine: Engine) -> tuple[object, ...]:
    """Every row a void could change, by value."""
    with Session(engine) as session:
        runs = [
            (r.id, r.status, r.voided_at, r.void_reason)
            for r in session.scalars(select(AuditRun).order_by(AuditRun.id))
        ]
        statuses = [
            (s.source, s.status, s.changed_at, s.reason, s.audit_run_id)
            for s in session.scalars(select(SentimentSourceStatus).order_by(SentimentSourceStatus.source))
        ]
        outcomes = [
            (o.label_id, o.window, o.audit_run_id)
            for o in session.scalars(select(LabelOutcome).order_by(LabelOutcome.label_id))
        ]
    return (runs, statuses, outcomes, _counts(engine))


# ------------------------------------------------------------------- the void


def test_it_moves_exactly_run_ns_outcomes_and_marks_the_run(db_engine: Engine) -> None:
    run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    result = _void(db_engine, run2)

    assert result.archived == 2
    with Session(db_engine) as session:
        assert {o.audit_run_id for o in session.scalars(select(LabelOutcome))} == {run1}
        archived = session.scalars(select(LabelOutcomeVoided)).all()
        assert {(a.audit_run_id, a.voided_at, a.skip_reason) for a in archived} == {
            (run2, NOW, "off_watch_untradeable")
        }
        assert len(archived) == 2
        run = session.get(AuditRun, run2)
        assert run is not None
        # status keeps recording what the run did; the void is a separate fact.
        assert (run.status, run.voided_at, run.void_reason) == ("succeeded", NOW, REASON)
        # Its accuracy rows stay where they are.
        assert session.scalar(
            select(func.count())
            .select_from(SentimentAccuracy)
            .where(SentimentAccuracy.audit_run_id == run2)
        ) == 1
        first = session.get(AuditRun, run1)
        assert first is not None and first.voided_at is None


def test_it_restores_unaudited_with_nulls(db_engine: Engine) -> None:
    _run1, run2 = _history(db_engine, (("rules", "unaudited", "active"),))
    result = _void(db_engine, run2)
    assert _status(db_engine, "rules") == ("unaudited", None, None, None)
    assert [
        (r.source, r.from_status, r.to_status, r.changed_at, r.reason, r.audit_run_id)
        for r in result.restorations
    ] == [("rules", "active", "unaudited", None, None, None)]


def test_it_restores_active_from_the_earlier_transition(db_engine: Engine) -> None:
    """Q24c: ``changed_at`` goes back to exactly what run 1 wrote -- its
    ``started_at`` -- never run 1's audit_log ``at``, which is its finish."""
    with Session(db_engine) as session:
        run1 = _run(session, T1)
        _move(session, run1, T1, "massive", "unaudited", "active")
        session.commit()
    before = _status(db_engine, "massive")
    with Session(db_engine) as session:
        run2 = _run(session, T2)
        _move(session, run2, T2, "massive", "active", "demoted")
        session.commit()
    _void(db_engine, run2)
    # The fixture's start and finish differ, so the old ``at`` rule fails here.
    assert T1 != T1 + RUN_LENGTH
    after = _status(db_engine, "massive")
    assert after == (
        "active",
        T1,
        f"restored when run {run2} was voided: {REASON}; "
        "the original reason text is not retained",
        run1,
    )
    # changed_at and audit_run_id are what stood before run 2; only the
    # reason differs, because audit_log never stored the original.
    assert (after[0], after[1], after[3]) == (before[0], before[1], before[3])


def test_it_restores_demoted_from_the_earlier_transition(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        run1 = _run(session, T1)
        _move(session, run1, T1, "massive", "unaudited", "demoted")
        session.commit()
    before = _status(db_engine, "massive")
    with Session(db_engine) as session:
        run2 = _run(session, T2)
        _move(session, run2, T2, "massive", "demoted", "active")
        session.commit()
    _void(db_engine, run2, reason="  a bad grade  ")
    after = _status(db_engine, "massive")
    # Q24c: run 1's started_at, not its finish (T1 + RUN_LENGTH).
    assert after == (
        "demoted",
        T1,
        restored_reason(run2, "a bad grade"),
        run1,
    )
    assert (after[0], after[1], after[3]) == (before[0], before[1], before[3])
    assert restored_reason(run2, "a bad grade") == (
        f"restored when run {run2} was voided: a bad grade; "
        "the original reason text is not retained"
    )


def test_it_leaves_a_source_run_n_did_not_move_alone(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        run1 = _run(session, T1)
        _move(session, run1, T1, "rules", "unaudited", "active")
        session.commit()
    with Session(db_engine) as session:
        run2 = _run(session, T2)
        _move(session, run2, T2, "massive", "unaudited", "demoted")
        session.commit()
    rules_before = _status(db_engine, "rules")
    result = _void(db_engine, run2)
    assert _status(db_engine, "rules") == rules_before
    assert [r.source for r in result.restorations] == ["massive"]


def test_it_writes_one_audit_log_row_per_restored_source(db_engine: Engine) -> None:
    _run1, run2 = _history(
        db_engine, (("massive", "active", "demoted"), ("rules", "unaudited", "active"))
    )
    before = _counts(db_engine)
    _void(db_engine, run2)
    after = _counts(db_engine)
    assert after["audit_log"] == before["audit_log"] + 2
    with Session(db_engine) as session:
        rows = session.scalars(
            select(AuditLog).where(AuditLog.at == NOW).order_by(AuditLog.field)
        ).all()
    assert [(r.category, r.field, r.previous_value, r.new_value) for r in rows] == [
        ("sentiment", "massive", "demoted", "active"),
        ("sentiment", "rules", "active", "unaudited"),
    ]
    # The void notifies nothing itself: the script emits after the commit.
    assert after["notification"] == before["notification"] == 0


def test_the_notice_names_the_run_the_reason_and_each_restoration(db_engine: Engine) -> None:
    run1, run2 = _history(
        db_engine, (("massive", "active", "demoted"), ("rules", "unaudited", "active"))
    )
    title, body = void_notice_text(_void(db_engine, run2))
    assert title == f"Sentiment audit run {run2} voided"
    assert REASON in body
    # Run 1's started_at (14:00 UTC), the instant its transition carries (Q24c).
    assert "massive: demoted -> active (run 1's transition at 2026-10-31 10:00 EDT)" in body
    assert "rules: active -> unaudited (never audited)" in body
    assert "2 label outcome(s) archived" in body
    assert body.isascii()


# ------------------------------------------------------------------ refusals


def _assert_refused(
    engine: Engine,
    caplog: pytest.LogCaptureFixture,
    rule: VoidRule,
    run_id: int,
    reason: str = REASON,
    now: datetime = NOW,
) -> None:
    before = _snapshot(engine)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="corollary.data.news.audit_void"):
        with pytest.raises(VoidRefused) as refused:
            _void(engine, run_id, reason=reason, now=now)
    assert refused.value.refusal.rule is rule
    assert _snapshot(engine) == before
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "sentiment_audit_void_refused"]
    assert getattr(record, "rule") == rule.value
    assert getattr(record, "inputs")["run_id"] == run_id
    assert getattr(record, "at") == now.isoformat()


def test_a_nonexistent_run_is_refused(db_engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    _history(db_engine, (("massive", "active", "demoted"),))
    _assert_refused(db_engine, caplog, VoidRule.RUN_NOT_FOUND, 99)


def test_a_running_run_is_refused(db_engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    _history(db_engine, (("massive", "active", "demoted"),))
    with Session(db_engine) as session:
        running = _run(session, NOW - timedelta(minutes=1), status="running")
        session.commit()
    _assert_refused(db_engine, caplog, VoidRule.RUN_RUNNING, running)


def test_any_running_run_refuses_the_void(db_engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    with Session(db_engine) as session:
        _run(session, NOW - timedelta(minutes=1), status="running")
        session.commit()
    _assert_refused(db_engine, caplog, VoidRule.ANOTHER_RUN_RUNNING, run2)


def test_an_already_voided_run_is_refused(db_engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    _void(db_engine, run2)
    _assert_refused(
        db_engine, caplog, VoidRule.ALREADY_VOIDED, run2, now=NOW + timedelta(minutes=1)
    )


def test_a_run_that_is_not_the_latest_is_refused(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    run1, _run2 = _history(db_engine, (("massive", "active", "demoted"),))
    _assert_refused(db_engine, caplog, VoidRule.NOT_LATEST, run1)


def test_a_voided_later_run_makes_the_earlier_one_the_latest(db_engine: Engine) -> None:
    run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    _void(db_engine, run2)
    _void(db_engine, run1, now=NOW + timedelta(minutes=1))
    assert _status(db_engine, "massive") == ("unaudited", None, None, None)


@pytest.mark.parametrize("reason", ["", "   ", " \t\n "])
def test_an_empty_reason_is_refused(
    db_engine: Engine, caplog: pytest.LogCaptureFixture, reason: str
) -> None:
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    _assert_refused(db_engine, caplog, VoidRule.EMPTY_REASON, run2, reason=reason)


def test_voiding_a_failed_run_with_nothing_in_it_marks_it_voided(db_engine: Engine) -> None:
    """A failed run that graded nothing and moved nothing is voided like any
    other (ruling M3). There is no "nothing to void" refusal: a failed run
    after N is exactly what stands between N and its void."""
    _history(db_engine, (("massive", "active", "demoted"),))
    with Session(db_engine) as session:
        failed = _run(session, NOW - timedelta(hours=1), status="failed")
        session.commit()
    before = _snapshot(db_engine)
    result = _void(db_engine, failed)

    assert (result.archived, result.restorations, result.run_status) == (0, (), "failed")
    with Session(db_engine) as session:
        run = session.get(AuditRun, failed)
        assert run is not None
        assert (run.status, run.voided_at, run.void_reason) == ("failed", NOW, REASON)
    # Every other row is as it was: no status moved, no outcome, no log row.
    _runs, statuses, outcomes, counts = _snapshot(db_engine)
    assert (statuses, outcomes, counts) == before[1:]
    _title, body = void_notice_text(result)
    assert f"No source's status was restored: run {failed} failed and moved none." in body


def _graded(engine: Engine, run_id: int) -> set[tuple[int, str]]:
    with Session(engine) as session:
        return {
            (o.label_id, o.window)
            for o in session.scalars(
                select(LabelOutcome).where(LabelOutcome.audit_run_id == run_id)
            )
        }


def test_a_failed_run_after_n_is_voided_first_and_then_n(
    db_engine: Engine, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run N succeeds. Run M, after it, grades labels N never saw and then
    fails: step d's outcomes are committed, its transitions never are. Newest
    first (R4): voiding N is refused while M stands; voiding M archives M's
    outcomes and restores nothing; then N is voided. The next real run grades
    both sets of labels again."""
    _five_session_week(db_engine)
    n = asyncio.run(run_sentiment_audit(_deps(db_engine), trigger="scheduled"))
    graded_n = _graded(db_engine, n.run_id)
    assert graded_n

    with Session(db_engine) as session:
        for day in FLOOR_SESSIONS:
            at = datetime.combine(day, time(11, 0), tzinfo=NYSE_TZ).astimezone(UTC)
            _label(session, ticker="AAPL", source="rules", direction="bullish", at=at)
        session.commit()

    def broken(*_args: object) -> object:
        raise RuntimeError("SYNTHETIC failure after the outcomes were written")

    with monkeypatch.context() as patched:
        patched.setattr(audit_runner, "_load_accuracy_inputs", broken)
        with pytest.raises(RuntimeError, match="SYNTHETIC"):
            asyncio.run(
                run_sentiment_audit(
                    _deps(db_engine, clock=TickingClock(SATURDAY_10 + timedelta(hours=1))),
                    trigger="manual",
                )
            )
    with Session(db_engine) as session:
        m = session.scalars(select(AuditRun).order_by(AuditRun.id.desc())).first()
        assert m is not None and m.status == "failed" and m.id != n.run_id
        m_id = m.id
    graded_m = _graded(db_engine, m_id)
    assert graded_m and not graded_m & graded_n

    void_at = SATURDAY_10 + timedelta(hours=2)
    _assert_refused(db_engine, caplog, VoidRule.NOT_LATEST, n.run_id, now=void_at)

    statuses_before = _snapshot(db_engine)[1]
    voided_m = _void(db_engine, m_id, now=void_at)
    assert (voided_m.archived, voided_m.restorations) == (len(graded_m), ())
    assert _snapshot(db_engine)[1] == statuses_before
    assert f"run {m_id} failed and moved none" in void_notice_text(voided_m)[1]

    voided_n = _void(db_engine, n.run_id, now=void_at + timedelta(minutes=1))
    assert voided_n.archived == len(graded_n)
    assert _status(db_engine, "massive") == ("unaudited", None, None, None)
    assert _status(db_engine, "rules") == ("unaudited", None, None, None)

    again = asyncio.run(
        run_sentiment_audit(
            _deps(db_engine, clock=TickingClock(SATURDAY_10 + timedelta(hours=3))),
            trigger="manual",
        )
    )
    with Session(db_engine) as session:
        archived = {
            (a.audit_run_id, a.label_id, a.window)
            for a in session.scalars(select(LabelOutcomeVoided))
        }
    assert archived == {(n.run_id, *key) for key in graded_n} | {
        (m_id, *key) for key in graded_m
    }
    assert _graded(db_engine, again.run_id) == graded_n | graded_m


def test_a_clock_behind_the_run_is_refused(db_engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    _assert_refused(db_engine, caplog, VoidRule.CLOCK_BEHIND, run2, now=T2 + RUN_LENGTH)


def test_a_status_naming_run_n_without_its_audit_row_is_refused(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    with Session(db_engine) as session:
        row = session.get(SentimentSourceStatus, "rules")
        assert row is not None
        row.status, row.changed_at, row.reason, row.audit_run_id = "active", T2, "x", run2
        session.commit()
    _assert_refused(db_engine, caplog, VoidRule.RESTORE_UNPROVABLE, run2)


def test_a_restore_with_no_earlier_transition_is_refused(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    with Session(db_engine) as session:
        run1 = _run(session, T1)
        _move(session, run1, T1, "massive", "active", "demoted")  # nothing put it at active
        session.commit()
    _assert_refused(db_engine, caplog, VoidRule.RESTORE_UNPROVABLE, run1)


def test_an_earlier_transition_in_a_voided_run_never_supplies_a_restore(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Run 2 (voided) moved massive active -> demoted; run 3 claims to move it
    demoted -> active. Only standing runs count, and run 1 left it ``active``,
    so ``demoted`` cannot be proven and the void is refused."""
    run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    with Session(db_engine) as session:
        run3 = _run(session, NOW - timedelta(hours=2))
        # Written as a run would, though no run could: run 2 is voided below
        # first in real life. Forced here to prove the reader's exclusion.
        _move(session, run3, NOW - timedelta(hours=2), "massive", "demoted", "active")
        session.get(AuditRun, run2).voided_at = NOW - timedelta(hours=3)  # type: ignore[union-attr]
        session.get(AuditRun, run2).void_reason = "forced"  # type: ignore[union-attr]
        session.commit()
    _assert_refused(db_engine, caplog, VoidRule.RESTORE_UNPROVABLE, run3)
    assert run1 == 1


def test_a_malformed_previous_value_is_refused_by_the_planner() -> None:
    """The CHECK keeps such a row out of the table; the planner refuses it anyway."""
    run = RunFacts(2, "scheduled", "succeeded", T2, T2 + RUN_LENGTH, None)
    planned = plan_restorations(
        run,
        REASON,
        [SentimentLogRow(1, T2 + RUN_LENGTH, "massive", "retired", "demoted", 2)],
        [SourceRow("massive", "demoted", T2, "r", 2)],
        [run],
    )
    assert isinstance(planned, Refusal) and planned.rule is VoidRule.RESTORE_UNPROVABLE


def test_the_planner_restores_the_earlier_runs_started_at() -> None:
    """Q24c, pure: the restored ``changed_at`` is run 1's ``started_at``,
    read through the join that names run 1 -- not its audit_log ``at``."""
    run1 = RunFacts(1, "scheduled", "succeeded", T1, T1 + RUN_LENGTH, None)
    run2 = RunFacts(2, "scheduled", "succeeded", T2, T2 + RUN_LENGTH, None)
    planned = plan_restorations(
        run2,
        REASON,
        [
            SentimentLogRow(1, T1 + RUN_LENGTH, "massive", "unaudited", "demoted", 1),
            SentimentLogRow(2, T2 + RUN_LENGTH, "massive", "demoted", "active", 2),
        ],
        [SourceRow("massive", "active", T2, "r", 2)],
        [run1, run2],
    )
    assert not isinstance(planned, Refusal)
    assert [(r.to_status, r.changed_at, r.audit_run_id) for r in planned] == [
        ("demoted", T1, 1)
    ]


# ---------------------------------------------------- the real runner after it


def test_the_next_run_grades_the_voided_labels_again(db_engine: Engine) -> None:
    _five_session_week(db_engine)
    first = asyncio.run(run_sentiment_audit(_deps(db_engine), trigger="scheduled"))
    with Session(db_engine) as session:
        graded_first = {
            (o.label_id, o.window)
            for o in session.scalars(select(LabelOutcome).where(LabelOutcome.audit_run_id == first.run_id))
        }
    assert graded_first
    assert _status(db_engine, "massive")[0] == "demoted"

    result = _void(db_engine, first.run_id, now=SATURDAY_10 + timedelta(hours=1))
    assert result.archived == len(graded_first)
    assert _status(db_engine, "massive") == ("unaudited", None, None, None)
    assert _status(db_engine, "rules") == ("unaudited", None, None, None)

    second = asyncio.run(
        run_sentiment_audit(
            _deps(db_engine, clock=TickingClock(SATURDAY_10 + timedelta(hours=2))),
            trigger="manual",
        )
    )
    with Session(db_engine) as session:
        graded_again = {
            (o.label_id, o.window)
            for o in session.scalars(
                select(LabelOutcome).where(LabelOutcome.audit_run_id == second.run_id)
            )
        }
        archived = {
            (a.label_id, a.window) for a in session.scalars(select(LabelOutcomeVoided))
        }
        bases = {
            (a.source, a.basis)
            for a in session.scalars(
                select(SentimentAccuracy).where(SentimentAccuracy.audit_run_id == second.run_id)
            )
        }
        logs = [
            (r.field, r.previous_value, r.new_value)
            for r in session.scalars(
                select(AuditLog)
                .where(AuditLog.category == "sentiment")
                .order_by(AuditLog.id)
            )
        ]
        labels = int(session.scalar(select(func.count()).select_from(SentimentLabelRow)) or 0)
    # Every label the voided run graded is graded again, by the next run.
    assert graded_again == graded_first == archived
    assert len(graded_again) == 2 * labels
    # The since-demotion basis excludes the voided run: massive was judged as
    # unaudited, on trailing_28d only, and demoted afresh from unaudited.
    assert ("massive", "since_demotion") not in bases
    assert logs[-2:] == [("massive", "unaudited", "demoted"), ("rules", "unaudited", "active")]
    assert _status(db_engine, "massive")[3] == second.run_id


def test_voiding_a_repromotion_restores_the_since_demotion_basis(db_engine: Engine) -> None:
    """Run 1 demotes massive; run 2 re-promotes it; voiding run 2 puts massive
    back to demoted with run 1's instant, so the next run's ``since_demotion``
    counts from run 1's demotion, never from the voided run."""
    with Session(db_engine) as session:
        run1 = _run(session, T1)
        _move(session, run1, T1, "massive", "unaudited", "demoted")
        run2 = _run(session, T2)
        _move(session, run2, T2, "massive", "demoted", "active")
        session.commit()
    _void(db_engine, run2)
    status = _status(db_engine, "massive")
    assert status[0] == "demoted" and status[1] == T1 and status[3] == run1


def test_a_label_referenced_during_the_demoting_run_counts_after_the_restore(
    db_engine: Engine,
) -> None:
    """Q24c's reason: a label whose reference instant lies after run 1's start
    and before its finish counted toward re-promotion before run 2 moved the
    source, so it still counts once run 2 is voided. Under the old rule
    (``changed_at`` = run 1's audit_log ``at``, its finish) the strict ``>``
    of ``since_demotion`` would drop it. Read through the runner's own
    status loader and :func:`compute_accuracy`."""
    with Session(db_engine) as session:
        run1 = _run(session, T1)
        _move(session, run1, T1, "massive", "unaudited", "demoted")
        session.commit()
    with Session(db_engine) as session:
        row = session.get(SentimentSourceStatus, "massive")
        assert row is not None
        before = audit_runner._current_status(row)
        run2 = _run(session, T2)
        _move(session, run2, T2, "massive", "demoted", "active")
        session.commit()
    _void(db_engine, run2)
    with Session(db_engine) as session:
        row = session.get(SentimentSourceStatus, "massive")
        assert row is not None
        restored = audit_runner._current_status(row)
    assert restored.status is SourceStatus.DEMOTED
    inside = GradedOutcome(
        source="massive",
        window="1h",
        scope=Scope.WATCH,
        reference_at=T1 + RUN_LENGTH / 2,
        correct=True,
        neutral=False,
    )
    assert T1 < inside.reference_at < T1 + RUN_LENGTH
    rows = compute_accuracy(restored, [inside], NOW)
    since = next(
        r
        for r in rows
        if (r.window, r.basis, r.scope)
        == ("1h", AccuracyBasis.SINCE_DEMOTION, AccuracyScope.ALL)
    )
    assert (since.graded, since.correct) == (1, 1)
    # It counted before run 2, too: the void restored that very basis.
    assert restored == before


def test_a_void_restores_exactly_the_status_the_real_runner_wrote(db_engine: Engine) -> None:
    """Q24c against the real runner, not the synthetic ``_move``: the runner
    writes a transition's ``changed_at`` as its run's ``started_at`` and the
    audit_log row at ``finished_at``, two different instants. A later run that
    moves the source, voided, leaves the status row as the runner left it,
    the reason aside (audit_log never stored it)."""
    _five_session_week(db_engine)
    first = asyncio.run(run_sentiment_audit(_deps(db_engine), trigger="scheduled"))
    before = _status(db_engine, "massive")
    with Session(db_engine) as session:
        run = session.get(AuditRun, first.run_id)
        assert run is not None and run.finished_at is not None
        started, finished = run.started_at, run.finished_at
        logged = session.scalar(
            select(AuditLog.at).where(
                AuditLog.category == "sentiment", AuditLog.field == "massive"
            )
        )
    assert before[0] == "demoted" and before[3] == first.run_id
    # The premise Q24c rests on.
    assert before[1] == started
    assert logged == finished != started

    later = SATURDAY_10 + timedelta(hours=1)
    assert later > finished
    with Session(db_engine) as session:
        run2 = _run(session, later)
        _move(session, run2, later, "massive", "demoted", "active")
        session.commit()
    _void(db_engine, run2, now=later + timedelta(hours=1))
    assert _status(db_engine, "massive") == (
        before[0],
        before[1],
        restored_reason(run2, REASON),
        before[3],
    )


def test_a_run_cannot_open_with_its_span_over_a_void(db_engine: Engine) -> None:
    """The void's restore rows are stamped ``voided_at``. A run whose clock
    reads earlier would hold that instant in its span ``(started_at,
    finished_at]``, and the history join would hand the restore rows to it.
    At the void instant exactly, the span excludes it, so the run opens."""
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    _void(db_engine, run2)
    before = _snapshot(db_engine)
    with pytest.raises(RuntimeError, match="clock reads earlier"):
        audit_runner._open_run(
            _sessions(db_engine), lambda: NOW - timedelta(microseconds=1), "manual", "c0ffee"
        )
    assert _snapshot(db_engine) == before
    opened, started_at = audit_runner._open_run(
        _sessions(db_engine), lambda: NOW, "manual", "c0ffee"
    )
    assert started_at == NOW
    with Session(db_engine) as session:
        run = session.get(AuditRun, opened)
        assert run is not None and (run.status, run.started_at) == ("running", NOW)


# ------------------------------------------ a void and a run's open, at once

#: How long a hook holds one writer mid-transaction while the other tries to
#: write. Far below ``SQLITE_BUSY_TIMEOUT_MS`` (5 s), so a writer blocked on
#: the write lock is still waiting when the hook lets go; far above what an
#: unblocked open or void takes, so one that is *not* blocked has finished.
#: The passing path is not a timing assertion: under the lock the other
#: writer cannot finish inside the hook at any speed.
_HOLD_SECONDS = 0.5


def _span_holds(run: AuditRun, at: datetime) -> bool:
    """The history join's span ``(started_at, finished_at]``; open while running."""
    return run.started_at < at and (run.finished_at is None or at <= run.finished_at)


def test_a_run_opening_mid_void_waits_for_the_void(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auditor's interleaving (M1): a run opens between the void's reads
    and its writes. Without the write lock, the run's ``running`` row commits
    beside the void and the "any run is running" refusal never fires. With
    it, the run waits for the void's commit and only then reads its clock, so
    it opens strictly after the void and no restore row lies in its span."""
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    clock = SharedClock(NOW)
    opened: list[tuple[int, datetime]] = []
    errors: list[BaseException] = []
    finished_inside_the_void: list[bool] = []

    def open_run() -> None:
        try:
            opened.append(audit_runner._open_run(_sessions(db_engine), clock, "manual", "beef"))
        except BaseException as exc:  # pragma: no cover - only on a real failure
            errors.append(exc)

    opener = threading.Thread(target=open_run, name="run-opener")
    real_load_sources = audit_void._load_sources

    def between_reads_and_writes(session: Session) -> list[SourceRow]:
        opener.start()
        opener.join(_HOLD_SECONDS)
        finished_inside_the_void.append(not opener.is_alive())
        return real_load_sources(session)

    monkeypatch.setattr(audit_void, "_load_sources", between_reads_and_writes)
    result = void_audit_run(
        _sessions(db_engine), run_id=run2, reason=REASON, clock=clock, correlation_id="c0ffee"
    )
    opener.join(10)
    assert not opener.is_alive() and errors == []
    assert finished_inside_the_void == [False], "the run opened while the void was mid-transaction"
    [(run3, started_at)] = opened
    assert started_at > result.voided_at
    with Session(db_engine) as session:
        restore_rows = session.scalars(
            select(AuditLog).where(AuditLog.at == result.voided_at)
        ).all()
        runs = session.scalars(select(AuditRun)).all()
        session.expunge_all()
    assert [r.field for r in restore_rows] == ["massive"]
    assert [
        (run.id, row.id) for run in runs for row in restore_rows if _span_holds(run, row.at)
    ] == []
    assert {run.id: run.status for run in runs}[run3] == "running"


def test_a_void_arriving_mid_open_waits_and_then_sees_the_run(db_engine: Engine) -> None:
    """The reverse order (M1): a void lands between a run's clock reading and
    its ``running`` row. Without the write lock the void commits first, its
    ``voided_at`` is later than the run's reading, and that Saturday's run
    refuses to start ("clock reads earlier"). With it, the void waits for the
    run's commit and is then refused as ``another_run_running``."""
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    clock = SharedClock(NOW)
    outcome: list[object] = []
    finished_inside_the_open: list[bool] = []

    def void() -> None:
        try:
            outcome.append(
                void_audit_run(
                    _sessions(db_engine),
                    run_id=run2,
                    reason=REASON,
                    clock=clock,
                    correlation_id="c0ffee",
                )
            )
        except BaseException as exc:
            outcome.append(exc)

    voider = threading.Thread(target=void, name="voider")

    def the_runs_clock() -> datetime:
        # ``_open_run`` reads its clock once, after taking the lock and
        # before any check: the void is let loose exactly there.
        reading = clock()
        voider.start()
        voider.join(_HOLD_SECONDS)
        finished_inside_the_open.append(not voider.is_alive())
        return reading

    run3, started_at = audit_runner._open_run(
        _sessions(db_engine), the_runs_clock, "manual", "beef"
    )
    voider.join(10)
    assert not voider.is_alive()
    assert finished_inside_the_open == [False], "the void committed while the run was opening"
    [refused] = outcome
    assert isinstance(refused, VoidRefused), refused
    assert refused.refusal.rule is VoidRule.ANOTHER_RUN_RUNNING
    with Session(db_engine) as session:
        voided = session.get(AuditRun, run2)
        opened = session.get(AuditRun, run3)
        assert voided is not None and voided.voided_at is None
        assert opened is not None and (opened.status, opened.started_at) == ("running", started_at)


# ------------------------------------------------- the commit and the lock


def test_the_void_returns_straight_after_its_commit(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1, the auditor's reproduction: a Ctrl-C landing on the success log
    line used to escape the void after its COMMIT, so the caller never had the
    result to notify with. Nothing of the void's own runs after the commit
    now; the line is the caller's (:func:`log_voided`)."""
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(audit_void.logger, "info", interrupt)
    try:
        result = _void(db_engine, run2)
    except KeyboardInterrupt:  # pragma: no cover - only while F1 is open
        pytest.fail("a KeyboardInterrupt after the void's commit escaped void_audit_run")
    assert (result.run_id, result.voided_at) == (run2, NOW)
    with Session(db_engine) as session:
        run = session.get(AuditRun, run2)
        assert run is not None and run.voided_at == NOW


def test_the_success_line_is_logged_by_the_caller_not_the_void(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    _run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    with caplog.at_level(logging.DEBUG, logger="corollary.data.news.audit_void"):
        result = _void(db_engine, run2)
        assert [r for r in caplog.records if r.name == audit_void.logger.name] == []
        log_voided(result)
    [record] = [
        r for r in caplog.records if getattr(r, "event", None) == "sentiment_audit_run_voided"
    ]
    assert record.levelno == logging.INFO
    assert getattr(record, "audit_run_id") == run2
    assert getattr(record, "reason") == REASON
    assert getattr(record, "archived_outcomes") == 2
    assert getattr(record, "restorations") == [
        {"source": "massive", "from": "demoted", "to": "active", "audit_run_id": _run1}
    ]
    assert getattr(record, "at") == NOW.isoformat()
    assert getattr(record, "correlation_id") == "c0ffee"


def test_read_back_rebuilds_exactly_the_result_the_void_returned(db_engine: Engine) -> None:
    """F1: a caller whose void raised reads it back. Each kind of restoration
    -- to a run's earlier status, and to ``unaudited`` -- comes back as the
    void planned it."""
    run1, run2 = _history(
        db_engine, (("massive", "active", "demoted"), ("rules", "unaudited", "active"))
    )
    result = _void(db_engine, run2)
    assert {r.audit_run_id for r in result.restorations} == {run1, None}
    # Q24c: run 1's started_at, so the read-back must derive the same instant.
    assert {(r.source, r.changed_at) for r in result.restorations} == {
        ("massive", T1),
        ("rules", None),
    }
    assert read_back_void(
        _sessions(db_engine), run_id=run2, reason=f"  {REASON} ", voided_at=NOW,
        correlation_id="c0ffee",
    ) == result


def test_read_back_finds_no_void_that_is_not_this_one(db_engine: Engine) -> None:
    run1, run2 = _history(db_engine, (("massive", "active", "demoted"),))
    sessions = _sessions(db_engine)

    def read(run_id: int, reason: str = REASON, at: datetime = NOW) -> VoidResult | None:
        return read_back_void(
            sessions, run_id=run_id, reason=reason, voided_at=at, correlation_id="c0ffee"
        )

    assert read(run2) is None  # nothing voided yet
    _void(db_engine, run2)
    assert read(run2) is not None
    assert read(run2, reason="another reason") is None
    assert read(run2, at=NOW + timedelta(microseconds=1)) is None
    assert read(run1) is None
    assert read(99) is None


@pytest.mark.parametrize("rule", [VoidRule.RUN_NOT_FOUND, VoidRule.RESTORE_UNPROVABLE])
def test_a_refusal_is_logged_after_the_write_lock_is_released(
    db_engine: Engine, rule: VoidRule
) -> None:
    """F2: the refusal's log line used to be written inside ``BEGIN
    IMMEDIATE``, so a stderr that blocks (a conhost QuickEdit selection, an
    unread pipe) held every other writer -- the watchdog's on the event loop
    among them -- past ``SQLITE_BUSY_TIMEOUT_MS``. A second connection must
    take the lock **during** the log call."""
    if rule is VoidRule.RUN_NOT_FOUND:
        _history(db_engine, (("massive", "active", "demoted"),))
        run_id = 99
    else:
        with Session(db_engine) as session:
            run_id = _run(session, T1)
            _move(session, run_id, T1, "massive", "active", "demoted")
            session.commit()
    database = db_engine.url.database
    assert database is not None
    free_during_the_log: list[bool] = []

    class LockProbe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if getattr(record, "event", None) == "sentiment_audit_void_refused":
                free_during_the_log.append(_lock_is_free(Path(database)))

    target = logging.getLogger(audit_void.logger.name)
    probe = LockProbe(level=logging.WARNING)
    level = target.level
    target.addHandler(probe)
    target.setLevel(logging.WARNING)
    try:
        with pytest.raises(VoidRefused) as refused:
            _void(db_engine, run_id)
    finally:
        target.removeHandler(probe)
        target.setLevel(level)
    assert refused.value.refusal.rule is rule
    assert free_during_the_log == [True]


@pytest.mark.parametrize("step", ["rollback", "close"])
def test_a_refusal_is_logged_even_when_releasing_the_lock_raises(
    db_engine: Engine, caplog: pytest.LogCaptureFixture, step: str
) -> None:
    """E3: F2 moved the refusal's log line after the release, so a rollback
    or close that raised skipped it, and rule 8's record of the refusal was
    lost. It is logged in a ``finally`` now. The release's own error still
    propagates: it is a database fault, and dressing it as a clean refusal
    would hide it. Nothing is written either way."""
    _history(db_engine, (("massive", "active", "demoted"),))
    before = _snapshot(db_engine)

    class ReleaseRaises(Session):
        def rollback(self) -> None:
            if step != "rollback":
                return super().rollback()
            # Fails without rolling back: the ``with``'s close still does.
            raise RuntimeError("SYNTHETIC: the rollback failed")

        def close(self) -> None:
            super().close()
            if step == "close":
                raise RuntimeError("SYNTHETIC: the close failed")

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="corollary.data.news.audit_void"):
        with pytest.raises(RuntimeError, match=f"SYNTHETIC: the {step} failed"):
            void_audit_run(
                lambda: ReleaseRaises(db_engine),
                run_id=99,
                reason=REASON,
                clock=lambda: NOW,
                correlation_id="c0ffee",
            )
    [record] = [
        r for r in caplog.records if getattr(r, "event", None) == "sentiment_audit_void_refused"
    ]
    assert getattr(record, "rule") == VoidRule.RUN_NOT_FOUND.value
    assert getattr(record, "inputs")["run_id"] == 99
    assert getattr(record, "at") == NOW.isoformat()
    assert _snapshot(db_engine) == before


# ------------------------------------------------------------ the catch-up


def _catch_up_ran(engine: Engine, slot: datetime, now: datetime) -> bool:
    return scheduler._audit_runs_since_blocking(_sessions(engine), slot, now).ran


def test_a_voided_run_does_not_count_as_the_slots_run(db_engine: Engine) -> None:
    slot = T2
    with Session(db_engine) as session:
        run = _run(session, slot + timedelta(seconds=1))
        session.commit()
    assert _catch_up_ran(db_engine, slot, NOW) is True
    with Session(db_engine) as session:
        row = session.get(AuditRun, run)
        assert row is not None
        row.voided_at, row.void_reason = NOW, REASON
        session.commit()
    assert _catch_up_ran(db_engine, slot, NOW) is False
