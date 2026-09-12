"""Multi-leg reconstruction — pure. Broker positions plus mleg history → logical positions.

Alpaca returns **one position per OCC symbol**, with no leg grouping, no order
linkage and no open date. A four-leg iron condor is four rows and nothing on
any of them says so. This module turns those rows plus the ``mleg`` order
history into *logical* positions: the thing the payoff curve, the DTE column,
max loss and the risk class all assume exists.

Decision 5: **group only where an mleg order proves it**
--------------------------------------------------------

Each historical ``mleg`` order *proposes* a group. A group is **live** only if
every leg still holds a nonzero position on the expected side, with quantities
consistent with the order's ratios. Live groups become one logical position
with :attr:`LogicalPosition.legs` populated. Everything else is a single-leg
position labelled ``ungrouped``.

Nothing is inferred. The rejected alternative is *"a heuristic on
same-underlying/same-expiry/offsetting-sides, which is wrong on iron condors,
ratio spreads, and any two unrelated positions that happen to rhyme"* — and
this account holds the counter-example: three single-leg NVDA positions sharing
one expiry, mixed sides, different strikes, from three unrelated simple orders.
A heuristic fuses them into a spread nobody opened.

**Why it matters, and it is rule 4.** A short leg rendered alone reports as an
*undefined-risk naked short*, so a defined-risk credit spread would state the
wrong risk class — and the risk manager would then compute max loss against
the wrong number. That is the failure rule 4 exists to prevent, and grouping is
what prevents it.

The join behind the evidence is two-hop
---------------------------------------

A fill carries its **leg's** order id; the parent ``mleg`` id appears nowhere
on it. Reaching the parent needs ``GET /v2/orders?nested=true`` and a leg-id →
parent-id map built from ``legs[]`` — see :attr:`Order.leg_ids`. **A one-hop
join groups nothing at all, and does so silently**, because on a simple order
the leg id and the parent id coincide. This module sees the consequence rather
than the cause: without ``legs[]`` every proposal has nothing to check, so it
declines with :attr:`DeclineRule.NO_LEGS` rather than quietly producing a
portfolio of singles.

Five rules this module holds itself to
--------------------------------------

**Silent rejection is a bug.** Every proposal this module declines to make live
is returned in :attr:`GroupingResult.declined` *and* logged with the rule, the
inputs, and the evidence's timestamp. You will need this the first time a
spread you hold renders as two rows.

**Signs are preserved, never normalised.** A short leg reports ``qty: "-1"``
alongside ``side: "short"``, with ``cost_basis`` and ``market_value`` both
negative. A credit is a liability — the same rule ``web/src/lib/orders.ts``
states as a short's ``openUnitValue`` being negative. Nothing here takes an
absolute value.

**``multiplier`` is per contract, from the contracts endpoint, and is never
100 by default.** ``/v2/positions`` carries no multiplier field at all, so
there is nothing to fall back to; absent contract terms give
:attr:`PositionLeg.multiplier` of ``None`` and the symbol is surfaced in
:attr:`GroupingResult.unknown_multipliers`. Adjusted contracts are why:
detection is ``root_symbol != underlying_symbol`` and every live adjusted
contract still reports ``multiplier: "100"``, so the root comparison is the
only test that works.

**A broker position row belongs to exactly one logical position, wholly.** The
broker aggregates per OCC symbol and does not split by order, so splitting a
row across two groups would mean pro-rating a cost basis nobody sent. When two
proposals want the same symbol the older one takes it and the later is declined
with :attr:`DeclineRule.LEG_ALREADY_GROUPED` — visibly, not silently.

**Corollary's own five ``Position`` fields are absent, and in this phase that
is correct rather than a gap.** Nothing Corollary opened exists, so
``strategyId`` and ``openedByStrategyId`` are null, ``managedExit`` and
``attachedExit`` are null, every position is legitimately detached, and
``valueHistory`` comes from the contract's daily bars between the opening
fill's date and today — from ``fill``, not from the position, and not from
here. They are not modelled at all rather than modelled as ``None``, because a
field that exists invites a value.

Pure: no network, no database, no clock. Identical inputs give identical
output, which is the same standard the scanner is held to.
"""

import logging
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from math import gcd
from types import MappingProxyType
from typing import Final

from corollary.data.providers.interface import OptionContract
from corollary.engine.execution.interface import (
    BrokerPosition,
    Order,
    OrderClass,
    PositionIntent,
    PositionSide,
)
from corollary.instruments import OccSymbol, OptionType, parse_occ_symbol

__all__ = [
    "DeclineRule",
    "GroupingResult",
    "LogicalPosition",
    "PositionKind",
    "PositionLeg",
    "ProposalDeclined",
    "RiskClass",
    "group_positions",
]

logger = logging.getLogger(__name__)


#: The two intents an *opening* leg carries, and the side each one leaves in
#: the book. A leg whose intent is ``*_to_close`` opened nothing, so an order
#: made only of those proposes no group — it closed one.
_OPENING_SIDE: Final[Mapping[PositionIntent, PositionSide]] = MappingProxyType(
    {
        PositionIntent.BUY_TO_OPEN: PositionSide.LONG,
        PositionIntent.SELL_TO_OPEN: PositionSide.SHORT,
    }
)


class PositionKind(StrEnum):
    """Whether an ``mleg`` order proved this position, or nothing did.

    ``ungrouped`` is the label the design spec names, spelled exactly, so a
    row renders without translation.
    """

    MULTI_LEG = "multi_leg"
    UNGROUPED = "ungrouped"


class RiskClass(StrEnum):
    """Which of CLAUDE.md rule 4's three definitions of "risk" applies.

    A **structural** classification and nothing more — it says *which* rule
    computes max loss, never what the number is. The number belongs to the
    risk manager in Phase 6, which does not exist yet.

    The classification is deliberately conservative: coverage is only counted
    within one ``(option_type, expiration)`` bucket, so a short near-dated leg
    against a long far-dated one is :attr:`UNDEFINED` rather than
    :attr:`DEFINED`. Over-stating risk is the safe direction; under-stating it
    is how the risk manager sizes against a loss that can exceed the number it
    was given.
    """

    #: Rule 4: *"Long options — premium paid."* No short leg anywhere.
    LONG_PREMIUM = "long_premium"
    #: Rule 4: *"Defined-risk structures — maximum loss at expiry."* Every
    #: short leg is covered by a long of the same right and expiry.
    DEFINED = "defined"
    #: Rule 4: *"Undefined-risk structures — a stress loss computed at ±2σ."*
    #: An uncovered short. A naked short has no maximum loss to quote, and a
    #: confident wrong number under the word "risk" is worse than an honest
    #: absence.
    UNDEFINED = "undefined"


class DeclineRule(StrEnum):
    """Why a proposed group was not made live.

    Every one of these is logged and returned. The rule name *is* the
    explanation — if the bot renders a spread as two rows, this is the field
    that says which check said no.
    """

    #: The proposal carries no ``legs[]``. On a recorded history this means
    #: the orders were fetched without ``?nested=true`` — a one-hop join,
    #: which groups nothing at all.
    NO_LEGS = "no_legs"
    #: Cancelled, rejected, or still working. It opened nothing.
    NOT_FILLED = "not_filled"
    #: Every leg must carry ``buy_to_open`` or ``sell_to_open``. An order made
    #: of closing legs closed a spread; re-proposing it would re-open one that
    #: is gone.
    NOT_AN_OPENING_ORDER = "not_an_opening_order"
    #: Two legs on one symbol. Ratios arrive in simplest form, so those would
    #: already have been combined — a second row is a parse error, and
    #: ``mleg_leg``'s ``(group_id, symbol)`` primary key says the same thing.
    DUPLICATE_LEG_SYMBOL = "duplicate_leg_symbol"
    #: A ``ratio_qty`` that is absent, zero, negative or fractional. A
    #: fractional ratio is not a spread.
    RATIO_NOT_WHOLE = "ratio_not_whole"
    #: The GCD across the legs' ratios is not 1. Alpaca requires simplest
    #: form, so a 2:2 order is a 1:1 order this code misread — and acting on
    #: it would size the group wrong.
    RATIOS_NOT_SIMPLEST = "ratios_not_simplest"
    #: A leg is no longer held. The spread genuinely no longer exists; its
    #: survivors fall back to ungrouped rows.
    LEG_NOT_HELD = "leg_not_held"
    #: A leg is held on the opposite side from the one the order opened.
    SIDE_MISMATCH = "side_mismatch"
    #: The held quantities are not a whole multiple of the ratios, or the
    #: legs disagree on how many copies of the structure are held.
    RATIO_MISMATCH = "ratio_mismatch"
    #: An older proposal already claimed one of these symbols.
    LEG_ALREADY_GROUPED = "leg_already_grouped"


#: Which declines are ordinary lifecycle and which mean something is off.
#:
#: A cancelled order and a closed spread are declined on every refresh and
#: forever after, so warning on those would bury the ones that matter under
#: their own repetition. The rest each mean a position you hold is being
#: rendered with the wrong risk class, which is worth a warning every time.
_DECLINE_LEVEL: Final[Mapping[DeclineRule, int]] = MappingProxyType(
    {
        DeclineRule.NOT_FILLED: logging.INFO,
        DeclineRule.NOT_AN_OPENING_ORDER: logging.INFO,
        DeclineRule.LEG_NOT_HELD: logging.INFO,
    }
)


# --------------------------------------------------------------------------
# The pieces of a logical position
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionLeg:
    """One contract inside a logical position.

    Carries the broker's own row rather than copying its numbers, so every
    field the row has — ``current_price``, ``unrealized_pl``, ``change_today``
    — stays reachable and no sign is restated anywhere it could be restated
    wrongly.
    """

    #: The broker row, signs preserved exactly as sent.
    position: BrokerPosition
    #: The five facts the OCC symbol encodes.
    occ: OccSymbol
    #: This leg's proportional quantity within the group, in simplest form.
    #: ``1`` on an ungrouped single leg.
    ratio: int
    #: The underlying, from the contracts endpoint where supplied. Falls back
    #: to the symbol's OCC root, which is the same string **except on an
    #: adjusted contract** — ``AAPL1`` has an underlying of ``AAPL``.
    underlying: str
    #: Contracts per unit of premium, from ``/v2/options/contracts``, cached.
    #: ``None`` means the terms were not supplied — **never 100**, which is
    #: the number that is wrong on exactly the contracts that matter.
    multiplier: Decimal | None
    #: ``root_symbol != underlying_symbol``. ``None`` when the terms were not
    #: supplied: unknown is a third answer and must not collapse into
    #: ``False``, because asking a question the data cannot answer and getting
    #: "no" is how an adjusted contract gets sized as a standard one.
    is_adjusted: bool | None
    #: The intent the opening order carried for this leg. ``None`` on an
    #: ungrouped leg, where no order was joined.
    opened_as: PositionIntent | None

    @property
    def symbol(self) -> str:
        return self.position.symbol

    @property
    def side(self) -> PositionSide:
        return self.position.side

    @property
    def is_short(self) -> bool:
        return self.position.is_short

    @property
    def quantity(self) -> Decimal:
        """Signed: negative on a short."""
        return self.position.quantity

    @property
    def cost_basis(self) -> Decimal:
        """Signed. Negative on a short, because the credit received is a debt."""
        return self.position.cost_basis

    @property
    def market_value(self) -> Decimal:
        """Signed, same reason."""
        return self.position.market_value

    @property
    def strike(self) -> Decimal:
        return self.occ.strike

    @property
    def option_type(self) -> OptionType:
        return self.occ.option_type

    @property
    def expiration(self) -> date:
        return self.occ.expiration


@dataclass(frozen=True, slots=True)
class LogicalPosition:
    """One position as a trader holds it: a single contract, or a structure.

    The five fields Corollary would own — ``strategyId``,
    ``openedByStrategyId``, ``managedExit``, ``attachedExit`` and
    ``valueHistory`` — are **not here**, and that is correct for this phase
    rather than a gap. See the module docstring.
    """

    kind: PositionKind
    legs: tuple[PositionLeg, ...]
    #: Debit long, credit short — read from the order's signed net price.
    #: ``None`` when the order reported no fill price, or reported exactly
    #: zero: a costless structure is neither, and a direction invented to fill
    #: the field would be the invented value that picks the risk class.
    direction: PositionSide | None
    risk_class: RiskClass
    underlying: str
    #: The **nearest** leg expiration, which is the one that governs: it is
    #: the date on which the structure first changes shape. See
    #: :attr:`expirations` for all of them.
    expiry: date
    #: How many copies of the structure are held. A 1:1 vertical held two deep
    #: is ``units == 2`` with both ratios still ``1``.
    units: int
    #: The parent ``mleg`` order that proves this group. ``None`` when
    #: ungrouped — which is also what makes ungrouped legs unattributable to
    #: an opening order, deliberately: attributing one would be an inference.
    order_id: str | None
    #: The parent order's fill time. ``None`` when ungrouped; the entry date
    #: for a single leg comes from ``fill``, not from the position.
    opened_at: datetime | None
    #: The parent order's average fill price, **signed**. Negative is a
    #: credit. ``None`` when ungrouped or unfilled.
    net_price: Decimal | None

    @property
    def id(self) -> str:
        """Stable across refreshes: the parent order id, or the OCC symbol.

        Both are facts about the position rather than a position in a list, so
        an expanded row stays expanded when the poll returns.
        """
        return self.order_id if self.order_id is not None else self.legs[0].symbol

    @property
    def is_grouped(self) -> bool:
        return self.kind is PositionKind.MULTI_LEG

    @property
    def cost_basis(self) -> Decimal:
        """The net of the legs, signed. A credit spread's basis is negative."""
        return sum((leg.cost_basis for leg in self.legs), Decimal(0))

    @property
    def market_value(self) -> Decimal:
        """The net of the legs, signed."""
        return sum((leg.market_value for leg in self.legs), Decimal(0))

    @property
    def unrealized_pl(self) -> Decimal | None:
        """``None`` if any leg is missing one — a partial sum is not a P&L."""
        total = Decimal(0)
        for leg in self.legs:
            if leg.position.unrealized_pl is None:
                return None
            total += leg.position.unrealized_pl
        return total

    @property
    def expirations(self) -> tuple[date, ...]:
        """Every distinct leg expiration, nearest first. One on a vertical."""
        return tuple(sorted({leg.expiration for leg in self.legs}))


@dataclass(frozen=True, slots=True)
class ProposalDeclined:
    """One ``mleg`` order that did not become a live group, and why.

    The timestamp is the **evidence's** — the parent order's fill time — not
    the wall clock, because this module is pure and a wall clock would make
    identical inputs give different output. The log record carries emission
    time alongside it, which is where a wall clock belongs.
    """

    rule: DeclineRule
    #: The parent ``mleg`` order id. It is also the correlation handle for
    #: this decision: the same id keys ``mleg_group``, and the fills that
    #: opened the structure reach it through the two-hop join.
    order_id: str
    at: datetime
    detail: str
    #: The values the rule read, so the decision can be re-derived from the
    #: log alone.
    inputs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GroupingResult:
    """Logical positions, plus everything the grouper decided not to do."""

    #: One entry per logical position, in the order the broker listed the
    #: rows: a logical position takes the position of its earliest leg.
    positions: tuple[LogicalPosition, ...]
    #: Every proposal that did not become live, oldest evidence first.
    declined: tuple[ProposalDeclined, ...]
    #: Held symbols whose contract terms were not supplied, so whose
    #: multiplier is unknown. Surfaced rather than defaulted.
    unknown_multipliers: tuple[str, ...]
    #: Held symbols where ``root_symbol != underlying_symbol``. The
    #: deliverable is not 100 shares and sizing must read the deliverables.
    adjusted_contracts: tuple[str, ...]
    #: Position rows that are not option contracts, mapped to the reason.
    #: Shares delivered by an exercise or an assignment land here: Corollary
    #: is not a stock app, so they are *named*, not tracked — but they are
    #: never dropped without saying so.
    excluded: Mapping[str, str]

    @property
    def groups(self) -> tuple[LogicalPosition, ...]:
        return tuple(
            logical for logical in self.positions if logical.is_grouped
        )

    @property
    def ungrouped(self) -> tuple[LogicalPosition, ...]:
        return tuple(
            logical for logical in self.positions if not logical.is_grouped
        )


# --------------------------------------------------------------------------
# The grouper
# --------------------------------------------------------------------------


def group_positions(
    positions: Sequence[BrokerPosition],
    orders: Sequence[Order] = (),
    *,
    contracts: Mapping[str, OptionContract] | None = None,
) -> GroupingResult:
    """Assemble logical positions from broker rows and ``mleg`` order history.

    ``orders`` must be the **nested** form — ``GET /v2/orders?nested=true`` —
    or no ``mleg`` parent carries the ``legs[]`` this reads, and nothing
    groups. That failure is reported rather than silent; see
    :attr:`DeclineRule.NO_LEGS`.

    ``contracts`` is the per-symbol reference data from
    ``/v2/options/contracts``, cached by the caller. Omitting it is allowed
    and leaves every multiplier ``None``; it never substitutes 100.

    Passing every order ever placed is correct and costs a decline record per
    dead ``mleg`` order. A caller that windows the history gets the same
    groups with a shorter :attr:`GroupingResult.declined`.

    Raises ``ValueError`` on a response that cannot be true: two rows naming
    one symbol — the broker keys on the OCC symbol, so merging them would
    double a position — or a fractional contract quantity. Both are refused
    rather than reconciled, for the same reason the broker parser refuses a
    row whose ``qty`` and ``side`` disagree: either repair would mis-state a
    position and there is no way to tell which half is wrong.
    """
    terms: Mapping[str, OptionContract] = contracts if contracts is not None else {}

    held: dict[str, BrokerPosition] = {}
    occ_by_symbol: dict[str, OccSymbol] = {}
    row_order: dict[str, int] = {}
    excluded: dict[str, str] = {}

    for index, position in enumerate(positions):
        symbol = position.symbol
        if symbol in held or symbol in excluded:
            raise ValueError(
                f"two position rows for {symbol!r}. The broker keys on the "
                "OCC symbol, so this is not a response it sends; merging them "
                "would double a position and refusing is the only safe answer."
            )
        try:
            occ = parse_occ_symbol(symbol)
        except ValueError as exc:
            reason = (
                f"not an option contract (asset_class "
                f"{position.asset_class!r}): {exc}"
            )
            excluded[symbol] = reason
            logger.warning(
                "position excluded from logical grouping: %s",
                symbol,
                extra={
                    "event": "grouping_position_excluded",
                    "symbol": symbol,
                    "reason": reason,
                },
            )
            continue
        held[symbol] = position
        occ_by_symbol[symbol] = occ
        row_order[symbol] = index

    # Oldest evidence first, tie-broken on the id, so the output does not
    # depend on the order the history arrived in.
    proposals = sorted(
        (order for order in orders if order.order_class is OrderClass.MLEG),
        key=lambda order: (order.filled_at or order.created_at, order.id),
    )

    logical: list[LogicalPosition] = []
    declined: list[ProposalDeclined] = []
    claimed: set[str] = set()

    for order in proposals:
        outcome = _evaluate(order, held, occ_by_symbol, claimed, terms)
        if isinstance(outcome, ProposalDeclined):
            declined.append(outcome)
            _log_decline(outcome)
            continue
        logical.append(outcome)
        claimed.update(leg.symbol for leg in outcome.legs)

    for symbol, position in held.items():
        if symbol in claimed:
            continue
        logical.append(_single(position, occ_by_symbol[symbol], terms))

    logical.sort(key=lambda item: min(row_order[leg.symbol] for leg in item.legs))

    every_leg = [leg for item in logical for leg in item.legs]
    unknown = tuple(leg.symbol for leg in every_leg if leg.multiplier is None)
    adjusted = tuple(leg.symbol for leg in every_leg if leg.is_adjusted is True)
    _log_contract_gaps(unknown, adjusted)

    return GroupingResult(
        positions=tuple(logical),
        declined=tuple(declined),
        unknown_multipliers=unknown,
        adjusted_contracts=adjusted,
        excluded=MappingProxyType(excluded),
    )


def _evaluate(
    order: Order,
    held: Mapping[str, BrokerPosition],
    occ_by_symbol: Mapping[str, OccSymbol],
    claimed: Container[str],
    terms: Mapping[str, OptionContract],
) -> LogicalPosition | ProposalDeclined:
    """One proposal: well-formed, and still live? Or the rule that said no."""
    at = order.filled_at or order.created_at

    def decline(
        rule: DeclineRule, detail: str, **inputs: str
    ) -> ProposalDeclined:
        return ProposalDeclined(
            rule=rule,
            order_id=order.id,
            at=at,
            detail=detail,
            inputs=MappingProxyType(dict(inputs)),
        )

    # First, because it is the one whose cause is invisible: an order fetched
    # without `nested=true` looks exactly like one that has no legs.
    if not order.legs:
        return decline(
            DeclineRule.NO_LEGS,
            "an mleg order carries no legs[]. Either this history was "
            "fetched without ?nested=true — in which case the join is "
            "one-hop and nothing will ever group — or this row is a leg "
            "rather than a parent.",
            order_class=order.order_class.value,
            status=order.status,
            leg_count="0",
        )

    if order.filled_quantity <= 0:
        return decline(
            DeclineRule.NOT_FILLED,
            "the order never filled, so it opened nothing",
            status=order.status,
            filled_qty=str(order.filled_quantity),
        )

    symbols = [leg.symbol for leg in order.legs]
    if len(set(symbols)) != len(symbols):
        return decline(
            DeclineRule.DUPLICATE_LEG_SYMBOL,
            "two legs name one symbol; ratios arrive in simplest form so "
            "those would already have been combined",
            symbols=",".join(symbols),
        )

    expected_sides: list[PositionSide] = []
    ratios: list[int] = []
    for leg in order.legs:
        side = _OPENING_SIDE.get(leg.position_intent) if leg.position_intent else None
        if side is None:
            return decline(
                DeclineRule.NOT_AN_OPENING_ORDER,
                "a leg carries no opening intent, so this order did not open "
                "the structure it describes",
                symbol=leg.symbol,
                position_intent=(
                    leg.position_intent.value
                    if leg.position_intent is not None
                    else "none"
                ),
            )
        expected_sides.append(side)

        raw_ratio = leg.ratio_qty
        if (
            raw_ratio is None
            or raw_ratio <= 0
            or raw_ratio != raw_ratio.to_integral_value()
        ):
            return decline(
                DeclineRule.RATIO_NOT_WHOLE,
                "a leg ratio is absent, not positive, or fractional",
                symbol=leg.symbol,
                ratio_qty="none" if raw_ratio is None else str(raw_ratio),
            )
        ratios.append(int(raw_ratio))

    if gcd(*ratios) != 1:
        return decline(
            DeclineRule.RATIOS_NOT_SIMPLEST,
            "leg ratios are not in simplest form; Alpaca requires the GCD "
            "across them to be 1, so this order was misread",
            ratios=":".join(str(ratio) for ratio in ratios),
        )

    unit_counts: list[int] = []
    legs: list[PositionLeg] = []
    for leg, expected, ratio in zip(order.legs, expected_sides, ratios, strict=True):
        if leg.symbol in claimed:
            return decline(
                DeclineRule.LEG_ALREADY_GROUPED,
                "an older mleg order already claimed this symbol; a broker "
                "row belongs to exactly one logical position",
                symbol=leg.symbol,
            )

        position = held.get(leg.symbol)
        if position is None or position.quantity == 0:
            return decline(
                DeclineRule.LEG_NOT_HELD,
                "a leg is no longer held, so this spread no longer exists",
                symbol=leg.symbol,
                held="0" if position is None else str(position.quantity),
            )

        if position.side is not expected:
            return decline(
                DeclineRule.SIDE_MISMATCH,
                "a leg is held on the opposite side from the one this order "
                "opened",
                symbol=leg.symbol,
                opened=expected.value,
                held=position.side.value,
            )

        magnitude = abs(position.quantity)
        units = magnitude / ratio
        if units != units.to_integral_value():
            return decline(
                DeclineRule.RATIO_MISMATCH,
                "the held quantity is not a whole multiple of the leg ratio",
                symbol=leg.symbol,
                held=str(position.quantity),
                ratio=str(ratio),
            )
        unit_counts.append(int(units))
        legs.append(
            _leg(
                position,
                occ_by_symbol[leg.symbol],
                ratio,
                terms,
                leg.position_intent,
            )
        )

    if len(set(unit_counts)) != 1:
        return decline(
            DeclineRule.RATIO_MISMATCH,
            "the legs disagree on how many copies of the structure are held, "
            "so the quantities are not consistent with the order's ratios",
            held=",".join(
                f"{leg.symbol}={leg.quantity}x1/{leg.ratio}" for leg in legs
            ),
            units=",".join(str(count) for count in unit_counts),
        )

    return LogicalPosition(
        kind=PositionKind.MULTI_LEG,
        legs=tuple(legs),
        direction=_direction(order.net_price),
        risk_class=_risk_class(legs),
        underlying=legs[0].underlying,
        expiry=min(leg.expiration for leg in legs),
        units=unit_counts[0],
        order_id=order.id,
        opened_at=at,
        net_price=order.net_price,
    )


def _single(
    position: BrokerPosition,
    occ: OccSymbol,
    terms: Mapping[str, OptionContract],
) -> LogicalPosition:
    """One contract nothing explains: a position labelled ``ungrouped``.

    Its direction is the broker's own ``side`` rather than a net price — there
    is no order joined to it, and a single contract's side is not in doubt.
    """
    leg = _leg(position, occ, 1, terms, None)
    return LogicalPosition(
        kind=PositionKind.UNGROUPED,
        legs=(leg,),
        direction=position.side,
        risk_class=_risk_class([leg]),
        underlying=leg.underlying,
        expiry=leg.expiration,
        units=_whole_contracts(position),
        order_id=None,
        opened_at=None,
        net_price=None,
    )


def _leg(
    position: BrokerPosition,
    occ: OccSymbol,
    ratio: int,
    terms: Mapping[str, OptionContract],
    opened_as: PositionIntent | None,
) -> PositionLeg:
    contract = terms.get(position.symbol)
    return PositionLeg(
        position=position,
        occ=occ,
        ratio=ratio,
        underlying=contract.underlying_symbol if contract is not None else occ.root,
        multiplier=contract.multiplier if contract is not None else None,
        is_adjusted=contract.is_adjusted if contract is not None else None,
        opened_as=opened_as,
    )


def _whole_contracts(position: BrokerPosition) -> int:
    """``abs(qty)`` as a count. Options are whole contracts; refuse anything else.

    Truncating here would understate the size of a position, which is the
    wrong direction to be wrong in. The broker parser already refuses a row
    whose ``qty`` and ``side`` disagree, for the same reason.
    """
    magnitude = abs(position.quantity)
    if magnitude != magnitude.to_integral_value():
        raise ValueError(
            f"position {position.symbol!r} reports a fractional quantity "
            f"{position.quantity}; option contracts are whole."
        )
    return int(magnitude)


def _direction(net_price: Decimal | None) -> PositionSide | None:
    """Debit long, credit short. ``None`` when the order does not say.

    Zero is not a third direction, it is the absence of one: a costless
    structure is neither a debit nor a credit, and naming it either would be
    the substituted value that picks the risk class.
    """
    if net_price is None or net_price == 0:
        return None
    return PositionSide.LONG if net_price > 0 else PositionSide.SHORT


def _risk_class(legs: Sequence[PositionLeg]) -> RiskClass:
    """Which of rule 4's three definitions applies to this structure.

    A short leg's loss is bounded when a long leg of the **same right and the
    same expiry** stands against at least as many contracts: at expiry the
    pair pays ``±|K_long − K_short|`` whichever way the strikes sit, so the
    loss has a maximum and rule 4's defined-risk definition applies. Anything
    else — an uncovered short, a ratio spread's extra short, a short leg whose
    cover expires on a different date — is reported undefined.

    Strikes deliberately do not enter the test. A long call above or below a
    short call at one expiry bounds the loss either way, so requiring an
    ordering would call half of every vertical undefined.
    """
    shorts: dict[tuple[OptionType, date], int] = {}
    longs: dict[tuple[OptionType, date], int] = {}
    for leg in legs:
        bucket = (leg.option_type, leg.expiration)
        target = shorts if leg.is_short else longs
        target[bucket] = target.get(bucket, 0) + leg.ratio

    if not shorts:
        return RiskClass.LONG_PREMIUM
    for bucket, short_ratio in shorts.items():
        if longs.get(bucket, 0) < short_ratio:
            return RiskClass.UNDEFINED
    return RiskClass.DEFINED


# --------------------------------------------------------------------------
# Logging — the rule, the inputs, the timestamp
# --------------------------------------------------------------------------


def _log_decline(decision: ProposalDeclined) -> None:
    """Rule 8, applied to a grouping decision rather than an order.

    ``order_id`` is the correlation handle: it is what ``mleg_group`` keys on
    and what the two-hop join reaches from a fill, so one id traces a
    structure from its fills through this decision to what renders.
    """
    logger.log(
        _DECLINE_LEVEL.get(decision.rule, logging.WARNING),
        "mleg group declined (%s): %s",
        decision.rule.value,
        decision.detail,
        extra={
            "event": "mleg_group_declined",
            "rule": decision.rule.value,
            "order_id": decision.order_id,
            "order_at": decision.at.isoformat(),
            "detail": decision.detail,
            **{f"input_{key}": value for key, value in decision.inputs.items()},
        },
    )


def _log_contract_gaps(
    unknown_multipliers: tuple[str, ...], adjusted: tuple[str, ...]
) -> None:
    if unknown_multipliers:
        logger.warning(
            "%d held contracts have no multiplier: contract terms were not "
            "supplied, and /v2/positions carries no multiplier field",
            len(unknown_multipliers),
            extra={
                "event": "grouping_multiplier_unknown",
                "symbols": list(unknown_multipliers),
            },
        )
    if adjusted:
        logger.warning(
            "%d held contracts are adjusted: root_symbol != underlying_symbol, "
            "so the deliverable is not 100 shares and the reported multiplier "
            "does not say so",
            len(adjusted),
            extra={
                "event": "grouping_adjusted_contract",
                "symbols": list(adjusted),
            },
        )
