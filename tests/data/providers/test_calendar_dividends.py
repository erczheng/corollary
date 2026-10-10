"""``corollary.data.calendar_dividends`` -- Alpaca cash dividends into ``calendar_event`` rows.

Phase 3 step 7, unit 7.2b-A. Driven end to end through the real
``AlpacaProvider`` over the **verbatim** ``body_raw`` of
``p7_corporate_actions_cash_dividend_long.json`` (recorded 2026-10-10), so
the mapping is tested against what the vendor actually sends rather than
against records this file invented. The recorded page's six rows:

=========  ==========  =======  =========
symbol     ex_date     foreign  rate
=========  ==========  =======  =========
A          2026-10-06  false    0.255
AAGRY      2026-10-13  true     0.03892
AAP        2026-10-09  false    0.25
AAPW       2026-10-13  false    0.150599
ABBV       2026-10-15  false    1.73
ABEV       2025-12-22  true     0.019284
=========  ==========  =======  =========

ABEV is the probe's headline fact in one row: a 2025 ex-date returned by a
2026 window, because the window filters on process/payable date.
"""

from datetime import date, timedelta
from itertools import permutations
from decimal import Decimal
from typing import Any

import pytest

from corollary.data.calendar_dividends import (
    DIVIDEND_EX_DATE_HORIZON,
    DIVIDEND_REQUEST_HORIZON,
    DIVIDEND_REQUEST_LOOKBACK,
    DIVIDEND_UNIT,
    DIVIDEND_UNIT_FOREIGN,
    CashDividend,
    CashDividendRead,
    DividendFetch,
    DividendOutcome,
    SkippedDividend,
    dividend_events,
    fetch_dividends,
)
from corollary.data.calendar_event import CalendarKind, CalendarSource
from corollary.data.news.watchlist import MARKET_TICKER
from tests.data.providers.conftest import load_fixture

pytestmark = pytest.mark.asyncio

BODY_RAW: str = load_fixture("p7_corporate_actions_cash_dividend_long")["body_raw"]
PAGE_TOKEN = "QUJFVnwyMDI3LTAxLTExfDhkMGVhNzFkLTUyZTktNGE1Yi05NTM2LTY0OGFhZWViMTQ5MQ=="
TERMINAL = BODY_RAW.replace(f'"next_page_token":"{PAGE_TOKEN}"', '"next_page_token":null')
EMPTY = '{"corporate_actions":{"cash_dividends":[]},"next_page_token":null}'

RECORDED_DAY = date(2026, 10, 10)
EVERYONE = frozenset({"A", "AAGRY", "AAP", "AAPW", "ABBV", "ABEV"})
ABBV_ID = "ad029c18-25b0-4d0f-9cc4-25c4aa648302"
AAP_ID = "3d300f69-484e-4c8d-bcb8-c6a10f5c46aa"


def edited(old: str, new: str, *, body: str = TERMINAL) -> str:
    assert body.count(old) == 1, f"{old!r} occurs {body.count(old)} times in the body"
    return body.replace(old, new)


async def fetch(
    make_provider: Any,
    body: str | tuple[int, str] = TERMINAL,
    *,
    today: date = RECORDED_DAY,
    watch: frozenset[str] = EVERYONE,
) -> tuple[DividendFetch, Any]:
    served = body if isinstance(body, tuple) else (200, body)
    provider, transport = make_provider(lambda _request: served)
    return await fetch_dividends(provider, today=today, watch=watch), transport


def tickers(result: DividendFetch) -> list[str]:
    return [event.ticker or "" for event in result.events]


async def test_the_window_constants_are_the_orchestrators() -> None:
    assert DIVIDEND_REQUEST_HORIZON == timedelta(days=180)
    assert DIVIDEND_EX_DATE_HORIZON == timedelta(days=90)
    assert DIVIDEND_REQUEST_LOOKBACK == timedelta(days=7)
    assert DIVIDEND_UNIT == "USD/share"


async def test_the_request_window_and_symbols(make_provider: Any) -> None:
    _, transport = await fetch(
        make_provider, watch=frozenset({"ABBV", "AAPW", MARKET_TICKER})
    )
    params = transport.params_for("/v1/corporate-actions")
    assert params["start"] == "2026-10-03"  # today - 7 days
    assert params["end"] == "2027-04-08"  # today + 180 days
    # MARKET is the news feed's pseudo-ticker, not a security.
    assert params["symbols"] == "AAPW,ABBV"
    assert params["types"] == "cash_dividend"


async def test_a_row_maps_to_a_date_only_dividend_event(make_provider: Any) -> None:
    result, _ = await fetch(make_provider, watch=frozenset({"ABBV"}))
    assert result.outcome is DividendOutcome.ANNOUNCED
    [event] = result.events
    assert event.kind is CalendarKind.DIVIDEND
    assert event.source is CalendarSource.ALPACA
    assert event.vendor_id == ABBV_ID
    assert event.ticker == "ABBV"
    assert event.date == date(2026, 10, 15)
    assert event.at is None
    assert event.actual == Decimal("1.73")
    assert type(event.actual) is Decimal
    assert event.unit == "USD/share"
    assert event.estimate is None and event.prior is None
    assert event.session is None
    assert event.title == "Ex-dividend date"


async def test_ex_dates_before_today_are_dropped_even_though_alpaca_returned_them(
    make_provider: Any,
) -> None:
    result, _ = await fetch(make_provider)
    # A (10-06), AAP (10-09) and ABEV (2025-12-22) are past; sorted by date then ticker.
    assert tickers(result) == ["AAGRY", "AAPW", "ABBV"]
    assert [e.date for e in result.events] == [
        date(2026, 10, 13),
        date(2026, 10, 13),
        date(2026, 10, 15),
    ]


async def test_an_ex_date_of_today_is_kept_and_yesterday_is_not(
    make_provider: Any,
) -> None:
    on, _ = await fetch(make_provider, today=date(2026, 10, 13))
    assert tickers(on) == ["AAGRY", "AAPW", "ABBV"]
    after, _ = await fetch(make_provider, today=date(2026, 10, 14))
    assert tickers(after) == ["ABBV"]


async def test_an_ex_date_exactly_ninety_days_out_is_kept_and_ninety_one_is_not(
    make_provider: Any,
) -> None:
    abbv_ex = date(2026, 10, 15)
    at_edge, _ = await fetch(make_provider, today=abbv_ex - timedelta(days=90))
    assert "ABBV" in tickers(at_edge)
    beyond, _ = await fetch(make_provider, today=abbv_ex - timedelta(days=91))
    assert "ABBV" not in tickers(beyond)


async def test_only_the_watch_universe_is_kept(make_provider: Any) -> None:
    # The mock serves every row whatever ``symbols`` said: the client-side
    # filter is what this pins, independent of the server-side one.
    result, _ = await fetch(make_provider, watch=frozenset({"AAPW", "ZZZZ"}))
    assert tickers(result) == ["AAPW"]


async def test_the_watch_universe_is_normalised(make_provider: Any) -> None:
    result, transport = await fetch(make_provider, watch=frozenset({" abbv "}))
    assert tickers(result) == ["ABBV"]
    assert transport.params_for("/v1/corporate-actions")["symbols"] == "ABBV"


async def test_an_empty_watch_universe_is_refused_without_a_request(
    make_provider: Any,
) -> None:
    provider, transport = make_provider(lambda _r: (200, TERMINAL))
    with pytest.raises(ValueError, match="watch"):
        await fetch_dividends(provider, today=RECORDED_DAY, watch=frozenset({MARKET_TICKER}))
    assert transport.requests == []


@pytest.mark.parametrize(
    ("new", "title"),
    [
        ('"special":true,"symbol":"ABBV"', "Ex-dividend date (special)"),
        (
            '"special":false,"sub_type":"return_of_capital","symbol":"ABBV"',
            "Ex-dividend date (return of capital)",
        ),
        (
            '"special":true,"sub_type":"return_of_capital","symbol":"ABBV"',
            "Ex-dividend date (special, return of capital)",
        ),
    ],
)
async def test_special_and_return_of_capital_are_named_in_the_title(
    make_provider: Any, new: str, title: str
) -> None:
    body = edited('"special":false,"symbol":"ABBV"', new)
    result, _ = await fetch(make_provider, body, watch=frozenset({"ABBV"}))
    [event] = result.events
    assert event.title == title


async def test_foreign_is_not_named_in_the_title(make_provider: Any) -> None:
    # Alpaca documents ``foreign`` as a required boolean with no description.
    # A title claiming what it means would be a claim the data does not make.
    result, _ = await fetch(make_provider, watch=frozenset({"AAGRY"}))
    [event] = result.events
    assert event.title == "Ex-dividend date"


async def test_an_unparseable_rate_is_skipped_and_reported(make_provider: Any) -> None:
    # AAP's ex-date is 10-09, so view it from 10-09 where it is in the window.
    body = edited('"rate":0.25,', '"rate":"n/a",')
    result, _ = await fetch(make_provider, body, today=date(2026, 10, 9))
    assert "AAP" not in tickers(result)
    assert tickers(result) == ["AAGRY", "AAPW", "ABBV"]
    [skipped] = result.skipped
    assert skipped.vendor_id == AAP_ID
    assert "rate" in skipped.reason
    assert result.outcome is DividendOutcome.ANNOUNCED


async def test_a_skipped_row_outside_the_window_or_the_watch_is_not_reported(
    make_provider: Any,
) -> None:
    body = edited('"rate":0.25,', '"rate":"n/a",')
    past, _ = await fetch(make_provider, body)  # AAP's 10-09 is before 10-10
    assert past.skipped == ()
    unwatched, _ = await fetch(
        make_provider, body, today=date(2026, 10, 9), watch=frozenset({"ABBV"})
    )
    assert unwatched.skipped == ()


async def test_when_every_announced_row_is_unreadable_the_outcome_is_not_empty(
    make_provider: Any,
) -> None:
    # "No dividends announced" would be false here: one was, and it could not be read.
    body = edited('"rate":1.73,', '"rate":"n/a",')
    result, _ = await fetch(make_provider, body, watch=frozenset({"ABBV"}))
    assert result.events == ()
    assert len(result.skipped) == 1
    assert result.outcome is DividendOutcome.ANNOUNCED


async def test_a_vendor_id_listed_twice_with_different_rows_skips_both(
    make_provider: Any,
) -> None:
    # Unit 7.2c-1: first-wins made the result depend on page order. Two rows
    # under one id that disagree are both skipped and reported as conflicting.
    body = edited(
        "4a52283b-0fbf-4996-9e27-15f324115a5b", ABBV_ID
    )  # AAPW's row now carries ABBV's id
    result, _ = await fetch(make_provider, body)
    assert ABBV_ID not in [e.vendor_id for e in result.events]
    assert tickers(result) == ["AAGRY"]
    assert sorted(s.symbol or "" for s in result.skipped) == ["AAPW", "ABBV"]
    assert all(s.vendor_id == ABBV_ID and "conflict" in s.reason for s in result.skipped)
    assert result.outcome is DividendOutcome.ANNOUNCED


async def test_no_dividend_in_the_window_is_a_real_answer(make_provider: Any) -> None:
    for body, today in ((EMPTY, RECORDED_DAY), (TERMINAL, date(2027, 6, 1))):
        result, _ = await fetch(make_provider, body, today=today)
        assert result.outcome is DividendOutcome.NONE_ANNOUNCED
        assert result.events == () and result.skipped == ()
        assert result.error is None


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_a_failed_fetch_is_distinct_from_an_empty_window(
    make_provider: Any, status: int
) -> None:
    result, _ = await fetch(make_provider, (status, '{"message":"no"}'))
    assert result.outcome is DividendOutcome.FAILED
    assert result.events == () and result.skipped == ()
    assert result.error is not None and str(status) in result.error


async def test_a_malformed_body_is_a_failed_fetch(make_provider: Any) -> None:
    result, _ = await fetch(make_provider, (200, "[]"))
    assert result.outcome is DividendOutcome.FAILED


async def test_the_result_states_its_window(make_provider: Any) -> None:
    result, _ = await fetch(make_provider)
    assert result.first_ex_date == RECORDED_DAY
    assert result.last_ex_date == date(2027, 1, 8)  # today + 90


async def test_identical_inputs_give_identical_output(make_provider: Any) -> None:
    first, _ = await fetch(make_provider)
    second, _ = await fetch(make_provider)
    assert first == second


# --- unit 7.2c-1: width bounds, order-independent duplicates, foreign rows ------

def _cash(vendor_id: str = "ca-1", **overrides: Any) -> CashDividend:
    fields: dict[str, Any] = {
        "vendor_id": vendor_id,
        "symbol": "ABBV",
        "ex_date": date(2026, 10, 15),
        "rate": Decimal("1.73"),
        "special": False,
        "foreign": False,
    }
    fields.update(overrides)
    return CashDividend(**fields)


def _map(*rows: CashDividend) -> tuple[Any, ...]:
    return dividend_events(
        CashDividendRead(dividends=rows, skipped=()),
        today=RECORDED_DAY,
        watch=frozenset({"ABBV", "AAPW"}),
    )


async def test_two_copies_of_one_id_that_disagree_are_both_skipped_in_any_order() -> None:
    first = _cash("ca-1", rate=Decimal("1.73"))
    second = _cash("ca-1", rate=Decimal("1.74"))
    other = _cash("ca-2", symbol="AAPW")
    results = {_map(*order) for order in permutations([first, second, other])}
    assert len(results) == 1  # identical output whatever order the vendor paged in
    [(events, skipped)] = results
    assert [e.vendor_id for e in events] == ["ca-2"]
    assert [s.vendor_id for s in skipped] == ["ca-1", "ca-1"]
    assert all("conflict" in s.reason for s in skipped)


async def test_identical_copies_of_one_id_collapse_to_one_event() -> None:
    events, skipped = _map(_cash("ca-1"), _cash("ca-1"))
    assert [e.vendor_id for e in events] == ["ca-1"]
    assert skipped == ()


async def test_a_readable_copy_conflicts_with_an_unreadable_copy_of_its_id() -> None:
    unreadable = SkippedDividend(
        vendor_id="ca-1", symbol="ABBV", ex_date=date(2026, 10, 15), reason="rate is unreadable"
    )
    events, skipped = dividend_events(
        CashDividendRead(dividends=(_cash("ca-1"),), skipped=(unreadable,)),
        today=RECORDED_DAY,
        watch=frozenset({"ABBV"}),
    )
    assert events == ()
    assert {s.reason for s in skipped} >= {"rate is unreadable"}
    assert any("conflict" in s.reason for s in skipped)


async def test_a_rate_too_wide_for_its_column_skips_only_that_row() -> None:
    events, skipped = _map(_cash("ca-1", rate=Decimal("1e50")), _cash("ca-2", symbol="AAPW"))
    assert [e.vendor_id for e in events] == ["ca-2"]
    [row] = skipped
    assert row.vendor_id == "ca-1"
    assert "40" in row.reason


async def test_an_id_longer_than_the_column_is_refused_by_the_record() -> None:
    with pytest.raises(ValueError, match="128"):
        _cash("x" * 129)
    assert _cash("x" * 128).vendor_id == "x" * 128


async def test_an_id_longer_than_the_column_does_not_stop_the_fetch(
    make_provider: Any,
) -> None:
    body = edited(ABBV_ID, "x" * 129)
    result, _ = await fetch(make_provider, body)
    assert "ABBV" not in tickers(result)
    assert tickers(result) == ["AAGRY", "AAPW"]
    [skipped] = result.skipped
    assert skipped.symbol == "ABBV"
    assert result.outcome is DividendOutcome.ANNOUNCED


async def test_a_foreign_row_keeps_its_rate_but_claims_no_currency(
    make_provider: Any,
) -> None:
    result, _ = await fetch(make_provider, watch=frozenset({"AAGRY", "ABBV"}))
    by_ticker = {e.ticker: e for e in result.events}
    assert by_ticker["AAGRY"].actual == Decimal("0.03892")
    assert by_ticker["AAGRY"].unit == DIVIDEND_UNIT_FOREIGN == "per share"
    assert by_ticker["ABBV"].unit == DIVIDEND_UNIT == "USD/share"
