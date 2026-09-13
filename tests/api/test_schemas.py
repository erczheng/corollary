"""The money serializer and the camelCase wire form.

These two are the whole of the API contract that a route cannot get wrong by
itself: a lossy money serializer is wrong on every endpoint at once, and a
mis-generated alias is a column that renders empty with nothing raising.
"""

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from corollary.api import schemas
from corollary.api.schemas import (
    AccountMode,
    ActivityItem,
    ApiModel,
    EngineStateResponse,
    JsonMoney,
    Page,
    Position,
    PricePoint,
)
from corollary.db.models import ACCOUNT_MODES


class MoneyProbe(ApiModel):
    """A one-field model, so the serializer is measured and not the schema."""

    amount: JsonMoney


#: The figures on the real paper account, and the one realized round trip:
#: cash 96886.08, equity 99728.08, options buying power 69886.08, and the IWM
#: 280P bought at 8.21 and sold at 8.14 for -7.00 at a 100 multiplier. If the
#: serializer is ever lossy these are the numbers it will be lossy about.
ACCOUNT_FIGURES = ["96886.08", "99728.08", "69886.08", "-7.00", "8.21", "8.14"]


@pytest.mark.parametrize("raw", ACCOUNT_FIGURES)
def test_money_round_trips_exactly(raw: str) -> None:
    payload = json.loads(MoneyProbe(amount=Decimal(raw)).model_dump_json())

    assert Decimal(str(payload["amount"])) == Decimal(raw)


@pytest.mark.parametrize("raw", ACCOUNT_FIGURES)
def test_money_is_a_json_number_not_a_string(raw: str) -> None:
    """The spec's reinterpretation is a *number*, not a quoted decimal.

    Pydantic serializes a bare ``Decimal`` to a JSON string by default, which
    would typecheck against nothing on the frontend and render as ``NaN``
    through ``format.ts``.
    """
    payload = json.loads(MoneyProbe(amount=Decimal(raw)).model_dump_json())

    assert isinstance(payload["amount"], float), payload


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("96886.08", '{"amount":96886.08}'),
        ("99728.08", '{"amount":99728.08}'),
        ("69886.08", '{"amount":69886.08}'),
        ("8.21", '{"amount":8.21}'),
        ("8.14", '{"amount":8.14}'),
        # -7.00 loses its trailing zero, which is a *display* difference and
        # not a value one. Pinned so the shortening is a decision on the
        # record rather than something noticed in a screenshot.
        ("-7.00", '{"amount":-7.0}'),
    ],
)
def test_money_emits_the_shortest_exact_representation(raw: str, expected: str) -> None:
    assert MoneyProbe(amount=Decimal(raw)).model_dump_json() == expected


def test_python_mode_keeps_the_decimal() -> None:
    """Only the wire is a float. A server-side reader still gets the Decimal."""
    dumped = MoneyProbe(amount=Decimal("96886.08")).model_dump()

    assert dumped == {"amount": Decimal("96886.08")}
    assert isinstance(dumped["amount"], Decimal)


def test_a_lossy_conversion_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Nineteen significant digits do not fit a double, and must not be silent."""
    lossy = Decimal("1234567890123456789.01")

    with caplog.at_level(logging.WARNING, logger=schemas.__name__):
        MoneyProbe(amount=lossy).model_dump_json()

    assert any(
        record.__dict__.get("event") == "money_serialization_lossy"
        for record in caplog.records
    ), caplog.records


def test_an_exact_conversion_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=schemas.__name__):
        for raw in ACCOUNT_FIGURES:
            MoneyProbe(amount=Decimal(raw)).model_dump_json()

    assert caplog.records == []


def test_no_request_model_accepts_a_float_as_money() -> None:
    """The float exists on the way out and has no way back in.

    ``JsonMoney`` is a ``PlainSerializer`` only; validation is still
    ``Decimal``'s. A float arriving in a request body is a rejection rather
    than a quiet conversion, which is what keeps the sanctioned float
    confined to serialization.
    """
    with pytest.raises(ValueError):
        MoneyProbe.model_validate({"amount": 96886.08}, strict=True)


# --------------------------------------------------------------------------
# Aliasing
# --------------------------------------------------------------------------


def test_snake_case_fields_go_out_as_camel_case() -> None:
    state = EngineStateResponse(
        halted=True,
        halted_reason="opening snapshot has not succeeded",
        halted_at=datetime(2026, 9, 11, 13, 37, tzinfo=timezone.utc),
        t0=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
    )

    payload = json.loads(state.model_dump_json())

    assert set(payload) == {"halted", "haltedReason", "haltedAt", "t0"}


def test_construction_still_uses_python_names() -> None:
    """``populate_by_name`` -- routes build these in snake_case."""
    point = PricePoint(date=date(2026, 9, 11), value=Decimal("99728.08"))

    assert json.loads(point.model_dump_json()) == {
        "date": "2026-09-11",
        "value": 99728.08,
    }


def test_a_calendar_date_serializes_without_a_time() -> None:
    """An expiry is a day, not an instant.

    ``formatExpiry`` parses a bare ``YYYY-MM-DD`` as UTC midnight on purpose.
    Sending ``2026-11-21T00:00:00Z`` instead would render as Nov 20 in
    Eastern, which is the off-by-one the frontend documents at length.
    """
    payload = json.loads(
        PricePoint(date=date(2026, 11, 21), value=Decimal("1")).model_dump_json()
    )

    assert payload["date"] == "2026-11-21"


def test_page_is_generic_over_its_items() -> None:
    page: Page[PricePoint] = Page(
        items=[PricePoint(date=date(2026, 9, 11), value=Decimal("1.50"))],
        total=41,
        page=0,
        page_size=1,
        has_more=True,
    )

    assert json.loads(page.model_dump_json()) == {
        "items": [{"date": "2026-09-11", "value": 1.5}],
        "total": 41,
        "page": 0,
        "pageSize": 1,
        "hasMore": True,
    }


def test_account_mode_matches_the_database_spelling() -> None:
    """One vocabulary. ``fill.account`` stores exactly what the query says."""
    assert tuple(mode.value for mode in AccountMode) == ACCOUNT_MODES


def test_the_fields_with_no_alpaca_source_are_nullable() -> None:
    """Absent, never invented. Design spec, *Multi-leg grouping*."""
    for name in ("strategy_id", "managed_exit", "attached_exit"):
        assert type(None) in _optional_members(Position, name), name


def test_opened_by_strategy_id_is_nullable_against_the_ts_type() -> None:
    """Flagged in the module docstring; pinned here so it cannot drift back."""
    assert type(None) in _optional_members(Position, "opened_by_strategy_id")


def test_rejection_reason_defaults_to_none() -> None:
    """``types.ts`` has it optional; this model always emits the key."""
    item = ActivityItem(
        id="20260910131125598::68cda3e9",
        time=datetime(2026, 9, 10, 17, 11, 25, tzinfo=timezone.utc),
        contract="IWM 280P",
        action="BTO",
        price=Decimal("8.21"),
        quantity=1,
        pnl=None,
        pnl_pct=None,
        amount=None,
        status="filled",
    )

    assert json.loads(item.model_dump_json())["rejectionReason"] is None


def _optional_members(model: type[ApiModel], field: str) -> tuple[object, ...]:
    import typing

    annotation = model.model_fields[field].annotation
    return tuple(typing.get_args(annotation))
