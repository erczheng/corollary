"""The account-mode boundary: paper by default, and cash never faked.

Two rules meet here. Rule 5 says every cold start comes up in Paper, so a
missing ``?account=`` is Paper rather than "whatever was selected last". Rule
1 says there is one path to an order, and the way that is kept *structural* is
that routes depend on ``BrokerAccount`` -- the read half -- so ``submit_order``
is not in a type a route can name.

No route in sub-step A consumes a broker yet; the account, positions, activity
and markets routes are separate dispatches. The dependency is exercised
through a probe route, which resolves exactly as those routes will.
"""

import inspect
from typing import Callable

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from corollary.api import deps
from corollary.api.app import create_app
from corollary.api.deps import (
    LIVE_CREDENTIAL_ENV_VARS,
    ServiceRegistry,
    broker_for_account,
    missing_live_credentials,
)
from corollary.engine.execution.interface import BrokerAccount

from .conftest import RecordedBroker, probe_route


def build(registry: ServiceRegistry, db_engine: Engine) -> FastAPI:
    app = create_app(registry=registry, db_engine=db_engine)
    probe_route(app, "/probe", Depends(broker_for_account))
    return app


# --------------------------------------------------------------------------
# Rule 5 -- paper is the default
# --------------------------------------------------------------------------


def test_no_account_parameter_means_paper(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    with TestClient(build(registry, db_engine)) as client:
        assert client.get("/probe").json() == {"label": "paper"}


def test_paper_is_served_when_asked_for(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    with TestClient(build(registry, db_engine)) as client:
        assert client.get("/probe?account=paper").json() == {"label": "paper"}


def test_an_unknown_account_is_a_422(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """Not a silent fall back to paper, and not a 500."""
    with TestClient(build(registry, db_engine)) as client:
        assert client.get("/probe?account=margin").status_code == 422


# --------------------------------------------------------------------------
# Cash without credentials -- 409, never a substitution
# --------------------------------------------------------------------------


def test_cash_is_served_when_the_live_keys_are_present(
    make_registry: Callable[..., ServiceRegistry], db_engine: Engine
) -> None:
    with TestClient(build(make_registry(live_keys=True), db_engine)) as client:
        assert client.get("/probe?account=cash").json() == {"label": "cash"}


def test_cash_without_live_keys_is_a_409(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    with TestClient(build(registry, db_engine)) as client:
        response = client.get("/probe?account=cash")

    assert response.status_code == 409


def test_the_409_names_both_missing_variables(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    with TestClient(build(registry, db_engine)) as client:
        message = client.get("/probe?account=cash").json()["error"]["message"]

    for name in LIVE_CREDENTIAL_ENV_VARS:
        assert name in message, message


def test_the_409_message_is_not_truncated(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """It goes through the same 300-character scrub as a vendor body.

    The clause that matters most -- that paper was not substituted -- is at
    the *end*, and the clause a test asserts on is at the front. A message
    that outgrew the bound would truncate exactly the half nobody checks.
    """
    from corollary.wire import ERROR_BODY_MAX

    with TestClient(build(registry, db_engine)) as client:
        message = client.get("/probe?account=cash").json()["error"]["message"]

    assert "characters truncated" not in message
    assert len(message) < ERROR_BODY_MAX
    assert "not substituted" in message


def test_the_409_uses_the_shared_error_envelope(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """One shape for every non-2xx, so ``api.ts`` parses one thing."""
    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/probe?account=cash").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == "account_unavailable"


def test_cash_without_live_keys_does_not_serve_paper(
    registry: ServiceRegistry, db_engine: Engine, paper_broker: RecordedBroker
) -> None:
    """The failure this endpoint exists to prevent, asserted directly.

    A fall back would be invisible on screen: the header would say Cash and
    the figures would be somebody's real figures, just the wrong somebody's.
    """
    with TestClient(build(registry, db_engine)) as client:
        body = client.get("/probe?account=cash").json()

    assert "label" not in body
    assert paper_broker.calls == []


def test_missing_live_credentials_treats_blank_as_absent() -> None:
    both = dict.fromkeys(LIVE_CREDENTIAL_ENV_VARS, "")

    assert missing_live_credentials(both) == LIVE_CREDENTIAL_ENV_VARS
    assert missing_live_credentials({}) == LIVE_CREDENTIAL_ENV_VARS
    assert missing_live_credentials(
        dict.fromkeys(LIVE_CREDENTIAL_ENV_VARS, "   ")
    ) == LIVE_CREDENTIAL_ENV_VARS


def test_one_missing_half_is_still_a_refusal() -> None:
    """Half a pair authenticates nothing."""
    env = {LIVE_CREDENTIAL_ENV_VARS[0]: "PKSOMETHING"}

    assert missing_live_credentials(env) == (LIVE_CREDENTIAL_ENV_VARS[1],)


def test_a_registry_refuses_an_inconsistent_pair(
    paper_broker: RecordedBroker, cash_broker: RecordedBroker
) -> None:
    """A cash broker alongside missing live keys is a contradiction, not a default."""
    with pytest.raises(ValueError):
        ServiceRegistry(
            brokers={
                deps.AccountMode.PAPER: lambda: paper_broker,
                deps.AccountMode.CASH: lambda: cash_broker,
            },
            provider=lambda: None,  # type: ignore[arg-type,return-value]
            missing_live_credentials=LIVE_CREDENTIAL_ENV_VARS,
        )


# --------------------------------------------------------------------------
# Construction happens once
# --------------------------------------------------------------------------


def test_the_broker_is_built_once_and_cached(db_engine: Engine) -> None:
    built: list[RecordedBroker] = []

    def factory() -> BrokerAccount:
        broker = RecordedBroker(label=f"paper-{len(built)}")
        built.append(broker)
        return broker

    registry = ServiceRegistry(
        brokers={deps.AccountMode.PAPER: factory},
        provider=lambda: None,  # type: ignore[arg-type,return-value]
        missing_live_credentials=LIVE_CREDENTIAL_ENV_VARS,
    )

    with TestClient(build(registry, db_engine)) as client:
        first = client.get("/probe").json()
        second = client.get("/probe").json()

    assert first == second == {"label": "paper-0"}
    assert len(built) == 1


# --------------------------------------------------------------------------
# Rule 1, kept structural
#
# The tree-wide half of rule 1 lives in ``tests/test_hard_rules.py``, which
# reads every module under ``corollary/`` rather than only this package --
# ``test_no_api_module_names_an_order_verb`` and
# ``test_no_api_module_imports_the_vendor_sdk`` were that same assertion
# scoped to ``corollary/api/``, so the general guards contain them.
#
# **Containment is a claim about a specific helper, and it was briefly
# false.** The ``_identifiers`` function deleted from here walked
# ``ast.alias`` and collected both ``alias.name`` and ``alias.asname``;
# ``_referenced_names``, which replaced it, did not. For one revision
# ``from corollary.engine.risk import submit_order as _submit`` in a route
# passed the whole gate, having been caught here before the move. The alias
# branch is back in ``_referenced_names`` and its docstring says it is
# load-bearing *because of this deletion*. Before deleting anything else on
# grounds of containment, read the helper that is supposed to contain it.
#
# What stays here is the thing a tree-wide guard cannot express: *this*
# dependency's return annotation. ``broker_for_account`` handing back the read
# half is a fact about one signature, and it is the reason a route has no
# order method to call in the first place.
# --------------------------------------------------------------------------


def test_the_broker_dependency_is_typed_as_the_read_half() -> None:
    """``BrokerAccount``, not ``AlpacaBroker`` and not ``BrokerExecution``."""
    assert (
        inspect.signature(broker_for_account).return_annotation is BrokerAccount
    )
