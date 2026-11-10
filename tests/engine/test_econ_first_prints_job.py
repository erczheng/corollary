"""The ``econ_first_prints`` job as ``context_jobs`` ships it (spec R8, unit U6e).

``tests/data/test_econ_first_prints.py`` is the per-row due rule and what one
fill run writes. This file is the scheduler half: which instants the shipped
schedule wakes at, the plan every run refreshes, the body's skip and its
failure, the start-up catch-up, and the release-day grid through the real
:class:`~corollary.engine.scheduler.Scheduler` loop.

**Every schedule here is the shipped one**, read off ``context_jobs`` -- never
an :class:`EconFirstPrintCadence` built by hand -- so a change to what
``_econ_first_prints_job`` schedules with fails here.
``test_a_fixed_grid_or_a_plain_10_00_cadence_fails_the_09_25_check`` proves
that on three substitutes: the check the 09:25 test makes rejects each one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from corollary.data.econ_first_prints import is_due
from corollary.data.econ_store import read_attempt, read_economic_releases, read_value
from corollary.data.providers.fred import (
    FRED_API_KEY_ENV,
    FredError,
    FredRefusal,
    FredReleaseDate,
    FredVintageWindow,
    VintageFrequency,
)
from corollary.db.models import Base, EconReleaseValue
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine import scheduler as scheduler_module
from corollary.engine.scheduler import (
    ECON_FIRST_PRINTS_JOB,
    AtTime,
    ContextServices,
    EconFirstPrintCadence,
    EconFirstPrintsFailed,
    EveryInterval,
    JobSkipped,
    ReleaseAttemptPlan,
    Schedule,
    ScheduledJob,
    Scheduler,
    context_jobs,
    trading_days,
)
from tests.data.test_econ_first_prints import (
    BOP_DAY,
    BOP_WINDOW,
    CPI_DAY,
    CPI_WINDOW,
    KEY,
    PAYEMS_DAY,
    REFUSAL,
    SEEDS,
    WED,
    FakeVintages,
    not_posted,
    store_rows,
)
from tests.engine.test_calendar_jobs import FakeReleases
from tests.engine.test_scheduler import FakeClock, no_secrets, run_until_parked

ET: Final = ZoneInfo("America/New_York")

#: The release the 09:25 check is about: the seed's 09:15 time (G.17).
INDPRO: Final = "INDPRO"


def _et(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=ET).astimezone(UTC)


def _series_id(release_id: int) -> str:
    row = SEEDS.series.by_id(release_id)
    assert row is not None
    return row.series_id


def _release_name(release_id: int) -> str:
    row = SEEDS.times.by_id(release_id)
    assert row is not None
    return row.release_name


class Clock:
    """A clock a test moves by hand, between direct calls to a job's body."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class TimedVintages(FakeVintages):
    """:class:`FakeVintages` that also notes the clock's instant of every request."""

    def __init__(
        self, script: dict[str, Sequence[object]], clock: Callable[[], datetime]
    ) -> None:
        super().__init__(script)
        self.clock = clock
        self.asked: list[tuple[str, datetime]] = []

    async def vintage_window(
        self, series_id: str, release_day: date, *, frequency: VintageFrequency
    ) -> FredVintageWindow | FredRefusal:
        self.asked.append((series_id, self.clock()))
        return await super().vintage_window(series_id, release_day, frequency=frequency)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _services(
    session_factory: Callable[[], Session],
    *,
    vintages: FakeVintages | None = None,
    releases: FakeReleases | None = None,
) -> ContextServices:
    return ContextServices(
        funds=(),
        session_factory=session_factory,
        fred_vintages=vintages,
        fred_releases=releases,
    )


def _shipped(services: ContextServices, clock: Callable[[], datetime], name: str) -> ScheduledJob:
    found = [job for job in context_jobs(services, clock=clock) if job.name == name]
    assert len(found) == 1, name
    return found[0]


def _shipped_pair(
    services: ContextServices, clock: Callable[[], datetime]
) -> tuple[ScheduledJob, ScheduledJob]:
    """``econ_first_prints`` and ``calendar_releases`` from **one** shipped job set."""
    jobs = {job.name: job for job in context_jobs(services, clock=clock)}
    return jobs[ECON_FIRST_PRINTS_JOB], jobs["calendar_releases"]


def _plan(job: ScheduledJob) -> ReleaseAttemptPlan:
    assert isinstance(job.schedule, EconFirstPrintCadence)
    return job.schedule.plan


def _walk(schedule: Schedule, start: datetime, until: datetime) -> list[datetime]:
    """Every wake after ``start`` up to ``until``, asked the way the scheduler asks.

    The first from ``start``, then each from the slot before it
    (``Scheduler._following``), with the plan left as it is.
    """
    wakes: list[datetime] = []
    slot = schedule.next_run(start)
    while slot is not None and slot <= until:
        wakes.append(slot)
        slot = schedule.next_run(slot)
    return wakes


def _count(engine: Engine, model: type[Base]) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


# --------------------------------------------------------------------------
# Through the real scheduler loop: each release's first request
# --------------------------------------------------------------------------


async def _requests(
    engine: Engine,
    releases: Sequence[tuple[int, date]],
    *,
    start: datetime,
    stop: datetime,
) -> list[tuple[str, datetime]]:
    """Run the shipped job alone through :class:`Scheduler` on a fake clock; every request, timed.

    The rows are stored before the scheduler starts, so the start-up
    catch-up reads them. FRED never posts, so every due slot requests.
    """
    store_rows(engine, *releases)
    clock = FakeClock(start, stop_at=stop, wait_for_threads=True)
    vintages = TimedVintages(
        {_series_id(release_id): [not_posted] for release_id, _ in releases}, clock
    )
    services = _services(lambda: Session(engine), vintages=vintages)
    job = _shipped(services, clock, ECON_FIRST_PRINTS_JOB)
    scheduler = Scheduler([job], secrets=no_secrets, clock=clock, sleep=clock.sleep)
    await run_until_parked(scheduler, clock)
    return vintages.asked


def _first_request(asked: Sequence[tuple[str, datetime]], series_id: str) -> datetime | None:
    return next((at for sid, at in asked if sid == series_id), None)


def _assert_first_requests(
    asked: Sequence[tuple[str, datetime]], expected: Mapping[str, datetime]
) -> None:
    """The check the first-attempt tests make -- and the mutants below must fail."""
    for series_id, first in expected.items():
        assert _first_request(asked, series_id) == first, (series_id, asked)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("release_id", "first"),
    [(10, _et(WED, 8, 40)), (13, _et(WED, 9, 25)), (192, _et(WED, 10, 10))],
    ids=["08:30-is-first-tried-at-08:40", "09:15-is-first-tried-at-09:25", "10:00-at-10:10"],
)
async def test_each_release_alone_is_first_requested_ten_minutes_after_it(
    engine: Engine, release_id: int, first: datetime
) -> None:
    asked = await _requests(
        engine, [(release_id, WED)], start=_et(WED, 7, 0), stop=_et(WED, 10, 15)
    )
    _assert_first_requests(asked, {_series_id(release_id): first})


@pytest.mark.asyncio
async def test_the_09_15_release_is_first_requested_at_09_25(engine: Engine) -> None:
    """The guard the audit asked for (U6e M1): not 09:30, 09:40 or 10:00."""
    asked = await _requests(engine, [(13, WED)], start=_et(WED, 7, 0), stop=_et(WED, 10, 15))
    assert _series_id(13) == INDPRO
    _assert_first_requests(asked, {INDPRO: _et(WED, 9, 25)})


@pytest.mark.asyncio
async def test_all_three_on_one_day_each_keep_their_own_grid_and_share_the_catch_up(
    engine: Engine,
) -> None:
    asked = await _requests(
        engine,
        [(10, WED), (13, WED), (192, WED)],
        start=_et(WED, 7, 0),
        stop=_et(WED, 10, 15),
    )
    # 30-minute retries on each release's own grid; the 10:00 catch-up asks
    # both releases already out, and not the 10:00 one, first due at 10:10.
    assert asked == [
        ("CPIAUCSL", _et(WED, 8, 40)),
        ("CPIAUCSL", _et(WED, 9, 10)),
        (INDPRO, _et(WED, 9, 25)),
        ("CPIAUCSL", _et(WED, 9, 40)),
        (INDPRO, _et(WED, 9, 55)),
        ("CPIAUCSL", _et(WED, 10, 0)),
        (INDPRO, _et(WED, 10, 0)),
        ("CPIAUCSL", _et(WED, 10, 10)),
        ("JTSJOL", _et(WED, 10, 10)),
    ]


@dataclass(frozen=True, slots=True)
class _TenFortyGrid:
    """A fixed 30-minute grid on :10 and :40 -- the 08:30 release's grid, for every release."""

    def next_run(self, after: datetime) -> datetime | None:
        anchor = datetime(2000, 1, 1, 0, 10, tzinfo=UTC)
        every = timedelta(minutes=30)
        return anchor + every * ((after.astimezone(UTC) - anchor) // every + 1)

    def describe(self) -> str:
        return "every 30 min on :10 and :40"


def _half_hour_grid(plan: ReleaseAttemptPlan) -> Schedule:
    return EveryInterval(timedelta(minutes=30))


def _ten_forty_grid(plan: ReleaseAttemptPlan) -> Schedule:
    return _TenFortyGrid()


def _plain_10_00(plan: ReleaseAttemptPlan) -> Schedule:
    return AtTime(time(10, 0), trading_days)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutant", "moved_to"),
    [
        (_half_hour_grid, _et(WED, 9, 30)),
        (_ten_forty_grid, _et(WED, 9, 40)),
        (_plain_10_00, _et(WED, 10, 0)),
    ],
    ids=["30-min-grid-on-:00-:30", "30-min-grid-on-:10-:40", "plain-10:00-AtTime"],
)
async def test_a_fixed_grid_or_a_plain_10_00_cadence_fails_the_09_25_check(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    mutant: Callable[[ReleaseAttemptPlan], Schedule],
    moved_to: datetime,
) -> None:
    # Substitute the cadence the shipped job is built with. The body still
    # refreshes its plan; the substitute simply ignores it, as a fixed grid
    # would. The 09:15 release's first request moves, and the check fails.
    monkeypatch.setattr(scheduler_module, "EconFirstPrintCadence", mutant)
    asked = await _requests(engine, [(13, WED)], start=_et(WED, 7, 0), stop=_et(WED, 10, 15))
    assert _first_request(asked, INDPRO) == moved_to
    with pytest.raises(AssertionError):
        _assert_first_requests(asked, {INDPRO: _et(WED, 9, 25)})


# --------------------------------------------------------------------------
# The shipped schedule's wake sequence
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_all_three_pending_the_wakes_are_their_grids_and_the_catch_up(
    engine: Engine,
) -> None:
    """The audit's sequence for Wed 2026-11-18: 08:40 ... 17:55, then Thursday's 10:00."""
    store_rows(engine, (10, WED), (13, WED), (192, WED))
    clock = Clock(_et(WED, 7, 0))
    job = _shipped(
        _services(lambda: Session(engine), vintages=FakeVintages({})), clock, ECON_FIRST_PRINTS_JOB
    )
    # The body fills the plan: nothing is due at 07:00, all three pending.
    assert isinstance(await job.run(), JobSkipped)

    thursday = date(2026, 11, 19)
    wakes = _walk(job.schedule, clock(), _et(thursday, 10, 0))
    head = [
        _et(WED, 8, 40),
        _et(WED, 9, 10),
        _et(WED, 9, 25),
        _et(WED, 9, 40),
        _et(WED, 9, 55),
        _et(WED, 10, 0),  # the catch-up
    ]
    # From 10:10 the three grids interleave: :10, :25, :40, :55 to 17:55.
    tail = [_et(WED, 10, 10) + timedelta(minutes=15) * k for k in range(32)]
    assert tail[-2:] == [_et(WED, 17, 40), _et(WED, 17, 55)]
    assert wakes == [*head, *tail, _et(thursday, 10, 0)]
    # Nothing between 18:00 Wednesday and Thursday's catch-up.
    assert job.schedule.next_run(_et(WED, 17, 55)) == _et(thursday, 10, 0)
    assert job.schedule.next_run(_et(WED, 18, 0)) == _et(thursday, 10, 0)


def test_the_catch_up_follows_the_nyse_calendar_holidays_weekends_and_half_days() -> None:
    job = _shipped(_services(lambda: None), Clock(_et(WED, 7, 0)), ECON_FIRST_PRINTS_JOB)  # type: ignore[arg-type, return-value]
    assert _plan(job).pending == ()
    schedule = job.schedule
    # Thanksgiving (Thu 26 Nov) has none; the half-day Friday runs at 10:00.
    assert schedule.next_run(_et(date(2026, 11, 25), 10, 0)) == _et(date(2026, 11, 27), 10, 0)
    # A weekend.
    assert schedule.next_run(_et(date(2026, 11, 20), 10, 0)) == _et(date(2026, 11, 23), 10, 0)
    # Christmas Eve's half-day runs; Christmas and the weekend roll to Mon 28 Dec.
    assert schedule.next_run(_et(date(2026, 12, 23), 10, 0)) == _et(date(2026, 12, 24), 10, 0)
    assert schedule.next_run(_et(date(2026, 12, 24), 10, 0)) == _et(date(2026, 12, 28), 10, 0)


def test_a_release_on_thanksgiving_eve_is_next_due_at_the_half_days_catch_up() -> None:
    job = _shipped(_services(lambda: None), Clock(_et(WED, 7, 0)), ECON_FIRST_PRINTS_JOB)  # type: ignore[arg-type, return-value]
    wed = date(2026, 11, 25)
    released = _et(wed, 8, 30)
    _plan(job).replace([released])
    friday = _et(date(2026, 11, 27), 10, 0)
    assert job.schedule.next_run(_et(wed, 17, 10)) == _et(wed, 17, 40)
    assert job.schedule.next_run(_et(wed, 17, 40)) == friday
    assert is_due(released, _et(wed, 17, 40), friday)


def test_spring_forward_the_fridays_release_is_next_due_monday_at_10_00_edt() -> None:
    job = _shipped(_services(lambda: None), Clock(_et(WED, 7, 0)), ECON_FIRST_PRINTS_JOB)  # type: ignore[arg-type, return-value]
    friday = date(2027, 3, 12)
    released = _et(friday, 8, 30)
    assert released == datetime(2027, 3, 12, 13, 30, tzinfo=UTC)  # EST
    _plan(job).replace([released])
    last = datetime(2027, 3, 12, 22, 40, tzinfo=UTC)  # 17:40 EST
    assert job.schedule.next_run(_et(friday, 17, 10)) == last
    # The clocks change on Sunday 14 March: Monday's 10:00 ET is 14:00Z, not 15:00Z.
    monday = datetime(2027, 3, 15, 14, 0, tzinfo=UTC)
    assert job.schedule.next_run(last) == monday == _et(date(2027, 3, 15), 10, 0)
    assert is_due(released, last, monday)


# --------------------------------------------------------------------------
# The body: the plan, the skip, the failure, the catch-up
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_run_replaces_the_plan_and_a_filled_release_leaves_it(engine: Engine) -> None:
    store_rows(engine, (10, CPI_DAY), (13, CPI_DAY))
    cpi_at, indpro_at = _et(CPI_DAY, 8, 30), _et(CPI_DAY, 9, 15)
    clock = Clock(_et(CPI_DAY, 7, 0))
    vintages = FakeVintages({"CPIAUCSL": [not_posted, CPI_WINDOW], INDPRO: [not_posted]})
    job = _shipped(_services(lambda: Session(engine), vintages=vintages), clock, ECON_FIRST_PRINTS_JOB)
    plan = _plan(job)
    assert plan.pending == ()

    # 07:00, nothing due: the run fills the plan, and the next wake is 08:40.
    assert isinstance(await job.run(), JobSkipped)
    assert plan.pending == (cpi_at, indpro_at)
    assert job.schedule.next_run(clock()) == _et(CPI_DAY, 8, 40)

    # 08:40: CPI not yet posted -- still pending, retried at 09:10.
    clock.now = _et(CPI_DAY, 8, 40)
    assert await job.run() is None
    assert plan.pending == (cpi_at, indpro_at)
    assert job.schedule.next_run(clock()) == _et(CPI_DAY, 9, 10)

    # 09:10: CPI lands and leaves the plan. The next wake is the 09:15
    # release's 09:25, not CPI's 09:40.
    clock.now = _et(CPI_DAY, 9, 10)
    assert await job.run() is None
    assert plan.pending == (indpro_at,)
    assert job.schedule.next_run(clock()) == _et(CPI_DAY, 9, 25)

    # The rest of the day, a run at every wake: CPI is never asked again.
    ran: list[datetime] = []
    wake = job.schedule.next_run(clock())
    while wake is not None and wake.astimezone(ET).date() == CPI_DAY:
        clock.now = wake
        ran.append(wake)
        await job.run()
        wake = job.schedule.next_run(wake)
    assert ran[0] == _et(CPI_DAY, 9, 25) and ran[-1] == _et(CPI_DAY, 17, 55)
    assert _et(CPI_DAY, 9, 40) not in ran
    assert [call[0] for call in vintages.calls].count("CPIAUCSL") == 2
    # Past the 09:15 release's last slot the plan is empty; next is the catch-up.
    assert plan.pending == ()
    assert wake == _et(CPI_DAY + timedelta(days=1), 10, 0)
    with Session(engine) as session:
        assert read_value(session, 10, CPI_DAY) is not None


@pytest.mark.asyncio
async def test_with_no_fred_key_the_job_skips_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_read() -> Session:
        raise AssertionError("the no-key skip read the database")

    def no_seed_load() -> object:
        raise AssertionError("the no-key skip loaded the econ seeds")

    monkeypatch.setattr(scheduler_module, "load_econ_release_seeds", no_seed_load)
    job = _shipped(_services(no_read, vintages=None), Clock(_et(WED, 8, 40)), ECON_FIRST_PRINTS_JOB)
    outcome = await job.run()
    assert isinstance(outcome, JobSkipped)
    assert FRED_API_KEY_ENV in outcome.reason
    assert _plan(job).pending == ()
    # The catch-up is the same body, so it skips the same way at start.
    assert job.catch_up is not None
    assert isinstance(await job.catch_up(), JobSkipped)


@pytest.mark.asyncio
async def test_the_failure_is_raised_only_after_every_due_release_ran(engine: Engine) -> None:
    monday = date(2026, 11, 9)
    store_rows(engine, (10, CPI_DAY), (51, BOP_DAY), (50, PAYEMS_DAY), (13, monday))
    leaked = FredError(
        f"GET https://api.stlouisfed.org/fred/series/observations?api_key={KEY} failed"
    )
    vintages = FakeVintages(
        {
            # The first release in calendar order fails...
            "CPIAUCSL": [leaked],
            # ...and every one after it is still asked and recorded.
            "BOPGSTB": [BOP_WINDOW],
            "PAYEMS": [FredRefusal("PAYEMS", PAYEMS_DAY, REFUSAL)],
            INDPRO: [not_posted],
        }
    )
    clock = Clock(_et(monday, 10, 0))
    job = _shipped(_services(lambda: Session(engine), vintages=vintages), clock, ECON_FIRST_PRINTS_JOB)

    with pytest.raises(EconFirstPrintsFailed) as raised:
        await job.run()

    message = str(raised.value)
    assert message.startswith("1 of 4 economic first-print attempts failed")
    assert "release 10 on 2026-10-14 (CPIAUCSL): error, FredError" in message
    assert KEY not in message
    assert vintages.calls[0][0] == "CPIAUCSL"
    assert sorted(call[0] for call in vintages.calls) == ["BOPGSTB", "CPIAUCSL", INDPRO, "PAYEMS"]
    with Session(engine) as session:
        cpi = read_attempt(session, 10, CPI_DAY)
        payems = read_attempt(session, 50, PAYEMS_DAY)
        indpro = read_attempt(session, 13, monday)
        assert read_value(session, 51, BOP_DAY) is not None
    assert cpi is not None and cpi.attempt.outcome == "error"
    assert payems is not None and payems.attempt.outcome == "refused"
    assert indpro is not None and indpro.attempt.outcome == "not_posted"
    # The plan was refreshed before the raise: the schedule still wakes for
    # Monday's 09:15 release on its own grid.
    assert _plan(job).pending == (_et(monday, 9, 15),)
    assert job.schedule.next_run(clock()) == _et(monday, 10, 25)


@pytest.mark.asyncio
async def test_the_start_up_catch_up_is_the_body_and_fetches_a_missed_slot(engine: Engine) -> None:
    job = _shipped(_services(lambda: None), Clock(_et(WED, 7, 0)), ECON_FIRST_PRINTS_JOB)  # type: ignore[arg-type, return-value]
    assert job.catch_up is not None and job.catch_up is job.run
    # A start at 08:50 missed the 08:30 release's 08:40: the catch-up asks
    # at once, and the next request is back on the release's own grid.
    asked = await _requests(engine, [(10, WED)], start=_et(WED, 8, 50), stop=_et(WED, 9, 15))
    assert asked == [("CPIAUCSL", _et(WED, 8, 50)), ("CPIAUCSL", _et(WED, 9, 10))]


# --------------------------------------------------------------------------
# With the dates refresh: the stored value, and the plan's known limit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dates_refresh_leaves_a_value_the_job_stored_alone(engine: Engine) -> None:
    """Spec U6's done-when, at the job level: econ lands a figure, the real refresh runs over it."""
    store_rows(engine, (10, CPI_DAY))
    clock = Clock(_et(CPI_DAY, 8, 40))
    vintages = FakeVintages({"CPIAUCSL": [CPI_WINDOW]})
    later = date(2026, 11, 10)
    releases = FakeReleases(
        [
            FredReleaseDate(10, _release_name(10), CPI_DAY),
            FredReleaseDate(10, _release_name(10), later),
        ]
    )
    econ, refresh = _shipped_pair(
        _services(lambda: Session(engine), vintages=vintages, releases=releases), clock
    )

    assert await econ.run() is None
    with Session(engine) as session:
        before = session.get(EconReleaseValue, (10, CPI_DAY))
        assert before is not None
        snapshot = (before.actual, before.prior, before.period, before.fetched_at)

    # The same day's 10:00 refresh: CPI_DAY is inside its window, so it
    # rewrites that very calendar row.
    clock.now = _et(CPI_DAY, 10, 0)
    assert await refresh.run() is None
    ((start, end),) = releases.calls
    assert start <= CPI_DAY <= end

    with Session(engine) as session:
        after = session.get(EconReleaseValue, (10, CPI_DAY))
        assert after is not None
        assert (after.actual, after.prior, after.period, after.fetched_at) == snapshot
        cpi, _ = read_economic_releases(session, CPI_DAY, later, seeds=SEEDS)
    assert _count(engine, EconReleaseValue) == 1
    assert cpi.value is not None and cpi.value.actual == snapshot[0]
    assert cpi.display(clock()).state == "filled"
    # And the job, reading it through the join, never asks again.
    assert isinstance(await econ.run(), JobSkipped)
    assert len(vintages.calls) == 1


@pytest.mark.asyncio
async def test_known_limit_a_row_stored_after_the_jobs_last_run_waits_for_the_next_catch_up(
    engine: Engine,
) -> None:
    """U6e audit L2, documented rather than fixed: see ``econ_first_prints``'s module docstring.

    The plan learns of a row only when this job runs. Here the dates refresh
    writes Wednesday's CPI just after the job's Tuesday 10:00 run read the
    table, so Wednesday's 08:40 is not woken. What this pins is the bound:
    nothing is lost -- Wednesday's 10:00 catch-up asks, and the release is
    back on its own grid at 10:10. A fix (a scheduler that re-asks a
    schedule mid-sleep) turns the first ``next_run`` into 08:40.
    """
    tuesday = date(2026, 11, 17)
    clock = Clock(_et(tuesday, 10, 0))
    vintages = FakeVintages({"CPIAUCSL": [not_posted]})
    releases = FakeReleases([FredReleaseDate(10, _release_name(10), WED)])
    econ, refresh = _shipped_pair(
        _services(lambda: Session(engine), vintages=vintages, releases=releases), clock
    )

    # 10:00 Tuesday, both slots: econ reads first and finds nothing...
    assert isinstance(await econ.run(), JobSkipped)
    # ...then the refresh writes the row.
    assert await refresh.run() is None
    with Session(engine) as session:
        assert len(read_economic_releases(session, WED, WED, seeds=SEEDS)) == 1

    # The limit: the plan does not hold it, so the next wake is the catch-up.
    assert _plan(econ).pending == ()
    assert econ.schedule.next_run(clock()) == _et(WED, 10, 0)

    # The bound: that catch-up asks, and the grid resumes.
    clock.now = _et(WED, 10, 0)
    assert await econ.run() is None
    assert vintages.calls == [("CPIAUCSL", WED, "monthly")]
    assert _plan(econ).pending == (_et(WED, 8, 30),)
    assert econ.schedule.next_run(clock()) == _et(WED, 10, 10)
