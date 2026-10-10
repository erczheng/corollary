"""FRED release dates -> ``economic`` calendar inputs (unit 7.2b-R). No live calls.

The recorded window (``tests/fixtures/fred/p3_release_dates_forward.json``,
2026-10-10 .. 2026-11-09) crosses the 2026-11-01 end of daylight time, and
carries every one of the fifteen timed releases at least once -- so the times
are proven through the committed table, from FRED's own dates, on both sides
of a clock change.
"""

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import corollary.data.calendar_releases as releases
from corollary.data.calendar_event import CalendarEventInput, CalendarKind, CalendarSource
from corollary.data.calendar_releases import (
    FRED_RELEASES_HORIZON_DAYS,
    IgnoredRelease,
    ReleaseEvents,
    ReleasesNotFetched,
    actual_for,
    fetch_release_events,
    prior_for,
    release_events,
    release_vendor_id,
)
from corollary.data.providers.fred import FRED_API_KEY_ENV, FredReleaseDate, parse_release_dates
from corollary.data.seeds.calendar_seeds import load_econ_release_times

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fred" / "p3_release_dates_forward.json"

TIMES = load_econ_release_times()
START = date(2026, 10, 10)
END = date(2026, 11, 9)

CPI, EMPLOYMENT, CONSTRUCTION_SPENDING, G17 = 10, 50, 229, 13


def recorded_rows() -> tuple[FredReleaseDate, ...]:
    envelope = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert envelope["params"]["realtime_start"] == START.isoformat()
    assert envelope["params"]["realtime_end"] == END.isoformat()
    return parse_release_dates(envelope["body"]).rows


def recorded() -> ReleaseEvents:
    return release_events(recorded_rows(), TIMES, start=START, end=END)


def by_vendor_id(result: ReleaseEvents) -> dict[str, CalendarEventInput]:
    return {e.vendor_id: e for e in result.events}


def _row(release_id: int, day: date, name: str | None = None) -> FredReleaseDate:
    if name is None:
        known = TIMES.by_id(release_id)
        name = known.release_name if known is not None else f"Release {release_id}"
    return FredReleaseDate(release_id=release_id, release_name=name, date=day)


# --------------------------------------------------------------------------
# The recorded window
# --------------------------------------------------------------------------


def test_every_timed_release_date_in_the_recording_becomes_one_event() -> None:
    rows = recorded_rows()
    timed = {(r.release_id, r.date) for r in rows if TIMES.by_id(r.release_id) is not None}
    result = recorded()

    assert len(result.events) == len(timed)
    assert {e.vendor_id for e in result.events} == {
        f"{rid}:{day.isoformat()}" for rid, day in timed
    }
    # The recording carries all fifteen, so nothing in the table is unscheduled.
    assert {rid for rid, _ in timed} == {row.release_id for row in TIMES.rows}
    assert result.unscheduled == ()


def test_each_event_is_an_economic_fred_row_titled_and_timed_from_the_table() -> None:
    for event in recorded().events:
        release_id_text, _, day_text = event.vendor_id.partition(":")
        release_id, day = int(release_id_text), date.fromisoformat(day_text)
        row = TIMES.by_id(release_id)
        assert row is not None
        assert event.kind is CalendarKind.ECONOMIC
        assert event.source is CalendarSource.FRED
        assert event.date == day
        assert event.at == TIMES.at(release_id, day)
        assert event.title == row.release_name
        assert event.ticker is None and event.session is None


def test_estimate_prior_and_actual_are_all_none() -> None:
    events = recorded().events
    assert events
    for event in events:
        assert event.estimate is None  # Q3: consensus is unavailable
        assert event.prior is None  # blocked on an owner decision; see prior_for
        assert event.actual is None  # blocked on an owner decision; see actual_for


def test_recorded_times_hold_across_the_end_of_daylight_time() -> None:
    events = by_vendor_id(recorded())
    # CPI, Wed 2026-10-14, 08:30 EDT (UTC-4).
    cpi = events["10:2026-10-14"]
    assert cpi.at == datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    # Employment Situation, Fri 2026-11-06, 08:30 EST (UTC-5): same wall
    # time, one hour later in UTC.
    jobs = events["50:2026-11-06"]
    assert jobs.at == datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    # A 10:00 release on the first Monday after the change.
    construction = events["229:2026-11-02"]
    assert construction.at == datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    # Stored UTC, every one.
    assert all(e.at is not None and e.at.utcoffset() == timedelta(0) for e in events.values())


def test_releases_not_in_the_table_are_ignored_and_reported() -> None:
    rows = recorded_rows()
    result = recorded()
    untimed = {r.release_id for r in rows if TIMES.by_id(r.release_id) is None}
    assert untimed  # the recording is mostly releases nobody timed

    assert {i.release_id for i in result.ignored} == untimed
    # No event for any of them -- no invented time.
    assert not any(int(e.vendor_id.split(":")[0]) in untimed for e in result.events)
    # Each reported once, with every date it was listed on.
    for ignored in result.ignored:
        assert ignored.dates == tuple(
            sorted({r.date for r in rows if r.release_id == ignored.release_id})
        )
    assert [i.release_id for i in result.ignored] == sorted(untimed)


# --------------------------------------------------------------------------
# The mapping, on hand-written rows
# --------------------------------------------------------------------------


def test_an_edt_and_an_est_date_of_one_release_differ_by_an_hour_in_utc() -> None:
    result = release_events(
        [_row(CPI, date(2026, 10, 14)), _row(CPI, date(2026, 11, 10))],
        TIMES,
        start=date(2026, 10, 1),
        end=date(2026, 11, 30),
    )
    edt, est = result.events
    assert edt.at == datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    assert est.at == datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    # And across the spring change the other way.
    spring = release_events(
        [_row(G17, date(2027, 3, 5)), _row(G17, date(2027, 3, 16))],
        TIMES,
        start=date(2027, 3, 1),
        end=date(2027, 3, 31),
    )
    assert [e.at for e in spring.events] == [
        datetime(2027, 3, 5, 14, 15, tzinfo=UTC),  # 09:15 EST
        datetime(2027, 3, 16, 13, 15, tzinfo=UTC),  # 09:15 EDT
    ]


def test_the_vendor_id_is_release_and_date() -> None:
    assert release_vendor_id(CPI, date(2026, 10, 14)) == "10:2026-10-14"
    [event] = release_events(
        [_row(CPI, date(2026, 10, 14))], TIMES, start=START, end=END
    ).events
    assert event.vendor_id == "10:2026-10-14"


def test_unknown_ids_only_is_no_events_and_every_id_reported() -> None:
    rows = [
        _row(441, date(2026, 10, 12), "Coinbase Cryptocurrencies"),
        _row(441, date(2026, 10, 11), "Coinbase Cryptocurrencies"),
        _row(101, date(2026, 10, 12), "FOMC Press Release"),
    ]
    result = release_events(rows, TIMES, start=START, end=END)
    assert result.events == ()
    assert result.ignored == (
        IgnoredRelease(release_id=101, release_name="FOMC Press Release", dates=(date(2026, 10, 12),)),
        IgnoredRelease(
            release_id=441,
            release_name="Coinbase Cryptocurrencies",
            dates=(date(2026, 10, 11), date(2026, 10, 12)),
        ),
    )


def test_a_timed_release_absent_from_the_window_is_reported_unscheduled() -> None:
    result = release_events([_row(CPI, date(2026, 10, 14))], TIMES, start=START, end=END)
    assert CPI not in result.unscheduled
    assert set(result.unscheduled) == {row.release_id for row in TIMES.rows} - {CPI}
    assert list(result.unscheduled) == sorted(result.unscheduled)


def test_a_repeated_row_is_one_event() -> None:
    row = _row(EMPLOYMENT, date(2026, 11, 6))
    result = release_events([row, row], TIMES, start=START, end=END)
    assert [e.vendor_id for e in result.events] == ["50:2026-11-06"]


def test_a_name_that_disagrees_with_the_table_is_reported_and_still_titled_from_the_table() -> None:
    result = release_events(
        [_row(CPI, date(2026, 10, 14), "Consumer Price Index (renamed)")],
        TIMES,
        start=START,
        end=END,
    )
    [event] = result.events
    assert event.title == "Consumer Price Index"
    assert result.name_mismatches == ((CPI, "Consumer Price Index (renamed)"),)


def test_the_mapping_is_deterministic_whatever_the_input_order() -> None:
    rows = list(recorded_rows())
    forward = release_events(rows, TIMES, start=START, end=END)
    backward = release_events(list(reversed(rows)), TIMES, start=START, end=END)
    assert forward == backward
    assert [e.date for e in forward.events] == sorted(e.date for e in forward.events)


def test_the_prior_and_actual_seams_answer_none() -> None:
    assert prior_for(CPI, date(2026, 10, 14)) is None
    assert actual_for(CPI, date(2026, 10, 14)) is None


# --------------------------------------------------------------------------
# The fetch
# --------------------------------------------------------------------------


class FakeFred:
    def __init__(self, rows: Sequence[FredReleaseDate]) -> None:
        self.rows = rows
        self.calls: list[tuple[date, date]] = []

    async def release_dates(self, start: date, end: date) -> Sequence[FredReleaseDate]:
        self.calls.append((start, end))
        return self.rows


@pytest.mark.asyncio
async def test_a_missing_key_is_a_skip_naming_the_variable_never_a_success() -> None:
    outcome = await fetch_release_events(None, today=START)
    assert isinstance(outcome, ReleasesNotFetched)
    assert FRED_API_KEY_ENV in outcome.reason


@pytest.mark.asyncio
async def test_the_fetch_asks_for_today_through_the_horizon_and_maps_it() -> None:
    fred = FakeFred(recorded_rows())
    outcome = await fetch_release_events(fred, today=START, horizon_days=30)
    assert fred.calls == [(START, END)]
    assert isinstance(outcome, ReleaseEvents)
    assert outcome == recorded()


@pytest.mark.asyncio
async def test_the_default_horizon_is_thirty_days() -> None:
    assert FRED_RELEASES_HORIZON_DAYS == 30
    fred = FakeFred([_row(CPI, date(2026, 10, 14))])
    await fetch_release_events(fred, today=START)
    assert fred.calls == [(START, START + timedelta(days=30))]


@pytest.mark.asyncio
async def test_a_fetch_that_maps_nothing_is_a_skip_not_a_success() -> None:
    empty = await fetch_release_events(FakeFred([]), today=START)
    assert isinstance(empty, ReleasesNotFetched)
    untimed = await fetch_release_events(
        FakeFred([_row(441, date(2026, 10, 12), "Coinbase Cryptocurrencies")]), today=START
    )
    assert isinstance(untimed, ReleasesNotFetched)
    assert "441" in untimed.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("today", [datetime(2026, 10, 10, 12, 0, tzinfo=UTC), "2026-10-10"])
async def test_today_must_be_a_calendar_date(today: object) -> None:
    with pytest.raises(ValueError):
        await fetch_release_events(FakeFred([]), today=today)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_horizon_below_one_day_is_refused() -> None:
    with pytest.raises(ValueError):
        await fetch_release_events(FakeFred([]), today=START, horizon_days=0)


# --------------------------------------------------------------------------
# Unit 7.2c-1: a row the input refuses is skipped, not fatal
# --------------------------------------------------------------------------


def test_a_row_the_input_refuses_is_skipped_and_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # actual_for is None today; once it is filled in, a figure too wide for
    # its column must cost that one row, not the window.

    def too_wide(release_id: int, day: date) -> Decimal | None:
        return Decimal("1e50") if release_id == CPI else None

    monkeypatch.setattr(releases, "actual_for", too_wide)
    rows = [_row(CPI, date(2026, 10, 14)), _row(EMPLOYMENT, date(2026, 11, 6))]
    with caplog.at_level("WARNING", logger="corollary.data.calendar_releases"):
        result = release_events(rows, TIMES, start=START, end=END)
    assert [e.vendor_id for e in result.events] == ["50:2026-11-06"]
    [skipped] = result.skipped
    assert (skipped.release_id, skipped.date) == (CPI, date(2026, 10, 14))
    assert "40" in skipped.reason
    assert any(
        getattr(r, "event", None) == "fred_release_row_skipped" for r in caplog.records
    )


def test_a_clean_window_skips_nothing() -> None:
    assert recorded().skipped == ()
