"""Every state-changing request the owner makes lands on the bell and Discord.

The owner, 2026-09-24: *"put it on the bell and discord, any action i do
should be put into the discord."* Five routes change state -- halt, resume,
and the three settings writes -- and each emits exactly one notification
through the **same** path rule 9's halt alert takes
(``EngineRuntime.notify_operator_action`` -> ``_channels_for`` -> the
``FanoutNotifier``). There is no second notifier and no second routing gate.

What these tests pin, in the owner's order:

1. The notification never delays, blocks or fails the action. A notifier
   that raises, or a runtime that is not there at all, leaves every route
   answering its normal success. A refused request (4xx) emits nothing.
2. The settings notifications are built from the values ``_audit`` received,
   so the audit log and Discord cannot disagree about what changed.
3. No secret, key or webhook URL reaches a message (rule 6).
4. Halt and resume stay distinct events (rule 7), and nothing resumes
   automatically (rule 9) -- the resume notification is a record.
5. The routing gate is read after the change commits: switching Discord off
   for ``notification_routes_changed`` means that change is not posted.

Reading or dismissing a bell entry emits nothing -- it would page Discord for
reading Discord, and loop.
"""

import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.operator import OperatorEvent, resume_notice
from corollary.api.routes.engine import OperatorRule
from corollary.api.routes.settings import environment
from corollary.db.models import (
    AuditLog,
    NotificationDelivery,
    NotificationRecord,
)
from corollary.engine.runtime import (
    DISCORD_WEBHOOK_ENV,
    EngineRuntime,
    Notification,
    OperatorNotice,
)

EASTERN = ZoneInfo("America/New_York")

#: Obviously fake. Present only so the rule-6 test has something to look for
#: and fail to find.
FAKE_ENV: dict[str, str] = {
    "ALPACA_PAPER_API_KEY": "not-a-real-key-operator-paper",
    "ALPACA_PAPER_SECRET_KEY": "not-a-real-secret-operator-paper",
    "ANTHROPIC_API_KEY": "not-a-real-secret-operator-anthropic",
    "FINNHUB_API_KEY": "not-a-real-secret-operator-finnhub",
    "FRED_API_KEY": "not-a-real-secret-operator-fred",
    "ALPACA_OPTIONS_FEED": "indicative",
    "ALPACA_STOCK_FEED_HISTORICAL": "sip",
    "ALPACA_STOCK_FEED_REALTIME": "iex",
}
FAKE_WEBHOOK = "https://discord.com/api/webhooks/1234/not-a-real-webhook-token"


@pytest.fixture
def operator_client(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    # Read by the lifespan's Discord sink. The app is built with
    # ``discord=False``, so nothing is posted -- the sink records a dropped
    # delivery, which is how a test sees that Discord was *routed*.
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, FAKE_WEBHOOK)
    app.dependency_overrides[environment] = lambda: dict(FAKE_ENV)
    with TestClient(app) as client:
        yield client


def notifications(engine: Engine) -> list[NotificationRecord]:
    with Session(engine) as session:
        rows = list(session.scalars(select(NotificationRecord)).all())
        for row in rows:
            session.expunge(row)
    return sorted(rows, key=lambda row: row.at)


def channels_of(engine: Engine, notification_id: str) -> set[str]:
    with Session(engine) as session:
        return set(
            session.scalars(
                select(NotificationDelivery.channel).where(
                    NotificationDelivery.notification_id == notification_id
                )
            )
        )


def only(engine: Engine, event: str) -> NotificationRecord:
    rows = [row for row in notifications(engine) if row.event == event]
    assert len(rows) == 1, [(row.event, row.body) for row in notifications(engine)]
    return rows[0]


def audit_rows(engine: Engine) -> list[AuditLog]:
    with Session(engine) as session:
        rows = list(session.scalars(select(AuditLog).order_by(AuditLog.id)).all())
        for row in rows:
            session.expunge(row)
    return rows


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_the_five_events_are_the_contract_the_web_codes_against() -> None:
    assert {event.value for event in OperatorEvent} == {
        "operator_halt",
        "operator_resume",
        "risk_limits_changed",
        "data_feeds_changed",
        "notification_routes_changed",
    }


def test_none_of_them_is_a_critical_event() -> None:
    """PRD section 10's three, and nothing a human does on purpose."""
    from corollary.api.routes import settings

    assert settings._CRITICAL_EVENTS == frozenset(
        {"order_rejected", "daily_loss_halt", "engine_error"}
    )


# --------------------------------------------------------------------------
# Halt and resume
# --------------------------------------------------------------------------


def test_halt_emits_operator_halt_with_the_stored_reason(
    operator_client: TestClient, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        response = operator_client.post(
            "/api/engine/halt", json={"reason": "  checking a fill by hand  "}
        )
    assert response.status_code == 200

    row = only(db_engine, "operator_halt")
    assert row.severity == "warning"
    assert row.title == "Engine halted by operator"
    assert row.body == response.json()["haltedReason"]
    assert row.account is None
    # Seeded: bell on, Discord on (Discord is dropped in tests, but routed).
    assert channels_of(db_engine, row.id) == {"bell", "discord"}

    halted = [r for r in caplog.records if r.__dict__.get("event") == "engine_halted"]
    assert row.correlation_id == halted[0].__dict__["correlation_id"]


def test_a_repeated_halt_says_which_reason_it_replaced(
    operator_client: TestClient, db_engine: Engine
) -> None:
    operator_client.post("/api/engine/halt", json={"reason": "first cause"})
    operator_client.post("/api/engine/halt", json={"reason": "second cause"})

    rows = [row for row in notifications(db_engine) if row.event == "operator_halt"]
    assert len(rows) == 2
    assert "replaced" not in rows[0].body.lower()
    assert rows[1].body.startswith("second cause")
    assert "replaced" in rows[1].body.lower()
    assert "first cause" in rows[1].body


def test_resume_names_the_halt_it_ended_in_eastern_time(
    operator_client: TestClient, db_engine: Engine
) -> None:
    operator_client.post("/api/engine/halt", json={"reason": "stepping away"})
    halted_at = datetime.fromisoformat(
        operator_client.get("/api/engine/state").json()["haltedAt"]
    )

    assert operator_client.post("/api/engine/resume").status_code == 200

    row = only(db_engine, "operator_resume")
    assert row.severity == "info"
    assert row.title == "Engine resumed by operator"
    assert row.account is None
    assert "stepping away" in row.body
    eastern = halted_at.astimezone(EASTERN)
    assert eastern.strftime("%Y-%m-%d %H:%M:%S") in row.body
    assert eastern.tzname() in row.body  # EDT or EST, never UTC
    assert channels_of(db_engine, row.id) == {"bell", "discord"}


def test_resume_of_an_engine_that_was_not_halted_says_so(
    operator_client: TestClient, db_engine: Engine
) -> None:
    operator_client.post("/api/engine/resume")  # ends the cold-start halt
    operator_client.post("/api/engine/resume")  # nothing to end

    rows = [row for row in notifications(db_engine) if row.event == "operator_resume"]
    assert len(rows) == 2
    assert "not halted" in rows[1].body.lower()
    assert "not halted" not in rows[0].body.lower()


_RESUMED_AT = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
_HALTED_AT = datetime(2026, 9, 24, 14, 31, 5, tzinfo=timezone.utc)  # 10:31:05 EDT


def _resume_body(reason: str | None, halted_at: datetime | None) -> str:
    return resume_notice(
        was_halted=True,
        previous_reason=reason,
        previous_at=halted_at,
        at=_RESUMED_AT,
        correlation_id="c",
    ).body


def test_resume_body_with_time_and_reason_recorded() -> None:
    assert _resume_body("stepping away", _HALTED_AT) == (
        "Ended the halt that began 2026-09-24 10:31:05 EDT.\n"
        "Halt reason: stepping away"
    )


@pytest.mark.parametrize("reason", [None, ""])
def test_resume_body_for_the_cold_start_halt_says_so(reason: str | None) -> None:
    """The cold-start row carries neither a time nor a reason -- the notice
    must say which halt that is, not "began an unrecorded time"."""
    assert _resume_body(reason, None) == (
        "Ended the cold-start halt (no time or reason is recorded for it)."
    )


def test_resume_body_with_a_reason_but_no_time() -> None:
    assert _resume_body("stepping away", None) == (
        "Ended the halt (its start time is not recorded).\n"
        "Halt reason: stepping away"
    )


@pytest.mark.parametrize("reason", [None, ""])
def test_resume_body_with_a_time_but_no_reason(reason: str | None) -> None:
    assert _resume_body(reason, _HALTED_AT) == (
        "Ended the halt that began 2026-09-24 10:31:05 EDT.\n"
        "No halt reason is recorded."
    )


def test_resume_of_the_cold_start_halt_through_the_route(
    operator_client: TestClient, db_engine: Engine
) -> None:
    assert operator_client.post("/api/engine/resume").status_code == 200

    row = only(db_engine, "operator_resume")
    assert row.body == (
        "Ended the cold-start halt (no time or reason is recorded for it)."
    )


def test_halt_and_resume_are_distinct_events_with_distinct_titles(
    operator_client: TestClient, db_engine: Engine
) -> None:
    """Rule 7's spirit, and rule 9's: nothing here is a flatten, and the resume
    notification is one a human caused, never one the engine raised."""
    operator_client.post("/api/engine/halt", json={"reason": "x"})
    operator_client.post("/api/engine/resume")

    rows = notifications(db_engine)
    assert [row.event for row in rows] == ["operator_halt", "operator_resume"]
    assert len({row.title for row in rows}) == 2
    assert not any("flatten" in (row.title + row.body).lower() for row in rows)


def test_emitting_a_notification_never_resumes_the_engine(
    operator_client: TestClient, db_engine: Engine
) -> None:
    """A notification is a record. Every write path below leaves a halt alone."""
    operator_client.post("/api/engine/halt", json={"reason": "held"})
    operator_client.put(
        "/api/settings/limits",
        json={"limits": [{"key": "max_daily_loss_pct", "value": 15}]},
    )
    operator_client.put(
        "/api/settings/routes",
        json={"routes": [{"event": "engine_error", "bell": True, "discord": False}]},
    )
    assert operator_client.get("/api/engine/state").json()["halted"] is True


# --------------------------------------------------------------------------
# Settings: one notification per request, built from the audit values
# --------------------------------------------------------------------------


def test_a_limits_change_lists_every_change_and_matches_the_audit_log(
    operator_client: TestClient, db_engine: Engine
) -> None:
    response = operator_client.put(
        "/api/settings/limits",
        json={
            "limits": [
                {"key": "max_daily_loss_pct", "value": 15},
                {"key": "max_concurrent_positions", "value": 6},
                # Unchanged: in the request, in neither record.
                {"key": "max_risk_per_trade_pct", "value": 7},
            ]
        },
    )
    assert response.status_code == 200

    row = only(db_engine, "risk_limits_changed")
    assert row.severity == "info"
    assert row.title == "Risk limits changed"
    assert row.account is None
    audited = audit_rows(db_engine)
    assert row.body.splitlines() == [
        f"{a.field}: {a.previous_value} → {a.new_value}" for a in audited
    ]
    assert row.body.splitlines() == [
        "max_daily_loss_pct: 20 → 15",
        "max_concurrent_positions: 8 → 6",
    ]
    # Seeded: bell off, Discord on.
    assert channels_of(db_engine, row.id) == {"discord"}


def test_a_feeds_change_lists_names_and_values_and_no_secret(
    operator_client: TestClient, db_engine: Engine
) -> None:
    response = operator_client.put(
        "/api/settings/feeds",
        json={"feeds": [{"key": "stockHistorical", "value": "iex"}]},
    )
    assert response.status_code == 200

    row = only(db_engine, "data_feeds_changed")
    assert row.title == "Data feeds changed"
    audited = audit_rows(db_engine)
    assert row.body.splitlines() == [
        f"{a.field}: {a.previous_value} → {a.new_value}" for a in audited
    ]
    assert row.body.splitlines() == ["ALPACA_STOCK_FEED_HISTORICAL: sip → iex"]

    secrets = [
        value
        for name, value in FAKE_ENV.items()
        if name.endswith("_KEY") or name.endswith("SECRET_KEY")
    ]
    secrets += [FAKE_WEBHOOK, "not-a-real-webhook-token"]
    with Session(db_engine) as session:
        written = [
            text
            for record in session.scalars(select(NotificationRecord)).all()
            for text in (record.title, record.body)
        ] + [
            delivery.detail or ""
            for delivery in session.scalars(select(NotificationDelivery)).all()
        ]
    for secret in secrets:
        assert not any(secret in text for text in written), secret


def test_a_routing_change_uses_event_slash_channel_and_matches_the_audit_log(
    operator_client: TestClient, db_engine: Engine
) -> None:
    response = operator_client.put(
        "/api/settings/routes",
        json={
            "routes": [
                {"event": "engine_error", "bell": True, "discord": False},
                {"event": "price_alert", "bell": False, "discord": False},
            ]
        },
    )
    assert response.status_code == 200

    row = only(db_engine, "notification_routes_changed")
    assert row.title == "Notification routing changed"
    audited = audit_rows(db_engine)
    assert [a.field for a in audited] == [
        "engine_error.discord",
        "price_alert.bell",
        "price_alert.discord",
    ]
    assert row.body.splitlines() == [
        f"{a.field.replace('.', ' / ', 1)}: {a.previous_value} → {a.new_value}"
        for a in audited
    ]
    assert row.body.splitlines() == [
        "engine_error / discord: on → off",
        "price_alert / bell: on → off",
        "price_alert / discord: on → off",
    ]


def test_the_new_routing_governs_the_notification_about_itself(
    operator_client: TestClient, db_engine: Engine
) -> None:
    """The gate is read after the commit. Discord off for this event means the
    change that switched it off is not posted -- and the audit log still has it."""
    operator_client.put(
        "/api/settings/routes",
        json={
            "routes": [
                {
                    "event": "notification_routes_changed",
                    "bell": True,
                    "discord": False,
                }
            ]
        },
    )

    row = only(db_engine, "notification_routes_changed")
    assert channels_of(db_engine, row.id) == {"bell"}
    assert [a.field for a in audit_rows(db_engine)] == [
        "notification_routes_changed.bell",
        "notification_routes_changed.discord",
    ]


def test_a_request_that_changes_nothing_emits_nothing(
    operator_client: TestClient, db_engine: Engine
) -> None:
    assert operator_client.put(
        "/api/settings/limits",
        json={"limits": [{"key": "max_daily_loss_pct", "value": 20}]},
    ).status_code == 200
    assert operator_client.put(
        "/api/settings/feeds",
        json={"feeds": [{"key": "options", "value": "indicative"}]},
    ).status_code == 200
    assert operator_client.put(
        "/api/settings/routes",
        json={"routes": [{"event": "order_filled", "bell": True, "discord": True}]},
    ).status_code == 200

    assert audit_rows(db_engine) == []
    assert notifications(db_engine) == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/engine/halt", {}),
        ("post", "/api/engine/halt", {"reason": "x" * 257}),
        (
            "put",
            "/api/settings/limits",
            {"limits": [{"key": "max_daily_loss_pct", "value": 1000}]},
        ),
        (
            "put",
            "/api/settings/limits",
            {
                "limits": [
                    {"key": "max_daily_loss_pct", "value": 15},
                    {"key": "max_daily_loss_pct", "value": 16},
                ]
            },
        ),
        ("put", "/api/settings/feeds", {"feeds": [{"key": "options", "value": "opra"}]}),
        ("put", "/api/settings/feeds", {"feeds": [{"key": "options", "value": "nope"}]}),
        (
            "put",
            "/api/settings/routes",
            {
                "routes": [
                    {"event": "engine_error", "bell": True, "discord": False},
                    {"event": "engine_error", "bell": False, "discord": False},
                ]
            },
        ),
        (
            "put",
            "/api/settings/routes",
            {"routes": [{"event": "not_an_event", "bell": True, "discord": True}]},
        ),
    ],
)
def test_a_refused_request_emits_nothing(
    operator_client: TestClient,
    db_engine: Engine,
    method: str,
    path: str,
    body: dict[str, Any],
) -> None:
    response = getattr(operator_client, method)(path, json=body)
    assert 400 <= response.status_code < 500, response.text
    assert notifications(db_engine) == []


# --------------------------------------------------------------------------
# Never delay, block, or fail the action
# --------------------------------------------------------------------------


class RaisingRuntime:
    """Stands in for the runtime and raises from the one seam the routes use."""

    def __init__(self) -> None:
        self.calls = 0

    def notify_operator_action(self, notice: OperatorNotice) -> Notification:
        self.calls += 1
        raise RuntimeError("the notification path fell over")


class RaisingNotifier:
    def emit(self, notification: Notification) -> None:
        raise RuntimeError("a sink the fan-out did not isolate")


class SpyRuntime:
    def __init__(self) -> None:
        self.notices: list[OperatorNotice] = []

    def notify_operator_action(self, notice: OperatorNotice) -> None:
        self.notices.append(notice)


def _each_action(client: TestClient) -> list[int]:
    return [
        client.post("/api/engine/halt", json={"reason": "a"}).status_code,
        client.post("/api/engine/resume").status_code,
        client.put(
            "/api/settings/limits",
            json={"limits": [{"key": "max_daily_loss_pct", "value": 15}]},
        ).status_code,
        client.put(
            "/api/settings/feeds",
            json={"feeds": [{"key": "stockHistorical", "value": "iex"}]},
        ).status_code,
        client.put(
            "/api/settings/routes",
            json={
                "routes": [{"event": "engine_error", "bell": True, "discord": False}]
            },
        ).status_code,
    ]


@pytest.mark.risk
def test_a_raising_call_site_never_fails_any_of_the_five_actions(
    operator_client: TestClient,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app: Any = operator_client.app
    raising = RaisingRuntime()
    app.state.engine_runtime = raising

    with caplog.at_level(logging.INFO):
        assert _each_action(operator_client) == [200, 200, 200, 200, 200]

    assert raising.calls == 5
    failures = [
        r for r in caplog.records if r.__dict__.get("event") == "operator_notice_failed"
    ]
    assert len(failures) == 5
    assert all(r.__dict__["error_type"] == "RuntimeError" for r in failures)
    # The actions themselves all landed.
    assert len(audit_rows(db_engine)) == 3


@pytest.mark.risk
def test_a_raising_notifier_never_fails_any_of_the_five_actions(
    operator_client: TestClient, db_engine: Engine
) -> None:
    """The runtime's own guard, behind the call site's."""
    app: Any = operator_client.app
    app.state.engine_runtime = EngineRuntime(
        session_factory=lambda: Session(db_engine), notifier=RaisingNotifier()
    )
    assert _each_action(operator_client) == [200, 200, 200, 200, 200]


@pytest.mark.risk
def test_no_runtime_means_the_action_succeeds_and_the_log_says_so(
    operator_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    app: Any = operator_client.app
    app.state.engine_runtime = None

    with caplog.at_level(logging.INFO):
        assert _each_action(operator_client) == [200, 200, 200, 200, 200]

    skipped = [
        r
        for r in caplog.records
        if r.__dict__.get("event") == "operator_notice_not_emitted"
    ]
    assert sorted(r.__dict__["notification_event"] for r in skipped) == sorted(
        event.value for event in OperatorEvent
    )
    assert all(r.__dict__["correlation_id"] for r in skipped)


@pytest.mark.risk
def test_reading_or_dismissing_the_bell_emits_nothing(
    operator_client: TestClient, db_engine: Engine
) -> None:
    operator_client.post("/api/engine/halt", json={"reason": "something to read"})
    row = only(db_engine, "operator_halt")

    app: Any = operator_client.app
    spy = SpyRuntime()
    app.state.engine_runtime = spy

    assert operator_client.post(f"/api/notifications/{row.id}/read").status_code == 200
    assert (
        operator_client.post(f"/api/notifications/{row.id}/dismiss").status_code == 200
    )

    assert spy.notices == []
    assert [r.id for r in notifications(db_engine)] == [row.id]


def test_the_notice_reuses_the_routes_correlation_id(
    operator_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    app: Any = operator_client.app
    spy = SpyRuntime()
    app.state.engine_runtime = spy

    with caplog.at_level(logging.INFO):
        operator_client.put(
            "/api/settings/limits",
            json={"limits": [{"key": "max_daily_loss_pct", "value": 15}]},
        )

    changed = [
        r for r in caplog.records if r.__dict__.get("event") == "settings_changed"
    ]
    assert [n.correlation_id for n in spy.notices] == [
        changed[0].__dict__["correlation_id"]
    ]
    assert spy.notices[0].account is None


# --------------------------------------------------------------------------
# engine.py's log records: ``rule`` is an enum, prose is ``policy``
# --------------------------------------------------------------------------


def test_the_engine_routes_log_an_enum_rule_and_a_prose_policy(
    operator_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        operator_client.post("/api/engine/halt", json={"reason": "first"})
        operator_client.post("/api/engine/halt", json={"reason": "second"})
        operator_client.post("/api/engine/resume")

    wanted = {"engine_halt_repeated", "engine_halted", "engine_resumed"}
    records = [r.__dict__ for r in caplog.records if r.__dict__.get("event") in wanted]
    assert {r["event"] for r in records} == wanted
    values = {rule.value for rule in OperatorRule}
    for record in records:
        assert record["rule"] in values, record["rule"]
        assert " " in record["policy"]


def test_the_runtime_no_longer_documents_the_mismatch() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "corollary" / "engine" / "runtime.py"
    ).read_text(encoding="utf-8")
    assert "Mismatch worth someone's attention" not in source
