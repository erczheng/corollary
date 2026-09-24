"""``fred_observation`` storage and the ``DGS3MO`` refresh behind the risk-free rate.

Decision 19: the rate in use is the latest non-missing stored ``DGS3MO``
observation; the 0.0425 default only when none was ever obtained. Real SQLite
file, no vendor: the FRED side is a fake returning parsed recorded fixtures.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from corollary.data.macro.risk_free import (
    NotRefreshed,
    catch_up_dgs3mo,
    latest_dgs3mo_rate,
    refresh_dgs3mo,
    seed_rate_source,
    store_observations,
)
from corollary.calendars import NYSE_TZ
from corollary.data.providers.fred import FredError, FredObservation, parse_observations
from corollary.db.models import Base, FredObservationRecord
from corollary.db.session import create_db_engine, sqlite_url
from corollary.pricing.rates import (
    FALLBACK_RISK_FREE_RATE,
    RateProvenance,
    RiskFreeRateSource,
    rate_from_dgs3mo,
)
from tests.data.providers.test_fred_provider import dgs3mo_body

FETCHED = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)


def recorded() -> list[FredObservation]:
    return parse_observations("DGS3MO", dgs3mo_body())


def _fetched() -> datetime:
    return FETCHED


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "fred.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(db_engine: Engine) -> Callable[[], Session]:
    return lambda: Session(db_engine)


def _rows(sessions: Callable[[], Session]) -> dict[date, Decimal | None]:
    with sessions() as session:
        return {
            row.date: row.value
            for row in session.scalars(
                select(FredObservationRecord).where(
                    FredObservationRecord.series_id == "DGS3MO"
                )
            )
        }


class FakeFred:
    def __init__(
        self,
        answer: Sequence[FredObservation] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.answer = list(recorded() if answer is None else answer)
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    async def observations(self, series_id: str, *, limit: int = 10) -> list[FredObservation]:
        self.calls.append((series_id, limit))
        if self.fail is not None:
            raise self.fail
        return list(self.answer)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_stored_observations_yield_the_latest_as_a_fred_rate(
    sessions: Callable[[], Session],
) -> None:
    with sessions() as session:
        assert store_observations(session, recorded(), fetched_at=FETCHED) == 10
        session.commit()
    with sessions() as session:
        rate = latest_dgs3mo_rate(session)
    assert rate == rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22))
    assert rate is not None and rate.rate == Decimal("0.0416")


def test_an_empty_table_has_no_rate(sessions: Callable[[], Session]) -> None:
    with sessions() as session:
        assert latest_dgs3mo_rate(session) is None


def test_storing_twice_is_idempotent_and_a_revision_overwrites(
    sessions: Callable[[], Session],
) -> None:
    observations = recorded()
    with sessions() as session:
        store_observations(session, observations, fetched_at=FETCHED)
        store_observations(session, observations, fetched_at=FETCHED)
        session.commit()
    assert len(_rows(sessions)) == 10

    revised = [replace(observations[0], value=Decimal("4.17")), *observations[1:]]
    with sessions() as session:
        store_observations(session, revised, fetched_at=FETCHED)
        session.commit()
    rows = _rows(sessions)
    assert rows[date(2026, 9, 22)] == Decimal("4.17")
    with sessions() as session:
        count = session.scalar(select(func.count()).select_from(FredObservationRecord))
    assert count == 10


def test_a_missing_newest_observation_is_stored_as_null_and_skipped(
    sessions: Callable[[], Session],
) -> None:
    observations = recorded()
    gap = [replace(observations[0], value=None), *observations[1:]]
    with sessions() as session:
        store_observations(session, gap, fetched_at=FETCHED)
        session.commit()
        rate = latest_dgs3mo_rate(session)
    assert _rows(sessions)[date(2026, 9, 22)] is None
    second = observations[1]
    assert second.value is not None
    assert rate == rate_from_dgs3mo(second.value, second.date)


def test_a_retraction_overwrites_the_stale_value(sessions: Callable[[], Session]) -> None:
    """A value FRED later replaces with ``"."`` must stop being the rate."""
    observations = recorded()
    with sessions() as session:
        store_observations(session, observations, fetched_at=FETCHED)
        session.commit()
    with sessions() as session:
        store_observations(
            session, [replace(observations[0], value=None)], fetched_at=FETCHED
        )
        session.commit()
        rate = latest_dgs3mo_rate(session)
    assert rate is not None
    assert rate.observation_date == observations[1].date


def test_a_naive_fetched_at_is_refused(sessions: Callable[[], Session]) -> None:
    with sessions() as session, pytest.raises(ValueError):
        store_observations(session, recorded(), fetched_at=datetime(2026, 9, 23, 14, 0))


# --------------------------------------------------------------------------
# The rate source: seeding and refreshing
# --------------------------------------------------------------------------


def test_seeding_from_an_empty_table_keeps_the_fallback_and_says_so(
    sessions: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    rates = RiskFreeRateSource()
    with caplog.at_level(logging.INFO):
        assert seed_rate_source(sessions, rates) == FALLBACK_RISK_FREE_RATE
    assert rates.current() == FALLBACK_RISK_FREE_RATE
    assert any(getattr(r, "event", None) == "risk_free_rate_seeded" for r in caplog.records)


def test_seeding_from_a_stored_observation_adopts_it(
    sessions: Callable[[], Session],
) -> None:
    with sessions() as session:
        store_observations(session, recorded(), fetched_at=FETCHED)
        session.commit()
    rates = RiskFreeRateSource()
    seed_rate_source(sessions, rates)
    assert rates.current().provenance is RateProvenance.FRED_DGS3MO
    assert rates.current().observation_date == date(2026, 9, 22)


@pytest.mark.asyncio
async def test_a_refresh_stores_and_adopts_the_latest_observation(
    sessions: Callable[[], Session],
) -> None:
    rates = RiskFreeRateSource()
    fred = FakeFred()
    adopted = await refresh_dgs3mo(
        fred=fred, session_factory=sessions, rates=rates, now=_fetched
    )
    assert fred.calls == [("DGS3MO", 10)]
    assert adopted == rates.current()
    assert rates.current() == rate_from_dgs3mo(Decimal("4.16"), date(2026, 9, 22))
    assert len(_rows(sessions)) == 10


@pytest.mark.asyncio
async def test_a_failed_refresh_raises_and_leaves_the_rate_alone(
    sessions: Callable[[], Session],
) -> None:
    rates = RiskFreeRateSource()
    await refresh_dgs3mo(fred=FakeFred(), session_factory=sessions, rates=rates, now=_fetched)
    before = rates.current()
    with pytest.raises(FredError):
        await refresh_dgs3mo(
            fred=FakeFred(fail=FredError("GET /series/observations returned 503")),
            session_factory=sessions,
            rates=rates,
            now=_fetched,
        )
    assert rates.current() == before  # the last observation, never the default


@pytest.mark.asyncio
async def test_a_response_of_only_gaps_leaves_the_fallback_in_place(
    sessions: Callable[[], Session],
) -> None:
    rates = RiskFreeRateSource()
    gaps = [replace(o, value=None) for o in recorded()]
    adopted = await refresh_dgs3mo(
        fred=FakeFred(gaps), session_factory=sessions, rates=rates, now=_fetched
    )
    assert isinstance(adopted, NotRefreshed)
    assert "no DGS3MO value" in adopted.reason
    assert rates.current() == FALLBACK_RISK_FREE_RATE


@pytest.mark.asyncio
async def test_catch_up_fetches_only_when_nothing_is_stored(
    sessions: Callable[[], Session],
) -> None:
    rates = RiskFreeRateSource()
    fred = FakeFred()
    first = await catch_up_dgs3mo(
        fred=fred, session_factory=sessions, rates=rates, now=_fetched
    )
    assert first == rates.current()
    assert len(fred.calls) == 1
    # A restart (or a --reload save) with a current row present does not
    # re-fetch -- and says it did nothing, rather than returning quietly.
    second = await catch_up_dgs3mo(
        fred=fred, session_factory=sessions, rates=rates, now=_fetched
    )
    assert len(fred.calls) == 1
    assert isinstance(second, NotRefreshed)
    assert "2026-09-22" in second.reason


def _at_et(day: date, hour: int = 12) -> Callable[[], datetime]:
    moment = datetime(day.year, day.month, day.day, hour, tzinfo=NYSE_TZ)
    return lambda: moment.astimezone(timezone.utc)


async def _stored_through_sep_22(sessions: Callable[[], Session]) -> None:
    """The recorded response: ten sessions, the latest dated Tue 2026-09-22."""
    with sessions() as session:
        store_observations(session, recorded(), fetched_at=FETCHED)
        session.commit()


@pytest.mark.parametrize(
    ("today", "fetches"),
    [
        # Two sessions before Thu 24 Sep is Tue 22 Sep: FRED publishes a close
        # the next morning, so on Thursday the 22nd may be the newest there
        # is. At the boundary: current, no fetch.
        (date(2026, 9, 24), False),
        # Before Fri 25 Sep, two sessions back is Wed 23 Sep: the stored 22nd
        # is a session behind what FRED has had since Thursday morning.
        (date(2026, 9, 25), True),
        # Three weeks offline: the finding this rule exists for.
        (date(2026, 10, 14), True),
    ],
)
@pytest.mark.asyncio
async def test_catch_up_also_fetches_when_the_stored_observation_is_stale(
    sessions: Callable[[], Session], today: date, fetches: bool
) -> None:
    await _stored_through_sep_22(sessions)
    rates = RiskFreeRateSource()
    fred = FakeFred()
    outcome = await catch_up_dgs3mo(
        fred=fred, session_factory=sessions, rates=rates, now=_at_et(today)
    )
    assert (len(fred.calls) == 1) is fetches
    assert isinstance(outcome, NotRefreshed) is not fetches


@pytest.mark.asyncio
async def test_catch_up_counts_sessions_not_calendar_days(
    sessions: Callable[[], Session],
) -> None:
    """Across a weekend: Monday's two sessions back are Thursday and Friday."""
    thursday = [replace(o, date=date(2026, 9, 24)) for o in recorded()[-1:]]
    with sessions() as session:
        store_observations(session, thursday, fetched_at=FETCHED)
        session.commit()
    fred = FakeFred()
    outcome = await catch_up_dgs3mo(
        fred=fred,
        session_factory=sessions,
        rates=RiskFreeRateSource(),
        now=_at_et(date(2026, 9, 28), hour=9),
    )
    assert fred.calls == []
    assert isinstance(outcome, NotRefreshed)


@pytest.mark.asyncio
async def test_a_gap_row_counts_as_fetched_for_staleness(
    sessions: Callable[[], Session],
) -> None:
    """A ``"."`` row (a bond-market holiday) proves FRED was read that recently.

    Measured from non-missing rows only, a morning after a bond holiday
    would look stale and re-fetch on every ``--reload`` save.
    """
    await _stored_through_sep_22(sessions)
    gap = FredObservation(series_id="DGS3MO", date=date(2026, 9, 23), value=None)
    with sessions() as session:
        store_observations(session, [gap], fetched_at=FETCHED)
        session.commit()
    fred = FakeFred()
    outcome = await catch_up_dgs3mo(
        fred=fred,
        session_factory=sessions,
        rates=RiskFreeRateSource(),
        now=_at_et(date(2026, 9, 25)),
    )
    assert fred.calls == []
    assert isinstance(outcome, NotRefreshed)


@pytest.mark.asyncio
async def test_without_fred_the_jobs_do_nothing_and_say_so(
    sessions: Callable[[], Session],
) -> None:
    rates = RiskFreeRateSource()
    refreshed = await refresh_dgs3mo(fred=None, session_factory=sessions, rates=rates)
    caught_up = await catch_up_dgs3mo(fred=None, session_factory=sessions, rates=rates)
    for outcome in (refreshed, caught_up):
        assert isinstance(outcome, NotRefreshed)
        assert "FRED_API_KEY" in outcome.reason
    assert rates.current() == FALLBACK_RISK_FREE_RATE
    assert _rows(sessions) == {}
