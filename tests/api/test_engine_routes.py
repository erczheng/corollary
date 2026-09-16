"""Halt, resume, and the two things about them that cannot be retrofitted.

Rule 9: *"the dead-man's switch halts automatically, and never auto-resumes.
Recovery requires an explicit human resume. Never auto-resume on reconnect --
reconnecting into an unverified position state is how a bot doubles a position
it already holds."*

Nothing trades in Phase 2, so halting stops nothing. The state, the record and
the explicit-resume requirement are real and tested regardless, because rule 9
is the one item here that cannot be added later: by the time there is an order
path, every caller that clears a halt already exists.
"""

# --------------------------------------------------------------------------
# Why some of these carry ``@pytest.mark.risk`` and some do not
# --------------------------------------------------------------------------
#
# The line -- what earns the marker and what does not -- is stated once, at the
# top of ``tests/engine/test_runtime.py``. This file applies it; it does not
# restate it. Read that note before tagging anything here.
#
# Why the line reaches an API file at all: rule 9's second sentence,
# *"Recovery requires an explicit human resume"*, is enforced by
# ``POST /api/engine/resume`` rather than by the runtime, and that endpoint is
# the only code path in the project that ends a halt. The cold-start halt, the
# halt's own record, the fail-safe default, and every guard on the one control
# that clears a halt are therefore on the money side of the line. ``t0`` (an
# equity-chart marker that gates no trading and reaches no limit), reason
# validation, wire format and route mounting are not, and are left bare.

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.api import routes
from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.db.models import ENGINE_STATE_ID, EngineState

ENGINE_ROUTES = Path(__file__).resolve().parents[2] / "corollary" / "api" / "routes"
PACKAGE = Path(__file__).resolve().parents[2] / "corollary"


def stored(engine: Engine) -> EngineState:
    with Session(engine) as session:
        row = session.get(EngineState, ENGINE_STATE_ID)
        assert row is not None
        session.expunge(row)
        return row


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_a_cold_start_comes_up_halted(client: TestClient) -> None:
    """Halted until the opening snapshot succeeds, per the design spec.

    Coming up running and discovering the connection is down afterwards is the
    wrong order.
    """
    body = client.get("/api/engine/state").json()

    assert body["halted"] is True
    assert body["haltedReason"] is None
    assert body["haltedAt"] is None


def test_state_reports_t0(client: TestClient) -> None:
    """The first-ever-start marker, written by the lifespan."""
    assert client.get("/api/engine/state").json()["t0"] is not None


def test_t0_is_never_rewritten(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Decision 6: the equity curve is Alpaca's, with t0 marked.

    A t0 that moved on every ``uvicorn --reload`` restart would walk the
    marker forward until the chart claimed credit for none of the trading it
    covers -- or, worse, for all of it.
    """
    with TestClient(create_app(registry=registry, db_engine=db_engine)) as first:
        original = first.get("/api/engine/state").json()["t0"]

    with TestClient(create_app(registry=registry, db_engine=db_engine)) as second:
        assert second.get("/api/engine/state").json()["t0"] == original


# --------------------------------------------------------------------------
# Halt
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_halt_persists_the_reason_and_a_utc_timestamp(
    client: TestClient, db_engine: Engine
) -> None:
    response = client.post(
        "/api/engine/halt", json={"reason": "alpaca stream closed"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["halted"] is True
    assert body["haltedReason"] == "alpaca stream closed"

    row = stored(db_engine)
    assert row.halted is True
    assert row.halted_reason == "alpaca stream closed"
    assert row.halted_at is not None
    assert row.halted_at.tzinfo is not None
    assert row.halted_at.utcoffset() == timezone.utc.utcoffset(None)


@pytest.mark.risk
def test_halt_survives_a_new_session(client: TestClient) -> None:
    client.post("/api/engine/halt", json={"reason": "manual"})

    assert client.get("/api/engine/state").json()["haltedReason"] == "manual"


@pytest.mark.risk
def test_halt_logs_the_rule_the_inputs_and_the_timestamp(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 8's standard, applied to a state change rather than a rejection.

    *"Silent rejection is a bug -- you will need this the first time the bot
    does nothing when you expected it to trade."* An unexplained halt has
    exactly that symptom.
    """
    with caplog.at_level(logging.INFO):
        client.post("/api/engine/halt", json={"reason": "watchdog: 90s silent"})

    halts = [r for r in caplog.records if r.__dict__.get("event") == "engine_halted"]
    assert halts, [r.__dict__.get("event") for r in caplog.records]
    record = halts[0].__dict__
    assert record["rule"]
    assert record["reason"] == "watchdog: 90s silent"
    assert record["at"]
    assert record["correlation_id"]


def test_halt_requires_a_reason(client: TestClient) -> None:
    assert client.post("/api/engine/halt", json={"reason": ""}).status_code == 422
    assert client.post("/api/engine/halt", json={}).status_code == 422


def test_a_reason_longer_than_the_column_is_refused(client: TestClient) -> None:
    """256 characters is the column width. A longer one is a 422, not a truncation."""
    response = client.post("/api/engine/halt", json={"reason": "x" * 257})

    assert response.status_code == 422


@pytest.mark.risk
def test_halting_twice_keeps_the_first_cause_in_the_record(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The newest reason is served; the one it replaced is logged, not lost."""
    client.post("/api/engine/halt", json={"reason": "watchdog: 90s silent"})

    with caplog.at_level(logging.INFO):
        client.post("/api/engine/halt", json={"reason": "manual"})

    repeats = [
        r for r in caplog.records if r.__dict__.get("event") == "engine_halt_repeated"
    ]
    assert repeats, [r.__dict__.get("event") for r in caplog.records]
    assert repeats[0].__dict__["previous_reason"] == "watchdog: 90s silent"
    assert client.get("/api/engine/state").json()["haltedReason"] == "manual"


# --------------------------------------------------------------------------
# Resume -- rule 9
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_only_the_resume_endpoint_clears_the_halt(client: TestClient) -> None:
    """Nothing but ``POST /api/engine/resume`` may end a halt.

    Every other request the API can serve is made against a halted engine
    first, and the halt is asserted to survive all of them. Then resume is
    called, and only then does it clear.
    """
    client.post("/api/engine/halt", json={"reason": "watchdog: 90s silent"})

    for _ in range(3):
        assert client.get("/api/engine/state").json()["halted"] is True
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.post(
            "/api/engine/halt", json={"reason": "again"}
        ).is_success
        assert client.get("/api/engine/state").json()["halted"] is True

    body = client.post("/api/engine/resume").json()

    assert body["halted"] is False
    assert body["haltedReason"] is None
    assert body["haltedAt"] is None


@pytest.mark.risk
def test_resume_clears_the_stored_row(
    client: TestClient, db_engine: Engine
) -> None:
    client.post("/api/engine/halt", json={"reason": "manual"})

    client.post("/api/engine/resume")

    row = stored(db_engine)
    assert row.halted is False
    assert row.halted_reason is None
    assert row.halted_at is None


@pytest.mark.risk
def test_a_restart_does_not_resume(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Reconnecting reconnects the socket. It never clears the halt.

    ``uvicorn --reload`` restarts the whole process on every edit, which is
    the most frequent "reconnect" this codebase will ever see.
    """
    with TestClient(create_app(registry=registry, db_engine=db_engine)) as first:
        first.post("/api/engine/halt", json={"reason": "watchdog: 90s silent"})

    with TestClient(create_app(registry=registry, db_engine=db_engine)) as second:
        body = second.get("/api/engine/state").json()

    assert body["halted"] is True
    assert body["haltedReason"] == "watchdog: 90s silent"


def test_resume_logs_who_ended_the_halt(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    client.post("/api/engine/halt", json={"reason": "watchdog: 90s silent"})

    with caplog.at_level(logging.INFO):
        client.post("/api/engine/resume")

    resumes = [
        r for r in caplog.records if r.__dict__.get("event") == "engine_resumed"
    ]
    assert resumes, [r.__dict__.get("event") for r in caplog.records]
    assert resumes[0].__dict__["previous_reason"] == "watchdog: 90s silent"
    assert resumes[0].__dict__["correlation_id"]


@pytest.mark.risk
def test_nothing_else_in_the_package_clears_a_halt() -> None:
    """Structural, like ``test_no_order_path.py``.

    A reviewer can read this rule; a grep enforces it. The auto-resume rule 9
    forbids would arrive as one line in a reconnect handler, and it would look
    entirely reasonable in the diff that introduced it.
    """
    setters = re.compile(r"halted\s*=\s*False")
    offenders = [
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if setters.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == ["api/routes/engine.py"], offenders


@pytest.mark.risk
def test_the_resume_helper_has_exactly_one_caller() -> None:
    callers = [
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if "_clear_halt" in path.read_text(encoding="utf-8")
    ]

    assert callers == ["api/routes/engine.py"], callers


@pytest.mark.risk
def test_nothing_in_the_package_calls_the_resume_route() -> None:
    """``resume`` is public, single-argument, and two lines from anywhere.

    The grep above reads ``_clear_halt``, and the one in
    ``tests/engine/test_runtime.py`` reads ``engine/runtime.py``. Neither
    reaches ``resume(Session(engine))`` written in a *third* file -- which
    passes every other guard, because the private helper is not named and the
    literal ``halted = False`` is not written. There is no such line today.
    What makes it worth pinning is that the socket layer is the
    reconnect-adjacent module where one would look most reasonable, and rule 9
    is explicit: never auto-resume on reconnect. Reconnecting into an
    unverified position state is how a bot doubles a position it already
    holds.
    """
    calls = re.compile(r"\bresume\s*\(")
    callers = [
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if calls.search(path.read_text(encoding="utf-8"))
    ]

    # The definition itself, and nothing else in the package.
    assert callers == ["api/routes/engine.py"], callers

    imports = re.compile(r"import[^\n]*\bresume\b")
    importers = [
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if imports.search(path.read_text(encoding="utf-8"))
    ]

    assert importers == [], importers


# --------------------------------------------------------------------------
# Rule 7 -- halt and flatten are distinct
# --------------------------------------------------------------------------


def served_paths(app: FastAPI) -> list[str]:
    """Every path the app actually serves.

    Read off the OpenAPI document rather than by walking ``app.routes``:
    FastAPI 0.141 keeps an included router as a single nested ``_IncludedRouter``
    entry rather than flattening its routes into the list, so a naive walk
    finds ``/api/health`` and nothing else -- and a test that enumerates
    nothing passes every "this endpoint does not exist" assertion made of it.
    """
    return sorted(app.openapi()["paths"])


@pytest.mark.risk
def test_there_is_no_flatten_endpoint(app: FastAPI) -> None:
    """Rule 7 keeps the two apart, and there is no execution path this phase.

    A flatten that halted, or a halt that flattened, would be one control over
    two very different consequences -- and the destructive one is the one that
    would arrive by accident.
    """
    paths = served_paths(app)

    assert not [path for path in paths if "flatten" in path], paths


#: Every path ``corollary.api:app`` serves. Written out in full rather than
#: derived, because the failure this pins is a router that was *written* and
#: never *mounted* -- and anything derived from the routers themselves would
#: agree with the bug.
SERVED_API_PATHS: Final[tuple[str, ...]] = (
    "/api/account",
    "/api/account/history",
    "/api/account/transfers",
    "/api/activity",
    "/api/activity/rejections",
    "/api/activity/stats",
    "/api/engine/halt",
    "/api/engine/resume",
    "/api/engine/state",
    "/api/health",
    "/api/markets/chain/{underlying}",
    "/api/markets/stocks",
    "/api/markets/underlyings",
    "/api/positions",
    "/api/positions/working",
    "/api/settings/audit",
    "/api/settings/feeds",
    "/api/settings/keys",
    "/api/settings/limits",
    "/api/settings/routes",
    "/api/settings/sources",
)


def test_the_app_serves_exactly_the_routes_it_is_supposed_to(app: FastAPI) -> None:
    """What the *shipped* app mounts, not what the routers could serve.

    This is the one assertion in the suite that fails when a router is
    written, tested, and never added to ``create_app()``. It is not
    hypothetical: every route module below landed complete and green while
    ``app.py`` still mounted ``engine_router`` alone, so
    ``uv run python -m uvicorn corollary.api:app`` answered the 404 envelope
    on every ``/api/account``, ``/api/positions``, ``/api/activity``,
    ``/api/markets`` and ``/api/settings`` path -- with a fully passing
    suite, because every route test builds its own app and mounts its own
    router. The suite was blind to the shipped wiring by construction.

    Step 8 adds ``ws`` and must add it here too. That is the point: this
    list is meant to be edited deliberately, and a router nobody mounted
    fails rather than passing quietly.
    """
    api_paths = [path for path in served_paths(app) if path.startswith("/api")]

    assert tuple(api_paths) == SERVED_API_PATHS


def test_the_routes_package_exports_every_router() -> None:
    """Each router owns its own prefix, so mounting is one line.

    Checked here rather than trusted: a prefix that drifts moves every path
    under it at once, and the page that stops working is the one nobody
    opened today.
    """
    assert routes.account_router.prefix == "/api/account"
    assert routes.activity_router.prefix == "/api/activity"
    assert routes.engine_router.prefix == "/api/engine"
    assert routes.markets_router.prefix == "/api/markets"
    assert routes.positions_router.prefix == "/api/positions"
    assert routes.settings_router.prefix == "/api/settings"


@pytest.mark.risk
def test_state_survives_a_missing_row(
    client: TestClient, db_engine: Engine
) -> None:
    """A deleted singleton reads as halted, never as running.

    The row is seeded on startup, so this is a database somebody has been in.
    Absence of state is not evidence of a healthy engine, and defaulting the
    other way is the one mistake here that could matter.
    """
    with Session(db_engine) as session:
        row = session.get(EngineState, ENGINE_STATE_ID)
        assert row is not None
        session.delete(row)
        session.commit()

    body = client.get("/api/engine/state").json()

    assert body["halted"] is True


def test_halted_at_is_serialized_as_an_instant(client: TestClient) -> None:
    """UTC on the wire. ``America/New_York`` is a rendering decision."""
    halted_at = client.post("/api/engine/halt", json={"reason": "manual"}).json()[
        "haltedAt"
    ]

    parsed = datetime.fromisoformat(halted_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)
