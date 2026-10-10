"""The ``calendar_event`` storage layer (Phase 3 step 7, unit 7.2a).

Decisions 7, 8 and 9 and the spec's *Design -> Database* entry: vendor and
seed rows upsert by their key, the range read runs off ``date`` rather than
``at``'s UTC day, the seed's gaps stay readable, and geopolitical rows are
the only ones a person can create, edit or remove -- softly, and outside the
configuration audit log.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from corollary.data.calendar import (
    CalendarEventNotEditableError,
    CalendarEventNotFoundError,
    StoredCalendarEvent,
    UpsertCounts,
    central_bank_inputs,
    create_manual,
    delete_manual,
    group_by_date,
    import_central_bank_seed,
    import_central_bank_year,
    read_range,
    seed_gaps_between,
    update_manual,
    upsert_events,
)
from corollary.data.calendar_event import (
    CalendarEventInput,
    CalendarKind,
    CalendarSource,
    EarningsSession,
    IpoStatus,
)
from corollary.data.seeds.calendar_seeds import (
    Bank,
    GapKind,
    load_central_bank_seed,
    parse_central_bank_seed,
)
from corollary.db.models import AuditLog, Base, CalendarEvent
from corollary.db.session import create_db_engine, sqlite_url

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 11, 14, 0, tzinfo=UTC)
_URL = "https://example.invalid/schedule"


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


def _earnings(**overrides: object) -> CalendarEventInput:
    base = CalendarEventInput(
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id="NVDA:2026Q3",
        title="NVDA earnings",
        date=date(2026, 11, 19),
        ticker="NVDA",
        estimate=Decimal("1.2500"),
        unit="USD",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _dividend(**overrides: object) -> CalendarEventInput:
    base = CalendarEventInput(
        kind=CalendarKind.DIVIDEND,
        source=CalendarSource.ALPACA,
        vendor_id="ca-0001",
        title="AAPL ex-dividend",
        date=date(2026, 8, 12),
        ticker="AAPL",
        actual=Decimal("0.26"),
        unit="USD",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _rows(session: Session) -> list[CalendarEvent]:
    return list(session.scalars(select(CalendarEvent).order_by(CalendarEvent.id)))


# --- upsert -----------------------------------------------------------------


def test_upsert_is_idempotent(session: Session) -> None:
    first = upsert_events(session, [_earnings(), _dividend()], now=T0)
    session.commit()
    second = upsert_events(session, [_earnings(), _dividend()], now=T1)
    session.commit()

    assert first == UpsertCounts(inserted=2, updated=0, unchanged=0)
    assert second == UpsertCounts(inserted=0, updated=0, unchanged=2)
    rows = _rows(session)
    assert len(rows) == 2
    # An unchanged re-fetch does not touch updated_at.
    assert all(r.updated_at == T0 for r in rows)


def test_a_refetch_with_new_values_updates_in_place(session: Session) -> None:
    upsert_events(session, [_earnings()], now=T0)
    session.commit()
    # Rescheduled and reported: the same key, a new date and an actual.
    moved = _earnings(date=date(2026, 11, 20), actual=Decimal("1.31"))
    counts = upsert_events(session, [moved], now=T1)
    session.commit()

    assert counts == UpsertCounts(inserted=0, updated=1, unchanged=0)
    [row] = _rows(session)
    assert row.date == date(2026, 11, 20)
    assert row.actual == Decimal("1.31")
    assert row.created_at == T0
    assert row.updated_at == T1


def test_a_different_spelling_of_the_same_number_is_an_update(session: Session) -> None:
    # Decimal('1.25') == Decimal('1.2500'), but the stored text differs; the
    # row must end up holding what the vendor sent last.
    upsert_events(session, [_earnings(estimate=Decimal("1.2500"))], now=T0)
    session.commit()
    counts = upsert_events(session, [_earnings(estimate=Decimal("1.25"))], now=T1)
    session.commit()
    assert counts.updated == 1
    [row] = _rows(session)
    assert str(row.estimate) == "1.25"


def test_one_batch_naming_one_key_twice_is_refused(session: Session) -> None:
    with pytest.raises(ValueError, match="twice"):
        upsert_events(session, [_earnings(), _earnings(title="NVDA earnings call")], now=T0)


def test_upsert_needs_an_aware_now(session: Session) -> None:
    with pytest.raises(ValueError, match="aware"):
        upsert_events(session, [_earnings()], now=datetime(2026, 10, 10, 14, 0))


def test_money_columns_round_trip_exact_decimal(session: Session) -> None:
    upsert_events(
        session,
        [_earnings(estimate=Decimal("0.10"), prior=Decimal("-0.0300"), actual=Decimal("12.345678"))],
        now=T0,
    )
    session.commit()
    session.expire_all()
    [row] = _rows(session)
    for value, text in ((row.estimate, "0.10"), (row.prior, "-0.0300"), (row.actual, "12.345678")):
        assert type(value) is Decimal
        assert str(value) == text


# --- date-only rows and the range read ---------------------------------------


def test_a_date_only_row_stays_date_only(session: Session) -> None:
    upsert_events(session, [_dividend()], now=T0)
    session.commit()
    session.expire_all()
    [event] = read_range(session, date(2026, 8, 12), date(2026, 8, 12))
    assert event.at is None
    assert event.at_et() is None
    assert event.date == date(2026, 8, 12)


def test_an_earnings_session_round_trips_and_stays_date_only(session: Session) -> None:
    # Decision 7: "After close", not a placeholder 7:00 PM.
    upsert_events(session, [_earnings(session=EarningsSession.AMC)], now=T0)
    session.commit()
    session.expire_all()
    [event] = read_range(session, date(2026, 11, 19), date(2026, 11, 19))
    assert event.session is EarningsSession.AMC
    assert event.at is None
    [row] = _rows(session)
    assert row.session == "amc"


def test_an_earnings_session_defaults_to_none(session: Session) -> None:
    upsert_events(session, [_earnings(), _dividend()], now=T0)
    session.commit()
    session.expire_all()
    events = read_range(session, date(2026, 8, 1), date(2026, 11, 30))
    assert [e.session for e in events] == [None, None]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(None, EarningsSession.BMO, id="unknown -> before open"),
        pytest.param(EarningsSession.BMO, EarningsSession.AMC, id="before open -> after close"),
        pytest.param(EarningsSession.DMH, None, id="during hours -> unknown"),
    ],
)
def test_a_changed_session_is_an_update(
    session: Session, before: EarningsSession | None, after: EarningsSession | None
) -> None:
    upsert_events(session, [_earnings(session=before)], now=T0)
    session.commit()
    counts = upsert_events(session, [_earnings(session=after)], now=T1)
    session.commit()
    again = upsert_events(session, [_earnings(session=after)], now=T1)

    assert counts == UpsertCounts(inserted=0, updated=1, unchanged=0)
    assert again == UpsertCounts(inserted=0, updated=0, unchanged=1)
    [row] = _rows(session)
    assert row.session == (None if after is None else after.value)
    assert row.updated_at == T1


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: _dividend(session=EarningsSession.BMO), id="dividend"),
        pytest.param(
            lambda: CalendarEventInput(
                kind=CalendarKind.IPO,
                source=CalendarSource.FINNHUB,
                vendor_id="ipo-1",
                title="ACME IPO",
                date=date(2026, 11, 19),
                session=EarningsSession.BMO,
            ),
            id="ipo",
        ),
    ],
)
def test_a_session_on_a_non_earnings_input_is_refused(make: object) -> None:
    with pytest.raises(ValueError, match="session"):
        make()  # type: ignore[operator]


@pytest.mark.parametrize("bad", ["bmo", "", "pre"])
def test_a_session_that_is_not_the_enum_is_refused(bad: str) -> None:
    # The fetcher converts Finnhub's ``hour``; a raw string here is that
    # conversion skipped, and an empty one is the "unknown" it should map to None.
    with pytest.raises(ValueError, match="EarningsSession"):
        _earnings(session=bad)


def test_the_range_read_groups_by_date_not_by_the_utc_day_of_at(session: Session) -> None:
    # 20:30 ET on 11 Aug is 00:30Z on 12 Aug. It is an 11 Aug event.
    evening = datetime(2026, 8, 11, 20, 30, tzinfo=ET)
    late = _earnings(
        vendor_id="LATE:2026Q2",
        ticker="LATE",
        title="LATE earnings",
        date=date(2026, 8, 11),
        at=evening.astimezone(UTC),
    )
    assert late.at is not None and late.at.date() == date(2026, 8, 12)
    upsert_events(session, [late, _dividend()], now=T0)
    session.commit()

    eleventh = read_range(session, date(2026, 8, 11), date(2026, 8, 11))
    twelfth = read_range(session, date(2026, 8, 12), date(2026, 8, 12))
    assert [e.ticker for e in eleventh] == ["LATE"]
    assert [e.ticker for e in twelfth] == ["AAPL"]

    grouped = group_by_date(read_range(session, date(2026, 8, 1), date(2026, 8, 31)))
    assert list(grouped) == [date(2026, 8, 11), date(2026, 8, 12)]
    assert [e.ticker for e in grouped[date(2026, 8, 11)]] == ["LATE"]
    [shown] = grouped[date(2026, 8, 11)]
    at_et = shown.at_et()
    assert at_et is not None and at_et.time() == time(20, 30)


def test_a_timed_event_whose_date_is_not_its_et_day_is_refused() -> None:
    # The row the frontend's invariant forbids: a UTC day written as the date.
    with pytest.raises(ValueError, match="Eastern"):
        _earnings(date=date(2026, 8, 12), at=datetime(2026, 8, 12, 0, 30, tzinfo=UTC))


def test_the_range_is_inclusive_and_ordered(session: Session) -> None:
    upsert_events(
        session,
        [
            _earnings(vendor_id="B:1", ticker="B", title="B", date=date(2026, 11, 3)),
            _earnings(
                vendor_id="A:1",
                ticker="A",
                title="A",
                date=date(2026, 11, 2),
                at=datetime(2026, 11, 2, 16, 5, tzinfo=ET).astimezone(UTC),
            ),
            _dividend(vendor_id="ca-2", ticker="C", title="C", date=date(2026, 11, 2)),
            _earnings(vendor_id="Z:1", ticker="Z", title="Z", date=date(2026, 11, 4)),
        ],
        now=T0,
    )
    session.commit()
    got = read_range(session, date(2026, 11, 2), date(2026, 11, 3))
    # Date first; within a day the date-only (all-day) row leads the timed one.
    assert [e.ticker for e in got] == ["C", "A", "B"]


def test_an_inverted_range_is_refused(session: Session) -> None:
    with pytest.raises(ValueError, match="before"):
        read_range(session, date(2026, 11, 3), date(2026, 11, 2))


# --- manual geopolitical rows -------------------------------------------------


def test_manual_round_trip_create_edit_soft_delete(session: Session) -> None:
    created = create_manual(session, title="G20 summit", date=date(2026, 11, 14), now=T0)
    session.commit()
    assert created.kind is CalendarKind.GEOPOLITICAL
    assert created.source is CalendarSource.MANUAL
    assert created.vendor_id is None
    assert created.editable
    assert created.created_at == T0 and created.updated_at == T0

    at = datetime(2026, 11, 15, 9, 0, tzinfo=ET).astimezone(UTC)
    edited = update_manual(
        session, created.id, title="G20 leaders' summit", date=date(2026, 11, 15), at=at, now=T1
    )
    session.commit()
    assert edited.id == created.id
    assert edited.title == "G20 leaders' summit"
    assert edited.date == date(2026, 11, 15)
    assert edited.at == at
    assert edited.created_at == T0
    assert edited.updated_at == T1
    assert [e.id for e in read_range(session, date(2026, 11, 1), date(2026, 11, 30))] == [created.id]

    delete_manual(session, created.id, now=T1)
    session.commit()
    assert read_range(session, date(2026, 11, 1), date(2026, 11, 30)) == ()
    # Soft: the row is still there, stamped.
    [row] = _rows(session)
    assert row.deleted_at == T1
    # And gone for every further manual action.
    with pytest.raises(CalendarEventNotFoundError):
        update_manual(session, created.id, title="x", date=date(2026, 11, 15), now=T1)
    with pytest.raises(CalendarEventNotFoundError):
        delete_manual(session, created.id, now=T1)


def test_manual_rows_are_not_written_to_the_audit_log(session: Session) -> None:
    created = create_manual(session, title="Summit", date=date(2026, 11, 14), now=T0)
    update_manual(session, created.id, title="Summit, moved", date=date(2026, 11, 16), now=T1)
    delete_manual(session, created.id, now=T1)
    session.commit()
    assert session.scalar(select(func.count()).select_from(AuditLog)) == 0


@pytest.mark.parametrize("make", [_earnings, _dividend])
def test_editing_or_deleting_a_vendor_row_is_refused(session: Session, make: object) -> None:
    assert callable(make)
    upsert_events(session, [make()], now=T0)
    session.commit()
    [row] = _rows(session)
    with pytest.raises(CalendarEventNotEditableError) as edit:
        update_manual(session, row.id, title="hijacked", date=row.date, now=T1)
    assert edit.value.event_id == row.id
    with pytest.raises(CalendarEventNotEditableError):
        delete_manual(session, row.id, now=T1)
    session.expire_all()
    [after] = _rows(session)
    assert after.title == row.title
    assert after.deleted_at is None
    assert after.updated_at == T0


def test_editing_or_deleting_a_seed_row_is_refused(session: Session) -> None:
    seed = load_central_bank_seed(2026)
    assert seed is not None
    import_central_bank_seed(session, seed, now=T0)
    session.commit()
    row = _rows(session)[0]
    assert row.source == "seed"
    with pytest.raises(CalendarEventNotEditableError):
        update_manual(session, row.id, title="moved", date=row.date, now=T1)
    with pytest.raises(CalendarEventNotEditableError):
        delete_manual(session, row.id, now=T1)


def test_an_unknown_id_is_not_found(session: Session) -> None:
    with pytest.raises(CalendarEventNotFoundError):
        update_manual(session, 999, title="x", date=date(2026, 11, 1), now=T0)
    with pytest.raises(CalendarEventNotFoundError):
        delete_manual(session, 999, now=T0)


def test_the_two_errors_are_distinct() -> None:
    # The route layer maps them to different statuses (404 vs a refusal).
    assert not issubclass(CalendarEventNotEditableError, CalendarEventNotFoundError)
    assert not issubclass(CalendarEventNotFoundError, CalendarEventNotEditableError)


@pytest.mark.parametrize("title", ["", "   "])
def test_a_manual_row_needs_a_title(session: Session, title: str) -> None:
    with pytest.raises(ValueError, match="title"):
        create_manual(session, title=title, date=date(2026, 11, 14), now=T0)


def test_the_upsert_input_cannot_claim_to_be_manual() -> None:
    with pytest.raises(ValueError, match="manual"):
        CalendarEventInput(
            kind=CalendarKind.GEOPOLITICAL,
            source=CalendarSource.MANUAL,
            vendor_id="x",
            title="Summit",
            date=date(2026, 11, 14),
        )


# --- the seed import ----------------------------------------------------------


def _seed(events: list[str]) -> str:
    lines = [
        "year,2027",
        "bank,coverage,covers_from,retrieved,date_source_url,time_source_url,note",
        f"FOMC,full,,2026-10-10,{_URL},{_URL},",
        f"ECB,unpublished,,2026-10-10,{_URL},,ECB has not published 2027 dates",
        f"BOE,from,2027-03-01,2026-10-10,{_URL},,page had dropped earlier meetings",
        f"BOJ,full,,2026-10-10,{_URL},,",
        "bank,meeting_start,date,local_time,timezone,source_url,note",
        *events,
    ]
    return "\n".join(lines) + "\n"


_FOMC_JAN = f"FOMC,2027-01-26,2027-01-27,14:00,America/New_York,{_URL},"
_FOMC_MAR = f"FOMC,2027-03-16,2027-03-17,14:00,America/New_York,{_URL},"
_BOE_MAR = f"BOE,2027-03-18,2027-03-18,,Europe/London,{_URL},"
_BOJ_JAN = f"BOJ,2027-01-21,2027-01-22,,Asia/Tokyo,{_URL},"


def test_seed_import_writes_central_bank_rows(session: Session) -> None:
    seed = parse_central_bank_seed(_seed([_FOMC_JAN, _BOE_MAR, _BOJ_JAN]))
    counts = import_central_bank_seed(session, seed, now=T0)
    session.commit()
    assert counts == UpsertCounts(inserted=3, updated=0, unchanged=0, withdrawn=0)
    events = read_range(session, date(2027, 1, 1), date(2027, 12, 31))
    assert all(e.kind is CalendarKind.CENTRAL_BANK for e in events)
    assert all(e.source is CalendarSource.SEED for e in events)
    assert all(not e.editable for e in events)
    fomc = next(e for e in events if e.vendor_id == "FOMC:2027-01-27")
    assert fomc.date == date(2027, 1, 27)
    fomc_et = fomc.at_et()
    assert fomc_et is not None and fomc_et.time() == time(14, 0)
    boj = next(e for e in events if e.vendor_id == "BOJ:2027-01-22")
    assert boj.at is None  # the BoJ publishes no time; date-only stays date-only

    again = import_central_bank_seed(session, seed, now=T1)
    assert again == UpsertCounts(inserted=0, updated=0, unchanged=3, withdrawn=0)


def test_a_row_dropped_from_a_covered_window_is_withdrawn(session: Session) -> None:
    import_central_bank_seed(
        session, parse_central_bank_seed(_seed([_FOMC_JAN, _FOMC_MAR, _BOE_MAR, _BOJ_JAN])), now=T0
    )
    session.commit()
    # A corrected file: FOMC's January meeting gone. FOMC is 'full', so the
    # file claims the whole year and the missing row is withdrawn.
    counts = import_central_bank_seed(
        session, parse_central_bank_seed(_seed([_FOMC_MAR, _BOE_MAR, _BOJ_JAN])), now=T1
    )
    session.commit()
    assert counts.withdrawn == 1
    ids = {e.vendor_id for e in read_range(session, date(2027, 1, 1), date(2027, 12, 31))}
    assert "FOMC:2027-01-27" not in ids
    # Restored by a later file, it comes back.
    counts = import_central_bank_seed(
        session, parse_central_bank_seed(_seed([_FOMC_JAN, _FOMC_MAR, _BOE_MAR, _BOJ_JAN])), now=T1
    )
    assert counts.updated == 1
    ids = {e.vendor_id for e in read_range(session, date(2027, 1, 1), date(2027, 12, 31))}
    assert "FOMC:2027-01-27" in ids


def test_rows_outside_a_partial_window_are_not_withdrawn(session: Session) -> None:
    # A BoE row from before covers_from (an earlier, fuller import) is not
    # contradicted by a file that only claims March onward.
    early_boe = CalendarEventInput(
        kind=CalendarKind.CENTRAL_BANK,
        source=CalendarSource.SEED,
        vendor_id="BOE:2027-02-04",
        title="BoE policy decision",
        date=date(2027, 2, 4),
    )
    upsert_events(session, [early_boe], now=T0)
    counts = import_central_bank_seed(
        session, parse_central_bank_seed(_seed([_FOMC_JAN, _BOE_MAR, _BOJ_JAN])), now=T1
    )
    session.commit()
    assert counts.withdrawn == 0
    ids = {e.vendor_id for e in read_range(session, date(2027, 1, 1), date(2027, 12, 31))}
    assert "BOE:2027-02-04" in ids


@pytest.mark.parametrize("year", [2026, 2027])
def test_central_bank_inputs_are_one_per_decision(year: int) -> None:
    # Every committed decision becomes a valid input: its date is its ET date.
    seed = load_central_bank_seed(year)
    assert seed is not None
    inputs = central_bank_inputs(seed)
    assert len(inputs) == len(seed.decisions)
    assert len({i.vendor_id for i in inputs}) == len(inputs)
    for event in inputs:
        if event.at is not None:
            assert event.at.astimezone(ET).date() == event.date


def test_importing_a_year_with_no_seed_reports_it(session: Session, tmp_path: Path) -> None:
    result = import_central_bank_year(session, 2028, now=T0, directory=tmp_path)
    assert result.year == 2028
    assert result.counts is None
    assert [g.bank for g in result.gaps] == list(Bank)
    assert all(g.kind is GapKind.NO_FILE for g in result.gaps)
    assert all("2028" in g.reason for g in result.gaps)
    assert _rows(session) == []


def test_importing_a_seeded_year_carries_its_gaps(session: Session, tmp_path: Path) -> None:
    (tmp_path / "central_banks_2027.csv").write_text(
        _seed([_FOMC_JAN, _BOE_MAR, _BOJ_JAN]), encoding="utf-8", newline="\n"
    )
    result = import_central_bank_year(session, 2027, now=T0, directory=tmp_path)
    assert result.counts is not None and result.counts.inserted == 3
    assert [(g.bank, g.kind) for g in result.gaps] == [
        (Bank.ECB, GapKind.UNPUBLISHED),
        (Bank.BOE, GapKind.PARTIAL),
    ]
    partial = result.gaps[1]
    assert "2027-03-01" in partial.reason


def test_seed_gaps_between_names_every_year_the_range_touches(tmp_path: Path) -> None:
    (tmp_path / "central_banks_2027.csv").write_text(
        _seed([_FOMC_JAN, _BOE_MAR, _BOJ_JAN]), encoding="utf-8", newline="\n"
    )
    gaps = seed_gaps_between(date(2027, 12, 1), date(2028, 1, 31), directory=tmp_path)
    assert {(g.year, g.bank, g.kind) for g in gaps} == {
        (2027, Bank.ECB, GapKind.UNPUBLISHED),
        (2027, Bank.BOE, GapKind.PARTIAL),
        *((2028, bank, GapKind.NO_FILE) for bank in Bank),
    }


def test_stored_event_is_detached_from_the_session(session: Session) -> None:
    upsert_events(session, [_dividend()], now=T0)
    session.commit()
    [event] = read_range(session, date(2026, 8, 12), date(2026, 8, 12))
    session.close()
    assert isinstance(event, StoredCalendarEvent)
    assert event.title == "AAPL ex-dividend"


# --- unit 7.2b-F: ``now`` is stored as UTC before any reload ------------------

#: The same instant as ``T0``, stated in New York. Equal to ``T0``; a
#: different zone, which is what the audit caught leaking through.
T0_ET = T0.astimezone(ET)


def _is_utc(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is UTC


def _hold_flushed(session: Session) -> list[CalendarEvent]:
    """Keep a strong reference to every object each flush writes.

    The identity map holds persistent objects **weakly**: once ``upsert_events``
    returns, nothing references the row it built, it is collected, and the
    next query reloads it from the database -- through ``UtcDateTime``, so in
    UTC whatever ``now`` was. A test that queried afterwards would pass
    against the bug. Holding the objects is what lets the test see the
    in-memory value a caller still holding the row would see.
    """
    held: list[CalendarEvent] = []

    def keep(sess: Session, _ctx: object) -> None:
        held.extend(obj for obj in (*sess.new, *sess.dirty) if isinstance(obj, CalendarEvent))

    event.listen(session, "after_flush", keep)
    return held


def test_an_upsert_insert_stamps_utc_from_an_eastern_now(session: Session) -> None:
    held = _hold_flushed(session)
    upsert_events(session, [_earnings()], now=T0_ET)
    (row,) = held
    assert _is_utc(row.created_at) and _is_utc(row.updated_at)
    assert row.created_at == T0


def test_an_upsert_update_stamps_utc_from_an_eastern_now(session: Session) -> None:
    upsert_events(session, [_earnings()], now=T0)
    held = _hold_flushed(session)
    upsert_events(session, [_earnings(estimate=Decimal("1.30"))], now=T1.astimezone(ET))
    (row,) = held
    assert _is_utc(row.updated_at) and row.updated_at == T1


def test_a_seed_withdrawal_stamps_utc_from_an_eastern_now(session: Session) -> None:
    seed = load_central_bank_seed(2026)
    assert seed is not None
    import_central_bank_seed(session, seed, now=T0)
    fomc = [d for d in seed.decisions if d.bank is Bank.FOMC]
    trimmed = replace(seed, decisions=tuple(d for d in seed.decisions if d is not fomc[-1]))
    held = _hold_flushed(session)
    counts = import_central_bank_seed(session, trimmed, now=T1.astimezone(ET))
    assert counts.withdrawn == 1
    (withdrawn,) = [row for row in held if row.deleted_at is not None]
    assert _is_utc(withdrawn.deleted_at) and _is_utc(withdrawn.updated_at)
    assert withdrawn.deleted_at == T1


def test_manual_rows_stamp_utc_from_an_eastern_now(session: Session) -> None:
    created = create_manual(session, title="Summit", date=date(2026, 10, 14), now=T0_ET)
    assert _is_utc(created.created_at) and _is_utc(created.updated_at)
    updated = update_manual(
        session, created.id, title="Summit, moved", date=date(2026, 10, 15), now=T1.astimezone(ET)
    )
    assert _is_utc(updated.updated_at) and updated.updated_at == T1
    delete_manual(session, created.id, now=T1.astimezone(ET))
    (row,) = _rows(session)
    assert _is_utc(row.deleted_at) and _is_utc(row.updated_at)


# --- unit 7.2b-F: the IPO fields go through the upsert ------------------------


def _ipo(**overrides: object) -> CalendarEventInput:
    base = CalendarEventInput(
        kind=CalendarKind.IPO,
        source=CalendarSource.FINNHUB,
        vendor_id="symbol:IAM",
        title="Iambic Therapeutics, Inc.",
        date=date(2026, 10, 15),
        ticker="IAM",
        exchange="NASDAQ Global Select",
        shares=9375000,
        price_low=Decimal("15.00"),
        price_high=Decimal("17.00"),
        ipo_status=IpoStatus.EXPECTED,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_an_ipo_round_trips_through_the_store(session: Session) -> None:
    upsert_events(session, [_ipo()], now=T0)
    (stored,) = read_range(session, date(2026, 10, 15), date(2026, 10, 15))
    assert stored.kind is CalendarKind.IPO
    assert (stored.exchange, stored.shares, stored.ipo_status) == (
        "NASDAQ Global Select",
        9375000,
        IpoStatus.EXPECTED,
    )
    assert (stored.price_low, stored.price_high) == (Decimal("15.00"), Decimal("17.00"))


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"ipo_status": IpoStatus.PRICED}, id="status"),
        pytest.param({"exchange": "NYSE"}, id="exchange"),
        pytest.param({"shares": 10_000_000}, id="shares"),
        pytest.param({"price_low": Decimal("16.00"), "price_high": Decimal("16.00")}, id="price"),
        pytest.param({"price_low": Decimal("15.0")}, id="price spelling"),
        pytest.param({"date": date(2026, 10, 22)}, id="date moved"),
    ],
)
def test_a_changed_ipo_field_is_an_update_in_place(
    session: Session, change: dict[str, object]
) -> None:
    upsert_events(session, [_ipo()], now=T0)
    counts = upsert_events(session, [_ipo(**change)], now=T1)
    assert counts == UpsertCounts(inserted=0, updated=1, unchanged=0)
    (row,) = _rows(session)
    for name, value in change.items():
        stored = getattr(row, name)
        assert (stored.value if isinstance(stored, IpoStatus) else stored) == (
            value.value if isinstance(value, IpoStatus) else value
        )
    if "price_low" in change:
        assert format(row.price_low, "f") == format(change["price_low"], "f")


def test_an_unchanged_ipo_is_unchanged(session: Session) -> None:
    upsert_events(session, [_ipo()], now=T0)
    assert upsert_events(session, [_ipo()], now=T1) == UpsertCounts(0, 0, 1)
