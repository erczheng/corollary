"""The lifespan's Cash positions reader (cash-session unit 2).

The job side -- what the calendar does with what this reads -- is pinned in
``tests/engine/test_calendar_cash_scope.py``. Here:

* **Rule 5.** No confirmed session, or no live keys: no broker call at all,
  and the holder is cleared. A session that ends while the read is in flight
  writes nothing.
* **Same terms as the paper refresher.** The same interval, a failed read
  keeps the last value and logs its class name only, nothing raises out.
* **Lifespan.** The jobs get the holder view over the app's own holder and
  session, never this refresher; the refresher reads nothing until Cash is
  confirmed, and shutdown clears the holder.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from corollary.api.app import create_app
from corollary.api.deps import (
    POSITION_UNDERLYINGS_TTL,
    CashPositionsRefresher,
    ServiceRegistry,
)
from corollary.api.schemas import AccountMode
from corollary.engine.cash_session import CashSessionHolder, CashSessionReader
from corollary.engine.execution.interface import BrokerAccount, BrokerPosition
from corollary.engine.scheduler import (
    CashPositionUnderlyings,
    ContextServices,
    HeldCashUnderlyings,
)
from tests.api.conftest import FakeProvider, RecordedBroker
from tests.api.test_news_wiring import NOW, Clock, PositionsOnly, _capturing, _held
from tests.engine.test_scheduler import FakeClock

MASK = "••••1234"


def _confirm(sessions: CashSessionHolder, correlation_id: str = "cid-confirm") -> None:
    sessions.confirm(
        account_number_masked=MASK,
        balance=Decimal("25000.00"),
        at=NOW - timedelta(minutes=1),
        correlation_id=correlation_id,
        generation=sessions.generation,
    )


def _registry(cash: object | None, paper: object | None = None) -> ServiceRegistry:
    """A registry whose Cash book is ``cash`` -- or absent, as with no live keys."""
    paper_book = paper if paper is not None else PositionsOnly([])
    brokers: dict[AccountMode, Callable[[], BrokerAccount]] = {
        AccountMode.PAPER: lambda: paper_book,  # type: ignore[return-value]
    }
    missing: tuple[str, ...] = ("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY")
    if cash is not None:
        brokers[AccountMode.CASH] = lambda: cash  # type: ignore[return-value]
        missing = ()
    return ServiceRegistry(
        brokers=brokers, provider=lambda: FakeProvider(), missing_live_credentials=missing
    )


def _refresher(
    registry: ServiceRegistry,
    sessions: CashSessionHolder,
    clock: Callable[[], datetime] | None = None,
) -> tuple[CashPositionsRefresher, CashPositionUnderlyings]:
    holder = CashPositionUnderlyings()
    refresher = CashPositionsRefresher(
        registry=registry,
        session=CashSessionReader(sessions),
        holder=holder,
        clock=clock if clock is not None else Clock(NOW),
    )
    return refresher, holder


def _prefill(holder: CashPositionUnderlyings, sessions: CashSessionHolder) -> None:
    """A read left over from an earlier session -- what a clear must remove."""
    earlier = CashSessionHolder()
    _confirm(earlier, correlation_id="cid-earlier")
    session = earlier.current()
    assert session is not None
    holder.replace({"PLTR"}, at=NOW - timedelta(minutes=1), session=session)


# --------------------------------------------------------------------------
# Rule 5: no read without a confirmed session
# --------------------------------------------------------------------------


@pytest.mark.risk
@pytest.mark.asyncio
async def test_without_a_session_nothing_is_read_and_the_holder_is_cleared() -> None:
    cash = PositionsOnly([_held("PLTR261218C00030000")])
    paper = PositionsOnly([_held("AAPL", "us_equity")])
    sessions = CashSessionHolder()
    refresher, holder = _refresher(_registry(cash, paper), sessions)
    _prefill(holder, sessions)

    assert await refresher.refresh() is False
    assert cash.calls == 0 and paper.calls == 0  # neither book: never a substitute
    assert holder.current() == frozenset()
    assert holder.as_of is None and holder.session is None


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_session_without_live_keys_reads_nothing_and_clears() -> None:
    paper = PositionsOnly([_held("AAPL", "us_equity")])
    sessions = CashSessionHolder()
    _confirm(sessions)
    refresher, holder = _refresher(_registry(None, paper), sessions)
    _prefill(holder, sessions)

    assert await refresher.refresh() is False
    assert paper.calls == 0  # never paper in Cash's place
    assert holder.current() == frozenset() and holder.as_of is None


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_confirmed_session_is_read_and_tagged_with_that_session() -> None:
    cash = PositionsOnly(
        [_held("PLTR261218C00030000"), _held("PLTR261218P00025000"), _held("KO", "us_equity")]
    )
    sessions = CashSessionHolder()
    _confirm(sessions)
    refresher, holder = _refresher(_registry(cash), sessions)

    assert await refresher.refresh() is True
    assert cash.calls == 1
    assert holder.current() == frozenset({"PLTR", "KO"})
    assert holder.as_of == NOW
    assert holder.session is sessions.current()
    view = HeldCashUnderlyings(holder=holder, session=CashSessionReader(sessions))
    assert view.read(NOW).symbols == frozenset({"PLTR", "KO"})

    # Ended: the view yields nothing at once; the next tick reads nothing and clears.
    sessions.end()
    assert view.read(NOW).symbols == frozenset()
    assert await refresher.refresh() is False
    assert cash.calls == 1
    assert holder.as_of is None


class _BlockedPositions(PositionsOnly):
    """A Cash read that waits until the test lets it return."""

    def __init__(self, positions: list[BrokerPosition]) -> None:
        super().__init__(positions)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def positions(self) -> list[BrokerPosition]:
        self.entered.set()
        await self.release.wait()
        return await super().positions()


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("then", ["ended", "reconfirmed"])
async def test_a_session_that_ends_during_the_read_writes_nothing(then: str) -> None:
    cash = _BlockedPositions([_held("PLTR261218C00030000")])
    sessions = CashSessionHolder()
    _confirm(sessions)
    refresher, holder = _refresher(_registry(cash), sessions)

    task = asyncio.create_task(refresher.refresh())
    await asyncio.wait_for(cash.entered.wait(), timeout=5)
    sessions.end()
    if then == "reconfirmed":
        _confirm(sessions, correlation_id="cid-again")
    cash.release.set()

    assert await asyncio.wait_for(task, timeout=5) is False
    assert holder.current() == frozenset() and holder.session is None


# --------------------------------------------------------------------------
# The paper refresher's terms
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_the_cash_refresher_runs_on_the_paper_refreshers_interval() -> None:
    """The calendar holds both reads to ``HELD_POSITIONS_STALE_AFTER``, pinned
    to twice this interval in ``tests/engine/test_calendar_jobs.py``."""
    refresher, _ = _refresher(_registry(None), CashSessionHolder())
    assert refresher._interval == POSITION_UNDERLYINGS_TTL


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_last_value_and_logs_no_message_or_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cash = PositionsOnly([_held("PLTR261218C00030000")])
    sessions = CashSessionHolder()
    _confirm(sessions)
    clock = Clock(NOW)
    refresher, holder = _refresher(_registry(cash), sessions, clock)
    assert await refresher.refresh() is True

    cash.fail_with = RuntimeError("401 for key AKSECRETSECRET on 123456781234")
    clock.now = NOW + timedelta(minutes=6)
    with caplog.at_level(logging.INFO):
        assert await refresher.refresh() is False
    assert holder.current() == frozenset({"PLTR"})
    assert holder.as_of == NOW  # the view ages it out past the bound
    (said,) = [
        r for r in caplog.records if getattr(r, "event", None) == "cash_position_underlyings_unavailable"
    ]
    assert getattr(said, "error_type") == "RuntimeError"
    assert getattr(said, "account") == AccountMode.CASH.value
    for record in caplog.records:
        for needle in ("AKSECRETSECRET", "1234", "25000"):
            assert needle not in record.getMessage()
            assert needle not in repr(record.__dict__)


@pytest.mark.asyncio
async def test_a_naive_clock_is_a_logged_failure_that_clears_never_a_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cash = PositionsOnly([_held("PLTR261218C00030000")])
    sessions = CashSessionHolder()
    _confirm(sessions)
    refresher, holder = _refresher(_registry(cash), sessions, lambda: NOW.replace(tzinfo=None))
    _prefill(holder, sessions)
    with caplog.at_level(logging.ERROR):
        assert await refresher.refresh() is False
    assert holder.as_of is None
    assert [
        r for r in caplog.records if getattr(r, "event", None) == "cash_position_underlyings_refresh_failed"
    ]


@pytest.mark.asyncio
async def test_the_loop_reads_once_per_interval_and_close_clears_the_holder() -> None:
    cash = PositionsOnly([_held("KO", "us_equity")])
    sessions = CashSessionHolder()
    _confirm(sessions)
    holder = CashPositionUnderlyings()
    slept: list[float] = []
    second_sleep = asyncio.Event()

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            second_sleep.set()
            await asyncio.Event().wait()  # parked until aclose cancels it

    refresher = CashPositionsRefresher(
        registry=_registry(cash),
        session=CashSessionReader(sessions),
        holder=holder,
        clock=Clock(NOW),
        sleep=sleep,
    )
    refresher.start()
    try:
        await asyncio.wait_for(second_sleep.wait(), timeout=5)
        assert cash.calls == 2
        assert slept == [300.0, 300.0]
        assert holder.current() == frozenset({"KO"})
    finally:
        await refresher.aclose()
        await refresher.aclose()  # a second close is a no-op
    assert holder.as_of is None


# --------------------------------------------------------------------------
# The lifespan
# --------------------------------------------------------------------------


@pytest.mark.risk
@pytest.mark.asyncio
async def test_the_lifespan_reads_cash_only_once_confirmed_and_clears_at_shutdown(
    make_registry: Callable[..., ServiceRegistry],
    paper_broker: RecordedBroker,
    cash_broker: RecordedBroker,
    db_engine: Engine,
) -> None:
    registry = make_registry(live_keys=True)
    clock = FakeClock(NOW, stop_at=NOW)
    captured: list[ContextServices] = []
    app = create_app(registry=registry, db_engine=db_engine, scheduler=_capturing(captured, clock))
    async with app.router.lifespan_context(app):
        (services,) = captured
        refresher = app.state.cash_position_refresher
        assert isinstance(refresher, CashPositionsRefresher)

        async def paper_read() -> None:
            while app.state.position_underlyings.as_of is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(paper_read(), timeout=10)
        # Started beside the paper reader, and read nothing: no session.
        assert await refresher.refresh() is False
        assert cash_broker.calls == []
        assert services.cash_underlyings is not None
        assert services.cash_underlyings.read(NOW).symbols == frozenset()

        sessions: CashSessionHolder = app.state.cash_session
        _confirm(sessions)
        assert await refresher.refresh() is True
        assert cash_broker.calls == ["positions"]
        assert app.state.cash_positions.session is sessions.current()
    assert app.state.cash_positions.as_of is None
    assert not app.state.cash_session.active()
