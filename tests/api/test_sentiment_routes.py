"""``GET /api/settings/sentiment`` and ``POST /api/settings/sentiment/audit``.

Step 6's done-criterion: *"Settings shows each source's status, graded counts
per window, its watch/discovery split, and how many more labels each needs to
become eligible."* And the design's *API*: the manual trigger is limited to
once an hour, and there is **no reinstate route** -- the audit is the only
writer of the status (Q6).

The ``POST`` runs the real runner (``run_sentiment_audit``) over a database
with no labels, so a run grades nothing and succeeds in milliseconds; only
the scheduler's ``sentiment_audit_deps`` is replaced, because building real
``ContextServices`` would construct vendor clients.
"""

import inspect
import logging
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api import operator as operator_module
from corollary.api.app import create_app, no_socket_supervisor
from corollary.api.deps import ServiceRegistry, scheduler_status
from corollary.api.routes import sentiment as sentiment_routes
from corollary.api.routes.sentiment import MANUAL_AUDIT_INTERVAL, SENTIMENT_AUDIT_JOB, audit_now
from corollary.data.news import audit_runner
from corollary.data.news.audit import DEMOTION_FLOOR, REPROMOTION_FLOOR
from corollary.data.news.audit_runner import SentimentAuditDeps, WatchUniverseUnavailable
from corollary.data.providers.interface import Bar
from corollary.db.models import AuditRun, SentimentSourceStatus
from corollary.engine.runtime import OperatorNotice
from corollary.engine.scheduler import JobStatus

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
    """Give the app context services, and the audit the deps the test chose."""
    app.state.context_services = object()
    monkeypatch.setattr(sentiment_routes, "sentiment_audit_deps", lambda services: deps)


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
        assert source["reason"] is None
        assert source["accuracy"] == []
        assert source["eligibility"] == {
            "kind": "demote",
            "basis": "trailing_28d",
            "floor": DEMOTION_FLOOR,
            "eligible": False,
            "windows": [
                {"window": "1h", "graded": 0, "needed": DEMOTION_FLOOR},
                {"window": "1d", "graded": 0, "needed": DEMOTION_FLOOR},
            ],
            "asOfRunId": None,
        }
    assert body["lastRun"] is None
    assert body["accuracyRun"] is None
    assert body["nextRunAt"] is None
    assert body["manualAvailableAt"] is None


def test_an_active_source_serves_counts_split_and_how_many_more_at_the_floor(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    """29 graded needs one more; 30 needs none. Counts only -- no percentage."""
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=2),
                        finished_at=NOW - timedelta(days=2) + timedelta(minutes=3))
    set_source_status(db_engine, "rules", "active", at=NOW - timedelta(days=2), run_id=run)
    add_accuracy(db_engine, run, source="rules", window="1h",
                 watch=(20, 12, 3), discovery=(DEMOTION_FLOOR - 1 - 20, 5, 1))
    add_accuracy(db_engine, run, source="rules", window="1d",
                 watch=(DEMOTION_FLOOR, 18, 0))
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
            "combined": {"graded": DEMOTION_FLOOR - 1, "correct": 17, "neutral": 4},
            "watch": {"graded": 20, "correct": 12, "neutral": 3},
            "discovery": {"graded": DEMOTION_FLOOR - 1 - 20, "correct": 5, "neutral": 1},
        },
        {
            "window": "1d",
            "basis": "trailing_28d",
            "combined": {"graded": DEMOTION_FLOOR, "correct": 18, "neutral": 0},
            "watch": {"graded": DEMOTION_FLOOR, "correct": 18, "neutral": 0},
            "discovery": {"graded": 0, "correct": 0, "neutral": 0},
        },
    ]
    assert rules["eligibility"] == {
        "kind": "demote",
        "basis": "trailing_28d",
        "floor": DEMOTION_FLOOR,
        "eligible": False,
        "windows": [
            {"window": "1h", "graded": DEMOTION_FLOOR - 1, "needed": 1},
            {"window": "1d", "graded": DEMOTION_FLOOR, "needed": 0},
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


def test_a_source_at_the_floor_in_both_windows_is_eligible(
    sentiment_client: TestClient, db_engine: Engine
) -> None:
    run = add_audit_run(db_engine, started_at=NOW - timedelta(days=1),
                        finished_at=NOW - timedelta(days=1))
    add_accuracy(db_engine, run, source="massive", window="1h", watch=(DEMOTION_FLOOR, 16, 0))
    # Over the floor is still "none needed", never a negative count.
    add_accuracy(db_engine, run, source="massive", window="1d",
                 watch=(DEMOTION_FLOOR + 1, 16, 0))
    massive = _source(sentiment_client.get(SENTIMENT).json(), "massive")
    assert massive["status"] == "unaudited"
    assert massive["eligibility"]["eligible"] is True
    assert [w["needed"] for w in massive["eligibility"]["windows"]] == [0, 0]


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
                 watch=(60, 40, 0), discovery=(REPROMOTION_FLOOR - 1 - 60, 25, 1))
    add_accuracy(db_engine, latest, source="massive", window="1d", basis="since_demotion",
                 watch=(REPROMOTION_FLOOR, 70, 0))

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
        "eligible": False,
        "windows": [
            {"window": "1h", "graded": REPROMOTION_FLOOR - 1, "needed": 1},
            {"window": "1d", "graded": REPROMOTION_FLOOR, "needed": 0},
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
        "eligible": False,
        "windows": [
            {"window": "1h", "graded": 0, "needed": REPROMOTION_FLOOR},
            {"window": "1d", "graded": 0, "needed": REPROMOTION_FLOOR},
        ],
        "asOfRunId": None,
    }
    # The figure that demoted it is still shown, on its own basis.
    assert [a["basis"] for a in rules["accuracy"]] == ["trailing_28d", "trailing_28d"]


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
    assert response.json() == {
        "status": "started",
        "trigger": "manual",
        "requestedAt": "2026-10-14T15:00:00Z",
    }
    _wait_for_run(app)
    (run,) = _runs(db_engine)
    assert (run.trigger, run.status) == ("manual", "succeeded")

    (notice,) = delivered
    assert notice.event == "sentiment_audit_requested"
    assert notice.severity == "info"
    assert "11:00:00 EDT" in notice.body

    # The run is the record GET reads back.
    body = sentiment_client.get(SENTIMENT).json()
    assert body["lastRun"]["id"] == run.id
    assert body["lastRun"]["trigger"] == "manual"
    assert datetime.fromisoformat(body["manualAvailableAt"]) == NOW + MANUAL_AUDIT_INTERVAL


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


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", f"{SENTIMENT}/massive"),
        ("post", f"{SENTIMENT}/massive/reinstate"),
        ("post", f"{SENTIMENT}/reinstate"),
        ("put", SENTIMENT),
        ("patch", SENTIMENT),
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


def test_no_api_module_writes_the_source_status() -> None:
    """Nothing under ``corollary/api`` assigns a ``sentiment_source_status`` field."""
    import re
    from pathlib import Path

    package = Path(sentiment_routes.__file__).resolve().parents[1]
    writes = re.compile(r"\.(status|changed_at|audit_run_id)\s*=(?!=)")
    offenders = []
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "SentimentSourceStatus" in text and writes.search(text):
            offenders.append(path.name)
    assert offenders == [], offenders


def test_the_module_imports_no_event_loop_blocking_sleep() -> None:
    """The run is a task on the loop; the route never waits for it to finish."""
    source = inspect.getsource(sentiment_routes)
    assert "time.sleep" not in source
    assert "run_until_complete" not in source
    assert inspect.iscoroutinefunction(sentiment_routes.start_sentiment_audit)
