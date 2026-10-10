"""Finnhub earnings and IPO rows into ``calendar_event`` (Phase 3 step 7, unit 7.2b-F).

Decision 7 (earnings: ``hour`` is a session, never a time), Q15 (IPOs, a new
kind, refreshed daily), decision 15 (every host has a limiter bucket). The
recorded fixtures are replayed through the provider's own HTTP path, so the
request it builds is exercised; synthetic rows cover what the recordings do
not carry (``bmo``/``dmh``, an ``epsActual``, malformed values).

``p7_calendar_ipo_past.json``'s ``body`` is truncated to 50 of 58 rows, and
the one empty-string symbol is in the tail. Its ``body_raw`` holds the whole
response as the exact text Finnhub sent, so that is what these tests serve.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from corollary.data.calendar import read_range, upsert_events
from corollary.data.calendar_event import (
    CalendarKind,
    CalendarSource,
    EarningsSession,
    IpoStatus,
)
from corollary.data.calendar_finnhub import (
    FinnhubCalendarBatch,
    earnings_events,
    earnings_vendor_id,
    fetch_earnings,
    fetch_ipos,
    ipo_events,
    ipo_vendor_id,
    parse_offer_price,
)
from corollary.data.providers.finnhub import (
    CalendarAccessDenied,
    CalendarProviderError,
    FINNHUB_TOKEN_HEADER,
)
from corollary.db.models import Base, CalendarEvent
from corollary.db.session import create_db_engine, sqlite_url
from corollary.ratelimit import FINNHUB_HOST, HostRateLimiter
from tests.data.providers.test_finnhub_provider import (  # noqa: F401 - fixtures
    FIXTURE_DIR,
    ProviderFactory,
    Served,
    _never_sleep,
    fixture_body_bytes,
    limiter,
    make_provider,
)

T0 = datetime(2026, 10, 10, 11, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 11, 11, 0, tzinfo=UTC)
LOGGER = "corollary.data.calendar_finnhub"


def _raw_body(name: str) -> str:
    """A fixture's ``body_raw``: the untruncated response text, exactly as sent."""
    payload: dict[str, Any] = json.loads((FIXTURE_DIR / f"{name}.json").read_text("utf-8"))
    raw = payload["body_raw"]
    assert isinstance(raw, str)
    return raw


def _decoded(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def _earnings_rows() -> list[Any]:
    rows = _decoded(fixture_body_bytes("p3_calendar_earnings").decode("utf-8"))[
        "earningsCalendar"
    ]
    assert isinstance(rows, list)
    return rows


def _ipo_rows(name: str) -> list[Any]:
    rows = _decoded(_raw_body(name))["ipoCalendar"]
    assert isinstance(rows, list)
    return rows


def _earning(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": "NVDA",
        "date": "2026-11-19",
        "hour": "amc",
        "quarter": 3,
        "year": 2027,
        "epsEstimate": Decimal("1.25"),
        "epsActual": None,
        "revenueEstimate": 54000000000,
        "revenueActual": None,
    }
    row.update(overrides)
    return row


def _ipo(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": "2026-10-15",
        "exchange": "NASDAQ Global Select",
        "name": "Iambic Therapeutics, Inc.",
        "numberOfShares": 9375000,
        "price": "15.00-17.00",
        "status": "expected",
        "symbol": "IAM",
        "totalSharesValue": 183281250,
    }
    row.update(overrides)
    return row


@pytest.fixture
def session(tmp_path: Path) -> Any:
    eng = create_db_engine(sqlite_url(tmp_path / "corollary.db"))
    Base.metadata.create_all(eng)
    with Session(eng) as sess:
        yield sess
    eng.dispose()


# --- the provider: one request, metered, on the finnhub.io bucket -------------


def _route(path: str, served: Served) -> Any:
    def choose(request: httpx.Request) -> Served | None:
        return served if request.url.path == f"/api/v1{path}" else None

    return choose


@pytest.mark.asyncio
async def test_the_earnings_request_is_one_metered_get(
    make_provider: ProviderFactory, limiter: HostRateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts: list[str] = []
    real = limiter.acquire

    async def spy(host: str, tokens: int = 1) -> None:
        hosts.append(host)
        await real(host, tokens)

    monkeypatch.setattr(limiter, "acquire", spy)
    provider, transport = make_provider(_route("/calendar/earnings", "p3_calendar_earnings"))
    rows = await provider.earnings_calendar(date(2026, 9, 24), date(2026, 10, 15))
    assert len(rows) == 8
    assert hosts == [FINNHUB_HOST]
    (request,) = transport.requests
    assert dict(request.url.params) == {"from": "2026-09-24", "to": "2026-10-15"}
    assert FINNHUB_TOKEN_HEADER in request.headers
    assert "token" not in request.url.params  # rule 6: header, never the query string
    # Exact on the way in: the recorded 1.563 is a Decimal, never a double.
    assert rows[0]["epsEstimate"] == Decimal("1.563")
    assert type(rows[0]["epsEstimate"]) is Decimal


@pytest.mark.asyncio
async def test_the_ipo_request_reads_the_ipo_calendar(make_provider: ProviderFactory) -> None:
    provider, transport = make_provider(_route("/calendar/ipo", "p7_calendar_ipo"))
    rows = await provider.ipo_calendar(date(2026, 10, 10), date(2026, 11, 9))
    assert [row["symbol"] for row in rows] == ["IAM"]
    (request,) = transport.requests
    assert dict(request.url.params) == {"from": "2026-10-10", "to": "2026-11-09"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("served", "error", "match"),
    [
        pytest.param(
            (403, '{"error":"You don\'t have access to this resource."}'),
            CalendarAccessDenied,
            "not on this plan",
            id="premium 403",
        ),
        pytest.param((401, "{}"), CalendarAccessDenied, "403|401", id="401"),
        pytest.param((429, "{}"), CalendarProviderError, "429", id="429"),
        pytest.param((502, "<html>bad gateway</html>"), CalendarProviderError, "502", id="502"),
        pytest.param((200, "not json"), CalendarProviderError, "JSON", id="not JSON"),
        pytest.param((200, "[]"), CalendarProviderError, "not an object", id="a list"),
        pytest.param((200, "{}"), CalendarProviderError, "ipoCalendar", id="key missing"),
        pytest.param(
            (200, '{"ipoCalendar": null}'), CalendarProviderError, "ipoCalendar", id="key null"
        ),
    ],
)
async def test_a_failed_calendar_request_raises(
    make_provider: ProviderFactory, served: Served, error: type[Exception], match: str
) -> None:
    provider, _ = make_provider(_route("/calendar/ipo", served))
    with pytest.raises(error, match=match):
        await provider.ipo_calendar(date(2026, 10, 10), date(2026, 11, 9))


def test_access_denied_is_a_provider_error() -> None:
    assert issubclass(CalendarAccessDenied, CalendarProviderError)


@pytest.mark.asyncio
async def test_an_inverted_window_is_refused_before_any_request(
    make_provider: ProviderFactory,
) -> None:
    provider, transport = make_provider(_route("/calendar/earnings", "p3_calendar_earnings"))
    with pytest.raises(ValueError, match="before"):
        await provider.earnings_calendar(date(2026, 10, 15), date(2026, 9, 24))
    assert transport.requests == []


# --- earnings: decision 7 ------------------------------------------------------

WATCH = ("AA", "AAL", "AIMUF")


def test_recorded_earnings_map_onto_the_watch_universe() -> None:
    batch = earnings_events(_earnings_rows(), WATCH)
    assert batch.skipped == ()
    assert batch.outside_watch == 5  # 8 recorded rows, 3 watched
    by_ticker = {event.ticker: event for event in batch.events}
    assert set(by_ticker) == set(WATCH)
    for event in batch.events:
        assert event.kind is CalendarKind.EARNINGS
        assert event.source is CalendarSource.FINNHUB
        assert event.at is None  # never an invented time
        assert event.prior is None
        assert event.unit == "EPS"
    aa = by_ticker["AA"]
    assert aa.date == date(2026, 10, 15)
    assert aa.session is EarningsSession.AMC
    assert aa.vendor_id == "AA:2026Q3"
    assert type(aa.estimate) is Decimal and str(aa.estimate) == "1.563"
    assert aa.actual is None
    assert by_ticker["AAL"].session is None  # recorded ``hour: ""``
    assert by_ticker["AAL"].estimate == Decimal("-0.3546")
    assert by_ticker["AIMUF"].estimate is None  # recorded ``epsEstimate: null``
    assert by_ticker["AIMUF"].vendor_id == "AIMUF:2026Q2"


def test_the_watch_universe_is_compared_in_upper_case() -> None:
    batch = earnings_events(_earnings_rows(), ["aa", " aal "])
    assert sorted(event.ticker or "" for event in batch.events) == ["AA", "AAL"]


def test_an_empty_watch_universe_writes_nothing() -> None:
    batch = earnings_events(_earnings_rows(), [])
    assert batch.events == () and batch.outside_watch == 8


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        ("bmo", EarningsSession.BMO),
        ("amc", EarningsSession.AMC),
        ("dmh", EarningsSession.DMH),
        ("", None),
        (None, None),
        ("pre", None),
        ("08:00", None),
    ],
)
def test_the_hour_is_a_session_not_a_time(hour: str | None, expected: object) -> None:
    (event,) = earnings_events([_earning(hour=hour)], ["NVDA"]).events
    assert event.session is expected
    assert event.at is None


def test_an_unknown_hour_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        earnings_events([_earning(hour="pre")], ["NVDA"])
    (record,) = [r for r in caplog.records if r.name == LOGGER]
    assert getattr(record, "event") == "finnhub_earnings_unknown_hour"
    assert getattr(record, "hour") == "pre"


def test_an_actual_lands_beside_the_estimate_exactly() -> None:
    (event,) = earnings_events(
        [_earning(epsActual=Decimal("1.3100"), epsEstimate=2)], ["NVDA"]
    ).events
    assert type(event.actual) is Decimal and format(event.actual, "f") == "1.3100"
    assert event.estimate == Decimal("2") and type(event.estimate) is Decimal


def test_revenue_is_not_stored_in_the_eps_columns() -> None:
    (event,) = earnings_events(
        [_earning(epsEstimate=None, revenueEstimate=54000000000)], ["NVDA"]
    ).events
    assert event.estimate is None  # revenue never stands in for a missing EPS


def test_the_vendor_id_is_the_fiscal_period_not_the_date() -> None:
    assert earnings_vendor_id("NVDA", 2027, 3) == "NVDA:2027Q3"
    moved = earnings_events([_earning(date="2026-11-26")], ["NVDA"]).events[0]
    assert moved.vendor_id == earnings_events([_earning()], ["NVDA"]).events[0].vendor_id


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param({"symbol": None}, "symbol", id="no symbol"),
        pytest.param({"symbol": ""}, "symbol", id="empty symbol"),
        pytest.param({"date": None}, "date", id="no date"),
        pytest.param({"date": "2026/11/19"}, "date", id="malformed date"),
        pytest.param({"date": "20261119"}, "date", id="compact date"),
        pytest.param({"year": None}, "year", id="no year"),
        pytest.param({"year": True}, "year", id="bool year"),
        pytest.param({"year": Decimal("2027.0")}, "year", id="fractional year"),
        pytest.param({"quarter": None}, "quarter", id="no quarter"),
        pytest.param({"quarter": 5}, "quarter", id="quarter five"),
        pytest.param({"quarter": 0}, "quarter", id="quarter zero"),
        pytest.param({"epsEstimate": True}, "epsEstimate", id="bool estimate"),
        pytest.param({"epsEstimate": float("nan")}, "epsEstimate", id="NaN estimate"),
        pytest.param({"epsActual": float("inf")}, "epsActual", id="Infinity actual"),
        pytest.param({"epsEstimate": "abc"}, "epsEstimate", id="text estimate"),
        pytest.param({"epsEstimate": [1]}, "epsEstimate", id="list estimate"),
    ],
)
def test_an_unreadable_earnings_row_is_skipped_and_reported(
    overrides: dict[str, Any], reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    good = _earning(symbol="AMD")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        batch = earnings_events([_earning(**overrides), good], ["NVDA", "AMD", ""])
    assert [event.ticker for event in batch.events] == ["AMD"]
    (skipped,) = batch.skipped
    assert skipped.index == 0 and reason in skipped.reason
    assert any(getattr(r, "event", None) == "finnhub_calendar_row_skipped" for r in caplog.records)


def test_a_finite_float_is_our_bug_and_raises() -> None:
    # A finite float can only arrive if the decoder was bypassed: loud, not a skip.
    with pytest.raises(TypeError):
        earnings_events([_earning(epsEstimate=1.25)], ["NVDA"])


def test_a_row_that_is_not_an_object_is_skipped() -> None:
    batch = earnings_events(["NVDA", None, _earning()], ["NVDA"])
    assert len(batch.events) == 1
    assert [s.index for s in batch.skipped] == [0, 1]


def test_an_identical_duplicate_collapses_to_one_row() -> None:
    batch = earnings_events([_earning(), _earning()], ["NVDA"])
    assert len(batch.events) == 1 and batch.skipped == ()


def test_conflicting_duplicates_are_all_skipped() -> None:
    batch = earnings_events(
        [_earning(), _earning(date="2026-11-26"), _earning(symbol="AMD")], ["NVDA", "AMD"]
    )
    assert [event.ticker for event in batch.events] == ["AMD"]
    assert sorted(s.index for s in batch.skipped) == [0, 1]
    assert all("NVDA:2027Q3" in s.reason for s in batch.skipped)


def test_a_rescheduled_report_moves_its_row(session: Session) -> None:
    first = earnings_events([_earning()], ["NVDA"]).events
    upsert_events(session, first, now=T0)
    moved = earnings_events([_earning(date="2026-11-26", hour="bmo")], ["NVDA"]).events
    counts = upsert_events(session, moved, now=T1)
    assert (counts.inserted, counts.updated) == (0, 1)
    rows = list(session.scalars(select(CalendarEvent)))
    assert len(rows) == 1
    assert rows[0].date == date(2026, 11, 26) and rows[0].session == "bmo"


def test_the_recorded_earnings_round_trip_through_the_store(session: Session) -> None:
    batch = earnings_events(_earnings_rows(), WATCH)
    upsert_events(session, batch.events, now=T0)
    stored = read_range(session, date(2026, 9, 24), date(2026, 10, 15))
    assert {(e.ticker, e.session) for e in stored} == {
        ("AA", EarningsSession.AMC),
        ("AAL", None),
        ("AIMUF", None),
    }
    assert all(e.at is None for e in stored)
    assert {e.ticker: e.estimate for e in stored}["AA"] == Decimal("1.563")


@pytest.mark.asyncio
async def test_fetch_earnings_requests_once_and_filters(make_provider: ProviderFactory) -> None:
    provider, transport = make_provider(_route("/calendar/earnings", "p3_calendar_earnings"))
    batch = await fetch_earnings(
        provider, WATCH, start=date(2026, 9, 24), end=date(2026, 10, 15)
    )
    assert isinstance(batch, FinnhubCalendarBatch)
    assert len(transport.requests) == 1  # 1/day: the whole window in one request
    assert {event.ticker for event in batch.events} == set(WATCH)


# --- IPOs: Q15 -------------------------------------------------------------------


def test_the_recorded_upcoming_ipo_maps_in_full() -> None:
    batch = ipo_events(_ipo_rows("p7_calendar_ipo"))
    assert batch.skipped == ()
    (event,) = batch.events
    assert event.kind is CalendarKind.IPO and event.source is CalendarSource.FINNHUB
    assert event.ticker == "IAM"
    assert event.title == "Iambic Therapeutics, Inc."
    assert event.date == date(2026, 10, 15) and event.at is None
    assert event.exchange == "NASDAQ Global Select"
    assert event.shares == 9375000
    assert (event.price_low, event.price_high) == (Decimal("15.00"), Decimal("17.00"))
    assert type(event.price_low) is Decimal and format(event.price_low, "f") == "15.00"
    assert event.ipo_status is IpoStatus.EXPECTED
    assert event.vendor_id == "symbol:IAM"
    assert (event.estimate, event.actual, event.session) == (None, None, None)


def test_every_recorded_past_ipo_is_kept() -> None:
    rows = _ipo_rows("p7_calendar_ipo_past")
    assert len(rows) == 58
    batch = ipo_events(rows)
    assert batch.skipped == ()
    assert len(batch.events) == 58
    assert len({event.vendor_id for event in batch.events}) == 58
    # 8 null symbols and 1 empty string: every one kept, by name.
    nameless = [event for event in batch.events if event.ticker is None]
    assert len(nameless) == 9
    assert all(event.vendor_id.startswith("name:") for event in nameless)
    assert {event.ipo_status for event in batch.events} == set(IpoStatus)


def test_the_empty_string_symbol_is_kept_by_name() -> None:
    rows = [row for row in _ipo_rows("p7_calendar_ipo_past") if row["symbol"] == ""]
    assert len(rows) == 1  # recorded: Meey Global Corp
    (event,) = ipo_events(rows).events
    assert event.ticker is None
    assert event.title == "Meey Global Corp"
    assert event.vendor_id == "name:meey global corp"


def test_a_recorded_single_price_is_low_equal_to_high() -> None:
    rows = [r for r in _ipo_rows("p7_calendar_ipo_past") if r["price"] == "5.60"]
    (event,) = ipo_events(rows).events
    assert event.price_low == event.price_high == Decimal("5.60")
    assert format(event.price_high, "f") == "5.60"


def test_a_recorded_filed_row_has_no_price_shares_or_exchange() -> None:
    rows = [r for r in _ipo_rows("p7_calendar_ipo_past") if r["symbol"] == "FFLY"]
    (event,) = ipo_events(rows).events
    assert (event.price_low, event.price_high, event.shares, event.exchange) == (
        None,
        None,
        None,
        None,
    )
    assert event.ipo_status is IpoStatus.FILED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("14.00-16.00", (Decimal("14.00"), Decimal("16.00"))),
        ("10.00", (Decimal("10.00"), Decimal("10.00"))),
        (" 4.00 - 5.00 ", (Decimal("4.00"), Decimal("5.00"))),
        ("18", (Decimal("18"), Decimal("18"))),
        (None, None),
        ("", None),
    ],
)
def test_the_offer_price_is_parsed_from_text(
    text: str | None, expected: tuple[Decimal, Decimal] | None
) -> None:
    assert parse_offer_price(text) == expected


@pytest.mark.parametrize("text", ["abc", "$10.00", "10.00-", "-10.00", "1e3", "１０.00", "10-12-14"])
def test_an_unreadable_offer_price_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_offer_price(text)


@pytest.mark.parametrize("status", ["expected", "filed", "priced", "withdrawn"])
def test_every_status_maps(status: str) -> None:
    (event,) = ipo_events([_ipo(status=status)]).events
    assert event.ipo_status is IpoStatus(status)


@pytest.mark.parametrize("status", ["postponed", "", None, "Expected"])
def test_an_unknown_status_is_null(status: str | None) -> None:
    (event,) = ipo_events([_ipo(status=status)]).events
    assert event.ipo_status is None


def test_zero_shares_is_no_share_count() -> None:
    (event,) = ipo_events([_ipo(numberOfShares=0)]).events
    assert event.shares is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param({"symbol": None, "name": None}, "neither", id="neither symbol nor name"),
        pytest.param({"symbol": "", "name": "  "}, "neither", id="blank symbol and name"),
        pytest.param({"date": None}, "date", id="no date"),
        pytest.param({"price": "TBD"}, "price", id="unreadable price"),
        pytest.param({"price": "17.00-15.00"}, "price", id="inverted range"),
        pytest.param({"price": 10}, "price", id="price not text"),
        pytest.param({"numberOfShares": True}, "numberOfShares", id="bool shares"),
        pytest.param({"numberOfShares": -3}, "numberOfShares", id="negative shares"),
        pytest.param({"numberOfShares": Decimal("1.5")}, "numberOfShares", id="fractional"),
        pytest.param({"exchange": 7}, "exchange", id="exchange not text"),
    ],
)
def test_an_unreadable_ipo_row_is_skipped_and_reported(
    overrides: dict[str, Any], reason: str
) -> None:
    batch = ipo_events([_ipo(**overrides), _ipo(symbol="ABC", name="ABC Corp")])
    assert [event.ticker for event in batch.events] == ["ABC"]
    (skipped,) = batch.skipped
    assert skipped.index == 0 and reason in skipped.reason


def test_a_symbol_with_no_name_is_titled_by_symbol() -> None:
    (event,) = ipo_events([_ipo(name=None)]).events
    assert event.title == "IAM IPO"


def test_the_ipo_vendor_id_never_carries_the_date() -> None:
    assert ipo_vendor_id("IAM", "Iambic Therapeutics, Inc.") == "symbol:IAM"
    assert ipo_vendor_id(None, "Holtec Nuclear Corp") == "name:holtec nuclear corp"
    # Punctuation, case and spacing do not make a second company.
    assert ipo_vendor_id(None, "HOLTEC  Nuclear Corp.") == "name:holtec nuclear corp"
    assert ipo_vendor_id(None, None) is None
    assert ipo_vendor_id("", "   ") is None
    long = ipo_vendor_id(None, "X" * 300)
    assert long is not None and len(long) <= 128


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="by symbol"),
        pytest.param({"symbol": None}, id="by name"),
    ],
)
def test_a_moved_ipo_date_updates_rather_than_duplicates(
    session: Session, overrides: dict[str, Any]
) -> None:
    upsert_events(session, ipo_events([_ipo(**overrides)]).events, now=T0)
    moved = ipo_events([_ipo(date="2026-10-22", status="priced", **overrides)]).events
    counts = upsert_events(session, moved, now=T1)
    assert (counts.inserted, counts.updated) == (0, 1)
    (row,) = session.scalars(select(CalendarEvent))
    assert row.date == date(2026, 10, 22) and row.ipo_status == "priced"


def test_the_recorded_past_ipos_round_trip_through_the_store(session: Session) -> None:
    batch = ipo_events(_ipo_rows("p7_calendar_ipo_past"))
    counts = upsert_events(session, batch.events, now=T0)
    assert counts.inserted == 58
    again = upsert_events(session, ipo_events(_ipo_rows("p7_calendar_ipo_past")).events, now=T1)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 58)
    stored = read_range(session, date(2026, 9, 10), date(2026, 10, 10))
    assert len(stored) == 58
    trxb = next(e for e in stored if e.ticker == "TRXB")
    assert (trxb.price_low, trxb.price_high) == (Decimal("14.00"), Decimal("16.00"))


@pytest.mark.asyncio
async def test_fetch_ipos_requests_once(make_provider: ProviderFactory) -> None:
    raw = _raw_body("p7_calendar_ipo_past")
    provider, transport = make_provider(_route("/calendar/ipo", (200, raw)))
    batch = await fetch_ipos(provider, start=date(2026, 9, 10), end=date(2026, 10, 10))
    assert len(transport.requests) == 1
    assert len(batch.events) == 58


# --- a value too wide for its column skips its row, not the batch (7.2c-1) -----


@pytest.mark.parametrize("eps", [Decimal("1e50"), Decimal("1e-45")], ids=["1e50", "1e-45"])
def test_an_eps_too_wide_for_its_column_skips_only_that_row(
    session: Session, eps: Decimal
) -> None:
    rows = [_earning(symbol="NVDA", epsEstimate=eps), _earning(symbol="AAPL")]
    batch = earnings_events(rows, ["NVDA", "AAPL"])
    assert [e.ticker for e in batch.events] == ["AAPL"]
    assert [s.index for s in batch.skipped] == [0]
    assert "40" in batch.skipped[0].reason
    upsert_events(session, batch.events, now=T0)
    session.commit()
    assert [e.ticker for e in read_range(session, date(2026, 11, 1), date(2026, 11, 30))] == [
        "AAPL"
    ]


def test_a_share_count_past_sqlite_integer_skips_only_that_row(session: Session) -> None:
    rows = [
        _ipo(symbol="BIG", numberOfShares=99999999999999999999),
        _ipo(symbol="IAM"),
    ]
    batch = ipo_events(rows)
    assert [e.ticker for e in batch.events] == ["IAM"]
    assert [s.index for s in batch.skipped] == [0]
    upsert_events(session, batch.events, now=T0)
    session.commit()
