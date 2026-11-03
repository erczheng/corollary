"""``AlpacaProvider.cusip_name_changes`` -- where a re-CUSIPed security went.

The SPDR builder follows an unresolved N-PORT CUSIP through the vendor's
``name_change`` corporate action (old CUSIP -> new CUSIP) and resolves the
successor by CUSIP (2026-11-02: Exxon and ONEOK were re-CUSIPed under their
own tickers, and the old CUSIPs named inactive asset records).

The 200 body is **recorded**:
``tests/fixtures/alpaca/p4_corporate_actions_name_change_xom_oke.json``, one
read-only GET on 2026-11-02, untrimmed. Every other body is synthesised here
and says so.
"""

import json
from datetime import date
from typing import Any

import pytest

from corollary.data.providers.interface import CusipChange, ProviderError, RateLimitedError
from corollary.ratelimit import ALPACA_DATA_HOST
from tests.data.providers.conftest import TEST_CREDENTIALS, load_fixture

pytestmark = pytest.mark.asyncio

XOM_OLD, XOM_NEW = "30231G102", "30233Q108"
OKE_OLD, OKE_NEW = "682680103", "30609A109"
START, END = date(2026, 6, 30), date(2026, 11, 2)
# SYNTHETIC, UUID-shaped like every recorded action id.
ACTION_ID = "00000000-0000-4000-8000-000000000001"


def recorded() -> tuple[int, str]:
    fixture = load_fixture("p4_corporate_actions_name_change_xom_oke")
    return (fixture["status_code"], json.dumps(fixture["body"]))


def literal(body: Any, status: int = 200) -> tuple[int, str]:
    return (status, body if isinstance(body, str) else json.dumps(body))


async def test_the_recorded_name_changes_are_read_from_one_data_host_request(make_provider):
    provider, transport = make_provider(lambda _r: recorded())

    changes = await provider.cusip_name_changes(XOM_OLD, start=START, end=END)

    # Everything the vendor answered, in its order: the rule that picks the
    # one whose old_cusip is the filed CUSIP lives in the builder, not here.
    assert changes == (
        CusipChange(
            action_id="f91ae982-5eaf-49bc-970a-d541f59b838d", old_cusip=OKE_OLD,
            new_cusip=OKE_NEW, old_symbol="OKE", new_symbol="OKE",
            process_date=date(2026, 9, 11),
        ),
        CusipChange(
            action_id="71daae23-2985-4549-870e-a6d678474b6b", old_cusip=XOM_OLD,
            new_cusip=XOM_NEW, old_symbol="XOM", new_symbol="XOM",
            process_date=date(2026, 7, 2),
        ),
    )
    (request,) = transport.requests
    assert request.method == "GET"
    assert request.url.host == ALPACA_DATA_HOST
    assert request.url.path == "/v1/corporate-actions"
    params = dict(request.url.params)
    assert params["cusips"] == XOM_OLD
    assert params["types"] == "name_change"
    assert (params["start"], params["end"]) == ("2026-06-30", "2026-11-02")


async def test_the_cusip_is_trimmed_and_upper_cased_before_the_request(make_provider):
    provider, transport = make_provider(lambda _r: recorded())
    await provider.cusip_name_changes(" 30231g102 ", start=START, end=END)
    assert dict(transport.requests[0].url.params)["cusips"] == XOM_OLD


@pytest.mark.parametrize("bad", ["", "XOM", "30231G10", "30231G10X", "000000000", "3023/G102"])
async def test_a_value_that_is_not_a_cusip_raises_before_any_request(make_provider, bad):
    provider, transport = make_provider(lambda _r: recorded())
    with pytest.raises(ValueError):
        await provider.cusip_name_changes(bad, start=START, end=END)
    assert transport.requests == []


async def test_an_end_before_the_start_raises_before_any_request(make_provider):
    provider, transport = make_provider(lambda _r: recorded())
    with pytest.raises(ValueError):
        await provider.cusip_name_changes(XOM_OLD, start=END, end=START)
    assert transport.requests == []


async def test_a_page_without_name_changes_is_no_rows(make_provider):
    # SYNTHETIC: asked for one type, an empty window may omit its key.
    provider, _ = make_provider(lambda _r: literal({"corporate_actions": {}, "next_page_token": None}))
    assert await provider.cusip_name_changes(XOM_OLD, start=START, end=END) == ()


@pytest.mark.parametrize(
    "row",
    [
        {"id": ACTION_ID, "old_cusip": XOM_OLD, "old_symbol": "XOM", "new_symbol": "XOM",
         "process_date": "2026-07-02"},  # no new_cusip
        {"id": ACTION_ID, "old_cusip": XOM_OLD, "new_cusip": "not-a-cusip", "old_symbol": "XOM",
         "new_symbol": "XOM", "process_date": "2026-07-02"},
        {"id": ACTION_ID, "old_cusip": 30231, "new_cusip": XOM_NEW, "old_symbol": "XOM",
         "new_symbol": "XOM", "process_date": "2026-07-02"},
        {"id": ACTION_ID, "old_cusip": XOM_OLD, "new_cusip": XOM_NEW, "old_symbol": "XOM",
         "new_symbol": "XOM", "process_date": "July 2"},
        {"id": ACTION_ID, "old_cusip": XOM_OLD, "new_cusip": XOM_NEW, "old_symbol": "XOM",
         "new_symbol": "XOM"},  # no process_date
        {"old_cusip": XOM_OLD, "new_cusip": XOM_NEW, "old_symbol": "XOM",
         "new_symbol": "XOM", "process_date": "2026-07-02"},  # no id
        # Rule 6 (audit 2026-11-02): the id and both symbols reach the
        # successor log line, so each is shape-checked rather than passed on
        # as free vendor text.
        {"id": "x", "old_cusip": XOM_OLD, "new_cusip": XOM_NEW, "old_symbol": "XOM",
         "new_symbol": "XOM", "process_date": "2026-07-02"},  # id not UUID-shaped
        {"id": ACTION_ID + "\nforged log line", "old_cusip": XOM_OLD, "new_cusip": XOM_NEW,
         "old_symbol": "XOM", "new_symbol": "XOM", "process_date": "2026-07-02"},
        {"id": "f91ae982-5eaf-49bc-970a-d541f59b838", "old_cusip": XOM_OLD,
         "new_cusip": XOM_NEW, "old_symbol": "XOM", "new_symbol": "XOM",
         "process_date": "2026-07-02"},  # one hex digit short
        {"id": 7, "old_cusip": XOM_OLD, "new_cusip": XOM_NEW, "old_symbol": "XOM",
         "new_symbol": "XOM", "process_date": "2026-07-02"},
        {"id": ACTION_ID, "old_cusip": XOM_OLD, "new_cusip": XOM_NEW,
         "old_symbol": "XOM\nforged log line", "new_symbol": "XOM",
         "process_date": "2026-07-02"},
        {"id": ACTION_ID, "old_cusip": XOM_OLD, "new_cusip": XOM_NEW, "old_symbol": "XOM",
         "new_symbol": "EXXONMOBIL", "process_date": "2026-07-02"},  # too long
        {"id": ACTION_ID, "old_cusip": XOM_OLD, "new_cusip": XOM_NEW, "old_symbol": "XOM",
         "new_symbol": "XOM1", "process_date": "2026-07-02"},
        {"id": ACTION_ID, "old_cusip": XOM_OLD, "new_cusip": XOM_NEW, "old_symbol": 42,
         "new_symbol": "XOM", "process_date": "2026-07-02"},
        "not a row",
    ],
)
async def test_an_unreadable_row_raises_rather_than_answering(make_provider, row):
    # SYNTHETIC rows. A name change is an identity claim: one the provider
    # cannot read is a vendor shape change, never "no successor".
    body = {"corporate_actions": {"name_changes": [row]}, "next_page_token": None}
    provider, _ = make_provider(lambda _r: literal(body))
    with pytest.raises(ProviderError):
        await provider.cusip_name_changes(XOM_OLD, start=START, end=END)


async def test_a_blank_symbol_is_read_as_empty_not_refused(make_provider):
    # SYNTHETIC: the builder refuses a successor whose ticker disagrees, so a
    # missing symbol must reach it as "" rather than abort the build.
    row = {"id": ACTION_ID, "old_cusip": XOM_OLD, "new_cusip": XOM_NEW,
           "process_date": "2026-07-02"}
    body = {"corporate_actions": {"name_changes": [row]}, "next_page_token": None}
    provider, _ = make_provider(lambda _r: literal(body))
    [change] = await provider.cusip_name_changes(XOM_OLD, start=START, end=END)
    assert (change.old_symbol, change.new_symbol) == ("", "")


async def test_a_class_share_symbol_and_an_upper_case_id_are_read(make_provider):
    # SYNTHETIC. The symbol check is the project's equity-symbol shape, not
    # "letters only": a class share is a symbol, and a UUID is case-free.
    row = {"id": ACTION_ID.upper(), "old_cusip": XOM_OLD, "new_cusip": XOM_NEW,
           "old_symbol": "BRK/B", "new_symbol": "BRK.B", "process_date": "2026-07-02"}
    body = {"corporate_actions": {"name_changes": [row]}, "next_page_token": None}
    provider, _ = make_provider(lambda _r: literal(body))
    [change] = await provider.cusip_name_changes(XOM_OLD, start=START, end=END)
    assert (change.old_symbol, change.new_symbol) == ("BRK.B", "BRK.B")
    assert change.action_id == ACTION_ID.upper()


@pytest.mark.parametrize("field", ["id", "old_symbol", "new_symbol"])
@pytest.mark.parametrize("half", ["key_id", "secret_key"])
async def test_a_refused_row_quotes_its_bad_field_only_through_the_redaction(
    make_provider, field, half
):
    # SYNTHETIC: a vendor echoing the request's own credential into a field.
    # Neither half is an id or a symbol, so the row is refused, and the
    # refusal -- an exception message, which is log output -- quotes it only
    # through the provider's redaction (rule 6).
    credential = getattr(TEST_CREDENTIALS, half)
    row = {"id": ACTION_ID, "old_cusip": XOM_OLD, "new_cusip": XOM_NEW, "old_symbol": "XOM",
           "new_symbol": "XOM", "process_date": "2026-07-02", field: credential}
    body = {"corporate_actions": {"name_changes": [row]}, "next_page_token": None}
    provider, _ = make_provider(lambda _r: literal(body))
    with pytest.raises(ProviderError) as raised:
        await provider.cusip_name_changes(XOM_OLD, start=START, end=END)
    assert field in str(raised.value)
    assert credential not in str(raised.value)


async def test_name_changes_that_are_not_a_list_raise(make_provider):
    body = {"corporate_actions": {"name_changes": {"id": "x"}}, "next_page_token": None}
    provider, _ = make_provider(lambda _r: literal(body))
    with pytest.raises(ProviderError):
        await provider.cusip_name_changes(XOM_OLD, start=START, end=END)


async def test_a_429_raises_rate_limited_for_the_builder_to_wait_out(make_provider):
    # SYNTHETIC body, in Alpaca's {code, message} error shape.
    provider, _ = make_provider(
        lambda _r: literal({"code": 42910000, "message": "rate limit exceeded"}, 429)
    )
    with pytest.raises(RateLimitedError):
        await provider.cusip_name_changes(XOM_OLD, start=START, end=END)


async def test_a_server_error_raises(make_provider):
    provider, _ = make_provider(lambda _r: literal("upstream unavailable", 503))
    with pytest.raises(ProviderError):
        await provider.cusip_name_changes(XOM_OLD, start=START, end=END)

