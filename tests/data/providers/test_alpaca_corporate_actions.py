"""``AlpacaProvider.cash_dividends`` -- the corporate-actions read (Phase 3 step 7, unit 7.2b-A).

Replayed from the **verbatim** ``body_raw`` of
``tests/fixtures/alpaca/p7_corporate_actions_cash_dividend_long.json``: the
first page of a ``types=cash_dividend`` request over +365 days, exactly the
bytes Alpaca sent. ``body_raw`` and not ``body``, because ``rate`` is an
unquoted JSON number and the whole point of these tests is that the literal
text becomes the ``Decimal`` -- a re-serialised ``body`` would have been
through a float formatter already.

Variants (an integer rate, an unparseable rate, a second page) are made by
**editing that text** in one place each, so every other byte is still
Alpaca's.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from corollary.data.calendar_dividends import CashDividend, CashDividendRead
from corollary.data.providers.interface import ProviderError
from corollary.ratelimit import (
    ALPACA_DATA_HOST,
    ALPACA_PAPER_TRADING_HOST,
    HostRateLimiter,
)
from tests.data.providers.conftest import load_fixture

pytestmark = pytest.mark.asyncio

FIXTURE = "p7_corporate_actions_cash_dividend_long"
PATH = "/v1/corporate-actions"
BODY_RAW: str = load_fixture(FIXTURE)["body_raw"]
PAGE_TOKEN = "QUJFVnwyMDI3LTAxLTExfDhkMGVhNzFkLTUyZTktNGE1Yi05NTM2LTY0OGFhZWViMTQ5MQ=="
#: The same page with its token cleared: a terminal page, one edit from Alpaca's.
TERMINAL = BODY_RAW.replace(f'"next_page_token":"{PAGE_TOKEN}"', '"next_page_token":null')

START = date(2026, 10, 3)
END = date(2027, 4, 8)
SYMBOLS = ("ABBV", "AAPW", "AAGRY")


def edited(old: str, new: str, *, body: str = TERMINAL) -> str:
    """``body`` with exactly one occurrence of ``old`` replaced -- refused otherwise."""
    assert body.count(old) == 1, f"{old!r} occurs {body.count(old)} times in the body"
    return body.replace(old, new)


async def read(make_provider: Any, *bodies: str) -> tuple[CashDividendRead, Any]:
    served = {"n": 0}

    def route(_request: Any) -> tuple[int, str]:
        index = min(served["n"], len(bodies) - 1)
        served["n"] += 1
        return (200, bodies[index])

    provider, transport = make_provider(route)
    result = await provider.cash_dividends(symbols=SYMBOLS, start=START, end=END)
    return result, transport


def by_id(result: CashDividendRead) -> dict[str, CashDividend]:
    return {row.vendor_id: row for row in result.dividends}


async def test_the_fixture_has_not_been_edited() -> None:
    # The tests below lean on these facts of the recorded page; if a
    # re-recording changes them, every assertion should be re-read, not patched.
    assert BODY_RAW.count('"id":') == 6
    assert f'"next_page_token":"{PAGE_TOKEN}"' in BODY_RAW
    assert '"rate":0.150599' in BODY_RAW


async def test_the_request_carries_the_window_types_symbols_and_limit(
    make_provider: Any,
) -> None:
    _, transport = await read(make_provider, TERMINAL)
    assert transport.hosts == [ALPACA_DATA_HOST]
    assert transport.requests[0].url.path == PATH
    assert transport.params_for(PATH) == {
        "types": "cash_dividend",
        # Sorted, so the same universe is the same URL every day.
        "symbols": "AAGRY,AAPW,ABBV",
        "start": "2026-10-03",
        "end": "2027-04-08",
        "limit": "1000",
    }


async def test_the_request_is_billed_to_the_data_bucket_of_the_shared_limiter(
    make_provider: Any, limiter: HostRateLimiter
) -> None:
    data = limiter.bucket_for(ALPACA_DATA_HOST)
    trading = limiter.bucket_for(ALPACA_PAPER_TRADING_HOST)
    data_before, trading_before = data.available, trading.available
    await read(make_provider, BODY_RAW, TERMINAL)
    assert data_before - data.available == 2  # one token per page
    assert trading.available == trading_before


async def test_every_rate_is_the_exact_decimal_of_its_literal_text(
    make_provider: Any,
) -> None:
    result, _ = await read(make_provider, TERMINAL)
    literal = dict(
        re.findall(r'"id":"([0-9a-f-]+)",[^{}]*?"rate":([-0-9.eE+]+)', TERMINAL)
    )
    assert len(literal) == 6
    rows = by_id(result)
    assert set(rows) == set(literal)
    for vendor_id, text in literal.items():
        rate = rows[vendor_id].rate
        assert type(rate) is Decimal
        assert rate == Decimal(text)
        assert str(rate) == text  # not merely equal: the same digits
    assert result.skipped == ()


async def test_a_rate_not_representable_in_binary_survives_exactly(
    make_provider: Any,
) -> None:
    result, _ = await read(make_provider, TERMINAL)
    rate = by_id(result)["4a52283b-0fbf-4996-9e27-15f324115a5b"].rate
    assert rate == Decimal("0.150599")
    # The trap this guards: through a double, the same text is not this number.
    assert Decimal(float("0.150599")) != Decimal("0.150599")


async def test_an_integer_rate_is_an_exact_decimal(make_provider: Any) -> None:
    body = edited('"rate":1.73,', '"rate":2,')
    result, _ = await read(make_provider, body)
    rate = by_id(result)["ad029c18-25b0-4d0f-9cc4-25c4aa648302"].rate
    assert type(rate) is Decimal
    assert rate == Decimal(2)
    assert str(rate) == "2"


async def test_the_row_fields_are_read(make_provider: Any) -> None:
    result, _ = await read(make_provider, TERMINAL)
    abbv = by_id(result)["ad029c18-25b0-4d0f-9cc4-25c4aa648302"]
    assert abbv == CashDividend(
        vendor_id="ad029c18-25b0-4d0f-9cc4-25c4aa648302",
        symbol="ABBV",
        ex_date=date(2026, 10, 15),
        rate=Decimal("1.73"),
        special=False,
        sub_type=None,
        foreign=False,
    )
    assert by_id(result)["d5842311-bfd2-4fc9-bd1d-6ab79394e0e6"].foreign is True


async def test_special_and_sub_type_are_read(make_provider: Any) -> None:
    body = edited(
        '"special":false,"symbol":"ABBV"',
        '"special":true,"sub_type":"return_of_capital","symbol":"ABBV"',
    )
    result, _ = await read(make_provider, body)
    abbv = by_id(result)["ad029c18-25b0-4d0f-9cc4-25c4aa648302"]
    assert abbv.special is True
    assert abbv.sub_type == "return_of_capital"


async def test_pagination_follows_the_token_and_keeps_both_pages(
    make_provider: Any,
) -> None:
    # Page 2: page 1's ABBV row alone, re-keyed, behind a null token.
    abbv = re.search(r'\{[^{}]*"symbol":"ABBV"\}', TERMINAL)
    assert abbv is not None
    row = abbv.group(0).replace(
        "ad029c18-25b0-4d0f-9cc4-25c4aa648302", "00000000-0000-0000-0000-000000000002"
    )
    page2 = f'{{"corporate_actions":{{"cash_dividends":[{row}]}},"next_page_token":null}}'
    result, transport = await read(make_provider, BODY_RAW, page2)
    assert len(transport.requests) == 2
    assert "page_token" not in transport.requests[0].url.params
    assert transport.requests[1].url.params["page_token"] == PAGE_TOKEN
    # The second request is the first one plus the token, nothing else moved.
    first = dict(transport.requests[0].url.params)
    second = dict(transport.requests[1].url.params)
    second.pop("page_token")
    assert first == second
    assert len(result.dividends) == 7
    assert "00000000-0000-0000-0000-000000000002" in by_id(result)


@pytest.mark.parametrize(
    ("old", "new", "cause"),
    [
        ('"rate":0.25,', '"rate":"n/a",', "rate"),
        ('"rate":0.25,', "", "rate is missing"),
        ('"rate":0.25,', '"rate":null,', "rate is missing"),
        ('"rate":0.25,', '"rate":NaN,', "rate"),
        ('"rate":0.25,', '"rate":true,', "rate"),
        ('"rate":0.25,', '"rate":0,', "not positive"),
        ('"rate":0.25,', '"rate":-0.25,', "not positive"),
        ('"special":false,"symbol":"AAP"}', '"special":"no","symbol":"AAP"}', "special"),
        ('"ex_date":"2026-10-09"', '"ex_date":"2026-13-09"', "ex_date"),
    ],
)
async def test_an_unreadable_row_is_skipped_and_reported_never_coerced(
    make_provider: Any, old: str, new: str, cause: str
) -> None:
    body = edited(old, new)
    result, _ = await read(make_provider, body)
    aap = "3d300f69-484e-4c8d-bcb8-c6a10f5c46aa"
    assert aap not in by_id(result)
    assert len(result.dividends) == 5  # the other rows are unaffected
    [skipped] = result.skipped
    assert skipped.vendor_id == aap
    assert skipped.symbol == "AAP"
    assert cause in skipped.reason


async def test_a_skipped_row_keeps_its_ex_date_when_that_was_readable(
    make_provider: Any,
) -> None:
    result, _ = await read(make_provider, edited('"rate":0.25,', '"rate":"n/a",'))
    [skipped] = result.skipped
    assert skipped.ex_date == date(2026, 10, 9)


async def test_a_row_without_an_id_is_skipped(make_provider: Any) -> None:
    body = edited('"id":"3d300f69-484e-4c8d-bcb8-c6a10f5c46aa",', "")
    result, _ = await read(make_provider, body)
    [skipped] = result.skipped
    assert skipped.vendor_id is None
    assert "id" in skipped.reason


async def test_a_page_with_no_dividends_is_an_empty_read_not_an_error(
    make_provider: Any,
) -> None:
    for body in (
        '{"corporate_actions":{"cash_dividends":[]},"next_page_token":null}',
        # Asked for one type, a page with none may omit the key entirely.
        '{"corporate_actions":{},"next_page_token":null}',
    ):
        result, _ = await read(make_provider, body)
        assert result == CashDividendRead(dividends=(), skipped=())


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        '{"next_page_token":null}',
        '{"corporate_actions":[],"next_page_token":null}',
        '{"corporate_actions":{"cash_dividends":{}},"next_page_token":null}',
    ],
)
async def test_a_body_of_the_wrong_shape_is_a_provider_error(
    make_provider: Any, body: str
) -> None:
    with pytest.raises(ProviderError):
        await read(make_provider, body)


async def test_an_http_error_is_a_provider_error(make_provider: Any) -> None:
    provider, _ = make_provider(lambda _r: (500, '{"message":"internal"}'))
    with pytest.raises(ProviderError, match="500"):
        await provider.cash_dividends(symbols=SYMBOLS, start=START, end=END)


async def test_the_arguments_are_refused_before_any_request(make_provider: Any) -> None:
    provider, transport = make_provider(lambda _r: (200, TERMINAL))
    with pytest.raises(ValueError, match="symbol"):
        await provider.cash_dividends(symbols=(), start=START, end=END)
    with pytest.raises(ValueError, match="end"):
        await provider.cash_dividends(symbols=SYMBOLS, start=END, end=START)
    assert transport.requests == []
