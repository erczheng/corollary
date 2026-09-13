"""Dependency injection: the broker, the provider, the session, and the book.

Four things live here, and three of them are boundaries rather than plumbing.

**The account-mode boundary.** Every account-scoped route takes
``?account=paper|cash``, defaulting to ``paper`` because rule 5 says every
cold start comes up in Paper. If ``cash`` is asked for and the live keys are
absent, the request **fails with 409 and a stated reason**. It never falls
back. Serving paper's book while the header says Cash misreports real money,
and it does so in the one direction nobody checks -- the numbers look
plausible because they are somebody's real numbers.

**The broker type.** Every route signature depends on
:class:`~corollary.engine.execution.interface.BrokerAccount`, the read half of
the vendor surface. ``BrokerExecution`` does not exist until Phase 6, so
``submit_order`` is not a method any route can reach. That is what makes
CLAUDE.md rule 1 structural rather than disciplinary, and it is why
:func:`broker_for_account` is annotated with the ABC and not with
``AlpacaBroker``.

**The vendor SDK is not imported here, or anywhere outside the two files
CLAUDE.md names.** This module constructs ``AlpacaBroker`` and
``AlpacaProvider`` -- our classes -- and touches nothing from ``alpaca``.

**Construction is lazy, and that is deliberate rather than lazy.** The
registry is built and cached on ``app.state`` at startup, but the broker and
the provider are constructed on first use. A ``uvicorn --reload`` server that
refused to start because a credential was unset would take the whole terminal
offline -- including ``/api/health`` and the engine routes, neither of which
needs a broker -- for a condition that affects four endpoints. Missing
credentials surface where they are used, as a stated condition.
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Annotated, Any, Final

from fastapi import Depends, Query, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.api.schemas import AccountMode
from corollary.data.providers.alpaca import (
    ALPACA_LIVE_KEY_ENV,
    ALPACA_LIVE_SECRET_ENV,
    AlpacaCredentials,
    AlpacaProvider,
)
from corollary.data.providers.interface import MarketDataProvider
from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.engine.execution.interface import BrokerAccount

__all__ = [
    "AccountMode",
    "AccountModeDep",
    "ApiError",
    "BrokerDep",
    "LIVE_CREDENTIAL_ENV_VARS",
    "ProviderDep",
    "ServiceRegistry",
    "SessionDep",
    "account_mode",
    "broker_for_account",
    "db_session",
    "market_data",
    "missing_live_credentials",
    "service_registry",
]

logger = logging.getLogger(__name__)

#: Both halves of the live pair. Named as a pair because the 409 must name
#: both: reporting only the one that happens to be checked first sends
#: somebody to set one variable and try again.
LIVE_CREDENTIAL_ENV_VARS: Final[tuple[str, str]] = (
    ALPACA_LIVE_KEY_ENV,
    ALPACA_LIVE_SECRET_ENV,
)


# --------------------------------------------------------------------------
# The stated-condition error
# --------------------------------------------------------------------------


class ApiError(Exception):
    """A condition the API states rather than crashing on.

    Rendered into the shared error envelope by the handler in ``api/app.py``,
    so ``api.ts`` has one shape to parse for every non-2xx.

    Not a ``fastapi.HTTPException``: that renders ``{"detail": ...}``, a
    second shape, and the whole value of the envelope is that there is only
    one. :attr:`code` is what a client branches on; :attr:`message` is for a
    human and is scrubbed before it is rendered.
    """

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def missing_live_credentials(env: Mapping[str, str]) -> tuple[str, ...]:
    """Which of the two live variables are unset or blank, in declared order.

    Blank counts as absent. ``ALPACA_LIVE_API_KEY=`` in a ``.env`` is a
    variable somebody meant to fill in, and treating an empty string as a
    credential produces a 401 from the vendor instead of a 409 from here --
    the same outcome, diagnosed three layers further away.
    """
    return tuple(
        name for name in LIVE_CREDENTIAL_ENV_VARS if not (env.get(name) or "").strip()
    )


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

BrokerFactory = Callable[[], BrokerAccount]
ProviderFactory = Callable[[], MarketDataProvider]


class ServiceRegistry:
    """The brokers and the provider, built at most once each and closed once.

    A mode with no factory is a mode with no credentials. That is the whole
    representation of "Cash is unavailable" -- there is no cash broker object
    holding blank keys, so there is nothing for a later change to accidentally
    call.

    :attr:`missing_live_credentials` is carried alongside so the 409 can name
    the variables. The constructor refuses the inconsistent combination rather
    than trusting two arguments to agree.
    """

    def __init__(
        self,
        *,
        brokers: Mapping[AccountMode, BrokerFactory],
        provider: ProviderFactory,
        missing_live_credentials: Sequence[str] = (),
    ) -> None:
        self._factories: dict[AccountMode, BrokerFactory] = dict(brokers)
        self._provider_factory = provider
        self.missing_live_credentials: tuple[str, ...] = tuple(missing_live_credentials)
        if (AccountMode.CASH in self._factories) and self.missing_live_credentials:
            raise ValueError(
                "a cash broker was supplied alongside missing live credentials "
                f"{self.missing_live_credentials!r}; one of the two is wrong, and "
                "guessing which would mean guessing whose money is at stake"
            )
        self._brokers: dict[AccountMode, BrokerAccount] = {}
        self._provider: MarketDataProvider | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServiceRegistry":
        """Build from the process environment.

        Paper always exists as a factory -- whether its credentials are set is
        discovered when it is first used, which keeps a missing paper key from
        stopping the server booting. Cash exists only when both live variables
        are set.
        """
        import os

        source: Mapping[str, str] = os.environ if env is None else env
        missing = missing_live_credentials(source)

        brokers: dict[AccountMode, BrokerFactory] = {
            AccountMode.PAPER: lambda: AlpacaBroker.from_env(source),
        }
        if not missing:
            brokers[AccountMode.CASH] = lambda: AlpacaBroker(
                credentials=AlpacaCredentials.live_from_env(source)
            )
        return cls(
            brokers=brokers,
            provider=lambda: AlpacaProvider.from_env(source),
            missing_live_credentials=missing,
        )

    def broker(self, mode: AccountMode) -> BrokerAccount:
        """The broker for one book, built on first use and cached after.

        Raises :class:`ApiError` with 409 when the book has no credentials.
        **Never substitutes another book.**
        """
        factory = self._factories.get(mode)
        if factory is None:
            raise self._unavailable(mode)
        cached = self._brokers.get(mode)
        if cached is None:
            cached = self._brokers[mode] = factory()
        return cached

    def _unavailable(self, mode: AccountMode) -> ApiError:
        message = (
            f"The {mode.value} account is not configured: "
            f"{' and '.join(LIVE_CREDENTIAL_ENV_VARS)} are both required and "
            f"{self._absent_phrase()}. Paper was not substituted -- serving "
            "one book under the other's name misreports which money moved."
        )
        logger.warning(
            "refused an account-scoped request: %s",
            message,
            extra={
                "event": "account_unavailable",
                "rule": "a book with no credentials is refused, never substituted",
                "account": mode.value,
                "missing": list(self.missing_live_credentials),
            },
        )
        return ApiError(status_code=409, code="account_unavailable", message=message)

    def _absent_phrase(self) -> str:
        """Which of the pair is missing, said once.

        The message has to stay inside :data:`~corollary.wire.ERROR_BODY_MAX`,
        because it is scrubbed through ``vendor_detail`` on the way out like
        every other error body. Repeating both variable names -- once as
        "required", once as "missing" -- put the earlier wording at 279 of
        300, close enough that a reworded sentence would have truncated the
        half that says paper was *not* substituted, while the half a test
        asserts on sat safely at the front.
        """
        missing = self.missing_live_credentials
        if not missing:
            return "neither is configured for it"
        if set(missing) == set(LIVE_CREDENTIAL_ENV_VARS):
            return "neither is set"
        return f"{' and '.join(missing)} is not set"

    @property
    def provider(self) -> MarketDataProvider:
        """The one market-data provider, built on first use and cached after.

        One instance, so the 200/min budget for ``data.alpaca.markets`` is
        counted once. Two providers in one process would each believe they
        held the whole bucket.
        """
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    async def aclose(self) -> None:
        """Close whatever was actually built. Called from the lifespan.

        Every close is attempted even if one raises: an HTTP client left open
        on shutdown is a warning, and skipping the rest because of it turns
        one warning into several.
        """
        built: list[Any] = list(self._brokers.values())
        if self._provider is not None:
            built.append(self._provider)
        self._brokers.clear()
        self._provider = None
        for service in built:
            close = getattr(service, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                logger.exception(
                    "failed to close %s on shutdown", type(service).__name__
                )


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def service_registry(request: Request) -> ServiceRegistry:
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise ApiError(
            status_code=503,
            code="not_started",
            message=(
                "The service registry was never built. The app was started "
                "without its lifespan -- in production that is a process "
                "manager problem, in a test it is a missing "
                "`with TestClient(app)`."
            ),
        )
    assert isinstance(registry, ServiceRegistry)
    return registry


def account_mode(
    account: Annotated[
        AccountMode,
        Query(description="Which book to read. Defaults to paper (rule 5)."),
    ] = AccountMode.PAPER,
) -> AccountMode:
    """Which book this request is about. **Paper unless told otherwise.**

    Rule 5: every cold start comes up in Paper, and nothing persists Cash
    across a restart. A missing parameter is therefore Paper, never "whatever
    was selected last".
    """
    return account


def broker_for_account(
    mode: Annotated[AccountMode, Depends(account_mode)],
    registry: Annotated[ServiceRegistry, Depends(service_registry)],
) -> BrokerAccount:
    """The broker for the requested book, or a 409 naming what is missing.

    Typed :class:`BrokerAccount`, never ``AlpacaBroker``: the read half of the
    vendor surface is the only half that exists, so there is no order path in
    a type a route can name.
    """
    return registry.broker(mode)


def market_data(
    registry: Annotated[ServiceRegistry, Depends(service_registry)],
) -> MarketDataProvider:
    """The market-data provider. Account-independent -- quotes are quotes."""
    return registry.provider


def db_session(request: Request) -> Iterator[Session]:
    """A session on the app's engine.

    No pooling cleverness and no retry loop: the design spec chose one process
    with one writer precisely so there is nothing to contend with. The route
    commits; this only opens and closes.
    """
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        raise ApiError(
            status_code=503,
            code="not_started",
            message=(
                "No database engine is configured. The app was started without "
                "its lifespan."
            ),
        )
    assert isinstance(engine, Engine)
    with Session(engine) as session:
        yield session


AccountModeDep = Annotated[AccountMode, Depends(account_mode)]
BrokerDep = Annotated[BrokerAccount, Depends(broker_for_account)]
ProviderDep = Annotated[MarketDataProvider, Depends(market_data)]
SessionDep = Annotated[Session, Depends(db_session)]
