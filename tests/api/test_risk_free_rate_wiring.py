"""The risk-free rate through the registry and the lifespan (decision 19, step 3).

* One :class:`RiskFreeRateSource` per registry, shared by the market-data
  provider and the FRED job.
* The lifespan seeds it from ``fred_observation`` before serving.
* A missing ``FRED_API_KEY`` leaves FRED unavailable -- logged once -- and the
  app boots; the rate stays the labelled default.
* With FRED configured and the table empty, the shipped scheduler fetches once
  at start. Replayed from the recorded response; no live calls.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import partial

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from corollary.api.app import create_app
from corollary.api.deps import RiskFreeRateSplitError, ServiceRegistry
from corollary.data.macro.risk_free import store_observations
from corollary.data.providers.alpaca import AlpacaProvider
from corollary.data.providers.fred import (
    FRED_API_KEY_ENV,
    FredCredentials,
    FredProvider,
    parse_observations,
)
from corollary.db.models import FredObservationRecord
from corollary.engine.scheduler import build_context_scheduler
from corollary.pricing.rates import (
    FALLBACK_RISK_FREE_RATE,
    RateProvenance,
    RiskFreeRateSource,
    rate_from_dgs3mo,
)
from corollary.ratelimit import FRED_HOST, HostRateLimiter
from tests.data.providers.test_fred_provider import FAKE_KEY, dgs3mo_body, fixture_body_text
from tests.engine.test_scheduler import WED_1500_ET, FakeClock

UTC = timezone.utc

#: Paper keys only, and obviously fake -- enough for ``from_env`` to build a
#: provider object; nothing here makes a request with them.
PAPER_ENV = {
    "ALPACA_PAPER_API_KEY": "PKTESTTESTTESTTEST",
    "ALPACA_PAPER_SECRET_KEY": "not-a-real-secret",
    "ALPACA_OPTIONS_FEED": "indicative",
    "ALPACA_STOCK_FEED_HISTORICAL": "sip",
    "ALPACA_STOCK_FEED_REALTIME": "iex",
}


def _store_recorded(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        store_observations(
            session,
            parse_observations("DGS3MO", dgs3mo_body()),
            fetched_at=datetime(2026, 9, 23, 14, 0, tzinfo=UTC),
        )
        session.commit()


@asynccontextmanager
async def _recorded_fred() -> AsyncIterator[FredProvider]:
    """A FRED provider replaying the recorded DGS3MO response.

    A context manager because the provider does not close a client it was
    handed, and a ``MockTransport`` client left open is a resource warning
    at best.
    """
    body = fixture_body_text("p3_observations_dgs3mo").encode("utf-8")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == FRED_HOST, request.url.host
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = FredProvider(
            credentials=FredCredentials(api_key=FAKE_KEY),
            client=client,
            limiter=HostRateLimiter(per_host={}),
        )
        try:
            yield provider
        finally:
            await provider.aclose()


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


def test_from_env_shares_one_rate_source_with_the_provider() -> None:
    registry = ServiceRegistry.from_env(PAPER_ENV)
    provider = registry.provider
    assert isinstance(provider, AlpacaProvider)
    assert provider.rates is registry.rates


@pytest.mark.asyncio
async def test_a_missing_fred_key_leaves_fred_unavailable_and_says_so_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ServiceRegistry.from_env(PAPER_ENV)
    with caplog.at_level(logging.DEBUG):
        assert registry.fred_provider() is None
        assert registry.fred_provider() is None
    said = [r for r in caplog.records if getattr(r, "event", None) == "fred_unavailable"]
    assert len(said) == 1
    assert said[0].levelno == logging.WARNING
    assert FRED_API_KEY_ENV in said[0].getMessage()
    await registry.aclose()


@pytest.mark.asyncio
async def test_a_set_fred_key_builds_one_provider_and_closes_it() -> None:
    registry = ServiceRegistry.from_env({**PAPER_ENV, FRED_API_KEY_ENV: FAKE_KEY})
    fred = registry.fred_provider()
    assert isinstance(fred, FredProvider)
    assert registry.fred_provider() is fred
    httpx_filters_before_close = list(logging.getLogger("httpx").filters)
    await registry.aclose()
    assert len(logging.getLogger("httpx").filters) == len(httpx_filters_before_close) - 1


def test_a_registry_whose_provider_holds_another_rate_source_refuses_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A split registry would price every chain at the default forever.

    The FRED job updates the registry's source; the provider reads its own.
    Nothing would error -- the chain would just say ``default`` long after an
    observation was stored. So the registry refuses the pair on first use.
    """
    provider = AlpacaProvider.from_env(PAPER_ENV, risk_free_rate=RiskFreeRateSource())
    registry = ServiceRegistry(brokers={}, provider=lambda: provider)
    with caplog.at_level(logging.ERROR), pytest.raises(RiskFreeRateSplitError):
        registry.provider
    assert any(
        getattr(r, "event", None) == "risk_free_rate_split" for r in caplog.records
    )


def test_a_registry_given_the_providers_rate_source_serves_it() -> None:
    provider = AlpacaProvider.from_env(PAPER_ENV, risk_free_rate=RiskFreeRateSource())
    registry = ServiceRegistry(
        brokers={}, provider=lambda: provider, rates=provider.rates
    )
    assert registry.provider is provider
    assert registry.rates is provider.rates


def test_a_registry_built_without_fred_has_none_and_a_default_rate(
    registry: ServiceRegistry,
) -> None:
    assert registry.fred_provider() is None
    assert registry.rates.current() == FALLBACK_RISK_FREE_RATE


# --------------------------------------------------------------------------
# The lifespan
# --------------------------------------------------------------------------


def test_the_lifespan_seeds_the_rate_from_a_stored_observation(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    _store_recorded(db_engine)
    app = create_app(registry=registry, db_engine=db_engine)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert registry.rates.current() == rate_from_dgs3mo(
            Decimal("4.16"), date(2026, 9, 22)
        )


def test_with_nothing_stored_the_app_boots_at_the_default(
    registry: ServiceRegistry, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(registry=registry, db_engine=db_engine)
    with caplog.at_level(logging.INFO):
        with TestClient(app) as client:
            assert client.get("/api/health").status_code == 200
    assert registry.rates.current() == FALLBACK_RISK_FREE_RATE
    seeded = [r for r in caplog.records if getattr(r, "event", None) == "risk_free_rate_seeded"]
    assert len(seeded) == 1
    assert getattr(seeded[0], "source") == "default"


def test_an_unmigrated_database_names_the_migration_when_the_seed_fails(
    registry: ServiceRegistry, db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """At 0006 the table is missing, and FRED will never fix that on its own.

    Every daily job would fetch successfully and then fail at the upsert, so
    "until FRED refreshes" would be false. The log names the fix instead.
    """
    with db_engine.begin() as connection:
        connection.execute(text("DROP TABLE fred_observation"))
    app = create_app(registry=registry, db_engine=db_engine)
    with caplog.at_level(logging.ERROR):
        with TestClient(app) as client:
            assert client.get("/api/health").status_code == 200
    assert registry.rates.current() == FALLBACK_RISK_FREE_RATE
    [failed] = [
        r for r in caplog.records
        if getattr(r, "event", None) == "risk_free_rate_seed_failed"
    ]
    assert "uv run alembic upgrade head" in failed.getMessage()
    assert "until FRED refreshes" not in failed.getMessage()
    assert "fred_observation" in getattr(failed, "detail")
    assert getattr(failed, "error_type") == "OperationalError"


@pytest.mark.risk
def test_a_missing_fred_key_does_not_stop_the_shipped_scheduler_app_booting(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FRED_API_KEY`` unset, the shipped scheduler factory: the app serves."""
    monkeypatch.delenv(FRED_API_KEY_ENV, raising=False)
    registry = ServiceRegistry.from_env(PAPER_ENV)
    app = create_app(
        registry=registry, db_engine=db_engine, scheduler=build_context_scheduler
    )
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert app.state.scheduler is not None
        assert "fred_dgs3mo" in app.state.scheduler.status()
    assert registry.rates.current() == FALLBACK_RISK_FREE_RATE


def _registry_with(
    make_registry: Callable[..., ServiceRegistry], fred: FredProvider
) -> ServiceRegistry:
    base = make_registry()
    return ServiceRegistry(
        brokers={mode: (lambda b=base, m=mode: b.broker(m)) for mode in base._factories},
        provider=lambda: base.provider,
        fred=lambda: fred,
        # The provider's registry's source, not a fresh one: a split pair is
        # exactly what ``RiskFreeRateSplitError`` refuses.
        rates=base.rates,
        missing_live_credentials=base.missing_live_credentials,
    )


@pytest.mark.asyncio
async def test_with_fred_and_an_empty_table_the_lifespan_fetches_once_at_start(
    make_registry: Callable[..., ServiceRegistry], db_engine: Engine
) -> None:
    async with _recorded_fred() as fred:
        await _fetch_once_at_start(make_registry, db_engine, fred)


async def _fetch_once_at_start(
    make_registry: Callable[..., ServiceRegistry],
    db_engine: Engine,
    fred: FredProvider,
) -> None:
    registry = _registry_with(make_registry, fred)
    clock = FakeClock(
        WED_1500_ET,
        stop_at=WED_1500_ET + timedelta(minutes=30),
        wait_for_threads=True,
    )
    app = create_app(
        registry=registry,
        db_engine=db_engine,
        scheduler=partial(build_context_scheduler, clock=clock, sleep=clock.sleep),
    )
    async with app.router.lifespan_context(app):
        assert registry.rates.current() == FALLBACK_RISK_FREE_RATE  # seeded empty
        await asyncio.wait_for(app.state.scheduler.ready(), timeout=10)
        driver = asyncio.get_running_loop().create_task(clock.drive())
        try:
            await asyncio.wait_for(clock.parked.wait(), timeout=10)
        finally:
            driver.cancel()
        status = app.state.scheduler.status()["fred_dgs3mo"]

    assert status.failures == 0
    assert status.runs == 1  # the catch-up; 10:00 ET was already past
    assert registry.rates.current().provenance is RateProvenance.FRED_DGS3MO
    assert registry.rates.current().observation_date == date(2026, 9, 22)
    with Session(db_engine) as session:
        stored = list(session.scalars(select(FredObservationRecord)))
    assert len(stored) == 10
