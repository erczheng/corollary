"""Vendor window replacement: ``replace_window`` (Phase 3 step 7, unit 7.2c-1).

The stale-row gap all three fetcher audits reported: a vendor row that
disappears from a fresh, complete fetch was never withdrawn. These tests pin
the replacement's semantics -- upsert, then soft-delete the live rows of that
source and those kinds in the window that the fetch no longer lists -- and
every refusal, which must write nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.data.calendar import (
    WindowReplace,
    WindowReplaceError,
    create_manual,
    read_range,
    replace_window,
    upsert_events,
)
from corollary.data.calendar_event import (
    CalendarEventInput,
    CalendarKind,
    CalendarSource,
    IpoStatus,
)
from corollary.db.models import Base, CalendarEvent
from corollary.db.session import create_db_engine, sqlite_url

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 11, 14, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
START = date(2026, 10, 10)
END = date(2026, 10, 31)

EARNINGS = frozenset({CalendarKind.EARNINGS})
IPO = frozenset({CalendarKind.IPO})
ECONOMIC = frozenset({CalendarKind.ECONOMIC})


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        yield sess


def _earnings(symbol: str = "NVDA", day: date = date(2026, 10, 20)) -> CalendarEventInput:
    return CalendarEventInput(
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id=f"{symbol}:2026Q3",
        title=f"{symbol} earnings",
        date=day,
        ticker=symbol,
        estimate=Decimal("1.25"),
        unit="EPS",
    )


def _ipo(vendor_id: str, ticker: str | None, day: date = date(2026, 10, 15)) -> CalendarEventInput:
    return CalendarEventInput(
        kind=CalendarKind.IPO,
        source=CalendarSource.FINNHUB,
        vendor_id=vendor_id,
        title="New Iceland Arctic Acquisition Corp.",
        date=day,
        ticker=ticker,
        ipo_status=IpoStatus.EXPECTED,
    )


def _release(release_id: int, day: date) -> CalendarEventInput:
    at = datetime.combine(day, time(8, 30), tzinfo=ET).astimezone(UTC)
    return CalendarEventInput(
        kind=CalendarKind.ECONOMIC,
        source=CalendarSource.FRED,
        vendor_id=f"{release_id}:{day.isoformat()}",
        title="Consumer Price Index",
        date=day,
        at=at,
    )


def _dividend(vendor_id: str = "ca-1", day: date = date(2026, 10, 20)) -> CalendarEventInput:
    return CalendarEventInput(
        kind=CalendarKind.DIVIDEND,
        source=CalendarSource.ALPACA,
        vendor_id=vendor_id,
        title="Ex-dividend date",
        date=day,
        ticker="AAPL",
        actual=Decimal("0.26"),
        unit="USD/share",
    )


def _row(session: Session, source: CalendarSource, vendor_id: str) -> CalendarEvent:
    row = session.scalars(
        select(CalendarEvent).where(
            CalendarEvent.source == source.value, CalendarEvent.vendor_id == vendor_id
        )
    ).one()
    return row


def _live_keys(session: Session) -> set[str | None]:
    return {
        row.vendor_id
        for row in session.scalars(select(CalendarEvent).where(CalendarEvent.deleted_at.is_(None)))
    }


def _replace(
    session: Session,
    events: list[CalendarEventInput],
    *,
    source: CalendarSource = CalendarSource.FINNHUB,
    kinds: frozenset[CalendarKind] = EARNINGS,
    start: date = START,
    end: date = END,
    now: datetime = T1,
) -> WindowReplace:
    return replace_window(
        session,
        events,
        source=source,
        kinds=kinds,
        window_start=start,
        window_end=end,
        now=now,
    )


# --- the three stale-row cases the audits reported ----------------------------


def test_a_cancelled_earnings_report_is_withdrawn(session: Session) -> None:
    _replace(session, [_earnings("NVDA"), _earnings("AAPL")], now=T0)
    result = _replace(session, [_earnings("NVDA")])
    assert result == WindowReplace(
        inserted=0,
        updated=0,
        unchanged=1,
        withdrawn=1,
        revived=0,
        withdrawn_keys=((CalendarKind.EARNINGS, "AAPL:2026Q3"),),
    )
    aapl = _row(session, CalendarSource.FINNHUB, "AAPL:2026Q3")
    assert aapl.deleted_at == T1
    assert aapl.updated_at == T1
    assert [e.ticker for e in read_range(session, START, END)] == ["NVDA"]


def test_a_rekeyed_ipo_withdraws_its_name_keyed_row(session: Session) -> None:
    named = _ipo("name:new iceland arctic acquisition corp", None)
    _replace(session, [named], kinds=IPO, now=T0)
    symbol = _ipo("symbol:NIAAU", "NIAAU")
    result = _replace(session, [symbol], kinds=IPO)
    assert (result.inserted, result.withdrawn) == (1, 1)
    assert result.withdrawn_keys == ((CalendarKind.IPO, named.vendor_id),)
    assert [e.vendor_id for e in read_range(session, START, END)] == ["symbol:NIAAU"]


def test_a_rescheduled_release_withdraws_the_old_date(session: Session) -> None:
    old, new = _release(10, date(2026, 10, 14)), _release(10, date(2026, 10, 15))
    _replace(session, [old], source=CalendarSource.FRED, kinds=ECONOMIC, now=T0)
    result = _replace(session, [new], source=CalendarSource.FRED, kinds=ECONOMIC)
    assert (result.inserted, result.withdrawn) == (1, 1)
    live = read_range(session, START, END)
    assert [(e.vendor_id, e.date) for e in live] == [("10:2026-10-15", date(2026, 10, 15))]


# --- revival ------------------------------------------------------------------


def test_a_withdrawn_row_that_reappears_is_revived_not_reinserted(session: Session) -> None:
    _replace(session, [_earnings("NVDA"), _earnings("AAPL")], now=T0)
    _replace(session, [_earnings("NVDA")], now=T1)
    first_id = _row(session, CalendarSource.FINNHUB, "AAPL:2026Q3").id

    result = _replace(session, [_earnings("NVDA"), _earnings("AAPL")], now=T2)
    assert (result.inserted, result.updated, result.unchanged, result.revived) == (0, 0, 1, 1)
    assert result.withdrawn == 0
    aapl = _row(session, CalendarSource.FINNHUB, "AAPL:2026Q3")
    assert aapl.id == first_id
    assert aapl.deleted_at is None
    assert aapl.updated_at == T2


def test_a_revived_row_with_new_content_is_counted_once_as_revived(session: Session) -> None:
    _replace(session, [_earnings("AAPL")], now=T0)
    _replace(session, [], now=T1)
    moved = _earnings("AAPL", day=date(2026, 10, 22))
    result = _replace(session, [moved], now=T2)
    assert (result.inserted, result.updated, result.unchanged, result.revived) == (0, 0, 0, 1)
    assert _row(session, CalendarSource.FINNHUB, "AAPL:2026Q3").date == date(2026, 10, 22)


def test_the_counts_partition_the_batch_and_flush_without_committing(
    session: Session, engine: Engine
) -> None:
    _replace(session, [_earnings("NVDA"), _earnings("AAPL"), _earnings("MSFT")], now=T0)
    _replace(session, [_earnings("NVDA"), _earnings("AAPL")], now=T1)  # MSFT withdrawn
    session.commit()
    changed = replace(_earnings("AAPL"), estimate=Decimal("1.30"))
    batch = [_earnings("NVDA"), changed, _earnings("MSFT"), _earnings("AMD")]
    result = _replace(session, batch, now=T2)
    assert (result.inserted, result.updated, result.unchanged, result.revived) == (1, 1, 1, 1)
    assert result.inserted + result.updated + result.unchanged + result.revived == len(batch)
    # flushed: visible in this session; not committed: invisible to another
    assert _row(session, CalendarSource.FINNHUB, "AMD:2026Q3").deleted_at is None
    with Session(engine) as other:
        assert other.scalars(
            select(CalendarEvent).where(CalendarEvent.vendor_id == "AMD:2026Q3")
        ).first() is None


def test_an_empty_complete_fetch_withdraws_the_whole_window(session: Session) -> None:
    _replace(session, [_earnings("NVDA"), _earnings("AAPL")], now=T0)
    result = _replace(session, [])
    assert result.withdrawn == 2
    assert result.withdrawn_keys == (
        (CalendarKind.EARNINGS, "AAPL:2026Q3"),
        (CalendarKind.EARNINGS, "NVDA:2026Q3"),
    )
    assert read_range(session, START, END) == ()


# --- what it must leave alone -------------------------------------------------


def test_rows_outside_the_window_are_untouched(session: Session) -> None:
    before = _earnings("AAPL", day=date(2026, 10, 9))
    after = _earnings("MSFT", day=date(2026, 11, 1))
    edge_low = _earnings("AMD", day=START)
    edge_high = _earnings("INTC", day=END)
    upsert_events(session, [before, after, edge_low, edge_high], now=T0)
    result = _replace(session, [])
    # Both ends of the window are inclusive; one day past either end is outside.
    assert {key for _, key in result.withdrawn_keys} == {"AMD:2026Q3", "INTC:2026Q3"}
    assert _live_keys(session) == {"AAPL:2026Q3", "MSFT:2026Q3"}


def test_other_sources_and_kinds_are_untouched(session: Session) -> None:
    upsert_events(
        session,
        [
            _earnings("NVDA"),
            _ipo("symbol:IAM", "IAM", day=date(2026, 10, 20)),
            _release(10, date(2026, 10, 20)),
            _dividend("ca-1", date(2026, 10, 20)),
        ],
        now=T0,
    )
    result = _replace(session, [], kinds=EARNINGS)
    assert result.withdrawn_keys == ((CalendarKind.EARNINGS, "NVDA:2026Q3"),)
    assert _live_keys(session) == {"symbol:IAM", "10:2026-10-20", "ca-1"}


def test_several_kinds_of_one_source_are_replaced_together(session: Session) -> None:
    upsert_events(session, [_earnings("NVDA"), _ipo("symbol:IAM", "IAM")], now=T0)
    result = _replace(session, [_earnings("NVDA")], kinds=EARNINGS | IPO)
    assert result.withdrawn_keys == ((CalendarKind.IPO, "symbol:IAM"),)


def test_manual_and_seed_rows_are_never_touched(session: Session) -> None:
    manual = create_manual(session, title="G7 summit", date=date(2026, 10, 20), now=T0)
    seed = CalendarEventInput(
        kind=CalendarKind.CENTRAL_BANK,
        source=CalendarSource.SEED,
        vendor_id="FOMC:2026-10-28",
        title="FOMC policy decision",
        date=date(2026, 10, 28),
    )
    upsert_events(session, [seed], now=T0)
    _replace(session, [], kinds=EARNINGS)
    _replace(session, [], source=CalendarSource.FRED, kinds=ECONOMIC)
    live = read_range(session, START, END)
    assert {e.source for e in live} == {CalendarSource.MANUAL, CalendarSource.SEED}
    assert manual.id in {e.id for e in live}


def test_a_listed_row_already_withdrawn_elsewhere_is_not_double_counted(
    session: Session,
) -> None:
    _replace(session, [_earnings("AAPL")], now=T0)
    _replace(session, [], now=T1)
    result = _replace(session, [], now=T2)
    assert result.withdrawn == 0
    assert _row(session, CalendarSource.FINNHUB, "AAPL:2026Q3").deleted_at == T1


# --- refusals: each writes nothing --------------------------------------------


def _refused(session: Session, **kwargs: object) -> WindowReplaceError:
    upsert_events(session, [_earnings("AAPL")], now=T0)
    with pytest.raises(WindowReplaceError) as info:
        _replace(session, **kwargs)  # type: ignore[arg-type]
    # The pre-existing row is still live: a refused call withdraws nothing.
    assert _live_keys(session) == {"AAPL:2026Q3"}
    assert _row(session, CalendarSource.FINNHUB, "AAPL:2026Q3").updated_at == T0
    return info.value


def test_an_event_before_the_window_is_refused(session: Session) -> None:
    err = _refused(session, events=[_earnings("NVDA", day=date(2026, 10, 9))])
    assert "NVDA:2026Q3" in str(err)


def test_an_event_after_the_window_is_refused(session: Session) -> None:
    _refused(session, events=[_earnings("NVDA", day=date(2026, 11, 1))])


def test_an_event_of_another_source_is_refused(session: Session) -> None:
    _refused(session, events=[_dividend()], kinds=EARNINGS)


def test_an_event_of_a_kind_outside_kinds_is_refused(session: Session) -> None:
    _refused(session, events=[_ipo("symbol:IAM", "IAM")], kinds=EARNINGS)


def test_a_kind_the_source_does_not_produce_is_refused(session: Session) -> None:
    _refused(session, events=[], kinds=frozenset({CalendarKind.DIVIDEND}))


@pytest.mark.parametrize("source", [CalendarSource.MANUAL, CalendarSource.SEED])
def test_manual_and_seed_sources_are_refused(session: Session, source: CalendarSource) -> None:
    kind = CalendarKind.GEOPOLITICAL if source is CalendarSource.MANUAL else CalendarKind.CENTRAL_BANK
    _refused(session, events=[], source=source, kinds=frozenset({kind}))


def test_empty_kinds_are_refused(session: Session) -> None:
    _refused(session, events=[], kinds=frozenset())


def test_an_inverted_window_is_refused(session: Session) -> None:
    _refused(session, events=[], start=END, end=START)


def test_a_key_named_twice_is_refused(session: Session) -> None:
    _refused(session, events=[_earnings("NVDA"), _earnings("NVDA")])


def test_a_bad_event_late_in_the_batch_writes_nothing(session: Session) -> None:
    good = _earnings("NVDA")
    bad = _earnings("MSFT", day=date(2026, 11, 2))
    _refused(session, events=[good, bad])
    assert "NVDA:2026Q3" not in _live_keys(session)


def test_a_naive_now_is_refused(session: Session) -> None:
    with pytest.raises(ValueError):
        _replace(session, [], now=datetime(2026, 10, 11, 14, 0))


def test_the_error_is_a_value_error() -> None:
    assert issubclass(WindowReplaceError, ValueError)
