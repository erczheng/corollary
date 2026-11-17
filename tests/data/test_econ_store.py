"""The economic first-print store and its read side (U6d, spec R6 and R8).

The calendar rows are written by the real refresh path --
``calendar_releases.release_events`` into ``calendar.replace_window`` -- so
"the refresh nulls ``calendar_event.prior``/``.actual`` and the value
survives" is tested against the code that does the nulling, not a stand-in.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from corollary.data.calendar import replace_window, upsert_events
from corollary.data.calendar_event import CalendarEventInput, CalendarKind, CalendarSource
from corollary.data.calendar_releases import release_events, release_vendor_id
from corollary.data.econ_store import (
    StoredEconValue,
    _stored_attempt,
    _stored_value,
    parse_release_vendor_id,
    read_attempt,
    read_economic_releases,
    read_value,
    record_attempt,
    store_first_print,
)
from corollary.data.econ_values import (
    NOT_PUBLISHED_NOTE,
    NOT_READ_PREFIX,
    Attempt,
    Figure,
    NotPosted,
    ValueDisplay,
    attempt_of_error,
    unavailable_from,
)
from corollary.data.providers.fred import FredReleaseDate
from corollary.data.seeds.calendar_seeds import EconReleaseSeries, load_econ_release_seeds
from corollary.db.models import Base, CalendarEvent, EconReleaseFetch, EconReleaseValue
from corollary.db.session import create_db_engine, sqlite_url

SEEDS = load_econ_release_seeds()


def _series(release_id: int) -> EconReleaseSeries:
    row = SEEDS.series.by_id(release_id)
    assert row is not None
    return row


CPI = _series(10)
PAYEMS = _series(50)
BOP = _series(51)
CPI_DAY = date(2026, 10, 14)
BOP_DAY = date(2026, 11, 4)
PAYEMS_DAY = date(2026, 11, 6)
RELEASES = ((10, CPI_DAY), (51, BOP_DAY), (50, PAYEMS_DAY))
WINDOW = (date(2026, 10, 1), date(2026, 11, 30))

#: 08:30 ET each: EDT on 14 October, EST on 4 and 6 November.
CPI_AT = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
BOP_AT = datetime(2026, 11, 4, 13, 30, tzinfo=UTC)
PAYEMS_AT = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)

CPI_FIGURE = Figure("CPIAUCSL", date(2026, 9, 1), Decimal("0.6485"), Decimal("0.3960"), CPI_DAY)
BOP_FIGURE = Figure("BOPGSTB", date(2026, 9, 1), Decimal("-105.0470"), Decimal("-78.3120"), BOP_DAY)
REFRESHED = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
FETCHED = datetime(2026, 10, 14, 12, 40, tzinfo=UTC)
#: The reader's clock: only the unreadable-row log is stamped with it.
READ = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)


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


def _refresh(session: Session, now: datetime = REFRESHED) -> None:
    """The daily release-dates refresh, as the scheduler runs it."""
    rows = [
        FredReleaseDate(release_id, _name(release_id), day) for release_id, day in RELEASES
    ]
    events = release_events(rows, SEEDS.times, start=WINDOW[0], end=WINDOW[1]).events
    assert len(events) == len(RELEASES)
    replace_window(
        session,
        events,
        source=CalendarSource.FRED,
        kinds=frozenset({CalendarKind.ECONOMIC}),
        window_start=WINDOW[0],
        window_end=WINDOW[1],
        tickers=None,
        now=now,
    )
    session.commit()


def _name(release_id: int) -> str:
    row = SEEDS.times.by_id(release_id)
    assert row is not None
    return row.release_name


def _calendar_row(session: Session, release_id: int, day: date) -> CalendarEvent:
    return session.scalars(
        select(CalendarEvent).where(CalendarEvent.vendor_id == release_vendor_id(release_id, day))
    ).one()


# --------------------------------------------------------------------------
# The value: written once, exact
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("series", "figure"), [(CPI, CPI_FIGURE), (BOP, BOP_FIGURE)], ids=["cpi", "bopgstb"]
)
def test_a_stored_figure_round_trips_exactly(
    engine: Engine, series: EconReleaseSeries, figure: Figure
) -> None:
    with Session(engine) as sess:
        stored = store_first_print(
            sess, series.release_id, figure.vintage_date, series, figure, fetched_at=FETCHED
        )
        sess.commit()
    assert stored == StoredEconValue(
        release_id=series.release_id,
        release_date=figure.vintage_date,
        series_id=figure.series_id,
        period=figure.period,
        actual=figure.actual,
        prior=figure.prior,
        unit_label=series.unit_label,
        vintage_date=figure.vintage_date,
        fetched_at=FETCHED,
    )

    with Session(engine) as sess:
        read = read_value(sess, series.release_id, figure.vintage_date)
        raw = sess.execute(text("SELECT actual, prior FROM econ_release_value")).one()
    assert read == stored
    assert read is not None
    assert type(read.actual) is Decimal and type(read.prior) is Decimal
    assert (str(read.actual), str(read.prior)) == (str(figure.actual), str(figure.prior))
    assert tuple(raw) == (str(figure.actual), str(figure.prior))


def test_a_second_store_for_the_same_release_changes_nothing(engine: Engine) -> None:
    with Session(engine) as sess:
        first = store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        sess.commit()
    revised = Figure("CPIAUCSL", date(2026, 9, 1), Decimal("0.7000"), Decimal("0.4000"), CPI_DAY)
    with Session(engine) as sess:
        again = store_first_print(
            sess, 10, CPI_DAY, CPI, revised, fetched_at=FETCHED + timedelta(days=3)
        )
        sess.commit()
        rows = list(sess.scalars(select(EconReleaseValue)))
    assert again == first
    assert len(rows) == 1
    assert (rows[0].actual, rows[0].prior, rows[0].fetched_at) == (
        Decimal("0.6485"),
        Decimal("0.3960"),
        FETCHED,
    )


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ((50, CPI_DAY, CPI, CPI_FIGURE), ValueError),  # the seed row is release 10's
        ((10, CPI_DAY, PAYEMS, CPI_FIGURE), ValueError),  # figure and seed row disagree
        ((10, date(2026, 10, 15), CPI, CPI_FIGURE), ValueError),  # another day's vintage
        ((10, CPI_DAY, CPI, NotPosted("CPIAUCSL", CPI_DAY)), TypeError),
    ],
)
def test_a_store_refuses_a_callers_bug(
    session: Session, args: tuple[Any, ...], error: type[Exception]
) -> None:
    with pytest.raises(error):
        store_first_print(session, *args, fetched_at=FETCHED)
    assert session.scalar(select(func.count()).select_from(EconReleaseValue)) == 0


def test_a_store_refuses_a_naive_instant(session: Session) -> None:
    with pytest.raises(ValueError, match="aware"):
        store_first_print(
            session, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED.replace(tzinfo=None)
        )


# --------------------------------------------------------------------------
# The last attempt: upserted every time
# --------------------------------------------------------------------------


def test_the_attempt_row_keeps_only_the_last_attempt(engine: Engine) -> None:
    attempts: list[tuple[Any, str | None]] = [
        ("not_posted", None),
        ("error", "FredError: GET /series/observations returned 503"),
        ("figure", None),
    ]
    for minutes, (outcome, detail) in zip((10, 40, 70), attempts):
        with Session(engine) as sess:
            record_attempt(
                sess, 10, CPI_DAY, Attempt(outcome, detail), at=CPI_AT + timedelta(minutes=minutes)
            )
            sess.commit()
    with Session(engine) as sess:
        rows = list(sess.scalars(select(EconReleaseFetch)))
    assert len(rows) == 1
    assert (rows[0].outcome, rows[0].detail, rows[0].attempted_at) == (
        "figure",
        None,
        CPI_AT + timedelta(minutes=70),
    )


@pytest.mark.parametrize(
    "raw",
    [("figure", None), ("error", "FredError: api_key=abcdef0123456789abcdef0123456789"), "not_posted", None],
)
def test_record_attempt_takes_an_attempt_never_a_raw_outcome(session: Session, raw: Any) -> None:
    # Audit P4: a raw (outcome, detail) could carry an unscrubbed error text
    # into the table. Only an Attempt -- from attempt_of / attempt_of_error --
    # is accepted.
    with pytest.raises(TypeError, match="Attempt"):
        record_attempt(session, 10, CPI_DAY, raw, at=CPI_AT)
    assert session.scalar(select(func.count()).select_from(EconReleaseFetch)) == 0


def test_an_error_attempt_reaches_the_table_scrubbed(session: Session) -> None:
    key = "abcdef0123456789abcdef0123456789"
    exc = RuntimeError(f"GET https://api.stlouisfed.org/fred/x?api_key={key} failed, key {key}")
    record_attempt(session, 10, CPI_DAY, attempt_of_error(exc, secrets=(key,)), at=CPI_AT)
    row = session.get(EconReleaseFetch, (10, CPI_DAY))
    assert row is not None and row.outcome == "error"
    assert row.detail is not None and row.detail.startswith("RuntimeError: ")
    assert key not in row.detail


# --------------------------------------------------------------------------
# The read side, and the refresh
# --------------------------------------------------------------------------


def test_after_a_value_is_stored_the_refresh_runs_and_the_value_is_still_read(
    engine: Engine,
) -> None:
    with Session(engine) as sess:
        _refresh(sess)
        store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        # A producer writing the figure onto the calendar row -- the design
        # R6 rejected -- to show what the refresh does to it.
        row = _calendar_row(sess, 10, CPI_DAY)
        row.prior, row.actual = Decimal("0.3960"), Decimal("0.6485")
        sess.commit()

    with Session(engine) as sess:
        _refresh(sess, REFRESHED + timedelta(days=14))  # the real path: _apply
        row = _calendar_row(sess, 10, CPI_DAY)
        assert (row.prior, row.actual) == (None, None)  # nulled, as R6 says it would be
        releases = read_economic_releases(sess, *WINDOW, now=READ, seeds=SEEDS)

    cpi = releases[0]
    assert cpi.value is not None
    assert (cpi.value.actual, cpi.value.prior) == (Decimal("0.6485"), Decimal("0.3960"))
    assert cpi.display(CPI_AT + timedelta(days=30)) == ValueDisplay(
        "filled", "computed from FRED's levels: CPIAUCSL"
    )


def test_storing_and_attempting_never_touch_the_calendar_row(engine: Engine) -> None:
    with Session(engine) as sess:
        _refresh(sess)
        before = {
            (r.vendor_id, r.prior, r.actual, r.updated_at)
            for r in sess.scalars(select(CalendarEvent))
        }
        store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        record_attempt(sess, 10, CPI_DAY, Attempt("figure", None), at=FETCHED)
        record_attempt(sess, 50, PAYEMS_DAY, Attempt("not_posted", None), at=PAYEMS_AT)
        sess.commit()
    with Session(engine) as sess:
        after = {
            (r.vendor_id, r.prior, r.actual, r.updated_at)
            for r in sess.scalars(select(CalendarEvent))
        }
    assert after == before
    assert all(prior is None and actual is None for _, prior, actual, _ in after)


def test_the_read_joins_value_seed_and_last_attempt_per_row(engine: Engine) -> None:
    with Session(engine) as sess:
        _refresh(sess)
        store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        record_attempt(sess, 10, CPI_DAY, Attempt("figure", None), at=FETCHED)
        record_attempt(
            sess, 50, PAYEMS_DAY, Attempt("no_value", "x(t-1) (2026-09-01) is '.'"), at=PAYEMS_AT
        )
        sess.commit()
        releases = read_economic_releases(sess, *WINDOW, now=READ, seeds=SEEDS)

    assert [(r.release_id, r.release_date) for r in releases] == [
        (10, CPI_DAY),
        (51, BOP_DAY),
        (50, PAYEMS_DAY),
    ]
    cpi, bop, payems = releases
    assert cpi.event.vendor_id == "10:2026-10-14"
    assert [r.release_at for r in releases] == [CPI_AT, BOP_AT, PAYEMS_AT]
    assert [(r.unit_label, r.decimals) for r in releases] == [("% m/m", 1), ("$B", 1), ("K", 0)]
    assert cpi.series == CPI and bop.series == BOP and payems.series == PAYEMS

    assert cpi.value is not None and cpi.value.series_id == "CPIAUCSL"
    assert cpi.last_attempt is not None and cpi.last_attempt.attempt == Attempt("figure", None)
    assert bop.value is None and bop.last_attempt is None
    assert payems.value is None and payems.last_attempt is not None
    assert payems.last_attempt.attempted_at == PAYEMS_AT

    # Each state, from the row's own release instant.
    assert bop.display(BOP_AT - timedelta(microseconds=1)) == ValueDisplay("upcoming", None)
    assert bop.display(BOP_AT) == ValueDisplay("awaiting", None)
    assert bop.display(unavailable_from(BOP_AT)) == ValueDisplay(
        "unavailable", NOT_PUBLISHED_NOTE
    )
    assert payems.display(unavailable_from(PAYEMS_AT)) == ValueDisplay(
        "unavailable", "x(t-1) (2026-09-01) is '.'"
    )
    assert cpi.display(CPI_AT) == ValueDisplay("filled", "computed from FRED's levels: CPIAUCSL")


def test_only_economic_rows_are_read(engine: Engine) -> None:
    with Session(engine) as sess:
        _refresh(sess)
        upsert_events(
            sess,
            [
                CalendarEventInput(
                    kind=CalendarKind.EARNINGS,
                    source=CalendarSource.FINNHUB,
                    vendor_id="NVDA:2026Q3",
                    title="NVDA earnings",
                    ticker="NVDA",
                    date=date(2026, 11, 5),
                )
            ],
            now=REFRESHED,
        )
        sess.commit()
        releases = read_economic_releases(sess, *WINDOW, now=READ, seeds=SEEDS)
    assert {r.event.kind for r in releases} == {CalendarKind.ECONOMIC}
    assert len(releases) == 3


@pytest.mark.parametrize("vendor_id", ["10-2026-10-14", "x:2026-10-14", "10:2026-14-10", "10:"])
def test_a_malformed_vendor_id_is_refused(vendor_id: str) -> None:
    with pytest.raises(ValueError, match="vendor_id"):
        parse_release_vendor_id(vendor_id)


def test_the_vendor_id_parses_back_to_what_the_producer_composed() -> None:
    assert parse_release_vendor_id(release_vendor_id(10, CPI_DAY)) == (10, CPI_DAY)


def test_a_calendar_row_whose_vendor_id_names_another_day_is_refused(engine: Engine) -> None:
    with Session(engine) as sess:
        upsert_events(
            sess,
            [
                CalendarEventInput(
                    kind=CalendarKind.ECONOMIC,
                    source=CalendarSource.FRED,
                    vendor_id="10:2026-10-15",
                    title="Consumer Price Index",
                    date=CPI_DAY,
                    at=CPI_AT,
                )
            ],
            now=REFRESHED,
        )
        sess.commit()
        with pytest.raises(ValueError, match="10:2026-10-15"):
            read_economic_releases(sess, *WINDOW, now=READ, seeds=SEEDS)


# --------------------------------------------------------------------------
# A damaged row costs its own release, never the read
# --------------------------------------------------------------------------


def _damage(
    engine: Engine, table: str, column: str, raw: object, release_id: int, day: date
) -> None:
    """Write ``raw`` into one stored row past its CHECKs: the damaged database a read must survive.

    SQLite's ``ignore_check_constraints`` is per connection, and is set back
    before the connection returns to the pool.
    """
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        try:
            done = conn.execute(
                text(
                    f"UPDATE {table} SET {column} = :raw "
                    "WHERE release_id = :release_id AND release_date = :day"
                ),
                {"raw": raw, "release_id": release_id, "day": day.isoformat()},
            )
            assert done.rowcount == 1, (table, column, release_id, day)
            conn.commit()
        finally:
            conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")


def _unreadable_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if getattr(r, "event", None) == "econ_release_row_unreadable"
    ]


@pytest.mark.parametrize(
    ("column", "raw", "error_type"),
    [
        # Will not decode at all.
        ("actual", "not a number", "InvalidOperation"),
        ("fetched_at", "garbage", "ValueError"),
        ("period", "2026-13-45", "ValueError"),
        # Decodes, and breaks the table's own rules.
        ("prior", "-Infinity", "ValueError"),
        ("actual", "NaN", "ValueError"),
        # Finite, but no Money column stores it -- and the display's rounding
        # would raise on it, which is how it 500'd the range (rules audit).
        ("actual", "1E+100", "ValueError"),
        ("prior", "9" * 90, "ValueError"),
        ("series_id", "   ", "ValueError"),
        ("unit_label", "", "ValueError"),
        ("vintage_date", "2026-10-13", "ValueError"),
    ],
)
def test_an_unreadable_value_row_makes_its_release_unavailable_and_no_other(
    engine: Engine, caplog: pytest.LogCaptureFixture, column: str, raw: str, error_type: str
) -> None:
    with Session(engine) as sess:
        _refresh(sess)
        store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        store_first_print(sess, 51, BOP_DAY, BOP, BOP_FIGURE, fetched_at=FETCHED)
        record_attempt(sess, 50, PAYEMS_DAY, Attempt("not_posted", None), at=PAYEMS_AT)
        sess.commit()
    _damage(engine, "econ_release_value", column, raw, 10, CPI_DAY)

    with caplog.at_level(logging.ERROR, logger="corollary.data.econ_store"):
        with Session(engine) as sess:
            cpi, bop, payems = read_economic_releases(
                sess, *WINDOW, now=READ, correlation_id="cid-read", seeds=SEEDS
            )

    reason = f"econ_release_value ({error_type})"
    assert (cpi.value, cpi.unreadable, cpi.last_attempt) == (None, reason, None)
    unavailable = ValueDisplay("unavailable", f"{NOT_READ_PREFIX}: {reason}")
    # At once, whatever the clock: a damaged row is neither upcoming nor awaiting.
    assert cpi.display(CPI_AT - timedelta(days=1)) == unavailable
    assert cpi.display(READ) == unavailable
    # The unit falls back to the seed's, as for any release with no value.
    assert cpi.unit_label == "% m/m"

    # Every other release reads exactly as it did.
    assert bop.unreadable is None and bop.value is not None
    assert (bop.value.actual, bop.value.prior) == (BOP_FIGURE.actual, BOP_FIGURE.prior)
    assert bop.display(READ).state == "filled"
    assert payems.unreadable is None and payems.value is None
    assert payems.last_attempt is not None
    assert payems.last_attempt.attempt == Attempt("not_posted", None)

    # Rule 8: the rule, the inputs and the time.
    (record,) = _unreadable_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.__dict__["rule"]
    assert {
        key: record.__dict__[key]
        for key in ("table", "release_id", "release_date", "error_type", "correlation_id", "at")
    } == {
        "table": "econ_release_value",
        "release_id": 10,
        "release_date": "2026-10-14",
        "error_type": error_type,
        "correlation_id": "cid-read",
        "at": READ.isoformat(),
    }
    assert record.__dict__["calendar_event_id"] == cpi.event.id
    assert record.__dict__["error"]

    # Never repaired or deleted: the damaged text is still exactly what is stored.
    with engine.connect() as conn:
        stored = conn.execute(
            text(f"SELECT {column} FROM econ_release_value WHERE release_id = 10")
        ).scalar_one()
    assert stored == raw


def test_a_value_row_whose_key_no_longer_names_its_release_is_not_joined(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A damaged ``release_date`` moves the row off its key, so nothing joins to it.

    Read by key, the release finds no row and reads by its clock, as any
    release with no value does; the stray row is never read. (The old bulk
    read by ``release_id`` decoded it, and raised out of the whole read.)
    """
    with Session(engine) as sess:
        _refresh(sess)
        store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        sess.commit()
    _damage(engine, "econ_release_value", "release_date", "not a date", 10, CPI_DAY)

    with caplog.at_level(logging.ERROR, logger="corollary.data.econ_store"):
        with Session(engine) as sess:
            cpi, _, _ = read_economic_releases(sess, *WINDOW, now=READ, seeds=SEEDS)
    assert (cpi.value, cpi.unreadable) == (None, None)
    assert cpi.display(CPI_AT).state == "awaiting"
    assert _unreadable_records(caplog) == []


def test_a_damaged_row_for_another_date_of_the_same_release_is_never_read(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Read by key, a damaged September CPI row cannot reach an October read."""
    september = date(2026, 9, 11)
    with Session(engine) as sess:
        _refresh(sess)
        store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        old = Figure("CPIAUCSL", date(2026, 8, 1), Decimal("0.4000"), Decimal("0.2000"), september)
        store_first_print(sess, 10, september, CPI, old, fetched_at=FETCHED)
        sess.commit()
    _damage(engine, "econ_release_value", "actual", "not a number", 10, september)

    with caplog.at_level(logging.ERROR, logger="corollary.data.econ_store"):
        with Session(engine) as sess:
            cpi, _, _ = read_economic_releases(sess, *WINDOW, now=READ, seeds=SEEDS)
    assert cpi.unreadable is None
    assert cpi.value is not None and cpi.value.actual == CPI_FIGURE.actual
    assert _unreadable_records(caplog) == []


@pytest.mark.parametrize(
    ("column", "raw"),
    [
        ("outcome", "bogus"),  # an outcome no attempt has
        ("detail", "a not_posted attempt says nothing"),  # a detail where none belongs
        ("attempted_at", "garbage"),  # an instant that will not parse
    ],
)
def test_an_unreadable_attempt_row_makes_an_unfilled_release_unavailable(
    engine: Engine, caplog: pytest.LogCaptureFixture, column: str, raw: str
) -> None:
    with Session(engine) as sess:
        _refresh(sess)
        record_attempt(sess, 50, PAYEMS_DAY, Attempt("not_posted", None), at=PAYEMS_AT)
        record_attempt(sess, 51, BOP_DAY, Attempt("not_posted", None), at=BOP_AT)
        sess.commit()
    _damage(engine, "econ_release_fetch", column, raw, 50, PAYEMS_DAY)

    with caplog.at_level(logging.ERROR, logger="corollary.data.econ_store"):
        with Session(engine) as sess:
            _, bop, payems = read_economic_releases(sess, *WINDOW, now=READ, seeds=SEEDS)

    assert (payems.value, payems.last_attempt) == (None, None)
    assert payems.unreadable == "econ_release_fetch (ValueError)"
    assert payems.display(READ) == ValueDisplay(
        "unavailable", f"{NOT_READ_PREFIX}: econ_release_fetch (ValueError)"
    )
    assert bop.unreadable is None and bop.last_attempt is not None
    (record,) = _unreadable_records(caplog)
    assert (record.__dict__["table"], record.__dict__["release_id"]) == ("econ_release_fetch", 50)


def test_an_unreadable_attempt_beside_a_readable_value_keeps_the_figure(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The attempt does not decide a filled row's state, so its damage hides nothing."""
    with Session(engine) as sess:
        _refresh(sess)
        store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        record_attempt(sess, 10, CPI_DAY, Attempt("figure", None), at=FETCHED)
        sess.commit()
    _damage(engine, "econ_release_fetch", "outcome", "bogus", 10, CPI_DAY)

    with caplog.at_level(logging.ERROR, logger="corollary.data.econ_store"):
        with Session(engine) as sess:
            cpi, _, _ = read_economic_releases(sess, *WINDOW, now=READ, seeds=SEEDS)
    assert cpi.unreadable == "econ_release_fetch (ValueError)"
    assert cpi.display(READ) == ValueDisplay("filled", "computed from FRED's levels: CPIAUCSL")
    assert len(_unreadable_records(caplog)) == 1  # still logged


def test_both_rows_unreadable_names_both(engine: Engine) -> None:
    with Session(engine) as sess:
        _refresh(sess)
        store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        record_attempt(sess, 10, CPI_DAY, Attempt("figure", None), at=FETCHED)
        sess.commit()
    _damage(engine, "econ_release_value", "actual", "not a number", 10, CPI_DAY)
    _damage(engine, "econ_release_fetch", "outcome", "bogus", 10, CPI_DAY)

    with Session(engine) as sess:
        cpi, _, _ = read_economic_releases(sess, *WINDOW, now=READ, seeds=SEEDS)
    assert cpi.unreadable == (
        "econ_release_value (InvalidOperation); econ_release_fetch (ValueError)"
    )


def test_a_single_key_read_of_a_damaged_row_still_raises(engine: Engine) -> None:
    """``read_value``/``read_attempt`` are explicit lookups: they report, never paper over."""
    with Session(engine) as sess:
        store_first_print(sess, 10, CPI_DAY, CPI, CPI_FIGURE, fetched_at=FETCHED)
        record_attempt(sess, 10, CPI_DAY, Attempt("figure", None), at=FETCHED)
        sess.commit()
    _damage(engine, "econ_release_value", "actual", "NaN", 10, CPI_DAY)
    _damage(engine, "econ_release_fetch", "outcome", "bogus", 10, CPI_DAY)
    with Session(engine) as sess:
        with pytest.raises(ValueError, match="non-finite actual"):
            read_value(sess, 10, CPI_DAY)
        with pytest.raises(ValueError, match="unknown outcome 'bogus'"):
            read_attempt(sess, 10, CPI_DAY)


@pytest.mark.parametrize(
    "column", ["series_id", "period", "actual", "prior", "unit_label", "fetched_at"]
)
def test_a_null_in_a_not_null_value_column_is_unreadable_not_an_attribute_error(
    column: str,
) -> None:
    """NOT NULL survives ``ignore_check_constraints``, so only file-level damage gets here.

    The column types decode a NULL to ``None``, so a loaded row looks
    exactly like this one -- and ``None.astimezone`` or ``None.is_finite``
    would raise ``AttributeError``, which the read does not catch (rules audit).
    """
    fields: dict[str, Any] = {
        "release_id": 10,
        "release_date": CPI_DAY,
        "series_id": "CPIAUCSL",
        "period": date(2026, 9, 1),
        "actual": Decimal("0.6485"),
        "prior": Decimal("0.3960"),
        "unit_label": "% m/m",
        "vintage_date": CPI_DAY,
        "fetched_at": FETCHED,
        column: None,
    }
    with pytest.raises(ValueError, match=f"NULL in NOT NULL column {column}"):
        _stored_value(EconReleaseValue(**fields))


@pytest.mark.parametrize("column", ["attempted_at", "outcome"])
def test_a_null_in_a_not_null_attempt_column_is_unreadable(column: str) -> None:
    fields: dict[str, Any] = {
        "release_id": 10,
        "release_date": CPI_DAY,
        "attempted_at": FETCHED,
        "outcome": "not_posted",
        "detail": None,
        column: None,
    }
    with pytest.raises(ValueError, match=f"NULL in NOT NULL column {column}"):
        _stored_attempt(EconReleaseFetch(**fields))


def test_the_read_refuses_a_naive_clock(engine: Engine) -> None:
    with Session(engine) as sess:
        _refresh(sess)
        with pytest.raises(ValueError, match="now must be a timezone-aware"):
            read_economic_releases(sess, *WINDOW, now=READ.replace(tzinfo=None), seeds=SEEDS)
