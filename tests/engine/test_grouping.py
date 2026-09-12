"""``engine/grouping.py`` -- does an ``mleg`` order still describe a live spread?

**Most of what follows runs on real recorded data.** The fixtures under
``tests/fixtures/alpaca/`` were captured against the real paper account by
``tests/fixtures/record_alpaca.py``: 13 position rows across 9 logical
positions, and 11 top-level orders of which 4 are ``mleg`` parents carrying
their ``legs[]``. They are replayed through an ``httpx.MockTransport`` and
parsed by the *real* broker parser, so the sign conventions under test are the
ones Alpaca actually sends -- a short leg's ``qty: "-1"`` alongside
``side: "short"``, with ``cost_basis`` and ``market_value`` both negative.

**Two shapes the account does not hold are hand-authored, and say so where a
reader meets them** -- an iron condor and a ratio spread. A hand-written
fixture proves the logic is self-consistent, never that it matches what Alpaca
sends. ``make_position`` and ``mleg_order`` are the only hand-authored
builders; everything reached through :func:`recorded` is a recording.

The negative control is the important one
-----------------------------------------

"It grouped correctly" and "it would also have grouped correctly with a broken
join" are different claims. The join is **two-hop** -- a fill carries its
*leg's* order id, so reaching the parent needs ``?nested=true`` and a leg-id to
parent-id map built from ``legs[]`` -- and a one-hop implementation groups
nothing at all, silently, because on a simple order the two ids coincide.
:func:`test_a_one_hop_join_groups_nothing_and_does_not_do_it_silently` strips
``legs[]`` off the recorded parents and pins both halves: nothing groups, and
the grouper says so.
"""

import asyncio
import functools
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from corollary.data.providers.alpaca import AlpacaCredentials
from corollary.data.providers.interface import OptionContract
from corollary.engine.execution.alpaca import AlpacaBroker
from corollary.engine.execution.interface import (
    BrokerPosition,
    Order,
    OrderClass,
    OrderSide,
    PositionIntent,
    PositionSide,
)
from corollary.engine.grouping import (
    DeclineRule,
    GroupingResult,
    PositionKind,
    RiskClass,
    group_positions,
)
from corollary.instruments import parse_occ_symbol
from corollary.ratelimit import ALPACA_PAPER_TRADING_HOST, HostRateLimiter

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "alpaca"

#: Obviously fake, and pointed at the paper host. Rule 6: no key material in
#: tests or fixtures. Rule 5: paper is the default everywhere.
CREDENTIALS = AlpacaCredentials(
    key_id="PKTESTTESTTESTTEST",
    secret_key="not-a-real-secret",
    trading_base_url=f"https://{ALPACA_PAPER_TRADING_HOST}",
    is_paper=True,
)


# --------------------------------------------------------------------------
# The recording, replayed through the real parser
# --------------------------------------------------------------------------


def _body_bytes(name: str) -> tuple[int, bytes]:
    """A fixture's ``body``, sliced out of the file rather than re-serialised.

    ``raw_decode`` reports where the value ended, which is what makes an exact
    slice possible. The broker then parses the file's own bytes with
    ``corollary.wire.decode_json``, so every money field becomes an exact
    ``Decimal`` from the literal text and no float exists on the path.
    """
    text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    marker = '"body":'
    start = text.index(marker) + len(marker)
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    status = json.loads(text)["status_code"]
    return int(status), text[start:end].encode("utf-8")


def _serve(request: httpx.Request) -> httpx.Response:
    routes = {"/v2/positions": "positions", "/v2/orders": "orders_nested"}
    name = routes.get(request.url.path)
    if name is None:  # pragma: no cover - a URL nobody predicted
        raise AssertionError(f"no fixture routed for {request.method} {request.url}")
    status, body = _body_bytes(name)
    return httpx.Response(
        status_code=status,
        content=body,
        headers={"content-type": "application/json"},
        request=request,
    )


async def _never_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError(f"a replay test waited {seconds}s on the rate limiter")


async def _replay() -> tuple[tuple[BrokerPosition, ...], tuple[Order, ...]]:
    limiter = HostRateLimiter(
        requests_per_minute=10_000, clock=lambda: 0.0, sleep=_never_sleep
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_serve)) as client:
        broker = AlpacaBroker(credentials=CREDENTIALS, client=client, limiter=limiter)
        return tuple(await broker.positions()), tuple(await broker.orders())


@functools.cache
def recorded() -> tuple[tuple[BrokerPosition, ...], tuple[Order, ...]]:
    """The real recording: 13 position rows, 11 orders, 4 of them ``mleg``."""
    return asyncio.run(_replay())


def recorded_positions() -> tuple[BrokerPosition, ...]:
    return recorded()[0]


def recorded_orders() -> tuple[Order, ...]:
    return recorded()[1]


def vertical_for(symbol: str) -> Order:
    """The recorded ``mleg`` parent one of whose legs is ``symbol``."""
    for order in recorded_orders():
        if symbol in {leg.symbol for leg in order.legs}:
            return order
    raise AssertionError(f"no recorded mleg order holds a {symbol} leg")


def contracts_for(
    positions: Sequence[BrokerPosition], *, multiplier: Decimal = Decimal(100)
) -> dict[str, OptionContract]:
    """Contract terms for every held symbol, as the contracts endpoint sends.

    Hand-authored: ``/v2/options/contracts`` responses for these particular
    contracts were not recorded. The *fields the grouper reads* are the point
    -- ``multiplier``, ``underlying_symbol`` and ``root_symbol`` -- and those
    are structural fields the spec confirms come back populated.
    """
    return {
        position.symbol: option_contract(position.symbol, multiplier=multiplier)
        for position in positions
    }


# --------------------------------------------------------------------------
# Hand-authored builders -- used only for shapes the account does not hold
# --------------------------------------------------------------------------


def make_position(symbol: str, quantity: int) -> BrokerPosition:
    """A hand-authored position row, signs arranged as the recording shows.

    A short reports a negative ``qty`` *alongside* ``side: "short"``, with
    ``cost_basis`` and ``market_value`` both negative, because a credit is a
    liability. The ``100`` below is a **fixture** convention for turning a
    premium into a basis; the engine never assumes it and reads ``multiplier``
    per contract.
    """
    entry = Decimal("1.50")
    basis = entry * quantity * 100
    return BrokerPosition(
        symbol=symbol,
        asset_class="us_option",
        quantity=Decimal(quantity),
        quantity_available=Decimal(quantity),
        side=PositionSide.LONG if quantity > 0 else PositionSide.SHORT,
        average_entry_price=entry,
        cost_basis=basis,
        market_value=basis,
        current_price=entry,
        lastday_price=entry,
        change_today=Decimal(0),
        unrealized_pl=Decimal(0),
        unrealized_plpc=Decimal(0),
        unrealized_intraday_pl=Decimal(0),
        unrealized_intraday_plpc=Decimal(0),
        asset_marginable=True,
        exchange="",
    )


_AT = datetime(2026, 9, 10, 17, 11, 25, tzinfo=UTC)


def mleg_order(
    order_id: str,
    legs: Sequence[tuple[str, PositionIntent, int]],
    *,
    net_price: Decimal | None,
    units: int = 1,
    at: datetime = _AT,
) -> Order:
    """A hand-authored ``mleg`` parent with its legs nested.

    Shaped on the recording: the parent carries ``symbol: ""``, ``side: None``
    and no ``position_intent`` -- it is the structure, the legs are the
    instruments -- and its ``filled_avg_price`` is **signed**, negative for a
    net credit.
    """
    return Order(
        id=order_id,
        symbol="",
        asset_class="",
        order_class=OrderClass.MLEG,
        side=None,
        position_intent=None,
        order_type="market",
        time_in_force="day",
        status="filled",
        quantity=Decimal(units),
        filled_quantity=Decimal(units),
        filled_avg_price=net_price,
        limit_price=None,
        stop_price=None,
        ratio_qty=None,
        created_at=at,
        submitted_at=at,
        filled_at=at,
        canceled_at=None,
        expired_at=None,
        updated_at=at,
        extended_hours=False,
        legs=tuple(
            Order(
                id=f"{order_id}-leg{index}",
                symbol=symbol,
                asset_class="us_option",
                order_class=OrderClass.MLEG,
                side=(
                    OrderSide.BUY
                    if intent
                    in {PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE}
                    else OrderSide.SELL
                ),
                position_intent=intent,
                order_type="market",
                time_in_force="day",
                status="filled",
                quantity=Decimal(units * ratio),
                filled_quantity=Decimal(units * ratio),
                filled_avg_price=Decimal("1.50"),
                limit_price=None,
                stop_price=None,
                ratio_qty=Decimal(ratio),
                created_at=at,
                submitted_at=at,
                filled_at=at,
                canceled_at=None,
                expired_at=None,
                updated_at=at,
                extended_hours=False,
            )
            for index, (symbol, intent, ratio) in enumerate(legs)
        ),
    )


def option_contract(
    symbol: str,
    *,
    multiplier: Decimal = Decimal(100),
    underlying: str | None = None,
) -> OptionContract:
    occ = parse_occ_symbol(symbol)
    return OptionContract(
        symbol=occ.symbol,
        underlying_symbol=underlying if underlying is not None else occ.root,
        root_symbol=occ.root,
        expiration=occ.expiration,
        option_type=occ.option_type,
        strike=occ.strike,
        style="american",
        multiplier=multiplier,
        size=Decimal(100),
        open_interest=None,
        open_interest_date=None,
        close_price=None,
        close_price_date=None,
        tradable=True,
        status="active",
        name=occ.symbol,
    )


def shape(result: GroupingResult) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (logical.id, tuple(leg.symbol for leg in logical.legs))
        for logical in result.positions
    ]


# --------------------------------------------------------------------------
# The recording: 13 rows, 9 logical positions
# --------------------------------------------------------------------------


def test_the_recording_is_the_shape_these_tests_assume() -> None:
    positions, orders = recorded()
    assert len(positions) == 13
    assert len(orders) == 11
    assert sum(order.order_class is OrderClass.MLEG for order in orders) == 4


def test_thirteen_rows_become_nine_logical_positions() -> None:
    result = group_positions(recorded_positions(), recorded_orders())

    assert len(result.positions) == 9
    assert len(result.groups) == 4
    assert len(result.ungrouped) == 5
    assert sum(len(logical.legs) for logical in result.positions) == 13


def test_every_recorded_row_lands_in_exactly_one_logical_position() -> None:
    result = group_positions(recorded_positions(), recorded_orders())
    placed = [leg.symbol for logical in result.positions for leg in logical.legs]

    assert sorted(placed) == sorted(
        position.symbol for position in recorded_positions()
    )
    assert len(set(placed)) == len(placed), "a row was counted twice"


def test_the_four_recorded_verticals_group_on_their_order_ids() -> None:
    result = group_positions(recorded_positions(), recorded_orders())
    grouped = {
        logical.order_id: tuple(sorted(leg.symbol for leg in logical.legs))
        for logical in result.groups
    }

    assert grouped == {
        vertical_for("SPY261130P00721000").id: (
            "SPY261130P00706000",
            "SPY261130P00721000",
        ),
        vertical_for("NVDA261218P00205000").id: (
            "NVDA261218P00200000",
            "NVDA261218P00205000",
        ),
        vertical_for("TSLA261218P00340000").id: (
            "TSLA261218P00330000",
            "TSLA261218P00340000",
        ),
        vertical_for("AMD270115P00470000").id: (
            "AMD270115P00460000",
            "AMD270115P00470000",
        ),
    }
    assert result.declined == ()


def test_the_five_recorded_singles_are_labelled_ungrouped() -> None:
    result = group_positions(recorded_positions(), recorded_orders())

    assert {logical.legs[0].symbol for logical in result.ungrouped} == {
        "AAPL261218C00340000",
        "QQQ270115C00740000",
        "NVDA260911C00205000",
        "NVDA260911C00240000",
        "NVDA260911P00230000",
    }
    for logical in result.ungrouped:
        assert logical.kind is PositionKind.UNGROUPED
        assert logical.kind.value == "ungrouped"
        assert logical.order_id is None
        assert len(logical.legs) == 1


def test_identical_inputs_produce_identical_output() -> None:
    """Determinism, and not incidentally: the scanner rule applies here too."""
    positions, orders = recorded()

    first = group_positions(positions, orders)
    again = group_positions(positions, orders)
    from_reordered_history = group_positions(positions, tuple(reversed(orders)))

    assert shape(first) == shape(again)
    assert shape(first) == shape(from_reordered_history)


# --------------------------------------------------------------------------
# Rule 4: a credit spread is defined risk, and its short leg alone is not
# --------------------------------------------------------------------------


def test_a_recorded_credit_spread_reports_short_direction_and_defined_risk() -> None:
    """Direction comes from the order's net price: debit long, credit short."""
    result = group_positions(recorded_positions(), recorded_orders())
    spy = next(
        logical
        for logical in result.groups
        if logical.order_id == vertical_for("SPY261130P00721000").id
    )

    assert spy.net_price == Decimal("-2.01")
    assert spy.direction is PositionSide.SHORT
    assert spy.risk_class is RiskClass.DEFINED
    assert spy.underlying == "SPY"
    assert spy.expiry == parse_occ_symbol("SPY261130P00721000").expiration
    assert spy.units == 1


def test_the_short_leg_of_a_spread_is_naked_only_when_the_evidence_is_missing() -> None:
    """The failure rule 4 exists to prevent, pinned from both sides.

    With the ``mleg`` order in hand the SPY 721 put is one leg of a
    defined-risk credit spread. Without it -- which is what a one-hop join
    leaves you -- the same row reports as an *undefined-risk* naked short, and
    a risk manager sizing against that computes max loss wrong.
    """
    positions = recorded_positions()

    with_evidence = group_positions(positions, recorded_orders())
    spy_group = next(
        logical
        for logical in with_evidence.groups
        if any(leg.symbol == "SPY261130P00721000" for leg in logical.legs)
    )
    assert spy_group.risk_class is RiskClass.DEFINED

    without_evidence = group_positions(positions, ())
    naked = next(
        logical
        for logical in without_evidence.positions
        if logical.legs[0].symbol == "SPY261130P00721000"
    )
    assert naked.kind is PositionKind.UNGROUPED
    assert naked.risk_class is RiskClass.UNDEFINED
    assert naked.direction is PositionSide.SHORT


def test_a_long_single_leg_reports_long_premium_risk() -> None:
    result = group_positions(recorded_positions(), recorded_orders())
    aapl = next(
        logical
        for logical in result.ungrouped
        if logical.legs[0].symbol == "AAPL261218C00340000"
    )

    assert aapl.risk_class is RiskClass.LONG_PREMIUM
    assert aapl.direction is PositionSide.LONG
    assert aapl.units == 1


def test_a_short_legs_negative_signs_survive_grouping() -> None:
    """``-4155`` cost basis and ``-3955`` market value. Never ``abs()``-ed."""
    result = group_positions(recorded_positions(), recorded_orders())
    amd = next(
        logical
        for logical in result.groups
        if any(leg.symbol == "AMD270115P00470000" for leg in logical.legs)
    )
    short = next(leg for leg in amd.legs if leg.symbol == "AMD270115P00470000")
    long_leg = next(leg for leg in amd.legs if leg.symbol == "AMD270115P00460000")

    assert short.quantity == Decimal(-1)
    assert short.side is PositionSide.SHORT
    assert short.cost_basis == Decimal("-4155")
    assert short.market_value == Decimal("-3955")
    assert isinstance(short.cost_basis, Decimal)

    # And the logical position's basis is the *net* of the two, signed.
    assert long_leg.cost_basis == Decimal("3815")
    assert amd.cost_basis == Decimal("-340")
    assert amd.market_value == Decimal("-615")


# --------------------------------------------------------------------------
# The negative control: a one-hop join groups nothing
# --------------------------------------------------------------------------


def _one_hop(orders: Sequence[Order]) -> tuple[Order, ...]:
    """The recorded history as a one-hop join would see it: no ``legs[]``."""
    return tuple(
        replace(order, legs=()) if order.order_class is OrderClass.MLEG else order
        for order in orders
    )


def test_a_one_hop_join_groups_nothing_and_does_not_do_it_silently() -> None:
    """Strip ``legs[]`` off the recorded parents -- the one-hop world.

    The recorded history carries the leg orders *only* inside their parents,
    so without ``?nested=true`` there is no leg-id to parent-id map and no
    proposal has any legs to check. Both halves matter: nothing groups, and
    the grouper reports four declined proposals naming the rule. Silent
    rejection is a bug.
    """
    positions, orders = recorded()

    result = group_positions(positions, _one_hop(orders))

    assert result.groups == ()
    assert len(result.ungrouped) == 13
    assert [decline.rule for decline in result.declined] == [DeclineRule.NO_LEGS] * 4
    assert {decline.order_id for decline in result.declined} == {
        order.id for order in orders if order.order_class is OrderClass.MLEG
    }


def test_every_decline_records_the_rule_the_inputs_and_a_timestamp() -> None:
    positions, orders = recorded()

    result = group_positions(positions, _one_hop(orders))

    assert result.declined
    for decline in result.declined:
        assert decline.rule in set(DeclineRule)
        assert decline.order_id
        assert decline.detail
        assert decline.inputs
        assert decline.at.tzinfo is not None, "UTC in storage, and aware"
        assert decline.at.utcoffset() == UTC.utcoffset(None)


def test_declines_are_logged_with_the_rule_and_the_order_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 8 on the wire, not just in the returned record.

    This asserts against the real logger with nothing propped up around it,
    which is only possible because ``corollary/db/migrations/env.py`` passes
    ``disable_existing_loggers=False`` -- see the comment there for why that
    argument is load-bearing rather than tidiness.
    """
    positions, orders = recorded()

    with caplog.at_level(logging.INFO, logger="corollary.engine.grouping"):
        group_positions(positions, _one_hop(orders))

    declines = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "mleg_group_declined"
    ]
    assert len(declines) == 4
    assert {record.rule for record in declines} == {"no_legs"}  # type: ignore[attr-defined]
    assert all(record.order_id for record in declines)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Evidence, not a heuristic
# --------------------------------------------------------------------------


def test_two_unrelated_same_expiry_positions_do_not_group() -> None:
    """The test that proves this is evidence rather than a heuristic.

    These three rows are **real**, recorded off the live paper account: three
    single-leg NVDA positions sharing one expiry (2026-09-11), mixed sides,
    different strikes, each from its own simple order. Exactly the shape a
    same-underlying/same-expiry/offsetting-sides heuristic would fuse into an
    imagined vertical or condor. They must stay three rows.

    And the same underlying carries a *genuine* vertical from a real ``mleg``
    order at a different expiry, so this also pins that the grouper keys on
    the order rather than on the symbol.
    """
    result = group_positions(recorded_positions(), recorded_orders())
    same_expiry = {
        "NVDA260911C00205000",
        "NVDA260911C00240000",
        "NVDA260911P00230000",
    }

    singles = [
        logical for logical in result.positions if logical.legs[0].symbol in same_expiry
    ]
    assert len(singles) == 3
    for logical in singles:
        assert logical.kind is PositionKind.UNGROUPED
        assert len(logical.legs) == 1
        assert logical.order_id is None

    assert len({logical.expiry for logical in singles}) == 1, (
        "the fixture's point is that these three share an expiry"
    )

    real_vertical = next(
        logical
        for logical in result.groups
        if any(leg.symbol == "NVDA261218P00205000" for leg in logical.legs)
    )
    assert len(real_vertical.legs) == 2


def test_two_verticals_sharing_an_expiry_across_underlyings_stay_apart() -> None:
    """Recorded: TSLA 261218 and NVDA 261218 are both real verticals.

    A same-expiry heuristic that ignored the underlying would fuse four legs
    into one imagined condor.
    """
    result = group_positions(recorded_positions(), recorded_orders())
    december = [
        logical
        for logical in result.groups
        if logical.expiry == parse_occ_symbol("TSLA261218P00330000").expiration
    ]

    assert len(december) == 2
    assert {logical.underlying for logical in december} == {"TSLA", "NVDA"}


# --------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------


def test_one_leg_closed_ungroups_the_survivor() -> None:
    """Recorded rows minus one: the AMD long put is gone, the short remains.

    A partially-closed spread is not live -- its survivors fall back to
    ungrouped rows, because that spread genuinely no longer exists. And the
    surviving short is then reported as what it now is: naked.
    """
    positions = tuple(
        position
        for position in recorded_positions()
        if position.symbol != "AMD270115P00460000"
    )

    result = group_positions(positions, recorded_orders())

    assert len(result.groups) == 3
    survivor = next(
        logical
        for logical in result.positions
        if logical.legs[0].symbol == "AMD270115P00470000"
    )
    assert survivor.kind is PositionKind.UNGROUPED
    assert survivor.risk_class is RiskClass.UNDEFINED

    (decline,) = result.declined
    assert decline.rule is DeclineRule.LEG_NOT_HELD
    assert decline.order_id == vertical_for("AMD270115P00470000").id
    assert "AMD270115P00460000" in decline.inputs.values()


def test_a_leg_held_on_the_wrong_side_does_not_group() -> None:
    """The order opened the AMD 470 put short; the book shows it long."""
    positions = tuple(
        make_position(position.symbol, 1)
        if position.symbol == "AMD270115P00470000"
        else position
        for position in recorded_positions()
    )

    result = group_positions(positions, recorded_orders())

    assert len(result.groups) == 3
    (decline,) = result.declined
    assert decline.rule is DeclineRule.SIDE_MISMATCH


def test_ratio_mismatch_does_not_group() -> None:
    """The order is 1:1; the book holds two longs against one short."""
    positions = tuple(
        make_position(position.symbol, 2)
        if position.symbol == "TSLA261218P00330000"
        else position
        for position in recorded_positions()
    )

    result = group_positions(positions, recorded_orders())

    assert len(result.groups) == 3
    assert {logical.legs[0].symbol for logical in result.ungrouped} >= {
        "TSLA261218P00330000",
        "TSLA261218P00340000",
    }
    (decline,) = result.declined
    assert decline.rule is DeclineRule.RATIO_MISMATCH
    assert decline.order_id == vertical_for("TSLA261218P00340000").id


def test_a_quantity_the_ratio_does_not_divide_does_not_group() -> None:
    """Hand-authored: three contracts against a ratio of two is 1.5 spreads."""
    order = mleg_order(
        "ratio-2-1",
        [
            ("SPY270115P00700000", PositionIntent.BUY_TO_OPEN, 2),
            ("SPY270115P00690000", PositionIntent.SELL_TO_OPEN, 1),
        ],
        net_price=Decimal("1.10"),
    )
    positions = (
        make_position("SPY270115P00700000", 3),
        make_position("SPY270115P00690000", -1),
    )

    result = group_positions(positions, (order,))

    assert result.groups == ()
    (decline,) = result.declined
    assert decline.rule is DeclineRule.RATIO_MISMATCH


def test_two_units_with_one_unit_closed_stays_one_live_spread() -> None:
    """Hand-authored. Closing one of two verticals leaves one vertical.

    The liveness rule is "every leg still holds a nonzero position on the
    expected side, with quantities consistent with the order's ratios" -- a
    proportional reduction still satisfies it, and reporting the survivor as
    two ungrouped legs would put a naked short on screen where a defined-risk
    spread is held.
    """
    order = mleg_order(
        "two-units",
        [
            ("SPY270115P00700000", PositionIntent.BUY_TO_OPEN, 1),
            ("SPY270115P00710000", PositionIntent.SELL_TO_OPEN, 1),
        ],
        net_price=Decimal("-1.10"),
        units=2,
    )
    positions = (
        make_position("SPY270115P00700000", 1),
        make_position("SPY270115P00710000", -1),
    )

    result = group_positions(positions, (order,))

    (group,) = result.groups
    assert group.units == 1
    assert group.risk_class is RiskClass.DEFINED
    assert result.declined == ()


def test_an_unfilled_mleg_order_is_not_evidence() -> None:
    """Hand-authored: a cancelled spread never opened anything."""
    order = replace(
        mleg_order(
            "cancelled",
            [
                ("SPY270115P00700000", PositionIntent.BUY_TO_OPEN, 1),
                ("SPY270115P00710000", PositionIntent.SELL_TO_OPEN, 1),
            ],
            net_price=None,
        ),
        status="canceled",
        filled_quantity=Decimal(0),
        filled_at=None,
    )
    positions = (
        make_position("SPY270115P00700000", 1),
        make_position("SPY270115P00710000", -1),
    )

    result = group_positions(positions, (order,))

    assert result.groups == ()
    (decline,) = result.declined
    assert decline.rule is DeclineRule.NOT_FILLED


def test_a_closing_mleg_order_proposes_nothing() -> None:
    """Hand-authored: the order that *closed* a spread must not re-open it."""
    order = mleg_order(
        "closing",
        [
            ("SPY270115P00700000", PositionIntent.SELL_TO_CLOSE, 1),
            ("SPY270115P00710000", PositionIntent.BUY_TO_CLOSE, 1),
        ],
        net_price=Decimal("1.05"),
    )
    positions = (
        make_position("SPY270115P00700000", 1),
        make_position("SPY270115P00710000", -1),
    )

    result = group_positions(positions, (order,))

    assert result.groups == ()
    assert len(result.ungrouped) == 2
    (decline,) = result.declined
    assert decline.rule is DeclineRule.NOT_AN_OPENING_ORDER


def test_the_same_holding_is_not_claimed_by_two_orders() -> None:
    """Hand-authored: two identical spreads, one holding. One group, one decline.

    A broker position row belongs to exactly one logical position, wholly --
    the broker itself aggregates per OCC symbol and does not split by order, so
    splitting here would mean pro-rating a cost basis nobody sent. The later
    proposal is declined rather than dropped.
    """
    legs = [
        ("SPY270115P00700000", PositionIntent.BUY_TO_OPEN, 1),
        ("SPY270115P00710000", PositionIntent.SELL_TO_OPEN, 1),
    ]
    first = mleg_order("first", legs, net_price=Decimal("-1.10"))
    second = mleg_order(
        "second", legs, net_price=Decimal("-1.20"), at=_AT.replace(hour=18)
    )
    positions = (
        make_position("SPY270115P00700000", 2),
        make_position("SPY270115P00710000", -2),
    )

    result = group_positions(positions, (second, first))

    (group,) = result.groups
    assert group.order_id == "first", "oldest evidence first, deterministically"
    assert group.units == 2
    (decline,) = result.declined
    assert decline.rule is DeclineRule.LEG_ALREADY_GROUPED
    assert decline.order_id == "second"


# --------------------------------------------------------------------------
# Hand-authored structures the account does not hold
# --------------------------------------------------------------------------


def test_an_iron_condor_groups() -> None:
    """**Hand-authored.** This account holds no four-leg structure.

    Four legs, one order, one logical position. Both wings are covered, so the
    structure is defined-risk -- which is the whole reason the four rows must
    not be rendered as four positions, two of them naked shorts.
    """
    order = mleg_order(
        "condor",
        [
            ("SPY270115P00690000", PositionIntent.BUY_TO_OPEN, 1),
            ("SPY270115P00700000", PositionIntent.SELL_TO_OPEN, 1),
            ("SPY270115C00760000", PositionIntent.SELL_TO_OPEN, 1),
            ("SPY270115C00770000", PositionIntent.BUY_TO_OPEN, 1),
        ],
        net_price=Decimal("-3.25"),
    )
    positions = (
        make_position("SPY270115P00690000", 1),
        make_position("SPY270115P00700000", -1),
        make_position("SPY270115C00760000", -1),
        make_position("SPY270115C00770000", 1),
    )

    result = group_positions(positions, (order,))

    (condor,) = result.groups
    assert len(result.positions) == 1
    assert len(condor.legs) == 4
    assert condor.kind is PositionKind.MULTI_LEG
    assert condor.direction is PositionSide.SHORT
    assert condor.risk_class is RiskClass.DEFINED
    assert [leg.ratio for leg in condor.legs] == [1, 1, 1, 1]
    assert condor.units == 1


def test_an_iron_condor_with_one_wing_closed_ungroups_the_rest() -> None:
    """**Hand-authored.** Half a condor is not a condor."""
    order = mleg_order(
        "condor",
        [
            ("SPY270115P00690000", PositionIntent.BUY_TO_OPEN, 1),
            ("SPY270115P00700000", PositionIntent.SELL_TO_OPEN, 1),
            ("SPY270115C00760000", PositionIntent.SELL_TO_OPEN, 1),
            ("SPY270115C00770000", PositionIntent.BUY_TO_OPEN, 1),
        ],
        net_price=Decimal("-3.25"),
    )
    positions = (
        make_position("SPY270115P00690000", 1),
        make_position("SPY270115P00700000", -1),
    )

    result = group_positions(positions, (order,))

    assert result.groups == ()
    assert len(result.ungrouped) == 2
    (decline,) = result.declined
    assert decline.rule is DeclineRule.LEG_NOT_HELD


def test_a_ratio_spread_groups_but_reports_undefined_risk() -> None:
    """**Hand-authored.** Grouping and defined risk are different questions.

    A 1x2 short ratio spread is a real structure from a real order, so it
    groups. Its second short is uncovered, so its loss is unbounded and the
    risk class must say so -- the honest absence, not a confident number.
    """
    order = mleg_order(
        "ratio",
        [
            ("SPY270115P00700000", PositionIntent.BUY_TO_OPEN, 1),
            ("SPY270115P00690000", PositionIntent.SELL_TO_OPEN, 2),
        ],
        net_price=Decimal("-0.40"),
    )
    positions = (
        make_position("SPY270115P00700000", 1),
        make_position("SPY270115P00690000", -2),
    )

    result = group_positions(positions, (order,))

    (ratio,) = result.groups
    assert [leg.ratio for leg in ratio.legs] == [1, 2]
    assert ratio.units == 1
    assert ratio.risk_class is RiskClass.UNDEFINED
    assert ratio.direction is PositionSide.SHORT


def test_ratios_not_in_simplest_form_are_declined() -> None:
    """**Hand-authored.** Alpaca requires the GCD across ``ratio_qty`` to be 1.

    A 2:2 order is a 1:1 order this code did not parse correctly, and acting on
    it would size the group wrong. Declining the whole proposal is the same
    refusal-to-partially-evaluate the indicator whitelist uses.
    """
    order = mleg_order(
        "not-simplest",
        [
            ("SPY270115P00700000", PositionIntent.BUY_TO_OPEN, 2),
            ("SPY270115P00710000", PositionIntent.SELL_TO_OPEN, 2),
        ],
        net_price=Decimal("-1.10"),
    )
    positions = (
        make_position("SPY270115P00700000", 2),
        make_position("SPY270115P00710000", -2),
    )

    result = group_positions(positions, (order,))

    assert result.groups == ()
    (decline,) = result.declined
    assert decline.rule is DeclineRule.RATIOS_NOT_SIMPLEST


def test_a_calendar_spreads_short_near_leg_is_not_called_defined() -> None:
    """**Hand-authored.** Coverage is within one expiry, conservatively.

    A short near-dated leg against a long far-dated one has no maximum loss
    *at the near expiry*, so it is not classified defined-risk. Over-stating
    risk is the safe direction; under-stating it is how the risk manager
    computes max loss wrong.
    """
    order = mleg_order(
        "calendar",
        [
            ("SPY261130P00700000", PositionIntent.SELL_TO_OPEN, 1),
            ("SPY270115P00700000", PositionIntent.BUY_TO_OPEN, 1),
        ],
        net_price=Decimal("1.40"),
    )
    positions = (
        make_position("SPY261130P00700000", -1),
        make_position("SPY270115P00700000", 1),
    )

    result = group_positions(positions, (order,))

    (calendar,) = result.groups
    assert calendar.risk_class is RiskClass.UNDEFINED
    assert calendar.direction is PositionSide.LONG, "a debit is a long"
    assert calendar.expiry == parse_occ_symbol("SPY261130P00700000").expiration
    assert len(calendar.expirations) == 2


def test_a_duplicate_leg_symbol_within_one_order_is_declined() -> None:
    """**Hand-authored.** Ratios arrive in simplest form, so two rows on one
    symbol would already have been combined. A second is a parse error."""
    order = mleg_order(
        "duplicate",
        [
            ("SPY270115P00700000", PositionIntent.BUY_TO_OPEN, 1),
            ("SPY270115P00700000", PositionIntent.SELL_TO_OPEN, 1),
        ],
        net_price=Decimal("0"),
    )
    positions = (make_position("SPY270115P00700000", 1),)

    result = group_positions(positions, (order,))

    assert result.groups == ()
    (decline,) = result.declined
    assert decline.rule is DeclineRule.DUPLICATE_LEG_SYMBOL


def test_a_zero_net_price_reports_no_direction_rather_than_guessing() -> None:
    """**Hand-authored.** Neither a debit nor a credit, so neither long nor short.

    ``mleg_group.net_price`` is nullable for the same reason: a group labelled
    debit or credit off a substituted number is the invented value picking the
    risk class.
    """
    order = mleg_order(
        "costless",
        [
            ("SPY270115P00700000", PositionIntent.BUY_TO_OPEN, 1),
            ("SPY270115C00760000", PositionIntent.SELL_TO_OPEN, 1),
        ],
        net_price=Decimal("0"),
    )
    positions = (
        make_position("SPY270115P00700000", 1),
        make_position("SPY270115C00760000", -1),
    )

    result = group_positions(positions, (order,))

    (collar,) = result.groups
    assert collar.net_price == Decimal(0)
    assert collar.direction is None


# --------------------------------------------------------------------------
# Multipliers, and the adjusted contracts that are the reason for them
# --------------------------------------------------------------------------


def test_the_multiplier_comes_from_the_contracts_endpoint() -> None:
    positions = recorded_positions()
    result = group_positions(
        positions, recorded_orders(), contracts=contracts_for(positions)
    )

    for logical in result.positions:
        for leg in logical.legs:
            assert leg.multiplier == Decimal(100)
            assert leg.is_adjusted is False
    assert result.unknown_multipliers == ()
    assert result.adjusted_contracts == ()


def test_an_absent_multiplier_is_none_and_surfaced_never_a_hundred() -> None:
    """``/v2/positions`` carries no multiplier field at all -- confirmed on
    every one of the 13 recorded rows. There is nothing to fall back to, and
    100 is the number that is wrong on an adjusted contract."""
    result = group_positions(recorded_positions(), recorded_orders())

    for logical in result.positions:
        for leg in logical.legs:
            assert leg.multiplier is None
            assert leg.is_adjusted is None, "unknown is a third answer"
    assert len(result.unknown_multipliers) == 13


def test_a_non_standard_multiplier_is_carried_not_normalised() -> None:
    positions = recorded_positions()
    contracts = contracts_for(positions, multiplier=Decimal(10))
    result = group_positions(positions, recorded_orders(), contracts=contracts)

    assert result.positions[0].legs[0].multiplier == Decimal(10)


def test_an_adjusted_contract_is_flagged_and_surfaced() -> None:
    """Detection is ``root_symbol != underlying_symbol``, and nothing else.

    **Hand-authored**: this account holds no adjusted contract. Note the
    multiplier below is the standard 100 -- every live adjusted contract
    reports exactly that, which is why the root comparison is the only test
    that works.
    """
    positions = (make_position("AAPL1261218C00150000", 1),)
    contracts = {
        "AAPL1261218C00150000": option_contract(
            "AAPL1261218C00150000", underlying="AAPL"
        )
    }

    result = group_positions(positions, (), contracts=contracts)

    (logical,) = result.positions
    assert logical.legs[0].is_adjusted is True
    assert logical.legs[0].multiplier == Decimal(100)
    assert logical.underlying == "AAPL", "the underlying, not the modified root"
    assert result.adjusted_contracts == ("AAPL1261218C00150000",)


def test_corollary_s_own_position_fields_have_no_source_in_this_phase() -> None:
    """Nothing Corollary opened exists, so these are legitimately absent.

    Pinned as a test rather than left to a docstring because inventing a value
    here is the tempting mistake: a logical position carrying a strategy id it
    was never opened by is a position the engine believes it manages.
    """
    result = group_positions(recorded_positions(), recorded_orders())

    for logical in result.positions:
        assert not hasattr(logical, "strategy_id")
        assert not hasattr(logical, "managed_exit")
        assert not hasattr(logical, "attached_exit")
        assert not hasattr(logical, "value_history")


# --------------------------------------------------------------------------
# Rows that are not option contracts
# --------------------------------------------------------------------------


def test_shares_delivered_by_an_exercise_are_named_not_grouped() -> None:
    """Corollary is not a stock app, so the resulting shares are *named*.

    An ITM expiry auto-exercises and delivers stock. A share row carries no OCC
    symbol, cannot be a leg, and must not vanish -- it is reported with a
    reason instead.
    """
    shares = BrokerPosition(
        symbol="NVDA",
        asset_class="us_equity",
        quantity=Decimal(100),
        quantity_available=Decimal(100),
        side=PositionSide.LONG,
        average_entry_price=Decimal("205"),
        cost_basis=Decimal("20500"),
        market_value=Decimal("21817"),
        current_price=Decimal("218.17"),
        lastday_price=Decimal("218.17"),
        change_today=Decimal(0),
        unrealized_pl=Decimal("1317"),
        unrealized_plpc=Decimal("0.06424"),
        unrealized_intraday_pl=Decimal(0),
        unrealized_intraday_plpc=Decimal(0),
        asset_marginable=True,
        exchange="NASDAQ",
    )

    result = group_positions((*recorded_positions(), shares), recorded_orders())

    assert len(result.positions) == 9
    assert "NVDA" in result.excluded
    assert "us_equity" in result.excluded["NVDA"]


def test_two_rows_for_one_symbol_is_refused_rather_than_merged() -> None:
    duplicated = (*recorded_positions(), recorded_positions()[0])

    with pytest.raises(ValueError, match="two position rows"):
        group_positions(duplicated, recorded_orders())
