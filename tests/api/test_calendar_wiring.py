"""The lifespan hands the calendar jobs the same vendor instances as every other job.

Phase 3 step 7, unit 7.2c-2. Decision 15: one bucket per host. A calendar job
holding a *second* Finnhub, Alpaca or FRED client would spend that host's
budget twice and look fine locally, so the services the real lifespan builds
must carry the very objects the news and FRED jobs hold -- or ``None`` when
the vendor is unconfigured, so the job skips. The object-graph walk in
``test_news_wiring.py`` covers these fields too: it walks every field.
"""

import pytest
from sqlalchemy import Engine

from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.engine.scheduler import ContextServices
from tests.api.test_news_wiring import _PLACEHOLDER_ENV, _refusing_scheduler


@pytest.mark.asyncio
async def test_the_calendar_jobs_share_the_news_and_fred_jobs_vendor_instances(
    db_engine: Engine,
) -> None:
    registry = ServiceRegistry.from_env(_PLACEHOLDER_ENV)
    captured: list[ContextServices] = []
    app = create_app(registry=registry, db_engine=db_engine, scheduler=_refusing_scheduler(captured))
    async with app.router.lifespan_context(app):
        (services,) = captured
        assert services.finnhub is not None and services.alpaca is not None
        assert services.fred is not None
        assert services.finnhub_calendar is services.finnhub
        assert services.dividends is services.alpaca
        assert services.fred_releases is services.fred
    await registry.aclose()


@pytest.mark.asyncio
async def test_with_no_vendor_key_every_calendar_vendor_is_absent(db_engine: Engine) -> None:
    registry = ServiceRegistry.from_env({})
    captured: list[ContextServices] = []
    app = create_app(registry=registry, db_engine=db_engine, scheduler=_refusing_scheduler(captured))
    async with app.router.lifespan_context(app):
        (services,) = captured
        assert (services.finnhub_calendar, services.dividends, services.fred_releases) == (
            None,
            None,
            None,
        )
    await registry.aclose()
