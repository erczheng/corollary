"""The halt reason is owner-typed free text, scrubbed once where it enters.

``HaltRequest.reason`` is copied into ``engine_state.halted_reason``, two log
lines, the bell and Discord. Rule 6 says no key reaches log output, and a
reason is exactly where a key lands when the owner pastes the wrong line. So
the route scrubs the reason **once**, before any of those sinks sees it, and
every sink carries the same scrubbed text -- not a per-sink redaction that one
future sink forgets.

Every credential in this file is an obviously fake dummy.
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.routes.engine import HALTED_REASON_WIDTH
from corollary.db.models import (
    ENGINE_STATE_ID,
    EngineState,
    NotificationDelivery,
    NotificationRecord,
)
from corollary.engine.notify import discord_payload
from corollary.engine.runtime import DISCORD_WEBHOOK_ENV, EngineRuntime, Notification
from corollary.wire import REDACTED

DUMMY_SECRET = "dummy-secret-not-a-real-alpaca-secret"
#: Shorter than 32 characters on purpose, so only the configured-URL
#: derivation -- not the long-run shape -- can redact it.
DUMMY_TOKEN = "dummy-hook-tok-0001"
DUMMY_HOOK_ID = "987654321098765432"
DUMMY_WEBHOOK = f"https://discord.com/api/webhooks/{DUMMY_HOOK_ID}/{DUMMY_TOKEN}"
#: Configured nowhere; only its shape can catch it.
UNCONFIGURED_KEY = "sk-ant-api03-dummy-filler"

LEAKS = (DUMMY_SECRET, DUMMY_WEBHOOK, DUMMY_TOKEN, UNCONFIGURED_KEY)


class RecordingNotifier:
    def __init__(self) -> None:
        self.emitted: list[Notification] = []

    def emit(self, notification: Notification) -> None:
        self.emitted.append(notification)


@pytest.fixture
def halt_client(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    # ``app.state.secret_values`` reads the environment per request.
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", DUMMY_SECRET)
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, DUMMY_WEBHOOK)
    with TestClient(app) as client:
        yield client


def _stored_reason(db_engine: Engine) -> str | None:
    with Session(db_engine) as session:
        state = session.get(EngineState, ENGINE_STATE_ID)
        assert state is not None
        return state.halted_reason


def _log_texts(caplog: pytest.LogCaptureFixture) -> list[str]:
    texts: list[str] = []
    for record in caplog.records:
        texts.append(record.getMessage())
        texts.extend(str(value) for value in record.__dict__.values())
    return texts


def _assert_scrubbed(texts: list[str], what: str) -> None:
    for leak in LEAKS:
        assert not any(leak in text for text in texts), (what, leak)


@pytest.mark.risk
def test_a_secret_in_the_halt_reason_reaches_no_sink(
    halt_client: TestClient, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    app: Any = halt_client.app
    recorder = RecordingNotifier()
    app.state.engine_runtime = EngineRuntime(
        session_factory=lambda: Session(db_engine), notifier=recorder
    )
    reason = (
        f"pasted {DUMMY_SECRET} and {DUMMY_WEBHOOK} and {UNCONFIGURED_KEY} "
        "by mistake"
    )

    # A first halt, so the second takes the already-halted path and its
    # ``engine_halt_repeated`` line is exercised too.
    halt_client.post("/api/engine/halt", json={"reason": "an earlier cause"})
    with caplog.at_level(logging.INFO):
        response = halt_client.post("/api/engine/halt", json={"reason": reason})
    assert response.status_code == 200

    stored = _stored_reason(db_engine)
    assert stored is not None
    _assert_scrubbed([stored, response.text], "engine_state / response")
    assert stored == (
        f"pasted {REDACTED} and {REDACTED} and {REDACTED} by mistake"
    )

    # One boundary: both log lines carry the stored text, not the request's.
    halted = [r for r in caplog.records if r.__dict__.get("event") == "engine_halted"]
    repeated = [
        r for r in caplog.records if r.__dict__.get("event") == "engine_halt_repeated"
    ]
    # caplog holds the whole test, so the first halt's line is here too.
    assert len(halted) == 2 and len(repeated) == 1
    assert halted[-1].__dict__["reason"] == stored
    assert repeated[0].__dict__["reason"] == stored
    assert halted[-1].__dict__["rule"] and halted[-1].__dict__["at"]
    _assert_scrubbed(_log_texts(caplog), "logs")

    notification = recorder.emitted[-1]
    assert notification.event == "operator_halt"
    assert notification.body.startswith(stored)
    _assert_scrubbed([notification.body, notification.title], "notice")
    payload = repr(discord_payload(notification))
    _assert_scrubbed([payload], "discord payload")
    assert REDACTED in payload


@pytest.mark.risk
def test_the_stored_bell_row_carries_the_scrubbed_reason(
    halt_client: TestClient, db_engine: Engine
) -> None:
    reason = f"token {DUMMY_TOKEN} alone, then the tail {DUMMY_HOOK_ID}/{DUMMY_TOKEN}"
    response = halt_client.post("/api/engine/halt", json={"reason": reason})
    assert response.status_code == 200

    with Session(db_engine) as session:
        rows = list(session.scalars(select(NotificationRecord)).all())
        written = [text for row in rows for text in (row.title, row.body)] + [
            row.detail or ""
            for row in session.scalars(select(NotificationDelivery)).all()
        ]
    assert [row.event for row in rows] == ["operator_halt"]
    _assert_scrubbed(written, "notification rows")
    assert DUMMY_HOOK_ID not in rows[0].body
    # The webhook id is a path segment of the configured URL, so it goes too.
    assert rows[0].body == (
        f"token {REDACTED} alone, then the tail {REDACTED}/{REDACTED}"
    )


@pytest.mark.risk
def test_a_plain_reason_is_stored_and_notified_unchanged(
    halt_client: TestClient, db_engine: Engine
) -> None:
    reason = "checking the AAPL241220C00150000 fill by hand"
    response = halt_client.post("/api/engine/halt", json={"reason": reason})
    assert response.status_code == 200
    assert response.json()["haltedReason"] == reason
    assert _stored_reason(db_engine) == reason


@pytest.mark.risk
def test_a_malformed_configured_value_cannot_stop_a_halt_recording(
    halt_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``urlsplit`` raises ValueError on an unbalanced IPv6 bracket. The scrub
    # runs before the halt is recorded, so an exception there would 500 the
    # halt and record nothing -- the emergency control failing over a
    # redaction detail. The literal value must still be redacted.
    malformed = "https://[dummy-malformed-value"
    monkeypatch.setenv("FRED_API_KEY", malformed)
    response = halt_client.post(
        "/api/engine/halt", json={"reason": f"saw {malformed} in a log"}
    )
    assert response.status_code == 200
    assert _stored_reason(db_engine) == f"saw {REDACTED} in a log"


def test_the_scrub_bound_is_the_column_width() -> None:
    column = EngineState.__table__.c.halted_reason
    assert getattr(column.type, "length") == HALTED_REASON_WIDTH


@pytest.mark.risk
def test_a_full_width_reason_that_redaction_lengthens_still_fits_the_column(
    halt_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A short configured value grows to ten characters per occurrence.
    monkeypatch.setenv("FRED_API_KEY", "zq")
    reason = " ".join(["zq"] * 85)  # 254 characters -> about 900 redacted
    response = halt_client.post("/api/engine/halt", json={"reason": reason})
    assert response.status_code == 200
    stored = _stored_reason(db_engine)
    assert stored is not None
    assert len(stored) <= HALTED_REASON_WIDTH
    assert "zq" not in stored


@pytest.mark.risk
@pytest.mark.parametrize("reason", ["   ", "\t\n", "   "])
def test_a_whitespace_only_reason_is_refused_and_does_not_halt(
    halt_client: TestClient, reason: str
) -> None:
    assert halt_client.post("/api/engine/resume").status_code == 200
    assert halt_client.get("/api/engine/state").json()["halted"] is False

    response = halt_client.post("/api/engine/halt", json={"reason": reason})
    assert response.status_code == 422
    assert halt_client.get("/api/engine/state").json()["halted"] is False
