"""``GET /api/calendar`` and the manual routes (Phase 3 step 7, unit 7.3).

What is pinned, from the spec's *API* section, decisions 7, 8, 9 and 20, Q3
and Q15:

* the range is validated server-side -- both bounds, ``from <= to``, and at
  most ``CALENDAR_MAX_SPAN_DAYS`` dates, permitted at the boundary and
  refused one past it;
* events group by ``date``, never by ``at``'s UTC day;
* Decimal figures are exact JSON strings; earnings carry a session, IPOs
  their five fields, economic rows ``consensus: "unavailable"``;
* every notice: central-bank seed gaps (no file, unpublished, partial,
  unreadable), each job's state including a premium ``CalendarAccessDenied``
  told apart from an outage, and the release figures' pending owner decision;
* manual rows round-trip; vendor and seed rows are refused with a 409, an
  unknown id with a 404; nothing reaches ``audit_log`` (decision 9);
* one ``calendar_changed`` notice per change, Discord only, none for a
  no-op or a refusal (decision 20);
* no route depends on a broker or a provider, and no error body carries
  vendor detail.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.api.deps import (
    broker_for_account,
    fundamentals_data,
    market_data,
    service_registry,
)
from corollary.api.routes.calendar import (
    CALENDAR_JOBS,
    CALENDAR_MAX_SPAN_DAYS,
    central_bank_seed_dir,
    request_now,
)
from corollary.api.routes.calendar import router as calendar_router
from corollary.data.calendar import create_manual, upsert_events
from corollary.data.calendar_event import (
    TITLE_MAX,
    CalendarEventInput,
    CalendarKind,
    CalendarSource,
    EarningsSession,
    IpoStatus,
)
from corollary.db.models import (
    AuditLog,
    CalendarEvent,
    NotificationDelivery,
    NotificationRecord,
)
from corollary.engine.runtime import DISCORD_WEBHOOK_ENV
from corollary.engine.scheduler import JobStatus, _calendar_jobs

#: The app-wide request-validation handler's logger (finding 3).
APP_LOGGER = "corollary.api.app"

UTC = timezone.utc
#: Wednesday 2026-10-14, 11:00 EDT.
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
FAKE_WEBHOOK = "https://discord.com/api/webhooks/1234/not-a-real-webhook-token"
COMMITTED_SEEDS: Path | None = None


@dataclass
class FakeScheduler:
    """Stands in for ``Scheduler``: the route reads ``status()`` and nothing else."""

    statuses: dict[str, JobStatus]

    def status(self) -> dict[str, JobStatus]:
        return dict(self.statuses)


def job(name: str, **fields: Any) -> JobStatus:
    base: dict[str, Any] = {
        "name": name,
        "rule": "test",
        "schedule": "daily",
        "next_run": None,
        "last_started": None,
        "last_success": None,
        "last_failure": None,
        "last_error_type": None,
        "runs": 0,
        "failures": 0,
    }
    base.update(fields)
    return JobStatus(**base)


@pytest.fixture
def calendar(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    # The lifespan's Discord sink reads this; the test app posts nothing and
    # records a dropped delivery, which is how a test sees Discord was routed.
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, FAKE_WEBHOOK)
    app.dependency_overrides[request_now] = lambda: NOW
    with TestClient(app) as client:
        yield client


def add_events(engine: Engine, *events: CalendarEventInput) -> None:
    with Session(engine) as session:
        upsert_events(session, list(events), now=NOW)
        session.commit()


def row_id(engine: Engine, vendor_id: str) -> int:
    with Session(engine) as session:
        return session.scalars(
            select(CalendarEvent.id).where(CalendarEvent.vendor_id == vendor_id)
        ).one()


def earnings(**overrides: Any) -> CalendarEventInput:
    fields: dict[str, Any] = {
        "kind": CalendarKind.EARNINGS,
        "source": CalendarSource.FINNHUB,
        "vendor_id": "NVDA:2026Q3",
        "title": "NVDA earnings",
        "ticker": "NVDA",
        "date": date(2026, 10, 15),
        "session": EarningsSession.AMC,
        "estimate": Decimal("1.2350"),
        "actual": Decimal("-0.10"),
        "unit": "USD/share",
    }
    fields.update(overrides)
    return CalendarEventInput(**fields)


def release(at: datetime, day: date, vendor_id: str = "10:2026-10-13") -> CalendarEventInput:
    return CalendarEventInput(
        kind=CalendarKind.ECONOMIC,
        source=CalendarSource.FRED,
        vendor_id=vendor_id,
        title="Consumer Price Index",
        date=day,
        at=at,
    )


def fomc() -> CalendarEventInput:
    return CalendarEventInput(
        kind=CalendarKind.CENTRAL_BANK,
        source=CalendarSource.SEED,
        vendor_id="FOMC:2026-10-28",
        title="FOMC decision",
        date=date(2026, 10, 28),
        at=datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
    )


def ipo() -> CalendarEventInput:
    return CalendarEventInput(
        kind=CalendarKind.IPO,
        source=CalendarSource.FINNHUB,
        vendor_id="IPO:ACME",
        title="Acme Corp IPO",
        ticker="ACME",
        date=date(2026, 10, 16),
        exchange="NASDAQ Global",
        shares=12_500_000,
        price_low=Decimal("17.00"),
        price_high=Decimal("19.50"),
        ipo_status=IpoStatus.EXPECTED,
    )


def read(client: TestClient, start: str, end: str) -> dict[str, Any]:
    response = client.get("/api/calendar", params={"from": start, "to": end})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def all_events(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for day in body["days"] for event in day["events"]]


def calendar_notices(engine: Engine) -> list[NotificationRecord]:
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(NotificationRecord)
                .where(NotificationRecord.event == "calendar_changed")
            )
        )
        for row in rows:
            session.expunge(row)
    return rows


def audit_count(engine: Engine) -> int:
    with Session(engine) as session:
        return len(list(session.scalars(select(AuditLog))))


# --------------------------------------------------------------------------
# Range validation
# --------------------------------------------------------------------------


def test_the_widest_permitted_range_is_served(calendar: TestClient) -> None:
    # 1 Oct .. 31 Dec is 31 + 30 + 31 = 92 dates, inclusive.
    assert CALENDAR_MAX_SPAN_DAYS == 92
    body = read(calendar, "2026-10-01", "2026-12-31")
    assert (body["start"], body["end"], body["maxSpanDays"]) == ("2026-10-01", "2026-12-31", 92)


def test_one_date_past_the_widest_range_is_refused(calendar: TestClient) -> None:
    response = calendar.get("/api/calendar", params={"from": "2026-10-01", "to": "2027-01-01"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_calendar_range"
    assert "93 dates" in response.json()["error"]["message"]


def test_a_single_day_is_a_range(calendar: TestClient) -> None:
    assert read(calendar, "2026-10-14", "2026-10-14")["total"] == 0


def test_from_after_to_is_refused_and_logged(
    calendar: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = calendar.get("/api/calendar", params={"from": "2026-10-15", "to": "2026-10-14"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_calendar_range"
    refusals = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "calendar_request_refused"
    ]
    assert len(refusals) == 1
    assert "from <= to" in getattr(refusals[0], "rule")
    assert getattr(refusals[0], "inputs") == {"from": "2026-10-15", "to": "2026-10-14"}
    assert getattr(refusals[0], "at") == NOW.isoformat()


@pytest.mark.parametrize(
    "params",
    [
        {"to": "2026-10-14"},
        {"from": "2026-10-14"},
        {"from": "2026-02-30", "to": "2026-03-01"},
        {"from": "20261014", "to": "2026-10-15"},
        {"from": "2026-10-14T00:00", "to": "2026-10-15"},
        {"from": "1760400000", "to": "2026-10-15"},
        {"from": "2026-10-14", "to": "２０２６-10-15"},
    ],
)
def test_a_malformed_or_missing_bound_is_a_422(
    calendar: TestClient, params: dict[str, str]
) -> None:
    response = calendar.get("/api/calendar", params=params)
    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}


# --------------------------------------------------------------------------
# Rows: grouping, exact figures, session and IPO fields
# --------------------------------------------------------------------------


def test_events_group_by_their_eastern_date_not_the_utc_day(
    calendar: TestClient, db_engine: Engine
) -> None:
    # 20:30 EDT on 13 Oct is 00:30Z on the 14th: it belongs to the 13th.
    add_events(db_engine, release(datetime(2026, 10, 14, 0, 30, tzinfo=UTC), date(2026, 10, 13)))

    body = read(calendar, "2026-10-13", "2026-10-14")

    assert [day["date"] for day in body["days"]] == ["2026-10-13"]
    (event,) = body["days"][0]["events"]
    assert (event["date"], event["at"]) == ("2026-10-13", "2026-10-14T00:30:00Z")

    # And a read of the 14th alone does not find it.
    assert read(calendar, "2026-10-14", "2026-10-14")["days"] == []


def test_days_ascend_and_all_day_rows_lead_each_day(
    calendar: TestClient, db_engine: Engine
) -> None:
    add_events(
        db_engine,
        earnings(),
        release(datetime(2026, 10, 15, 12, 30, tzinfo=UTC), date(2026, 10, 15), "10:2026-10-15"),
        fomc(),
    )

    body = read(calendar, "2026-10-14", "2026-10-31")

    assert [day["date"] for day in body["days"]] == ["2026-10-15", "2026-10-28"]
    assert [event["type"] for event in body["days"][0]["events"]] == ["earnings", "economic"]
    assert body["total"] == 3


def test_decimal_figures_are_exact_strings_on_the_wire(
    calendar: TestClient, db_engine: Engine
) -> None:
    add_events(db_engine, earnings(), ipo())

    response = calendar.get("/api/calendar", params={"from": "2026-10-14", "to": "2026-10-20"})

    assert '"estimate":"1.2350"' in response.text
    assert '"actual":"-0.10"' in response.text
    assert '"priceLow":"17.00"' in response.text
    assert '"priceHigh":"19.50"' in response.text


def test_an_earnings_row_carries_its_session_and_no_time(
    calendar: TestClient, db_engine: Engine
) -> None:
    add_events(db_engine, earnings())

    (event,) = all_events(read(calendar, "2026-10-15", "2026-10-15"))

    assert event == {
        "id": str(row_id(db_engine, "NVDA:2026Q3")),
        "date": "2026-10-15",
        "at": None,
        "type": "earnings",
        "title": "NVDA earnings",
        "ticker": "NVDA",
        "source": "finnhub",
        "editable": False,
        "session": "amc",
        "estimate": "1.2350",
        "prior": None,
        "actual": "-0.10",
        "consensus": None,
        "unit": "USD/share",
        "exchange": None,
        "shares": None,
        "priceLow": None,
        "priceHigh": None,
        "ipoStatus": None,
    }


def test_an_ipo_row_carries_its_five_fields(calendar: TestClient, db_engine: Engine) -> None:
    add_events(db_engine, ipo())

    (event,) = all_events(read(calendar, "2026-10-16", "2026-10-16"))

    assert event["type"] == "ipo"
    assert {key: event[key] for key in ("exchange", "shares", "priceLow", "priceHigh", "ipoStatus")} == {
        "exchange": "NASDAQ Global",
        "shares": 12_500_000,
        "priceLow": "17.00",
        "priceHigh": "19.50",
        "ipoStatus": "expected",
    }
    assert event["session"] is None


def test_an_economic_row_states_consensus_unavailable(
    calendar: TestClient, db_engine: Engine
) -> None:
    add_events(db_engine, release(datetime(2026, 10, 15, 12, 30, tzinfo=UTC), date(2026, 10, 15)))

    (event,) = all_events(read(calendar, "2026-10-15", "2026-10-15"))

    assert event["consensus"] == "unavailable"
    assert (event["estimate"], event["prior"], event["actual"]) == (None, None, None)
    assert event["at"] == "2026-10-15T12:30:00Z"


def test_a_central_bank_row_carries_its_time(calendar: TestClient, db_engine: Engine) -> None:
    add_events(db_engine, fomc())

    (event,) = all_events(read(calendar, "2026-10-28", "2026-10-28"))

    assert (event["type"], event["source"], event["at"]) == (
        "central-bank",
        "seed",
        "2026-10-28T18:00:00Z",
    )


# --------------------------------------------------------------------------
# Notices
# --------------------------------------------------------------------------


def test_the_release_figures_are_stated_as_pending_an_owner_decision(
    calendar: TestClient,
) -> None:
    notice = read(calendar, "2026-10-14", "2026-10-14")["notices"]["releaseFigures"]

    assert notice["state"] == "pending_owner_decision"
    assert "owner decision" in notice["reason"]


def test_a_partial_central_bank_seed_is_stated_with_its_first_date(
    calendar: TestClient,
) -> None:
    # The committed 2026 seed covers the ECB only from 2026-10-10; 2027 is full.
    gaps = read(calendar, "2026-12-20", "2027-01-10")["notices"]["seedGaps"]

    assert [(gap["bank"], gap["year"], gap["kind"], gap["coversFrom"]) for gap in gaps] == [
        ("ECB", 2026, "partial", "2026-10-10")
    ]


def test_a_year_with_no_seed_file_names_every_bank(calendar: TestClient) -> None:
    gaps = read(calendar, "2028-01-01", "2028-01-31")["notices"]["seedGaps"]

    assert [(gap["bank"], gap["kind"]) for gap in gaps] == [
        ("FOMC", "no_file"),
        ("ECB", "no_file"),
        ("BOE", "no_file"),
        ("BOJ", "no_file"),
    ]
    assert all("no central-bank seed for 2028" in gap["reason"] for gap in gaps)


def _seed_dir(tmp_path: Path, text: str) -> Path:
    directory = tmp_path / "seeds"
    directory.mkdir()
    (directory / "central_banks_2027.csv").write_text(text, encoding="utf-8", newline="\n")
    return directory


def _committed_2027() -> str:
    path = Path(__file__).resolve().parents[2] / "corollary" / "data" / "seeds" / "central_banks_2027.csv"
    return path.read_text(encoding="utf-8")


def test_an_unpublished_bank_is_stated(
    app: FastAPI, calendar: TestClient, tmp_path: Path
) -> None:
    lines = _committed_2027().splitlines()
    unpublished = [
        line.replace("BOJ,full,", "BOJ,unpublished,", 1) if line.startswith("BOJ,full,") else line
        for line in lines
        if not (line.startswith("BOJ,") and not line.startswith("BOJ,full,"))
    ]
    directory = _seed_dir(tmp_path, "\n".join(unpublished) + "\n")
    app.dependency_overrides[central_bank_seed_dir] = lambda: directory

    gaps = read(calendar, "2027-01-01", "2027-01-31")["notices"]["seedGaps"]

    assert [(gap["bank"], gap["year"], gap["kind"]) for gap in gaps] == [
        ("BOJ", 2027, "unpublished")
    ]


def test_an_unreadable_seed_is_stated_and_does_not_hide_the_other_year(
    app: FastAPI, calendar: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = _seed_dir(tmp_path, "year,2027\nthis is not a seed\n")
    app.dependency_overrides[central_bank_seed_dir] = lambda: directory

    gaps = read(calendar, "2026-12-20", "2027-01-10")["notices"]["seedGaps"]

    # 2026 has no file in this directory: four no_file gaps, then 2027's
    # malformed file as one ``unreadable`` notice naming no bank.
    assert [(gap["bank"], gap["year"], gap["kind"]) for gap in gaps] == [
        ("FOMC", 2026, "no_file"),
        ("ECB", 2026, "no_file"),
        ("BOE", 2026, "no_file"),
        ("BOJ", 2026, "no_file"),
        (None, 2027, "unreadable"),
    ]
    assert any(
        getattr(record, "event", None) == "calendar_seed_unreadable" for record in caplog.records
    )


def test_with_no_scheduler_every_job_says_so(calendar: TestClient) -> None:
    jobs = read(calendar, "2026-10-14", "2026-10-14")["notices"]["jobs"]

    assert [notice["job"] for notice in jobs] == list(CALENDAR_JOBS)
    assert {notice["state"] for notice in jobs} == {"scheduler_not_running"}
    assert all("no context scheduler" in notice["message"] for notice in jobs)


def test_the_job_names_are_the_real_schedulers() -> None:
    """A renamed job would otherwise read as ``not_scheduled`` forever."""
    none = cast(Any, None)
    assert [job.name for job in _calendar_jobs(none, none, none)] == list(CALENDAR_JOBS)


def test_each_job_state_is_served_with_its_reason(
    app: FastAPI, calendar: TestClient, db_engine: Engine
) -> None:
    add_events(db_engine, earnings())
    seven = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)  # 07:00 EDT
    app.state.scheduler = FakeScheduler(
        {
            "calendar_earnings": job("calendar_earnings", last_success=seven, runs=1),
            "calendar_ipo": job(
                "calendar_ipo",
                last_success=seven - timedelta(days=1),
                last_failure=seven + timedelta(minutes=5),
                last_error_type="CalendarAccessDenied",
                failures=1,
            ),
            "calendar_dividends": job(
                "calendar_dividends",
                skips=1,
                last_skipped=seven + timedelta(minutes=10),
                last_skip_reason="Alpaca market data is unavailable",
            ),
            "calendar_releases": job(
                "calendar_releases",
                last_success=seven - timedelta(days=1),
                last_failure=seven,
                last_error_type="TimeoutError",
                failures=1,
            ),
            # calendar_central_banks absent: a scheduler without the job.
        }
    )

    jobs = {
        notice["job"]: notice
        for notice in read(calendar, "2026-10-14", "2026-10-20")["notices"]["jobs"]
    }

    earnings_job = jobs["calendar_earnings"]
    assert (earnings_job["state"], earnings_job["rowsInRange"]) == ("ok", 1)
    assert earnings_job["lastSuccess"] == "2026-10-14T11:00:00Z"
    assert "07:00 EDT" in earnings_job["message"]

    denied = jobs["calendar_ipo"]
    assert (denied["state"], denied["accessDenied"]) == ("access_denied", True)
    assert "Q15" in denied["message"] and "Stale since 2026-10-13 07:00 EDT" in denied["message"]

    skipped = jobs["calendar_dividends"]
    assert (skipped["state"], skipped["accessDenied"]) == ("skipped", False)
    assert skipped["lastSkipReason"] == "Alpaca market data is unavailable"
    assert "Alpaca market data is unavailable" in skipped["message"]
    assert skipped["rowsInRange"] == 0

    failing = jobs["calendar_releases"]
    assert (failing["state"], failing["accessDenied"]) == ("failing", False)
    assert failing["lastErrorType"] == "TimeoutError"
    assert "stale since 2026-10-13 07:00 EDT" in failing["message"]

    assert jobs["calendar_central_banks"]["state"] == "not_scheduled"


def test_a_job_that_has_not_run_and_one_with_nothing_in_range(
    app: FastAPI, calendar: TestClient
) -> None:
    app.state.scheduler = FakeScheduler(
        {
            "calendar_dividends": job(
                "calendar_dividends",
                last_success=NOW - timedelta(hours=4),
                last_success_started=NOW - timedelta(hours=4),
                runs=1,
            ),
            "calendar_central_banks": job(
                "calendar_central_banks", next_run=datetime(2026, 10, 15, 10, 45, tzinfo=UTC)
            ),
        }
    )

    jobs = {
        notice["job"]: notice
        for notice in read(calendar, "2026-10-14", "2026-10-20")["notices"]["jobs"]
    }

    # "Dividends or a stated reason they are absent": fetched, none listed.
    assert jobs["calendar_dividends"]["state"] == "ok"
    assert "none in this range" in jobs["calendar_dividends"]["message"]
    assert jobs["calendar_central_banks"]["state"] == "never_run"
    assert "2026-10-15 06:45 EDT" in jobs["calendar_central_banks"]["message"]


def test_a_skip_after_a_failure_reads_as_the_skip(app: FastAPI, calendar: TestClient) -> None:
    app.state.scheduler = FakeScheduler(
        {
            "calendar_earnings": job(
                "calendar_earnings",
                last_failure=NOW - timedelta(hours=2),
                last_error_type="TimeoutError",
                last_skipped=NOW - timedelta(hours=1),
                last_skip_reason="Finnhub is unavailable",
            )
        }
    )

    (notice,) = [
        notice
        for notice in read(calendar, "2026-10-14", "2026-10-14")["notices"]["jobs"]
        if notice["job"] == "calendar_earnings"
    ]

    assert notice["state"] == "skipped"
    assert "never fetched successfully" in notice["message"].lower()


# --------------------------------------------------------------------------
# Coverage: "none in this range" only for dates the job actually fetched
# --------------------------------------------------------------------------

#: 07:00 EDT on Wednesday 14 Oct: the earnings slot, the day of ``NOW``.
SEVEN_EDT = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)


def _notice(client: TestClient, job_name: str, start: str, end: str) -> dict[str, Any]:
    (notice,) = [
        notice
        for notice in read(client, start, end)["notices"]["jobs"]
        if notice["job"] == job_name
    ]
    return notice


def _succeeded(app: FastAPI, job_name: str, at: datetime = SEVEN_EDT) -> None:
    app.state.scheduler = FakeScheduler(
        {
            job_name: job(
                job_name,
                last_started=at,
                last_success=at,
                last_success_started=at,
                runs=1,
            )
        }
    )


def test_a_fully_covered_empty_range_says_none(app: FastAPI, calendar: TestClient) -> None:
    _succeeded(app, "calendar_earnings")

    notice = _notice(calendar, "calendar_earnings", "2026-10-14", "2026-10-20")

    assert notice["state"] == "ok"
    assert notice["coveredThrough"] == "2026-11-04"  # 14 Oct + 21 days
    assert "none in this range" in notice["message"]
    assert "not covered" not in notice["message"].lower()


def test_a_range_wholly_past_the_horizon_never_says_none(
    app: FastAPI, calendar: TestClient
) -> None:
    """The auditor's case: Finnhub was never asked about these dates."""
    _succeeded(app, "calendar_earnings")

    notice = _notice(calendar, "calendar_earnings", "2026-11-15", "2026-12-15")

    assert notice["state"] == "ok"
    assert notice["coveredThrough"] == "2026-11-04"
    assert "none" not in notice["message"]
    assert (
        "Not covered: the job looks 21 days ahead, so dates after 2026-11-04 have "
        "not been fetched" in notice["message"]
    )


def test_a_straddling_range_says_both(app: FastAPI, calendar: TestClient) -> None:
    _succeeded(app, "calendar_earnings")

    notice = _notice(calendar, "calendar_earnings", "2026-10-20", "2026-11-20")

    assert "none from 2026-10-20 through 2026-11-04" in notice["message"]
    assert "none in this range" not in notice["message"]
    assert "dates after 2026-11-04 have not been fetched" in notice["message"]


def test_a_straddling_range_with_rows_in_its_covered_part_says_no_none(
    app: FastAPI, calendar: TestClient, db_engine: Engine
) -> None:
    add_events(db_engine, earnings())  # 15 Oct
    _succeeded(app, "calendar_earnings")

    notice = _notice(calendar, "calendar_earnings", "2026-10-15", "2026-11-20")

    assert notice["rowsInRange"] == 1
    assert "none" not in notice["message"]
    assert "dates after 2026-11-04 have not been fetched" in notice["message"]


def test_a_range_before_the_last_fetch_is_not_claimed_by_it(
    app: FastAPI, calendar: TestClient
) -> None:
    """The last fetch began on the 14th; it says nothing about the 1st."""
    _succeeded(app, "calendar_earnings")

    notice = _notice(calendar, "calendar_earnings", "2026-10-01", "2026-10-13")

    assert "none" not in notice["message"]
    assert "dates before 2026-10-14" in notice["message"]


def test_a_job_that_never_ran_has_no_coverage_and_says_no_none(
    app: FastAPI, calendar: TestClient
) -> None:
    app.state.scheduler = FakeScheduler({"calendar_earnings": job("calendar_earnings")})

    notice = _notice(calendar, "calendar_earnings", "2026-10-14", "2026-10-20")

    assert notice["state"] == "never_run"
    assert notice["coveredThrough"] is None
    assert "none" not in notice["message"]


def test_with_no_scheduler_no_job_claims_coverage(calendar: TestClient) -> None:
    jobs = read(calendar, "2026-10-14", "2026-10-20")["notices"]["jobs"]
    assert {notice["coveredThrough"] for notice in jobs} == {None}


@pytest.mark.parametrize(
    ("job_name", "days", "through"),
    [
        ("calendar_earnings", 21, "2026-11-04"),
        ("calendar_ipo", 30, "2026-11-13"),
        ("calendar_dividends", 90, "2027-01-12"),
        ("calendar_releases", 30, "2026-11-13"),
    ],
)
def test_each_jobs_horizon_is_the_one_its_fetcher_uses(
    app: FastAPI, calendar: TestClient, job_name: str, days: int, through: str
) -> None:
    _succeeded(app, job_name)
    day_after = (date.fromisoformat(through) + timedelta(days=1)).isoformat()

    covered = _notice(calendar, job_name, through, through)
    beyond = _notice(calendar, job_name, day_after, day_after)

    assert covered["coveredThrough"] == beyond["coveredThrough"] == through
    assert "none in this range" in covered["message"]
    assert f"looks {days} days ahead" in beyond["message"]
    assert "none" not in beyond["message"]


def test_the_central_bank_job_covers_this_year_and_next(
    app: FastAPI, calendar: TestClient
) -> None:
    _succeeded(app, "calendar_central_banks")

    inside = _notice(calendar, "calendar_central_banks", "2027-12-01", "2027-12-31")
    beyond = _notice(calendar, "calendar_central_banks", "2027-12-15", "2028-01-15")

    assert inside["coveredThrough"] == "2027-12-31"
    assert "none in this range" in inside["message"]
    assert "dates after 2027-12-31 have not been fetched" in beyond["message"]


def test_coverage_is_dated_from_the_runs_start_not_its_finish(
    app: FastAPI, calendar: TestClient
) -> None:
    """A run that began at 23:59 ET and finished after midnight fetched from the 13th."""
    started = datetime(2026, 10, 14, 3, 59, tzinfo=UTC)  # 23:59 EDT on the 13th
    finished = started + timedelta(minutes=2)  # 00:01 EDT on the 14th
    app.state.scheduler = FakeScheduler(
        {
            "calendar_earnings": job(
                "calendar_earnings",
                last_started=started,
                last_success=finished,
                last_success_started=started,
                runs=1,
            )
        }
    )

    notice = _notice(calendar, "calendar_earnings", "2026-10-14", "2026-10-14")

    assert notice["coveredThrough"] == "2026-11-03"


@pytest.mark.parametrize("slot_run", ["in_flight", "failed"])
def test_a_later_runs_start_does_not_redate_a_success_that_straddled_midnight(
    app: FastAPI, calendar: TestClient, slot_run: str
) -> None:
    """Audit finding 2. A catch-up began 23:59 ET on the 13th and finished at
    00:00:20 on the 14th; the 07:00 slot run on the 14th has started since.

    ``last_started`` now belongs to the slot run, so the success has to be
    dated from its *own* recorded start. Dated from its finish, the fetch
    reads as the 14th's, and 4 Nov reads "none" though it was never asked
    about -- for as long as the slot run is in flight, and for good if it
    fails.
    """
    started = datetime(2026, 10, 14, 3, 59, tzinfo=UTC)  # 23:59 EDT on the 13th
    finished = datetime(2026, 10, 14, 4, 0, 20, tzinfo=UTC)  # 00:00:20 EDT on the 14th
    fields: dict[str, Any] = {
        "last_started": SEVEN_EDT,
        "last_success": finished,
        "last_success_started": started,
        "runs": 1,
    }
    if slot_run == "failed":
        fields.update(
            last_failure=SEVEN_EDT + timedelta(seconds=30),
            last_error_type="TimeoutError",
            failures=1,
        )
    app.state.scheduler = FakeScheduler(
        {"calendar_earnings": job("calendar_earnings", **fields)}
    )

    notice = _notice(calendar, "calendar_earnings", "2026-11-04", "2026-11-04")

    assert notice["coveredThrough"] == "2026-11-03"  # 13 Oct + 21 days
    assert "none" not in notice["message"]
    assert "dates after 2026-11-03 have not been fetched" in notice["message"]


def test_a_success_with_no_recorded_start_claims_no_coverage(
    app: FastAPI, calendar: TestClient
) -> None:
    """The scheduler always records both; a status without the start is not
    dated by guessing -- the finish could be a day later than the fetch."""
    app.state.scheduler = FakeScheduler(
        {"calendar_earnings": job("calendar_earnings", last_success=SEVEN_EDT, runs=1)}
    )

    notice = _notice(calendar, "calendar_earnings", "2026-10-14", "2026-10-14")

    assert notice["coveredThrough"] is None
    assert "none in this range" not in notice["message"]


def test_a_failing_job_still_states_what_its_last_success_did_not_cover(
    app: FastAPI, calendar: TestClient
) -> None:
    app.state.scheduler = FakeScheduler(
        {
            "calendar_earnings": job(
                "calendar_earnings",
                last_success=SEVEN_EDT - timedelta(days=1),
                last_success_started=SEVEN_EDT - timedelta(days=1),
                last_failure=SEVEN_EDT,
                last_error_type="TimeoutError",
                failures=1,
            )
        }
    )

    notice = _notice(calendar, "calendar_earnings", "2026-10-20", "2026-11-20")

    assert notice["state"] == "failing"
    assert notice["coveredThrough"] == "2026-11-03"
    assert "dates after 2026-11-03 have not been fetched" in notice["message"]


# --------------------------------------------------------------------------
# A start-up catch-up that found the rows fresh is calm, and Eastern
# --------------------------------------------------------------------------


def test_a_fresh_catch_up_reads_up_to_date_in_eastern_time(
    app: FastAPI, calendar: TestClient
) -> None:
    newest = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)  # 06:00 EDT
    raw_reason = (
        "the newest stored finnhub earnings row was written at "
        f"{newest.isoformat()}, less than 1 day ago; the start-up catch-up does "
        "not fetch again, and the daily slot will"
    )
    app.state.scheduler = FakeScheduler(
        {
            "calendar_earnings": job(
                "calendar_earnings",
                last_started=NOW - timedelta(hours=1),
                skips=1,
                last_skipped=NOW - timedelta(hours=1),
                last_skip_reason=raw_reason,
                last_skip_fresh_as_of=newest,
                next_run=datetime(2026, 10, 15, 11, 0, tzinfo=UTC),
            )
        }
    )

    notice = _notice(calendar, "calendar_earnings", "2026-10-14", "2026-10-20")

    assert notice["state"] == "fresh_at_start"
    assert notice["freshAsOf"] == "2026-10-14T10:00:00Z"
    assert notice["lastSkipReason"] is None
    assert notice["coveredThrough"] == "2026-11-04"
    message = notice["message"]
    assert "up to date as of 2026-10-14 06:00 EDT" in message
    assert "next fetch 2026-10-15 07:00 EDT" in message
    assert "none in this range" in message
    for alarming_or_utc in ("+00:00", "T10:00", "0:00:00", "nothing fetched", "Stale"):
        assert alarming_or_utc not in message


def test_a_real_skip_is_still_a_skip(app: FastAPI, calendar: TestClient) -> None:
    app.state.scheduler = FakeScheduler(
        {
            "calendar_earnings": job(
                "calendar_earnings",
                skips=1,
                last_skipped=NOW - timedelta(hours=1),
                last_skip_reason="Finnhub is unavailable",
            )
        }
    )

    notice = _notice(calendar, "calendar_earnings", "2026-10-14", "2026-10-20")

    assert notice["state"] == "skipped"
    assert notice["freshAsOf"] is None
    assert notice["lastSkipReason"] == "Finnhub is unavailable"


# --------------------------------------------------------------------------
# Manual rows
# --------------------------------------------------------------------------


def test_a_manual_entry_round_trips(calendar: TestClient, db_engine: Engine) -> None:
    created = calendar.post(
        "/api/calendar/manual",
        json={"title": "  G20 summit  ", "date": "2026-10-16"},
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert {key: item[key] for key in ("date", "at", "type", "title", "source", "editable")} == {
        "date": "2026-10-16",
        "at": None,
        "type": "geopolitical",
        "title": "G20 summit",
        "source": "manual",
        "editable": True,
    }
    event_id = item["id"]
    assert all_events(read(calendar, "2026-10-16", "2026-10-16")) == [item]

    edited = calendar.put(
        f"/api/calendar/manual/{event_id}",
        json={"title": "G20 summit, day 2", "date": "2026-10-17", "at": "2026-10-17T09:00:00-04:00"},
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["id"], edited.json()["at"]) == (event_id, "2026-10-17T13:00:00Z")
    assert all_events(read(calendar, "2026-10-16", "2026-10-16")) == []
    assert all_events(read(calendar, "2026-10-17", "2026-10-17")) == [edited.json()]

    removed = calendar.delete(f"/api/calendar/manual/{event_id}")
    assert removed.status_code == 204
    assert removed.content == b""
    assert all_events(read(calendar, "2026-10-14", "2026-10-31")) == []

    # A soft delete: the row is kept, marked removed.
    with Session(db_engine) as session:
        row = session.get(CalendarEvent, int(event_id))
        assert row is not None and row.deleted_at is not None

    assert calendar.delete(f"/api/calendar/manual/{event_id}").status_code == 404
    assert (
        calendar.put(
            f"/api/calendar/manual/{event_id}", json={"title": "x", "date": "2026-10-17"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("method", ["put", "delete"])
@pytest.mark.parametrize("vendor_id", ["NVDA:2026Q3", "FOMC:2026-10-28"])
def test_a_vendor_or_seed_row_is_not_editable(
    calendar: TestClient, db_engine: Engine, method: str, vendor_id: str
) -> None:
    add_events(db_engine, earnings(), fomc())
    event_id = row_id(db_engine, vendor_id)
    before = all_events(read(calendar, "2026-10-14", "2026-10-31"))

    if method == "put":
        response = calendar.put(
            f"/api/calendar/manual/{event_id}", json={"title": "hijack", "date": "2026-10-15"}
        )
    else:
        response = calendar.delete(f"/api/calendar/manual/{event_id}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "calendar_event_not_editable"
    assert all_events(read(calendar, "2026-10-14", "2026-10-31")) == before
    assert calendar_notices(db_engine) == []


def test_an_unknown_id_is_a_404(calendar: TestClient) -> None:
    for response in (
        calendar.put("/api/calendar/manual/999", json={"title": "x", "date": "2026-10-15"}),
        calendar.delete("/api/calendar/manual/999"),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "calendar_event_not_found"


@pytest.mark.parametrize("raw_id", ["0", "-1", "abc", str(2**63)])
def test_an_impossible_id_is_a_422(calendar: TestClient, raw_id: str) -> None:
    assert calendar.delete(f"/api/calendar/manual/{raw_id}").status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"title": "   ", "date": "2026-10-16"},
        {"title": "x" * (TITLE_MAX + 1), "date": "2026-10-16"},
        {"title": "line one\nline two", "date": "2026-10-16"},
        {"title": "bell\x07", "date": "2026-10-16"},
        {"date": "2026-10-16"},
        {"title": "summit"},
        {"title": "summit", "date": 1760400000},
        {"title": "summit", "date": "2026-10-16T00:00:00"},
        {"title": "summit", "date": "16/10/2026"},
        {"title": "summit", "date": "2026-10-16", "at": "2026-10-16T09:00:00"},
        {"title": "summit", "date": "2026-10-16", "at": 1760620000},
        {"title": "summit", "date": "2026-10-16", "at": "1792155600"},  # 13:00Z on the 16th
        {"title": "summit", "date": "2026-10-16", "at": "1792155600.5"},
        {"title": "summit", "date": "2026-10-16", "at": "2026-10-16"},
        {"title": "summit", "date": "2026-10-16", "at": "2026-10-16T09Z"},
        {"title": "summit", "date": "2026-10-16", "at": "16/10/2026 09:00+00:00"},
        # 00:30Z on the 17th is 20:30 EDT on the 16th: not the 17th.
        {"title": "summit", "date": "2026-10-17", "at": "2026-10-17T00:30:00Z"},
        {"title": "summit", "date": "0001-01-01"},
        {"title": "summit", "date": "2026-10-16", "source": "seed"},
        {"title": "summit", "date": "2026-10-16", "kind": "earnings"},
    ],
)
def test_every_manual_field_is_validated_server_side(
    calendar: TestClient, db_engine: Engine, body: dict[str, Any]
) -> None:
    response = calendar.post("/api/calendar/manual", json=body)

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}
    with Session(db_engine) as session:
        assert list(session.scalars(select(CalendarEvent))) == []
    assert calendar_notices(db_engine) == []


@pytest.mark.parametrize(
    "at", ["2026-10-16T09:00:00-04:00", "2026-10-16T13:00Z", "2026-10-16T13:00:00.250Z"]
)
def test_an_iso_instant_with_an_offset_is_accepted(calendar: TestClient, at: str) -> None:
    response = calendar.post(
        "/api/calendar/manual", json={"title": "Summit", "date": "2026-10-16", "at": at}
    )
    assert response.status_code == 201, response.text


def test_a_title_at_the_cap_is_accepted(calendar: TestClient) -> None:
    response = calendar.post(
        "/api/calendar/manual", json={"title": "x" * TITLE_MAX, "date": "2026-10-16"}
    )
    assert response.status_code == 201


def test_a_late_evening_entry_is_filed_under_its_eastern_date(calendar: TestClient) -> None:
    response = calendar.post(
        "/api/calendar/manual",
        json={"title": "Address", "date": "2026-10-16", "at": "2026-10-17T00:30:00Z"},
    )
    assert response.status_code == 201, response.text
    assert [day["date"] for day in read(calendar, "2026-10-16", "2026-10-17")["days"]] == [
        "2026-10-16"
    ]


def test_the_manual_routes_write_no_audit_row(calendar: TestClient, db_engine: Engine) -> None:
    """Decision 9: a note that a summit is on Thursday governs nothing."""
    event_id = calendar.post(
        "/api/calendar/manual", json={"title": "Summit", "date": "2026-10-16"}
    ).json()["id"]
    calendar.put(f"/api/calendar/manual/{event_id}", json={"title": "Summit 2", "date": "2026-10-16"})
    calendar.delete(f"/api/calendar/manual/{event_id}")

    assert audit_count(db_engine) == 0


def test_each_change_notifies_once_on_discord_only(
    calendar: TestClient, db_engine: Engine
) -> None:
    event_id = calendar.post(
        "/api/calendar/manual", json={"title": "Summit", "date": "2026-10-16"}
    ).json()["id"]
    calendar.put(
        f"/api/calendar/manual/{event_id}",
        json={"title": "Summit", "date": "2026-10-16", "at": "2026-10-16T14:00:00Z"},
    )
    calendar.delete(f"/api/calendar/manual/{event_id}")

    # One clock for all three, so keyed by title rather than ordered.
    notices = calendar_notices(db_engine)
    by_title = {notice.title: notice for notice in notices}
    assert len(notices) == len(by_title) == 3
    assert {(notice.severity, notice.account) for notice in notices} == {("info", None)}
    assert by_title["Calendar entry added"].body == "Summit -- 2026-10-16 (all day)"
    assert by_title["Calendar entry edited"].body == (
        "Summit -- 2026-10-16 (all day) → Summit -- 2026-10-16 10:00 EDT"
    )
    assert by_title["Calendar entry removed"].body == "Summit -- 2026-10-16 10:00 EDT"
    with Session(db_engine) as session:
        channels = {
            (delivery.notification_id, delivery.channel)
            for delivery in session.scalars(select(NotificationDelivery))
        }
    assert channels == {(notice.id, "discord") for notice in notices}


def test_an_edit_that_changes_nothing_notifies_nothing(
    calendar: TestClient, db_engine: Engine
) -> None:
    body = {"title": "Summit", "date": "2026-10-16", "at": "2026-10-16T10:00:00-04:00"}
    event_id = calendar.post("/api/calendar/manual", json=body).json()["id"]

    # The same instant written in another zone is the same value.
    same = {"title": "Summit", "date": "2026-10-16", "at": "2026-10-16T14:00:00Z"}
    assert calendar.put(f"/api/calendar/manual/{event_id}", json=same).status_code == 200

    assert [notice.title for notice in calendar_notices(db_engine)] == ["Calendar entry added"]


# --------------------------------------------------------------------------
# Every 422 is logged: the rule, the field locations and types, the time --
# never a value (finding 3)
# --------------------------------------------------------------------------

SECRET_TITLE = "Summit at the Hotel Zebra -- do not leak"

_ROUTES = {"/api/calendar", "/api/calendar/manual", "/api/calendar/manual/{event_id}"}


def _validation_refusals(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "api_request_refused"
    ]


@pytest.mark.parametrize(
    ("method", "url", "body", "expected", "never", "undeclared"),
    [
        (
            "POST",
            "/api/calendar/manual",
            {"title": "   ", "date": "2026-10-16"},
            [("body.title", "value_error")],
            [],
            0,
        ),
        (
            "POST",
            "/api/calendar/manual",
            {"title": "bell\x07ringer", "date": "2026-10-16"},
            [("body.title", "value_error")],
            ["ringer"],
            0,
        ),
        (
            "POST",
            "/api/calendar/manual",
            {"title": SECRET_TITLE, "date": "2026-10-16", "source": "seed", "kind": "earnings"},
            [],
            [SECRET_TITLE, "'seed'", "'earnings'", "body.source", "body.kind"],
            2,
        ),
        (
            "POST",
            "/api/calendar/manual",
            {"title": SECRET_TITLE, "date": "16/10/2026"},
            [("body.date", "value_error")],
            [SECRET_TITLE, "16/10/2026"],
            0,
        ),
        (
            "POST",
            "/api/calendar/manual",
            {"title": SECRET_TITLE, "date": "2026-10-16", "at": "2026-10-16T09:00:00"},
            [("body.at", "value_error")],
            [SECRET_TITLE, "2026-10-16T09:00:00"],
            0,
        ),
        (
            "PUT",
            "/api/calendar/manual/0",
            {"title": SECRET_TITLE, "date": "2026-10-16"},
            [("path.event_id", "greater_than_equal")],
            [SECRET_TITLE],
            0,
        ),
        (
            "DELETE",
            f"/api/calendar/manual/{2**63}",
            None,
            [("path.event_id", "less_than_equal")],
            [str(2**63)],
            0,
        ),
        ("GET", "/api/calendar?from=2026-10-14", None, [("query.to", "missing")], [], 0),
        ("GET", "/api/calendar?to=2026-10-14", None, [("query.from", "missing")], [], 0),
    ],
)
def test_every_request_validation_refusal_is_logged_without_its_values(
    calendar: TestClient,
    caplog: pytest.LogCaptureFixture,
    method: str,
    url: str,
    body: dict[str, Any] | None,
    expected: list[tuple[str, str]],
    never: list[str],
    undeclared: int,
) -> None:
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        response = calendar.request(method, url, json=body)

    assert response.status_code == 422, response.text
    (refusal,) = _validation_refusals(caplog)
    assert refusal.levelno == logging.WARNING
    assert "server-side" in refusal.rule
    assert refusal.method == method
    assert refusal.route in _ROUTES
    assert [(error["loc"], error["type"]) for error in refusal.errors] == expected
    assert refusal.extra_forbidden == undeclared
    assert datetime.fromisoformat(refusal.at).tzinfo is not None
    assert refusal.correlation_id
    title = body.get("title") if isinstance(body, dict) else None
    assert refusal.title_length == (len(title) if isinstance(title, str) else None)
    logged = repr(refusal.__dict__)
    for value in never:
        assert value not in logged, value


#: The auditor's probe: a client-chosen key *name* shaped like a credential.
#: Assembled at runtime so this file's text does not itself trip the
#: repository-wide key-id scan in ``tests/fixtures/test_record_alpaca.py``.
PROBE_KEY = "PK" + "SECRETKEYVALUE1234567890"


def _logged_text(caplog: pytest.LogCaptureFixture) -> str:
    """Everything the app logger wrote: messages and every structured field."""
    return "\n".join(
        f"{record.getMessage()} {record.__dict__!r}"
        for record in caplog.records
        if record.name == APP_LOGGER
    )


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "/api/calendar/manual"),
        ("PUT", "/api/calendar/manual/1"),
    ],
)
def test_an_undeclared_key_name_never_reaches_the_log(
    calendar: TestClient, caplog: pytest.LogCaptureFixture, method: str, url: str
) -> None:
    """Audit finding 1 (rule 6). ``extra="forbid"`` puts an unknown key into
    ``loc`` exactly as sent; the log counts it and never names it."""
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        response = calendar.request(
            method, url, json={"title": "t", "date": "2026-10-16", PROBE_KEY: "v"}
        )

    assert response.status_code == 422, response.text
    (refusal,) = _validation_refusals(caplog)
    assert refusal.extra_forbidden == 1
    assert refusal.errors == []
    assert PROBE_KEY not in _logged_text(caplog)


def test_an_undeclared_key_beside_a_real_error_is_counted_and_the_error_kept(
    calendar: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        response = calendar.post(
            "/api/calendar/manual",
            json={"title": "t", "date": "16/10/2026", PROBE_KEY: "v"},
        )

    assert response.status_code == 422, response.text
    (refusal,) = _validation_refusals(caplog)
    assert [(error["loc"], error["type"]) for error in refusal.errors] == [
        ("body.date", "value_error")
    ]
    assert refusal.extra_forbidden == 1
    assert PROBE_KEY not in _logged_text(caplog)


def test_a_100_kb_key_name_yields_a_bounded_log_line(
    calendar: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    huge = "K" * 100_000
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        response = calendar.post(
            "/api/calendar/manual", json={"title": "t", "date": "2026-10-16", huge: "v"}
        )

    assert response.status_code == 422
    (refusal,) = _validation_refusals(caplog)
    assert len(f"{refusal.getMessage()} {refusal.__dict__!r}") < 4_000
    assert "K" * 100 not in _logged_text(caplog)


def test_a_json_decode_refusal_keeps_its_integer_position(
    calendar: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Integer segments are positions, not client-chosen names: they are kept."""
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        response = calendar.post(
            "/api/calendar/manual",
            content=b'{"title": "t",',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    (refusal,) = _validation_refusals(caplog)
    ((loc, kind),) = [(error["loc"], error["type"]) for error in refusal.errors]
    assert kind == "json_invalid"
    assert loc.startswith("body.") and loc.removeprefix("body.").isdigit()


def test_validation_log_locations_are_reduced_to_declared_names() -> None:
    """The reduction itself: a declared name or an index survives; anything
    else -- including an over-long segment -- becomes the placeholder."""
    from corollary.api.app import UNDECLARED_SEGMENT, logged_location

    declared = frozenset({"title", "date"})

    assert logged_location(("body", "title"), declared) == "body.title"
    assert logged_location(("body", 3), declared) == "body.3"
    assert logged_location(("body", PROBE_KEY), declared) == f"body.{UNDECLARED_SEGMENT}"
    assert logged_location(("query", "x" * 10_000), declared) == f"query.{UNDECLARED_SEGMENT}"
    assert logged_location((PROBE_KEY,), declared) == UNDECLARED_SEGMENT


def test_a_store_refusal_on_add_logs_the_entry_shape_not_a_null_id(
    calendar: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    body = {"title": SECRET_TITLE, "date": "2026-10-17", "at": "2026-10-17T00:30:00Z"}
    response = calendar.post("/api/calendar/manual", json=body)

    assert response.status_code == 422
    (refusal,) = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "calendar_request_refused"
    ]
    assert refusal.inputs == {
        "date": "2026-10-17",
        "at": "2026-10-17T00:30:00+00:00",
        "title_length": len(SECRET_TITLE),
    }
    assert SECRET_TITLE not in repr(refusal.__dict__)


# --------------------------------------------------------------------------
# Structure: no broker, no vendor detail
# --------------------------------------------------------------------------

_VENDOR_DEPENDENCIES = {broker_for_account, market_data, fundamentals_data, service_registry}


def _calls(dependant: Any) -> set[Any]:
    found = {dependant.call}
    for child in dependant.dependencies:
        found |= _calls(child)
    return found


@pytest.mark.risk
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/calendar", "GET"),
        ("/api/calendar/manual", "POST"),
        ("/api/calendar/manual/{event_id}", "PUT"),
        ("/api/calendar/manual/{event_id}", "DELETE"),
    ],
)
def test_no_calendar_route_depends_on_a_broker_or_a_provider(path: str, method: str) -> None:
    routes = [
        route
        for route in calendar_router.routes
        if getattr(route, "path", None) == path and method in getattr(route, "methods", ())
    ]
    assert len(routes) == 1, path
    assert not (_calls(getattr(routes[0], "dependant")) & _VENDOR_DEPENDENCIES)


def test_the_calendar_router_is_mounted(calendar: TestClient) -> None:
    assert calendar.get("/api/calendar", params={"from": "2026-10-14", "to": "2026-10-14"}).status_code == 200


def test_no_response_carries_a_key_or_vendor_detail(
    app: FastAPI, calendar: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 6: a job notice carries an error *type*, never an exception's text."""
    sentinel = "sk-live-SENTINEL-0123456789"
    monkeypatch.setenv("FINNHUB_API_KEY", sentinel)
    add_events(db_engine, earnings())
    app.state.scheduler = FakeScheduler(
        {
            "calendar_ipo": job(
                "calendar_ipo",
                last_failure=NOW,
                last_error_type="CalendarAccessDenied",
                failures=1,
            )
        }
    )
    event_id = row_id(db_engine, "NVDA:2026Q3")

    bodies = [
        calendar.get("/api/calendar", params={"from": "2026-10-14", "to": "2026-10-20"}).text,
        calendar.get("/api/calendar", params={"from": "2026-10-20", "to": "2026-10-14"}).text,
        calendar.put(
            f"/api/calendar/manual/{event_id}", json={"title": "x", "date": "2026-10-15"}
        ).text,
        calendar.post("/api/calendar/manual", json={"title": sentinel, "date": 5}).text,
    ]

    assert not any(sentinel in body for body in bodies)
    assert not any("finnhub.io" in body or "token=" in body for body in bodies)
