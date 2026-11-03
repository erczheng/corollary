"""A Cash session confirmed mid-day reaches the calendar the same day (cash-session unit 2b).

The calendar's earnings and dividends jobs run at 07:00 and 07:10 ET, and a
Cash session exists only once the owner confirms it -- never at start. Before
this unit a session confirmed at 08:00 joined the calendar the next morning,
and its first Cash read could wait up to five minutes. Now:

* the Cash positions refresher looks for a change of session every
  :data:`CASH_SESSION_POLL` and reads at once on one;
* after the first read under a session that found an underlying, it wakes the
  two jobs through a :class:`JobWake` -- never the scheduler -- and each job's
  own task runs its woken body once, over the Cash-only tickers;
* the scheduler caps a job's woken runs at one per
  :data:`WOKEN_RUN_MIN_INTERVAL`, so a second confirmation inside the hour is
  read but runs nothing; and the woken run leaves the next slot where it was.

Rule 5 (marked ``risk``): no session, nothing read and nothing woken; a
session that ends while its read is in flight wakes nothing.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from corollary.api.deps import CASH_SESSION_POLL, POSITION_UNDERLYINGS_TTL, CashPositionsRefresher
from corollary.data.calendar_event import CalendarKind, CalendarSource
from corollary.engine.cash_session import CashSessionHolder, CashSessionReader
from corollary.engine.execution.interface import BrokerPosition
from corollary.engine.scheduler import (
    CASH_SESSION_WAKE_JOBS,
    WOKEN_RUN_MIN_INTERVAL,
    CashPositionUnderlyings,
    HeldCashUnderlyings,
    JobWake,
    Scheduler,
    context_jobs,
)
from tests.api.test_cash_positions_refresher import _registry
from tests.api.test_news_wiring import _held
from tests.engine.test_calendar_cash_scope import CASH_ONLY, _NEW_PLTR, _scoped_services
from tests.engine.test_calendar_jobs import (
    NOW,
    _live,
    _ReplaceSpy,
    _seed_row,
    db_engine,  # noqa: F401 -- a fixture, used by name
    replace_spy,  # noqa: F401 -- a fixture, used by name
)
from tests.engine.test_scheduler import FakeClock

MASK = "••••1234"


def _confirm(sessions: CashSessionHolder, at: datetime, correlation_id: str = "cid-confirm") -> None:
    sessions.confirm(
        account_number_masked=MASK,
        balance=Decimal("25000.00"),
        at=at,
        correlation_id=correlation_id,
        generation=sessions.generation,
    )


def _events() -> tuple[JobWake, dict[str, asyncio.Event]]:
    events = {name: asyncio.Event() for name in CASH_SESSION_WAKE_JOBS}
    return JobWake(tuple(events.items())), events


class Ticks:
    """An injected sleep that records each sleep and runs a scripted action after one."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self.after: dict[int, Callable[[], None]] = {}
        self.reached = asyncio.Event()
        self.stop_at: int | None = None

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        action = self.after.pop(len(self.slept), None)
        if action is not None:
            action()
        if self.stop_at is not None and len(self.slept) >= self.stop_at:
            self.reached.set()
            await asyncio.Event().wait()  # parked until aclose cancels it
        await asyncio.sleep(0)


class CashBook:
    """``positions()``, noting how many sleeps the refresher had taken at each read."""

    def __init__(self, ticks: Ticks, rows: list[BrokerPosition]) -> None:
        self.ticks = ticks
        self.rows = rows
        self.read_after: list[int] = []
        self.during_read: Callable[[], None] | None = None

    async def positions(self) -> list[BrokerPosition]:
        self.read_after.append(len(self.ticks.slept))
        if self.during_read is not None:
            self.during_read()
        return list(self.rows)


def _looping(
    book: CashBook, sessions: CashSessionHolder, ticks: Ticks, wake: JobWake
) -> tuple[CashPositionsRefresher, CashPositionUnderlyings]:
    holder = CashPositionUnderlyings()
    refresher = CashPositionsRefresher(
        registry=_registry(book),
        session=CashSessionReader(sessions),
        holder=holder,
        clock=lambda: NOW,
        sleep=ticks,
        wake=wake,
    )
    return refresher, holder


async def _run_until(refresher: CashPositionsRefresher, ticks: Ticks, sleeps: int) -> None:
    ticks.stop_at = sleeps
    refresher.start()
    try:
        await asyncio.wait_for(ticks.reached.wait(), timeout=10)
    finally:
        await refresher.aclose()


# --------------------------------------------------------------------------
# The refresher: read at once on a new session, wake once for it
# --------------------------------------------------------------------------


def test_the_poll_is_seconds_and_the_cap_an_hour() -> None:
    assert CASH_SESSION_POLL <= timedelta(seconds=5)
    assert POSITION_UNDERLYINGS_TTL % CASH_SESSION_POLL == timedelta(0)
    assert WOKEN_RUN_MIN_INTERVAL == timedelta(hours=1)


@pytest.mark.risk
@pytest.mark.asyncio
async def test_without_a_session_the_loop_reads_nothing_and_wakes_nothing() -> None:
    ticks = Ticks()
    wake, events = _events()
    book = CashBook(ticks, [_held(CASH_ONLY, "us_equity")])
    refresher, holder = _looping(book, CashSessionHolder(), ticks, wake)
    await _run_until(refresher, ticks, sleeps=3 * int(POSITION_UNDERLYINGS_TTL / CASH_SESSION_POLL))
    assert book.read_after == []
    assert not any(event.is_set() for event in events.values())
    assert holder.as_of is None


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_mid_interval_confirmation_is_read_within_one_poll_and_wakes_once() -> None:
    ticks = Ticks()
    wake, events = _events()
    sessions = CashSessionHolder()
    book = CashBook(ticks, [_held(CASH_ONLY, "us_equity")])
    refresher, holder = _looping(book, sessions, ticks, wake)
    ticks.after[10] = lambda: _confirm(sessions, NOW)
    looks = int(POSITION_UNDERLYINGS_TTL / CASH_SESSION_POLL)
    await _run_until(refresher, ticks, sleeps=10 + 2 * looks + 1)
    # Confirmed during sleep 10; read straight after it, not an interval later.
    assert book.read_after[0] == 10
    assert sum(ticks.slept[:10]) < POSITION_UNDERLYINGS_TTL.total_seconds()
    # Then on the interval as before -- under the same session, never re-woken.
    assert book.read_after == [10, 10 + looks, 10 + 2 * looks]
    assert all(event.is_set() for event in events.values())
    assert holder.as_of is None  # aclose cleared it


@pytest.mark.asyncio
async def test_a_session_is_woken_for_once_even_across_many_reads() -> None:
    sessions = CashSessionHolder()
    _confirm(sessions, NOW)
    calls: list[tuple[str, ...]] = []
    wake, _ = _events()
    holder = CashPositionUnderlyings()
    refresher = CashPositionsRefresher(
        registry=_registry(CashBook(Ticks(), [_held(CASH_ONLY, "us_equity")])),
        session=CashSessionReader(sessions),
        holder=holder,
        clock=lambda: NOW,
        wake=wake,
    )
    original = wake.__call__

    def counting() -> tuple[str, ...]:
        calls.append(original())
        return calls[-1]

    refresher._wake = counting  # type: ignore[assignment]  # observe the one capability
    assert await refresher.refresh() is True
    assert await refresher.refresh() is True
    assert calls == [CASH_SESSION_WAKE_JOBS]
    # A new confirmation is a new session: woken for again (the hourly cap is
    # the scheduler's, pinned below and in tests/engine/test_scheduler_wake.py).
    sessions.end()
    _confirm(sessions, NOW, correlation_id="cid-again")
    assert await refresher.refresh() is True
    assert calls == [CASH_SESSION_WAKE_JOBS, CASH_SESSION_WAKE_JOBS]


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_session_that_ends_during_its_read_wakes_nothing() -> None:
    sessions = CashSessionHolder()
    _confirm(sessions, NOW)
    wake, events = _events()
    book = CashBook(Ticks(), [_held(CASH_ONLY, "us_equity")])
    book.during_read = sessions.end
    holder = CashPositionUnderlyings()
    refresher = CashPositionsRefresher(
        registry=_registry(book),
        session=CashSessionReader(sessions),
        holder=holder,
        clock=lambda: NOW,
        wake=wake,
    )
    assert await refresher.refresh() is False
    assert not any(event.is_set() for event in events.values())
    assert holder.as_of is None


@pytest.mark.asyncio
async def test_an_empty_read_wakes_nothing_and_a_later_holding_still_wakes() -> None:
    sessions = CashSessionHolder()
    _confirm(sessions, NOW)
    wake, events = _events()
    book = CashBook(Ticks(), [])
    refresher = CashPositionsRefresher(
        registry=_registry(book),
        session=CashSessionReader(sessions),
        holder=CashPositionUnderlyings(),
        clock=lambda: NOW,
        wake=wake,
    )
    assert await refresher.refresh() is True
    assert not any(event.is_set() for event in events.values())
    book.rows = [_held(CASH_ONLY, "us_equity")]
    assert await refresher.refresh() is True
    assert all(event.is_set() for event in events.values())


@pytest.mark.asyncio
async def test_a_wake_that_raises_is_logged_and_the_read_stands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = CashSessionHolder()
    _confirm(sessions, NOW)
    holder = CashPositionUnderlyings()
    refresher = CashPositionsRefresher(
        registry=_registry(CashBook(Ticks(), [_held(CASH_ONLY, "us_equity")])),
        session=CashSessionReader(sessions),
        holder=holder,
        clock=lambda: NOW,
    )

    def broken() -> tuple[str, ...]:
        raise RuntimeError("boom")

    refresher._wake = broken  # type: ignore[assignment]
    with caplog.at_level(logging.ERROR):
        assert await refresher.refresh() is True
    assert holder.current() == frozenset({CASH_ONLY})
    (line,) = [
        r for r in caplog.records if getattr(r, "event", None) == "cash_session_calendar_wake_failed"
    ]
    assert line.error_type == "RuntimeError"  # type: ignore[attr-defined]


def test_a_poll_longer_than_the_interval_or_not_positive_is_refused() -> None:
    for poll in (timedelta(0), POSITION_UNDERLYINGS_TTL + timedelta(seconds=1)):
        with pytest.raises(ValueError):
            CashPositionsRefresher(
                registry=_registry(None),
                session=CashSessionReader(CashSessionHolder()),
                holder=CashPositionUnderlyings(),
                poll=poll,
            )


# --------------------------------------------------------------------------
# End to end: the refresher, the scheduler and the two real calendar jobs
# --------------------------------------------------------------------------


async def _until(condition: Callable[[], bool], timeout: float = 10) -> None:
    async def wait() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=timeout)


async def _drive_for(clock: FakeClock, span: timedelta) -> None:
    clock.stop_at = clock.now + span
    clock.parked.clear()
    await asyncio.wait_for(clock.drive(), timeout=10)


def _events_named(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == event]


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_mid_morning_confirmation_reaches_the_calendar_once_and_leaves_the_slots(
    db_engine: Engine,
    replace_spy: _ReplaceSpy,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """08:00 ET, after both slots. Confirm: one Cash read within a poll, one
    woken earnings and one woken dividends run over PLTR alone (NVDA is held
    in Cash too, but watched). Confirm again inside the hour: read, never
    re-run. Neither moves tomorrow's 07:00 and 07:10 slots."""
    clock = FakeClock(NOW, stop_at=NOW)
    sessions = CashSessionHolder()
    reader = CashSessionReader(sessions)
    holder = CashPositionUnderlyings()
    services = _scoped_services(db_engine, HeldCashUnderlyings(holder=holder, session=reader))
    shipped = {job.name: job for job in context_jobs(services, clock=clock)}
    # The start-up catch-ups are not the subject; without them the only
    # calendar writes are the woken runs.
    jobs = [dataclasses.replace(shipped[name], catch_up=None) for name in CASH_SESSION_WAKE_JOBS]
    scheduler = Scheduler(jobs, secrets=lambda: (), clock=clock, sleep=clock.sleep)
    ticks = Ticks()
    book = CashBook(ticks, [_held(CASH_ONLY, "us_equity"), _held("NVDA", "us_equity")])
    read_at: list[datetime] = []
    real_positions = book.positions

    async def positions() -> list[BrokerPosition]:
        read_at.append(clock())
        return await real_positions()

    book.positions = positions  # type: ignore[method-assign]
    refresher = CashPositionsRefresher(
        registry=_registry(book),
        session=reader,
        holder=holder,
        clock=clock,
        sleep=clock.sleep,
        wake=scheduler.waker(CASH_SESSION_WAKE_JOBS),
    )
    with caplog.at_level(logging.INFO):
        scheduler.start()
        await asyncio.wait_for(scheduler.ready(), timeout=10)
        refresher.start()
        try:
            await _drive_for(clock, timedelta(minutes=1))
            assert read_at == []  # no session, no read
            slots = {name: status.next_run for name, status in scheduler.status().items()}
            assert all(slot is not None and slot > NOW for slot in slots.values())

            confirmed = clock()
            _confirm(sessions, confirmed)
            await _drive_for(clock, timedelta(minutes=1))
            (first,) = read_at
            assert first - confirmed <= CASH_SESSION_POLL
            await _until(lambda: len(_events_named(caplog, "context_job_woken_ran")) == 2)

            assert sorted(call["tickers"] for call in replace_spy.calls) == [
                frozenset({CASH_ONLY}),
                frozenset({CASH_ONLY}),
            ]
            live = _live(db_engine)
            assert _NEW_PLTR["calendar_earnings"] in live
            assert _NEW_PLTR["calendar_dividends"] in live
            (asked,) = services.dividends.calls  # type: ignore[union-attr]
            assert asked[0] == (CASH_ONLY,)

            # A second confirmation inside the hour: read at once, run nothing.
            sessions.end()
            _confirm(sessions, clock(), correlation_id="cid-again")
            await _drive_for(clock, timedelta(minutes=1))
            assert len(read_at) == 2
            await _until(lambda: len(_events_named(caplog, "context_job_wake_capped")) == 2)
            assert len(replace_spy.calls) == 2

            status = scheduler.status()
            for name in CASH_SESSION_WAKE_JOBS:
                assert status[name].woken_runs == 1
                assert status[name].next_run == slots[name]  # not consumed
                assert status[name].runs == 0 and status[name].failures == 0
        finally:
            await refresher.aclose()
            await scheduler.aclose()
    assert holder.as_of is None


def _fail_while(fake: object, method: str, failing: dict[str, bool]) -> None:
    """Make ``fake.method`` raise while ``failing["on"]``: the vendor is down."""
    real = getattr(fake, method)

    async def call(*args: object, **kwargs: object) -> object:
        if failing["on"]:
            raise RuntimeError("vendor down")
        return await real(*args, **kwargs)

    setattr(fake, method, call)


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_woken_write_is_not_the_next_starts_catch_up_evidence(
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The catch-ups kept, across two lifespans (unit 2b audit, finding 1).
    First: the watch universe's rows are three days old and the start-up
    catch-up fails (the vendor is down), then the vendor recovers, a Cash
    confirm wakes a PLTR-only write. Second: a restart an hour on, with no
    Cash session. The only fresh row is the woken one, so the restart's
    catch-ups fetch rather than calling a failed sweep fresh."""
    for kind, source, vendor_id, ticker in (
        (CalendarKind.EARNINGS, CalendarSource.FINNHUB, "NVDA:2026Q1", "NVDA"),
        (CalendarKind.DIVIDEND, CalendarSource.ALPACA, "div-aapl-old", "AAPL"),
    ):
        _seed_row(
            db_engine,
            kind=kind,
            source=source,
            vendor_id=vendor_id,
            day=date(2026, 11, 17),
            ticker=ticker,
            stamped=NOW - timedelta(days=3),
        )
    clock = FakeClock(NOW, stop_at=NOW)
    sessions = CashSessionHolder()
    reader = CashSessionReader(sessions)
    holder = CashPositionUnderlyings()
    services = _scoped_services(db_engine, HeldCashUnderlyings(holder=holder, session=reader))
    failing = {"on": True}
    _fail_while(services.finnhub_calendar, "earnings_calendar", failing)
    _fail_while(services.dividends, "cash_dividends", failing)
    jobs = [job for job in context_jobs(services, clock=clock) if job.name in CASH_SESSION_WAKE_JOBS]
    assert all(job.catch_up is not None for job in jobs)
    scheduler = Scheduler(jobs, secrets=lambda: (), clock=clock, sleep=clock.sleep)
    book = CashBook(Ticks(), [_held(CASH_ONLY, "us_equity")])
    refresher = CashPositionsRefresher(
        registry=_registry(book),
        session=reader,
        holder=holder,
        clock=clock,
        sleep=clock.sleep,
        wake=scheduler.waker(CASH_SESSION_WAKE_JOBS),
    )
    with caplog.at_level(logging.INFO):
        scheduler.start()
        await asyncio.wait_for(scheduler.ready(), timeout=10)
        refresher.start()
        try:
            await _until(
                lambda: all(
                    scheduler.status()[name].failures == 1 for name in CASH_SESSION_WAKE_JOBS
                )
            )
            failing["on"] = False
            _confirm(sessions, clock())
            await _drive_for(clock, timedelta(minutes=1))
            await _until(lambda: len(_events_named(caplog, "context_job_woken_ran")) == 2)
            live = _live(db_engine)
            assert _NEW_PLTR["calendar_earnings"] in live
            assert _NEW_PLTR["calendar_dividends"] in live
            woken_at = clock()
        finally:
            await refresher.aclose()
            await scheduler.aclose()

    # The restart: a new process, no Cash session (rule 5), an hour on.
    restart_at = woken_at + timedelta(hours=1)
    restarted = _scoped_services(db_engine, None)
    later = FakeClock(restart_at, stop_at=restart_at)
    again = Scheduler(
        [job for job in context_jobs(restarted, clock=later) if job.name in CASH_SESSION_WAKE_JOBS],
        secrets=lambda: (),
        clock=later,
        sleep=later.sleep,
    )
    again.start()
    try:
        await asyncio.wait_for(again.ready(), timeout=10)
        await _until(
            lambda: all(again.status()[name].runs == 1 for name in CASH_SESSION_WAKE_JOBS)
        )
    finally:
        await again.aclose()
    status = again.status()
    for name in CASH_SESSION_WAKE_JOBS:
        assert status[name].skips == 0, (name, status[name])
    assert restarted.finnhub_calendar.calls != []  # type: ignore[union-attr]
    assert restarted.dividends.calls != []  # type: ignore[union-attr]
