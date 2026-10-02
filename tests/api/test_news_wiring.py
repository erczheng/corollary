"""The news jobs in the lifespan (Phase 3 step 4, unit 4B2).

What is pinned here is the *wiring*, not the job bodies (``tests/data/news``)
or the schedules (``tests/engine/test_scheduler.py``):

* **One app state.** The jobs are handed the very ``AssetDirectoryHolder`` the
  news routes read, and the positions the watch universe uses are written to
  the very ``PositionUnderlyings`` the watch route reports -- so a route and a
  job can never disagree about either.
* **Positions reach the jobs as an in-memory read, never as anything that can
  reach a broker.** The lifespan's refresher reads the **paper** account
  (rule 5), read-only, every five minutes, maps OCC symbols to their
  underlyings, and keeps the last value on a failure; the jobs hold only a
  view of the holder it writes. The risk-marked object-graph walk below
  proves no broker, registry or runtime is reachable from what the real
  lifespan hands the jobs.
* **Missing keys never stop boot**, and a shutdown closes the scheduler before
  the providers its jobs call.

No vendor socket is opened and no network is touched: every app here is built
with the default ``streams=no_socket_supervisor`` and fake providers.
"""

import asyncio
import collections
import dataclasses
import enum
import functools
import importlib
import logging
import threading
import types
import weakref
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import partial

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from corollary.data.seeds import SeedError
from corollary.data.seeds.nport import DatabaseSeedLoader, invalidate_spdr_seed_cache
from corollary.api.app import create_app
from corollary.api.deps import (
    POSITION_UNDERLYINGS_TTL,
    PaperPositionsRefresher,
    PositionUnderlyings,
    ServiceRegistry,
    position_underlying,
)
from corollary.api.routes.markets import UNIVERSE_SYMBOLS
from corollary.api.schemas import AccountMode
from corollary.data.providers.alpaca import (
    ALPACA_LIVE_KEY_ENV,
    ALPACA_LIVE_SECRET_ENV,
    ALPACA_OPTIONS_FEED_ENV,
    ALPACA_PAPER_KEY_ENV,
    ALPACA_PAPER_SECRET_ENV,
    ALPACA_STOCK_FEED_HISTORICAL_ENV,
    ALPACA_STOCK_FEED_REALTIME_ENV,
)
from corollary.data.providers.finnhub import FINNHUB_API_KEY_ENV
from corollary.data.providers.fred import FRED_API_KEY_ENV
from corollary.data.providers.massive import MASSIVE_API_KEY_ENV
from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.engine.execution.interface import (
    BrokerAccount,
    BrokerPosition,
    PositionSide,
)
from corollary.engine.runtime import EngineRuntime
from corollary.engine.scheduler import (
    ContextServices,
    EveryInterval,
    HeldPositionUnderlyings,
    ScheduledJob,
    Scheduler,
    build_context_scheduler,
)
from tests.api.conftest import FakeProvider, RecordedBroker
from tests.engine.test_scheduler import (
    HALF_DAY_OPEN,
    FakeAlpacaContext,
    FakeClock,
    _forbidden,
    _outside_corollary,
    _reachable_corollary_modules,
    _service_modules,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # Wed 10:00 ET


def _held(symbol: str, asset_class: str = "us_option") -> BrokerPosition:
    """A SYNTHETIC position row; only the symbol matters here."""
    one = Decimal(1)
    return BrokerPosition(
        symbol=symbol,
        asset_class=asset_class,
        quantity=one,
        quantity_available=one,
        side=PositionSide.LONG,
        average_entry_price=one,
        cost_basis=one,
        market_value=one,
        current_price=one,
        lastday_price=one,
        change_today=Decimal(0),
        unrealized_pl=Decimal(0),
        unrealized_plpc=Decimal(0),
        unrealized_intraday_pl=Decimal(0),
        unrealized_intraday_plpc=Decimal(0),
        asset_marginable=True,
        exchange="",
    )


class PositionsOnly:
    """Just ``positions()`` -- all the closure may call -- with a switchable failure."""

    def __init__(self, positions: Sequence[BrokerPosition]) -> None:
        self.positions_held = list(positions)
        self.calls = 0
        self.fail_with: BaseException | None = None

    async def positions(self) -> list[BrokerPosition]:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.positions_held)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _source(
    broker: PositionsOnly, clock: Clock
) -> tuple[PaperPositionsRefresher, PositionUnderlyings]:
    holder = PositionUnderlyings()
    source = PaperPositionsRefresher(
        broker=lambda: broker,  # type: ignore[arg-type, return-value]
        holder=holder,
        clock=clock,
    )
    return source, holder


# --------------------------------------------------------------------------
# The positions callable
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "underlying"),
    [
        ("AAPL261218C00150000", "AAPL"),
        ("AAPL261218P00140000", "AAPL"),
        # An adjusted root: the news is about the company, AAPL. (Sizing never
        # reads this -- it is the watch universe's membership, nothing else.)
        ("AAPL1261218C00150000", "AAPL"),
        ("MSFT", "MSFT"),
        # Not an equity symbol: passed through, and watch_universe skips it
        # with its reason rather than this guessing.
        ("GME.WS", "GME.WS"),
    ],
)
def test_a_held_symbol_maps_to_the_underlying_its_news_is_about(
    symbol: str, underlying: str
) -> None:
    assert position_underlying(symbol) == underlying


@pytest.mark.asyncio
async def test_paper_positions_are_read_mapped_and_written_where_the_route_reads() -> None:
    broker = PositionsOnly(
        [_held("AAPL261218C00150000"), _held("AAPL261218P00140000"), _held("MSFT", "us_equity")]
    )
    clock = Clock(NOW)
    source, holder = _source(broker, clock)
    assert holder.as_of is None
    assert await source.refresh() is True
    assert holder.current() == frozenset({"AAPL", "MSFT"})
    assert holder.as_of == NOW
    # The jobs' view reads the holder and never the broker.
    view = HeldPositionUnderlyings(holder)
    assert await view() == frozenset({"AAPL", "MSFT"})
    assert broker.calls == 1


@pytest.mark.asyncio
async def test_positions_are_read_at_start_and_then_once_per_five_minutes() -> None:
    """The watch tier asks every ~14 s; the broker is asked every 5 min, by the refresher."""
    assert POSITION_UNDERLYINGS_TTL == timedelta(minutes=5)
    broker = PositionsOnly([_held("AAPL261218C00150000")])
    holder = PositionUnderlyings()
    slept: list[float] = []
    second_sleep = asyncio.Event()

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            second_sleep.set()
            await asyncio.Event().wait()  # parked until aclose cancels it

    refresher = PaperPositionsRefresher(
        broker=lambda: broker,  # type: ignore[arg-type, return-value]
        holder=holder,
        clock=Clock(NOW),
        sleep=sleep,
    )
    refresher.start()
    try:
        await asyncio.wait_for(second_sleep.wait(), timeout=5)
        assert broker.calls == 2
        assert slept == [300.0, 300.0]
        assert holder.current() == frozenset({"AAPL"})
    finally:
        await refresher.aclose()
        await refresher.aclose()  # a second close is a no-op


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_last_value_and_logs_no_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broker = PositionsOnly([_held("AAPL261218C00150000")])
    clock = Clock(NOW)
    source, holder = _source(broker, clock)
    assert await source.refresh() is True

    broker.fail_with = RuntimeError("401 for key PKSECRETSECRET")
    broker.positions_held = [_held("TSLA261218C00300000")]
    clock.now = NOW + timedelta(minutes=6)
    with caplog.at_level(logging.WARNING):
        assert await source.refresh() is False
    assert holder.current() == frozenset({"AAPL"})
    assert holder.as_of == NOW  # still says when it was last true
    said = [r for r in caplog.records if getattr(r, "event", None) == "position_underlyings_unavailable"]
    assert len(said) == 1
    assert getattr(said[0], "error_type") == "RuntimeError"
    assert getattr(said[0], "account") == AccountMode.PAPER.value
    for record in caplog.records:
        assert "PKSECRETSECRET" not in record.getMessage()
        assert "PKSECRETSECRET" not in repr(record.__dict__)

    # And the next read recovers.
    broker.fail_with = None
    clock.now = NOW + timedelta(minutes=11)
    assert await source.refresh() is True
    assert holder.current() == frozenset({"TSLA"})
    assert holder.as_of == NOW + timedelta(minutes=11)


@pytest.mark.asyncio
async def test_a_failure_before_any_read_is_no_positions_and_no_timestamp() -> None:
    broker = PositionsOnly([])
    broker.fail_with = ConnectionError("down")
    source, holder = _source(broker, Clock(NOW))
    assert await source.refresh() is False
    assert holder.current() == frozenset()
    assert holder.as_of is None  # the route states "never read", not "none held"


@pytest.mark.asyncio
async def test_a_registry_with_no_paper_keys_is_a_logged_failure_not_a_crash() -> None:
    """``registry.broker(PAPER)`` raising is caught like any failed read -- never a halt."""
    registry = ServiceRegistry.from_env({})
    holder = PositionUnderlyings()
    refresher = PaperPositionsRefresher(
        broker=lambda: registry.broker(AccountMode.PAPER), holder=holder, clock=Clock(NOW)
    )
    assert await refresher.refresh() is False
    assert holder.as_of is None
    await registry.aclose()


class _RefusingHolder(PositionUnderlyings):
    """A holder whose ``replace`` raises -- a statement outside the broker read."""

    def replace(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        raise RuntimeError("holder refused PKSECRETSECRET")


def _refresh_failures(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if getattr(r, "event", None) == "position_underlyings_refresh_failed"
    ]


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_naive_clock_is_a_logged_failure_not_a_raise(caplog: pytest.LogCaptureFixture) -> None:
    """Re-audit: ``require_aware(now)`` sat outside the ``try``."""
    broker = PositionsOnly([_held("AAPL261218C00150000")])
    source, holder = _source(broker, Clock(NOW.replace(tzinfo=None)))
    with caplog.at_level(logging.WARNING):
        assert await source.refresh() is False
    assert holder.as_of is None and holder.current() == frozenset()
    (record,) = _refresh_failures(caplog)
    assert getattr(record, "error_type") == "ValueError"
    assert getattr(record, "account") == AccountMode.PAPER.value


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_holder_that_refuses_its_input_is_a_logged_failure_not_a_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Re-audit: ``holder.replace(...)`` sat outside the ``try``; its message is never logged."""
    broker = PositionsOnly([_held("AAPL261218C00150000")])
    refresher = PaperPositionsRefresher(
        broker=lambda: broker,  # type: ignore[arg-type, return-value]
        holder=_RefusingHolder(),
        clock=Clock(NOW),
    )
    with caplog.at_level(logging.WARNING):
        assert await refresher.refresh() is False
    (record,) = _refresh_failures(caplog)
    assert getattr(record, "error_type") == "RuntimeError"
    for logged in caplog.records:
        assert "PKSECRETSECRET" not in logged.getMessage()
        assert "PKSECRETSECRET" not in repr(logged.__dict__)


@pytest.mark.risk
@pytest.mark.asyncio
async def test_the_read_loop_survives_a_refresh_that_raises(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop's own guard: a regression in ``refresh`` is a logged, stale list, never a dead task."""
    broker = PositionsOnly([_held("AAPL261218C00150000")])
    holder = PositionUnderlyings()
    second_sleep = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            second_sleep.set()
            await asyncio.Event().wait()  # parked until aclose cancels it

    refresher = PaperPositionsRefresher(
        broker=lambda: broker,  # type: ignore[arg-type, return-value]
        holder=holder,
        clock=Clock(NOW),
        sleep=sleep,
    )
    real_refresh = refresher.refresh
    calls: list[int] = []

    async def flaky_refresh() -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("regressed PKSECRETSECRET")
        return await real_refresh()

    monkeypatch.setattr(refresher, "refresh", flaky_refresh)
    with caplog.at_level(logging.WARNING):
        refresher.start()
        try:
            await asyncio.wait_for(second_sleep.wait(), timeout=5)
        finally:
            await refresher.aclose()
    assert len(calls) == 2  # the loop went round again after the raise
    assert holder.current() == frozenset({"AAPL"})
    said = [r for r in caplog.records if getattr(r, "event", None) == "position_underlyings_loop_error"]
    assert len(said) == 1
    assert getattr(said[0], "error_type") == "RuntimeError"
    for logged in caplog.records:
        assert "PKSECRETSECRET" not in logged.getMessage()
        assert "PKSECRETSECRET" not in repr(logged.__dict__)


# --------------------------------------------------------------------------
# The lifespan
# --------------------------------------------------------------------------


def _capturing(
    captured: list[ContextServices], clock: FakeClock
) -> Callable[[ContextServices, Callable[[], Sequence[str]]], Scheduler]:
    def factory(
        services: ContextServices, secrets: Callable[[], Sequence[str]]
    ) -> Scheduler:
        captured.append(services)
        return build_context_scheduler(services, secrets, clock=clock, sleep=clock.sleep)

    return factory


def _context_tasks() -> list[asyncio.Task[object]]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("context-job:") and not task.done()
    ]


async def _drive(app: object, clock: FakeClock) -> None:
    scheduler = app.state.scheduler  # type: ignore[attr-defined]
    await asyncio.wait_for(scheduler.ready(), timeout=10)
    driver = asyncio.get_running_loop().create_task(clock.drive())
    try:
        await asyncio.wait_for(clock.parked.wait(), timeout=30)
    finally:
        driver.cancel()


@pytest.mark.asyncio
async def test_the_jobs_are_handed_the_app_state_the_routes_read(
    make_registry: Callable[..., ServiceRegistry],
    paper_broker: RecordedBroker,
    cash_broker: RecordedBroker,
    db_engine: Engine,
) -> None:
    registry = make_registry(live_keys=True)  # a cash book exists, and is never read
    clock = FakeClock(NOW, stop_at=NOW)
    captured: list[ContextServices] = []
    app = create_app(registry=registry, db_engine=db_engine, scheduler=_capturing(captured, clock))
    async with app.router.lifespan_context(app):
        (services,) = captured
        assert services.assets is app.state.asset_directory
        assert services.news_store is not None
        assert services.news_store.assets is app.state.asset_directory
        assert services.markets == UNIVERSE_SYMBOLS
        assert services.seed_loader is app.state.spdr_seed_loader
        # No field of the services is a broker; positions are a holder view.
        for spec in dataclasses.fields(services):
            assert not isinstance(getattr(services, spec.name), BrokerAccount), spec.name
        assert isinstance(services.position_underlyings, HeldPositionUnderlyings)
        assert services.position_underlyings.holder is app.state.position_underlyings
        assert isinstance(app.state.position_refresher, PaperPositionsRefresher)

        # The lifespan's refresher reads the paper book once at start.
        async def first_read() -> None:
            while app.state.position_underlyings.as_of is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(first_read(), timeout=10)
        held = await services.position_underlyings()
        assert "positions" in paper_broker.calls
        assert cash_broker.calls == []
        assert frozenset(held) == app.state.position_underlyings.current()
        assert app.state.position_underlyings.as_of is not None

        # The conftest provider is market data only: no news, no asset list.
        assert services.alpaca is None
        assert services.finnhub is None
        assert services.massive is None
        # No SEC factory in a test registry, and no CUSIP lookup: the
        # ``spdr_holdings`` job skips. The seed is the database's snapshot.
        assert services.sec is None and services.cusips is None
        # No OpenFIGI factory either: the job would fall back to NoIsinResolver.
        assert services.openfigi is None
        assert isinstance(app.state.spdr_seed_loader, DatabaseSeedLoader)
        assert app.state.spdr_seed_loader.bound


class NewsAlpaca(FakeAlpacaContext):
    """The Alpaca fake a lifespan resolves as its news, asset and tradeability source."""

    closed_with_live_jobs: int | None = None

    async def aclose(self) -> None:
        self.closed_with_live_jobs = len(_context_tasks())


@pytest.mark.asyncio
async def test_the_directory_the_routes_read_is_filled_at_start_and_closed_last(
    db_engine: Engine, paper_broker: RecordedBroker
) -> None:
    """The start-up catch-up fills ``app.state.asset_directory``; the routes 503 until then.

    And shutdown closes the scheduler -- every job task done -- before the
    registry closes the provider those jobs call.
    """
    alpaca = NewsAlpaca()
    registry = ServiceRegistry(
        brokers={AccountMode.PAPER: lambda: paper_broker},
        provider=lambda: alpaca,  # type: ignore[arg-type, return-value]
    )
    clock = FakeClock(NOW, stop_at=NOW + timedelta(minutes=1), wait_for_threads=True)
    captured: list[ContextServices] = []
    app = create_app(registry=registry, db_engine=db_engine, scheduler=_capturing(captured, clock))
    assert app.state.asset_directory.current() is None
    async with app.router.lifespan_context(app):
        await _drive(app, clock)
        assert captured[0].alpaca is alpaca
        assert app.state.asset_directory.current() is not None
        assert alpaca.directory_calls == 1
        assert app.state.scheduler.status()["asset_directory"].runs == 1
    assert alpaca.closed_with_live_jobs == 0


@pytest.mark.asyncio
async def test_no_vendor_key_at_all_still_boots_with_every_news_vendor_absent(
    db_engine: Engine,
) -> None:
    """Rule: a missing key disables a feed, never the app. Nothing here reads ``.env``."""
    registry = ServiceRegistry.from_env({})
    clock = FakeClock(
        HALF_DAY_OPEN, stop_at=HALF_DAY_OPEN + timedelta(minutes=16), wait_for_threads=True
    )
    captured: list[ContextServices] = []
    app = create_app(registry=registry, db_engine=db_engine, scheduler=_capturing(captured, clock))
    async with app.router.lifespan_context(app):
        assert app.state.scheduler is not None
        (services,) = captured
        assert (services.alpaca, services.finnhub, services.massive, services.fred) == (
            None,
            None,
            None,
            None,
        )
        await _drive(app, clock)
        status = app.state.scheduler.status()
        for name in ("news_alpaca", "news_finnhub_market", "news_massive", "tradeability_cache"):
            assert status[name].skips >= 1, name
            assert status[name].runs == 0, name
            assert status[name].failures == 0, name
        assert status["asset_directory"].skips == 1  # the catch-up, with no Alpaca


@pytest.mark.asyncio
async def test_an_app_with_no_scheduler_builds_no_vendor_client_at_boot(
    paper_broker: RecordedBroker, db_engine: Engine
) -> None:
    """A route test's app runs no jobs, so it resolves no news vendor either."""
    built: list[str] = []

    def provider() -> FakeProvider:
        built.append("provider")
        return FakeProvider()

    registry = ServiceRegistry(
        brokers={AccountMode.PAPER: lambda: paper_broker},
        provider=provider,  # type: ignore[arg-type]
        massive=lambda: built.append("massive"),  # type: ignore[arg-type, func-returns-value]
    )
    app = create_app(registry=registry, db_engine=db_engine)
    async with app.router.lifespan_context(app):
        assert app.state.scheduler is None
    assert built == []


# --------------------------------------------------------------------------
# The registry's Massive client
# --------------------------------------------------------------------------


class ClosableMassive:
    def __init__(self) -> None:
        self.closed = False

    async def news_since(self, published_after: datetime) -> object:
        raise AssertionError("not called here")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_the_registry_builds_massive_once_and_closes_it(
    paper_broker: RecordedBroker,
) -> None:
    built: list[ClosableMassive] = []

    def massive() -> ClosableMassive:
        built.append(ClosableMassive())
        return built[-1]

    registry = ServiceRegistry(
        brokers={AccountMode.PAPER: lambda: paper_broker},
        provider=FakeProvider,  # type: ignore[arg-type]
        massive=massive,  # type: ignore[arg-type]
    )
    first = registry.massive_provider()
    assert first is registry.massive_provider()
    assert len(built) == 1
    await registry.aclose()
    assert built[0].closed


def test_a_massive_that_cannot_be_built_is_unavailable_not_a_crash(
    paper_broker: RecordedBroker, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> ClosableMassive:
        raise RuntimeError("bad key PKSECRETSECRET")

    registry = ServiceRegistry(
        brokers={AccountMode.PAPER: lambda: paper_broker},
        provider=FakeProvider,  # type: ignore[arg-type]
        massive=broken,  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.ERROR):
        assert registry.massive_provider() is None
        assert registry.massive_provider() is None
    said = [r for r in caplog.records if getattr(r, "event", None) == "massive_unavailable"]
    assert len(said) == 1
    assert "PKSECRETSECRET" not in repr(said[0].__dict__)
    assert ServiceRegistry.from_env({}).massive_provider() is None


class ClosableOpenFigi:
    """Stands in for :class:`OpenFigiProvider` in the registry's build-once tests."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_the_registry_builds_openfigi_once_and_closes_it(
    paper_broker: RecordedBroker,
) -> None:
    built: list[ClosableOpenFigi] = []

    def openfigi() -> ClosableOpenFigi:
        built.append(ClosableOpenFigi())
        return built[-1]

    registry = ServiceRegistry(
        brokers={AccountMode.PAPER: lambda: paper_broker},
        provider=FakeProvider,  # type: ignore[arg-type]
        openfigi=openfigi,  # type: ignore[arg-type]
    )
    first = registry.openfigi_provider()
    assert first is registry.openfigi_provider()
    assert len(built) == 1
    await registry.aclose()
    assert built[0].closed


def test_an_openfigi_that_cannot_be_built_is_unavailable_not_a_crash(
    paper_broker: RecordedBroker, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> ClosableOpenFigi:
        raise RuntimeError("bad key PKSECRETSECRET")

    registry = ServiceRegistry(
        brokers={AccountMode.PAPER: lambda: paper_broker},
        provider=FakeProvider,  # type: ignore[arg-type]
        openfigi=broken,  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.ERROR):
        assert registry.openfigi_provider() is None
        assert registry.openfigi_provider() is None
    said = [r for r in caplog.records if getattr(r, "event", None) == "openfigi_unavailable"]
    assert len(said) == 1
    assert "PKSECRETSECRET" not in repr(said[0].__dict__)


@pytest.mark.asyncio
async def test_the_registry_from_env_builds_openfigi_keyless_when_the_key_is_unset() -> None:
    from corollary.data.providers.openfigi import KEYLESS_JOBS_PER_REQUEST, OpenFigiProvider

    registry = ServiceRegistry.from_env({})
    provider = registry.openfigi_provider()
    try:
        assert isinstance(provider, OpenFigiProvider)
        assert provider.jobs_per_request == KEYLESS_JOBS_PER_REQUEST
    finally:
        await registry.aclose()


# --------------------------------------------------------------------------
# What the jobs are handed cannot reach a broker, the registry or the runtime
# --------------------------------------------------------------------------

#: Leaves: values that hold no object references worth following. Modules
#: are code, not state -- the import scan is what covers code -- and
#: following one would walk every module global. A class *is* walked when it
#: is reached as a value (its bases and non-dunder class attributes; see
#: :func:`_class_children`), and a weakref is followed to its referent while
#: it is alive (unit 4B2 re-audit: both were leaves, so a class attribute or a
#: weakref could hide the registry). The event
#: loop is shared by everything in the process: reaching the runtime's tasks
#: through it is not *holding* the runtime, and a job could as well call
#: ``asyncio.all_tasks()``. Loggers are process-global in the same way.
_LEAVES: tuple[type, ...] = (
    str,
    bytes,
    bytearray,
    int,
    float,
    complex,
    bool,
    Decimal,
    datetime,
    date,
    time,
    timedelta,
    types.ModuleType,
    enum.Enum,
    asyncio.AbstractEventLoop,
    logging.Logger,
    logging.Manager,
    threading.Thread,
)

#: Class-dict values that are code rather than state. Skipped when walking a
#: class reached as a value -- unless defined in a forbidden module, in which
#: case they are yielded so :func:`_is_forbidden` flags them.
_CLASS_CODE: tuple[type, ...] = (
    types.FunctionType,
    staticmethod,
    classmethod,
    property,
    types.MemberDescriptorType,
    types.GetSetDescriptorType,
    types.WrapperDescriptorType,
    types.MethodDescriptorType,
    types.BuiltinFunctionType,
)


def _defining_module(obj: object) -> str | None:
    """The module that *defines* a function, method, class or descriptor -- not ``builtins``."""
    if isinstance(obj, (staticmethod, classmethod)):
        obj = obj.__func__
    elif isinstance(obj, property):
        obj = obj.fget
    elif isinstance(obj, types.MethodType):
        obj = obj.__func__
    module = getattr(obj, "__module__", None)
    return module if isinstance(module, str) else None


def _in_forbidden_module(module: str | None) -> bool:
    return module is not None and any(
        module == banned or module.startswith(banned + ".") for banned in _FORBIDDEN_OBJECT_MODULES
    )


def _class_children(cls: type) -> Iterator[tuple[str, object]]:
    """A class reached as a value: its bases, and its non-dunder class attributes."""
    for base in cls.__mro__[1:]:
        if base is not object:
            yield f".__mro__[{base.__qualname__}]", base
    for name, value in list(vars(cls).items()):
        if name.startswith("__") and name.endswith("__"):
            continue
        if isinstance(value, _CLASS_CODE) and not _in_forbidden_module(_defining_module(value)):
            continue
        yield f".{name}", value

_UNSET = object()


def _children(obj: object) -> Iterator[tuple[str, object]]:
    """Every object ``obj`` holds a reference to, with how it is held."""
    if isinstance(obj, type):
        yield from _class_children(obj)
        return
    if isinstance(obj, weakref.ref):
        referent = obj()
        if referent is not None:
            yield "()", referent
    if isinstance(obj, functools.partial):
        yield ".func", obj.func
        for i, arg in enumerate(obj.args):
            yield f".args[{i}]", arg
        for key, value in obj.keywords.items():
            yield f".keywords[{key!r}]", value
    if isinstance(obj, (types.MethodType, types.BuiltinMethodType)):
        yield ".__self__", obj.__self__
    if isinstance(obj, types.MethodType):
        yield ".__func__", obj.__func__
    if isinstance(obj, types.FunctionType):
        for i, cell in enumerate(obj.__closure__ or ()):
            try:
                yield f".__closure__[{i}]", cell.cell_contents
            except ValueError:  # an empty cell
                continue
        for i, default in enumerate(obj.__defaults__ or ()):
            yield f".__defaults__[{i}]", default
        for key, value in (obj.__kwdefaults__ or {}).items():
            yield f".__kwdefaults__[{key!r}]", value
        wrapped = getattr(obj, "__wrapped__", _UNSET)
        if wrapped is not _UNSET:
            yield ".__wrapped__", wrapped
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            yield f"[{key!r}]", key
            yield f"[{key!r}]", value
    elif isinstance(obj, (list, tuple, set, frozenset, collections.deque)):
        for i, item in enumerate(list(obj)):
            yield f"[{i}]", item
    # ``object.__getattribute__``, not ``getattr``: it never falls back to a
    # class's ``__getattr__`` (SQLAlchemy's name resolvers compute through one).
    try:
        instance_dict = object.__getattribute__(obj, "__dict__")
    except (AttributeError, TypeError):
        instance_dict = None
    if isinstance(instance_dict, dict):
        for key, value in list(instance_dict.items()):
            yield f".{key}", value
    # Slots through their descriptors, never ``getattr``: a class's
    # ``__getattr__`` fallback (SQLAlchemy memoizes through one) would
    # compute a value rather than read a reference. Iterating the class
    # dict's member descriptors also sees name-mangled private slots. Only
    # classes that declare ``__slots__``: a C type's members are descriptors
    # too, and a function's include ``__globals__`` -- a module's globals are
    # code-level reach, which the import scan covers, not state a job holds.
    for cls in type(obj).__mro__:
        if "__slots__" not in vars(cls):
            continue
        for name, attr in list(vars(cls).items()):
            if not isinstance(attr, types.MemberDescriptorType):
                continue
            try:
                value = attr.__get__(obj, type(obj))
            except AttributeError:  # an unset slot
                continue
            yield f".{name}", value


#: Classes whose instances a context job must never be able to reach.
_FORBIDDEN_TYPES: tuple[type, ...] = (
    ServiceRegistry,
    AlpacaBroker,
    EngineRuntime,
    PaperPositionsRefresher,
    FastAPI,
)
#: Modules whose objects a context job must never be able to reach: the
#: broker surface, the runtime and its switch, the sockets that feed it, and
#: the whole API (whose app state holds all of those).
_FORBIDDEN_OBJECT_MODULES = (
    "corollary.engine.execution",
    "corollary.engine.runtime",
    "corollary.engine.state",
    "corollary.engine.sockets",
    "corollary.api",
)


def _is_forbidden(obj: object, identities: set[int]) -> bool:
    if id(obj) in identities or isinstance(obj, _FORBIDDEN_TYPES):
        return True
    if _in_forbidden_module(type(obj).__module__):
        return True
    # A function's type is ``builtins.function`` whatever defines it: judge
    # code by the module that defines it (unit 4B2 re-audit). A function from
    # ``corollary.api.app`` reaches the app through its ``__globals__``, which
    # the walk deliberately does not follow -- this is what catches it.
    if isinstance(obj, (types.FunctionType, types.MethodType, type, staticmethod, classmethod, property)):
        return _in_forbidden_module(_defining_module(obj))
    return False


def _code_modules(root: object) -> set[str]:
    """The defining module of every function, method and class the walk reaches.

    Fed to the import scan alongside :func:`_service_modules`'s top-level
    fields, so code nested in a partial, a closure or a default is scanned
    too -- a module's imports are what its functions' ``__globals__`` hold.
    """
    modules: set[str] = set()
    for _, obj in _walk(root):
        if isinstance(obj, (types.FunctionType, types.MethodType, type)):
            module = _defining_module(obj)
            if module is not None:
                modules.add(module)
    return modules


def _walk(root: object, *, max_depth: int = 60) -> Iterator[tuple[str, object]]:
    """Breadth-first over everything reachable from ``root``, with its path.

    Follows dataclass fields and instance attributes (``__dict__`` and every
    ``__slots__`` in the MRO), ``functools.partial`` func/args/keywords, bound
    methods' ``__self__``, functions' closure cells, defaults and
    ``__wrapped__``, and container items. A visited set (by ``id``) keeps it
    finite; ``max_depth`` is a backstop, and reaching it fails the test
    rather than silently stopping short.
    """
    seen: set[int] = {id(root)}
    frontier: collections.deque[tuple[str, object, int]] = collections.deque([("services", root, 0)])
    while frontier:
        path, obj, depth = frontier.popleft()
        yield path, obj
        if isinstance(obj, _LEAVES) or obj is None:
            continue
        for step, child in _children(obj):
            if child is None or id(child) in seen:
                continue
            assert depth < max_depth, f"object graph deeper than {max_depth}: {path}{step}"
            seen.add(id(child))
            frontier.append((path + step, child, depth + 1))


def _describe(obj: object) -> str:
    if isinstance(obj, (types.FunctionType, types.MethodType, type)):
        return f"{_defining_module(obj)}.{getattr(obj, '__qualname__', '?')}"
    return f"{type(obj).__module__}.{type(obj).__qualname__}"


def _forbidden_reachable(root: object, identities: set[int]) -> list[str]:
    return [
        f"{path} -> {_describe(obj)}"
        for path, obj in _walk(root)
        if _is_forbidden(obj, identities)
    ]


def _refusing_scheduler(
    captured: list[ContextServices],
) -> Callable[[ContextServices, Callable[[], Sequence[str]]], Scheduler | None]:
    """The lifespan's own ``_context_services`` runs; no job is started."""

    def factory(
        services: ContextServices, secrets: Callable[[], Sequence[str]]
    ) -> Scheduler | None:
        captured.append(services)
        return None

    return factory


#: Placeholder values, never keys: enough for ``from_env`` to build every
#: real provider and broker object, none of which is ever called here.
_PLACEHOLDER_ENV = {
    ALPACA_PAPER_KEY_ENV: "placeholder-paper-key",
    ALPACA_PAPER_SECRET_ENV: "placeholder-paper-secret",
    ALPACA_LIVE_KEY_ENV: "placeholder-live-key",
    ALPACA_LIVE_SECRET_ENV: "placeholder-live-secret",
    ALPACA_OPTIONS_FEED_ENV: "indicative",
    ALPACA_STOCK_FEED_HISTORICAL_ENV: "sip",
    ALPACA_STOCK_FEED_REALTIME_ENV: "iex",
    FINNHUB_API_KEY_ENV: "placeholder-finnhub",
    FRED_API_KEY_ENV: "placeholder-fred",
    MASSIVE_API_KEY_ENV: "placeholder-massive",
    "SEC_USER_AGENT": "Placeholder Tester placeholder.tester@example.com",  # SYNTHETIC
}


@pytest.mark.risk
def test_the_graph_walk_finds_a_registry_behind_a_callable() -> None:
    """The walk itself, on the shape the audit found: a callable closing over the registry."""
    registry = ServiceRegistry.from_env({})
    holder = PositionUnderlyings()
    refresher = PaperPositionsRefresher(
        broker=lambda: registry.broker(AccountMode.PAPER), holder=holder
    )
    identities = {id(registry)}
    # Through a bound method's __self__, then a closure cell.
    leaky = ContextServices(session_factory=lambda: None, position_underlyings=refresher.refresh)  # type: ignore[arg-type, return-value]
    found = _forbidden_reachable(leaky, identities)
    assert any("ServiceRegistry" in line for line in found), found
    assert any(".__self__" in line and "__closure__" in line for line in found), found
    # Through a partial's keywords, into a lambda's closure.
    via_partial = ContextServices(
        session_factory=lambda: None,  # type: ignore[arg-type, return-value]
        position_underlyings=functools.partial(_no_positions, source=lambda: registry),
    )
    assert _forbidden_reachable(via_partial, identities)
    # And the production reader over the same holder reaches nothing forbidden.
    clean = ContextServices(
        session_factory=lambda: None,  # type: ignore[arg-type, return-value]
        position_underlyings=HeldPositionUnderlyings(holder),
    )
    assert _forbidden_reachable(clean, identities) == []


@pytest.mark.risk
def test_the_graph_walk_finds_code_from_a_forbidden_module_behind_a_partial() -> None:
    """Re-audit probe 1: a function from ``corollary.api.app`` reaches the app via ``__globals__``."""
    # ``corollary.api`` re-exports the FastAPI object as ``app``, which
    # shadows the submodule on attribute access; import the module by name.
    app_module = importlib.import_module("corollary.api.app")

    probe = ContextServices(
        session_factory=lambda: None,  # type: ignore[arg-type, return-value]
        seed_loader=functools.partial(len, app_module._context_services),  # type: ignore[arg-type]
    )
    found = _forbidden_reachable(probe, set())
    assert any("corollary.api.app._context_services" in line for line in found), found
    # And the import half sees it: the nested function's module is scanned.
    nested = {m for m in _code_modules(probe) if m.startswith("corollary.")}
    assert "corollary.api.app" in nested
    assert _forbidden(_reachable_corollary_modules(nested))
    # The top-level-only scan is what missed it: ``functools.partial(len, ...)``
    # is ``builtins`` there.
    assert "corollary.api.app" not in _service_modules(probe)


@pytest.mark.risk
def test_the_graph_walk_follows_a_live_weakref_to_the_registry() -> None:
    """Re-audit probe 2: ``weakref.ref(registry)`` was a leaf."""
    registry = ServiceRegistry.from_env({})
    ref = weakref.ref(registry)
    probe = ContextServices(
        session_factory=lambda: None,  # type: ignore[arg-type, return-value]
        seed_loader=functools.partial(len, ref),  # type: ignore[arg-type]
    )
    found = _forbidden_reachable(probe, {id(registry)})
    assert any(".args[0]()" in line and "ServiceRegistry" in line for line in found), found


class _HoldsABrokerOnTheClass:
    """A class attribute is state reached through the class, not code."""

    broker: object = None


@pytest.mark.risk
def test_the_graph_walk_walks_a_class_reached_as_a_value() -> None:
    """Re-audit probe 2: a class reached as a value was a leaf, hiding its attributes."""
    registry = ServiceRegistry.from_env({})
    books = type("Books", (), {"broker": registry})
    probe = ContextServices(
        session_factory=lambda: None,  # type: ignore[arg-type, return-value]
        seed_loader=functools.partial(len, books),  # type: ignore[arg-type]
    )
    found = _forbidden_reachable(probe, {id(registry)})
    assert any(".args[0].broker" in line and "ServiceRegistry" in line for line in found), found
    # Through a base class's attribute, too.
    _HoldsABrokerOnTheClass.broker = registry
    try:
        derived = type("Derived", (_HoldsABrokerOnTheClass,), {})
        probe = ContextServices(
            session_factory=lambda: None,  # type: ignore[arg-type, return-value]
            seed_loader=functools.partial(len, derived),  # type: ignore[arg-type]
        )
        assert _forbidden_reachable(probe, {id(registry)})
    finally:
        _HoldsABrokerOnTheClass.broker = None
    # A class from a forbidden module is itself flagged, identities or not.
    probe = ContextServices(
        session_factory=lambda: None,  # type: ignore[arg-type, return-value]
        seed_loader=functools.partial(len, ServiceRegistry),  # type: ignore[arg-type]
    )
    found = _forbidden_reachable(probe, set())
    assert any("corollary.api.deps.ServiceRegistry" in line for line in found), found


async def _no_positions(*, source: object) -> frozenset[str]:
    return frozenset()


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("wiring", ["fakes", "real_providers"])
async def test_nothing_the_lifespan_hands_the_jobs_can_reach_a_broker_or_the_runtime(
    wiring: str,
    make_registry: Callable[..., ServiceRegistry],
    paper_broker: RecordedBroker,
    cash_broker: RecordedBroker,
    db_engine: Engine,
) -> None:
    """Rule 1 and rule 9, structurally, on the services the **real** lifespan builds.

    Walks every object reachable from the :class:`ContextServices` that
    ``create_app``'s lifespan passes to its scheduler factory -- fields,
    partials, bound methods, closure cells, instance attributes, recursively
    -- and fails if a broker (either book), the service registry, the app,
    the engine runtime, the paper-positions refresher, or any object from
    ``corollary.engine.execution`` / ``runtime`` / ``state`` / ``sockets`` or
    ``corollary.api`` is among them. ``fakes`` is the route tests' registry
    with both books; ``real_providers`` builds every production provider and
    both real ``AlpacaBroker`` objects from placeholder values, so that
    something like an ``AlpacaProvider`` holding a broker would be found.
    Nothing is called on any of them; no network is touched.
    """
    if wiring == "fakes":
        registry = make_registry(live_keys=True)
    else:
        registry = ServiceRegistry.from_env(_PLACEHOLDER_ENV)
    # Build both books first, so the objects exist to be found if reachable.
    books = [registry.broker(AccountMode.PAPER), registry.broker(AccountMode.CASH)]
    captured: list[ContextServices] = []
    app = create_app(registry=registry, db_engine=db_engine, scheduler=_refusing_scheduler(captured))
    async with app.router.lifespan_context(app):
        (services,) = captured
        runtime = app.state.engine_runtime
        assert isinstance(runtime, EngineRuntime)
        identities = {id(registry), id(app), id(app.state), id(runtime), *(id(b) for b in books)}

        found = _forbidden_reachable(services, identities)
        assert found == [], "\n".join(sorted(found, key=len)[:8])

        # The walk went deep, not just one level: it reached the holder
        # behind the positions view and the database engine behind the
        # session factory.
        reached = {id(obj) for _, obj in _walk(services)}
        assert id(app.state.position_underlyings) in reached
        assert id(app.state.asset_directory) in reached
        assert id(db_engine) in reached
        # The notice outbox (owner decision 2026-09-30) is walked too, and is
        # the one the lifespan drains -- a job's only way to notify.
        assert services.notices is app.state.context_notices
        assert id(services.notices) in reached
        if wiring == "real_providers":
            assert services.alpaca is not None and services.finnhub is not None
            assert services.massive is not None and services.fred is not None
            assert id(services.alpaca) in reached
            # The SPDR job's two vendors (unit 4SEC-B2) are walked too: SEC,
            # and the CUSIP lookup -- the very market-data provider, not a broker.
            assert services.sec is not None and id(services.sec) in reached
            assert services.cusips is services.alpaca
            # OpenFIGI (spec Q17) is walked too, and reaches no broker.
            assert services.openfigi is not None and id(services.openfigi) in reached

        # The import half: every module an object in the services comes from
        # is corollary code the scan may walk, and none of it -- transitively
        # -- imports the runtime, the sockets, the broker surface or the API.
        modules = _service_modules(services)
        if wiring == "real_providers":
            assert _outside_corollary(modules) == []
        # Not only the top-level fields: every function, method and class the
        # walk reaches (unit 4B2 re-audit) -- a module's imports are what its
        # functions' ``__globals__`` hold.
        code = _code_modules(services)
        corollary_modules = {m for m in modules | code if m.startswith("corollary.")}
        assert "corollary.engine.scheduler" in corollary_modules
        assert _forbidden(_reachable_corollary_modules(corollary_modules)) == []
    await registry.aclose()


# --------------------------------------------------------------------------
# Shutdown waits for a job's worker thread
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_jobs_in_flight_database_write(db_engine: Engine) -> None:
    """``aclose`` cancels the job task; the thread it was awaiting still commits.

    The lifespan drains the context session factory after the scheduler, so
    the write lands *before* the lifespan returns, never after.
    """
    opened, release = threading.Event(), threading.Event()
    order: list[str] = []

    def write(session_factory: Callable[[], Session]) -> None:
        with session_factory() as session:
            opened.set()
            release.wait(timeout=10)
            session.execute(text("SELECT 1"))
            session.commit()
            order.append("thread committed")

    def factory(
        services: ContextServices, secrets: Callable[[], Sequence[str]]
    ) -> Scheduler:
        async def slow_write() -> None:
            await asyncio.to_thread(write, services.session_factory)

        job = ScheduledJob(
            name="slow_write",
            schedule=EveryInterval(timedelta(hours=1)),
            run=slow_write,
            rule="test: a write that outlives its task",
            catch_up=slow_write,
        )
        return Scheduler([job], secrets=secrets)

    app = create_app(registry=ServiceRegistry.from_env({}), db_engine=db_engine, scheduler=factory)
    async with app.router.lifespan_context(app):
        assert await asyncio.to_thread(opened.wait, 10), "the job never opened its session"
        # Released only once shutdown is under way: aclose has cancelled the
        # task by then, and only the drain stands between the commit and exit.
        threading.Timer(0.3, release.set).start()
    order.append("lifespan returned")
    assert order == ["thread committed", "lifespan returned"]


# --------------------------------------------------------------------------
# The SPDR seed is the database's accepted N-PORT snapshot (unit 4SEC-B2)
# --------------------------------------------------------------------------


def test_the_apps_seed_loader_serves_the_accepted_snapshot_from_its_database(
    db_engine: Engine,
) -> None:
    from datetime import date
    from decimal import Decimal

    from corollary.data.seeds import SPDR_SECTORS
    from corollary.db.models import SpdrHoldingRow, SpdrHoldingsSnapshot

    invalidate_spdr_seed_cache()
    app = create_app(db_engine=db_engine)
    loader = app.state.spdr_seed_loader
    assert isinstance(loader, DatabaseSeedLoader) and loader.bound
    assert loader() is None  # nothing accepted: no leaders, seedMissing
    with Session(db_engine) as session, session.begin():
        refused = SpdrHoldingsSnapshot(
            report_date=date(2026, 6, 30), filed_date=date(2026, 8, 28), built_at=NOW,
            status="refused", rule="weight_band", reason="XLB below 90", skipped_lines=29,
        )
        session.add(refused)
    assert loader() is None  # a refusal is not a seed
    with Session(db_engine) as session, session.begin():
        snap = SpdrHoldingsSnapshot(
            report_date=date(2026, 3, 31), filed_date=date(2026, 5, 29), built_at=NOW,
            status="accepted", rule=None, reason=None, skipped_lines=0,
        )
        session.add(snap)
        session.flush()
        for n, (etf, sector) in enumerate(SPDR_SECTORS.items()):
            for i in range(5):
                session.add(SpdrHoldingRow(
                    snapshot_id=snap.id, etf=etf, symbol=f"{etf}{chr(65 + i)}", sector=sector,
                    series_id="S000000000", accession="0000000000-26-000000",
                    cusip=f"SYN{n:05d}{i}", name="SYNTHETIC", weight=Decimal("20"),
                ))
    seed = loader()
    assert seed is not None
    assert seed.as_of == date(2026, 3, 31)
    assert seed.newer_report_date == date(2026, 6, 30)
    assert seed.sector_of("XLKA") == SPDR_SECTORS["XLK"]
    invalidate_spdr_seed_cache()


def test_an_unbound_seed_loader_refuses_rather_than_answering_no_seed() -> None:
    loader = DatabaseSeedLoader()
    assert not loader.bound
    with pytest.raises(SeedError, match="not bound"):
        loader()


# --------------------------------------------------------------------------
# Context notices reach the runtime's one notification path
# --------------------------------------------------------------------------


class _RecordingRuntime:
    def __init__(self, *, fail: bool = False) -> None:
        self.notices: list[object] = []
        self.fail = fail

    def notify_operator_action(self, notice: object) -> object:
        if self.fail:
            raise RuntimeError("https://discord.example/webhook/SYNTHETIC")
        self.notices.append(notice)
        return notice


def _context_notice(n: int) -> object:
    from corollary.engine.scheduler import ContextNotice

    return ContextNotice(
        event="spdr_seed_amended", severity="info", title="SPDR seed amended",
        body=f"body {n}", at=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
        correlation_id=f"c{n}",
    )


def test_queued_context_notices_are_handed_to_the_runtime_as_operator_notices() -> None:
    from corollary.api.app import flush_context_notices
    from corollary.engine.runtime import OperatorNotice
    from corollary.engine.scheduler import ContextNotices

    outbox = ContextNotices()
    outbox.put(_context_notice(1))  # type: ignore[arg-type]
    outbox.put(_context_notice(2))  # type: ignore[arg-type]
    runtime = _RecordingRuntime()
    assert flush_context_notices(outbox, runtime) == 2  # type: ignore[arg-type]
    assert [type(n) for n in runtime.notices] == [OperatorNotice, OperatorNotice]
    first = runtime.notices[0]
    assert isinstance(first, OperatorNotice)
    assert (first.event, first.severity, first.account, first.correlation_id) == (
        "spdr_seed_amended", "info", None, "c1"
    )
    assert flush_context_notices(outbox, runtime) == 0  # type: ignore[arg-type]


def test_a_failing_or_missing_runtime_never_raises_out_of_the_flush(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from corollary.api.app import flush_context_notices
    from corollary.engine.scheduler import ContextNotices

    outbox = ContextNotices()
    outbox.put(_context_notice(1))  # type: ignore[arg-type]
    flush_context_notices(outbox, _RecordingRuntime(fail=True))  # type: ignore[arg-type]
    outbox.put(_context_notice(2))  # type: ignore[arg-type]
    flush_context_notices(outbox, None)
    assert len(outbox) == 0
    assert "discord.example" not in caplog.text  # class name only (rule 6)


@pytest.mark.asyncio
async def test_the_lifespan_delivers_a_queued_context_notice_into_the_bell_table(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """End to end: a notice put on the outbox becomes a stored ``notification`` row."""
    from corollary.db.models import NotificationRecord

    captured: list[ContextServices] = []
    app = create_app(registry=registry, db_engine=db_engine, scheduler=_refusing_scheduler(captured))
    async with app.router.lifespan_context(app):
        (services,) = captured
        services.notices.put(_context_notice(7))  # type: ignore[arg-type]
    # The scheduler factory returned None, so no loop ran: shutdown's final
    # flush is what delivered it.
    with Session(db_engine) as session:
        rows = session.query(NotificationRecord).filter_by(event="spdr_seed_amended").all()
    assert [(r.severity, r.account) for r in rows] == [("info", None)]
