"""``AlpacaBroker.positions()`` against the recorded paper account.

One row per contract, with no grouping: the recording holds **13 rows** across
nine logical positions -- four verticals contributing two each and five
singles contributing one. Reconstructing the logical position is step 6's job
and deliberately not this layer's; what this layer must not do is tidy the
broker's numbers on the way in.

The number it must not tidy is the sign. A short leg reports ``qty: "-1"``
*alongside* ``side: "short"``, with ``cost_basis`` and ``market_value`` both
negative. That is correct and load-bearing: a credit is a liability, which is
the same rule ``orders.ts`` states as a short's ``openUnitValue`` being
negative. Take the absolute value anywhere on this path and every credit
spread's payoff curve inverts.
"""

from decimal import Decimal

import pytest

from corollary.engine.execution.interface import BrokerError, PositionSide

from .conftest import fixture_body_bytes, load_fixture, single


@pytest.mark.asyncio
async def test_every_recorded_row_parses(make_broker) -> None:
    broker, transport = make_broker(single("positions"))
    positions = await broker.positions()
    assert len(positions) == len(load_fixture("positions")["body"])
    assert transport.paths == ["/v2/positions"]


@pytest.mark.asyncio
async def test_one_row_per_contract_not_per_logical_position(make_broker) -> None:
    """Thirteen rows, and every one of them a distinct OCC symbol."""
    broker, _ = make_broker(single("positions"))
    positions = await broker.positions()
    symbols = [position.symbol for position in positions]
    assert len(symbols) == 13
    assert len(set(symbols)) == 13


@pytest.mark.asyncio
async def test_a_short_keeps_its_negative_quantity_and_basis(make_broker) -> None:
    broker, _ = make_broker(single("positions"))
    shorts = [p for p in await broker.positions() if p.side is PositionSide.SHORT]

    assert shorts, "the recording holds short legs"
    for short in shorts:
        assert short.quantity < 0, short.symbol
        assert short.cost_basis < 0, short.symbol
        assert short.market_value < 0, short.symbol
        assert short.is_short is True


@pytest.mark.asyncio
async def test_a_long_keeps_its_positive_quantity_and_basis(make_broker) -> None:
    broker, _ = make_broker(single("positions"))
    longs = [p for p in await broker.positions() if p.side is PositionSide.LONG]

    assert longs
    for long in longs:
        assert long.quantity > 0, long.symbol
        assert long.cost_basis > 0, long.symbol
        assert long.is_short is False


@pytest.mark.asyncio
async def test_the_amd_short_put_matches_the_wire_exactly(make_broker) -> None:
    """A named row, so a sign flip cannot hide behind a passing aggregate."""
    broker, _ = make_broker(single("positions"))
    by_symbol = {p.symbol: p for p in await broker.positions()}
    amd = by_symbol["AMD270115P00470000"]

    raw = {
        row["symbol"]: row for row in load_fixture("positions")["body"]
    }["AMD270115P00470000"]

    assert amd.quantity == Decimal(raw["qty"])
    assert amd.cost_basis == Decimal(raw["cost_basis"])
    assert amd.market_value == Decimal(raw["market_value"])
    assert amd.average_entry_price == Decimal(raw["avg_entry_price"])
    assert amd.side is PositionSide.SHORT


@pytest.mark.asyncio
async def test_every_price_is_an_exact_decimal(make_broker) -> None:
    broker, _ = make_broker(single("positions"))
    for position in await broker.positions():
        for field in (
            "quantity",
            "average_entry_price",
            "cost_basis",
            "market_value",
        ):
            value = getattr(position, field)
            assert isinstance(value, Decimal), f"{position.symbol}.{field}"


@pytest.mark.asyncio
async def test_exchange_is_empty_on_an_option_and_that_survives(make_broker) -> None:
    """Present but empty. An empty string is not the same as absent."""
    broker, _ = make_broker(single("positions"))
    positions = await broker.positions()
    assert all(p.exchange == "" for p in positions)
    assert all(p.asset_class == "us_option" for p in positions)


@pytest.mark.asyncio
async def test_asset_marginable_survives(make_broker) -> None:
    broker, _ = make_broker(single("positions"))
    assert all(p.asset_marginable is True for p in await broker.positions())


@pytest.mark.asyncio
async def test_a_sign_that_disagrees_with_side_is_rejected(make_broker) -> None:
    """The one place this layer refuses to pass the vendor through.

    ``qty`` and ``side`` encode the same fact twice. If they ever disagree,
    one of them is wrong and there is no way to tell which -- and either
    choice mis-states the risk class of the position. So it raises rather than
    picking, and the rejection names the rule and the inputs.
    """
    body = fixture_body_bytes("positions").decode("utf-8")
    broken = body.replace('"qty": "-1"', '"qty": "1"', 1)
    assert broken != body, "the fixture changed"

    broker, _ = make_broker(lambda _request: (200, broken))
    with pytest.raises(BrokerError) as raised:
        await broker.positions()

    message = str(raised.value)
    assert "side" in message
    assert "qty" in message or "quantity" in message


@pytest.mark.asyncio
async def test_no_positions_is_an_empty_list_not_an_error(make_broker) -> None:
    broker, _ = make_broker(lambda _request: (200, "[]"))
    assert await broker.positions() == []
