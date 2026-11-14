"""R8's attempt rule and one fill cycle (unit U6e), on a temp database with a fake FRED.

The scheduler half -- the schedule's slots and the shipped job -- is
``tests/engine/test_econ_first_prints_job.py``. This file is the rule the job
body applies to each stored row, and what one body run writes.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from corollary.data.calendar import replace_window
from corollary.data.calendar_event import CalendarKind, CalendarSource
from corollary.data.calendar_releases import release_events
from corollary.data.econ_first_prints import (
    ATTEMPT_DELAY,
    ATTEMPT_EVERY,
    CATCH_UP_AT,
    RELEASE_DAY_UNTIL,
    fetch_first_print,
    fill_first_prints,
    is_due,
    latest_due,
    next_release_day_slot,
    release_day_slots,
)
from corollary.data.econ_store import read_attempt, read_economic_releases, read_value
from corollary.data.econ_values import Figure, NotPosted
from corollary.data.providers.fred import (
    FredError,
    FredRefusal,
    FredReleaseDate,
    FredVintageWindow,
    VintageFrequency,
)
from corollary.data.seeds.calendar_seeds import EconReleaseSeries, load_econ_release_seeds
from corollary.db.models import Base, EconReleaseFetch, EconReleaseValue
from corollary.db.session import create_db_engine, sqlite_url
from tests.data.test_econ_values import recorded

ET: Final = ZoneInfo("America/New_York")
SEEDS: Final = load_econ_release_seeds()
KEY: Final = "abcdef0123456789abcdef0123456789"
TICK: Final = timedelta(microseconds=1)


def _et(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=ET).astimezone(UTC)


def _series(release_id: int) -> EconReleaseSeries:
    row = SEEDS.series.by_id(release_id)
    assert row is not None
    return row


def _release_at(release_id: int, day: date) -> datetime:
    at = SEEDS.times.at(release_id, day)
    assert at is not None
    return at


#: A plain Wednesday, EST. One release at each of the seed's three times.
WED: Final = date(2026, 11, 18)
AT_0830: Final = _release_at(10, WED)  # CPI
AT_0915: Final = _release_at(13, WED)  # G.17
AT_1000: Final = _release_at(192, WED)  # JOLTS


def test_the_seed_still_has_exactly_three_release_times() -> None:
    # The first-attempt instants below are pinned for these three; a new time
    # in the seed needs its own row in the next test.
    assert {row.et_time for row in SEEDS.times.rows} == {time(8, 30), time(9, 15), time(10, 0)}
    assert (AT_0830, AT_0915, AT_1000) == (_et(WED, 8, 30), _et(WED, 9, 15), _et(WED, 10, 0))


@pytest.mark.parametrize(
    ("release_at", "first", "last"),
    [
        (AT_0830, _et(WED, 8, 40), _et(WED, 17, 40)),
        (AT_0915, _et(WED, 9, 25), _et(WED, 17, 55)),
        (AT_1000, _et(WED, 10, 10), _et(WED, 17, 40)),
    ],
    ids=["08:30", "09:15", "10:00"],
)
def test_the_release_day_grid_starts_ten_minutes_after_and_runs_every_30_to_18_00(
    release_at: datetime, first: datetime, last: datetime
) -> None:
    slots = release_day_slots(release_at)
    assert slots[0] == first == release_at + ATTEMPT_DELAY
    assert slots[-1] == last
    assert all(b - a == ATTEMPT_EVERY for a, b in zip(slots, slots[1:]))
    assert last <= _et(WED, 18, 0) < last + ATTEMPT_EVERY
    # From any earlier instant the next slot is the first; past the last, none.
    assert next_release_day_slot(release_at, release_at - timedelta(hours=3)) == first
    assert next_release_day_slot(release_at, release_at) == first
    assert next_release_day_slot(release_at, first - TICK) == first
    assert next_release_day_slot(release_at, first) == first + ATTEMPT_EVERY
    assert next_release_day_slot(release_at, last) is None
    assert (ATTEMPT_DELAY, ATTEMPT_EVERY, RELEASE_DAY_UNTIL, CATCH_UP_AT) == (
        timedelta(minutes=10),
        timedelta(minutes=30),
        time(18, 0),
        time(10, 0),
    )


def test_the_release_day_grid_ignores_the_nyse_calendar() -> None:
    # The jobs report is released on Good Friday, when NYSE is shut. The
    # grid comes from the release's own day, never the session: 08:40 EDT.
    good_friday = date(2026, 4, 3)
    slots = release_day_slots(_release_at(50, good_friday))
    assert slots[0] == datetime(2026, 4, 3, 12, 40, tzinfo=UTC)
    assert slots[-1] == datetime(2026, 4, 3, 21, 40, tzinfo=UTC)  # 17:40 EDT


def test_a_half_days_13_00_close_does_not_cut_the_release_day_grid() -> None:
    half_day = date(2026, 11, 27)
    slots = release_day_slots(_et(half_day, 8, 30))
    assert _et(half_day, 13, 10) in slots and slots[-1] == _et(half_day, 17, 40)


# --------------------------------------------------------------------------
# The due rule
# --------------------------------------------------------------------------


def test_nothing_is_due_before_the_first_attempt() -> None:
    assert latest_due(AT_0830, AT_0830 + ATTEMPT_DELAY - TICK) is None
    assert not is_due(AT_0830, None, AT_0830 + ATTEMPT_DELAY - TICK)
    # The 10:00 release is not fetched by its own day's 10:00 catch-up.
    assert not is_due(AT_1000, None, _et(WED, 10, 0))
    assert is_due(AT_1000, None, _et(WED, 10, 10))


def test_an_attempt_covers_its_slot_and_the_next_slot_is_due_again() -> None:
    first = AT_0830 + ATTEMPT_DELAY
    assert is_due(AT_0830, None, first)
    assert not is_due(AT_0830, first, first)
    assert not is_due(AT_0830, first, first + ATTEMPT_EVERY - TICK)
    assert is_due(AT_0830, first, first + ATTEMPT_EVERY)


def test_the_days_10_00_catch_up_covers_a_release_already_out_that_day() -> None:
    # 08:30's grid is 09:40 then 10:10; the 10:00 catch-up falls between and
    # covers it, as R8 says: every row whose release time has passed.
    assert latest_due(AT_0830, _et(WED, 10, 0)) == _et(WED, 10, 0)
    assert is_due(AT_0830, _et(WED, 9, 40), _et(WED, 10, 0))


def test_after_18_00_nothing_is_due_until_the_next_catch_up() -> None:
    last = _et(WED, 17, 40)
    assert not is_due(AT_0830, last, _et(WED, 23, 59))
    assert not is_due(AT_0830, last, _et(date(2026, 11, 19), 9, 59))
    assert latest_due(AT_0830, _et(date(2026, 11, 19), 10, 0)) == _et(date(2026, 11, 19), 10, 0)
    assert is_due(AT_0830, last, _et(date(2026, 11, 19), 10, 0))


def test_the_catch_up_follows_the_nyse_calendar_across_a_holiday() -> None:
    # Wednesday 25 Nov's 08:30 release, last tried at 17:40. Thanksgiving has
    # no catch-up; the half-day Friday does, at 10:00.
    wed = date(2026, 11, 25)
    released, last = _et(wed, 8, 30), _et(wed, 17, 40)
    thanksgiving, friday = date(2026, 11, 26), date(2026, 11, 27)
    assert not is_due(released, last, _et(thanksgiving, 23, 0))
    assert latest_due(released, _et(friday, 9, 59)) == last
    assert is_due(released, last, _et(friday, 10, 0))


def test_a_restart_that_missed_the_catch_up_is_due_at_once() -> None:
    # Last tried Monday's catch-up; the process was down through Tuesday's.
    released = _et(date(2026, 11, 13), 8, 30)
    monday, tuesday = date(2026, 11, 16), date(2026, 11, 17)
    assert is_due(released, _et(monday, 10, 0), _et(tuesday, 15, 0))


def test_the_spring_forward_day_keeps_the_grid_on_et_wall_time() -> None:
    # 2027-03-15 is the Monday after the change: 08:30 EDT is 12:30Z.
    monday = date(2027, 3, 15)
    slots = release_day_slots(_et(monday, 8, 30))
    assert slots[0] == datetime(2027, 3, 15, 12, 40, tzinfo=UTC)
    assert latest_due(_et(date(2027, 3, 12), 8, 30), _et(monday, 10, 0)) == datetime(
        2027, 3, 15, 14, 0, tzinfo=UTC
    )


# --------------------------------------------------------------------------
# The fetch helper: the seed row's frequency, bound
# --------------------------------------------------------------------------


class FakeVintages:
    """``FredProvider.vintage_window``, scripted per series. The last answer repeats."""

    def __init__(self, script: dict[str, Sequence[object]]) -> None:
        self.script = {sid: list(answers) for sid, answers in script.items()}
        self.calls: list[tuple[str, date, VintageFrequency]] = []

    @property
    def secrets(self) -> Sequence[str]:
        return (KEY,)

    async def vintage_window(
        self, series_id: str, release_day: date, *, frequency: VintageFrequency
    ) -> FredVintageWindow | FredRefusal:
        self.calls.append((series_id, release_day, frequency))
        answers = self.script[series_id]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(series_id, release_day, frequency)
        assert isinstance(answer, (FredVintageWindow, FredRefusal))
        return answer


def not_posted(series_id: str, day: date, frequency: VintageFrequency) -> FredVintageWindow:
    return FredVintageWindow(series_id, day, frequency, rows=())


@pytest.mark.asyncio
async def test_the_helper_asks_at_the_seed_rows_frequency_and_reads_with_that_row() -> None:
    eci = _series(11)  # quarterly
    source = FakeVintages({eci.series_id: [not_posted]})
    result = await fetch_first_print(source, eci, date(2026, 10, 30))
    assert source.calls == [("ECIALLCIV", date(2026, 10, 30), "quarterly")]
    assert result == NotPosted("ECIALLCIV", date(2026, 10, 30))


# --------------------------------------------------------------------------
# One body run, on a temp database
# --------------------------------------------------------------------------

CPI_DAY: Final = date(2026, 10, 14)
BOP_DAY: Final = date(2026, 11, 4)
PAYEMS_DAY: Final = date(2026, 11, 6)
CPI, BOP, PAYEMS = _series(10), _series(51), _series(50)
CPI_AT: Final = _release_at(10, CPI_DAY)
BOP_AT: Final = _release_at(51, BOP_DAY)
PAYEMS_AT: Final = _release_at(50, PAYEMS_DAY)
CPI_WINDOW: Final = recorded("cpiaucsl", "CPIAUCSL", CPI_DAY)
BOP_WINDOW: Final = recorded("bopgstb", "BOPGSTB", BOP_DAY)
REFUSAL: Final = "The series does not exist in ALFRED but may exist in FRED"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def store_rows(engine: Engine, *releases: tuple[int, date]) -> None:
    """The release-dates refresh's own write path, for these releases."""
    start, end = min(d for _, d in releases), max(d for _, d in releases)
    rows = [FredReleaseDate(rid, _name(rid), day) for rid, day in releases]
    events = release_events(rows, SEEDS.times, start=start, end=end).events
    assert len(events) == len(releases)
    with Session(engine) as session:
        replace_window(
            session,
            events,
            source=CalendarSource.FRED,
            kinds=frozenset({CalendarKind.ECONOMIC}),
            window_start=start,
            window_end=end,
            tickers=None,
            now=datetime(2026, 10, 1, 11, 0, tzinfo=UTC),
        )
        session.commit()


def _name(release_id: int) -> str:
    row = SEEDS.times.by_id(release_id)
    assert row is not None
    return row.release_name


def sessions(engine: Engine) -> Callable[[], Session]:
    return lambda: Session(engine)


async def fill(engine: Engine, source: FakeVintages, now: datetime):  # type: ignore[no-untyped-def]
    return await fill_first_prints(source, sessions(engine), SEEDS, clock=lambda: now)


def _count(engine: Engine, model: type[Base]) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.asyncio
async def test_not_posted_then_a_figure_then_no_further_request(engine: Engine) -> None:
    store_rows(engine, (10, CPI_DAY))
    source = FakeVintages({"CPIAUCSL": [not_posted, CPI_WINDOW]})

    first = await fill(engine, source, CPI_AT + ATTEMPT_DELAY)
    assert [r.attempt.outcome for r in first.attempted] == ["not_posted"]
    with Session(engine) as session:
        assert read_value(session, 10, CPI_DAY) is None
        last = read_attempt(session, 10, CPI_DAY)
        assert last is not None and last.attempt.outcome == "not_posted"
    assert first.pending == (CPI_AT,)

    second = await fill(engine, source, CPI_AT + ATTEMPT_DELAY + ATTEMPT_EVERY)
    assert [r.attempt.outcome for r in second.attempted] == ["figure"]
    with Session(engine) as session:
        value = read_value(session, 10, CPI_DAY)
        assert value is not None and (value.actual, value.prior) == (
            CPI_WINDOW_FIGURE.actual,
            CPI_WINDOW_FIGURE.prior,
        )
    assert second.pending == ()

    third = await fill(engine, source, CPI_AT + ATTEMPT_DELAY + 2 * ATTEMPT_EVERY)
    assert third.attempted == () and len(source.calls) == 2
    # Nor at any later catch-up.
    later = await fill(engine, source, _et(date(2026, 10, 20), 10, 0))
    assert later.attempted == () and len(source.calls) == 2


#: What the recorded CPI window computes to (``test_econ_values``'s long division).
CPI_WINDOW_FIGURE: Final = Figure(
    "CPIAUCSL", date(2026, 9, 1), Decimal("0.6485"), Decimal("0.3960"), CPI_DAY
)


@pytest.mark.asyncio
async def test_a_refusal_is_recorded_and_the_catch_up_keeps_trying_it(engine: Engine) -> None:
    store_rows(engine, (50, PAYEMS_DAY))
    source = FakeVintages({"PAYEMS": [FredRefusal("PAYEMS", PAYEMS_DAY, REFUSAL)]})
    run = await fill(engine, source, PAYEMS_AT + ATTEMPT_DELAY)
    assert [r.attempt.outcome for r in run.attempted] == ["refused"]
    with Session(engine) as session:
        (release,) = read_economic_releases(session, PAYEMS_DAY, PAYEMS_DAY, seeds=SEEDS)
    assert release.last_attempt is not None
    assert release.last_attempt.attempt.detail == REFUSAL
    shown = release.display(PAYEMS_AT + ATTEMPT_DELAY)
    assert shown.state == "unavailable" and shown.note is not None and REFUSAL in shown.note

    # Ten days on -- unavailable on the seven-day rule too -- the 10:00
    # catch-up still asks, once.
    catch_up = _et(PAYEMS_DAY + timedelta(days=10), 10, 0)
    again = await fill(engine, source, catch_up)
    assert [r.release_id for r in again.attempted] == [50]
    assert source.calls[-1] == ("PAYEMS", PAYEMS_DAY, "monthly")


@pytest.mark.asyncio
async def test_an_error_is_recorded_scrubbed_and_the_others_proceed(engine: Engine) -> None:
    store_rows(engine, (10, CPI_DAY), (51, BOP_DAY), (50, PAYEMS_DAY))
    leaked = FredError(f"GET https://api.stlouisfed.org/fred/series/observations?api_key={KEY} failed")
    source = FakeVintages(
        {"CPIAUCSL": [CPI_WINDOW], "BOPGSTB": [BOP_WINDOW], "PAYEMS": [leaked]}
    )
    # A catch-up after all three: each is due once.
    run = await fill(engine, source, _et(date(2026, 11, 9), 10, 0))

    outcomes = {r.release_id: r.attempt.outcome for r in run.attempted}
    assert outcomes == {10: "figure", 51: "figure", 50: "error"}
    assert [r.release_id for r in run.errors] == [50]
    assert _count(engine, EconReleaseValue) == 2
    with Session(engine) as session:
        error = read_attempt(session, 50, PAYEMS_DAY)
        raw = session.get(EconReleaseFetch, (50, PAYEMS_DAY))
    assert error is not None and error.attempt.outcome == "error"
    assert raw is not None and raw.detail is not None
    assert raw.detail.startswith("FredError: ") and KEY not in raw.detail


@pytest.mark.asyncio
async def test_a_caller_bug_in_first_print_is_an_error_never_awaiting(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    # A window fetched at another frequency than the seed row states: the
    # helper binds the frequency, so only a source ignoring it can produce
    # this -- and first_print's ValueError must be recorded as an error.
    store_rows(engine, (10, CPI_DAY))

    def quarterly(series_id: str, day: date, _: VintageFrequency) -> FredVintageWindow:
        return FredVintageWindow(series_id, day, "quarterly", rows=())

    source = FakeVintages({"CPIAUCSL": [quarterly]})
    with caplog.at_level(logging.ERROR, logger="corollary.data.econ_first_prints"):
        run = await fill(engine, source, CPI_AT + ATTEMPT_DELAY)
    (result,) = run.attempted
    assert result.attempt.outcome == "error" and result.bug
    assert result.attempt.detail is not None and result.attempt.detail.startswith("ValueError: ")
    assert any(getattr(r, "event", None) == "econ_first_print_bug" for r in caplog.records)
    with Session(engine) as session:
        assert read_value(session, 10, CPI_DAY) is None


@pytest.mark.asyncio
async def test_a_failing_store_costs_only_its_own_release(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_rows(engine, (10, CPI_DAY), (51, BOP_DAY))
    import corollary.data.econ_first_prints as module

    real = module.store_first_print

    def failing(session: Session, release_id: int, *args: object, **kwargs: object) -> object:
        if release_id == 10:
            raise RuntimeError("disk I/O error")
        return real(session, release_id, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "store_first_print", failing)
    source = FakeVintages({"CPIAUCSL": [CPI_WINDOW], "BOPGSTB": [BOP_WINDOW]})
    run = await fill(engine, source, _et(date(2026, 11, 9), 10, 0))
    assert {r.release_id: r.attempt.outcome for r in run.attempted} == {10: "error", 51: "figure"}
    with Session(engine) as session:
        assert read_value(session, 10, CPI_DAY) is None
        assert read_value(session, 51, BOP_DAY) is not None
        cpi = read_attempt(session, 10, CPI_DAY)
    assert cpi is not None and cpi.attempt.outcome == "error"


@pytest.mark.asyncio
async def test_a_stored_value_is_never_fetched_again_or_rewritten(engine: Engine) -> None:
    store_rows(engine, (10, CPI_DAY))
    source = FakeVintages({"CPIAUCSL": [CPI_WINDOW]})
    await fill(engine, source, CPI_AT + ATTEMPT_DELAY)
    with Session(engine) as session:
        before = session.get(EconReleaseValue, (10, CPI_DAY))
        assert before is not None
        snapshot = (before.actual, before.prior, before.period, before.fetched_at)
    for days in range(1, 9):
        await fill(engine, source, _et(CPI_DAY + timedelta(days=days), 10, 0))
    assert len(source.calls) == 1
    with Session(engine) as session:
        after = session.get(EconReleaseValue, (10, CPI_DAY))
        assert after is not None
        assert (after.actual, after.prior, after.period, after.fetched_at) == snapshot


@pytest.mark.asyncio
async def test_nothing_due_makes_no_request_and_reports_the_upcoming(engine: Engine) -> None:
    store_rows(engine, (10, CPI_DAY))
    source = FakeVintages({"CPIAUCSL": [CPI_WINDOW]})
    run = await fill(engine, source, CPI_AT - timedelta(days=1))
    assert run.attempted == () and source.calls == []
    assert run.pending == (CPI_AT,)
