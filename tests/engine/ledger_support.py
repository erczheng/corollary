"""Builders for the ledger tests — activities, spelled the way Alpaca spells them.

Shared by the three ledger test modules rather than copied into each, because
the *shapes* are the thing under test and three drifting copies of a shape is
how a fixture stops proving anything.

Two conventions are deliberately kept apart here, because keeping them apart
is half of what the matcher is for:

* :func:`fill` builds a ``TradeActivity`` — an **unsigned** ``qty`` with a
  separate ``side``.
* :func:`option_event` builds a ``NonTradeActivity`` — a **signed** ``qty``
  and no ``side`` at all.

Nothing here is recorded. ``tests/engine/test_ledger_recording.py`` is the
module that replays real bytes; this one is authored arithmetic.
"""

from datetime import datetime, timezone
from decimal import Decimal

from corollary.data.providers.interface import OptionContract, OptionDeliverable
from corollary.engine.execution.interface import (
    FillSide,
    NonTradeActivity,
    TradeActivity,
)
from corollary.instruments import parse_occ_symbol

__all__ = [
    "SESSION",
    "at",
    "contract",
    "equity_deliverable",
    "fee",
    "fill",
    "option_event",
    "optrd",
]

#: An arbitrary but fixed trading day. Fixed matters: a fixture tied to the
#: wall clock silently changes what it is testing next week.
SESSION = (2026, 9, 10)


def at(hour: int, minute: int = 0, second: int = 0, *, day: int | None = None) -> datetime:
    """An aware UTC instant on :data:`SESSION`. Naive values are never built."""
    year, month, default_day = SESSION
    return datetime(
        year, month, day or default_day, hour, minute, second, tzinfo=timezone.utc
    )


def _activity_id(stamp: datetime, tail: str) -> str:
    """``<17-digit stamp>::<tail>`` — the composite shape, not a bare UUID."""
    return f"{stamp:%Y%m%d%H%M%S}{stamp.microsecond // 1000:03d}::{tail}"


def fill(
    symbol: str,
    side: FillSide | str,
    qty: str | int,
    price: str,
    *,
    when: datetime,
    order_id: str | None = None,
    activity_id: str | None = None,
    activity_type: str = "FILL",
) -> TradeActivity:
    """A ``FILL`` row: unsigned ``qty``, direction in ``side``."""
    resolved = FillSide(side)
    tail = order_id or f"{symbol}-{resolved.value}-{price}"
    return TradeActivity(
        id=activity_id or _activity_id(when, tail),
        activity_type=activity_type,
        order_id=order_id if order_id is not None else tail,
        order_status="filled",
        symbol=symbol,
        side=resolved,
        quantity=Decimal(qty),
        price=Decimal(price),
        transaction_time=when,
    )


def option_event(
    activity_type: str,
    symbol: str,
    qty: str | int,
    *,
    when: datetime,
    net_amount: str = "0",
    group_id: str | None = None,
    execution_id: str | None = None,
    activity_id: str | None = None,
) -> NonTradeActivity:
    """An ``OPEXP`` / ``OPEXC`` / ``OPASN`` row: **signed** ``qty``, no ``side``.

    ``net_amount`` defaults to Alpaca's documented ``"0"`` — the whole point
    of the event branch is that the money is not on this row.
    """
    return NonTradeActivity(
        id=activity_id or _activity_id(when, f"{activity_type}-{symbol}"),
        activity_type=activity_type,
        activity_date=when.date(),
        created_at=when,
        net_amount=Decimal(net_amount),
        quantity=Decimal(qty),
        symbol=symbol,
        group_id=group_id,
        execution_id=execution_id,
        status="executed",
        currency="USD",
    )


def optrd(
    underlying: str,
    qty: str | int,
    price: str,
    *,
    when: datetime,
    net_amount: str,
    group_id: str | None = None,
    activity_id: str | None = None,
) -> NonTradeActivity:
    """The ``OPTRD`` row that carries the money for an option event.

    Its ``price`` is the strike and its ``symbol`` is the **underlying**, not
    a contract — which is why the ledger declines it and names the shares
    rather than tracking them.
    """
    return NonTradeActivity(
        id=activity_id or _activity_id(when, f"OPTRD-{underlying}"),
        activity_type="OPTRD",
        activity_date=when.date(),
        created_at=when,
        net_amount=Decimal(net_amount),
        quantity=Decimal(qty),
        price=Decimal(price),
        symbol=underlying,
        group_id=group_id,
        status="executed",
        currency="USD",
    )


def fee(
    amount: str | None,
    *,
    when: datetime,
    sub_type: str = "OCC",
    description: str = "OCC Clearing Fee",
    execution_id: str | None = None,
    group_id: str | None = None,
    order_id: str | None = None,
    activity_id: str | None = None,
) -> NonTradeActivity:
    """A ``FEE`` row.

    ``order_id`` goes into ``extra``, which is the only place it can go: the
    modelled ``NonTradeActivity`` has no ``order_id`` field, because the
    published schema has none and this account has never sent one.

    ``amount=None`` builds the row a **missing or empty** ``net_amount``
    produces: ``NonTradeActivity.net_amount`` is ``Decimal | None`` and
    ``wire.as_decimal("")`` answers ``None``, so an absent field and an empty
    string arrive here as the same thing. Unobserved on this account -- all 19
    recorded ``FEE`` rows carry an amount -- which is exactly why it needs a
    fixture rather than a wait.
    """
    return NonTradeActivity(
        id=activity_id or _activity_id(when, f"FEE-{sub_type}-{amount}"),
        activity_type="FEE",
        activity_sub_type=sub_type,
        activity_date=when.date(),
        created_at=when,
        net_amount=None if amount is None else Decimal(amount),
        group_id=group_id,
        execution_id=execution_id,
        status="executed",
        currency="USD",
        description=description,
        extra={"order_id": order_id} if order_id is not None else {},
    )


def equity_deliverable(symbol: str, amount: str) -> OptionDeliverable:
    """One equity deliverable: ``amount`` shares of ``symbol`` per contract.

    This — not ``multiplier`` and not ``size`` — is what says how many shares
    a contract delivers. Alpaca's own spec on ``size`` is explicit: *"This
    field should **not** be used as a multiplier."*
    """
    return OptionDeliverable(
        type="equity",
        symbol=symbol,
        amount=Decimal(amount),
        allocation_percentage=None,
        settlement_type="PHYS",
        settlement_method="CC",
        delayed_settlement=False,
    )


def contract(
    symbol: str,
    *,
    underlying: str | None = None,
    multiplier: str = "100",
    deliverables: tuple[OptionDeliverable, ...] = (),
) -> OptionContract:
    """Contract terms as ``/v2/options/contracts`` sends them.

    ``underlying`` defaults to the OCC root, which is the standard case.
    Passing a *different* one is how an adjusted contract is built: detection
    is ``root_symbol != underlying_symbol``, and every live adjusted contract
    still reports ``multiplier: "100"`` and ``size: "100"``, so the root
    comparison is the only test that works.

    ``deliverables`` is empty unless the request asked for them
    (``show_deliverables=true``) — which is why absent deliverables must mean
    *unknown* rather than *standard*.
    """
    occ = parse_occ_symbol(symbol)
    return OptionContract(
        symbol=occ.symbol,
        underlying_symbol=underlying if underlying is not None else occ.root,
        root_symbol=occ.root,
        expiration=occ.expiration,
        option_type=occ.option_type,
        strike=occ.strike,
        style="american",
        multiplier=Decimal(multiplier),
        size=Decimal(100),
        open_interest=None,
        open_interest_date=None,
        close_price=None,
        close_price_date=None,
        tradable=True,
        status="active",
        name=occ.symbol,
        deliverables=deliverables,
    )
