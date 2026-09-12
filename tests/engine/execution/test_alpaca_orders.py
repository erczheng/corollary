"""``AlpacaBroker.orders()`` -- and the two-hop join the read surface exists for.

A fill carries its **leg's** order id. For a simple order the leg id and the
parent id coincide, which is what makes the mistake invisible; for an ``mleg``
order each leg is a full order object with its own ``id`` and the parent id
appears nowhere on the fill. Reaching the parent therefore needs
``?nested=true`` and a leg-id to parent-id map built from ``legs[]``.

A one-hop join groups nothing, silently. The negative control below proves
that by stripping ``legs[]`` off the recorded parents and watching eight of
fifteen fills become unreachable -- the same eight that carry the spreads.

Intent comes from the order, never from ``side``. On the activities endpoint
``side`` takes three values (``buy`` 9, ``sell_short`` 5, ``sell`` 1 in this
recording), and ``buy`` is *both* buy-to-open and buy-to-close. Two of the
four actions are indistinguishable without the join, which is what makes the
join mandatory rather than convenient.
"""

from decimal import Decimal

import pytest

from corollary.engine.execution.interface import (
    FillSide,
    OrderClass,
    OrderSide,
    PositionIntent,
)

from .conftest import load_fixture, single


def _leg_parents(orders) -> dict[str, str]:
    """The map step 6 builds. Four lines, and the whole of the second hop."""
    return {
        leg_id: order.id for order in orders for leg_id in order.leg_ids
    }


@pytest.mark.asyncio
async def test_nested_true_is_actually_sent(make_broker) -> None:
    """The parameter whose absence is invisible in the parsed result."""
    broker, transport = make_broker(single("orders_nested"))
    await broker.orders()
    params = transport.params_for("/v2/orders")
    assert params["nested"] == "true"
    assert params["status"] == "all"


@pytest.mark.asyncio
async def test_the_recorded_history_parses(make_broker) -> None:
    broker, _ = make_broker(single("orders_nested"))
    orders = await broker.orders()
    assert len(orders) == len(load_fixture("orders_nested")["body"]) == 11


@pytest.mark.asyncio
async def test_four_mleg_parents_each_carry_two_legs(make_broker) -> None:
    broker, _ = make_broker(single("orders_nested"))
    orders = await broker.orders()
    mlegs = [order for order in orders if order.order_class is OrderClass.MLEG]

    assert len(mlegs) == 4
    for parent in mlegs:
        assert parent.is_multi_leg is True
        assert len(parent.legs) == 2
        assert all(leg.legs == () for leg in parent.legs), "legs do not nest twice"


@pytest.mark.asyncio
async def test_an_mleg_parent_has_no_symbol_side_or_intent(make_broker) -> None:
    """The parent is the *structure*; the legs are the instruments.

    Alpaca sends ``symbol: ""``, ``side: ""`` and ``asset_class: ""`` on the
    parent, and no ``position_intent`` at all. Reading the parent's ``side``
    as an action would read an empty string.
    """
    broker, _ = make_broker(single("orders_nested"))
    parent = next(
        order
        for order in await broker.orders()
        if order.order_class is OrderClass.MLEG
    )
    assert parent.symbol == ""
    assert parent.side is None
    assert parent.position_intent is None
    assert parent.legs[0].symbol != ""


@pytest.mark.asyncio
async def test_an_mleg_net_price_is_signed_and_a_credit_is_negative(
    make_broker,
) -> None:
    """``filled_avg_price: "-2.01"`` on the parent. Direction, for free.

    The spec's grouping rule reads direction off the order's net price --
    debit long, credit short -- so the sign is the fact, not a presentation
    choice.
    """
    broker, _ = make_broker(single("orders_nested"))
    mlegs = [
        order
        for order in await broker.orders()
        if order.order_class is OrderClass.MLEG
    ]
    credits = [order for order in mlegs if order.is_credit]

    assert len(credits) == 4, "all four recorded verticals were sold for a credit"
    for order in credits:
        assert order.net_price is not None
        assert order.net_price < 0
        assert isinstance(order.net_price, Decimal)


@pytest.mark.asyncio
async def test_a_leg_carries_the_position_intent(make_broker) -> None:
    """Where the four-way action actually lives."""
    broker, _ = make_broker(single("orders_nested"))
    parent = next(
        order
        for order in await broker.orders()
        if order.order_class is OrderClass.MLEG
    )
    intents = {leg.position_intent for leg in parent.legs}
    assert intents == {PositionIntent.SELL_TO_OPEN, PositionIntent.BUY_TO_OPEN}
    for leg in parent.legs:
        assert leg.ratio_qty == Decimal(1)
        assert leg.side in (OrderSide.BUY, OrderSide.SELL)


@pytest.mark.asyncio
async def test_leg_ids_are_disjoint_from_parent_ids(make_broker) -> None:
    """If they overlapped, the second hop would be ambiguous."""
    broker, _ = make_broker(single("orders_nested"))
    orders = await broker.orders()
    parents = {order.id for order in orders}
    legs = set(_leg_parents(orders))

    assert len(legs) == 8
    assert not (parents & legs)


@pytest.mark.asyncio
async def test_every_recorded_fill_reaches_a_parent_order(make_broker) -> None:
    """The two-hop join, end to end, on real data."""
    broker, _ = make_broker(
        lambda request: (
            "orders_nested" if "/v2/orders" in request.url.path else "activities_fill"
        )
    )
    orders = await broker.orders()
    fills = await broker.activities()

    parents = {order.id: order for order in orders}
    leg_parents = _leg_parents(orders)

    unreachable = [
        fill.symbol
        for fill in fills
        if fill.order_id not in parents and fill.order_id not in leg_parents
    ]
    assert unreachable == []
    assert len(fills) == 15


@pytest.mark.asyncio
async def test_a_one_hop_join_silently_groups_nothing(make_broker) -> None:
    """The negative control, and the reason ``nested=true`` is not optional.

    Strip ``legs[]`` off the parents -- which is what a request without
    ``nested`` is documented to return -- and the eight fills belonging to the
    four spreads have no route to a parent at all. Nothing raises. The join
    simply returns fewer groups than there are, which on screen is a book with
    no spreads in it.
    """
    broker, _ = make_broker(
        lambda request: (
            "orders_nested" if "/v2/orders" in request.url.path else "activities_fill"
        )
    )
    orders = await broker.orders()
    fills = await broker.activities()

    one_hop = {order.id for order in orders}
    orphans = [fill for fill in fills if fill.order_id not in one_hop]

    assert len(orphans) == 8, "the four two-leg spreads, silently ungrouped"
    assert len(fills) - len(orphans) == 7, "only the single-leg orders survive"


@pytest.mark.asyncio
async def test_side_alone_cannot_name_the_action(make_broker) -> None:
    """``sell_short`` and ``sell`` are decidable; ``buy`` is not.

    This is the whole argument for the join stated as a type: two of the four
    actions collapse onto one value of ``side``, so a ledger reading ``side``
    would book every buy-to-close as an opening lot and double the position.
    """
    assert FillSide.SELL_SHORT.implied_intent is PositionIntent.SELL_TO_OPEN
    assert FillSide.SELL.implied_intent is PositionIntent.SELL_TO_CLOSE
    assert FillSide.BUY.implied_intent is None

    broker, _ = make_broker(single("activities_fill"))
    sides = [fill.side for fill in await broker.activities()]
    assert set(sides) == {FillSide.BUY, FillSide.SELL, FillSide.SELL_SHORT}, (
        "side takes three values on this endpoint, not two"
    )
    assert sides.count(FillSide.BUY) == 9
    assert sides.count(FillSide.SELL_SHORT) == 5
    assert sides.count(FillSide.SELL) == 1


@pytest.mark.parametrize("limit", [0, -1, 501])
@pytest.mark.asyncio
async def test_a_limit_outside_the_documented_range_is_refused(
    make_broker, limit
) -> None:
    """Alpaca's ceiling is 500, and its silent default of 50 is the real trap.

    A truncated order history is a two-hop join missing its parents, which on
    screen is a book with no spreads in it rather than an error. So the limit
    is always stated, and a value the endpoint would reject is refused here.
    """
    broker, transport = make_broker(single("orders_nested"))
    with pytest.raises(ValueError):
        await broker.orders(limit=limit)
    assert transport.requests == []


@pytest.mark.asyncio
async def test_a_naive_window_bound_is_refused(make_broker) -> None:
    from datetime import datetime

    broker, transport = make_broker(single("orders_nested"))
    with pytest.raises(ValueError):
        await broker.orders(after=datetime(2026, 9, 10, 12, 0, 0))
    assert transport.requests == []


@pytest.mark.asyncio
async def test_an_empty_order_list_is_not_an_error(make_broker) -> None:
    """The recorded ``status=open`` response: everything has filled."""
    broker, _ = make_broker(single("orders_open"))
    assert await broker.orders() == []
    assert load_fixture("orders_open")["body"] == []


@pytest.mark.asyncio
async def test_an_empty_order_class_reads_as_simple(make_broker) -> None:
    """Alpaca spells a simple order both ``"simple"`` and ``""``."""
    broker, _ = make_broker(lambda _request: (200, EMPTY_CLASS_ORDER))
    order = (await broker.orders())[0]
    assert order.order_class is OrderClass.SIMPLE
    assert order.is_multi_leg is False


EMPTY_CLASS_ORDER = """[
  {
    "id": "1b3d0b6e-0000-4000-8000-000000000001",
    "symbol": "NVDA260911C00205000",
    "asset_class": "us_option",
    "order_class": "",
    "side": "buy",
    "position_intent": "buy_to_open",
    "type": "limit",
    "time_in_force": "day",
    "status": "filled",
    "qty": "1",
    "filled_qty": "1",
    "filled_avg_price": "14.2",
    "created_at": "2026-09-10T17:37:14.0Z",
    "legs": null
  }
]"""
