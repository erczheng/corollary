"""``GET /api/settings/sentiment`` and ``POST /api/settings/sentiment/audit``.

Step 6's done-criterion: *"Settings shows each source's status, graded counts
per window, its watch/discovery split, and how many more labels each needs to
become eligible."* And the design's *API*: the manual trigger is limited to
once an hour, and there is **no reinstate route** -- the audit is the only
writer of the status (Q6).

The ``POST`` runs the real entry point (the scheduler's
``run_sentiment_audit_now``, over the real runner) on a database with no
labels, so a run grades nothing and succeeds in milliseconds; only the
scheduler's ``sentiment_audit_deps`` is replaced, because building real
``ContextServices`` would construct vendor clients.
"""

import ast
import inspect
import logging
import re
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session
from fastapi.routing import iter_route_contexts
from starlette.routing import BaseRoute, Mount, WebSocketRoute

from corollary.api import operator as operator_module
from corollary.api.app import create_app, no_socket_supervisor
from corollary.api.deps import ServiceRegistry, scheduler_status
from corollary.api.routes import sentiment as sentiment_routes
from corollary.api.routes.sentiment import MANUAL_AUDIT_INTERVAL, audit_now
from corollary.data.news import audit_runner
from corollary.data.news.audit import (
    DEMOTION_FLOOR,
    MIN_ELIGIBLE_SESSIONS,
    REPROMOTION_FLOOR,
    AccuracyBasis,
    AccuracyRow,
    AccuracyScope,
    CurrentStatus,
    SourceStatus,
    decide_transition,
)
from corollary.data.news.audit_runner import SentimentAuditDeps, WatchUniverseUnavailable
from corollary.data.providers.interface import Bar
from corollary.db.models import AuditRun, SentimentSourceStatus
from corollary.engine.runtime import OperatorNotice
from corollary.engine import scheduler as scheduler_module
from corollary.engine.scheduler import SENTIMENT_AUDIT_JOB, JobStatus

from .news_support import add_accuracy, add_audit_run, set_source_status

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
SENTIMENT = "/api/settings/sentiment"
AUDIT = "/api/settings/sentiment/audit"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def sentiment_client(app: FastAPI) -> Iterator[TestClient]:
    app.dependency_overrides[audit_now] = lambda: NOW
    with TestClient(app) as test_client:
        yield test_client


class _NoBars:
    """A ``MinuteBarSource`` a run over no labels never calls."""

    async def stock_bars(
        self, symbols: Sequence[str], **_: Any
    ) -> Mapping[str, Sequence[Bar]]:  # pragma: no cover - never reached
        raise AssertionError("a run over no labels fetches no bars")


def _deps(
    engine: Engine,
    *,
    clock: datetime = NOW,
    universe_error: BaseException | None = None,
) -> SentimentAuditDeps:
    async def watch_universe() -> Collection[str]:
        if universe_error is not None:
            raise universe_error
        return frozenset({"SPY", "MARKET"})

    return SentimentAuditDeps(
        session_factory=lambda: Session(engine),
        bars=_NoBars(),
        watch_universe=watch_universe,
        emit=lambda notice, at, correlation_id: None,
        clock=lambda: clock,
    )


def _arm(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    deps: SentimentAuditDeps | None,
) -> None:
    """Give the app context services, and the audit the deps the test chose.

    Patched on the scheduler, which ``run_sentiment_audit_now`` reads it
    from -- the route composes nothing of its own.
    """
    app.state.context_services = object()
    monkeypatch.setattr(
        scheduler_module, "sentiment_audit_deps", lambda services, clock=None: deps
    )


def _wait_for_run(app: FastAPI) -> None:
    task = app.state.sentiment_audit_task
    assert task is not None
    deadline = time.monotonic() + 10
    while not task.done():
        assert time.monotonic() < deadline, "the manual run never finished"
        time.sleep(0.01)


def _runs(engine: Engine) -> list[AuditRun]:
    with Session(engine) as session:
        return list(session.scalars(select(AuditRun).order_by(AuditRun.id)))


def _refusals(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.__dict__.get("event") == "sentiment_audit_refused"]


def _assert_logged(caplog: pytest.LogCaptureFixture, status: int) -> dict[str, Any]:
    """Rule 8's spirit: the rule, the inputs and the timestamp, every refusal."""
    (record,) = _refusals(caplog)
    fields = record.__dict__
    assert fields["status"] == status
    assert fields["rule"]
    assert isinstance(fields["inputs"], dict) and fields["inputs"]
    assert fields["at"] == NOW.isoformat()
    assert fields["correlation_id"]
    return fields


def _source(body: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [s for s in body["sources"] if s["source"] == name]
    return found


# --------------------------------------------------------------------------
# GET
# --------------------------------------------------------------------------


def test_unaudited_sources_say_never_audited_and_invent_no_date(
    sentiment_client: TestClient,
) -> None:
    response = sentiment_client.get(SENTIMENT)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [s["source"] for s in body["sources"]] == ["rules", "massive"]
    for source in body["sources"]:
        assert source["status"] == "unaudited"
        assert source["changedAt"] is None
        assert source["neverAudited"] is True
        assert source["statusMissing"] is False
        assert source["reason"] is None
        assert source["accuracy"] == []
        # Nothing counted on this basis: zero labels over zero sessions, a
        # count rather than a guess, so the whole session floor is needed.
        assert source["eligibility"] == {
            "kind": "demote",
            "basis": "trailing_28d",
            "floor": DEMOTION_FLOOR,
            "sessionFloor": MIN_ELIGIBLE_SESSIONS,
            "eligible": False,
            "windows": [
                {"window": "1h", "graded": 0, "needed": DEMOTION_FLOOR,
                 "sessions": 0, "sessionsNeeded": MIN_ELIGIBLE_SESSIONS},
                {"window": "1d", "graded": 0, "needed": DEMOTION_FLOOR,
                 "sessions": 0, "sessionsNeeded": MIN_ELIGIBLE_SESSIONS},
            ],
            "asOfRunId": None,
        }
    assert body["lastRun"] is None
    assert body["accuracyRun"] is None
    assert body["voidedRuns"] == []  # nothing voided: an empty list, never null
    assert body["nextRunAt"] is None
    assert body["manualAvailableAt"] is None


def test_an_active_source_serves_counts_split_and_how_many_more_at_the_floor(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """29 graded needs one more; 30 needs none. One session under the floor
    needs one more; over it needs none. Counts only -- no percentage."""
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=2),
                        finished_at=NOW - timedelta(days=2) + timedelta(minutes=3))
    set_source_status(db_engine, "rules", "active", at=NOW - timedelta(days=2), run_id=run)
    add_accuracy(db_engine, run, source="rules", window="1h",
                 watch=(20, 12, 3), discovery=(DEMOTION_FLOOR - 1 - 20, 5, 1),
                 sessions=MIN_ELIGIBLE_SESSIONS - 1, watch_sessions=3, discovery_sessions=2)
    add_accuracy(db_engine, run, source="rules", window="1d",
                 watch=(DEMOTION_FLOOR, 18, 0),
                 sessions=MIN_ELIGIBLE_SESSIONS + 1,
                 watch_sessions=MIN_ELIGIBLE_SESSIONS + 1, discovery_sessions=0)
    add_accuracy(db_engine, run, source="massive", window="1h")
    add_accuracy(db_engine, run, source="massive", window="1d")
    failed = add_audit_run(db_engine, started_at=NOW - timedelta(hours=5),
                           finished_at=NOW - timedelta(hours=5) + timedelta(minutes=1),
                           status="failed", trigger="manual",
                           detail='{"stage": "fetch"}')

    body = sentiment_client.get(SENTIMENT).json()
    rules = _source(body, "rules")
    assert rules["status"] == "active"
    assert rules["neverAudited"] is False
    assert datetime.fromisoformat(rules["changedAt"]) == NOW - timedelta(days=2)
    assert rules["reason"] == "test transition"
    assert rules["accuracy"] == [
        {
            "window": "1h",
            "basis": "trailing_28d",
            "combined": {"graded": DEMOTION_FLOOR - 1, "correct": 17, "neutral": 4,
                         "sessions": MIN_ELIGIBLE_SESSIONS - 1},
            "watch": {"graded": 20, "correct": 12, "neutral": 3, "sessions": 3},
            "discovery": {"graded": DEMOTION_FLOOR - 1 - 20, "correct": 5, "neutral": 1,
                          "sessions": 2},
        },
        {
            "window": "1d",
            "basis": "trailing_28d",
            "combined": {"graded": DEMOTION_FLOOR, "correct": 18, "neutral": 0,
                         "sessions": MIN_ELIGIBLE_SESSIONS + 1},
            "watch": {"graded": DEMOTION_FLOOR, "correct": 18, "neutral": 0,
                      "sessions": MIN_ELIGIBLE_SESSIONS + 1},
            "discovery": {"graded": 0, "correct": 0, "neutral": 0, "sessions": 0},
        },
    ]
    assert rules["eligibility"] == {
        "kind": "demote",
        "basis": "trailing_28d",
        "floor": DEMOTION_FLOOR,
        "sessionFloor": MIN_ELIGIBLE_SESSIONS,
        "eligible": False,
        "windows": [
            {"window": "1h", "graded": DEMOTION_FLOOR - 1, "needed": 1,
             "sessions": MIN_ELIGIBLE_SESSIONS - 1, "sessionsNeeded": 1},
            # Over the session floor is "none needed", never a negative count.
            {"window": "1d", "graded": DEMOTION_FLOOR, "needed": 0,
             "sessions": MIN_ELIGIBLE_SESSIONS + 1, "sessionsNeeded": 0},
        ],
        "asOfRunId": run,
    }
    # The figures are the succeeded run's; the newest run is the failed one.
    assert body["accuracyRun"]["id"] == run
    assert body["lastRun"]["id"] == failed
    assert body["lastRun"]["status"] == "failed"
    assert body["lastRun"]["trigger"] == "manual"
    # A manual run five hours ago leaves the limit open.
    assert body["manualAvailableAt"] is None


def test_a_source_at_both_floors_in_both_windows_is_eligible(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """The permit at the boundary: exactly the label floor over exactly the
    session floor, in both windows."""
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=1),
                        finished_at=NOW - timedelta(days=1))
    add_accuracy(db_engine, run, source="massive", window="1h", watch=(DEMOTION_FLOOR, 16, 0),
                 sessions=MIN_ELIGIBLE_SESSIONS, watch_sessions=MIN_ELIGIBLE_SESSIONS,
                 discovery_sessions=0)
    # Over the floor is still "none needed", never a negative count.
    add_accuracy(db_engine, run, source="massive", window="1d",
                 watch=(DEMOTION_FLOOR + 1, 16, 0),
                 sessions=MIN_ELIGIBLE_SESSIONS, watch_sessions=MIN_ELIGIBLE_SESSIONS,
                 discovery_sessions=0)
    massive = _source(sentiment_client.get(SENTIMENT).json(), "massive")
    assert massive["status"] == "unaudited"
    assert massive["eligibility"]["eligible"] is True
    assert [w["needed"] for w in massive["eligibility"]["windows"]] == [0, 0]
    assert [w["sessionsNeeded"] for w in massive["eligibility"]["windows"]] == [0, 0]


def test_one_session_under_the_floor_is_not_eligible_at_any_label_count(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """The reject at the boundary: well over the label floor, one session
    short in one window. The other window meeting both floors does not carry it."""
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=1),
                        finished_at=NOW - timedelta(days=1))
    add_accuracy(db_engine, run, source="rules", window="1h", watch=(DEMOTION_FLOOR + 10, 20, 0),
                 sessions=MIN_ELIGIBLE_SESSIONS - 1, watch_sessions=MIN_ELIGIBLE_SESSIONS - 1,
                 discovery_sessions=0)
    add_accuracy(db_engine, run, source="rules", window="1d", watch=(DEMOTION_FLOOR + 10, 20, 0),
                 sessions=MIN_ELIGIBLE_SESSIONS, watch_sessions=MIN_ELIGIBLE_SESSIONS,
                 discovery_sessions=0)
    eligibility = _source(sentiment_client.get(SENTIMENT).json(), "rules")["eligibility"]
    assert eligibility["eligible"] is False
    assert eligibility["windows"] == [
        {"window": "1h", "graded": DEMOTION_FLOOR + 10, "needed": 0,
         "sessions": MIN_ELIGIBLE_SESSIONS - 1, "sessionsNeeded": 1},
        {"window": "1d", "graded": DEMOTION_FLOOR + 10, "needed": 0,
         "sessions": MIN_ELIGIBLE_SESSIONS, "sessionsNeeded": 0},
    ]


def test_a_row_from_before_the_session_floor_is_never_eligible(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """NULL ``sessions`` is a count that does not exist: served as ``null``,
    with ``sessionsNeeded`` ``null`` beside it rather than a number nobody
    counted, and never eligible however many labels it carries."""
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=1),
                        finished_at=NOW - timedelta(days=1))
    for window in ("1h", "1d"):
        add_accuracy(db_engine, run, source="rules", window=window,
                     watch=(DEMOTION_FLOOR + 20, 30, 0), discovery=(DEMOTION_FLOOR, 20, 0))
    rules = _source(sentiment_client.get(SENTIMENT).json(), "rules")
    assert rules["eligibility"] == {
        "kind": "demote",
        "basis": "trailing_28d",
        "floor": DEMOTION_FLOOR,
        "sessionFloor": MIN_ELIGIBLE_SESSIONS,
        "eligible": False,
        "windows": [
            {"window": "1h", "graded": 2 * DEMOTION_FLOOR + 20, "needed": 0,
             "sessions": None, "sessionsNeeded": None},
            {"window": "1d", "graded": 2 * DEMOTION_FLOOR + 20, "needed": 0,
             "sessions": None, "sessionsNeeded": None},
        ],
        "asOfRunId": run,
    }
    for cell in rules["accuracy"]:
        for scope in ("combined", "watch", "discovery"):
            assert cell[scope]["sessions"] is None


def test_each_scope_carries_its_own_stored_session_count(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """Sessions do not sum across the split -- one session can hold a watch
    and a discovery label -- so each scope is read from its own row, never
    derived from the others."""
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=1),
                        finished_at=NOW - timedelta(days=1))
    add_accuracy(db_engine, run, source="massive", window="1h",
                 watch=(10, 6, 0), discovery=(8, 4, 0),
                 sessions=5, watch_sessions=4, discovery_sessions=3)
    add_accuracy(db_engine, run, source="massive", window="1d",
                 watch=(9, 6, 0), discovery=(7, 4, 0),
                 sessions=6, watch_sessions=2, discovery_sessions=7)
    massive = _source(sentiment_client.get(SENTIMENT).json(), "massive")
    assert [
        (c["window"], c["combined"]["sessions"], c["watch"]["sessions"],
         c["discovery"]["sessions"])
        for c in massive["accuracy"]
    ] == [("1h", 5, 4, 3), ("1d", 6, 2, 7)]
    # Eligibility reads the combined figure, the one a transition acts on.
    assert [w["sessions"] for w in massive["eligibility"]["windows"]] == [5, 6]


@pytest.mark.parametrize(
    ("status", "graded", "sessions", "expected"),
    [
        (SourceStatus.UNAUDITED, DEMOTION_FLOOR, MIN_ELIGIBLE_SESSIONS, True),
        (SourceStatus.UNAUDITED, DEMOTION_FLOOR - 1, MIN_ELIGIBLE_SESSIONS, False),
        (SourceStatus.UNAUDITED, DEMOTION_FLOOR, MIN_ELIGIBLE_SESSIONS - 1, False),
        (SourceStatus.UNAUDITED, DEMOTION_FLOOR + 50, None, False),
        # U3 B2: the re-promotion permit at its boundary, served by the route
        # -- exactly the label floor over exactly the session floor since the
        # demotion -- and the reject one short of each.
        (SourceStatus.DEMOTED, REPROMOTION_FLOOR, MIN_ELIGIBLE_SESSIONS, True),
        (SourceStatus.DEMOTED, REPROMOTION_FLOOR - 1, MIN_ELIGIBLE_SESSIONS, False),
        (SourceStatus.DEMOTED, REPROMOTION_FLOOR, MIN_ELIGIBLE_SESSIONS - 1, False),
        (SourceStatus.DEMOTED, REPROMOTION_FLOOR + 50, None, False),
    ],
)
def test_settings_never_calls_a_source_eligible_that_the_engine_would_judge_ineligible(
    sentiment_client: TestClient,
    db_engine: Engine,
    status: SourceStatus,
    graded: int,
    sessions: int | None,
    expected: bool,
) -> None:
    """The gap this closes: Settings once called a source eligible that the
    engine would not move.

    ``eligible`` means "meets both floors", which a transition needs but
    which does not make one: an eligible source at 61% stays where it is. So
    the accuracy is pinned at the extreme that moves an eligible source --
    every label wrong for an unaudited one (it demotes), every label right
    for a demoted one (it re-promotes) -- and there the engine moves the
    source exactly when it judges it eligible. The route's ``eligible`` must
    say the same, for the same stored counts; ``expected`` pins which, so
    the two cannot agree by both being wrong.
    """
    if status is SourceStatus.DEMOTED:
        demoted_at = NOW - timedelta(days=14)
        demoting = add_audit_run(db_engine, started_at=demoted_at, finished_at=demoted_at)
        set_source_status(db_engine, "rules", "demoted", at=demoted_at, run_id=demoting)
        bases = (AccuracyBasis.TRAILING_28D, AccuracyBasis.SINCE_DEMOTION)
        correct = graded
        current = CurrentStatus(source="rules", status=status, changed_at=demoted_at)
    else:
        bases = (AccuracyBasis.TRAILING_28D,)
        correct = 0
        current = CurrentStatus(source="rules", status=status, changed_at=None)
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=1),
                        finished_at=NOW - timedelta(days=1))
    # Every basis the runner writes for this status, at the same counts.
    for basis in bases:
        for window in ("1h", "1d"):
            add_accuracy(db_engine, run, source="rules", window=window, basis=basis.value,
                         watch=(graded, correct, 0), sessions=sessions,
                         watch_sessions=sessions,
                         discovery_sessions=None if sessions is None else 0)
    served = _source(sentiment_client.get(SENTIMENT).json(), "rules")["eligibility"]

    rows = [
        AccuracyRow(source="rules", window=window, basis=basis, scope=AccuracyScope.ALL,
                    graded=graded, correct=correct, neutral=0, sessions=sessions)
        for basis in bases
        for window in ("1h", "1d")
    ]
    moved = decide_transition(current, rows, NOW)
    assert served["kind"] == ("repromote" if status is SourceStatus.DEMOTED else "demote")
    assert served["eligible"] is expected
    assert served["eligible"] is (moved is not None)
    if moved is not None:
        assert moved.previous is status


def test_a_demoted_source_one_session_short_since_the_demotion_is_not_eligible(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """Re-promotion has the same session floor as demotion, on ``since_demotion``."""
    demoting = add_audit_run(db_engine, started_at=NOW - timedelta(days=14),
                             finished_at=NOW - timedelta(days=14))
    set_source_status(db_engine, "massive", "demoted", at=NOW - timedelta(days=14),
                      run_id=demoting)
    latest = add_audit_run(db_engine, started_at=NOW - timedelta(days=7),
                           finished_at=NOW - timedelta(days=7))
    for window in ("1h", "1d"):
        add_accuracy(db_engine, latest, source="massive", window=window,
                     watch=(140, 90, 0), sessions=12, watch_sessions=12, discovery_sessions=0)
        add_accuracy(db_engine, latest, source="massive", window=window, basis="since_demotion",
                     watch=(REPROMOTION_FLOOR, 80, 0), sessions=MIN_ELIGIBLE_SESSIONS - 1,
                     watch_sessions=MIN_ELIGIBLE_SESSIONS - 1, discovery_sessions=0)
    eligibility = _source(sentiment_client.get(SENTIMENT).json(), "massive")["eligibility"]
    assert eligibility["kind"] == "repromote"
    assert eligibility["eligible"] is False
    assert [(w["needed"], w["sessionsNeeded"]) for w in eligibility["windows"]] == [
        (0, 1),
        (0, 1),
    ]


def test_a_demoted_source_reports_repromotion_progress_since_the_demotion(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """99 since the demotion needs one more; 100 needs none -- on ``since_demotion``."""
    demoting = add_audit_run(db_engine, started_at=NOW - timedelta(days=14),
                             finished_at=NOW - timedelta(days=14))
    set_source_status(db_engine, "massive", "demoted", at=NOW - timedelta(days=14),
                      run_id=demoting)
    latest = add_audit_run(db_engine, started_at=NOW - timedelta(days=7),
                           finished_at=NOW - timedelta(days=7))
    for window in ("1h", "1d"):
        add_accuracy(db_engine, latest, source="massive", window=window,
                     watch=(140, 70, 2))
    add_accuracy(db_engine, latest, source="massive", window="1h", basis="since_demotion",
                 watch=(60, 40, 0), discovery=(REPROMOTION_FLOOR - 1 - 60, 25, 1),
                 sessions=8, watch_sessions=6, discovery_sessions=5)
    add_accuracy(db_engine, latest, source="massive", window="1d", basis="since_demotion",
                 watch=(REPROMOTION_FLOOR, 70, 0),
                 sessions=9, watch_sessions=9, discovery_sessions=0)

    massive = _source(sentiment_client.get(SENTIMENT).json(), "massive")
    assert massive["status"] == "demoted"
    assert [(a["window"], a["basis"]) for a in massive["accuracy"]] == [
        ("1h", "trailing_28d"),
        ("1h", "since_demotion"),
        ("1d", "trailing_28d"),
        ("1d", "since_demotion"),
    ]
    assert massive["eligibility"] == {
        "kind": "repromote",
        "basis": "since_demotion",
        "floor": REPROMOTION_FLOOR,
        "sessionFloor": MIN_ELIGIBLE_SESSIONS,
        "eligible": False,
        "windows": [
            {"window": "1h", "graded": REPROMOTION_FLOOR - 1, "needed": 1,
             "sessions": 8, "sessionsNeeded": 0},
            {"window": "1d", "graded": REPROMOTION_FLOOR, "needed": 0,
             "sessions": 9, "sessionsNeeded": 0},
        ],
        "asOfRunId": latest,
    }


def test_a_source_demoted_by_the_latest_run_has_counted_nothing_since(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """The demoting run judged it as active, on trailing_28d: zero since, stated as zero."""
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=1),
                        finished_at=NOW - timedelta(days=1))
    set_source_status(db_engine, "rules", "demoted", at=NOW - timedelta(days=1), run_id=run)
    for window in ("1h", "1d"):
        add_accuracy(db_engine, run, source="rules", window=window, watch=(40, 15, 0))

    rules = _source(sentiment_client.get(SENTIMENT).json(), "rules")
    assert rules["eligibility"] == {
        "kind": "repromote",
        "basis": "since_demotion",
        "floor": REPROMOTION_FLOOR,
        "sessionFloor": MIN_ELIGIBLE_SESSIONS,
        "eligible": False,
        "windows": [
            {"window": "1h", "graded": 0, "needed": REPROMOTION_FLOOR,
             "sessions": 0, "sessionsNeeded": MIN_ELIGIBLE_SESSIONS},
            {"window": "1d", "graded": 0, "needed": REPROMOTION_FLOOR,
             "sessions": 0, "sessionsNeeded": MIN_ELIGIBLE_SESSIONS},
        ],
        "asOfRunId": None,
    }
    # The figure that demoted it is still shown, on its own basis.
    assert [a["basis"] for a in rules["accuracy"]] == ["trailing_28d", "trailing_28d"]


# --------------------------------------------------------------------------
# GET -- voided runs (remainder spec R4 step 3, R9)
# --------------------------------------------------------------------------

#: The reason R9's example line quotes, with a character outside ASCII so a
#: re-encoding would show.
_VOID_REASON = "graded under the replaced reference rule — see Q23"


def test_a_voided_run_is_absent_from_last_run_and_accuracy_run(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """Every reader excludes a voided run: ``lastRun`` falls back to the
    newest standing run in any state, ``accuracyRun`` to the newest standing
    succeeded one."""
    standing = add_audit_run(db_engine, started_at=NOW - timedelta(days=14),
                             finished_at=NOW - timedelta(days=14) + timedelta(minutes=3))
    standing_failed = add_audit_run(db_engine, started_at=NOW - timedelta(days=10),
                                    finished_at=NOW - timedelta(days=10), status="failed",
                                    detail='{"stage": "fetch"}')
    voided = add_audit_run(db_engine, started_at=NOW - timedelta(days=7),
                           finished_at=NOW - timedelta(days=7) + timedelta(minutes=3),
                           voided_at=NOW - timedelta(days=1), void_reason=_VOID_REASON)

    body = sentiment_client.get(SENTIMENT).json()
    assert body["lastRun"]["id"] == standing_failed
    assert body["lastRun"]["status"] == "failed"
    assert body["accuracyRun"]["id"] == standing
    assert voided not in {body["lastRun"]["id"], body["accuracyRun"]["id"]}


def test_with_every_run_voided_there_is_no_last_run_and_no_figures(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """Nothing stands: ``null``, ``null``, and no figures -- the voided run's
    rows are still stored (R4 step 3), and none of them is served."""
    voided = add_audit_run(db_engine, started_at=NOW - timedelta(days=7),
                           finished_at=NOW - timedelta(days=7),
                           voided_at=NOW - timedelta(days=1), void_reason=_VOID_REASON)
    for window in ("1h", "1d"):
        add_accuracy(db_engine, voided, source="rules", window=window,
                     watch=(DEMOTION_FLOOR, 0, 0), sessions=MIN_ELIGIBLE_SESSIONS,
                     watch_sessions=MIN_ELIGIBLE_SESSIONS, discovery_sessions=0)

    body = sentiment_client.get(SENTIMENT).json()
    assert body["lastRun"] is None
    assert body["accuracyRun"] is None
    rules = _source(body, "rules")
    assert rules["accuracy"] == []
    assert rules["eligibility"]["eligible"] is False
    assert rules["eligibility"]["asOfRunId"] is None
    assert [w["graded"] for w in rules["eligibility"]["windows"]] == [0, 0]
    assert [r["id"] for r in body["voidedRuns"]] == [voided]


def test_the_figures_and_eligibility_come_from_the_newest_standing_succeeded_run(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """The voided newest run's rows would make the source eligible; the
    standing run before it is one session short. The page shows the standing
    run's rows and calls the source ineligible, as of that run."""
    standing = add_audit_run(db_engine, started_at=NOW - timedelta(days=14),
                             finished_at=NOW - timedelta(days=14))
    voided = add_audit_run(db_engine, started_at=NOW - timedelta(days=7),
                           finished_at=NOW - timedelta(days=7),
                           voided_at=NOW - timedelta(days=1), void_reason=_VOID_REASON)
    for window in ("1h", "1d"):
        add_accuracy(db_engine, standing, source="massive", window=window,
                     watch=(DEMOTION_FLOOR + 4, 21, 2), sessions=MIN_ELIGIBLE_SESSIONS - 1,
                     watch_sessions=MIN_ELIGIBLE_SESSIONS - 1, discovery_sessions=0)
        add_accuracy(db_engine, voided, source="massive", window=window,
                     watch=(DEMOTION_FLOOR + 9, 11, 0), sessions=MIN_ELIGIBLE_SESSIONS + 2,
                     watch_sessions=MIN_ELIGIBLE_SESSIONS + 2, discovery_sessions=0)

    massive = _source(sentiment_client.get(SENTIMENT).json(), "massive")
    assert [
        (a["window"], a["combined"]["graded"], a["combined"]["correct"],
         a["combined"]["sessions"])
        for a in massive["accuracy"]
    ] == [
        ("1h", DEMOTION_FLOOR + 4, 21, MIN_ELIGIBLE_SESSIONS - 1),
        ("1d", DEMOTION_FLOOR + 4, 21, MIN_ELIGIBLE_SESSIONS - 1),
    ]
    assert massive["eligibility"]["eligible"] is False
    assert massive["eligibility"]["asOfRunId"] == standing
    assert [w["sessionsNeeded"] for w in massive["eligibility"]["windows"]] == [1, 1]


def test_voided_runs_are_listed_newest_void_first_with_the_reason_verbatim(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """Ordered by when each was voided, not by run id; a tie on the instant
    falls to the higher id. The reason is the stored text, unaltered."""
    add_audit_run(db_engine, started_at=NOW - timedelta(days=28),
                  finished_at=NOW - timedelta(days=28))
    # Run ids rise with start time; the voids land in a different order.
    second = add_audit_run(db_engine, started_at=NOW - timedelta(days=21),
                           finished_at=NOW - timedelta(days=21),
                           voided_at=NOW - timedelta(days=2), void_reason="second voided")
    third = add_audit_run(db_engine, started_at=NOW - timedelta(days=14),
                          finished_at=NOW - timedelta(days=14), status="failed",
                          voided_at=NOW - timedelta(days=3), void_reason=_VOID_REASON)
    fourth = add_audit_run(db_engine, started_at=NOW - timedelta(days=7),
                           finished_at=NOW - timedelta(days=7),
                           voided_at=NOW - timedelta(days=2), void_reason="tied, higher id")

    body = sentiment_client.get(SENTIMENT).json()
    assert body["voidedRuns"] == [
        {"id": fourth, "voidedAt": "2026-10-12T15:00:00Z", "reason": "tied, higher id"},
        {"id": second, "voidedAt": "2026-10-12T15:00:00Z", "reason": "second voided"},
        {"id": third, "voidedAt": "2026-10-11T15:00:00Z", "reason": _VOID_REASON},
    ]


def test_a_voided_run_with_no_stored_reason_is_left_out_and_logged(
    sentiment_client: TestClient, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """``ck_audit_run_voided`` makes this a damaged database: forced past the
    CHECK here. The run is still excluded -- ``voided_at`` is what voids it --
    but no reason is invented for it, and the gap is logged."""
    standing = add_audit_run(db_engine, started_at=NOW - timedelta(days=14),
                             finished_at=NOW - timedelta(days=14))
    damaged = add_audit_run(db_engine, started_at=NOW - timedelta(days=7),
                            finished_at=NOW - timedelta(days=7))
    with db_engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            update(AuditRun)
            .where(AuditRun.id == damaged)
            .values(voided_at=NOW - timedelta(days=1))
        )
        conn.commit()
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    with Session(db_engine) as session:
        row = session.get(AuditRun, damaged)
        assert row is not None and row.voided_at is not None and row.void_reason is None

    with caplog.at_level(logging.ERROR):
        body = sentiment_client.get(SENTIMENT).json()
    assert body["lastRun"]["id"] == standing
    assert body["voidedRuns"] == []
    (record,) = [
        r for r in caplog.records if r.__dict__.get("event") == "audit_run_void_reason_missing"
    ]
    assert record.__dict__["audit_run_id"] == damaged
    assert record.__dict__["rule"]


def test_a_missing_status_row_says_so_rather_than_reading_as_never_audited(
    sentiment_client: TestClient, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Fix L6: a damaged database is not a clean slate. The display fallback
    stays ``unaudited``, and ``statusMissing`` states that it is a fallback."""
    with Session(db_engine) as session:
        row = session.get(SentimentSourceStatus, "massive")
        assert row is not None
        session.delete(row)
        session.commit()
    with caplog.at_level(logging.ERROR):
        body = sentiment_client.get(SENTIMENT).json()
    massive = _source(body, "massive")
    assert massive["statusMissing"] is True
    assert massive["neverAudited"] is False  # missing is not "never audited"
    assert massive["status"] == "unaudited"
    assert _source(body, "rules")["statusMissing"] is False
    assert [
        r.__dict__["source"]
        for r in caplog.records
        if r.__dict__.get("event") == "sentiment_source_status_missing"
    ] == ["massive"]


def test_next_run_is_the_scheduled_jobs_next_slot_in_utc(
    app: FastAPI, sentiment_client: TestClient
) -> None:
    slot = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)  # Saturday 10:00 EDT
    status = JobStatus(
        name=SENTIMENT_AUDIT_JOB, rule="r", schedule="s", cadence=None,  # type: ignore[arg-type]
        next_run=slot, last_started=None, last_success=None, last_failure=None,
        last_error_type=None, runs=0, failures=0,
    )
    app.dependency_overrides[scheduler_status] = lambda: {SENTIMENT_AUDIT_JOB: status}
    body = sentiment_client.get(SENTIMENT).json()
    assert body["nextRunAt"] == "2026-10-17T14:00:00Z"


def test_manual_available_at_states_when_the_limit_lifts(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    add_audit_run(db_engine, started_at=NOW - timedelta(minutes=30),
                  finished_at=NOW - timedelta(minutes=29), trigger="manual")
    body = sentiment_client.get(SENTIMENT).json()
    assert datetime.fromisoformat(body["manualAvailableAt"]) == NOW + timedelta(minutes=30)


def test_the_wire_carries_no_float(sentiment_client: TestClient, db_engine: Engine) -> None:
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=1),
                        finished_at=NOW - timedelta(days=1))
    add_accuracy(db_engine, run, source="rules", window="1h", watch=(7, 3, 1))
    text = sentiment_client.get(SENTIMENT).text

    def walk(value: object) -> None:
        assert not isinstance(value, float), value
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    import json

    walk(json.loads(text))


# --------------------------------------------------------------------------
# POST -- refusals
# --------------------------------------------------------------------------


def test_no_context_services_is_a_503_and_logged(
    sentiment_client: TestClient, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        response = sentiment_client.post(AUDIT)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "sentiment_audit_unavailable"
    assert _runs(db_engine) == []
    _assert_logged(caplog, 503)


def test_no_market_data_provider_is_a_503(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _arm(app, monkeypatch, None)
    with caplog.at_level(logging.WARNING):
        response = sentiment_client.post(AUDIT)
    assert response.status_code == 503, response.text
    assert "SIP minute bars" in response.json()["error"]["message"]
    assert _runs(db_engine) == []
    _assert_logged(caplog, 503)


def test_a_degraded_watch_universe_is_a_503_with_its_fixed_reason(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reason = "the SPDR seed has never been built; nothing graded"
    _arm(app, monkeypatch, _deps(db_engine, universe_error=WatchUniverseUnavailable(reason)))
    with caplog.at_level(logging.WARNING):
        response = sentiment_client.post(AUDIT)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["message"] == reason
    assert _runs(db_engine) == []  # no row: a refused run costs no hour
    _assert_logged(caplog, 503)


def test_a_manual_run_inside_the_hour_is_a_429_with_retry_after(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_audit_run(db_engine, started_at=NOW - timedelta(minutes=30),
                  finished_at=NOW - timedelta(minutes=29), trigger="manual")
    _arm(app, monkeypatch, _deps(db_engine))
    with caplog.at_level(logging.WARNING):
        response = sentiment_client.post(AUDIT)
    assert response.status_code == 429, response.text
    assert response.headers["Retry-After"] == "1800"
    assert response.json()["error"]["code"] == "sentiment_audit_rate_limited"
    assert len(_runs(db_engine)) == 1
    fields = _assert_logged(caplog, 429)
    assert fields["inputs"]["retry_after_seconds"] == 1800


def test_retry_after_rounds_up_a_fraction_of_a_second(
    app: FastAPI, sentiment_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_audit_run(db_engine,
                  started_at=NOW - MANUAL_AUDIT_INTERVAL + timedelta(seconds=1, milliseconds=500),
                  finished_at=NOW - timedelta(minutes=5), trigger="manual")
    _arm(app, monkeypatch, _deps(db_engine))
    response = sentiment_client.post(AUDIT)
    assert response.status_code == 429, response.text
    # 1.5 s left: waiting 1 s would be refused again, so the header says 2.
    assert response.headers["Retry-After"] == "2"


def test_a_live_running_row_is_a_409(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_audit_run(db_engine, started_at=NOW - timedelta(minutes=10), finished_at=None,
                  status="running")
    _arm(app, monkeypatch, _deps(db_engine))
    with caplog.at_level(logging.WARNING):
        response = sentiment_client.post(AUDIT)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "sentiment_audit_running"
    _assert_logged(caplog, 409)


def test_the_runners_lock_held_is_a_409(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A scheduled run in this process holds the lock before it has a row."""
    _arm(app, monkeypatch, _deps(db_engine))
    assert audit_runner._RUN_LOCK.acquire(blocking=False)
    try:
        with caplog.at_level(logging.WARNING):
            response = sentiment_client.post(AUDIT)
    finally:
        audit_runner._RUN_LOCK.release()
    assert response.status_code == 409, response.text
    assert _runs(db_engine) == []
    _assert_logged(caplog, 409)


def test_a_manual_task_still_in_flight_is_a_409(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Pending:
        def done(self) -> bool:
            return False

    _arm(app, monkeypatch, _deps(db_engine))
    app.state.sentiment_audit_task = _Pending()
    with caplog.at_level(logging.WARNING):
        response = sentiment_client.post(AUDIT)
    app.state.sentiment_audit_task = None
    assert response.status_code == 409, response.text
    _assert_logged(caplog, 409)


# --------------------------------------------------------------------------
# POST -- admitted
# --------------------------------------------------------------------------


def test_a_manual_run_is_202_runs_in_the_background_and_notifies(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered: list[OperatorNotice] = []
    monkeypatch.setattr(
        operator_module, "deliver_notice", lambda runtime, notice: delivered.append(notice)
    )
    _arm(app, monkeypatch, _deps(db_engine))
    response = sentiment_client.post(AUDIT)
    assert response.status_code == 202, response.text
    _wait_for_run(app)
    (run,) = _runs(db_engine)
    assert (run.trigger, run.status) == ("manual", "succeeded")
    # The 202 names the row the run opened: it went out after that row existed.
    assert response.json() == {
        "status": "started",
        "trigger": "manual",
        "runId": run.id,
        "requestedAt": "2026-10-14T15:00:00Z",
    }

    (notice,) = delivered
    assert notice.event == "sentiment_audit_requested"
    assert notice.severity == "info"
    assert "11:00:00 EDT" in notice.body

    # The run is the record GET reads back.
    body = sentiment_client.get(SENTIMENT).json()
    assert body["lastRun"]["id"] == run.id
    assert body["lastRun"]["trigger"] == "manual"
    assert datetime.fromisoformat(body["manualAvailableAt"]) == NOW + MANUAL_AUDIT_INTERVAL


def _notices(monkeypatch: pytest.MonkeyPatch) -> list[OperatorNotice]:
    delivered: list[OperatorNotice] = []
    monkeypatch.setattr(
        operator_module, "deliver_notice", lambda runtime, notice: delivered.append(notice)
    )
    return delivered


def test_a_row_landing_after_the_routes_check_is_a_409_not_a_202(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fix L1: the universe read returned, but another process's ``running``
    row landed before this run's insert, so the runner refused at step a. The
    run never opened a row: no 202, and no "started" notice."""
    delivered = _notices(monkeypatch)
    deps = _deps(db_engine)
    original = deps.watch_universe

    async def universe_then_a_rival_row() -> Collection[str]:
        members = await original()
        add_audit_run(db_engine, started_at=NOW - timedelta(seconds=1), finished_at=None,
                      status="running", trigger="scheduled")
        return members

    _arm(app, monkeypatch, SentimentAuditDeps(
        session_factory=deps.session_factory, bars=deps.bars,
        watch_universe=universe_then_a_rival_row, emit=deps.emit, clock=deps.clock,
    ))
    with caplog.at_level(logging.INFO):
        response = sentiment_client.post(AUDIT)
    assert response.status_code == 409, response.text
    assert delivered == []
    assert [r.trigger for r in _runs(db_engine)] == ["scheduled"]  # the rival's only
    _assert_logged(caplog, 409)
    # Fix L3: already logged as a refusal, so never an ERROR "did not succeed".
    assert not [
        r for r in caplog.records
        if r.__dict__.get("event") == "sentiment_audit_manual_failed"
    ]
    # Nor an ERROR from the runner's step a: a refusal is not a failure.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_run_that_fails_reading_its_universe_is_a_500_and_notifies_nothing(
    app: FastAPI, sentiment_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its row is opened only to be closed ``failed``: it never started."""
    delivered = _notices(monkeypatch)
    _arm(app, monkeypatch, _deps(db_engine, universe_error=RuntimeError("SYNTHETIC")))
    response = sentiment_client.post(AUDIT)
    assert response.status_code == 500, response.text
    assert "SYNTHETIC" not in response.text
    assert delivered == []
    assert [(r.trigger, r.status) for r in _runs(db_engine)] == [("manual", "failed")]


def test_the_runners_lock_refusal_is_not_logged_as_a_failed_run(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _arm(app, monkeypatch, _deps(db_engine))
    assert audit_runner._RUN_LOCK.acquire(blocking=False)
    try:
        with caplog.at_level(logging.INFO):
            assert sentiment_client.post(AUDIT).status_code == 409
    finally:
        audit_runner._RUN_LOCK.release()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_refusal_emits_no_notice(
    app: FastAPI, sentiment_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered: list[OperatorNotice] = []
    monkeypatch.setattr(
        operator_module, "deliver_notice", lambda runtime, notice: delivered.append(notice)
    )
    add_audit_run(db_engine, started_at=NOW - timedelta(minutes=1),
                  finished_at=NOW, trigger="manual")
    _arm(app, monkeypatch, _deps(db_engine))
    assert sentiment_client.post(AUDIT).status_code == 429
    assert delivered == []


def test_exactly_one_hour_after_the_last_manual_run_is_admitted(
    app: FastAPI, sentiment_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_audit_run(db_engine, started_at=NOW - MANUAL_AUDIT_INTERVAL,
                  finished_at=NOW - MANUAL_AUDIT_INTERVAL + timedelta(minutes=2),
                  trigger="manual")
    _arm(app, monkeypatch, _deps(db_engine))
    response = sentiment_client.post(AUDIT)
    assert response.status_code == 202, response.text
    _wait_for_run(app)


def test_a_scheduled_run_does_not_count_against_the_manual_limit(
    app: FastAPI, sentiment_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_audit_run(db_engine, started_at=NOW - timedelta(minutes=10),
                  finished_at=NOW - timedelta(minutes=8), trigger="scheduled")
    _arm(app, monkeypatch, _deps(db_engine))
    response = sentiment_client.post(AUDIT)
    assert response.status_code == 202, response.text
    _wait_for_run(app)


def test_a_voided_manual_run_still_holds_the_hourly_limit(
    app: FastAPI,
    sentiment_client: TestClient,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator's ruling: the limit is on how often a run *starts*,
    not on whether it stands. Voiding a manual run does not hand back its
    hour -- otherwise void-and-rerun would be a way round the limit. The
    run is gone from ``lastRun`` and still behind ``manualAvailableAt``."""
    voided = add_audit_run(db_engine, started_at=NOW - timedelta(minutes=30),
                           finished_at=NOW - timedelta(minutes=29), trigger="manual",
                           voided_at=NOW - timedelta(minutes=10), void_reason=_VOID_REASON)
    body = sentiment_client.get(SENTIMENT).json()
    assert body["lastRun"] is None
    assert [r["id"] for r in body["voidedRuns"]] == [voided]
    assert datetime.fromisoformat(body["manualAvailableAt"]) == NOW + timedelta(minutes=30)

    _arm(app, monkeypatch, _deps(db_engine))
    response = sentiment_client.post(AUDIT)
    assert response.status_code == 429, response.text
    assert response.headers["Retry-After"] == "1800"
    assert [r.id for r in _runs(db_engine)] == [voided]  # no row: nothing started


def test_the_hourly_limit_survives_a_restart(
    registry: ServiceRegistry, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit is read from ``audit_run``, so a fresh process still refuses."""
    first = create_app(registry=registry, db_engine=db_engine, streams=no_socket_supervisor)
    first.dependency_overrides[audit_now] = lambda: NOW
    with TestClient(first) as client:
        _arm(first, monkeypatch, _deps(db_engine))
        assert client.post(AUDIT).status_code == 202
        _wait_for_run(first)

    later = NOW + timedelta(minutes=20)
    second = create_app(registry=registry, db_engine=db_engine, streams=no_socket_supervisor)
    second.dependency_overrides[audit_now] = lambda: later
    with TestClient(second) as client:
        _arm(second, monkeypatch, _deps(db_engine, clock=later))
        response = client.post(AUDIT)
    assert response.status_code == 429, response.text
    assert response.headers["Retry-After"] == str(40 * 60)


# --------------------------------------------------------------------------
# No reinstate route (Q6)
# --------------------------------------------------------------------------


def test_the_only_sentiment_routes_are_the_read_and_the_audit_trigger(app: FastAPI) -> None:
    """The audit is the only writer of the status: no route sets it by hand."""
    # The OpenAPI document is every mounted route, however the routers nest.
    paths: dict[str, dict[str, Any]] = app.openapi()["paths"]
    served = {
        (path, frozenset(methods))
        for path, methods in paths.items()
        if path.startswith(SENTIMENT)
    }
    assert served == {
        (SENTIMENT, frozenset({"get"})),
        (AUDIT, frozenset({"post"})),
    }


def _mounted(routes: Sequence[BaseRoute], prefix: str = "") -> Iterator[tuple[str, str]]:
    """Every ``(path, method)`` the app answers, descending into mounts.

    Read from ``app.routes`` rather than the OpenAPI document, so a route
    declared ``include_in_schema=False`` is still seen. FastAPI 0.141 keeps
    an included router as one nested entry, so the walk goes through
    ``iter_route_contexts`` -- what FastAPI's own OpenAPI builder walks --
    which yields each route with its include prefix applied. A websocket is
    ``WEBSOCKET``: not a write method, but listed rather than skipped. A
    route this cannot read fails the test rather than vanishing from it.
    """
    for context in iter_route_contexts(routes):
        route = context.original_route
        path = prefix + (context.path or "")
        if isinstance(route, Mount):
            yield from _mounted(route.routes, path)
        elif isinstance(route, WebSocketRoute):
            yield path, "WEBSOCKET"
        elif context.methods is not None:
            for method in sorted(context.methods):
                yield path, method
        else:  # pragma: no cover - a route kind this enumeration cannot read
            raise AssertionError(f"unrecognised route {type(route).__name__} at {path!r}")


#: Methods that read. Anything else could change state.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "WEBSOCKET"})
#: A path that names one of the acts R4 confines to the script.
_STATUS_ACT = re.compile(r"void|demot|re-?promot|reinstat|restor", re.IGNORECASE)


def test_no_api_route_could_void_demote_or_repromote(app: FastAPI) -> None:
    """R4: voiding runs through ``scripts/void_audit_run.py``, never a route,
    and the audit run is the only thing that demotes or re-promotes (Q6).

    Under ``/api``, every route on a ``sentiment`` path that does not read
    is exactly the audit trigger -- the one writer starting itself, not a
    hand on the status -- and no route at all is named for a void, a
    demotion, a re-promotion, a reinstatement or a restore.
    """
    routes = set(_mounted(app.routes))
    api = {(path, method) for path, method in routes if path.startswith("/api")}
    assert (SENTIMENT, "GET") in api  # the enumeration sees the router at all

    sentiment_writes = {
        (path, method)
        for path, method in api
        if "sentiment" in path.lower() and method not in _READ_METHODS
    }
    assert sentiment_writes == {(AUDIT, "POST")}
    assert sorted(path for path, _ in api if _STATUS_ACT.search(path)) == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", f"{SENTIMENT}/massive"),
        ("post", f"{SENTIMENT}/massive/reinstate"),
        ("post", f"{SENTIMENT}/reinstate"),
        ("put", SENTIMENT),
        ("patch", SENTIMENT),
        # R4: a void is the script's, never a request's.
        ("post", f"{AUDIT}/1/void"),
        ("post", f"{SENTIMENT}/void"),
        ("delete", AUDIT),
        ("delete", f"{AUDIT}/1"),
    ],
)
def test_a_reinstate_request_changes_nothing(
    sentiment_client: TestClient, db_engine: Engine, method: str, path: str
) -> None:
    set_source_status(db_engine, "massive", "demoted", at=NOW - timedelta(days=3))
    response = sentiment_client.request(method.upper(), path, json={"status": "active"})
    assert response.status_code in (404, 405), response.text
    with Session(db_engine) as session:
        row = session.get(SentimentSourceStatus, "massive")
        assert row is not None and row.status == "demoted"


#: An attribute assignment to a ``SentimentSourceStatus`` column -- only
#: meaningful in a module that names the model.
_STATUS_FIELD_WRITE = re.compile(r"\.(status|changed_at|audit_run_id|reason)\s*=(?!=)")
#: A write through the model itself, anywhere: a bulk ``update``/``insert``/
#: ``delete`` on it, or a row built to be added or merged.
_STATUS_MODEL_WRITE = re.compile(
    r"\b(update|insert|delete)\s*\(\s*(\w+\.)*SentimentSourceStatus\b"
    r"|\bSentimentSourceStatus\s*\("
    r"|\bSentimentSourceStatus\b[^\n]*\)\s*\.\s*(update|delete)\s*\("
    r"|\bSentimentSourceStatus\s*\.\s*__table__\s*\.\s*(update|insert|delete)\b"
)
#: A raw SQL write to the table, which names no model at all.
_STATUS_SQL_WRITE = re.compile(
    r"\b(update|insert\s+(or\s+\w+\s+)?into|replace\s+into|delete\s+from)\s+"
    r"(\w+\.)?[\"'`\[]?sentiment_source_status\b",
    re.IGNORECASE,
)


def _writes_the_source_status(text: str) -> bool:
    return bool(
        ("SentimentSourceStatus" in text and _STATUS_FIELD_WRITE.search(text))
        or _STATUS_MODEL_WRITE.search(text)
        or _STATUS_SQL_WRITE.search(text)
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "row: SentimentSourceStatus\nrow.status = 'active'",
        "row: SentimentSourceStatus\nrow.reason = 'by hand'",
        "row: SentimentSourceStatus\nrow.changed_at = now",
        "session.execute(update(SentimentSourceStatus).values(status='active'))",
        "session.execute(insert( SentimentSourceStatus ).values(source='rules'))",
        "session.add(SentimentSourceStatus(source='rules', status='active'))",
        "session.execute(text(\"UPDATE sentiment_source_status SET status = 'active'\"))",
        "conn.exec_driver_sql('insert or replace into sentiment_source_status VALUES (1)')",
        "conn.execute(text('DELETE FROM \"sentiment_source_status\"'))",
        "session.query(SentimentSourceStatus).update({'status': 'active'})",
        "session.execute(update(models.SentimentSourceStatus).values(status='x'))",
        "conn.execute(SentimentSourceStatus.__table__.update())",
        "conn.execute(text('UPDATE main.sentiment_source_status SET status = 1'))",
    ],
)
def test_the_source_status_write_scan_catches_each_form(snippet: str) -> None:
    """The scan below is only as good as what it can see: prove each form fails it."""
    assert _writes_the_source_status(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "rows = session.scalars(select(SentimentSourceStatus))",
        "row = session.get(SentimentSourceStatus, 'massive')\nif row.status == 'active': pass",
        "text('SELECT status FROM sentiment_source_status')",
    ],
)
def test_the_source_status_write_scan_passes_reads(snippet: str) -> None:
    assert not _writes_the_source_status(snippet)


def test_no_api_module_writes_the_source_status() -> None:
    """Nothing under ``corollary/api`` writes ``sentiment_source_status`` --
    by attribute, through the model, or in raw SQL."""
    package = Path(sentiment_routes.__file__).resolve().parents[1]
    offenders = [
        path.name
        for path in sorted(package.rglob("*.py"))
        if _writes_the_source_status(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], offenders


#: The tables R4 leaves to the audit run and the void script: a run, its
#: graded outcomes, and the archive a void moves them to. The manual
#: ``POST /api/settings/sentiment/audit`` starts ``audit_runner``, which
#: writes its own rows, so no module under ``corollary/api`` writes any of
#: the three -- in any column, by any path. The rule is the table, not the
#: column: enumerating the ways to write ``voided_at`` did not converge.
_GUARDED_MODELS = frozenset({"AuditRun", "LabelOutcome", "LabelOutcomeVoided"})
_GUARDED_TABLES = frozenset({"audit_run", "label_outcome", "label_outcome_voided"})
#: The statement builders, called on the model or table they write.
_STATEMENT_CALLS = frozenset({"update", "insert", "delete"})
#: Methods that write when called on a chain over a guarded model, table or
#: row: ``query(AuditRun)...update(...)``, ``AuditRun.__table__.delete()``,
#: ``insert(AuditRun).on_conflict_do_update(...)``, ``run.__dict__.update``.
_CHAIN_WRITES = frozenset(
    {
        "update",
        "delete",
        "insert",
        "values",
        "ordered_values",
        "from_select",
        "on_conflict_do_update",
        "on_conflict_do_nothing",
    }
)
#: The session's unit of work, and its bulk forms.
_SESSION_WRITES = frozenset(
    {
        "add",
        "add_all",
        "merge",
        "delete",
        "bulk_save_objects",
        "bulk_insert_mappings",
        "bulk_update_mappings",
    }
)
#: Calls whose first argument names the table they hand back.
_TABLE_BUILDERS = frozenset({"aliased", "table", "Table"})
#: ``audit_run``'s void columns. Not the rule -- the rule is the table --
#: but a second net for a write whose table the scan cannot see: a bare
#: ``run.voided_at = now``, ``update(t)`` with ``t`` built elsewhere.
_VOID_COLUMNS = frozenset({"voided_at", "void_reason"})
#: A dotted name holding either part names the void's module or its script.
_VOID_MODULE_PARTS = frozenset({"audit_void", "void_audit_run"})
#: Names the API has no reason to say at all, as a name or an attribute:
#: the void's module, its entry point, and the archive's model.
_VOID_NAMES = _VOID_MODULE_PARTS | {"LabelOutcomeVoided"}
#: The void's module or script named in a string -- ``scripts/void_audit_run.py``,
#: ``scripts.void_audit_run``, ``corollary.data.news.audit_void`` -- which is
#: how ``subprocess``, ``runpy`` and ``importlib`` reach it. ``\b`` counts
#: ``_`` as part of a word, so the notice's event name
#: ``sentiment_audit_voided`` is not one.
_VOID_MODULE_TEXT = re.compile(r"\b(?:audit_void|void_audit_run)\b")
_VOID_ARCHIVE = re.compile(r"label_outcome_voided|LabelOutcomeVoided")
#: A logging call. The keys of its ``extra={...}`` name log fields, not
#: columns: ``routes/sentiment.py`` logs a damaged row's ``"voided_at"``.
_LOG_CALLS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
#: Stands in, in a folded string, for a part the scan cannot evaluate: an
#: f-string field, or a name concatenated in.
_OPAQUE = "\x00"
#: An optional schema qualifier and an opening quote: ``main.``, ``"main".``, ``[``.
_SQL_QUALIFIER = r"(?:[\"'`\[]?\w+[\"'`\]]?\s*\.\s*)?[\"'`\[]?"
#: What follows ``UPDATE <table>``: a closing quote, an alias, then ``SET``.
#: ``SET`` is required so prose (``f"cannot update {name}"``) is not SQL.
_SQL_AFTER_UPDATE = r"[\"'`\]]?(?:\s+(?:as\s+)?\w+)?\s+set\b"


def _sql_write(table: str) -> re.Pattern[str]:
    """SQL that writes ``table``: ``UPDATE ... SET``, ``INSERT [OR x] INTO``,
    ``REPLACE INTO``, ``DELETE FROM``. Row-value ``SET (a, b) = (...)`` is
    the same ``UPDATE``, so no column is read at all."""
    return re.compile(
        r"\bupdate(?:\s+or\s+\w+)?\s+" + _SQL_QUALIFIER + table + _SQL_AFTER_UPDATE
        + r"|\b(?:insert(?:\s+or\s+\w+)?|replace)\s+into\s+" + _SQL_QUALIFIER + table
        + r"|\bdelete\s+from\s+" + _SQL_QUALIFIER + table,
        re.IGNORECASE,
    )


#: A raw SQL write of a guarded table, in any column.
_SQL_GUARDED_WRITE = _sql_write(r"(?:label_outcome_voided|label_outcome|audit_run)\b")
#: A raw SQL write whose table the scan cannot read -- an opaque fold, a
#: ``%s`` field, a ``str.format`` field. ``corollary/api`` holds no raw SQL
#: at all, so it could be a guarded table and is refused as one.
_SQL_OPAQUE_WRITE = _sql_write(r"(?:\x00|%\(?\w*\)?s|\{[^{}]*\})")


@dataclass
class _Guarded:
    """What one module calls the guarded tables. Names are bound module-wide,
    not per scope -- a ``run`` that holds a row in one function is taken to
    hold one in every function -- which over-reports rather than under."""

    #: A guarded model class: its own name, or an alias of it.
    models: set[str] = field(default_factory=lambda: set(_GUARDED_MODELS))
    #: A guarded model, table or column: ``t = AuditRun.__table__``,
    #: ``col = AuditRun.voided_at``, ``Run = aliased(AuditRun)``.
    handles: set[str] = field(default_factory=lambda: set(_GUARDED_MODELS))
    #: May hold a guarded row, or a statement or query over one.
    rows: set[str] = field(default_factory=set)
    #: Local functions annotated to return a guarded row (``_latest_run``).
    returns_row: set[str] = field(default_factory=set)
    #: ``update`` / ``insert`` / ``delete``, and any name imported as one.
    statements: set[str] = field(default_factory=lambda: set(_STATEMENT_CALLS))

    def size(self) -> tuple[int, int, int, int]:
        return (len(self.models), len(self.handles), len(self.rows), len(self.returns_row))


def _names_the_void(dotted: str) -> bool:
    return bool(_VOID_MODULE_PARTS & set(dotted.split(".")))


def _docstring_ids(tree: ast.AST) -> set[int]:
    """The docstring constants, which may mention anything."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def _log_field_ids(tree: ast.AST) -> set[int]:
    """The keys of every logging call's literal ``extra={...}``. Only a dict
    written in the call counts: one held in a variable is scanned like any
    other."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in _LOG_CALLS:
            for keyword in node.keywords:
                if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                    found.update(id(key) for key in keyword.value.keys if key is not None)
    return found


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _void_attribute(node: ast.AST) -> str | None:
    """The void column ``node`` names by attribute, anywhere inside it:
    ``AuditRun.voided_at``, ``t.c.void_reason``, ``AuditRun.voided_at.key``."""
    for part in ast.walk(node):
        if isinstance(part, ast.Attribute) and part.attr in _VOID_COLUMNS:
            return part.attr
    return None


def _is_model(node: ast.AST, g: _Guarded) -> bool:
    """A guarded model class: by name, by alias, or as ``models.AuditRun``."""
    return (isinstance(node, ast.Name) and node.id in g.models) or (
        isinstance(node, ast.Attribute) and node.attr in _GUARDED_MODELS
    )


def _is_guarded_table_name(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in _GUARDED_TABLES
    )


def _is_handle(node: ast.AST, g: _Guarded) -> bool:
    """A guarded model, table or column, as an expression: ``AuditRun``,
    ``AuditRun.__table__``, ``t.c.voided_at``, ``aliased(AuditRun)``,
    ``table('audit_run', ...)``, ``metadata.tables['audit_run']``."""
    if _is_model(node, g) or _is_guarded_table_name(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in g.handles
    if isinstance(node, ast.Attribute):
        return _is_handle(node.value, g)
    if isinstance(node, ast.Subscript):
        return _is_handle(node.slice, g) or _is_handle(node.value, g)
    if isinstance(node, ast.Call):
        return (
            _call_name(node) in _TABLE_BUILDERS
            and bool(node.args)
            and _is_handle(node.args[0], g)
        )
    return False


def _refs(node: ast.AST, g: _Guarded, *, row_attributes: bool) -> bool:
    """Whether ``node`` reaches a guarded model, table or row anywhere inside
    it. Without ``row_attributes``, an attribute read off a row (``run.id``,
    ``run.started_at.isoformat()``) is a value, not the row: ``seen.add(run.id)``
    adds an integer to a set."""
    if (
        not row_attributes
        and isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in g.rows
        and node.value.id not in g.handles
    ):
        return False
    if isinstance(node, ast.Name) and (node.id in g.handles or node.id in g.rows):
        return True
    if _is_model(node, g) or _is_guarded_table_name(node):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in g.returns_row:
            return True
    return any(
        _refs(child, g, row_attributes=row_attributes) for child in ast.iter_child_nodes(node)
    )


def _annotation_refs(node: ast.AST | None, g: _Guarded) -> bool:
    """Whether an annotation names a guarded model, including as a string
    (``-> "AuditRun | None"``)."""
    if node is None:
        return False
    if _refs(node, g, row_attributes=True):
        return True
    for part in ast.walk(node):
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            try:
                parsed = ast.parse(part.value, mode="eval")
            except SyntaxError:
                continue
            if _refs(parsed, g, row_attributes=True):
                return True
    return False


def _stored_names(target: ast.AST) -> list[str]:
    return [
        part.id
        for part in ast.walk(target)
        if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store)
    ]


def _bind_value(targets: Sequence[ast.AST], value: ast.AST, g: _Guarded) -> None:
    names = [name for target in targets for name in _stored_names(target)]
    if _is_model(value, g):
        g.models.update(names)
        g.handles.update(names)
    elif _is_handle(value, g):
        g.handles.update(names)
    elif _refs(value, g, row_attributes=False):
        g.rows.update(names)


def _bind(node: ast.AST, g: _Guarded) -> None:
    """Record what ``node`` binds: an alias, a handle, a row, a row-returning
    function."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if _annotation_refs(node.returns, g):
            g.returns_row.add(node.name)
        a = node.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
            if arg is not None and _annotation_refs(arg.annotation, g):
                g.rows.add(arg.arg)
    elif isinstance(node, ast.Assign):
        _bind_value(node.targets, node.value, g)
    elif isinstance(node, ast.AnnAssign):
        if _annotation_refs(node.annotation, g):
            g.rows.update(_stored_names(node.target))
        if node.value is not None:
            _bind_value([node.target], node.value, g)
    elif isinstance(node, ast.NamedExpr):
        _bind_value([node.target], node.value, g)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        if _refs(node.iter, g, row_attributes=False):
            g.rows.update(_stored_names(node.target))


def _guarded_names(tree: ast.AST) -> _Guarded:
    """Every name in the module that stands for a guarded model, table or
    row, to a fixpoint: a function defined below its caller still counts."""
    g = _Guarded()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name in _GUARDED_MODELS:
                    g.models.add(bound)
                    g.handles.add(bound)
                if alias.name in _STATEMENT_CALLS:
                    g.statements.add(bound)
    while True:
        before = g.size()
        for node in ast.walk(tree):
            _bind(node, g)
        if g.size() == before:
            return g


def _root(node: ast.AST) -> str | None:
    """The name an attribute, subscript or call chain hangs from."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _folded(node: ast.AST) -> str | None:
    """The text of a string expression, each part the scan cannot evaluate
    replaced by :data:`_OPAQUE`: a literal, a ``+`` chain, the template of a
    ``%`` format, an f-string. ``None`` for anything that is not one."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            inner = _folded(value.value if isinstance(value, ast.FormattedValue) else value)
            parts.append(_OPAQUE if inner is None else inner)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        if left is None and right is None:
            return None
        return (_OPAQUE if left is None else left) + (_OPAQUE if right is None else right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _folded(node.left)
    return None


def _setattr_parts(node: ast.Call) -> tuple[ast.AST | None, ast.AST | None]:
    """The object and the name a ``setattr`` sets: ``setattr(obj, name, v)``,
    ``object.__setattr__(obj, name, v)``, ``obj.__setattr__(name, v)``."""
    args = node.args
    if isinstance(node.func, ast.Name) or len(args) >= 3:
        return (args[0] if args else None), (args[1] if len(args) > 1 else None)
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    return receiver, (args[0] if args else None)


def _call_violations(node: ast.Call, g: _Guarded) -> list[str]:
    """The writes a call makes to a guarded table, in any column."""
    name = _call_name(node)
    found: list[str] = []
    if name in g.statements and node.args and _is_handle(node.args[0], g):
        found.append(f"builds {name}() on {ast.unparse(node.args[0])}")
    if _is_model(node.func, g):
        found.append(f"constructs {ast.unparse(node.func)}")
    if isinstance(node.func, ast.Attribute):
        receiver = node.func.value
        # ``.values()`` with nothing to set is a dict's, never a write.
        if (
            name in _CHAIN_WRITES
            and (name != "values" or node.args or node.keywords)
            and _refs(receiver, g, row_attributes=True)
        ):
            found.append(f"calls .{name}() on a chain over a guarded table")
        if name in _SESSION_WRITES and any(
            _refs(arg, g, row_attributes=False) for arg in node.args
        ):
            found.append(f"passes a guarded row or table to .{name}()")
    if name in {"setattr", "__setattr__"}:
        target, attribute = _setattr_parts(node)
        if not (isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)):
            # The FastAPI patch loop, ``setattr(run, k, v)``: any column at all.
            found.append(f"calls {name}() with a name the scan cannot read")
        elif target is not None and _refs(target, g, row_attributes=False):
            found.append(f"sets {attribute.value} on a guarded row by {name}()")
    return found


def _key_violations(node: ast.Dict, g: _Guarded, log_fields: set[int]) -> list[str]:
    """A mapping keyed by a guarded column (``{AuditRun.status: x}``), which
    is what Core's ``values`` and ``set_`` take."""
    found: list[str] = []
    for key in node.keys:
        if key is None or id(key) in log_fields:
            continue
        if _is_handle(key, g):
            found.append(f"keys a mapping by {ast.unparse(key)}")
        elif (column := _void_attribute(key)) is not None:
            found.append(f"writes .{column} as a dict key")
    return found


def _assignment_violations(
    node: ast.Assign | ast.AugAssign | ast.AnnAssign, g: _Guarded
) -> list[str]:
    """An attribute set on a guarded row or model, in any column; or, where
    the row cannot be seen, a void column set on anything."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    found: list[str] = []
    for target in targets:
        for part in ast.walk(target):
            if isinstance(part, ast.Attribute) and isinstance(part.ctx, ast.Store):
                root = _root(part.value)
                if root is not None and (root in g.rows or root in g.handles):
                    found.append(f"assigns {ast.unparse(part)}")
                elif part.attr in _VOID_COLUMNS:
                    found.append(f"assigns .{part.attr}")
    return found


def _string_violations(node: ast.AST, *, log_field: bool) -> list[str]:
    """What a string expression says, once folded."""
    text = _folded(node)
    if text is None:
        return []
    found: list[str] = []
    if text in _VOID_COLUMNS and not log_field:
        found.append(f"names {text} in a string")
    if _VOID_MODULE_TEXT.search(text):
        found.append("names the void's module or script in a string")
    if _VOID_ARCHIVE.search(text):
        found.append("names the archive table in a string")
    if _SQL_GUARDED_WRITE.search(text):
        found.append("writes a guarded table in SQL")
    if _SQL_OPAQUE_WRITE.search(text):
        found.append("writes in SQL to a table the scan cannot read")
    return found


def _void_violations(source: str) -> list[str]:
    """What ``source`` does that only the audit run and the void script may
    (R4, "never a route"): write ``audit_run``, ``label_outcome`` or
    ``label_outcome_voided`` **in any form, whatever the column**; import or
    otherwise reach the void's module or script; name the archive.

    **A tripwire, not a proof.** It is built to catch the forms a
    well-meaning change would reach for -- the ORM, Core, a dialect upsert,
    raw SQL, a patch loop, a subprocess -- and a determined one can still get
    past it: a name assembled where nothing folds it, a write delegated to a
    helper outside ``corollary/api``. The guarantee is R4 plus review. What
    this adds is that the honest mistake fails a test instead of shipping.

    A write is found by its target, never by its column: a statement built
    on a guarded model or table (``update(AuditRun)``, ``delete(t)``), a
    write method on a chain over one (``query(...).update``,
    ``.on_conflict_do_update``), a guarded row handed to the session or
    constructed, an attribute set on a guarded row, a mapping keyed by a
    guarded column, raw SQL naming a guarded table. Model aliases, tables,
    columns and rows are tracked through imports, simple assignments,
    annotations, ``for`` targets and functions annotated to return a row.

    Also refused, because nothing in ``corollary/api`` needs them: a
    ``setattr`` whose name is not a literal, raw SQL writing a table the scan
    cannot read, and any non-docstring string naming the void's module or
    script. Kept as a second net where the table is out of sight: a void
    column set by attribute, used as a dict key, or named in a string.

    Reads pass: ``select(...)``, ``session.get``, the ``voided_at IS NULL``
    filter, building ``VoidedAuditRun(voided_at=...)``. Docstrings may name
    anything, and a logging call's literal ``extra`` keys name log fields."""
    tree = ast.parse(source)
    g = _guarded_names(tree)
    docstrings = _docstring_ids(tree)
    log_fields = _log_field_ids(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"imports {a.name}" for a in node.names if _names_the_void(a.name)]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            for alias in node.names:
                if _names_the_void(f"{base}.{alias.name}") or alias.name == "LabelOutcomeVoided":
                    found.append(f"imports {base}.{alias.name}")
        elif isinstance(node, ast.Call):
            found += _call_violations(node, g)
        elif isinstance(node, ast.Dict):
            found += _key_violations(node, g, log_fields)
        elif isinstance(node, (ast.Assign, ast.AugAssign)) or (
            isinstance(node, ast.AnnAssign) and node.value is not None
        ):
            found += _assignment_violations(node, g)
        elif isinstance(node, ast.Name) and node.id in _VOID_NAMES:
            found.append(f"names {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _VOID_NAMES:
            found.append(f"names {node.attr}")
        is_string = isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp))
        if is_string and id(node) not in docstrings:
            found += _string_violations(node, log_field=id(node) in log_fields)
    # A string folded whole and piece by piece can say one thing twice.
    return list(dict.fromkeys(found))


#: The forms the scan must refuse. Each one fails the test if it passes.
_MUST_CATCH = [
    "from corollary.data.news.audit_void import void_audit_run",
    "from corollary.data.news import audit_void",
    "from corollary.data.news import (\n    audit_runner,\n    audit_void,\n)",
    "import corollary.data.news.audit_void as voiding",
    "from . import audit_void",
    "importlib.import_module('corollary.data.news.audit_void')",
    "from scripts.void_audit_run import main",
    "from corollary.db.models import LabelOutcomeVoided",
    "rows = session.scalars(select(models.LabelOutcomeVoided))",
    "conn.execute(text('INSERT INTO label_outcome_voided SELECT * FROM label_outcome'))",
    "run.voided_at = now",
    "run.voided_at, run.void_reason = now, body.reason",
    "session.execute(update(AuditRun).values(voided_at=now, void_reason=reason))",
    "session.execute(update(AuditRun).values({'void_reason': reason}))",
    "session.query(AuditRun).filter_by(id=n).update({'voided_at': now})",
    "session.add(AuditRun(started_at=now, voided_at=now))",
    "setattr(run, 'void_reason', reason)",
    "conn.execute(text('UPDATE audit_run SET voided_at = :at WHERE id = :id'))",
    "conn.exec_driver_sql('update audit_run set \"void_reason\" = ?')",
    "conn.exec_driver_sql('INSERT INTO audit_run (started_at, voided_at) VALUES (?, ?)')",
    # The case the route test cannot see: a void folded into the body of
    # the existing ``POST /api/settings/sentiment/audit``.
    "def audit_now(body):\n"
    "    if body.void_run_id is not None:\n"
    "        return void_audit_run(sessions, run_id=body.void_run_id, reason=body.reason)\n",
    # E1: every form below passed the scan before it was widened.
    # Core's column-keyed dict, by the model and by the table.
    "session.execute(update(AuditRun).values("
    "{AuditRun.voided_at: now, AuditRun.void_reason: body.reason}))",
    "session.execute(update(t).values({t.c.voided_at: now}))",
    "session.execute(update(AuditRun).values({AuditRun.voided_at.key: now}))",
    # ``**``, whose keyword has no name of its own.
    "session.execute(update(AuditRun).values(**{'voided_at': now}))",
    "session.add(AuditRun(**{'voided_at': now, 'void_reason': reason}))",
    "session.add(AuditRun(**dict(void_reason=reason)))",
    # A list of parameter dicts: executemany, and the bulk mappings.
    "session.execute(update(AuditRun), [{'id': n, 'voided_at': now}])",
    "session.bulk_update_mappings(AuditRun, [{'id': n, 'void_reason': reason}])",
    # The other ways to spell a mapping, and one held in a variable.
    "session.execute(update(AuditRun).values(dict(voided_at=now)))",
    "session.execute(update(AuditRun).ordered_values((AuditRun.voided_at, now)))",
    "session.execute(insert(AuditRun).from_select([AuditRun.void_reason], rows))",
    "session.execute(insert(AuditRun).values(id=n).on_conflict_do_update("
    "index_elements=['id'], set_={'voided_at': now}))",
    "session.execute(insert(AuditRun).values(id=n).on_conflict_do_update("
    "index_elements=['id'], set_={AuditRun.void_reason: reason}))",
    "changes = {'void_reason': reason}\nsession.execute(update(AuditRun).values(changes))",
    # A mapping whose keys were built somewhere the scan cannot see.
    "session.execute(update(AuditRun).values(**body.model_dump()))",
    "session.execute(update(AuditRun).where(AuditRun.id == n).values(body.model_dump()))",
    "session.add(AuditRun(**body.model_dump()))",
    "row['voided_at'] = now",
    "column = 'voided' + '_at'\nsetattr(run, column, now)",
    # Raw SQL assembled rather than written out.
    "conn.execute(text('UPDATE audit_run SET ' + 'voided_at = :at'))",
    "column = 'voided_at'\nconn.execute(text(f'UPDATE audit_run SET {column} = :at'))",
    "conn.execute(text(f'UPDATE audit_run SET {body.column} = :at WHERE id = :id'))",
    "conn.execute(text('UPDATE audit_run SET ' + body.column + ' = :at'))",
    "conn.execute(text('UPDATE audit_run SET %s = :at' % body.column))",
    "conn.execute(text('UPDATE audit_run SET {} = :at'.format(body.column)))",
    "conn.exec_driver_sql('REPLACE INTO audit_run (id, started_at, voided_at) VALUES (?, ?, ?)')",
    "conn.exec_driver_sql(f'INSERT INTO audit_run (id, {body.column}) VALUES (?, ?)')",
    # The dunder spellings of setattr.
    "run.__setattr__('voided_at', now)",
    "object.__setattr__(run, 'void_reason', reason)",
    # The void's module reached by attribute rather than imported by name.
    "import corollary.data.news\n"
    "corollary.data.news.audit_void._write_void(session, n, (), now, reason)",
    "news = importlib.import_module('corollary.data.news')\nnews.audit_void.void_audit_run",
    # U4d: forms the column enumeration still let through. The rule is now
    # the table, so none of these depends on which column it sets.
    "session.query(AuditRun).filter_by(id=n).update(body.model_dump())",
    "session.execute(update(AuditRun).where(AuditRun.id == n), body.model_dump())",
    "session.execute(update(AuditRun), [body.model_dump()])",
    "session.bulk_update_mappings(AuditRun, [body.model_dump()])",
    "session.execute(insert(AuditRun).values(id=n).on_conflict_do_update("
    "index_elements=['id'], set_=body.model_dump()))",
    "from sqlalchemy.dialects.sqlite import insert as upsert\n"
    "stmt = upsert(AuditRun).on_conflict_do_update(index_elements=['id'], set_=body.model_dump())",
    "stmt = update(AuditRun).where(AuditRun.id == n)\n"
    "session.execute(stmt.values(**body.model_dump()))",
    "for k, v in body.model_dump().items():\n    setattr(run, k, v)",
    "col = AuditRun.voided_at\nsession.execute(update(AuditRun).values({col: now}))",
    "col = AuditRun.voided_at\nsession.execute(update(t).values({col: now}))",
    "subprocess.run([sys.executable, 'scripts/void_audit_run.py', '--run', str(n)])",
    "runpy.run_module('scripts.void_audit_run')",
    "runpy.run_path(str(ROOT / 'scripts' / 'void_audit_run.py'))",
    "importlib.import_module('corollary.data.news.' + 'audit_void')",
    "from corollary.db.models import AuditRun as Run\n"
    "session.execute(update(Run).values(status='failed'))",
    "from corollary.db.models import AuditRun as Run\nsession.add(Run(started_at=now))",
    "Run = models.AuditRun\nsession.add(Run(started_at=now))",
    "conn.execute(text("
    "'UPDATE audit_run SET (voided_at, void_reason) = (:at, :why) WHERE id = :id'))",
    "object.__setattr__(run, name, value)",
    # Column-agnostic: every write below sets a column the void does not
    # own, or none -- the table alone is what fails it.
    "session.execute(update(AuditRun).values(status='failed'))",
    "session.execute(delete(AuditRun).where(AuditRun.id == n))",
    "session.execute(sa.insert(AuditRun.__table__).values(id=n))",
    "conn.execute(AuditRun.__table__.update().values(status='failed'))",
    "t = AuditRun.__table__\nconn.execute(update(t).values(status='failed'))",
    "conn.execute(metadata.tables['audit_run'].delete())",
    "conn.execute(insert(table('label_outcome', column('id'))))",
    "session.query(LabelOutcome).filter_by(audit_run_id=n).delete()",
    "session.add(LabelOutcome(audit_run_id=n, label_id=m))",
    "session.merge(AuditRun(id=n, status='failed'))",
    "session.add_all([AuditRun(started_at=now)])",
    "run = session.get(AuditRun, n)\nsession.delete(run)",
    "session.delete(session.get(AuditRun, n))",
    "run: AuditRun\nrun.status = 'failed'",
    "def close(run: AuditRun | None) -> None:\n    run.finished_at = now",
    "def _latest() -> 'AuditRun | None': ...\nrun = _latest()\nrun.status = 'failed'",
    "for run in session.scalars(select(AuditRun)):\n    run.status = 'failed'",
    "run = session.get(AuditRun, n)\nsetattr(run, 'status', 'failed')",
    "run = session.get(AuditRun, n)\nrun.__dict__.update(body.model_dump())",
    "session.execute(update(LabelOutcome).values({LabelOutcome.audit_run_id: n}))",
    "conn.execute(text('DELETE FROM label_outcome WHERE audit_run_id = :n'))",
    "conn.execute(text('UPDATE audit_run SET status = :s'))",
    "conn.exec_driver_sql('INSERT OR REPLACE INTO \"main\".\"label_outcome\" VALUES (?)')",
    "conn.execute(text(f'UPDATE {table} SET status = :s'))",
    "conn.execute(text('DELETE FROM %s' % table))",
]


#: Reads, and the near misses that must stay reads.
_MUST_PASS = [
    "stmt = select(AuditRun).where(AuditRun.voided_at.is_(None))",
    "stmt = select(AuditRun).order_by(AuditRun.voided_at.desc(), AuditRun.id.desc())",
    "voided_at, reason = run.voided_at, run.void_reason",
    "out.append(VoidedAuditRun(id=run.id, voided_at=voided_at, reason=reason))",
    "class VoidedAuditRun(BaseModel):\n    voided_at: datetime\n    reason: str",
    'def f():\n    """A void moves outcomes to label_outcome_voided; see void_audit_run."""',
    "EVENTS = ('sentiment_audit_voided',)",
    "text('SELECT voided_at, void_reason FROM audit_run WHERE voided_at IS NOT NULL')",
    # A log line's field, exactly as ``routes/sentiment.py`` writes one:
    # ``extra``'s keys name log fields, not columns.
    "logger.error('no reason', extra={'event': 'x', 'voided_at': voided_at.isoformat()})",
    "text(f'SELECT voided_at FROM audit_run WHERE id = {n}')",
    # U4d: reads of every guarded table, and values read off a row.
    "run = session.get(AuditRun, n)",
    "rows = session.scalars(select(LabelOutcome).where(LabelOutcome.audit_run_id == n)).all()",
    "text('SELECT * FROM label_outcome WHERE audit_run_id = :n')",
    "run = session.get(AuditRun, n)\nlogger.info('x', extra={'audit_run_id': run.id})",
    "seen = set()\nfor run in session.scalars(select(AuditRun)):\n    seen.add(run.id)",
    "def _summary(run: AuditRun) -> AuditRunSummary:\n"
    "    return AuditRunSummary(id=run.id, status=run.status)",
    "run = session.get(AuditRun, n)\npayload = {'id': run.id}\npayload.update(extra)",
    "rows = session.scalars(select(AuditRun)).all()\n"
    "by_id = {run.id: run for run in rows}\nfor run in by_id.values():\n    pass",
    "setattr(request.app.state, 'sentiment_audit_task', task)",
    "message = f'cannot update {name} now'",
]


@pytest.mark.parametrize("snippet", _MUST_CATCH)
def test_the_void_scan_catches_each_form(snippet: str) -> None:
    """The scan below is only as good as what it can see: prove each form fails it."""
    assert _void_violations(snippet), snippet


@pytest.mark.parametrize("snippet", _MUST_PASS)
def test_the_void_scan_passes_reads(snippet: str) -> None:
    assert _void_violations(snippet) == []


def test_the_void_scan_fails_a_planted_violation_in_the_real_route_module() -> None:
    """The real module passes as it stands, and the same text with a void
    planted in it, in memory, does not."""
    source = Path(sentiment_routes.__file__).read_text(encoding="utf-8")
    assert _void_violations(source) == []
    planted = source + (
        "\n\nfrom corollary.data.news.audit_void import void_audit_run as _void\n"
        "\n\ndef _planted(run: AuditRun, now: datetime) -> None:\n"
        "    run.voided_at = now\n"
    )
    assert _void_violations(planted) == [
        "imports corollary.data.news.audit_void.void_audit_run",
        "assigns run.voided_at",
    ]


def test_the_void_scan_fails_a_write_in_any_column_through_the_modules_own_reads() -> None:
    """Column-agnostic, on the real module: a row from its own ``_latest_run``
    (annotated ``-> AuditRun | None``) has a non-void column set and is
    deleted, and both are found."""
    source = Path(sentiment_routes.__file__).read_text(encoding="utf-8")
    planted = source + (
        "\n\ndef _planted(session: Session) -> None:\n"
        "    last = _latest_run(session, succeeded_only=False)\n"
        "    if last is not None:\n"
        "        last.status = 'failed'\n"
        "        session.delete(last)\n"
    )
    assert _void_violations(planted) == [
        "assigns last.status",
        "passes a guarded row or table to .delete()",
    ]


def test_no_api_module_voids_a_run() -> None:
    """R4: a void is ``scripts/void_audit_run.py`` and never a route, and a
    run's rows are the runner's. No module under ``corollary/api`` writes
    ``audit_run``, ``label_outcome`` or ``label_outcome_voided`` in any
    column, reaches the void's module or script, or names the archive table.

    **This scan is a tripwire, not a proof.** It catches the forms a
    well-meaning change would use; a determined one can get past it. The
    guarantee is R4 plus review. It complements the route test, which
    checks paths and names and so cannot see a write folded into an
    existing route's body."""
    package = Path(sentiment_routes.__file__).resolve().parents[1]
    offenders = {
        str(path.relative_to(package)): found
        for path in sorted(package.rglob("*.py"))
        if (found := _void_violations(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, offenders


def test_the_module_imports_no_event_loop_blocking_sleep() -> None:
    """The run is a task on the loop; the route never waits for it to finish."""
    source = inspect.getsource(sentiment_routes)
    assert "time.sleep" not in source
    assert "run_until_complete" not in source
    assert inspect.iscoroutinefunction(sentiment_routes.start_sentiment_audit)
