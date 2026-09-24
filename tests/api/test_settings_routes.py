"""Settings, server-backed: the six routes that make §8.7 a real page.

Design spec decision 3, in full, because every test here is downstream of it:

    Risk limits, feed selection and notification routing persist to SQLite
    with §8.7's audit log. These are config, not orders -- rule 1 untouched --
    and they are the database's natural first customers, so Phase 6's risk
    manager reads a table that already exists.

    API key presence, feed status and options level must be server-backed
    regardless: only the server can see the environment.

Four things in this file are load-bearing rather than thorough.

**An unset ceiling is ``null``.** Not 7, not 0, not an omitted row. Two order
tickets once did this lookup themselves with different invented fallbacks --
``?? 7`` in one and ``?? 0`` in the other -- so one reported a ceiling nobody
had set and the other reported every trade as over-limit. The server must not
be a third guesser.

**Every limit proves it rejects, and proves it permits at the boundary.**
CLAUDE.md's testing table asks for both, and only the second one catches a
validator that rejects everything.

**No ``Money`` column is ever ordered, compared or aggregated in SQL.** The
guard raises, so a test that passes is a test that never wrote that query;
:func:`test_reading_the_limits_never_asks_sql_to_order_money` states the
invariant anyway, because the reason it holds is not visible at the call site.

**A rejection leaves a record.** Rule 8: the rule, the inputs, the timestamp.
"""

import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.routes.settings import (
    ALPACA_DATA_PLAN_ENV,
    UNSET,
    _FEED_CATALOGUE,
    _KEY_CATALOGUE,
    environment,
    router as settings_router,
)
from corollary.db.models import (
    ENGINE_STATE_ID,
    AuditLog,
    DataFeed,
    EngineState,
    NotificationRoute,
    RiskLimit,
)

SETTINGS_MODULE = (
    Path(__file__).resolve().parents[2]
    / "corollary"
    / "api"
    / "routes"
    / "settings.py"
)

#: Obviously fake, and never a real key. Rule 6 covers tests as squarely as it
#: covers code: *"No keys in code, in tests, in fixtures, or in log output."*
#: The values exist only so :func:`test_a_key_value_never_reaches_the_wire`
#: has something to look for and fail to find.
DEFAULT_ENV: Mapping[str, str] = {
    "ALPACA_PAPER_API_KEY": "PKTESTTESTTESTTEST",
    "ALPACA_PAPER_SECRET_KEY": "not-a-real-secret-paper",
    "ANTHROPIC_API_KEY": "not-a-real-secret-anthropic",
    "FINNHUB_API_KEY": "not-a-real-secret-finnhub",
    "FRED_API_KEY": "not-a-real-secret-fred",
    "ALPACA_OPTIONS_FEED": "indicative",
    "ALPACA_STOCK_FEED_HISTORICAL": "sip",
    "ALPACA_STOCK_FEED_REALTIME": "iex",
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def settings_app(app: FastAPI) -> FastAPI:
    """The shared app with the settings router mounted.

    Mounted here rather than in ``api/app.py`` because that file belongs to
    the dispatch that wires the routers; this one only has to prove the router
    works wherever it is included.
    """
    app.include_router(settings_router)
    app.dependency_overrides[environment] = lambda: dict(DEFAULT_ENV)
    return app


@pytest.fixture
def settings_client(settings_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(settings_app) as test_client:
        yield test_client


def set_env(app: FastAPI, **overrides: str | None) -> None:
    """Replace the environment the routes see. ``None`` removes a variable."""
    env = dict(DEFAULT_ENV)
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    app.dependency_overrides[environment] = lambda: env


def audit_rows(engine: Engine) -> list[tuple[str, str, str, str]]:
    """``(category, field, previous, new)`` for every audit row, oldest first."""
    with Session(engine) as session:
        return [
            (row.category, row.field, row.previous_value, row.new_value)
            for row in session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
        ]


def stored_limits(engine: Engine) -> dict[str, Decimal]:
    with Session(engine) as session:
        return {row.key: row.value for row in session.scalars(select(RiskLimit)).all()}


def stored_feeds(engine: Engine) -> dict[str, str]:
    with Session(engine) as session:
        return {row.key: row.value for row in session.scalars(select(DataFeed)).all()}


def delete_limit(engine: Engine, key: str) -> None:
    with Session(engine) as session:
        row = session.get(RiskLimit, key)
        assert row is not None
        session.delete(row)
        session.commit()


def by_key(body: list[dict[str, object]], key: str) -> dict[str, object]:
    found = [row for row in body if row["key"] == key]
    assert found, f"{key} is absent from {[row['key'] for row in body]}"
    return found[0]


def put_limit(client: TestClient, key: str, value: str) -> object:
    return client.put(
        "/api/settings/limits", json={"limits": [{"key": key, "value": value}]}
    )


# --------------------------------------------------------------------------
# Risk limits -- reading
# --------------------------------------------------------------------------


def test_the_five_seeded_ceilings_are_served(settings_client: TestClient) -> None:
    """CLAUDE.md rule 4's ceilings, as seeded, in a stable order."""
    body = settings_client.get("/api/settings/limits").json()

    assert [row["key"] for row in body] == [
        "max_risk_per_trade_pct",
        "max_daily_loss_pct",
        "max_concurrent_positions",
        "max_exposure_per_underlying",
        "max_net_directional_pct",
    ]
    assert [row["value"] for row in body] == [7, 20, 8, 25, 40]


def test_a_limit_carries_its_editable_range_and_unit(
    settings_client: TestClient,
) -> None:
    """The range the field accepts, mirroring ``settings.ts``.

    Not the enforced range -- the engine enforces (rule 4). This only stops
    the control accepting a value the server then refuses, which is a bug
    report whichever way round it happens.
    """
    body = settings_client.get("/api/settings/limits").json()

    per_trade = by_key(body, "max_risk_per_trade_pct")
    assert (per_trade["min"], per_trade["max"], per_trade["unit"]) == (1, 25, "%")

    positions = by_key(body, "max_concurrent_positions")
    assert (positions["min"], positions["max"], positions["unit"]) == (1, 20, "count")
    assert positions["help"]


def test_an_unset_limit_is_null_and_never_a_default(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """**The one that matters.** ``null`` means no ceiling is configured.

    ``riskLimitFor`` returns ``number | null`` for exactly this reason, and the
    server must not be the third component to invent a fallback. The key still
    appears -- an omitted row would leave the page with nothing to say "no
    ceiling configured" *about*.
    """
    delete_limit(db_engine, "max_risk_per_trade_pct")

    body = settings_client.get("/api/settings/limits").json()

    assert by_key(body, "max_risk_per_trade_pct")["value"] is None
    assert by_key(body, "max_daily_loss_pct")["value"] == 20


def test_reading_the_limits_never_asks_sql_to_order_money(
    settings_client: TestClient,
) -> None:
    """``Money`` is TEXT on SQLite, so SQL sorts it lexicographically.

    ``ORDER BY value`` over the five seeded ceilings gives 20, 25, 40, 7, 8 --
    plausible, and wrong. The guard raises rather than answering, so this
    passing at all means the route loaded the rows and ordered them in Python.
    The declared order is the catalogue's, which is also why it is stable.
    """
    first = settings_client.get("/api/settings/limits")
    second = settings_client.get("/api/settings/limits")

    assert first.status_code == 200
    assert first.json() == second.json()


# --------------------------------------------------------------------------
# Risk limits -- writing
# --------------------------------------------------------------------------


def test_a_limit_change_persists_and_is_audited(
    settings_client: TestClient, db_engine: Engine
) -> None:
    response = put_limit(settings_client, "max_risk_per_trade_pct", "5")

    assert response.status_code == 200
    assert by_key(response.json(), "max_risk_per_trade_pct")["value"] == 5
    assert stored_limits(db_engine)["max_risk_per_trade_pct"] == Decimal("5")
    assert audit_rows(db_engine) == [("risk", "max_risk_per_trade_pct", "7", "5")]


def test_a_raise_is_audited_in_the_same_words_as_a_lowering(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """Only *raises* confirm on the client. The server audits both directions.

    Nagging on the safe direction trains people to dismiss the dialog that
    matters -- but a record that only covers half the changes is not a record.
    """
    put_limit(settings_client, "max_daily_loss_pct", "10")
    put_limit(settings_client, "max_daily_loss_pct", "30")

    assert audit_rows(db_engine) == [
        ("risk", "max_daily_loss_pct", "20", "10"),
        ("risk", "max_daily_loss_pct", "10", "30"),
    ]


def test_a_change_survives_a_new_session(settings_client: TestClient) -> None:
    put_limit(settings_client, "max_concurrent_positions", "4")

    body = settings_client.get("/api/settings/limits").json()

    assert by_key(body, "max_concurrent_positions")["value"] == 4


def test_setting_an_unset_limit_records_that_there_was_none(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """``(unset)`` rather than an invented previous value of ``0`` or ``7``."""
    delete_limit(db_engine, "max_risk_per_trade_pct")

    put_limit(settings_client, "max_risk_per_trade_pct", "7")

    assert audit_rows(db_engine) == [
        ("risk", "max_risk_per_trade_pct", UNSET, "7")
    ]


def test_a_no_op_write_is_not_audited(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """Writing the value that is already there changed nothing.

    An audit log that fills with rows saying ``7 -> 7`` is one nobody reads,
    which defeats the point of having one on a bad day.
    """
    put_limit(settings_client, "max_risk_per_trade_pct", "7")

    assert audit_rows(db_engine) == []


# --------------------------------------------------------------------------
# Risk limits -- every ceiling rejects, and permits at the boundary
# --------------------------------------------------------------------------

#: ``(key, low, high)`` from ``models.RISK_LIMIT_RANGES``, written out rather
#: than imported so that a range silently widening in the model is a test
#: failure rather than a test that agrees with it.
RANGES: tuple[tuple[str, str, str], ...] = (
    ("max_risk_per_trade_pct", "1", "25"),
    ("max_daily_loss_pct", "1", "50"),
    ("max_concurrent_positions", "1", "20"),
    ("max_exposure_per_underlying", "5", "100"),
    ("max_net_directional_pct", "5", "100"),
)


@pytest.mark.parametrize(("key", "low", "high"), RANGES)
def test_every_limit_permits_its_boundaries(
    settings_client: TestClient, db_engine: Engine, key: str, low: str, high: str
) -> None:
    """The half that catches a validator which rejects everything."""
    assert put_limit(settings_client, key, low).status_code == 200
    assert stored_limits(db_engine)[key] == Decimal(low)

    assert put_limit(settings_client, key, high).status_code == 200
    assert stored_limits(db_engine)[key] == Decimal(high)


@pytest.mark.parametrize(("key", "low", "high"), RANGES)
def test_every_limit_rejects_outside_its_boundaries(
    settings_client: TestClient, db_engine: Engine, key: str, low: str, high: str
) -> None:
    before = stored_limits(db_engine)[key]

    under = put_limit(settings_client, key, str(Decimal(low) - 1))
    over = put_limit(settings_client, key, str(Decimal(high) + 1))

    assert under.status_code == 422, under.json()
    assert over.status_code == 422, over.json()
    assert stored_limits(db_engine)[key] == before
    assert audit_rows(db_engine) == []


@pytest.mark.parametrize("value", ["0", "-7", "Infinity", "NaN"])
def test_a_ceiling_that_is_not_a_ceiling_is_refused(
    settings_client: TestClient, db_engine: Engine, value: str
) -> None:
    """Each of these disables the risk manager in its own way.

    ``Infinity`` passes every check, ``NaN`` raises inside one, and ``0`` and
    ``-7`` reject every trade so the bot silently never trades.
    """
    response = put_limit(settings_client, "max_risk_per_trade_pct", value)

    assert response.status_code == 422, response.json()
    assert stored_limits(db_engine)["max_risk_per_trade_pct"] == Decimal("7")


def test_a_fractional_position_count_is_refused(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """8.5 concurrent positions is not a setting, it is a typo."""
    response = put_limit(settings_client, "max_concurrent_positions", "8.5")

    assert response.status_code == 422
    assert stored_limits(db_engine)["max_concurrent_positions"] == Decimal("8")


def test_an_unknown_limit_key_is_refused(settings_client: TestClient) -> None:
    response = put_limit(settings_client, "max_leverage", "3")

    assert response.status_code == 422


def test_a_float_never_crosses_the_request_boundary(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """Money arrives as a string or a whole number, never as a JSON float.

    A JSON float is an IEEE double by the time ``json`` has finished with it,
    and CLAUDE.md's rule is that no float touches money. Responses serialize
    to a number because the API boundary is a display boundary; requests do
    not get the same licence, because a request value is *stored*.
    """
    response = settings_client.put(
        "/api/settings/limits",
        json={"limits": [{"key": "max_risk_per_trade_pct", "value": 7.5}]},
    )

    assert response.status_code == 422
    assert "string" in response.json()["error"]["message"]
    assert stored_limits(db_engine)["max_risk_per_trade_pct"] == Decimal("7")


def test_a_whole_number_is_accepted_exactly(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """An integer in JSON is exact, so it needs no quoting to be safe."""
    response = settings_client.put(
        "/api/settings/limits",
        json={"limits": [{"key": "max_concurrent_positions", "value": 6}]},
    )

    assert response.status_code == 200
    assert stored_limits(db_engine)["max_concurrent_positions"] == Decimal("6")


def test_a_batch_is_all_or_nothing(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """One bad value in a batch writes none of it.

    Half-applying a set of risk ceilings leaves the engine enforcing a
    combination nobody chose, and the page showing a state nobody saved.
    """
    response = settings_client.put(
        "/api/settings/limits",
        json={
            "limits": [
                {"key": "max_daily_loss_pct", "value": "15"},
                {"key": "max_risk_per_trade_pct", "value": "99"},
            ]
        },
    )

    assert response.status_code == 422
    assert stored_limits(db_engine)["max_daily_loss_pct"] == Decimal("20")
    assert audit_rows(db_engine) == []


def test_the_same_key_twice_in_one_request_is_refused(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """Which of the two wins is not something a caller should have to know."""
    response = settings_client.put(
        "/api/settings/limits",
        json={
            "limits": [
                {"key": "max_daily_loss_pct", "value": "15"},
                {"key": "max_daily_loss_pct", "value": "16"},
            ]
        },
    )

    assert response.status_code == 422
    assert stored_limits(db_engine)["max_daily_loss_pct"] == Decimal("20")


def test_a_rejection_records_the_rule_the_inputs_and_the_timestamp(
    settings_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 8, applied to a config write.

    *"Silent rejection is a bug -- you will need this the first time the bot
    does nothing when you expected it to trade."* A limit refused without a
    record is the same failure a floor earlier.
    """
    with caplog.at_level(logging.INFO):
        put_limit(settings_client, "max_risk_per_trade_pct", "99")

    rejections = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "settings_rejected"
    ]
    assert rejections, [r.__dict__.get("event") for r in caplog.records]
    logged = rejections[0].__dict__
    assert logged["rule"]
    assert logged["field"] == "max_risk_per_trade_pct"
    assert logged["at"]
    assert logged["correlation_id"]


def test_an_accepted_change_is_logged_too(
    settings_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        put_limit(settings_client, "max_risk_per_trade_pct", "6")

    changes = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "settings_changed"
    ]
    assert changes
    logged = changes[0].__dict__
    assert logged["category"] == "risk"
    assert logged["field"] == "max_risk_per_trade_pct"
    assert logged["previous_value"] == "7"
    assert logged["new_value"] == "6"
    assert logged["correlation_id"]


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------


def test_the_three_feed_variables_are_served(settings_client: TestClient) -> None:
    body = settings_client.get("/api/settings/feeds").json()

    assert [(row["key"], row["envVar"], row["value"]) for row in body] == [
        ("options", "ALPACA_OPTIONS_FEED", "indicative"),
        ("stockHistorical", "ALPACA_STOCK_FEED_HISTORICAL", "sip"),
        ("stockRealtime", "ALPACA_STOCK_FEED_REALTIME", "iex"),
    ]


def test_a_feed_change_persists_and_is_audited_under_its_env_var(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """``audit_log.field`` is the stored key, and the stored key is the env var.

    ``models.AuditLog`` names ``ALPACA_OPTIONS_FEED`` as its own example, and
    ``data_feed.key`` is the variable name itself, so anything else here would
    be a second spelling of one fact.
    """
    response = settings_client.put(
        "/api/settings/feeds",
        json={"feeds": [{"key": "stockHistorical", "value": "iex"}]},
    )

    assert response.status_code == 200
    assert stored_feeds(db_engine)["ALPACA_STOCK_FEED_HISTORICAL"] == "iex"
    assert audit_rows(db_engine) == [
        ("feed", "ALPACA_STOCK_FEED_HISTORICAL", "sip", "iex")
    ]


def test_historical_iex_is_permitted_and_says_what_it_costs(
    settings_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """It is a legal choice, and it quietly reinterprets every strategy.

    ``min_avg_volume`` is compared against whatever feed produced the bars, so
    a 5,000,000 threshold measured on IEX is reading a fortieth of real
    volume. Nothing errors; the scanner just returns a different universe.
    """
    with caplog.at_level(logging.INFO):
        response = settings_client.put(
            "/api/settings/feeds",
            json={"feeds": [{"key": "stockHistorical", "value": "iex"}]},
        )

    assert response.status_code == 200
    warnings = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "settings_feed_warning"
    ]
    assert warnings
    assert "min_avg_volume" in warnings[0].__dict__["rule"]


def test_an_upgrade_only_feed_is_refused_on_the_basic_plan(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """Rule 4's shape, applied to a feed: the client marks it, the server refuses.

    Asking for ``opra`` on Basic returns an auth error from Alpaca rather than
    empty data, so letting it be stored moves the failure to the middle of a
    poll, several layers from its cause.
    """
    response = settings_client.put(
        "/api/settings/feeds", json={"feeds": [{"key": "options", "value": "opra"}]}
    )

    assert response.status_code == 422
    assert "upgrade" in response.json()["error"]["message"].lower()
    assert stored_feeds(db_engine)["ALPACA_OPTIONS_FEED"] == "indicative"


def test_the_paid_plan_permits_what_basic_refuses(
    settings_app: FastAPI, settings_client: TestClient, db_engine: Engine
) -> None:
    """Upgrading sets all three and changes nothing else in the code."""
    set_env(settings_app, **{ALPACA_DATA_PLAN_ENV: "algo_trader_plus"})

    response = settings_client.put(
        "/api/settings/feeds",
        json={
            "feeds": [
                {"key": "options", "value": "opra"},
                {"key": "stockRealtime", "value": "sip"},
            ]
        },
    )

    assert response.status_code == 200, response.json()
    assert stored_feeds(db_engine)["ALPACA_OPTIONS_FEED"] == "opra"
    assert stored_feeds(db_engine)["ALPACA_STOCK_FEED_REALTIME"] == "sip"


def test_an_unknown_plan_falls_back_to_the_restrictive_one(
    settings_app: FastAPI, settings_client: TestClient
) -> None:
    """A typo in the plan name must not silently unlock a feed nobody paid for.

    It refuses rather than serving a 500, because the page you would use to
    fix the typo is the one that would go down.
    """
    set_env(settings_app, **{ALPACA_DATA_PLAN_ENV: "premium"})

    response = settings_client.put(
        "/api/settings/feeds", json={"feeds": [{"key": "options", "value": "opra"}]}
    )

    assert response.status_code == 422


def test_a_feed_value_alpaca_does_not_accept_is_refused(
    settings_client: TestClient, db_engine: Engine
) -> None:
    response = settings_client.put(
        "/api/settings/feeds",
        json={"feeds": [{"key": "options", "value": "nasdaq"}]},
    )

    assert response.status_code == 422
    assert stored_feeds(db_engine)["ALPACA_OPTIONS_FEED"] == "indicative"


def test_the_page_says_when_the_running_process_disagrees(
    settings_client: TestClient,
) -> None:
    """The provider reads the environment; Settings edits the table.

    Until the process restarts those are two different values, and a page that
    shows the stored one without saying so is claiming the engine is doing
    something it is not.
    """
    settings_client.put(
        "/api/settings/feeds",
        json={"feeds": [{"key": "stockHistorical", "value": "iex"}]},
    )

    row = by_key(settings_client.get("/api/settings/feeds").json(), "stockHistorical")

    assert "sip" in str(row["help"])
    assert "restart" in str(row["help"]).lower()


def test_an_unset_feed_variable_is_named(
    settings_app: FastAPI, settings_client: TestClient
) -> None:
    """``ALPACA_STOCK_FEED_REALTIME`` is the one actually missing from ``.env``."""
    set_env(settings_app, ALPACA_STOCK_FEED_REALTIME=None)

    row = by_key(settings_client.get("/api/settings/feeds").json(), "stockRealtime")

    assert "not set" in str(row["help"]).lower()


# --------------------------------------------------------------------------
# Notification routing
# --------------------------------------------------------------------------


def test_the_shipped_routing_table_is_served(settings_client: TestClient) -> None:
    """PRD §10's defaults, one row per event with both channels on it."""
    body = settings_client.get("/api/settings/routes").json()

    assert [row["event"] for row in body] == [
        "order_filled",
        "order_rejected",
        "stop_loss_hit",
        "daily_loss_halt",
        "engine_error",
        "operator_halt",
        "operator_resume",
        "price_alert",
        "recommendations_ready",
        "strategy_promotion",
        "risk_limits_changed",
        "data_feeds_changed",
        "notification_routes_changed",
    ]
    routed = {row["event"]: row for row in body}
    assert routed["order_filled"] == {
        "event": "order_filled",
        "bell": True,
        "discord": True,
    }
    assert routed["recommendations_ready"]["discord"] is False


def test_a_routing_change_persists_and_is_audited_per_cell(
    settings_client: TestClient, db_engine: Engine
) -> None:
    """``event.channel``, exactly as ``notificationAuditField`` spells it.

    "Order filled changed" is not a change -- which channel it changed on is
    the whole content of the row.
    """
    response = settings_client.put(
        "/api/settings/routes",
        json={"routes": [{"event": "order_filled", "bell": True, "discord": False}]},
    )

    assert response.status_code == 200
    assert audit_rows(db_engine) == [
        ("notification", "order_filled.discord", "on", "off")
    ]

    with Session(db_engine) as session:
        row = session.get(NotificationRoute, ("order_filled", "discord"))
        assert row is not None and row.enabled is False


def test_only_the_cells_that_changed_are_audited(
    settings_client: TestClient, db_engine: Engine
) -> None:
    settings_client.put(
        "/api/settings/routes",
        json={"routes": [{"event": "price_alert", "bell": True, "discord": True}]},
    )

    assert audit_rows(db_engine) == []


def test_silencing_a_critical_event_is_permitted_and_recorded(
    settings_client: TestClient, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Permitted, per PRD §10 -- and a rule-9 alert routed nowhere is worth a line.

    The confirm that names what stops arriving is the client's job. The
    server's job is to make sure the decision left a trace.
    """
    with caplog.at_level(logging.INFO):
        response = settings_client.put(
            "/api/settings/routes",
            json={
                "routes": [
                    {"event": "engine_error", "bell": False, "discord": False}
                ]
            },
        )

    assert response.status_code == 200
    silenced = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "settings_critical_event_silenced"
    ]
    assert silenced
    assert silenced[0].__dict__["notification_event"] == "engine_error"
    assert len(audit_rows(db_engine)) == 2


def test_an_unknown_notification_event_is_refused(
    settings_client: TestClient,
) -> None:
    response = settings_client.put(
        "/api/settings/routes",
        json={"routes": [{"event": "margin_call", "bell": True, "discord": True}]},
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# API keys -- presence, and nothing else
# --------------------------------------------------------------------------


def test_keys_report_presence_and_never_a_value(
    settings_client: TestClient,
) -> None:
    body = settings_client.get("/api/settings/keys").json()

    paper = by_key(
        [{"key": row["envVar"], **row} for row in body], "ALPACA_PAPER_API_KEY"
    )
    assert paper["present"] is True
    assert paper["optional"] is False
    assert set(paper) >= {"envVar", "purpose", "present", "optional"}
    assert "value" not in paper
    assert "masked" not in paper


def test_a_key_value_never_reaches_the_wire(settings_client: TestClient) -> None:
    """Rule 6: the UI never renders a key, not even four characters of one.

    Checked against the raw body rather than the parsed object, because a
    leak would arrive as a substring of some field nobody thought about.
    """
    raw = settings_client.get("/api/settings/keys").text

    for value in DEFAULT_ENV.values():
        assert value not in raw, value
    assert not re.search(r"PK[A-Z0-9]{6,}", raw)


def test_an_absent_key_reads_absent(
    settings_app: FastAPI, settings_client: TestClient
) -> None:
    set_env(settings_app, FINNHUB_API_KEY=None)

    body = settings_client.get("/api/settings/keys").json()
    finnhub = by_key(
        [{"key": row["envVar"], **row} for row in body], "FINNHUB_API_KEY"
    )

    assert finnhub["present"] is False


def test_a_blank_key_is_absent_rather_than_present(
    settings_app: FastAPI, settings_client: TestClient
) -> None:
    """``FRED_API_KEY=`` is a variable somebody meant to fill in."""
    set_env(settings_app, FRED_API_KEY="   ")

    body = settings_client.get("/api/settings/keys").json()
    fred = by_key([{"key": row["envVar"], **row} for row in body], "FRED_API_KEY")

    assert fred["present"] is False


def test_the_live_pair_agrees_with_the_cash_account_boundary(
    settings_client: TestClient,
) -> None:
    """One fact, one source.

    ``ServiceRegistry`` decides whether Cash is available; if the keys panel
    answered from the environment instead, the page could report a live key
    present while every cash request 409s. Two copies of one fact is how they
    disagree.
    """
    body = settings_client.get("/api/settings/keys").json()
    live = [row for row in body if row["envVar"].startswith("ALPACA_LIVE_")]

    assert len(live) == 2
    assert all(row["present"] is False for row in live)
    assert all(row["optional"] is True for row in live)


def test_the_live_pair_reads_present_when_cash_is_configured(
    make_registry: Callable[..., object], db_engine: Engine
) -> None:
    from corollary.api.app import create_app

    app = create_app(registry=make_registry(live_keys=True), db_engine=db_engine)  # type: ignore[arg-type]
    app.include_router(settings_router)
    app.dependency_overrides[environment] = lambda: dict(DEFAULT_ENV)

    with TestClient(app) as client:
        body = client.get("/api/settings/keys").json()

    live = [row for row in body if row["envVar"].startswith("ALPACA_LIVE_")]
    assert all(row["present"] is True for row in live)


# --------------------------------------------------------------------------
# The audit log
# --------------------------------------------------------------------------


def test_one_log_spans_all_three_categories(
    settings_client: TestClient,
) -> None:
    """PRD §8.7: one log rather than three.

    On a bad day the question is whether *anything* changed first.
    """
    put_limit(settings_client, "max_daily_loss_pct", "15")
    settings_client.put(
        "/api/settings/feeds",
        json={"feeds": [{"key": "stockHistorical", "value": "iex"}]},
    )
    settings_client.put(
        "/api/settings/routes",
        json={"routes": [{"event": "price_alert", "bell": False, "discord": True}]},
    )

    body = settings_client.get("/api/settings/audit").json()

    assert {row["category"] for row in body["items"]} == {
        "risk",
        "feed",
        "notification",
    }
    assert body["total"] == 3


def test_the_audit_log_is_newest_first(settings_client: TestClient) -> None:
    put_limit(settings_client, "max_daily_loss_pct", "15")
    put_limit(settings_client, "max_daily_loss_pct", "16")

    items = settings_client.get("/api/settings/audit").json()["items"]

    assert [row["newValue"] for row in items] == ["16", "15"]


def test_the_audit_log_pages_without_lying_about_the_total(
    settings_client: TestClient,
) -> None:
    for value in ("11", "12", "13"):
        put_limit(settings_client, "max_daily_loss_pct", value)

    first = settings_client.get("/api/settings/audit?page=0&pageSize=2").json()
    second = settings_client.get("/api/settings/audit?page=1&pageSize=2").json()

    assert [row["newValue"] for row in first["items"]] == ["13", "12"]
    assert first["total"] == 3
    assert first["hasMore"] is True
    assert [row["newValue"] for row in second["items"]] == ["11"]
    assert second["hasMore"] is False


def test_an_audit_row_carries_an_aware_utc_timestamp(
    settings_client: TestClient,
) -> None:
    """UTC in storage. Eastern is a display concern and lives in the browser."""
    put_limit(settings_client, "max_daily_loss_pct", "15")

    row = settings_client.get("/api/settings/audit").json()["items"][0]

    assert row["time"].endswith("Z") or "+00:00" in row["time"]


# --------------------------------------------------------------------------
# Data sources -- decision 8's per-panel markers
# --------------------------------------------------------------------------


def test_sources_say_which_panels_are_real_and_which_are_fixtures(
    settings_client: TestClient,
) -> None:
    """Decision 8: Settings mixes real and mock within one page.

    So the markers sit on the affected panels, and this route is where the
    distinction becomes answerable rather than asserted in prose.
    """
    # Cold start comes up halted, so the connected branch needs an explicit
    # resume to reach -- which is rule 9 working, not a fixture detail.
    settings_client.post("/api/engine/resume")

    body = settings_client.get("/api/settings/sources").json()
    named = {row["name"]: row for row in body}

    alpaca = next(row for name, row in named.items() if name.startswith("Alpaca"))
    assert alpaca["status"] == "connected"
    assert "Basic" in alpaca["detail"]

    sentiment = next(
        row for name, row in named.items() if "sentiment" in name.lower()
    )
    assert sentiment["status"] == "disconnected"
    assert "sample data" in sentiment["detail"].lower()


def test_a_source_nothing_reads_yet_does_not_claim_to_be_connected(
    settings_client: TestClient,
) -> None:
    """A present key is not a wired integration, and saying so is the point."""
    body = settings_client.get("/api/settings/sources").json()
    finnhub = next(row for row in body if row["name"].startswith("Finnhub"))

    assert finnhub["status"] == "disconnected"
    assert "key present" in finnhub["detail"].lower()


def test_missing_paper_credentials_read_disconnected(
    settings_app: FastAPI, settings_client: TestClient
) -> None:
    set_env(settings_app, ALPACA_PAPER_API_KEY=None, ALPACA_PAPER_SECRET_KEY=None)

    body = settings_client.get("/api/settings/sources").json()
    alpaca = next(row for row in body if row["name"].startswith("Alpaca"))

    assert alpaca["status"] == "disconnected"
    assert "ALPACA_PAPER_API_KEY" in alpaca["detail"]


def test_a_halted_engine_is_not_reported_as_connected(
    settings_client: TestClient,
) -> None:
    """Rule 9's halt is real state in this phase, so the panel may as well say so.

    ``connected`` here means credentials are configured *and* the engine is
    not halted. There is no live connection probe until the watchdog lands,
    and inventing one on a settings page would cost a vendor request per
    render.
    """
    settings_client.post("/api/engine/halt", json={"reason": "alpaca stream closed"})

    body = settings_client.get("/api/settings/sources").json()
    alpaca = next(row for row in body if row["name"].startswith("Alpaca"))

    assert alpaca["status"] == "degraded"
    assert "alpaca stream closed" in alpaca["detail"]


def test_a_missing_engine_state_row_is_not_evidence_of_health(
    settings_client: TestClient, db_engine: Engine
) -> None:
    with Session(db_engine) as session:
        row = session.get(EngineState, ENGINE_STATE_ID)
        assert row is not None
        session.delete(row)
        session.commit()

    body = settings_client.get("/api/settings/sources").json()
    alpaca = next(row for row in body if row["name"].startswith("Alpaca"))

    assert alpaca["status"] == "degraded"


def test_sources_are_deterministic(settings_client: TestClient) -> None:
    first = settings_client.get("/api/settings/sources").json()
    second = settings_client.get("/api/settings/sources").json()

    assert first == second


# --------------------------------------------------------------------------
# Structural: config is not an order
#
# ``test_the_settings_module_never_imports_the_vendor_sdk`` used to sit here
# as one regex. It is now two guards, in two files, and neither contains the
# other -- which is the correction, because the note that replaced it once
# claimed containment that does not hold.
#
# The parsed half is tree-wide: ``tests/test_hard_rules.py`` takes the
# top-level package off every ``ast.Import`` and ``ast.ImportFrom`` in the
# package, so it catches statement shapes no line-anchored regex sees --
# ``import os, alpaca`` and ``x = 1; import alpaca``. What it does *not* add
# is indentation or aliasing: the two deleted regexes each opened with a
# leading-whitespace match, which already caught the indented import, and
# the first closed on a word boundary, which already caught the aliased
# form ``import alpaca as a``.
#
# The textual half is ``test_the_settings_module_never_writes_the_vendor
# _import`` below, and it stays *here* for the reason the order-path guard
# beside it stays here. Parsing gives up **text**: a line
# reading ``import alpaca`` inside a docstring or behind a ``#`` fires no
# ``ast.Import`` node and the tree-wide guard is silent, while it is one diff
# away from being an import. That is the same argument, applied to the same
# module, and declining to apply it to the vendor SDK while applying it to
# ``submit_order`` was an asymmetry with no reason behind it.
#
# The containment claim is also why ``tests/api/test_account_mode.py``'s
# sibling deletion went wrong -- see the note there. Check the helper, in
# both directions, before deleting anything else on these grounds.
#
# The guard below stays, and stays *here*, because it asserts something the
# general one deliberately cannot. Every tree-wide rule-1 guard reads
# identifiers and ignores text, since the names it forbids appear correctly
# in prose elsewhere. This module is the one place with no such prose and no
# reason to grow any, so it can afford the stricter test: not in the code,
# and not in a comment a later diff could uncomment either.
# --------------------------------------------------------------------------


def test_settings_reaches_no_order_path() -> None:
    """Rule 1, structurally. Config is not an order.

    ``BrokerExecution`` does not exist until Phase 6, and a settings route has
    no business naming it or ``submit_order`` even in a comment that a later
    diff could turn into code.
    """
    source = SETTINGS_MODULE.read_text(encoding="utf-8")

    assert "submit_order" not in source
    assert "BrokerExecution" not in source


#: The vendor import as *text*, in both shapes the deleted one-module regex
#: matched. Unanchored on purpose: a line-anchored pattern was already equal
#: to the parsed guard on indentation and on aliasing, and what this has to
#: cover is the line an ``ast`` walk cannot see at all.
VENDOR_IMPORT_TEXT = re.compile(r"\bimport\s+alpaca\b|\bfrom\s+alpaca[\s.]")


def test_the_settings_module_never_writes_the_vendor_import() -> None:
    """``alpaca`` is imported in exactly two files, and this is neither.

    ``tests/test_hard_rules.py`` proves that by parsing, for every module in
    the package, and that is the guard that matters. This one covers what
    parsing gives up: the words ``import alpaca`` inside a docstring or behind
    a ``#`` raise no ``ast.Import`` node and sit one diff from being real.
    Settings reads feed *names* out of the environment; the SDK is not its
    business, in code or in prose.
    """
    source = SETTINGS_MODULE.read_text(encoding="utf-8")

    found = VENDOR_IMPORT_TEXT.findall(source)
    assert found == [], f"the settings module names the vendor import: {found}"


def test_every_variable_this_module_reads_is_named_in_env_example() -> None:
    """A variable nobody knows to set is a setup gap, not a feature.

    ``ALPACA_STOCK_FEED_REALTIME`` was exactly that -- named in ``.env.example``
    and absent from ``.env`` -- and the provider refuses to start rather than
    defaulting because of it. A variable this module reads that is in neither
    place would be strictly worse: it would read as unset forever, silently.
    """
    example = (
        Path(__file__).resolve().parents[2] / ".env.example"
    ).read_text(encoding="utf-8")

    names = (
        {meta.env_var for meta in _KEY_CATALOGUE}
        | {meta.env_var for meta in _FEED_CATALOGUE}
        | {ALPACA_DATA_PLAN_ENV}
    )

    assert not sorted(name for name in names if name not in example)


def test_the_router_is_mounted_under_one_prefix() -> None:
    paths = {route.path for route in settings_router.routes}  # type: ignore[attr-defined]

    assert paths == {
        "/api/settings/limits",
        "/api/settings/feeds",
        "/api/settings/routes",
        "/api/settings/keys",
        "/api/settings/audit",
        "/api/settings/sources",
    }


def test_every_response_is_the_error_envelope_when_it_fails(
    settings_client: TestClient,
) -> None:
    """One shape for every non-2xx, so ``api.ts`` has one thing to parse."""
    body = put_limit(settings_client, "max_risk_per_trade_pct", "99").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert json.dumps(body)
