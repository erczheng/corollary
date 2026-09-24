"""The bell's API: ``GET /api/notifications`` and its read and dismiss actions.

Phase 3 decision 14. Three rules the routes carry:

* **Account scoping, per the bell's rules** -- a book sees its own rows and the
  book-less ones (``account IS NULL``: engine and audit events), never the
  other book's. An engine fault hidden because the other account is selected
  would hide the one event you most need to see.
* **The routing gate stays at emit time.** The route shows what the bell
  *received*, recorded as a ``bell`` delivery row when the notification was
  raised; it never re-reads ``notification_route``. Unchecking a route must
  not retroactively erase notifications already received.
* **No broker.** Notifications are the engine's own rows; a missing credential
  must not hide a halt alert.

The end-to-end cases drive a real halt through the app's own runtime, which
is what proves the lifespan wires the database sink at all.
"""

import importlib
import logging
from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.api.operator import OperatorEvent
from corollary.api.schemas import NotificationEvent
from corollary.db.models import (
    NotificationDelivery,
    NotificationRecord,
    NotificationRoute,
)
from corollary.db.seed import seed
from corollary.engine.runtime import HALT_EVENT, EngineRuntime, HaltDecision, HaltRule

T0 = datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc)


def _add(
    engine: Engine,
    ident: str,
    *,
    account: str | None,
    minutes: int = 0,
    bell: bool = True,
    event: str = "order_filled",
    severity: str = "info",
    dismissed: bool = False,
) -> None:
    with Session(engine) as session:
        session.add(
            NotificationRecord(
                id=ident,
                at=T0 + timedelta(minutes=minutes),
                event=event,
                severity=severity,
                account=account,
                title=f"title {ident}",
                body=f"body {ident}",
                correlation_id=f"corr-{ident}",
                dismissed_at=T0 if dismissed else None,
            )
        )
        session.flush()
        if bell:
            session.add(
                NotificationDelivery(
                    notification_id=ident,
                    channel="bell",
                    status="delivered",
                    attempted_at=T0,
                    detail="written to the bell",
                )
            )
        session.commit()


def _ids(client: TestClient, account: str | None = None) -> list[str]:
    url = "/api/notifications" + (f"?account={account}" if account else "")
    response = client.get(url)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()]


@pytest.fixture
def seeded(db_engine: Engine) -> Engine:
    with Session(db_engine) as session:
        seed(session)
        session.commit()
    return db_engine


# --------------------------------------------------------------------------
# Scoping
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_a_book_sees_its_own_rows_and_the_book_less_ones(
    client: TestClient, db_engine: Engine
) -> None:
    _add(db_engine, "paper1", account="paper", minutes=1)
    _add(db_engine, "cash1", account="cash", minutes=2)
    _add(db_engine, "engine1", account=None, minutes=3, event=HALT_EVENT, severity="critical")

    assert _ids(client, "paper") == ["engine1", "paper1"]
    assert _ids(client, "cash") == ["engine1", "cash1"]


@pytest.mark.risk
def test_no_account_parameter_means_paper(client: TestClient, db_engine: Engine) -> None:
    _add(db_engine, "paper1", account="paper")
    _add(db_engine, "cash1", account="cash")
    assert _ids(client) == ["paper1"]


@pytest.mark.risk
def test_cash_is_answered_without_live_keys(
    client: TestClient, db_engine: Engine
) -> None:
    """No broker dependency: a halt alert must show whatever credentials exist.

    The fixture registry has no cash broker configured, which makes every
    broker-backed route answer 409 for ``?account=cash``. This one answers.
    """
    _add(db_engine, "engine1", account=None, event=HALT_EVENT, severity="critical")
    assert _ids(client, "cash") == ["engine1"]


def test_newest_first(client: TestClient, db_engine: Engine) -> None:
    _add(db_engine, "old", account="paper", minutes=1)
    _add(db_engine, "new", account="paper", minutes=9)
    _add(db_engine, "mid", account=None, minutes=5)
    assert _ids(client, "paper") == ["new", "mid", "old"]


def test_the_limit_bounds_the_list(client: TestClient, db_engine: Engine) -> None:
    for n in range(5):
        _add(db_engine, f"n{n}", account="paper", minutes=n)
    response = client.get("/api/notifications?limit=2")
    assert [item["id"] for item in response.json()] == ["n4", "n3"]


# --------------------------------------------------------------------------
# The routing gate is at emit time
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_a_notification_the_bell_never_received_is_not_listed(
    client: TestClient, db_engine: Engine
) -> None:
    _add(db_engine, "discord-only", account=None, bell=False)
    assert _ids(client, "paper") == []


@pytest.mark.risk
def test_an_orphan_delivery_row_is_no_phantom_bell_item(
    client: TestClient, db_engine: Engine
) -> None:
    """A delivery whose notification row never landed shows nothing and breaks nothing.

    ``notification_delivery.notification_id`` is a soft reference, not a
    foreign key: when the bell write fails -- lock contention during a halt --
    Discord's outcome must still be recordable, and the orphan row is the
    evidence the bell write failed. The bell reads through ``notification``,
    so an orphan can never become an item.
    """
    with Session(db_engine) as session:
        session.add(
            NotificationDelivery(
                notification_id="ghost",
                channel="bell",
                status="delivered",
                attempted_at=T0,
                detail="written to the bell",
            )
        )
        session.commit()
    _add(db_engine, "engine1", account=None, event=HALT_EVENT, severity="critical")
    assert _ids(client, "paper") == ["engine1"]
    assert client.post("/api/notifications/ghost/read").status_code == 404


@pytest.mark.risk
def test_every_event_the_engine_emits_is_one_the_bell_can_render() -> None:
    """An engine event the Literal does not know is a row the bell must skip.

    The route survives that (below), but a halt alert skipped is a halt alert
    unseen -- so every event constant the engine emits today is pinned here.
    Step 6's ``sentiment_demoted``/``sentiment_repromoted`` join this list
    when they start being emitted.
    """
    emitted = {HALT_EVENT} | {event.value for event in OperatorEvent}
    assert emitted <= set(get_args(NotificationEvent))


@pytest.mark.risk
def test_a_row_with_an_unknown_event_does_not_hide_the_rest(
    client: TestClient, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """One unrenderable row is logged and skipped; the halt alert is still served."""
    _add(db_engine, "engine1", account=None, minutes=1, event=HALT_EVENT, severity="critical")
    _add(db_engine, "future", account=None, minutes=2, event="an_event_from_the_future")
    with caplog.at_level(logging.ERROR):
        assert _ids(client, "paper") == ["engine1"]
    skipped = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "notification_unrenderable"
    ]
    assert len(skipped) == 1
    assert skipped[0].levelno == logging.ERROR
    assert getattr(skipped[0], "notification_id") == "future"
    assert getattr(skipped[0], "notification_event") == "an_event_from_the_future"


def test_an_unrenderable_row_is_a_named_error_and_is_left_untouched(
    client: TestClient, db_engine: Engine
) -> None:
    _add(db_engine, "future", account=None, event="an_event_from_the_future")
    for action in ("read", "dismiss"):
        response = client.post(f"/api/notifications/future/{action}")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "notification_unrenderable"
    with Session(db_engine) as session:
        row = session.get(NotificationRecord, "future")
        assert row is not None and row.read_at is None and row.dismissed_at is None


@pytest.mark.risk
def test_unchecking_the_bell_route_erases_nothing_already_received(
    client: TestClient, seeded: Engine
) -> None:
    _add(seeded, "received", account=None, event=HALT_EVENT, severity="critical")
    with Session(seeded) as session:
        session.execute(update(NotificationRoute).values(enabled=False))
        session.commit()
    assert _ids(client, "paper") == ["received"]


# --------------------------------------------------------------------------
# Read and dismiss
# --------------------------------------------------------------------------


def test_the_wire_shape(client: TestClient, db_engine: Engine) -> None:
    _add(db_engine, "engine1", account=None, event=HALT_EVENT, severity="critical")
    [item] = client.get("/api/notifications").json()
    assert item == {
        "id": "engine1",
        "time": "2026-09-24T13:00:00Z",
        "event": "engine_error",
        "severity": "critical",
        "title": "title engine1",
        "detail": "body engine1",
        "account": None,
        "read": False,
        "correlationId": "corr-engine1",
    }


def test_read_marks_read_and_keeps_the_first_time(
    client: TestClient, db_engine: Engine
) -> None:
    _add(db_engine, "paper1", account="paper")
    first = client.post("/api/notifications/paper1/read")
    assert first.status_code == 200
    assert first.json()["read"] is True
    with Session(db_engine) as session:
        read_at = session.get(NotificationRecord, "paper1").read_at  # type: ignore[union-attr]
    again = client.post("/api/notifications/paper1/read")
    assert again.status_code == 200
    with Session(db_engine) as session:
        assert session.get(NotificationRecord, "paper1").read_at == read_at  # type: ignore[union-attr]
    [item] = client.get("/api/notifications").json()
    assert item["read"] is True


def test_dismiss_removes_it_from_the_list_and_keeps_the_row(
    client: TestClient, db_engine: Engine
) -> None:
    _add(db_engine, "paper1", account="paper")
    _add(db_engine, "paper2", account="paper", minutes=1)
    response = client.post("/api/notifications/paper1/dismiss")
    assert response.status_code == 200
    assert _ids(client, "paper") == ["paper2"]
    with Session(db_engine) as session:
        row = session.get(NotificationRecord, "paper1")
        assert row is not None and row.dismissed_at is not None


def test_a_dismissed_row_is_excluded(client: TestClient, db_engine: Engine) -> None:
    _add(db_engine, "gone", account="paper", dismissed=True)
    assert _ids(client, "paper") == []


def test_dismissing_a_book_less_row_clears_it_from_both_books(
    client: TestClient, db_engine: Engine
) -> None:
    _add(db_engine, "engine1", account=None)
    assert client.post("/api/notifications/engine1/dismiss?account=cash").status_code == 200
    assert _ids(client, "paper") == []


def test_an_unknown_id_is_a_404(client: TestClient) -> None:
    response = client.post("/api/notifications/nope/read")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "notification_not_found"


@pytest.mark.risk
def test_the_other_books_row_cannot_be_touched(
    client: TestClient, db_engine: Engine
) -> None:
    _add(db_engine, "cash1", account="cash")
    assert client.post("/api/notifications/cash1/read?account=paper").status_code == 404
    assert client.post("/api/notifications/cash1/dismiss?account=paper").status_code == 404
    with Session(db_engine) as session:
        row = session.get(NotificationRecord, "cash1")
        assert row is not None and row.read_at is None and row.dismissed_at is None


# --------------------------------------------------------------------------
# End to end: the app's own runtime writes the bell
# --------------------------------------------------------------------------


def _halt(runtime: EngineRuntime) -> None:
    runtime.halt(
        HaltDecision(
            rule=HaltRule.STREAM_CLOSED,
            reason="the trading stream closed (code 1006)",
            inputs={"socket": "trading"},
            at=datetime.now(timezone.utc),
        )
    )


@pytest.mark.risk
def test_a_halt_reaches_the_bell_in_both_books(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    app = create_app(registry=registry, db_engine=db_engine)
    with TestClient(app) as client:
        runtime = app.state.engine_runtime
        assert isinstance(runtime, EngineRuntime)
        _halt(runtime)
        for book in ("paper", "cash"):
            [item] = client.get(f"/api/notifications?account={book}").json()
            assert item["event"] == HALT_EVENT
            assert item["severity"] == "critical"
            assert item["account"] is None
            assert item["title"] == "Engine halted"


@pytest.mark.risk
def test_an_app_built_without_discord_records_the_drop_and_posts_nothing(
    registry: ServiceRegistry, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default -- and ``dev_app`` -- never posts, even with a webhook set.

    A ``--reload`` loop halting on every save must not page the owner's
    channel, and a halt routed to Discord must still leave a trace saying it
    was not sent.
    """
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/api/webhooks/1/TOKEN-DEV-ONLY-abcdef0123")
    app = create_app(registry=registry, db_engine=db_engine)
    with TestClient(app):
        _halt(app.state.engine_runtime)
    with Session(db_engine) as session:
        rows = list(
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "discord")
            )
        )
    assert [row.status for row in rows] == ["dropped"]
    assert "TOKEN-DEV-ONLY" not in rows[0].detail


def test_the_shipped_app_delivers_to_discord_and_dev_app_does_not() -> None:
    # The package root re-exports the ``app`` object, which shadows the module.
    app_module = importlib.import_module("corollary.api.app")
    assert app_module.app.state.discord_delivery is True
    assert app_module.dev_app.state.discord_delivery is False


def test_create_app_defaults_to_no_discord(registry: ServiceRegistry, db_engine: Engine) -> None:
    app: FastAPI = create_app(registry=registry, db_engine=db_engine)
    assert app.state.discord_delivery is False
