"""The calendar jobs in the context scheduler (Phase 3 step 7, unit 7.2c-2).

Five jobs: Finnhub earnings and IPOs, Alpaca dividends, FRED release dates,
and the committed central-bank seed. What is pinned here:

* **A complete, successful fetch replaces its window** through
  :func:`corollary.data.calendar.replace_window` -- a row the vendor no
  longer lists is withdrawn.
* **Anything less withdraws nothing.** A vendor error, a premium refusal
  (``CalendarAccessDenied``), a dividend read that ``FAILED``, a skip, or a
  fetch that is only partial (an unreadable row, or a watch universe built
  without its positions or its seed) leaves a pre-seeded row live.
* **A missing key is a skip with a reason, never a success.**
* **The clocks** are ET wall-clock times that follow DST, the vendor
  calendars every day and the FRED one on trading days only.
* **The start-up catch-up** fetches only when the stored rows are older than
  a day, and the central-bank seed always imports at start.

The rule 9 isolation of these five jobs -- raising, hanging, the heartbeat --
is in ``tests/engine/test_scheduler.py``, beside the rest of the shipped set.
"""

import asyncio
import logging
import threading
import time as time_module
from collections.abc import Iterator, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.deps import POSITION_UNDERLYINGS_TTL
from corollary.data import calendar as calendar_store
from corollary.data import calendar_releases as releases_module
from corollary.data.calendar import read_range, upsert_events
from corollary.data.calendar_dividends import (
    DIVIDEND_EX_DATE_HORIZON,
    CashDividend,
    CashDividendRead,
    SkippedDividend,
)
from corollary.data.calendar_event import CalendarEventInput, CalendarKind, CalendarSource
from corollary.data.calendar_finnhub import EARNINGS_WINDOW, IPO_WINDOW
from corollary.data.calendar_releases import ReleaseEvents, SkippedRelease
from corollary.data.providers.finnhub import CalendarAccessDenied, CalendarProviderError
from corollary.data.providers.fred import FredError, FredReleaseDate
from corollary.data.providers.interface import ProviderError
from corollary.db.models import Base, CalendarEvent
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine import scheduler as scheduler_module
from corollary.engine.scheduler import (
    CALENDAR_CATCH_UP_AFTER,
    HELD_POSITIONS_STALE_AFTER,
    AtTime,
    ContextServices,
    HeldPositionUnderlyings,
    JobBody,
    PositionUnderlyings,
    JobSkipped,
    ScheduledJob,
    Scheduler,
    context_jobs,
    every_day,
    trading_days,
)

UTC = timezone.utc

#: Tuesday 10 Nov 2026, 13:00 UTC = 08:00 EST: after the 07:00-07:10 vendor
#: slots, before FRED's 10:00.
NOW = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)
TODAY = date(2026, 11, 10)

CALENDAR_JOBS = (
    "calendar_earnings",
    "calendar_ipo",
    "calendar_dividends",
    "calendar_releases",
    "calendar_central_banks",
)


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


def _earning(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": "NVDA",
        "date": "2026-11-19",
        "hour": "amc",
        "quarter": 3,
        "year": 2027,
        "epsEstimate": Decimal("1.25"),
        "epsActual": None,
        "revenueEstimate": 54000000000,
        "revenueActual": None,
    }
    row.update(overrides)
    return row


def _ipo(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": "2026-11-20",
        "exchange": "NASDAQ Global Select",
        "name": "Iambic Therapeutics, Inc.",
        "numberOfShares": 9375000,
        "price": "15.00-17.00",
        "status": "expected",
        "symbol": "IAM",
        "totalSharesValue": 183281250,
    }
    row.update(overrides)
    return row


class FakeFinnhubCalendar:
    """``FinnhubProvider``'s two calendar reads, scripted."""

    def __init__(
        self,
        *,
        earnings: Sequence[object] = (),
        ipos: Sequence[object] = (),
        error: Exception | None = None,
    ) -> None:
        self.earnings = list(earnings)
        self.ipos = list(ipos)
        self.error = error
        self.calls: list[tuple[str, date, date]] = []

    async def earnings_calendar(self, start: date, end: date) -> list[Any]:
        self.calls.append(("earnings", start, end))
        if self.error is not None:
            raise self.error
        return list(self.earnings)

    async def ipo_calendar(self, start: date, end: date) -> list[Any]:
        self.calls.append(("ipo", start, end))
        if self.error is not None:
            raise self.error
        return list(self.ipos)


class FakeDividends:
    """``AlpacaProvider.cash_dividends``, scripted."""

    def __init__(
        self, read: CashDividendRead | None = None, *, error: Exception | None = None
    ) -> None:
        self.read = read if read is not None else CashDividendRead(dividends=(), skipped=())
        self.error = error
        self.calls: list[tuple[tuple[str, ...], date, date]] = []

    async def cash_dividends(
        self, *, symbols: Sequence[str], start: date, end: date
    ) -> CashDividendRead:
        self.calls.append((tuple(symbols), start, end))
        if self.error is not None:
            raise self.error
        return self.read


class FakeReleases:
    """``FredProvider.release_dates``, scripted."""

    def __init__(
        self, rows: Sequence[FredReleaseDate] = (), *, error: Exception | None = None
    ) -> None:
        self.rows = tuple(rows)
        self.error = error
        self.calls: list[tuple[date, date]] = []

    async def release_dates(self, start: date, end: date) -> Sequence[FredReleaseDate]:
        self.calls.append((start, end))
        if self.error is not None:
            raise self.error
        return self.rows


def _cpi(day: date) -> FredReleaseDate:
    return FredReleaseDate(release_id=10, release_name="Consumer Price Index", date=day)


def _dividend(vendor_id: str = "div-aapl", ex_date: date = date(2026, 11, 20)) -> CashDividend:
    return CashDividend(
        vendor_id=vendor_id, symbol="AAPL", ex_date=ex_date, rate=Decimal("0.26"), special=False
    )


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "calendar_jobs.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _services(
    db_engine: Engine,
    *,
    finnhub: object | None = None,
    dividends: object | None = None,
    releases: object | None = None,
    **extra: object,
) -> ContextServices:
    return ContextServices(
        session_factory=lambda: Session(db_engine),
        finnhub_calendar=finnhub,  # type: ignore[arg-type]
        dividends=dividends,  # type: ignore[arg-type]
        fred_releases=releases,  # type: ignore[arg-type]
        markets=("AAPL", "NVDA"),
        seed_loader=lambda: None,
        **extra,  # type: ignore[arg-type]
    )


def _jobs(services: ContextServices, now: datetime = NOW) -> dict[str, ScheduledJob]:
    return {job.name: job for job in context_jobs(services, clock=lambda: now)}


def _seed_row(
    db_engine: Engine,
    *,
    kind: CalendarKind,
    source: CalendarSource,
    vendor_id: str,
    day: date,
    ticker: str | None = None,
    at: datetime | None = None,
    stamped: datetime = NOW - timedelta(days=3),
) -> None:
    """One live row a fetch could withdraw, written ``stamped``."""
    with Session(db_engine) as session:
        upsert_events(
            session,
            [
                CalendarEventInput(
                    kind=kind,
                    source=source,
                    vendor_id=vendor_id,
                    title="pre-seeded",
                    date=day,
                    at=at,
                    ticker=ticker,
                )
            ],
            now=stamped,
        )
        session.commit()


def _live(db_engine: Engine) -> set[tuple[str, str | None]]:
    with Session(db_engine) as session:
        rows = read_range(session, date(2026, 1, 1), date(2028, 12, 31))
    return {(row.kind.value, row.vendor_id) for row in rows}


class _ReplaceSpy:
    """Wraps the real ``replace_window`` and records every call's keywords."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session: Session, events: Sequence[CalendarEventInput], **kwargs: Any) -> Any:
        self.calls.append({"events": tuple(events), **kwargs})
        return calendar_store.replace_window(session, events, **kwargs)


@pytest.fixture
def replace_spy(monkeypatch: pytest.MonkeyPatch) -> _ReplaceSpy:
    spy = _ReplaceSpy()
    monkeypatch.setattr(scheduler_module, "replace_window", spy)
    return spy


def _scheduler(job: ScheduledJob) -> Scheduler:
    return Scheduler([job], secrets=lambda: (), clock=lambda: NOW)


# --------------------------------------------------------------------------
# Success: a complete fetch replaces its window
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_earnings_replace_the_finnhub_earnings_window(
    db_engine: Engine, replace_spy: _ReplaceSpy
) -> None:
    _seed_row(
        db_engine,
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id="cancelled",
        day=date(2026, 11, 18),
        ticker="AAPL",
    )
    finnhub = FakeFinnhubCalendar(earnings=[_earning(), _earning(symbol="ZZZZ")])
    job = _jobs(_services(db_engine, finnhub=finnhub))["calendar_earnings"]

    assert await job.run() is None

    assert finnhub.calls == [("earnings", TODAY, TODAY + EARNINGS_WINDOW)]
    (call,) = replace_spy.calls
    assert call["source"] is CalendarSource.FINNHUB
    assert call["kinds"] == frozenset({CalendarKind.EARNINGS})
    assert (call["window_start"], call["window_end"]) == (TODAY, TODAY + EARNINGS_WINDOW)
    assert call["now"] == NOW
    # The watch filter ran: ZZZZ is not watched.
    assert [event.ticker for event in call["events"]] == ["NVDA"]
    live = _live(db_engine)
    assert ("earnings", "cancelled") not in live
    assert any(kind == "earnings" for kind, _ in live)


@pytest.mark.asyncio
async def test_ipos_replace_the_finnhub_ipo_window(
    db_engine: Engine, replace_spy: _ReplaceSpy
) -> None:
    _seed_row(
        db_engine,
        kind=CalendarKind.IPO,
        source=CalendarSource.FINNHUB,
        vendor_id="name:old listing",
        day=date(2026, 11, 20),
    )
    finnhub = FakeFinnhubCalendar(ipos=[_ipo()])
    job = _jobs(_services(db_engine, finnhub=finnhub))["calendar_ipo"]

    assert await job.run() is None

    assert finnhub.calls == [("ipo", TODAY, TODAY + IPO_WINDOW)]
    (call,) = replace_spy.calls
    assert call["source"] is CalendarSource.FINNHUB
    assert call["kinds"] == frozenset({CalendarKind.IPO})
    assert (call["window_start"], call["window_end"]) == (TODAY, TODAY + IPO_WINDOW)
    live = _live(db_engine)
    assert ("ipo", "name:old listing") not in live
    assert len([1 for kind, _ in live if kind == "ipo"]) == 1


@pytest.mark.asyncio
async def test_announced_dividends_replace_the_kept_ex_date_window(
    db_engine: Engine, replace_spy: _ReplaceSpy
) -> None:
    _seed_row(
        db_engine,
        kind=CalendarKind.DIVIDEND,
        source=CalendarSource.ALPACA,
        vendor_id="div-gone",
        day=date(2026, 11, 25),
        ticker="AAPL",
    )
    dividends = FakeDividends(CashDividendRead(dividends=(_dividend(),), skipped=()))
    job = _jobs(_services(db_engine, dividends=dividends))["calendar_dividends"]

    assert await job.run() is None

    (call,) = replace_spy.calls
    assert call["source"] is CalendarSource.ALPACA
    assert call["kinds"] == frozenset({CalendarKind.DIVIDEND})
    assert (call["window_start"], call["window_end"]) == (
        TODAY,
        TODAY + DIVIDEND_EX_DATE_HORIZON,
    )
    # The watch universe the dividends were asked for: Markets, plus nothing
    # else in this test (no seed, no positions, no manual watches).
    ((symbols, _, _),) = dividends.calls
    assert symbols == ("AAPL", "NVDA")
    assert _live(db_engine) == {("dividend", "div-aapl")}


@pytest.mark.asyncio
async def test_none_announced_is_a_real_answer_and_empties_the_window(
    db_engine: Engine, replace_spy: _ReplaceSpy
) -> None:
    _seed_row(
        db_engine,
        kind=CalendarKind.DIVIDEND,
        source=CalendarSource.ALPACA,
        vendor_id="div-gone",
        day=date(2026, 11, 25),
        ticker="AAPL",
    )
    job = _jobs(_services(db_engine, dividends=FakeDividends()))["calendar_dividends"]

    assert await job.run() is None

    (call,) = replace_spy.calls
    assert call["events"] == ()
    assert _live(db_engine) == set()


@pytest.mark.asyncio
async def test_release_dates_replace_the_fred_economic_window(
    db_engine: Engine, replace_spy: _ReplaceSpy
) -> None:
    # A rescheduled CPI: the old date's row goes, the new one arrives.
    old_cpi = datetime(2026, 11, 12, 13, 30, tzinfo=UTC)
    _seed_row(
        db_engine,
        kind=CalendarKind.ECONOMIC,
        source=CalendarSource.FRED,
        vendor_id="10:2026-11-12",
        day=date(2026, 11, 12),
        at=old_cpi,
    )
    releases = FakeReleases([_cpi(date(2026, 11, 13))])
    job = _jobs(_services(db_engine, releases=releases))["calendar_releases"]

    assert await job.run() is None

    assert releases.calls == [(TODAY, TODAY + timedelta(days=30))]
    (call,) = replace_spy.calls
    assert call["source"] is CalendarSource.FRED
    assert call["kinds"] == frozenset({CalendarKind.ECONOMIC})
    assert (call["window_start"], call["window_end"]) == (TODAY, TODAY + timedelta(days=30))
    assert _live(db_engine) == {("economic", "10:2026-11-13")}


@pytest.mark.asyncio
async def test_the_central_bank_seed_imports_this_year_and_next(db_engine: Engine) -> None:
    job = _jobs(_services(db_engine))["calendar_central_banks"]

    assert await job.run() is None

    with Session(db_engine) as session:
        years = {
            row.date.year
            for row in session.scalars(
                select(CalendarEvent).where(
                    CalendarEvent.kind == CalendarKind.CENTRAL_BANK.value,
                    CalendarEvent.deleted_at.is_(None),
                )
            )
        }
    assert years == {2026, 2027}
    # A second run is the same import: nothing changes, nothing fails.
    assert await job.run() is None


@pytest.mark.asyncio
async def test_a_year_with_no_central_bank_seed_file_is_a_skip(db_engine: Engine) -> None:
    job = _jobs(_services(db_engine), now=datetime(2031, 3, 2, 15, 0, tzinfo=UTC))[
        "calendar_central_banks"
    ]
    outcome = await job.run()
    assert isinstance(outcome, JobSkipped)
    assert "2031" in outcome.reason and "2032" in outcome.reason
    assert _live(db_engine) == set()


# --------------------------------------------------------------------------
# Failure: nothing is withdrawn
# --------------------------------------------------------------------------


def _seed_one_of_each(db_engine: Engine) -> set[tuple[str, str | None]]:
    _seed_row(
        db_engine,
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id="NVDA:2027Q3",
        day=date(2026, 11, 19),
        ticker="NVDA",
    )
    _seed_row(
        db_engine,
        kind=CalendarKind.IPO,
        source=CalendarSource.FINNHUB,
        vendor_id="symbol:IAM",
        day=date(2026, 11, 20),
    )
    _seed_row(
        db_engine,
        kind=CalendarKind.DIVIDEND,
        source=CalendarSource.ALPACA,
        vendor_id="div-aapl",
        day=date(2026, 11, 20),
        ticker="AAPL",
    )
    _seed_row(
        db_engine,
        kind=CalendarKind.ECONOMIC,
        source=CalendarSource.FRED,
        vendor_id="10:2026-11-12",
        day=date(2026, 11, 12),
        at=datetime(2026, 11, 12, 13, 30, tzinfo=UTC),
    )
    return _live(db_engine)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("calendar_earnings", CalendarProviderError("finnhub 502")),
        ("calendar_earnings", CalendarAccessDenied("premium")),
        ("calendar_ipo", CalendarProviderError("finnhub 502")),
        ("calendar_ipo", CalendarAccessDenied("premium")),
        ("calendar_dividends", ProviderError("alpaca 500")),
        ("calendar_releases", FredError("fred 500")),
    ],
)
async def test_a_failed_fetch_raises_and_withdraws_nothing(
    db_engine: Engine, replace_spy: _ReplaceSpy, name: str, error: Exception
) -> None:
    before = _seed_one_of_each(db_engine)
    services = _services(
        db_engine,
        finnhub=FakeFinnhubCalendar(error=error),
        dividends=FakeDividends(error=error),
        releases=FakeReleases(error=error),
    )
    job = _jobs(services)[name]

    with pytest.raises(Exception) as raised:
        await job.run()

    if name != "calendar_dividends":
        assert raised.value is error
    assert replace_spy.calls == []
    assert _live(db_engine) == before


@pytest.mark.asyncio
async def test_a_failed_dividend_read_is_a_failure_never_an_empty_window(
    db_engine: Engine, replace_spy: _ReplaceSpy
) -> None:
    before = _seed_one_of_each(db_engine)
    job = _jobs(_services(db_engine, dividends=FakeDividends(error=ProviderError("429"))))[
        "calendar_dividends"
    ]
    scheduler = _scheduler(job)
    record = scheduler._records[job.name]
    await scheduler._run_catch_up(job, job.run, record)

    status = scheduler.status()[job.name]
    assert status.failures == 1 and status.runs == 0 and status.skips == 0
    assert status.last_error_type == "DividendsNotFetched"
    assert replace_spy.calls == []
    assert _live(db_engine) == before


@pytest.mark.asyncio
async def test_a_skipped_release_fetch_withdraws_nothing(
    db_engine: Engine, replace_spy: _ReplaceSpy
) -> None:
    """FRED answered, and none of what it listed is a timed release: a skip."""
    before = _seed_one_of_each(db_engine)
    untimed = FredReleaseDate(release_id=441, release_name="Coinbase", date=TODAY)
    job = _jobs(_services(db_engine, releases=FakeReleases([untimed])))["calendar_releases"]

    outcome = await job.run()

    assert isinstance(outcome, JobSkipped)
    assert replace_spy.calls == []
    assert _live(db_engine) == before


# --------------------------------------------------------------------------
# Partial: upsert what was read, withdraw nothing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_earnings_row_makes_the_fetch_partial(
    db_engine: Engine, replace_spy: _ReplaceSpy, caplog: pytest.LogCaptureFixture
) -> None:
    """The unreadable row's key is unknown, so its stored row must not be withdrawn."""
    _seed_row(
        db_engine,
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id="AAPL:2027Q1",
        day=date(2026, 11, 18),
        ticker="AAPL",
    )
    rows = [_earning(), _earning(symbol="AAPL", quarter=1, epsEstimate="not a number")]
    job = _jobs(_services(db_engine, finnhub=FakeFinnhubCalendar(earnings=rows)))[
        "calendar_earnings"
    ]
    with caplog.at_level(logging.WARNING, logger=scheduler_module.__name__):
        assert await job.run() is None

    assert replace_spy.calls == []
    live = _live(db_engine)
    assert ("earnings", "AAPL:2027Q1") in live
    assert len([1 for kind, _ in live if kind == "earnings"]) == 2
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "calendar_window_kept"]
    assert record.job == "calendar_earnings"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_unreadable_ipo_row_makes_the_fetch_partial(
    db_engine: Engine, replace_spy: _ReplaceSpy
) -> None:
    _seed_row(
        db_engine,
        kind=CalendarKind.IPO,
        source=CalendarSource.FINNHUB,
        vendor_id="name:old listing",
        day=date(2026, 11, 20),
    )
    rows = [_ipo(), _ipo(symbol="BAD", name="Bad", price="cheap")]
    job = _jobs(_services(db_engine, finnhub=FakeFinnhubCalendar(ipos=rows)))["calendar_ipo"]
    assert await job.run() is None
    assert replace_spy.calls == []
    assert ("ipo", "name:old listing") in _live(db_engine)


@pytest.mark.asyncio
async def test_an_unreadable_dividend_row_makes_the_fetch_partial(
    db_engine: Engine, replace_spy: _ReplaceSpy
) -> None:
    _seed_row(
        db_engine,
        kind=CalendarKind.DIVIDEND,
        source=CalendarSource.ALPACA,
        vendor_id="div-unreadable",
        day=date(2026, 11, 25),
        ticker="AAPL",
    )
    read = CashDividendRead(
        dividends=(_dividend(),),
        skipped=(
            SkippedDividend(
                vendor_id="div-unreadable",
                symbol="AAPL",
                ex_date=date(2026, 11, 25),
                reason="rate is not a number",
            ),
        ),
    )
    job = _jobs(_services(db_engine, dividends=FakeDividends(read)))["calendar_dividends"]
    assert await job.run() is None
    assert replace_spy.calls == []
    assert _live(db_engine) == {("dividend", "div-unreadable"), ("dividend", "div-aapl")}


@pytest.mark.asyncio
async def test_a_skipped_release_date_makes_the_fetch_partial(
    db_engine: Engine,
    replace_spy: _ReplaceSpy,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused release input's key is known but its row is not written, so
    replacing would withdraw its stored predecessor: upsert only."""
    before = _seed_one_of_each(db_engine)

    async def mapped(fred: object, *, today: date, times: object = None) -> ReleaseEvents:
        return ReleaseEvents(
            start=today,
            end=today + timedelta(days=30),
            events=(),
            ignored=(),
            unscheduled=(),
            name_mismatches=(),
            skipped=(SkippedRelease(release_id=10, date=date(2026, 11, 12), reason="too wide"),),
        )

    monkeypatch.setattr(scheduler_module, "fetch_release_events", mapped)
    job = _jobs(_services(db_engine, releases=FakeReleases()))["calendar_releases"]
    with caplog.at_level(logging.WARNING, logger=scheduler_module.__name__):
        assert await job.run() is None

    assert replace_spy.calls == []
    assert _live(db_engine) == before
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "calendar_window_kept"]
    assert record.job == "calendar_releases"  # type: ignore[attr-defined]


async def _broken_positions() -> frozenset[str]:
    raise RuntimeError("the paper account could not be read")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["calendar_earnings", "calendar_dividends"])
async def test_a_watch_universe_without_its_positions_withdraws_nothing(
    db_engine: Engine, replace_spy: _ReplaceSpy, name: str
) -> None:
    """A position underlying left out of one cycle must not lose its calendar rows."""
    before = _seed_one_of_each(db_engine)
    services = _services(
        db_engine,
        finnhub=FakeFinnhubCalendar(earnings=[]),
        dividends=FakeDividends(),
        position_underlyings=_broken_positions,
    )
    assert await _jobs(services)[name].run() is None
    assert replace_spy.calls == []
    assert _live(db_engine) == before


# The production positions reader, over a holder in each state the refresher
# can leave it in. ``HeldPositionUnderlyings`` never raises, so the universe
# build's ``positions_error`` cannot say "never read" or "stale" -- the
# calendar jobs read the holder's ``as_of`` themselves (audit, unit 7.2c-2).


def _held(*symbols: str, as_of: datetime | None) -> HeldPositionUnderlyings:
    holder = PositionUnderlyings()
    if as_of is not None:
        holder.replace(symbols, at=as_of)
    return HeldPositionUnderlyings(holder)


def _seed_held_pltr(db_engine: Engine) -> None:
    """A held PLTR position's earnings and ex-dividend rows -- PLTR is in no
    other watch-universe member, so only the positions put it there."""
    _seed_row(
        db_engine,
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id="PLTR:2026Q4",
        day=date(2026, 11, 16),
        ticker="PLTR",
    )
    _seed_row(
        db_engine,
        kind=CalendarKind.DIVIDEND,
        source=CalendarSource.ALPACA,
        vendor_id="div-pltr",
        day=date(2026, 11, 20),
        ticker="PLTR",
    )


_HELD_STATES: dict[str, tuple[datetime | None, str]] = {
    "never read": (None, "never been read"),
    "stale": (NOW - HELD_POSITIONS_STALE_AFTER - timedelta(seconds=1), "last read at"),
}


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("state", sorted(_HELD_STATES))
@pytest.mark.parametrize("name", ["calendar_earnings", "calendar_dividends"])
async def test_a_held_positions_read_that_is_missing_or_stale_withdraws_nothing(
    db_engine: Engine,
    replace_spy: _ReplaceSpy,
    caplog: pytest.LogCaptureFixture,
    name: str,
    state: str,
) -> None:
    """A restart before the first paper read, or a refresher failing all day,
    must not let a "complete" fetch withdraw a held position's calendar rows:
    a missing ex-date is a short call's early-assignment risk."""
    _seed_held_pltr(db_engine)
    as_of, said = _HELD_STATES[state]
    services = _services(
        db_engine,
        finnhub=FakeFinnhubCalendar(earnings=[_earning()]),
        dividends=FakeDividends(CashDividendRead(dividends=(_dividend(),), skipped=())),
        position_underlyings=_held("NVDA", as_of=as_of),
    )
    with caplog.at_level(logging.WARNING, logger=scheduler_module.__name__):
        assert await _jobs(services)[name].run() is None

    assert replace_spy.calls == []
    live = _live(db_engine)
    assert {("earnings", "PLTR:2026Q4"), ("dividend", "div-pltr")} <= live
    # What the fetch did read is still written.
    fetched = ("earnings", "NVDA:2027Q3") if name == "calendar_earnings" else ("dividend", "div-aapl")
    assert fetched in live
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "calendar_window_kept"]
    assert record.job == name  # type: ignore[attr-defined]
    gaps: list[str] = record.gaps  # type: ignore[attr-defined]
    assert any(said in gap for gap in gaps), gaps


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "withdrawn"),
    [
        ("calendar_earnings", ("earnings", "PLTR:2026Q4")),
        ("calendar_dividends", ("dividend", "div-pltr")),
    ],
)
async def test_a_held_positions_read_exactly_at_the_bound_is_fresh_and_replaces(
    db_engine: Engine, replace_spy: _ReplaceSpy, name: str, withdrawn: tuple[str, str]
) -> None:
    """The boundary permits: read exactly ``HELD_POSITIONS_STALE_AFTER`` ago,
    the universe is complete, and a row nothing in it holds is withdrawn."""
    _seed_held_pltr(db_engine)
    services = _services(
        db_engine,
        finnhub=FakeFinnhubCalendar(earnings=[_earning()]),
        dividends=FakeDividends(CashDividendRead(dividends=(_dividend(),), skipped=())),
        position_underlyings=_held("NVDA", as_of=NOW - HELD_POSITIONS_STALE_AFTER),
    )
    assert await _jobs(services)[name].run() is None

    assert len(replace_spy.calls) == 1
    assert withdrawn not in _live(db_engine)


@pytest.mark.risk
def test_the_held_positions_bound_is_twice_the_refresh_interval() -> None:
    """One missed refresh is tolerated; two in a row is a gap."""
    assert HELD_POSITIONS_STALE_AFTER == 2 * POSITION_UNDERLYINGS_TTL


# --------------------------------------------------------------------------
# No key: a skip, never a success
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "variable"),
    [
        ("calendar_earnings", "FINNHUB_API_KEY"),
        ("calendar_ipo", "FINNHUB_API_KEY"),
        ("calendar_dividends", "ALPACA_PAPER_API_KEY"),
        ("calendar_releases", "FRED_API_KEY"),
    ],
)
async def test_without_a_vendor_the_job_and_its_catch_up_skip_and_never_succeed(
    db_engine: Engine, replace_spy: _ReplaceSpy, name: str, variable: str
) -> None:
    before = _seed_one_of_each(db_engine)
    job = _jobs(_services(db_engine))[name]
    assert job.catch_up is not None
    scheduler = _scheduler(job)
    record = scheduler._records[job.name]
    await scheduler._run_catch_up(job, job.run, record)
    await scheduler._run_catch_up(job, job.catch_up, record)

    status = scheduler.status()[name]
    assert status.skips == 2
    assert status.runs == 0 and status.last_success is None and status.failures == 0
    assert status.last_skip_reason is not None and variable in status.last_skip_reason
    assert replace_spy.calls == []
    assert _live(db_engine) == before


# --------------------------------------------------------------------------
# A premium refusal is surfaced as itself (Q15)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["calendar_earnings", "calendar_ipo"])
async def test_a_premium_refusal_is_its_own_error_type_and_its_own_log_event(
    db_engine: Engine, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    finnhub = FakeFinnhubCalendar(error=CalendarAccessDenied("403: not on this plan"))
    job = _jobs(_services(db_engine, finnhub=finnhub))[name]
    scheduler = _scheduler(job)
    with caplog.at_level(logging.ERROR, logger=scheduler_module.__name__):
        await scheduler._run_catch_up(job, job.run, scheduler._records[job.name])

    status = scheduler.status()[name]
    assert status.failing
    assert status.last_error_type == "CalendarAccessDenied"
    (refusal,) = [
        r for r in caplog.records if getattr(r, "event", None) == "calendar_access_denied"
    ]
    assert refusal.job == name  # type: ignore[attr-defined]
    assert "Q15" in refusal.rule  # type: ignore[attr-defined]
    # And the scheduler's own failure line, with the job's rule and inputs.
    assert any(getattr(r, "event", None) == "context_job_failed" for r in caplog.records)


# --------------------------------------------------------------------------
# The clocks
# --------------------------------------------------------------------------


def test_the_calendar_jobs_run_at_their_slots() -> None:
    jobs = context_jobs(ContextServices(session_factory=lambda: None))  # type: ignore[arg-type, return-value]
    by_name = {job.name: job for job in jobs}
    assert by_name["calendar_earnings"].schedule == AtTime(time(7, 0), every_day)
    assert by_name["calendar_ipo"].schedule == AtTime(time(7, 5), every_day)
    assert by_name["calendar_dividends"].schedule == AtTime(time(7, 10), every_day)
    assert by_name["calendar_releases"].schedule == AtTime(time(10, 0), trading_days)
    assert by_name["calendar_central_banks"].schedule == AtTime(time(6, 45), every_day)
    for name in CALENDAR_JOBS:
        assert by_name[name].catch_up is not None, name


def test_the_vendor_calendars_run_on_thanksgiving_and_fred_waits_for_the_half_day() -> None:
    jobs = {
        job.name: job
        for job in context_jobs(ContextServices(session_factory=lambda: None))  # type: ignore[arg-type, return-value]
    }
    wednesday_evening = datetime(2026, 11, 26, 0, 0, tzinfo=UTC)  # Wed 19:00 EST
    # Thanksgiving, Thu 26 Nov: the vendor calendars run (07:00 EST = 12:00Z).
    assert jobs["calendar_earnings"].schedule.next_run(wednesday_evening) == datetime(
        2026, 11, 26, 12, 0, tzinfo=UTC
    )
    assert jobs["calendar_dividends"].schedule.next_run(wednesday_evening) == datetime(
        2026, 11, 26, 12, 10, tzinfo=UTC
    )
    # FRED's slot is on trading days: the holiday is skipped and the
    # half-day Friday runs at 10:00 EST, before its 13:00 close.
    assert jobs["calendar_releases"].schedule.next_run(wednesday_evening) == datetime(
        2026, 11, 27, 15, 0, tzinfo=UTC
    )


def test_the_calendar_slots_stay_on_eastern_time_across_the_dst_change() -> None:
    jobs = {
        job.name: job
        for job in context_jobs(ContextServices(session_factory=lambda: None))  # type: ignore[arg-type, return-value]
    }
    earnings, ipo = jobs["calendar_earnings"].schedule, jobs["calendar_ipo"].schedule
    friday_night = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)
    # Sat 31 Oct, still EDT: 07:00 ET is 11:00Z.
    first = earnings.next_run(friday_night)
    assert first == datetime(2026, 10, 31, 11, 0, tzinfo=UTC)
    # Sun 1 Nov, EST from 02:00: 07:00 ET is 12:00Z -- 25 hours later.
    assert first is not None
    assert earnings.next_run(first) == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert ipo.next_run(datetime(2026, 11, 1, 11, 30, tzinfo=UTC)) == datetime(
        2026, 11, 1, 12, 5, tzinfo=UTC
    )
    # FRED: Fri 30 Oct (EDT) 14:00Z, then Mon 2 Nov (EST) 15:00Z.
    releases = jobs["calendar_releases"].schedule
    assert releases.next_run(datetime(2026, 10, 30, 12, 0, tzinfo=UTC)) == datetime(
        2026, 10, 30, 14, 0, tzinfo=UTC
    )
    assert releases.next_run(datetime(2026, 10, 30, 14, 0, tzinfo=UTC)) == datetime(
        2026, 11, 2, 15, 0, tzinfo=UTC
    )


# --------------------------------------------------------------------------
# Start-up catch-up
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_catch_up_on_an_empty_table_fetches(db_engine: Engine) -> None:
    finnhub = FakeFinnhubCalendar(earnings=[_earning()], ipos=[_ipo()])
    releases = FakeReleases([_cpi(date(2026, 11, 13))])
    dividends = FakeDividends(CashDividendRead(dividends=(_dividend(),), skipped=()))
    jobs = _jobs(_services(db_engine, finnhub=finnhub, dividends=dividends, releases=releases))
    for name in ("calendar_earnings", "calendar_ipo", "calendar_dividends", "calendar_releases"):
        catch_up = jobs[name].catch_up
        assert catch_up is not None
        assert await catch_up() is None, name
    assert [call[0] for call in finnhub.calls] == ["earnings", "ipo"]
    assert len(dividends.calls) == 1 and len(releases.calls) == 1


@pytest.mark.asyncio
async def test_a_catch_up_on_rows_written_within_a_day_skips_and_asks_nothing(
    db_engine: Engine,
) -> None:
    fresh = NOW - CALENDAR_CATCH_UP_AFTER + timedelta(minutes=1)
    for kind, source, vendor_id, ticker in (
        (CalendarKind.EARNINGS, CalendarSource.FINNHUB, "NVDA:2027Q3", "NVDA"),
        (CalendarKind.IPO, CalendarSource.FINNHUB, "symbol:IAM", None),
        (CalendarKind.DIVIDEND, CalendarSource.ALPACA, "div-aapl", "AAPL"),
    ):
        _seed_row(
            db_engine,
            kind=kind,
            source=source,
            vendor_id=vendor_id,
            day=date(2026, 11, 20),
            ticker=ticker,
            stamped=fresh,
        )
    _seed_row(
        db_engine,
        kind=CalendarKind.ECONOMIC,
        source=CalendarSource.FRED,
        vendor_id="10:2026-11-12",
        day=date(2026, 11, 12),
        at=datetime(2026, 11, 12, 13, 30, tzinfo=UTC),
        stamped=fresh,
    )
    finnhub, dividends, releases = FakeFinnhubCalendar(), FakeDividends(), FakeReleases()
    jobs = _jobs(_services(db_engine, finnhub=finnhub, dividends=dividends, releases=releases))
    for name in ("calendar_earnings", "calendar_ipo", "calendar_dividends", "calendar_releases"):
        catch_up = jobs[name].catch_up
        assert catch_up is not None
        outcome = await catch_up()
        assert isinstance(outcome, JobSkipped), name
        assert fresh.isoformat() in outcome.reason, name
        # The skip carries the instant, so a reader can say "up to date" in
        # its own zone instead of quoting this UTC text; the period is words.
        assert outcome.fresh_as_of == fresh, name
        assert "less than 1 day ago" in outcome.reason, name
        assert "0:00:00" not in outcome.reason, name
    assert finnhub.calls == [] and dividends.calls == [] and releases.calls == []


@pytest.mark.asyncio
async def test_a_fresh_catch_up_is_recorded_apart_from_a_real_skip(db_engine: Engine) -> None:
    """``last_skip_fresh_as_of`` marks the calm skip, and a later real skip clears it."""
    fresh = NOW - timedelta(hours=2)
    _seed_row(
        db_engine,
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id="NVDA:2027Q3",
        day=date(2026, 11, 20),
        ticker="NVDA",
        stamped=fresh,
    )
    job = _jobs(_services(db_engine, finnhub=FakeFinnhubCalendar()))["calendar_earnings"]
    assert job.catch_up is not None
    scheduler = _scheduler(job)
    record = scheduler._records[job.name]

    await scheduler._run_catch_up(job, job.catch_up, record)
    assert scheduler.status()[job.name].last_skip_fresh_as_of == fresh

    no_vendor = _jobs(_services(db_engine))["calendar_earnings"]
    await scheduler._run_catch_up(no_vendor, no_vendor.run, record)
    status = scheduler.status()[job.name]
    assert status.skips == 2
    assert status.last_skip_fresh_as_of is None


@pytest.mark.asyncio
async def test_a_catch_up_on_rows_older_than_a_day_fetches(db_engine: Engine) -> None:
    _seed_row(
        db_engine,
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id="NVDA:2027Q3",
        day=date(2026, 11, 19),
        ticker="NVDA",
        stamped=NOW - CALENDAR_CATCH_UP_AFTER - timedelta(minutes=1),
    )
    # Fresh rows of *another* kind from the same vendor do not count.
    _seed_row(
        db_engine,
        kind=CalendarKind.IPO,
        source=CalendarSource.FINNHUB,
        vendor_id="symbol:IAM",
        day=date(2026, 11, 20),
        stamped=NOW,
    )
    finnhub = FakeFinnhubCalendar(earnings=[_earning()])
    catch_up = _jobs(_services(db_engine, finnhub=finnhub))["calendar_earnings"].catch_up
    assert catch_up is not None
    assert await catch_up() is None
    assert [call[0] for call in finnhub.calls] == ["earnings"]


@pytest.mark.asyncio
async def test_the_catch_ups_run_inside_the_scheduler_before_the_first_slot(
    db_engine: Engine,
) -> None:
    """Under the real :class:`Scheduler`: the central-bank seed imports at start,
    and the vendor catch-ups fetch into an empty table."""
    finnhub = FakeFinnhubCalendar(earnings=[_earning()], ipos=[_ipo()])
    services = _services(
        db_engine,
        finnhub=finnhub,
        dividends=FakeDividends(CashDividendRead(dividends=(_dividend(),), skipped=())),
        releases=FakeReleases([_cpi(date(2026, 11, 13))]),
    )
    jobs = [job for job in context_jobs(services, clock=lambda: NOW) if job.name in CALENDAR_JOBS]
    never = asyncio.Event()

    async def parked(seconds: float) -> None:
        await never.wait()

    scheduler = Scheduler(jobs, secrets=lambda: (), clock=lambda: NOW, sleep=parked)
    scheduler.start()
    try:
        await asyncio.wait_for(scheduler.ready(), timeout=10)
        for _ in range(500):
            status = scheduler.status()
            if all(status[name].runs for name in CALENDAR_JOBS):
                break
            await asyncio.sleep(0.01)
    finally:
        await scheduler.aclose()

    status = scheduler.status()
    for name in CALENDAR_JOBS:
        assert status[name].runs == 1, (name, status[name])
        assert status[name].failures == 0, (name, status[name])
    kinds = {kind for kind, _ in _live(db_engine)}
    assert kinds == {"earnings", "ipo", "dividend", "economic", "central-bank"}


@pytest.mark.asyncio
async def test_a_quiet_feed_and_an_empty_feed_each_cost_one_request_per_restart(
    db_engine: Engine,
) -> None:
    """The stated deviation (no fetch record): a feed that changes nothing
    leaves no newer stamp, so every restart fetches once -- and a feed that
    lists nothing at all costs exactly the same, never more."""
    empty = FakeFinnhubCalendar(ipos=[])
    for _restart in range(3):
        catch_up = _jobs(_services(db_engine, finnhub=empty))["calendar_ipo"].catch_up
        assert catch_up is not None
        assert await catch_up() is None
    assert len(empty.calls) == 3

    quiet = FakeFinnhubCalendar(earnings=[_earning()])
    written = NOW - CALENDAR_CATCH_UP_AFTER - timedelta(hours=1)
    first = _jobs(_services(db_engine, finnhub=quiet), now=written)["calendar_earnings"]
    assert await first.run() is None
    for _restart in range(3):
        catch_up = _jobs(_services(db_engine, finnhub=quiet))["calendar_earnings"].catch_up
        assert catch_up is not None
        assert await catch_up() is None
    assert len(quiet.calls) == 1 + 3
    assert ("earnings", "NVDA:2027Q3") in _live(db_engine)


# --------------------------------------------------------------------------
# Off the loop: every database touch and blocking call of the shipped bodies
# --------------------------------------------------------------------------

#: How long each spied call blocks. A spied call made on the event-loop
#: thread stalls the loop this long, which the lag probe below sees.
_BLOCK_SECONDS = 0.25
_TICK_SECONDS = 0.01


class _ThreadSpy:
    """Wraps blocking callables; records which thread each call ran on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def wrap(self, name: str, inner: Any) -> Any:
        def spied(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, threading.get_ident()))
            time_module.sleep(_BLOCK_SECONDS)
            return inner(*args, **kwargs)

        return spied


async def _run_measuring_lag(body: JobBody) -> tuple[JobSkipped | None, float]:
    """Run ``body`` while a ticker on the same loop measures its worst stall."""
    stop = asyncio.Event()
    worst = 0.0

    async def ticker() -> None:
        nonlocal worst
        last = time_module.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(_TICK_SECONDS)
            now = time_module.perf_counter()
            worst = max(worst, now - last)
            last = now

    probe = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        outcome = await body()
    finally:
        stop.set()
        await probe
    return outcome, worst


#: What each job's catch-up, on an empty table, must reach -- so a spy that is
#: never hit cannot pass the test vacuously. Earnings and dividends run over
#: a never-read held holder, so they take the partial path (``upsert_events``).
_EXPECTED_BLOCKING = {
    "calendar_earnings": {"session_factory", "seed_loader", "earnings_events", "upsert_events"},
    "calendar_ipo": {"session_factory", "ipo_events", "replace_window"},
    "calendar_dividends": {"session_factory", "seed_loader", "upsert_events"},
    "calendar_releases": {"session_factory", "load_econ_release_times", "replace_window"},
    "calendar_central_banks": {"session_factory", "import_central_bank_year"},
}


@pytest.mark.risk
@pytest.mark.asyncio
@pytest.mark.parametrize("name", CALENDAR_JOBS)
async def test_each_shipped_calendar_body_blocks_only_off_the_event_loop(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The hang test in ``test_scheduler.py`` replaces the bodies; this runs
    the real ones. Every session, mapper, file read and store write must run
    on a worker thread, and the loop -- the one rule 9's heartbeat runs on --
    must never stall while a body runs.

    The pure mappers inside ``fetch_dividends`` and ``fetch_release_events``
    (``dividend_events``, ``release_events``) run on the loop by design: in
    memory, over one response, no I/O.
    """
    spy = _ThreadSpy()
    for attr in (
        "earnings_events",
        "ipo_events",
        "load_econ_release_times",
        "import_central_bank_year",
        "upsert_events",
        "replace_window",
    ):
        monkeypatch.setattr(scheduler_module, attr, spy.wrap(attr, getattr(scheduler_module, attr)))
    monkeypatch.setattr(
        releases_module,
        "load_econ_release_times",
        spy.wrap("fetcher_load_econ_release_times", releases_module.load_econ_release_times),
    )
    services = ContextServices(
        session_factory=spy.wrap("session_factory", lambda: Session(db_engine)),
        finnhub_calendar=FakeFinnhubCalendar(earnings=[_earning()], ipos=[_ipo()]),
        dividends=FakeDividends(CashDividendRead(dividends=(_dividend(),), skipped=())),
        fred_releases=FakeReleases([_cpi(date(2026, 11, 13))]),
        markets=("AAPL", "NVDA"),
        seed_loader=spy.wrap("seed_loader", lambda: None),
        position_underlyings=_held(as_of=None),
    )
    catch_up = _jobs(services)[name].catch_up
    assert catch_up is not None
    loop_thread = threading.get_ident()

    outcome, worst = await _run_measuring_lag(catch_up)

    assert outcome is None
    reached = {called for called, _ in spy.calls}
    assert _EXPECTED_BLOCKING[name] <= reached, reached
    on_loop = [called for called, thread in spy.calls if thread == loop_thread]
    assert on_loop == [], on_loop
    assert worst < _BLOCK_SECONDS * 0.6, f"the loop stalled {worst:.3f}s while {name} ran"


# --------------------------------------------------------------------------
# What a fetch is dated by: its own start, recorded, never inferred
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_success_keeps_its_own_start_after_a_later_run_starts_and_fails() -> None:
    """Audit finding 2, under the real :class:`Scheduler`.

    The catch-up starts 23:59 EST on the 9th and finishes 00:00:20 on the
    10th; the 07:00 slot run then starts and fails. ``last_started`` is the
    slot run's now, so the success's start must be on the record by itself:
    the route dates coverage from it.
    """
    started = datetime(2026, 11, 10, 4, 59, tzinfo=UTC)  # 23:59 EST on the 9th
    finished = datetime(2026, 11, 10, 5, 0, 20, tzinfo=UTC)  # 00:00:20 EST on the 10th
    slot = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # 07:00 EST on the 10th
    clock = [started]
    parked = asyncio.Event()
    never = asyncio.Event()

    async def catch_up() -> JobSkipped | None:
        clock[0] = finished
        return None

    async def run() -> JobSkipped | None:
        clock[0] = slot + timedelta(seconds=30)
        raise TimeoutError("the slot run failed")

    sleeps = 0

    async def sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            clock[0] = slot
            return
        parked.set()
        await never.wait()

    job = ScheduledJob(
        name="calendar_earnings",
        schedule=AtTime(time(7, 0), every_day),
        run=run,
        catch_up=catch_up,
        rule="test",
    )
    scheduler = Scheduler([job], secrets=lambda: (), clock=lambda: clock[0], sleep=sleep)
    scheduler.start()
    try:
        await asyncio.wait_for(parked.wait(), timeout=10)
    finally:
        await scheduler.aclose()

    status = scheduler.status()["calendar_earnings"]
    assert status.last_started == slot
    assert status.last_success == finished
    assert status.last_success_started == started
    assert status.failing


@pytest.mark.asyncio
async def test_a_skip_leaves_the_last_successes_start_alone(db_engine: Engine) -> None:
    job = _jobs(_services(db_engine, finnhub=FakeFinnhubCalendar(earnings=[_earning()])))[
        "calendar_earnings"
    ]
    scheduler = _scheduler(job)
    record = scheduler._records[job.name]
    await scheduler._run_catch_up(job, job.run, record)
    assert scheduler.status()[job.name].last_success_started == NOW

    no_vendor = _jobs(_services(db_engine))["calendar_earnings"]
    await scheduler._run_catch_up(no_vendor, no_vendor.run, record)

    status = scheduler.status()[job.name]
    assert status.skips == 1
    assert status.last_success_started == NOW


class _ClockMovingFinnhub(FakeFinnhubCalendar):
    """Moves the job's clock past midnight ET while the fetch is in flight."""

    def __init__(self, clock: list[datetime], after: datetime, **rows: Any) -> None:
        super().__init__(**rows)
        self._clock = clock
        self._after = after

    async def earnings_calendar(self, start: date, end: date) -> list[Any]:
        self._clock[0] = self._after
        return await super().earnings_calendar(start, end)


@pytest.mark.asyncio
async def test_a_fetchs_rows_are_stamped_with_the_clock_read_before_the_fetch(
    db_engine: Engine,
) -> None:
    """Audit note 3: ``fresh_at_start`` dates coverage from the newest row's
    ``updated_at``, which is honest only because that stamp is the run's
    pre-fetch ``now`` -- the same instant the window was dated from. A run
    straddling midnight ET still stamps the earlier day.
    """
    before = datetime(2026, 11, 10, 4, 59, tzinfo=UTC)  # 23:59 EST on the 9th
    after = datetime(2026, 11, 10, 5, 0, 20, tzinfo=UTC)  # 00:00:20 EST on the 10th
    clock = [before]
    finnhub = _ClockMovingFinnhub(clock, after, earnings=[_earning()])
    services = _services(db_engine, finnhub=finnhub)
    job = {
        job.name: job for job in context_jobs(services, clock=lambda: clock[0])
    }["calendar_earnings"]

    assert await job.run() is None

    assert clock[0] == after  # the fetch did move the clock
    assert finnhub.calls == [("earnings", date(2026, 11, 9), date(2026, 11, 9) + EARNINGS_WINDOW)]
    with Session(db_engine) as session:
        stamps = session.scalars(select(CalendarEvent.updated_at)).all()
    assert stamps and all(stamp == before for stamp in stamps)


@pytest.mark.parametrize(
    ("years_ahead", "words"),
    [(0, "this year;"), (1, "this year and next;"), (2, "this year and the 2 after;")],
)
def test_the_central_bank_rule_names_the_years_the_constant_imports(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, years_ahead: int, words: str
) -> None:
    monkeypatch.setattr(scheduler_module, "CALENDAR_CENTRAL_BANK_YEARS_AHEAD", years_ahead)

    rule = _jobs(_services(db_engine))["calendar_central_banks"].rule

    assert words in rule
