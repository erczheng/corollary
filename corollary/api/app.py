"""The FastAPI application: lifespan, routers, and one error envelope.

``uv run uvicorn corollary.api:app`` is the whole app -- design spec decision
1, *One process*. The lifespan builds the service registry, bootstraps the
database, and constructs the one :class:`~corollary.engine.runtime.EngineRuntime`
-- which writes ``t0``, supervises rule 9's watchdog, and never clears a halt
-- then tears all three down.

**The connection half of that watchdog now runs.** The lifespan builds a
:class:`~corollary.engine.sockets.SocketSupervisor`, which holds the three
Alpaca sockets open through a market session and reports every message, open
and close into rule 9's per-socket liveness. That is the producer this file
spent two steps without: the condition was implemented, tested and unreachable
because nothing in the running process ever opened a socket.

**The heartbeat half still has no producer.** ``RiskManager`` has no body, so
that condition ships unarmed -- deliberately, since armed with nothing
heartbeating it would halt every engine ninety seconds after boot.

Two things about the sockets are worth knowing here rather than one file
away. They are held open **in session only**, from the market calendar, because
per-socket staleness cannot tell a legitimately quiet socket from a dead one
and the option feed is quiet all night. And the ``trade_updates`` socket is
never judged on silence at all, because a day with no fills is silent by
definition; losing it is detected by its close. Both decisions live in
``engine/sockets.py``, with the reasoning.

``api/routes/ws.py`` is the *browser* socket and attaches **no** wire, on
purpose: a browser tab opening says nothing about whether Alpaca is
connected, and recording activity from it would arm the switch to the wrong
signal -- a halt fired by a closed laptop lid, or a dead feed masked by a
healthy tab. The vendor sockets attach the connection condition; a
``RiskManager`` with a body attaches the heartbeat one. Wiring the supervisor
now is deliberate -- it is the part that would otherwise be written under time
pressure on the day the transport lands -- but "supervised" must not be read
as "armed".

Two things about this module are constraints rather than choices.

**``corollary.api:app`` must never stop being importable or stop answering
``GET /api/health``.** A ``uvicorn --reload`` server runs against that exact
string and a Vite dev proxy forwards ``/api`` to it, so a startup that raises
takes the whole terminal offline -- including the pages that need no broker
and no database. The lifespan therefore *states* its failures instead of
dying of them: an unmigrated database logs what to run and leaves the three
routes that need it answering 503, rather than preventing the process from
starting at all.

**Every failure comes back in one shape.** ``ApiErrorResponse`` wraps our own
stated conditions, the vendor's failures, FastAPI's 404 and 422, and an
unhandled bug. A client that has to guess between two shapes guesses wrong on
the day it matters, and the days that matter here are the ones where the
broker is down.

Rule 6 governs what may appear in that envelope. Vendor error text is scrubbed
through :func:`corollary.wire.vendor_detail` before it is rendered -- both
halves of the key pair by literal substitution, the account number by shape --
and an unhandled exception renders **no** detail at all. Alpaca echoes no
credential in an error body today; nothing about that is a guarantee, and a
WAF or proxy in front of it that reflects request headers would write one
into a body this layer quotes.
"""

import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from corollary.api.deps import AccountMode, ApiError, ServiceRegistry
from corollary.api.fanout import Fanout, quote_sink, trade_update_sink
from corollary.api.routes import (
    account_router,
    activity_router,
    engine_router,
    markets_router,
    notifications_router,
    positions_router,
    settings_router,
    ws_router,
)
from corollary.api.schemas import ApiErrorBody, ApiErrorResponse
from corollary.data.providers.alpaca import (
    ALPACA_LIVE_KEY_ENV,
    ALPACA_LIVE_SECRET_ENV,
    ALPACA_PAPER_KEY_ENV,
    ALPACA_PAPER_SECRET_ENV,
    CredentialsError,
)
from corollary.data.providers.interface import (
    FeedAccessError,
    ProviderError,
    RateLimitedError,
)
from corollary.db.seed import seed
from corollary.db.session import get_engine
from corollary.engine.execution.interface import (
    BrokerAuthError,
    BrokerError,
    BrokerRateLimitedError,
)
from corollary.engine.notify import DbNotifier, DiscordNotifier, FanoutNotifier
from corollary.engine.runtime import DISCORD_WEBHOOK_ENV, EngineRuntime, LoggingNotifier
from corollary.engine.scheduler import (
    ContextServices,
    Scheduler,
    SchedulerFactory,
    build_context_scheduler,
    no_scheduler,
)
from corollary.engine.sockets import SocketSupervisor
from corollary.wire import vendor_detail

__all__ = [
    "SECRET_ENV_VARS",
    "SocketSupervisorFactory",
    "app",
    "build_socket_supervisor",
    "create_app",
    "no_socket_supervisor",
]

logger = logging.getLogger(__name__)

#: Every environment variable whose **value** must never appear in a response
#: body. Taken from ``.env.example``; ``COROLLARY_DATABASE_URL`` is absent on
#: purpose, since it is a SQLite path rather than a credential, and redacting
#: it would turn a useful "no such file" into ``<redacted>``.
SECRET_ENV_VARS: Final[tuple[str, ...]] = (
    ALPACA_PAPER_KEY_ENV,
    ALPACA_PAPER_SECRET_ENV,
    ALPACA_LIVE_KEY_ENV,
    ALPACA_LIVE_SECRET_ENV,
    "ANTHROPIC_API_KEY",
    "MASSIVE_API_KEY",
    "FINNHUB_API_KEY",
    "FRED_API_KEY",
    "DISCORD_WEBHOOK_URL",
)

#: ``(exception, status, code)``. Registered in this order, though order does
#: not decide the outcome: Starlette resolves a handler by walking the raised
#: exception's MRO, so ``BrokerRateLimitedError`` finds its own handler rather
#: than ``BrokerError``'s. Listed most-specific-first anyway, because the
#: reader's expectation should match the runtime's.
_VENDOR_FAILURES: Final[tuple[tuple[type[Exception], int, str], ...]] = (
    # 502 rather than 401: the *upstream* rejected our credentials. A 401 to
    # the browser would say the person at the keyboard is unauthenticated,
    # which is a different problem with a different remedy.
    (BrokerAuthError, 502, "broker_auth"),
    (BrokerRateLimitedError, 429, "broker_rate_limited"),
    (BrokerError, 502, "broker_unavailable"),
    # A feed the plan does not entitle. Alpaca answers 403 "OPRA agreement is
    # not signed"; that is the upstream saying no, not the client asking
    # wrongly.
    (FeedAccessError, 502, "feed_unavailable"),
    (RateLimitedError, 429, "provider_rate_limited"),
    (ProviderError, 502, "provider_unavailable"),
    # Server-side configuration, not a client error: only the server can see
    # the environment.
    (CredentialsError, 503, "credentials_missing"),
)

_HTTP_CODES: Final[dict[int, str]] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}

_DATABASE_MESSAGE: Final = (
    "The database is unavailable. If this is a fresh checkout, run "
    "`uv run alembic upgrade head`; otherwise check that nothing else holds "
    "the SQLite file."
)


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def _environment_secrets() -> tuple[str, ...]:
    """The values of :data:`SECRET_ENV_VARS` that are actually set.

    Read per request rather than captured at startup, so a key added to the
    environment of a reloaded process is redacted from the first response
    after it, not from the next restart.
    """
    return tuple(
        value
        for name in SECRET_ENV_VARS
        if (value := (os.environ.get(name) or "").strip())
    )


def _scrub(request: Request, text: str) -> str:
    """Bound and de-identify text before it becomes a response body.

    The same treatment both vendor files give an error body, for the same
    reason and with the same helper: literal substitution of every credential
    in the environment, then the account-number shape, then a length bound.
    """
    secrets: Sequence[str] = request.app.state.secret_values()
    return vendor_detail(text, secrets=secrets)


def _envelope(status_code: int, code: str, message: str) -> JSONResponse:
    body = ApiErrorResponse(error=ApiErrorBody(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _vendor_handler(
    status_code: int, code: str
) -> Callable[[Request, Exception], Response]:
    def handle(request: Request, exc: Exception) -> Response:
        message = _scrub(request, str(exc))
        logger.warning(
            "%s answering %d: %s",
            type(exc).__name__,
            status_code,
            message,
            extra={
                "event": "api_vendor_failure",
                "rule": "a vendor failure is a stated condition, never a traceback",
                "code": code,
                "status": status_code,
                "path": request.url.path,
            },
        )
        return _envelope(status_code, code, message)

    return handle


def _api_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return _envelope(exc.status_code, exc.code, _scrub(request, exc.message))


def _database_handler(request: Request, exc: Exception) -> Response:
    """A database failure says what to run, and keeps the SQL in the log.

    ``str(SQLAlchemyError)`` carries the statement and its bound parameters.
    Those are not credentials, but they are not something anyone asked to see
    in a browser either, and the useful half fits in one sentence.
    """
    logger.exception(
        "database failure answering %s",
        request.url.path,
        extra={
            "event": "api_database_failure",
            "rule": "the database is a stated condition, never a traceback",
            "path": request.url.path,
        },
    )
    return _envelope(503, "database_unavailable", _DATABASE_MESSAGE)


def _validation_handler(request: Request, exc: Exception) -> Response:
    """422, built from field locations and messages -- never from the input.

    ``exc.errors()`` carries the offending ``input`` alongside each error.
    Echoing it back is how a mistyped request that happened to contain a
    credential ends up quoted in a response.
    """
    assert isinstance(exc, RequestValidationError)
    detail = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )
    return _envelope(422, "invalid_request", detail or "the request did not validate")


def _http_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    return _envelope(exc.status_code, code, _scrub(request, str(exc.detail)))


def _unhandled_handler(request: Request, exc: Exception) -> Response:
    """A bug gets the envelope and nothing else.

    No exception type, no message, no traceback. All three go to the log,
    where rule 6 already governs what may appear; a body is a surface nobody
    audited.
    """
    logger.exception(
        "unhandled exception answering %s",
        request.url.path,
        extra={
            "event": "api_unhandled_exception",
            "path": request.url.path,
        },
    )
    return _envelope(
        500,
        "internal_error",
        "The request failed. The reason is in the server log.",
    )


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------


def _bootstrap_database(db_engine: Engine) -> None:
    """Seed the default rows. ``t0`` belongs to ``EngineRuntime.start``.

    ``seed`` is idempotent by design -- Settings is server-backed and
    re-seeding must never undo a change a human made -- so running it on every
    startup is the intended use.

    A failure here is logged and swallowed **deliberately**. The most likely
    cause is a database that has never been migrated, and the right outcome is
    a terminal that boots, answers ``/api/health``, and says 503 on the three
    routes that need a table. Raising instead would mean a missing Alembic run
    takes down the dev server for every page.
    """
    try:
        with Session(db_engine) as session:
            inserted = seed(session)
            session.commit()
        if inserted:
            logger.info(
                "seeded %d default rows",
                inserted,
                extra={"event": "db_seeded", "rows": inserted},
            )
    except SQLAlchemyError:
        logger.exception(
            "could not bootstrap the database; run `uv run alembic upgrade head`",
            extra={
                "event": "db_bootstrap_failed",
                "rule": (
                    "a missing migration degrades three routes, it does not "
                    "stop the process"
                ),
            },
        )


#: How the lifespan gets its vendor sockets. A factory rather than an object,
#: because the supervisor needs the :class:`EngineRuntime` the lifespan
#: builds, and a factory rather than a boolean because a test has to be able
#: to hand in a scripted connection and a wound clock.
SocketSupervisorFactory = Callable[
    [EngineRuntime, Fanout, ServiceRegistry], SocketSupervisor | None
]


def build_socket_supervisor(
    runtime: EngineRuntime, fanout: Fanout, registry: ServiceRegistry
) -> SocketSupervisor:
    """The shipped wiring: three Alpaca sockets, the Paper book, one fan-out.

    This is the composition itself, and the three lines of it are the three
    decisions:

    **The Paper book, always** (rule 5). Every cold start comes up in Paper,
    and a socket that read whichever account the UI happened to have selected
    would stream one book's positions under the other's name. Switching to
    Cash is an explicit, confirmed act and it does not happen here.

    **The fan-out's sinks, not a second hub.** One quote reaches every
    connected browser through the one :class:`Fanout` this app owns; a second
    would let two tabs disagree about a price.

    **The runtime as the activity recorder.** The sockets report their
    messages, opens and closes straight into rule 9's watchdog, per socket.
    Nothing in this call can halt or resume anything -- the supervisor's whole
    authority over the switch is to say which feeds it is holding open.
    """
    return SocketSupervisor(
        runtime=runtime,
        broker=lambda: registry.broker(AccountMode.PAPER),
        on_quote=quote_sink(fanout),
        on_update=trade_update_sink(fanout),
    )


def no_socket_supervisor(
    runtime: EngineRuntime, fanout: Fanout, registry: ServiceRegistry
) -> None:
    """No vendor sockets at all. What a route test's app is built with.

    Explicit rather than a flag, and explicit rather than relying on the
    environment: a fixture that opens a websocket to Alpaca whenever the
    developer happens to have exported their keys is not a fixture. Route
    suites take this; the composition root has its own suite.
    """
    return None


def create_app(
    *,
    registry: ServiceRegistry | None = None,
    db_engine: Engine | None = None,
    secrets: Sequence[str] | None = None,
    streams: SocketSupervisorFactory = no_socket_supervisor,
    discord: bool = False,
    scheduler: SchedulerFactory = no_scheduler,
) -> FastAPI:
    """Build the application.

    Every argument exists so a test can supply its own: a registry holding a
    fake ``BrokerAccount``, a SQLite file under ``tmp_path``, and a known
    secret to prove redaction. Left to their defaults, the registry is built
    from the process environment and the engine is the process-wide one.

    ``secrets`` overrides which values are scrubbed out of error bodies. The
    default reads :data:`SECRET_ENV_VARS` from the environment on every
    failure.

    ``streams`` decides which vendor sockets the lifespan runs, and it
    **defaults to none**. The shipped app opts in --
    ``app = create_app(streams=build_socket_supervisor)`` at the bottom of
    this module -- so production behaviour is unchanged and every other caller
    is safe by construction rather than by timing.

    It was the other way round, and the safety of the fifteen test apps built
    from this rested on three coincidences: the supervisor's loop sleeps
    before its first tick, no test holds a lifespan open for five seconds, and
    no ``tests/conftest.py`` scrubs the environment. Break any one of them --
    ``pytest --pdb`` sitting at a breakpoint is enough -- and a test run reads
    the real paper credentials out of ``os.environ`` and opens
    ``wss://paper-api.alpaca.markets/stream``. Alpaca permits **one** trading
    stream per account, so a suite run during market hours would take the slot
    from the running engine, whose client then records a close and halts
    itself correctly, caused by a test.

    ``discord`` decides whether notifications routed to Discord are
    **posted**, and it defaults to no for the same reason ``streams`` does:
    the webhook URL is in the developer's environment, and a test app -- or a
    ``--reload`` loop halting on every save -- must not page the owner's
    channel by forgetting an argument. Off does not mean silent: the Discord
    sink is still installed, disabled, and records a ``dropped``
    ``notification_delivery`` row naming why for every alert routed to it, so
    "routed to Discord" and "never sent" are both on the record. The bell's
    database sink is installed either way.

    ``scheduler`` decides which context jobs the lifespan runs (Phase 3
    decision 1), and it **defaults to none** for the reason ``streams`` does:
    the jobs call Finnhub, FRED, Massive and StockTwits with keys from the
    developer's environment, and a test app must not do that by forgetting an
    argument. Both shipped apps opt in. The factory is handed a
    :class:`ContextServices` and the redaction callable -- never the
    :class:`EngineRuntime` -- so no job can reach rule 9's switch.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "registry", None) is None:
            app.state.registry = ServiceRegistry.from_env()
        if getattr(app.state, "db_engine", None) is None:
            app.state.db_engine = get_engine()
        _bootstrap_database(app.state.db_engine)
        # Design spec decision 1: one process. The engine runtime is a member
        # of this app rather than a service beside it, so rule 9's switch runs
        # wherever the API runs and there is one answer to "is it halted".
        # ``start`` writes ``t0`` on the first ever start and never touches
        # ``halted``; ``supervise`` puts the watchdog on its own task.
        #
        # The watchdog's producers are the three vendor sockets, started
        # below: every message, stream open and stream close they see is
        # recorded against it per socket. An app built with
        # ``streams=no_socket_supervisor`` -- which is the default, and what
        # every route test takes -- has none of them, so its watchdog ticks,
        # evaluates and finds nothing. That is rule 9 with nothing to judge,
        # not rule 9 disarmed: the conditions are the same code either way.
        db_engine = app.state.db_engine
        # Rule 9's voice (Phase 3 decision 14): a log line, the bell's row,
        # and the Discord webhook, fanned out so no one sink can silence the
        # others. The URL is read from the process environment -- never from
        # `.env`, which the launcher loads -- and handed to the one class that
        # uses it. The runtime separately reads it for *presence* only.
        # Started before the watchdog, so the first halt it could raise finds
        # a running delivery task.
        #
        # Discord is optional; the engine is not. Construction is guarded as
        # well as ``start()``: the sink already treats a malformed or
        # non-https URL as unavailable without raising, and this catches
        # whatever else a constructor might raise, so no setting of an
        # optional channel can abort the lifespan that carries rule 9. The
        # fallback is a disabled sink with no URL -- it still records a
        # ``dropped`` row naming why for every alert routed to Discord.
        try:
            discord_sink = DiscordNotifier(
                webhook_url=os.environ.get(DISCORD_WEBHOOK_ENV),
                session_factory=lambda: Session(db_engine),
                disabled_reason=(
                    None
                    if discord
                    else (
                        "this app was built without Discord delivery "
                        "(create_app(discord=False), e.g. dev_app)"
                    )
                ),
            )
        except Exception as exc:
            # Class name only: the message may quote the URL (rule 6).
            logger.error(
                "the Discord sink could not be built; alerts routed there "
                "will be recorded as dropped",
                extra={
                    "event": "notification_discord_not_built",
                    "variable": DISCORD_WEBHOOK_ENV,
                    "error_type": type(exc).__name__,
                },
            )
            discord_sink = DiscordNotifier(
                webhook_url=None,
                session_factory=lambda: Session(db_engine),
                disabled_reason=(
                    f"the Discord sink could not be built ({type(exc).__name__}); "
                    f"check {DISCORD_WEBHOOK_ENV}"
                ),
            )
        try:
            discord_sink.start()
        except Exception as exc:
            # Stated, never fatal: the sink then records every alert routed
            # to it as dropped, which is the trace this failure leaves.
            logger.error(
                "the Discord delivery task did not start; alerts routed there "
                "will be recorded as dropped",
                extra={
                    "event": "notification_discord_not_started",
                    "error_type": type(exc).__name__,
                },
            )
        runtime = EngineRuntime(
            session_factory=lambda: Session(db_engine),
            notifier=FanoutNotifier(
                [
                    LoggingNotifier(),
                    DbNotifier(session_factory=lambda: Session(db_engine)),
                    discord_sink,
                ]
            ),
        )
        app.state.engine_runtime = runtime
        runtime.start()
        runtime.supervise()
        # Rule 9's producers. Started after the watchdog is already
        # supervising, so the first socket to open reports into a running
        # switch rather than into one that starts a moment later.
        #
        # A failure here is stated, never fatal -- the same standing the
        # database bootstrap has, for the same reason: `corollary.api:app`
        # must never stop answering `GET /api/health`, and a vendor socket
        # that cannot be built is not a reason for the terminal to be offline.
        supervisor: SocketSupervisor | None = None
        try:
            supervisor = streams(runtime, app.state.fanout, app.state.registry)
            if supervisor is not None:
                supervisor.start()
        except Exception:
            logger.exception(
                "the vendor sockets did not start; the app keeps serving",
                extra={
                    "event": "sockets_not_started",
                    "rule": (
                        "a vendor socket that cannot start leaves the API "
                        "answering; rule 9 then halts on the silence"
                    ),
                },
            )
            supervisor = None
        app.state.socket_supervisor = supervisor
        # The context jobs (Phase 3 decision 1). Built from ContextServices,
        # which has no runtime in it: news, calendar and macro feeds are not
        # rule 9 producers, and a job cannot feed a switch it was never
        # handed. Stated-never-fatal like the sockets above -- and never a
        # halt either, since a feed that cannot be scheduled is a stale page,
        # not a lost connection.
        context_scheduler: Scheduler | None = None
        try:
            context_scheduler = scheduler(
                ContextServices(session_factory=lambda: Session(db_engine)),
                app.state.secret_values,
            )
            if context_scheduler is not None:
                context_scheduler.start()
        except Exception as exc:
            logger.error(
                "the context scheduler did not start; the app keeps serving "
                "and every context feed will read stale",
                extra={
                    "event": "context_scheduler_not_started",
                    "rule": (
                        "a context scheduler that cannot start leaves the API "
                        "answering and the engine's halt state untouched"
                    ),
                    "error_type": type(exc).__name__,
                },
            )
            context_scheduler = None
        app.state.scheduler = context_scheduler
        try:
            yield
        finally:
            # The context jobs first: they write through the database and,
            # from later steps, the registry's providers, both closed below.
            # ``aclose`` never raises -- a job task that died was logged when
            # it died -- so it cannot skip the steps after it, and the rule 9
            # alerts still queued in the Discord sink keep their grace period.
            if context_scheduler is not None:
                await context_scheduler.aclose()
            # Then the sockets, ahead of the runtime: they report into the
            # watchdog, and a socket
            # still reading while the supervisor it reports to is gone is a
            # message recorded against a switch nobody is watching.
            if supervisor is not None:
                await supervisor.aclose()
            await runtime.aclose()
            # After the runtime, which is what emits: nothing can enqueue a
            # new alert once the watchdog has stopped, and whatever is still
            # queued gets a bounded grace and then a `dropped` row.
            await discord_sink.aclose()
            await app.state.registry.aclose()

    app = FastAPI(
        title="Corollary",
        summary="Single-user equity options trading terminal.",
        lifespan=lifespan,
    )
    app.state.registry = registry
    app.state.db_engine = db_engine
    # Which socket wiring this app was built with, recorded so that the
    # *shipped* choice is assertable without starting a socket. The default
    # is the safe one, which means the production opt-in is the line that can
    # go missing -- and an app serving every route with rule 9's producers
    # quietly absent is exactly the failure this records.
    app.state.socket_factory = streams
    # Recorded for the same reason: the shipped opt-in to posting on Discord
    # is the line that can go missing, and a rule 9 alert that quietly stops
    # reaching the phone is exactly what the delivery table exists to catch.
    app.state.discord_delivery = discord
    # And for the same reason again: the shipped opt-in to the context jobs
    # is the line that can go missing, and a page whose every feed reads
    # *stale* because nothing ever polled it is the failure this records.
    app.state.scheduler_factory = scheduler
    # Replaced in the lifespan; ``None`` until then, and ``None`` there too
    # when the app was built with no scheduler or it could not start. A later
    # step's routes read ``status()`` off it for *stale since HH:MM ET*.
    app.state.scheduler = None
    # Replaced in the lifespan. Present so that a route reading it outside a
    # running app gets ``None`` rather than an AttributeError from Starlette's
    # State, which is a confusing way to learn the app was never started.
    app.state.engine_runtime = None
    # Replaced in the lifespan, for the same reason, and ``None`` there too
    # when the app was built with no sockets or when they could not start.
    app.state.socket_supervisor = None
    # One fan-out per app, built here rather than in the lifespan so that it
    # exists for an app nobody started -- and per app rather than per module,
    # so two tests cannot share one hub. It is the single place a quote
    # reaches every connected browser: a second one would let two clients
    # disagree about a price, which is the invariant CLAUDE.md protects on the
    # frontend's ``underlyings`` map. The vendor sockets publish into it;
    # ``api/routes/ws.py`` reads it and nothing else writes.
    app.state.fanout = Fanout()
    app.state.secret_values = (
        _environment_secrets if secrets is None else (lambda: tuple(secrets))
    )

    for exception, status_code, code in _VENDOR_FAILURES:
        app.add_exception_handler(exception, _vendor_handler(status_code, code))
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(SQLAlchemyError, _database_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(Exception, _unhandled_handler)

    @app.get("/api/health", summary="Liveness")
    def health() -> dict[str, str]:
        """Unchanged since Phase 1, and it must stay that way.

        No dependencies: not the registry, not the database, not the broker.
        Whatever else is broken, this answers.
        """
        return {"status": "ok"}

    # Every router carries its own ``/api/...`` prefix, so order here does
    # not decide a path. Listed alphabetically for the reader's sake.
    app.include_router(account_router)
    app.include_router(activity_router)
    app.include_router(engine_router)
    app.include_router(markets_router)
    app.include_router(notifications_router)
    app.include_router(positions_router)
    app.include_router(settings_router)
    app.include_router(ws_router)

    return app


#: The shipped app. ``uv run python -m uvicorn corollary.api:app`` starts
#: this one, and the opt-in to the vendor sockets is here rather than in
#: :func:`create_app`'s default so that nothing built for a test can open a
#: real connection by forgetting an argument.
app = create_app(
    streams=build_socket_supervisor,
    discord=True,
    scheduler=build_context_scheduler,
)

#: The same app with **no vendor sockets**, for ``--reload``.
#:
#: Not a convenience either. Uvicorn's reloader starts a fresh child on every
#: save while the old one is still holding three websockets, and Alpaca
#: answers the surplus trading-stream connection with **406** -- which is in
#: ``FATAL_STREAM_CODES``, so it surfaces as a ``StreamProtocolError``, and
#: the next watchdog tick halts the engine. On Windows the reloader
#: terminates the child rather than letting the lifespan's ``aclose()`` run,
#: so the old connection is reaped on the vendor's schedule and the new child
#: races it.
#:
#: The halt is the safe direction and the right outcome for a 406 --
#: rule 9's condition genuinely fired. The cost is the one CLAUDE.md names: a
#: halt log full of self-inflicted entries is a log nobody reads on the
#: morning that matters, and a developer resuming reflexively fifteen times
#: an afternoon is precisely the trained reflex rule 9 depends on not
#: existing. So the *editing* loop runs without the producers rather than the
#: producers learning to forgive a close, which is the one fix that must not
#: be made: "a close within N seconds of a start is ours" is a window in
#: which a genuine close is discarded.
#:
#: Use ``uv run python -m uvicorn corollary.api:dev_app --reload --env-file
#: .env`` for frontend and route work, and ``corollary.api:app`` -- no
#: ``--reload`` -- whenever the sockets or rule 9 are what is being worked
#: on. ``--env-file`` is still not optional: nothing under ``corollary/``
#: reads ``.env``, so without it every broker route answers 503.
#:
#: **No Discord either** (``discord=False``, the default). The bell works --
#: its database sink is installed -- so frontend work against the bell is
#: real. But ``--reload`` is exactly the loop that produces self-inflicted
#: halts, and each one routed to Discord would page the owner's channel: the
#: alert-fatigue version of the resume reflex above. The disabled sink
#: records a ``dropped`` delivery row per alert naming why, so the choice is
#: visible in the data rather than a silent gap.
#:
#: **The context scheduler, though, runs here** (Phase 3 decision 1). The
#: sockets are out because a reload's surplus connection earns a 406 and a
#: halt; context jobs are REST polls that by construction cannot halt
#: anything, so that argument does not reach them -- and without them every
#: News, calendar and macro panel would read *stale* for the whole of the
#: frontend work those panels need real data for. The reload loop's cost is
#: bounded by the scheduler itself: a job's first run is its first slot
#: *after* startup, never an immediate one, so saving a file does not re-fire
#: every poll. What a save does do is hand the new process a fresh
#: :func:`~corollary.ratelimit.default_limiter`, which is why no job may rely
#: on its bucket alone to stay under a vendor's ceiling across restarts.
dev_app = create_app(scheduler=build_context_scheduler)
