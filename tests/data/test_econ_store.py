"""The economic first-print store and its read side (U6d, spec R6 and R8).

The calendar rows are written by the real refresh path --
``calendar_releases.release_events`` into ``calendar.replace_window`` -- so
"the refresh nulls ``calendar_event.prior``/``.actual`` and the value
survives" is tested against the code that does the nulling, not a stand-in.
"""

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
    parse_release_vendor_id,
    read_economic_releases,
    read_value,
    record_attempt,
    store_first_print,
)
from corollary.data.econ_values import (
    NOT_PUBLISHED_NOTE,
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
        releases = read_economic_releases(sess, *WINDOW, seeds=SEEDS)

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
        releases = read_economic_releases(sess, *WINDOW, seeds=SEEDS)

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
        releases = read_economic_releases(sess, *WINDOW, seeds=SEEDS)
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
            read_economic_releases(sess, *WINDOW, seeds=SEEDS)
