"""Fixtures for the API tests. No network, no MCP, no live credentials.

Three things are faked and nothing else:

**The broker** is :class:`RecordedBroker`, a hand-written
:class:`~corollary.engine.execution.interface.BrokerAccount` that answers from
the responses recorded under ``tests/fixtures/alpaca/``. It parses those
recordings with the real ``AlpacaBroker`` over an ``httpx.MockTransport``
rather than hand-typing the domain objects: hand-typed doubles drift from the
fixtures silently, and the point of recording real responses was to stop
exactly that. No socket is opened -- the transport serves the fixture file's
own bytes.

**The database** is a file-backed SQLite under ``tmp_path``. Not ``:memory:``:
``PRAGMA journal_mode=WAL`` is a no-op on a memory database, and the app's
engine sets it on every connect.

**The environment** is a plain mapping passed into the registry, so a test
decides whether the live keys are present without touching ``os.environ`` --
and no test needs a real key for either account.
"""

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from corollary.api.app import create_app
from corollary.api.deps import AccountMode, ServiceRegistry
from corollary.api.routes.markets import MarketCaches
from corollary.api.routes.markets import router as markets_router
from corollary.data.providers.alpaca import (
    AlpacaCredentials,
    AlpacaProvider,
    FeedConfig,
)
from corollary.db.models import Base
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.engine.execution.interface import (
    Account,
    Activity,
    ActivityCategory,
    BrokerAccount,
    BrokerPosition,
    Order,
    OrderQueryStatus,
    PortfolioHistory,
)
from corollary.ratelimit import ALPACA_PAPER_TRADING_HOST, HostRateLimiter

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "alpaca"

#: Obviously fake, and pointed at the paper host. Rule 6: no key material in
#: tests or fixtures. Rule 5: nothing here constructs live credentials.
TEST_CREDENTIALS = AlpacaCredentials(
    key_id="PKTESTTESTTESTTEST",
    secret_key="not-a-real-secret",
    trading_base_url=f"https://{ALPACA_PAPER_TRADING_HOST}",
    is_paper=True,
)

#: Longest prefix wins, so ``/v2/account/activities`` is not served the
#: account fixture. Anything unrouted is a test failure rather than a 404 --
#: an unrouted path means the broker built a URL nobody predicted.
_FIXTURE_FOR_PATH: Mapping[str, str] = {
    "/v2/account/activities": "activities_mixed",
    "/v2/account/portfolio/history": "portfolio_history",
    "/v2/account": "account",
    "/v2/positions": "positions",
    "/v2/orders": "orders_nested",
}


def _fixture_body_bytes(name: str) -> bytes:
    """The exact bytes of a fixture's ``body``, sliced out of the file.

    Not ``json.dumps(json.load(...))``: the portfolio-history endpoint sends
    money as bare JSON numbers, so a re-serialised replay would put the whole
    equity curve through a double on the way in and both sides of every
    assertion would then read the same double back out.
    """
    text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    marker = '"body":'
    start = text.index(marker) + len(marker)
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end].encode("utf-8")


def _fixture_status(name: str) -> int:
    raw = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return int(raw["status_code"])


def _serve(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    for prefix in sorted(_FIXTURE_FOR_PATH, key=len, reverse=True):
        if path.startswith(prefix):
            name = _FIXTURE_FOR_PATH[prefix]
            return httpx.Response(
                status_code=_fixture_status(name),
                content=_fixture_body_bytes(name),
                headers={"content-type": "application/json"},
                request=request,
            )
    raise AssertionError(f"no fixture routed for {request.method} {request.url}")


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(
        f"a replay test waited {seconds}s on the rate limiter; these tests are "
        "about routes, not budgets"
    )


class RecordedBroker(BrokerAccount):
    """A ``BrokerAccount`` over recorded responses, with an injectable failure.

    :attr:`label` is what a test asserts on to prove *which* book a route
    reached. That matters more than any figure on these responses: serving
    paper's positions while the UI says Cash misreports real money, and an
    identity check is the only assertion that catches it.

    :attr:`fail_with` makes any call raise, which is how the vendor-error
    handlers are exercised without a network that can be made to fail.
    """

    def __init__(self, *, label: str, fail_with: BaseException | None = None) -> None:
        self.label = label
        self.fail_with = fail_with
        self.calls: list[str] = []
        self.closed = False
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(_serve))
        self._inner = AlpacaBroker(
            credentials=TEST_CREDENTIALS,
            client=self._client,
            limiter=HostRateLimiter(
                requests_per_minute=10_000, clock=lambda: 0.0, sleep=_never_sleep
            ),
        )

    def _record(self, call: str) -> None:
        self.calls.append(call)
        if self.fail_with is not None:
            raise self.fail_with

    async def account(self) -> Account:
        self._record("account")
        return await self._inner.account()

    async def positions(self) -> list[BrokerPosition]:
        self._record("positions")
        return await self._inner.positions()

    async def orders(
        self,
        *,
        status: OrderQueryStatus = OrderQueryStatus.ALL,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[Order]:
        self._record("orders")
        return await self._inner.orders(
            status=status, after=after, until=until, limit=limit, symbols=symbols
        )

    async def activities(
        self,
        *,
        types: Sequence[str] | None = None,
        category: ActivityCategory | None = None,
        after: datetime | None = None,
        until: datetime | None = None,
        since_id: str | None = None,
        page_size: int | None = None,
    ) -> list[Activity]:
        self._record("activities")
        return await self._inner.activities(
            types=types,
            category=category,
            after=after,
            until=until,
            since_id=since_id,
            page_size=page_size,
        )

    async def portfolio_history(
        self, *, period: str = "1M", timeframe: str = "1D"
    ) -> PortfolioHistory:
        self._record("portfolio_history")
        return await self._inner.portfolio_history(period=period, timeframe=timeframe)

    async def aclose(self) -> None:
        self.closed = True
        await self._client.aclose()


class FakeProvider:
    """A stand-in for ``MarketDataProvider``. Nothing in sub-step A reads it.

    It exists so the lifespan has something to close, and so the closing is
    observable.

    The markets routes need a provider that actually answers, and they get
    :class:`RecordingTransport` + a real ``AlpacaProvider`` below rather than
    an extension of this. Same reasoning as :class:`RecordedBroker`: a
    hand-typed double drifts from the recordings silently, and the recordings
    are the only thing in this repository that knows what Alpaca really sends.
    """

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def unmigrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """A database file with no tables in it -- ``alembic upgrade head`` unrun."""
    engine = create_db_engine(sqlite_url(tmp_path / "unmigrated.db"))
    yield engine
    engine.dispose()


@pytest.fixture
def paper_broker() -> RecordedBroker:
    return RecordedBroker(label="paper")


@pytest.fixture
def cash_broker() -> RecordedBroker:
    return RecordedBroker(label="cash")


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def make_registry(
    paper_broker: RecordedBroker,
    cash_broker: RecordedBroker,
    provider: FakeProvider,
) -> Callable[..., ServiceRegistry]:
    """Build a registry, with or without a cash book.

    ``live_keys=False`` is the shipped state: ``ALPACA_LIVE_API_KEY`` and
    ``ALPACA_LIVE_SECRET_KEY`` are commented out in ``.env.example`` and are
    meant to be absent until Phase 7.
    """

    def build(*, live_keys: bool = False) -> ServiceRegistry:
        brokers: dict[AccountMode, Callable[[], BrokerAccount]] = {
            AccountMode.PAPER: lambda: paper_broker
        }
        missing: tuple[str, ...] = ()
        if live_keys:
            brokers[AccountMode.CASH] = lambda: cash_broker
        else:
            missing = ("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY")
        return ServiceRegistry(
            brokers=brokers,
            provider=lambda: provider,
            missing_live_credentials=missing,
        )

    return build


@pytest.fixture
def registry(make_registry: Callable[..., ServiceRegistry]) -> ServiceRegistry:
    return make_registry()


@pytest.fixture
def app(registry: ServiceRegistry, db_engine: Engine) -> FastAPI:
    return create_app(registry=registry, db_engine=db_engine)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A client inside the lifespan -- ``with`` is what runs startup and shutdown."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def clientless_app(registry: ServiceRegistry, unmigrated_engine: Engine) -> FastAPI:
    return create_app(registry=registry, db_engine=unmigrated_engine)


def probe_route(app: FastAPI, path: str, dependency: Any) -> None:
    """Mount a one-line route that returns what a dependency resolved to.

    The account-mode boundary is a *dependency*, and in sub-step A no shipped
    route consumes a broker yet -- the account, positions, activity and
    markets routes are separate dispatches. Testing the dependency through a
    probe exercises the same resolution those routes will get, including the
    409, rather than waiting for one of them to exist.
    """

    @app.get(path)
    def _probe(resolved: Any = dependency) -> dict[str, str]:
        return {"label": getattr(resolved, "label", type(resolved).__name__)}


# --------------------------------------------------------------------------
# Market data: a real provider over recorded responses
# --------------------------------------------------------------------------
#
# The markets routes are the first ones that read a ``MarketDataProvider``, so
# they need more than :class:`FakeProvider`. They get the same treatment the
# broker gets: the **real** ``AlpacaProvider``, parsing the **real** recorded
# bytes, over an ``httpx.MockTransport``. Nothing here opens a socket and
# nothing here hand-types a price.
#
# This duplicates a little of ``tests/data/providers/conftest.py`` deliberately.
# Importing another package's conftest couples two suites owned by different
# dispatches, and the duplicated part is one transport and four one-line
# routers.

#: The instant the market-data fixtures were captured, to the second. Frozen
#: rather than read from the clock: feed selection, time to expiry and the
#: derived greeks all depend on "now", so a wall-clock test rots overnight and
#: the near-expiry contract becomes an expired one next week. Same value as
#: ``RECORDED_AT`` in the provider suite, and the same reasoning as
#: ``MARKET_TODAY`` on the frontend.
MARKET_DATA_RECORDED_AT = datetime(2026, 9, 10, 19, 10, 0, tzinfo=timezone.utc)

#: The Basic plan, per CLAUDE.md. Feed names are configuration, read only
#: inside the provider -- a test may name them because it is standing in for
#: the environment, which is the one other place they are allowed to appear.
BASIC_FEEDS = FeedConfig(
    options="indicative", stock_historical="sip", stock_realtime="iex"
)

#: What a route may hand back: a fixture name, or a literal ``(status, body)``
#: for a response no recording could contain.
Served = str | tuple[int, str | bytes] | None
Route = Callable[[httpx.Request], Served]


class RecordingTransport(httpx.MockTransport):
    """A mock transport that logs what it served.

    The request log makes two otherwise-invisible properties testable: that a
    cached daily series is fetched **once** rather than on every 2s poll, and
    that a chain costs one request against each of the two rate-limit buckets
    -- ``data.alpaca.markets`` and ``paper-api.alpaca.markets`` carry separate
    200/min budgets.

    An unrouted request raises rather than 404ing, because it means the
    provider built a URL nobody predicted, which is what a replay test exists
    to catch.
    """

    def __init__(self, route: Route) -> None:
        self.requests: list[httpx.Request] = []
        self._route = route
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        served = self._route(request)
        if served is None:
            raise AssertionError(f"no fixture routed for {request.method} {request.url}")
        if isinstance(served, tuple):
            status, body = served
            return httpx.Response(
                status_code=status,
                content=body.encode("utf-8") if isinstance(body, str) else body,
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(
            status_code=_fixture_status(served),
            content=_fixture_body_bytes(served),
            headers={"content-type": "application/json"},
            request=request,
        )

    @property
    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def count_for(self, path_fragment: str) -> int:
        return sum(1 for path in self.paths() if path_fragment in path)

    def params_for(self, path_fragment: str) -> dict[str, str]:
        """Query parameters of the first request whose path contains it."""
        for request in self.requests:
            if path_fragment in request.url.path:
                return dict(request.url.params)
        raise AssertionError(f"no request touched {path_fragment!r}")


def single(name: str) -> Route:
    """Route every request to one fixture."""
    return lambda _request: name


def sequence(*names: Served) -> Route:
    """Serve the named fixtures in order, then repeat the last one.

    The recorded chain and contract pages carry a ``next_page_token`` -- they
    are real pages out of the middle of a real chain -- so a test that wants
    one page still has to answer the follow-up request. The last name must be
    a terminal page or the provider keeps asking, which is behaviour under
    test rather than something to paper over.
    """
    calls = {"n": 0}

    def choose(_request: httpx.Request) -> Served:
        index = min(calls["n"], len(names) - 1)
        calls["n"] += 1
        return names[index]

    return choose


def by_path(mapping: dict[str, Route | str]) -> Route:
    """Dispatch on a path fragment to a fixture name or a nested router.

    Insertion order decides, so the more specific fragment goes first.
    """

    def choose(request: httpx.Request) -> Served:
        for fragment, target in mapping.items():
            if fragment in str(request.url):
                return target(request) if callable(target) else target
        return None

    return choose


def chain_page(name: str) -> Route:
    """One recorded chain page, then the terminal page so the loop stops."""
    return sequence(name, "option_chain_end")


def contracts_page(name: str) -> Route:
    """One recorded contracts page, then the terminal page."""
    return sequence(name, "option_contracts_end")


def market_data_routes(
    *,
    snapshots: Route | str = "stock_snapshots",
    bars: Route | str = "stock_bars_daily",
    chain: Route | str | None = None,
    contracts: Route | str | None = None,
) -> Route:
    """The four market-data paths the markets routes reach, in one router.

    ``chain`` and ``contracts`` are absent by default so that a stocks test
    routing an options request is an ``AssertionError`` naming the URL rather
    than a silently-served fixture.
    """
    mapping: dict[str, Route | str] = {
        "/v2/stocks/snapshots": snapshots,
        "/v2/stocks/bars": bars,
    }
    if chain is not None:
        mapping["/v1beta1/options/snapshots/"] = chain
    if contracts is not None:
        mapping["/v2/options/contracts"] = contracts
    return by_path(mapping)


@pytest.fixture
def make_market_client(
    paper_broker: RecordedBroker, db_engine: Engine
) -> Iterator[Callable[..., tuple[TestClient, RecordingTransport]]]:
    """An app whose provider answers from the recordings, plus its transport.

    Separate from the ``client`` fixture rather than replacing it: five other
    test modules depend on ``client`` resolving to :class:`FakeProvider`, and
    a provider that suddenly made HTTP calls underneath them would be a change
    none of those files asked for.
    """
    stack = ExitStack()

    def build(
        route: Route, *, now: datetime = MARKET_DATA_RECORDED_AT
    ) -> tuple[TestClient, RecordingTransport]:
        transport = RecordingTransport(route)
        provider = AlpacaProvider(
            credentials=TEST_CREDENTIALS,
            feeds=BASIC_FEEDS,
            client=httpx.AsyncClient(transport=transport),
            # Generous, with a clock that never advances: these tests are
            # about routes, and the budget has its own suite.
            limiter=HostRateLimiter(
                requests_per_minute=10_000, clock=lambda: 0.0, sleep=_never_sleep
            ),
            now=lambda: now,
        )
        registry = ServiceRegistry(
            brokers={AccountMode.PAPER: lambda: paper_broker},
            provider=lambda: provider,
            missing_live_credentials=("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY"),
        )
        app = create_app(registry=registry, db_engine=db_engine)
        # One clock for the whole request: the provider's ``now`` decides feed
        # selection and time to expiry, and the routes' decides the trading
        # date, the DTE window and which bar counts as today's. Two clocks an
        # hour apart would make "asked on the 14th" mean two different things
        # in one response.
        app.state.market_caches = MarketCaches(now=lambda: now)
        # The ``include_router`` line in ``api/app.py`` belongs to whoever
        # wires the sub-steps together, and the markets dispatch does not own
        # that file. Mounting here is guarded so it becomes a no-op the moment
        # that line lands rather than registering the same paths twice.
        if not any(
            getattr(route, "path", "").startswith("/api/markets")
            for route in app.routes
        ):
            app.include_router(markets_router)
        return stack.enter_context(TestClient(app)), transport

    yield build
    stack.close()
