"""A vendor failure is a stated condition, not a 500 with a traceback.

Two things are being tested. The first is that every failure -- ours, the
vendor's, FastAPI's own 404 and 422 -- comes back in one envelope, because
``api.ts`` should have one shape to parse and a client that has to guess
between two shapes will guess wrong on the day it matters.

The second is rule 6. Nothing that could carry a key or the account number
reaches the response body. Alpaca echoes neither in an error body today, and
nothing about that is a guarantee: anything in front of it that reflects
request headers into an error page writes a credential into a body this
layer would otherwise quote straight back out.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from corollary.api.app import create_app
from corollary.api.deps import BrokerDep, ProviderDep, ServiceRegistry
from corollary.data.providers.interface import (
    FeedAccessError,
    ProviderError,
    RateLimitedError,
)
from corollary.engine.execution.interface import (
    BrokerAuthError,
    BrokerError,
    BrokerRateLimitedError,
)

from .conftest import FakeProvider, RecordedBroker

#: Shaped like Alpaca's, and obviously not one. ``wire._ACCOUNT_NUMBER``
#: matches ``PA`` followed by ten uppercase alphanumerics.
FAKE_ACCOUNT_NUMBER = "PA0EXAMPLE00"
FAKE_SECRET = "not-a-real-secret-value"


def app_that_fails_with(
    exc: BaseException, db_engine: Engine, *, secrets: tuple[str, ...] = ()
) -> FastAPI:
    broker = RecordedBroker(label="paper", fail_with=exc)
    registry = ServiceRegistry(
        brokers={"paper": lambda: broker},  # type: ignore[dict-item]
        provider=FakeProvider,  # type: ignore[arg-type]
        missing_live_credentials=("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY"),
    )
    app = create_app(registry=registry, db_engine=db_engine, secrets=secrets)

    @app.get("/boom")
    async def _boom(broker: BrokerDep) -> dict[str, str]:
        await broker.account()
        return {"status": "unreachable"}

    return app


def call(app: FastAPI) -> Any:
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.get("/boom")


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (BrokerAuthError("the key pair was rejected"), 502, "broker_auth"),
        (BrokerRateLimitedError("429 from the vendor"), 429, "broker_rate_limited"),
        (BrokerError("connection reset"), 502, "broker_unavailable"),
        (RateLimitedError("429 on the data host"), 429, "provider_rate_limited"),
        (FeedAccessError("OPRA agreement is not signed"), 502, "feed_unavailable"),
        (ProviderError("malformed snapshot"), 502, "provider_unavailable"),
    ],
)
def test_vendor_failures_render_as_stated_conditions(
    exc: BaseException, status: int, code: str, db_engine: Engine
) -> None:
    response = call(app_that_fails_with(exc, db_engine))

    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code


def test_the_most_specific_handler_wins(db_engine: Engine) -> None:
    """``BrokerAuthError`` and ``RateLimitedError`` are subclasses.

    Registered against their base as well, so a handler ordering that let the
    base swallow the subclass would report a rate limit as a generic outage
    and send somebody debugging the wrong thing.
    """
    assert call(app_that_fails_with(BrokerAuthError("x"), db_engine)).status_code == 502
    assert (
        call(app_that_fails_with(BrokerRateLimitedError("x"), db_engine)).status_code
        == 429
    )
    assert (
        call(app_that_fails_with(FeedAccessError("x"), db_engine)).status_code == 502
    )


def test_a_key_in_an_error_message_never_reaches_the_body(
    db_engine: Engine,
) -> None:
    leaked = BrokerAuthError(
        f"GET /v2/account returned 401: "
        f'{{"echoed_header": "{FAKE_SECRET}"}}'
    )

    response = call(app_that_fails_with(leaked, db_engine, secrets=(FAKE_SECRET,)))

    assert FAKE_SECRET not in response.text
    assert "<redacted>" in response.json()["error"]["message"]


def test_the_account_number_never_reaches_the_body(db_engine: Engine) -> None:
    """Field-name redaction cannot see inside prose; a shape pass can.

    A ``FEE`` row's description reads *"CAT fee for proceed of 15 trades on
    2026-09-10 by PA0EXAMPLE00"*. If that text ever reaches an exception
    message, it must not reach a response.
    """
    leaked = BrokerError(
        f"unparseable activity: CAT fee for 15 trades by {FAKE_ACCOUNT_NUMBER}"
    )

    response = call(app_that_fails_with(leaked, db_engine))

    assert FAKE_ACCOUNT_NUMBER not in response.text


def test_an_unexpected_exception_renders_the_envelope_with_no_detail(
    db_engine: Engine,
) -> None:
    """A bug is a 500 with a shape, and nothing quoted out of it.

    The traceback belongs in the log, where rule 6 already governs what may
    appear. Quoting it into the body is how something nobody audited ends up
    on a screen.
    """
    response = call(app_that_fails_with(ZeroDivisionError("division by zero"), db_engine))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "ZeroDivisionError" not in response.text
    assert "division by zero" not in response.text


def test_a_database_failure_says_what_to_run(db_engine: Engine) -> None:
    failure = OperationalError("SELECT 1", {}, Exception("no such table: engine_state"))

    response = call(app_that_fails_with(failure, db_engine))

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "database_unavailable"
    assert "alembic upgrade head" in body["message"]


def test_an_unmigrated_database_is_a_stated_condition(
    clientless_app: FastAPI,
) -> None:
    """The engine routes against a database nobody has migrated."""
    with TestClient(clientless_app, raise_server_exceptions=False) as client:
        response = client.get("/api/engine/state")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"


def test_a_404_uses_the_envelope(client: TestClient) -> None:
    response = client.get("/api/nothing-here")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_422_uses_the_envelope(client: TestClient) -> None:
    response = client.post("/api/engine/halt", json={"reason": ""})

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "invalid_request"


def test_a_405_uses_the_envelope(client: TestClient) -> None:
    response = client.post("/api/engine/state")

    assert response.status_code == 405
    assert set(response.json()) == {"error"}


def test_the_lifespan_closes_what_it_built(
    make_registry: Callable[..., ServiceRegistry],
    db_engine: Engine,
    paper_broker: RecordedBroker,
    provider: FakeProvider,
) -> None:
    registry = make_registry()
    app = create_app(registry=registry, db_engine=db_engine)

    @app.get("/touch")
    async def _touch(broker: BrokerDep, data: ProviderDep) -> dict[str, str]:
        return {"label": getattr(broker, "label", "?")}

    with TestClient(app) as client:
        assert client.get("/touch").status_code == 200
        assert paper_broker.closed is False
        assert provider.closed is False

    assert paper_broker.closed is True
    assert provider.closed is True


def test_the_lifespan_closes_nothing_it_did_not_build(
    registry: ServiceRegistry,
    db_engine: Engine,
    paper_broker: RecordedBroker,
    provider: FakeProvider,
) -> None:
    """A broker nobody asked for was never constructed, so there is nothing to close."""
    with TestClient(create_app(registry=registry, db_engine=db_engine)) as client:
        client.get("/api/health")

    assert paper_broker.closed is False
    assert provider.closed is False
