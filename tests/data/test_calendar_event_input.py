"""``CalendarEventInput``'s record-level refusals, and Q15's IPO-only fields.

Unit 7.2b-F. Two groups:

* **The audit's untested refusals** -- a float, int or bool for money; NaN
  and Infinity; an estimate on an economic release. Each case below fails if
  the check it names is removed from ``__post_init__``.
* **The IPO fields** (Q15) -- ``exchange``, ``shares``, ``price_low``,
  ``price_high``, ``ipo_status``: accepted on an ``ipo`` row, refused on any
  other kind, and ``price_low <= price_high`` checked here because ``Money``
  refuses SQL comparison and the CHECK therefore cannot.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from corollary.data.calendar_event import (
    CalendarEventInput,
    CalendarKind,
    CalendarSource,
    IpoStatus,
)


def _earnings(**overrides: Any) -> CalendarEventInput:
    base = CalendarEventInput(
        kind=CalendarKind.EARNINGS,
        source=CalendarSource.FINNHUB,
        vendor_id="NVDA:2026Q3",
        title="NVDA earnings",
        date=date(2026, 11, 19),
        ticker="NVDA",
        estimate=Decimal("1.25"),
    )
    return replace(base, **overrides)


def _economic(**overrides: Any) -> CalendarEventInput:
    base = CalendarEventInput(
        kind=CalendarKind.ECONOMIC,
        source=CalendarSource.FRED,
        vendor_id="CPI:2026-10-15",
        title="CPI",
        date=date(2026, 10, 15),
        prior=Decimal("3.1"),
        unit="Percent",
    )
    return replace(base, **overrides)


def _ipo(**overrides: Any) -> CalendarEventInput:
    base = CalendarEventInput(
        kind=CalendarKind.IPO,
        source=CalendarSource.FINNHUB,
        vendor_id="symbol:IAM",
        title="Iambic Therapeutics, Inc.",
        date=date(2026, 10, 15),
        ticker="IAM",
        exchange="NASDAQ Global Select",
        shares=9375000,
        price_low=Decimal("15.00"),
        price_high=Decimal("17.00"),
        ipo_status=IpoStatus.EXPECTED,
    )
    return replace(base, **overrides)


# --- the audit's record-level refusals ---------------------------------------


@pytest.mark.parametrize("field", ["estimate", "prior", "actual"])
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(1.25, id="float"),
        pytest.param(1, id="int"),
        pytest.param(True, id="bool"),
        pytest.param("1.25", id="str"),
    ],
)
def test_money_that_is_not_a_decimal_is_refused(field: str, bad: object) -> None:
    with pytest.raises(ValueError, match=f"{field} must be a Decimal"):
        _earnings(**{field: bad})


@pytest.mark.parametrize("field", ["estimate", "prior", "actual"])
@pytest.mark.parametrize(
    "bad", ["NaN", "sNaN", "Infinity", "-Infinity"], ids=lambda text: text
)
def test_non_finite_money_is_refused(field: str, bad: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be a finite number"):
        _earnings(**{field: Decimal(bad)})


def test_an_economic_release_refuses_an_estimate() -> None:
    _economic()  # the baseline is valid
    with pytest.raises(ValueError, match="consensus is unavailable"):
        _economic(estimate=Decimal("3.0"))


def test_an_economic_release_keeps_its_prior_and_actual() -> None:
    event = _economic(actual=Decimal("3.2"))
    assert (event.prior, event.actual, event.estimate) == (Decimal("3.1"), Decimal("3.2"), None)


# --- the IPO fields ------------------------------------------------------------


def test_a_full_ipo_record_is_accepted() -> None:
    event = _ipo()
    assert event.price_low == Decimal("15.00") and event.price_high == Decimal("17.00")
    assert event.shares == 9375000 and event.ipo_status is IpoStatus.EXPECTED


def test_an_ipo_with_no_symbol_and_nothing_optional_is_accepted() -> None:
    event = _ipo(
        ticker=None,
        vendor_id="name:siyata ptt",
        title="SIYATA PTT",
        exchange=None,
        shares=None,
        price_low=None,
        price_high=None,
        ipo_status=IpoStatus.WITHDRAWN,
    )
    assert event.ticker is None


def test_a_single_price_is_low_equal_to_high() -> None:
    # The boundary of ``price_low <= price_high``: equal is permitted.
    event = _ipo(price_low=Decimal("10.00"), price_high=Decimal("10.00"))
    assert event.price_low == event.price_high


def test_the_status_values_are_finnhubs() -> None:
    assert [s.value for s in IpoStatus] == ["expected", "filed", "priced", "withdrawn"]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"exchange": "NYSE"}, id="exchange"),
        pytest.param({"shares": 100}, id="shares"),
        pytest.param({"price_low": Decimal("1"), "price_high": Decimal("1")}, id="price"),
        pytest.param({"ipo_status": IpoStatus.FILED}, id="ipo_status"),
    ],
)
def test_an_ipo_field_on_another_kind_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="only ipo"):
        _earnings(**overrides)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        pytest.param(
            {"price_low": Decimal("17.00"), "price_high": Decimal("15.00")},
            "above price_high",
            id="low above high",
        ),
        pytest.param({"price_low": None}, "both or neither", id="high without low"),
        pytest.param({"price_high": None}, "both or neither", id="low without high"),
        pytest.param({"price_low": 15.0}, "price_low must be a Decimal", id="float price"),
        pytest.param(
            {"price_high": Decimal("NaN")}, "price_high must be a finite", id="NaN price"
        ),
        pytest.param(
            {"price_low": Decimal("0"), "price_high": Decimal("1")},
            "must be positive",
            id="zero price",
        ),
        pytest.param({"shares": 0}, "positive integer", id="zero shares"),
        pytest.param({"shares": -5}, "positive integer", id="negative shares"),
        pytest.param({"shares": True}, "positive integer", id="bool shares"),
        pytest.param({"shares": Decimal("9375000")}, "positive integer", id="Decimal shares"),
        pytest.param({"exchange": ""}, "exchange", id="blank exchange"),
        pytest.param({"exchange": "   "}, "exchange", id="whitespace exchange"),
        pytest.param({"exchange": "X" * 65}, "exchange", id="exchange too long"),
        pytest.param({"ipo_status": "expected"}, "IpoStatus", id="raw status string"),
    ],
)
def test_a_bad_ipo_field_is_refused(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _ipo(**overrides)


# --- column widths: refused at construction, never at flush (unit 7.2c-1) -----

# The Money CHECK is ``length(text) BETWEEN 1 AND 40`` and Money binds with
# ``format(value, "f")``; SQLite INTEGER is signed 64-bit.
_FORTY = Decimal("1" * 40)  # 40 characters as "f"
_FORTY_ONE = Decimal("1" * 41)


@pytest.mark.parametrize("field", ["estimate", "prior", "actual"])
def test_a_money_value_of_forty_characters_is_accepted(field: str) -> None:
    make = _economic if field != "estimate" else _earnings
    event = make(**{field: _FORTY})
    assert getattr(event, field) == _FORTY
    assert len(format(-Decimal("1" * 39), "f")) == 40
    make(**{field: -Decimal("1" * 39)})  # the sign counts, and 40 still fits


@pytest.mark.parametrize(
    "value",
    [_FORTY_ONE, Decimal("1e50"), Decimal("1e-45"), -Decimal("1" * 40)],
    ids=["41-digits", "1e50", "1e-45", "negative-41-chars"],
)
@pytest.mark.parametrize("field", ["estimate", "prior", "actual"])
def test_a_money_value_too_wide_for_its_column_is_refused(field: str, value: Decimal) -> None:
    make = _economic if field != "estimate" else _earnings
    with pytest.raises(ValueError, match="40"):
        make(**{field: value})


@pytest.mark.parametrize("field", ["price_low", "price_high"])
def test_an_offer_price_too_wide_for_its_column_is_refused(field: str) -> None:
    wide = Decimal("1e50")
    with pytest.raises(ValueError, match="40"):
        _ipo(price_low=Decimal("1") if field == "price_high" else wide,
             price_high=wide)


def test_shares_at_the_sqlite_integer_ceiling_is_accepted() -> None:
    assert _ipo(shares=2**63 - 1).shares == 2**63 - 1


def test_shares_past_the_sqlite_integer_ceiling_is_refused() -> None:
    with pytest.raises(ValueError, match="64-bit"):
        _ipo(shares=2**63)


def test_the_money_width_matches_the_column_check() -> None:
    from corollary.data.calendar_event import MONEY_TEXT_MAX
    from corollary.db import models

    assert MONEY_TEXT_MAX == models._MONEY_TEXT_WIDTH
