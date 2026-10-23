"""``/api/account/cash-session``: rule 5's confirmed Cash session, server-checked.

The recorded account fixture carries a scrubbed ``account_number`` of
``00000000-0000-0000-0000-000000000000``, so the live mask is ``••••0000``.
Every test that could leak the full number looks for that string.
"""

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.api.routes import account as account_routes
from corollary.api.routes.account import CASH_SESSION_UNAVAILABLE, cash_session_now
from corollary.db.models import (
    AuditLog,
    EngineState,
    NotificationDelivery,
    NotificationRecord,
)
from corollary.engine.cash_session import CashSessionHolder
from corollary.engine.execution.interface import BrokerAuthError, BrokerError
from corollary.engine.runtime import DISCORD_WEBHOOK_ENV

from .conftest import FIXTURE_DIR, RecordedBroker

FULL_NUMBER = json.loads((FIXTURE_DIR / "account.json").read_text(encoding="utf-8"))[
    "body"
]["account_number"]
LIVE_MASK = "••••" + FULL_NUMBER[-4:]
LIVE_EQUITY = json.loads((FIXTURE_DIR / "account.json").read_text(encoding="utf-8"))[
    "body"
]["equity"]
NOW = datetime(2026, 10, 23, 14, 30, tzinfo=timezone.utc)
FAKE_WEBHOOK = "https://discord.com/api/webhooks/1234/not-a-real-webhook-token"


def _app(registry: ServiceRegistry, db_engine: Engine) -> FastAPI:
    app = create_app(registry=registry, db_engine=db_engine)
    app.dependency_overrides[cash_session_now] = lambda: NOW
    return app


@pytest.fixture
def live_client(
    make_registry: Callable[..., ServiceRegistry],
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    # Read by the lifespan's Discord sink. ``discord=False``: nothing is
    # posted, and the sink records a dropped delivery, which is how a test
    # sees that Discord was *routed*.
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, FAKE_WEBHOOK)
    with TestClient(_app(make_registry(live_keys=True), db_engine)) as client:
        yield client


@pytest.fixture
def keyless_client(
    make_registry: Callable[..., ServiceRegistry], db_engine: Engine
) -> Iterator[TestClient]:
    with TestClient(_app(make_registry(live_keys=False), db_engine)) as client:
        yield client


def _holder(client: TestClient) -> CashSessionHolder:
    app: Any = client.app
    holder = app.state.cash_session
    assert isinstance(holder, CashSessionHolder)
    return holder


def _audit(engine: Engine) -> list[tuple[str, str, str, str]]:
    with Session(engine) as session:
        return [
            (row.category, row.field, row.previous_value, row.new_value)
            for row in session.scalars(select(AuditLog).order_by(AuditLog.id))
        ]


def _notifications(engine: Engine) -> list[NotificationRecord]:
    with Session(engine) as session:
        rows = list(session.scalars(select(NotificationRecord)))
        for row in rows:
            session.expunge(row)
    return sorted(rows, key=lambda row: row.at)


def _channels(engine: Engine, notification_id: str) -> set[str]:
    with Session(engine) as session:
        return set(
            session.scalars(
                select(NotificationDelivery.channel).where(
                    NotificationDelivery.notification_id == notification_id
                )
            )
        )


def _halt_state(engine: Engine) -> tuple[bool, str | None, datetime | None]:
    with Session(engine) as session:
        state = session.scalars(select(EngineState)).one()
        return (state.halted, state.halted_reason, state.halted_at)


def _record_notices(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The events handed to the runtime, in the order it received them.

    ``_notifications`` sorts by ``at``, which the fixed clock makes equal for
    every row, so order is observed at the runtime's seam instead.
    """
    app: Any = client.app
    runtime = app.state.engine_runtime
    original = runtime.notify_operator_action
    seen: list[str] = []

    def spy(notice: Any) -> Any:
        seen.append(notice.event)
        return original(notice)

    monkeypatch.setattr(runtime, "notify_operator_action", spy)
    return seen


def _refusals(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.__dict__.get("event") == "cash_session_refused"
    ]


def _failing_audit(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seen_active: list[bool] | None = None,
    holder: CashSessionHolder | None = None,
) -> None:
    """Make the audit row's commit fail the way a locked SQLite file does."""

    def boom(session: Session, change: Any, at: datetime) -> None:
        if seen_active is not None and holder is not None:
            seen_active.append(holder.active())
        raise OperationalError(
            "INSERT INTO audit_log", {}, Exception("database is locked")
        )

    monkeypatch.setattr(account_routes, "_write_audit", boom)


INACTIVE = {
    "active": False,
    "accountNumberMasked": None,
    "confirmedAt": None,
    "balance": None,
}


# --------------------------------------------------------------------------
# Rule 5: a cold start has no session
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_a_freshly_built_app_has_no_cash_session(
    make_registry: Callable[..., ServiceRegistry], db_engine: Engine
) -> None:
    """Rule 5: every cold start comes up in Paper -- even with live keys set,
    and even after a session was confirmed in an earlier process on the same
    database."""
    registry = make_registry(live_keys=True)
    with TestClient(_app(registry, db_engine)) as first:
        assert first.post(
            "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
        ).status_code == 200
        assert _holder(first).active()

    # A restart: a new app on the same database, which holds the audit row.
    with TestClient(_app(registry, db_engine)) as second:
        assert _holder(second).active() is False
        assert second.get("/api/account/cash-session").json() == INACTIVE


# --------------------------------------------------------------------------
# POST
# --------------------------------------------------------------------------


def test_a_matching_confirmation_starts_a_session(
    live_client: TestClient, cash_broker: RecordedBroker
) -> None:
    response = live_client.post(
        "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "active": True,
        "accountNumberMasked": LIVE_MASK,
        "confirmedAt": "2026-10-23T14:30:00Z",
        "balance": LIVE_EQUITY,
    }
    # Money as a decimal string, never a JSON number.
    assert isinstance(body["balance"], str)
    # The server read the live account itself.
    assert "account" in cash_broker.calls
    assert live_client.get("/api/account/cash-session").json() == body
    assert _holder(live_client).active()


def test_a_mismatch_is_a_409_and_records_nothing(
    live_client: TestClient, db_engine: Engine
) -> None:
    response = live_client.post(
        "/api/account/cash-session", json={"accountNumber": "••••9999"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cash_account_mismatch"
    assert _holder(live_client).active() is False
    assert live_client.get("/api/account/cash-session").json() == INACTIVE
    assert _audit(db_engine) == []
    assert _notifications(db_engine) == []


@pytest.mark.risk
def test_a_mismatch_ends_an_existing_session(
    live_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-off (orchestrator decision P3): a mismatch means the UI is wrong
    about which account is live, so a session confirmed earlier does not
    survive it. The end is audited and notified like any other."""
    live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK})
    assert _holder(live_client).active()
    seen = _record_notices(live_client, monkeypatch)

    response = live_client.post(
        "/api/account/cash-session", json={"accountNumber": "••••9999"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cash_account_mismatch"
    assert _holder(live_client).active() is False
    assert live_client.get("/api/account/cash-session").json() == INACTIVE
    assert _audit(db_engine) == [
        ("account", "cash_session", "inactive", f"active {LIVE_MASK}"),
        ("account", "cash_session", f"active {LIVE_MASK}", "inactive"),
    ]
    assert seen == ["cash_session_ended"]


def test_no_live_credentials_is_a_503_with_a_fixed_reason(
    keyless_client: TestClient, db_engine: Engine, paper_broker: RecordedBroker
) -> None:
    response = keyless_client.post(
        "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
    )
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "cash_unavailable",
        "message": CASH_SESSION_UNAVAILABLE,
    }
    assert _holder(keyless_client).active() is False
    assert _audit(db_engine) == []
    # Paper was not read, let alone confirmed in Cash's place.
    assert paper_broker.calls == []


def test_a_live_account_with_no_number_is_refused(
    live_client: TestClient, cash_broker: RecordedBroker, db_engine: Engine
) -> None:
    async def unnamed() -> Any:
        import dataclasses

        account = await RecordedBroker.account(cash_broker)
        return dataclasses.replace(account, account_number_masked=None)

    cash_broker.account = unnamed  # type: ignore[method-assign]
    response = live_client.post(
        "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "cash_account_unnamed"
    assert _holder(live_client).active() is False
    assert _audit(db_engine) == []


@pytest.mark.parametrize("body", [{}, {"accountNumber": ""}, {"accountNumber": LIVE_MASK, "x": 1}])
def test_a_malformed_body_is_a_422(live_client: TestClient, body: dict[str, Any]) -> None:
    response = live_client.post("/api/account/cash-session", json=body)
    assert response.status_code == 422
    assert _holder(live_client).active() is False


def test_every_refusal_is_logged_with_rule_inputs_and_time(
    live_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        live_client.post(
            "/api/account/cash-session", json={"accountNumber": "••••9999"}
        )
    [record] = [
        r for r in caplog.records if r.__dict__.get("event") == "cash_session_refused"
    ]
    assert record.__dict__["reason"] == "account_mismatch"
    assert "rule 5" in record.__dict__["rule"]
    assert record.__dict__["echoed"] == "••••9999"
    assert record.__dict__["live"] == LIVE_MASK
    assert record.__dict__["at"]
    assert record.__dict__["correlation_id"]


def test_the_503_is_logged_too(
    keyless_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        keyless_client.post(
            "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
        )
    [record] = [
        r for r in caplog.records if r.__dict__.get("event") == "cash_session_refused"
    ]
    assert record.__dict__["reason"] == "no_live_credentials"
    assert record.__dict__["at"]


# --------------------------------------------------------------------------
# DELETE and GET
# --------------------------------------------------------------------------


def test_delete_then_get_shows_inactive(live_client: TestClient) -> None:
    live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK})
    response = live_client.delete("/api/account/cash-session")
    assert response.status_code == 204
    assert response.content == b""
    assert live_client.get("/api/account/cash-session").json() == INACTIVE
    assert _holder(live_client).active() is False


def test_delete_with_no_session_is_a_204_and_records_nothing(
    live_client: TestClient, db_engine: Engine
) -> None:
    assert live_client.delete("/api/account/cash-session").status_code == 204
    assert _audit(db_engine) == []
    assert _notifications(db_engine) == []


def test_get_with_no_session_is_inactive(keyless_client: TestClient) -> None:
    assert keyless_client.get("/api/account/cash-session").json() == INACTIVE


# --------------------------------------------------------------------------
# Audit and notify
# --------------------------------------------------------------------------


def test_confirm_and_end_are_audit_logged(
    live_client: TestClient, db_engine: Engine
) -> None:
    live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK})
    live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK})
    live_client.delete("/api/account/cash-session")
    live_client.delete("/api/account/cash-session")

    assert _audit(db_engine) == [
        ("account", "cash_session", "inactive", f"active {LIVE_MASK}"),
        ("account", "cash_session", f"active {LIVE_MASK}", f"active {LIVE_MASK}"),
        ("account", "cash_session", f"active {LIVE_MASK}", "inactive"),
    ]


def test_confirm_and_end_notify_bell_and_discord_at_info(
    live_client: TestClient, db_engine: Engine
) -> None:
    live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK})
    live_client.delete("/api/account/cash-session")

    rows = _notifications(db_engine)
    assert [row.event for row in rows] == [
        "cash_session_confirmed",
        "cash_session_ended",
    ]
    for row in rows:
        assert row.severity == "info"
        assert LIVE_MASK in row.body
        assert _channels(db_engine, row.id) == {"bell", "discord"}
    assert LIVE_EQUITY in rows[0].body
    # One correlation id per decision: the session's is the confirm's notice's.
    session_ids = {row.correlation_id for row in rows}
    assert len(session_ids) == 2


@pytest.mark.risk
def test_neither_action_touches_the_halt_state(
    live_client: TestClient, db_engine: Engine
) -> None:
    """Rule 9: confirming Cash never resumes the engine, ending it never halts."""
    before = _halt_state(db_engine)
    live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK})
    assert _halt_state(db_engine) == before
    live_client.delete("/api/account/cash-session")
    assert _halt_state(db_engine) == before


# --------------------------------------------------------------------------
# Masking -- responses, logs, audit rows, notifications (rule 6)
# --------------------------------------------------------------------------


def test_the_account_route_serves_the_mask_only(live_client: TestClient) -> None:
    response = live_client.get("/api/account?account=cash")
    assert response.status_code == 200
    assert response.json()["accountNumberMasked"] == LIVE_MASK
    assert FULL_NUMBER not in response.text


@pytest.mark.parametrize(
    "echo", [FULL_NUMBER, "PA3ABCD1234", "612345678", "QQ7"]
)
def test_a_full_number_echoed_back_is_never_logged_or_returned(
    live_client: TestClient, caplog: pytest.LogCaptureFixture, echo: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = live_client.post(
            "/api/account/cash-session", json={"accountNumber": echo}
        )
    assert response.status_code == 409
    assert echo not in response.text
    for record in caplog.records:
        assert echo not in record.getMessage()
        assert echo not in json.dumps(record.__dict__, default=str)


def test_no_full_number_anywhere_after_a_session(
    live_client: TestClient, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        confirmed = live_client.post(
            "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
        )
        read = live_client.get("/api/account/cash-session")
        live_client.delete("/api/account/cash-session")

    assert FULL_NUMBER not in confirmed.text + read.text
    for record in caplog.records:
        assert FULL_NUMBER not in record.getMessage()
        assert FULL_NUMBER not in json.dumps(record.__dict__, default=str)
    for row in _audit(db_engine):
        assert FULL_NUMBER not in " ".join(row)
    for note in _notifications(db_engine):
        assert FULL_NUMBER not in note.title + note.body
    assert FULL_NUMBER not in repr(_holder(live_client).current())


# --------------------------------------------------------------------------
# Rule 5 under concurrency: an end during a confirmation wins (audit M1)
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_a_delete_during_a_confirmation_answers_at_once_and_refuses_it(
    live_client: TestClient,
    cash_broker: RecordedBroker,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The human turned Cash off while the confirm was reading the live
    account. The DELETE answers without waiting on that read, and the
    confirm, which read before the end, is refused: Cash never comes on."""
    seen = _record_notices(live_client, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    async def blocking() -> Any:
        account = await RecordedBroker.account(cash_broker)
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return account

    cash_broker.account = blocking  # type: ignore[method-assign]
    results: dict[str, Any] = {}

    def post() -> None:
        results["post"] = live_client.post(
            "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
        )

    def delete() -> None:
        results["delete"] = live_client.delete("/api/account/cash-session")

    poster = threading.Thread(target=post)
    deleter = threading.Thread(target=delete)
    poster.start()
    try:
        assert entered.wait(10), "the confirm never reached the live account read"
        deleter.start()
        deleter.join(10)
        # Re-audit LOW-1: switching to Paper never waits behind the confirm's
        # slow live read -- the DELETE answers while the read is still blocked.
        assert not deleter.is_alive(), "DELETE waited on an in-flight confirm"
        assert results["delete"].status_code == 204
        assert _holder(live_client).active() is False
    finally:
        release.set()
        poster.join(10)
        if deleter.ident is not None:
            deleter.join(10)

    # The end bumped the generation, so the confirm that read before it is
    # refused: Cash stays off, and nothing was confirmed, recorded or notified.
    assert results["post"].status_code == 409, results["post"].text
    assert results["post"].json()["error"]["code"] == "cash_session_ended_while_confirming"
    assert _holder(live_client).active() is False
    assert live_client.get("/api/account/cash-session").json() == INACTIVE
    assert seen == []
    assert _audit(db_engine) == []


@pytest.mark.risk
def test_switching_to_paper_during_a_slow_reconfirm_ends_cash_at_once(
    live_client: TestClient,
    cash_broker: RecordedBroker,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-audit LOW-1: Cash is on, a re-confirm is stuck on a slow live read,
    and the owner switches to Paper. Cash must be off the moment the DELETE
    answers -- not after the read returns."""
    first = live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK})
    assert first.status_code == 200
    seen = _record_notices(live_client, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    async def blocking() -> Any:
        account = await RecordedBroker.account(cash_broker)
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return account

    cash_broker.account = blocking  # type: ignore[method-assign]
    results: dict[str, Any] = {}
    poster = threading.Thread(
        target=lambda: results.setdefault(
            "post",
            live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK}),
        )
    )
    poster.start()
    try:
        assert entered.wait(10), "the re-confirm never reached the live read"
        ended = live_client.delete("/api/account/cash-session")
        assert ended.status_code == 204
        # Off now, while the re-confirm's read is still blocked.
        assert _holder(live_client).active() is False
    finally:
        release.set()
        poster.join(10)

    assert results["post"].status_code == 409, results["post"].text
    assert _holder(live_client).active() is False
    assert seen == ["cash_session_ended"]
    assert _audit(db_engine)[-1] == ("account", "cash_session", f"active {LIVE_MASK}", "inactive")


@pytest.mark.risk
def test_an_end_by_any_path_during_the_read_refuses_the_confirm(
    live_client: TestClient,
    cash_broker: RecordedBroker,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Belt and braces: an end that does not go through the lock (the
    lifespan, a later unit) still refuses a confirm that read before it."""
    holder = _holder(live_client)

    async def ended_meanwhile() -> Any:
        account = await RecordedBroker.account(cash_broker)
        holder.end()
        return account

    cash_broker.account = ended_meanwhile  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO):
        response = live_client.post(
            "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cash_session_ended_while_confirming"
    assert holder.active() is False
    assert _audit(db_engine) == []
    assert _notifications(db_engine) == []
    [record] = _refusals(caplog)
    assert record.__dict__["reason"] == "ended_while_confirming"
    assert record.__dict__["echoed"] == LIVE_MASK
    assert record.__dict__["correlation_id"]
    assert "rule 5" in record.__dict__["rule"]


@pytest.mark.risk
def test_an_end_during_the_audit_write_refuses_and_corrects_the_row(
    live_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last window: the row saying ``active`` is written, then an end from
    outside the lock lands before the session starts. The end stands, no
    session starts, and a correcting row follows the one that claimed it."""
    holder = _holder(live_client)
    real_write = account_routes._write_audit
    calls: list[str] = []

    def write_then_end(session: Session, change: Any, at: datetime) -> None:
        real_write(session, change, at)
        calls.append(change.new)
        if len(calls) == 1:
            holder.end()

    monkeypatch.setattr(account_routes, "_write_audit", write_then_end)
    seen = _record_notices(live_client, monkeypatch)
    response = live_client.post(
        "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cash_session_ended_while_confirming"
    assert holder.active() is False
    assert _audit(db_engine) == [
        ("account", "cash_session", "inactive", f"active {LIVE_MASK}"),
        ("account", "cash_session", f"active {LIVE_MASK}", "inactive"),
    ]
    assert seen == []


# --------------------------------------------------------------------------
# Fail-off when the audit row cannot be written (audit L1, L2, L3)
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_a_failed_confirm_commit_with_no_prior_session_ends_inactive(
    live_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    holder = _holder(live_client)
    seen_active: list[bool] = []
    _failing_audit(monkeypatch, seen_active=seen_active, holder=holder)
    seen = _record_notices(live_client, monkeypatch)

    with caplog.at_level(logging.INFO):
        response = live_client.post(
            "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
        )
    assert response.status_code == 503
    # L2: the audit row is written before the session exists, so a failed
    # write never had a visible session behind it.
    assert seen_active == [False]
    assert holder.active() is False
    assert live_client.get("/api/account/cash-session").json() == INACTIVE
    # Nothing was on, so nothing ended and nobody is told otherwise.
    assert seen == []
    [unrecorded] = [
        r
        for r in caplog.records
        if r.__dict__.get("event") == "cash_session_unrecorded"
    ]
    assert unrecorded.levelno == logging.ERROR
    assert unrecorded.__dict__["correlation_id"]


@pytest.mark.risk
def test_a_failed_confirm_commit_ends_a_prior_session_and_says_so(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK})
    holder = _holder(live_client)
    assert holder.active()
    _failing_audit(monkeypatch)
    seen = _record_notices(live_client, monkeypatch)

    response = live_client.post(
        "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
    )
    assert response.status_code == 503
    assert holder.active() is False
    assert live_client.get("/api/account/cash-session").json() == INACTIVE
    # L3: the prior session ended, and the owner hears about it.
    assert seen == ["cash_session_ended"]


@pytest.mark.risk
def test_a_failed_end_commit_is_still_a_204_inactive_and_notified(
    live_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    live_client.post("/api/account/cash-session", json={"accountNumber": LIVE_MASK})
    _failing_audit(monkeypatch)
    seen = _record_notices(live_client, monkeypatch)

    with caplog.at_level(logging.INFO):
        response = live_client.delete("/api/account/cash-session")
    assert response.status_code == 204
    assert _holder(live_client).active() is False
    assert live_client.get("/api/account/cash-session").json() == INACTIVE
    assert seen == ["cash_session_ended"]
    [unrecorded] = [
        r
        for r in caplog.records
        if r.__dict__.get("event") == "cash_session_end_unrecorded"
    ]
    assert unrecorded.levelno == logging.ERROR
    assert "rule 5" in unrecorded.__dict__["rule"]
    assert unrecorded.__dict__["correlation_id"]
    assert unrecorded.__dict__["account"] == LIVE_MASK


# --------------------------------------------------------------------------
# A vendor error is a logged Cash refusal (audit L5)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (BrokerAuthError("401 request is not authorized"), "broker_auth"),
        (BrokerError("connection reset"), "broker_unavailable"),
    ],
)
def test_an_unreadable_live_account_is_a_logged_refusal(
    live_client: TestClient,
    cash_broker: RecordedBroker,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    error: BrokerError,
    code: str,
) -> None:
    cash_broker.fail_with = error
    with caplog.at_level(logging.INFO):
        response = live_client.post(
            "/api/account/cash-session", json={"accountNumber": FULL_NUMBER}
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == code
    assert _holder(live_client).active() is False
    assert _audit(db_engine) == []
    [record] = _refusals(caplog)
    assert record.__dict__["reason"] == "live_account_unreadable"
    # The echo was the full number; only its mask is logged (rule 6).
    assert record.__dict__["echoed"] == LIVE_MASK
    assert record.__dict__["correlation_id"]
    assert record.__dict__["error_type"] == type(error).__name__
    for each in caplog.records:
        assert FULL_NUMBER not in json.dumps(each.__dict__, default=str)


# --------------------------------------------------------------------------
# The holder does not outlive a lifespan (audit L4)
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_re_entering_the_same_apps_lifespan_leaves_no_session(
    make_registry: Callable[..., ServiceRegistry], db_engine: Engine
) -> None:
    """Rule 5: a lifespan cycle is a restart, even on the same app object."""
    app = _app(make_registry(live_keys=True), db_engine)
    holder = app.state.cash_session
    with TestClient(app) as first:
        assert first.post(
            "/api/account/cash-session", json={"accountNumber": LIVE_MASK}
        ).status_code == 200
        assert holder.active()
    # Shutdown ends it ...
    assert holder.active() is False

    # ... and startup ends whatever got in between, so nothing carries over.
    holder.confirm(
        account_number_masked=LIVE_MASK,
        balance=Decimal(LIVE_EQUITY),
        at=NOW,
        correlation_id="carried-over",
        generation=holder.generation,
    )
    with TestClient(app) as second:
        assert app.state.cash_session is holder
        assert holder.active() is False
        assert second.get("/api/account/cash-session").json() == INACTIVE
