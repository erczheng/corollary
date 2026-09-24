"""A broken ``DISCORD_WEBHOOK_URL`` degrades one channel, never the engine.

Discord is an optional channel. A value ``urllib.parse.urlsplit`` raises on --
an unbalanced IPv6 bracket -- used to raise out of ``DiscordNotifier``'s
constructor, which the lifespan ran before its guarded start, so one optional
setting took the whole app down: no ``/api/health``, no halt, no bell. Rule 9's
voice must not depend on its least essential sink.

What these pin, end to end through the real lifespan:

* the app starts and answers ``/api/health``;
* ``POST /api/engine/halt`` still persists the halt and writes its bell row;
* the Discord delivery for that notification is ``dropped``, naming the
  variable and why, never the value;
* the URL and its token reach no log record and no delivery row (rule 6);
* a sink whose construction raises anyway is contained by the lifespan.

Every URL here is a dummy value.
"""

import importlib
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.db.models import (
    ENGINE_STATE_ID,
    EngineState,
    NotificationDelivery,
    NotificationRecord,
)
from corollary.engine.notify import DiscordNotifier
from corollary.engine.runtime import DISCORD_WEBHOOK_ENV

pytestmark = pytest.mark.risk

TOKEN = "dummy-token-not-real"
HOST_FRAGMENT = "[::1"
MALFORMED_WEBHOOK_URL = f"https://{HOST_FRAGMENT}/api/webhooks/1/{TOKEN}"


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record, message, extras and traceback, as one string."""
    formatter = logging.Formatter()
    parts = []
    for record in caplog.records:
        parts.append(formatter.format(record))
        parts.append(repr(record.__dict__))
    return "\n".join(parts)


def _deliveries(engine: Engine) -> list[NotificationDelivery]:
    with Session(engine) as session:
        rows = list(
            session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id))
        )
        session.expunge_all()
        return rows


def _halted(engine: Engine) -> bool:
    with Session(engine) as session:
        state = session.get(EngineState, ENGINE_STATE_ID)
        assert state is not None
        return state.halted


@pytest.fixture
def malformed_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, MALFORMED_WEBHOOK_URL)
    yield


def _halt_and_collect(
    registry: ServiceRegistry, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> list[NotificationDelivery]:
    # discord=True: the shipped app's setting, so the webhook is actually read.
    app = create_app(registry=registry, db_engine=db_engine, discord=True)
    with caplog.at_level(logging.DEBUG):
        with TestClient(app) as client:
            assert client.get("/api/health").status_code == 200
            response = client.post(
                "/api/engine/halt", json={"reason": f"discord is broken: {TOKEN}"}
            )
            assert response.status_code == 200
    assert _halted(db_engine) is True
    with Session(db_engine) as session:
        state = session.get(EngineState, ENGINE_STATE_ID)
        assert state is not None and state.halted_reason is not None
        assert TOKEN not in state.halted_reason
    return _deliveries(db_engine)


def test_a_malformed_webhook_leaves_the_app_up_and_the_halt_recorded(
    registry: ServiceRegistry,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    malformed_env: None,
) -> None:
    rows = _halt_and_collect(registry, db_engine, caplog)

    with Session(db_engine) as session:
        records = list(session.scalars(select(NotificationRecord)))
    assert len(records) == 1
    [record] = records
    bell = [r for r in rows if r.channel == "bell"]
    discord = [r for r in rows if r.channel == "discord"]
    assert [(r.notification_id, r.status) for r in bell] == [(record.id, "delivered")]
    assert [(r.notification_id, r.status) for r in discord] == [(record.id, "dropped")]
    assert discord[0].detail == f"{DISCORD_WEBHOOK_ENV} is set but malformed"

    for row in rows:
        assert TOKEN not in row.detail
        assert HOST_FRAGMENT not in row.detail
    logged = _everything_logged(caplog)
    assert TOKEN not in logged
    assert HOST_FRAGMENT not in logged
    assert any(
        getattr(r, "event", None) == "notification_discord_misconfigured"
        and r.levelno == logging.ERROR
        for r in caplog.records
    )


def test_a_non_https_webhook_is_unavailable_through_the_app(
    registry: ServiceRegistry,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``.invalid`` never resolves, so a regression fails the assertion below
    # rather than posting a request anywhere real.
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, f"http://discord.invalid/api/webhooks/1/{TOKEN}")
    rows = _halt_and_collect(registry, db_engine, caplog)
    discord = [r for r in rows if r.channel == "discord"]
    assert [r.status for r in discord] == ["dropped"]
    assert discord[0].detail == f"{DISCORD_WEBHOOK_ENV} is set but is not an https URL"
    assert TOKEN not in _everything_logged(caplog)


def test_a_sink_whose_construction_raises_is_contained_by_the_lifespan(
    registry: ServiceRegistry,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    malformed_env: None,
) -> None:
    """Belt and braces: whatever the constructor raises, the app still starts.

    The first construction -- the configured sink -- raises with the URL in
    its message, as a careless parser would. The lifespan must log the class
    name only, fall back to a sink that records every alert as dropped, and
    keep the bell.
    """
    calls: list[dict[str, Any]] = []

    def exploding(**kwargs: Any) -> DiscordNotifier:
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError(f"cannot build a sink for {kwargs['webhook_url']}")
        return DiscordNotifier(**kwargs)

    # The package root re-exports the ``app`` object, which shadows the module.
    app_module = importlib.import_module("corollary.api.app")
    monkeypatch.setattr(app_module, "DiscordNotifier", exploding)
    rows = _halt_and_collect(registry, db_engine, caplog)

    bell = [r for r in rows if r.channel == "bell"]
    discord = [r for r in rows if r.channel == "discord"]
    assert [r.status for r in bell] == ["delivered"]
    assert [r.status for r in discord] == ["dropped"]
    assert "could not be built" in discord[0].detail
    assert "RuntimeError" in discord[0].detail
    logged = _everything_logged(caplog)
    assert TOKEN not in logged
    assert any(
        getattr(r, "event", None) == "notification_discord_not_built"
        and r.levelno == logging.ERROR
        for r in caplog.records
    )
