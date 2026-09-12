"""``AlpacaBroker.account()`` against the recorded paper account.

Three things on this object are traps, and each has a test because each one
produces a plausible wrong number rather than an error:

* **``options_approved_level`` and ``options_trading_level`` are integers.**
  Every *other* numeric field is a string, so a parser that maps the whole
  object through ``Decimal(str)`` uniformly breaks on exactly these two -- and
  a parser that reads them as money would report an options level of
  ``Decimal('3')``.
* **``pending_transfer_in`` and ``pending_transfer_out`` are absent.** Nothing
  reads them, so nothing breaks -- unless they are modelled as required, which
  is how an absent field becomes a 500 on every account request.
* **``multiplier`` is ``'4'``, not ``'2'``.** PRD §8.6 asserts *"Paper is a
  margin account at 2x cash"* and the broker reports otherwise, which is wrong
  in the direction that overstates capacity. The number comes from the account
  object; nothing here asserts a constant.
"""

from decimal import Decimal

import pytest

from corollary.engine.execution.interface import MarginClass

from .conftest import load_fixture, single


@pytest.mark.asyncio
async def test_account_money_is_exact_decimal(make_broker) -> None:
    broker, _ = make_broker(single("account"))
    account = await broker.account()
    raw = load_fixture("account")["body"]

    for field, wire in (
        ("cash", "cash"),
        ("equity", "equity"),
        ("last_equity", "last_equity"),
        ("buying_power", "buying_power"),
        ("options_buying_power", "options_buying_power"),
        ("long_market_value", "long_market_value"),
        ("short_market_value", "short_market_value"),
        ("position_market_value", "position_market_value"),
        ("portfolio_value", "portfolio_value"),
        ("initial_margin", "initial_margin"),
        ("maintenance_margin", "maintenance_margin"),
        ("sma", "sma"),
        ("accrued_fees", "accrued_fees"),
    ):
        value = getattr(account, field)
        assert isinstance(value, Decimal), f"{field} is {type(value).__name__}"
        assert value == Decimal(raw[wire]), field


@pytest.mark.asyncio
async def test_a_short_market_value_stays_negative(make_broker) -> None:
    """A credit is a liability, and the broker says so in its own numbers."""
    broker, _ = make_broker(single("account"))
    account = await broker.account()
    assert account.short_market_value < 0


@pytest.mark.asyncio
async def test_the_options_level_is_an_int_read_from_the_account(make_broker) -> None:
    """Read, never hardcoded -- and an ``int``, not a ``Decimal``.

    CLAUDE.md states the account level as 3 in prose. That is a fact about
    today's account, not about the type, and the one place it may come from is
    the account object.
    """
    broker, _ = make_broker(single("account"))
    account = await broker.account()
    raw = load_fixture("account")["body"]

    assert isinstance(raw["options_trading_level"], int), "the fixture changed"
    assert account.options_trading_level == raw["options_trading_level"]
    assert account.options_approved_level == raw["options_approved_level"]
    assert type(account.options_trading_level) is int
    assert type(account.options_approved_level) is int


@pytest.mark.asyncio
async def test_the_margin_class_comes_from_multiplier_not_from_prose(
    make_broker,
) -> None:
    """``multiplier: '4'`` is a PDT margin account, not the PRD's 2x."""
    broker, _ = make_broker(single("account"))
    account = await broker.account()

    assert account.multiplier == Decimal(load_fixture("account")["body"]["multiplier"])
    assert account.margin_class is MarginClass.PATTERN_DAY_TRADER
    assert account.multiplier != Decimal(2), (
        "this account reports 4x; the page must read the field, not the PRD"
    )


@pytest.mark.parametrize(
    "multiplier, expected",
    [
        ("1", MarginClass.CASH),
        ("2", MarginClass.REG_T),
        ("4", MarginClass.PATTERN_DAY_TRADER),
        ("7", MarginClass.UNKNOWN),
    ],
)
def test_every_multiplier_maps_to_a_named_class(multiplier, expected) -> None:
    """Including the one that does not: an unexpected multiplier is not 2x."""
    assert MarginClass.for_multiplier(Decimal(multiplier)) is expected


@pytest.mark.asyncio
async def test_an_absent_pending_transfer_field_is_not_required(make_broker) -> None:
    """The parse succeeding is the test, and the model not naming them is too.

    ``pending_transfer_in``/``out`` are absent from this account object. A
    model that required them would raise here; a model that merely *named*
    them would invite a caller to read a value the broker never sends.
    """
    raw = load_fixture("account")["body"]
    assert "pending_transfer_in" not in raw, "the fixture changed"
    assert "pending_transfer_out" not in raw, "the fixture changed"

    broker, _ = make_broker(single("account"))
    account = await broker.account()

    for absent in ("pending_transfer_in", "pending_transfer_out"):
        assert not hasattr(account, absent), (
            f"{absent} is not a field Alpaca sends on this plan"
        )


@pytest.mark.asyncio
async def test_the_day_change_comes_from_last_equity(make_broker) -> None:
    """``last_equity`` is equity at the previous close -- a day change free."""
    broker, _ = make_broker(single("account"))
    account = await broker.account()
    assert account.day_change == account.equity - account.last_equity
    assert isinstance(account.day_change, Decimal)


@pytest.mark.asyncio
async def test_the_account_carries_no_identifier(make_broker) -> None:
    """Rule 6, at the type level.

    The account number is the one genuinely sensitive field on any of these
    responses, and it is never needed downstream -- the API serves one
    account. Not modelling it is what stops it reaching a log line.
    """
    broker, _ = make_broker(single("account"))
    account = await broker.account()
    for identifier in ("id", "account_id", "account_number"):
        assert not hasattr(account, identifier)


@pytest.mark.asyncio
async def test_balance_asof_is_a_date_not_an_instant(make_broker) -> None:
    from datetime import date

    broker, _ = make_broker(single("account"))
    account = await broker.account()
    assert account.balance_asof == date(2026, 9, 10)


@pytest.mark.asyncio
async def test_the_blocked_flags_are_bools(make_broker) -> None:
    """Read by the engine before it ever considers trading."""
    broker, _ = make_broker(single("account"))
    account = await broker.account()
    assert account.trading_blocked is False
    assert account.account_blocked is False
    assert account.transfers_blocked is False
    assert account.shorting_enabled is True
    assert account.status == "ACTIVE"
