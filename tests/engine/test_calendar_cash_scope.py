"""The confirmed Cash book's underlyings join the earnings and dividends scope (cash-session unit 2).

What is pinned here, job side (the refresher that fills the holder is pinned
in ``tests/api/test_cash_positions_refresher.py``):

* **Rule 5.** No active Cash session, no Cash tickers -- whatever the holder
  still has. Ending the session drops Cash from the very next read, before
  any refresher tick. A read taken under one confirmation is never served
  under a later one.
* **Scope.** An active session with a current read puts its underlyings in
  the earnings and dividends fetch *and* their ``replace_window`` scope --
  and in nothing else: the news watch universe and the sentiment audit's
  universe are unchanged.
* **Degraded reads.** A missing or stale Cash read under an active session is
  a WARNING (``calendar_cash_positions_unavailable``), the run proceeds over
  the watch universe alone, and a Cash-only ticker's stored rows are never
  withdrawn, because it was never asked about.
"""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.data.calendar_dividends import CashDividend, CashDividendRead
from corollary.data.calendar_event import CalendarKind, CalendarSource
from corollary.engine import scheduler as scheduler_module
from corollary.engine.cash_session import CashSessionHolder, CashSessionReader
from corollary.engine.scheduler import (
    HELD_POSITIONS_STALE_AFTER,
    CashPositionUnderlyings,
    ContextServices,
    HeldCashUnderlyings,
    sentiment_audit_deps,
)
from tests.api.news_support import make_seed
from tests.engine.test_calendar_jobs import (
    NOW,
    FakeDividends,
    FakeFinnhubCalendar,
    _earning,
    _jobs,
    _live,
    _ReplaceSpy,
    _seed_row,
    _services,
    db_engine,  # noqa: F401 -- a fixture, used by name
    replace_spy,  # noqa: F401 -- a fixture, used by name
)

SCOPED_JOBS = ("calendar_earnings", "calendar_dividends")

#: PLTR is in no other member of the test watch universe (markets are AAPL and
#: NVDA, no seed, no manual watches, no paper positions): only Cash puts it there.
CASH_ONLY = "PLTR"


def _confirm(holder: CashSessionHolder, correlation_id: str = "cid-confirm") -> None:
    holder.confirm(
        account_number_masked="••••1234",
        balance=Decimal("25000.00"),
        at=NOW - timedelta(hours=1),
        correlation_id=correlation_id,
        generation=holder.generation,
    )


def _cash(
    *symbols: str, as_of: datetime | None = NOW, confirmed: bool = True
) -> tuple[HeldCashUnderlyings, CashSessionHolder]:
    """A Cash view over a holder read ``as_of`` under the current session."""
    sessions = CashSessionHolder()
    if confirmed:
        _confirm(sessions)
    reader = CashSessionReader(sessions)
    holder = CashPositionUnderlyings()
    current = reader.current()
    if as_of is not None and current is not None:
        holder.replace(symbols, at=as_of, session=current)
    return HeldCashUnderlyings(holder=holder, session=reader), sessions


def _pltr_earning() -> dict[str, object]:
    return _earning(symbol=CASH_ONLY, date="2026-11-18", quarter=4, year=2026)


def _pltr_dividend() -> CashDividend:
    return CashDividend(
        vendor_id="div-pltr-new",
        symbol=CASH_ONLY,
        ex_date=date(2026, 11, 24),
        rate=Decimal("0.10"),
        special=False,
    )


def _scoped_services(db_engine: Engine, view: HeldCashUnderlyings | None) -> ContextServices:
    """A complete fetch that lists a PLTR earnings report and a PLTR ex-date."""
    return _services(
        db_engine,
        finnhub=FakeFinnhubCalendar(earnings=[_earning(), _pltr_earning()]),
        dividends=FakeDividends(
            CashDividendRead(dividends=(_pltr_dividend(),), skipped=())
        ),
        cash_underlyings=view,
    )


def _seed_old_pltr_rows(db_engine: Engine) -> None:
    """A stored PLTR earnings report and ex-date that no fetch below lists."""
    _seed_row(
        db_engine,
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id="PLTR:2026Q3",
        day=date(2026, 11, 16),
        ticker=CASH_ONLY,
    )
    _seed_row(
        db_engine,
        kind=CalendarKind.DIVIDEND,
        source=CalendarSource.ALPACA,
        vendor_id="div-pltr-old",
        day=date(2026, 11, 20),
        ticker=CASH_ONLY,
    )


_OLD_PLTR = {"calendar_earnings": ("earnings", "PLTR:2026Q3"), "calendar_dividends": ("dividend", "div-pltr-old")}
_NEW_PLTR = {"calendar_earnings": ("earnings", "PLTR:2026Q4"), "calendar_dividends": ("dividend", "div-pltr-new")}


def _unavailable(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if getattr(r, "event", None) == "calendar_cash_positions_unavailable"
    ]


# --------------------------------------------------------------------------
# The view: rule 5, on every read
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_no_session_reads_no_cash_tickers_whatever_the_holder_has() -> None:
    view, sessions = _cash(CASH_ONLY)
    sessions.end()
    assert view.holder.current() == frozenset({CASH_ONLY})  # not cleared yet
    read = view.read(NOW)
    assert read.symbols == frozenset()
    assert read.unavailable is None  # no session is the ordinary state, not a fault


@pytest.mark.risk
def test_a_read_taken_under_an_earlier_session_is_never_served_under_a_new_one() -> None:
    view, sessions = _cash(CASH_ONLY)
    sessions.end()
    _confirm(sessions, correlation_id="cid-reconfirm")
    read = view.read(NOW)
    assert read.symbols == frozenset()
    assert read.unavailable is not None and "since this Cash session" in read.unavailable


@pytest.mark.risk
def test_a_current_read_under_the_active_session_is_served_and_the_bound_permits() -> None:
    view, _ = _cash(CASH_ONLY, as_of=NOW - HELD_POSITIONS_STALE_AFTER)
    assert view.read(NOW).symbols == frozenset({CASH_ONLY})
    stale = view.read(NOW + timedelta(seconds=1))
    assert stale.symbols == frozenset()
    assert stale.unavailable is not None and "past the" in stale.unavailable


def test_a_cleared_holder_has_no_symbols_time_or_session() -> None:
    view, _ = _cash(CASH_ONLY)
    view.holder.clear()
    assert view.holder.current() == frozenset()
    assert view.holder.as_of is None and view.holder.session is None


# --------------------------------------------------------------------------
# The jobs
# --------------------------------------------------------------------------


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCOPED_JOBS)
@pytest.mark.parametrize("case", ["no view", "never confirmed", "ended"])
async def test_without_an_active_session_the_scope_has_no_cash_tickers(
    db_engine: Engine,
    replace_spy: _ReplaceSpy,
    caplog: pytest.LogCaptureFixture,
    name: str,
    case: str,
) -> None:
    view: HeldCashUnderlyings | None
    if case == "no view":
        view = None
    elif case == "never confirmed":
        view, _ = _cash(CASH_ONLY, confirmed=False)
    else:
        # Ended after a good read and before any refresher tick: the very
        # next run already has no Cash.
        view, sessions = _cash(CASH_ONLY)
        sessions.end()
    services = _scoped_services(db_engine, view)
    with caplog.at_level(logging.WARNING, logger=scheduler_module.__name__):
        assert await _jobs(services)[name].run() is None

    (call,) = replace_spy.calls
    assert CASH_ONLY not in call["tickers"]
    assert _NEW_PLTR[name] not in _live(db_engine)
    assert _unavailable(caplog) == []
    if name == "calendar_dividends":
        (asked,) = services.dividends.calls  # type: ignore[union-attr]
        assert CASH_ONLY not in asked[0]


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCOPED_JOBS)
async def test_an_active_session_puts_its_underlyings_in_the_fetch_and_the_replace_scope(
    db_engine: Engine, replace_spy: _ReplaceSpy, name: str
) -> None:
    """PLTR is held only in Cash: its new rows are written, and -- being in
    scope of a complete fetch -- its stored row the fetch omits is withdrawn."""
    _seed_old_pltr_rows(db_engine)
    view, _ = _cash(CASH_ONLY)
    services = _scoped_services(db_engine, view)
    assert await _jobs(services)[name].run() is None

    (call,) = replace_spy.calls
    assert CASH_ONLY in call["tickers"]
    assert {"AAPL", "NVDA"} <= call["tickers"]  # the watch universe is still there
    live = _live(db_engine)
    assert _NEW_PLTR[name] in live
    assert _OLD_PLTR[name] not in live
    if name == "calendar_dividends":
        (asked,) = services.dividends.calls  # type: ignore[union-attr]
        assert CASH_ONLY in asked[0]


@pytest.mark.asyncio
async def test_a_cash_symbol_that_cannot_be_a_member_is_left_out_with_its_reason(
    db_engine: Engine, replace_spy: _ReplaceSpy, caplog: pytest.LogCaptureFixture
) -> None:
    view, _ = _cash(CASH_ONLY, "GME.WS")
    services = _scoped_services(db_engine, view)
    with caplog.at_level(logging.INFO, logger=scheduler_module.__name__):
        assert await _jobs(services)["calendar_earnings"].run() is None
    (call,) = replace_spy.calls
    assert CASH_ONLY in call["tickers"] and "GME.WS" not in call["tickers"]
    (skipped,) = [
        r for r in caplog.records if getattr(r, "event", None) == "calendar_cash_position_skipped"
    ]
    assert skipped.symbol == "GME.WS"  # type: ignore[attr-defined]
    (joined,) = [
        r for r in caplog.records if getattr(r, "event", None) == "calendar_cash_positions_in_scope"
    ]
    assert joined.added == [CASH_ONLY]  # type: ignore[attr-defined]
    # Tickers and counts only: nothing about the account reaches the log.
    for record in caplog.records:
        assert "1234" not in record.getMessage() and "1234" not in repr(record.__dict__)
        assert "25000" not in repr(record.__dict__)


_DEGRADED = {
    "never read": None,
    "stale": NOW - HELD_POSITIONS_STALE_AFTER - timedelta(seconds=1),
}


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCOPED_JOBS)
@pytest.mark.parametrize("state", sorted(_DEGRADED))
async def test_a_missing_or_stale_cash_read_proceeds_without_cash_and_withdraws_nothing_of_it(
    db_engine: Engine,
    replace_spy: _ReplaceSpy,
    caplog: pytest.LogCaptureFixture,
    name: str,
    state: str,
) -> None:
    """Not a skip and not a gap: the watch universe's window is still replaced,
    and PLTR's stored rows stay live because PLTR was never asked about."""
    _seed_old_pltr_rows(db_engine)
    view, _ = _cash(CASH_ONLY, as_of=_DEGRADED[state])
    services = _scoped_services(db_engine, view)
    with caplog.at_level(logging.WARNING, logger=scheduler_module.__name__):
        assert await _jobs(services)[name].run() is None

    (call,) = replace_spy.calls  # replaced, not kept: the calendar ran
    assert CASH_ONLY not in call["tickers"]
    assert {"AAPL", "NVDA"} <= call["tickers"]
    live = _live(db_engine)
    assert _OLD_PLTR[name] in live  # withdrawn nothing of it
    assert _NEW_PLTR[name] not in live  # and fetched nothing for it
    (record,) = _unavailable(caplog)
    assert record.job == name  # type: ignore[attr-defined]
    assert "Cash positions" in record.reason  # type: ignore[attr-defined]
    kept = [r for r in caplog.records if getattr(r, "event", None) == "calendar_window_kept"]
    assert kept == []


# --------------------------------------------------------------------------
# Calendar only: the news and audit universes are unchanged
# --------------------------------------------------------------------------


@pytest.mark.risk
@pytest.mark.asyncio
async def test_cash_underlyings_are_in_neither_the_news_nor_the_audit_universe(
    db_engine: Engine,
) -> None:
    view, _ = _cash(CASH_ONLY)
    services = _services(db_engine, cash_underlyings=view)
    assert view.read(NOW).symbols == frozenset({CASH_ONLY})  # Cash is live and current

    news = await scheduler_module._watch_universe_source(services)()
    assert CASH_ONLY not in news.universe.symbols
    assert "NVDA" in news.universe.symbols

    audited = ContextServices(
        funds=(),
        session_factory=lambda: Session(db_engine),
        markets=("AAPL", "NVDA"),
        alpaca=object(),  # type: ignore[arg-type]
        seed_loader=make_seed,
        cash_underlyings=view,
    )
    deps = sentiment_audit_deps(audited, clock=lambda: NOW)
    assert deps is not None
    universe = await deps.watch_universe()
    assert CASH_ONLY not in universe
    assert "NVDA" in universe
