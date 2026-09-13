"""The 30-symbol subscription budget -- pure. Desired symbols in, a plan out.

Alpaca's Basic plan caps a websocket stream at **30 symbols**, and every option
contract is its own symbol. Grouped multi-leg reaches 32 option symbols at
eight positions -- the concurrent-position ceiling this account actually runs
-- before a single underlying is counted. So the cap is not a footnote that a
large account might one day meet; it is met by a *full but ordinary* book, and
something has to lose a slot on a routine morning.

This module decides what. It is the arbiter, not the client: it opens no
socket, imports no vendor, reads no clock and reads no configuration.
``alpaca`` is imported in exactly two files and neither is this one -- what
comes back is a list of symbols a transport is then told to subscribe to, so
swapping vendors changes the transport and leaves the policy alone. Identical
inputs give identical output, which is the standard the scanner is held to and
matters here for a second reason: a plan that reshuffles between polls churns
the socket, and every resubscribe is a gap in the marks it was opened to
deliver.

**The failure this prevents is a silent one.** A stream that quietly accepts 30
of the 34 symbols it was handed, or that accepts all 34 and is silently
truncated by the server, leaves four contracts marking at their last known
price. Nothing errors. The position rows keep rendering, the P&L keeps
totalling, and four of the numbers in it are stale with nothing on screen to
say which. That is worse than a missing row: a stale price looks exactly like a
quiet market. So every symbol that does not make the cut is returned in
:attr:`SubscriptionPlan.dropped`, counted in
:attr:`SubscriptionPlan.not_streamed`, phrased for the UI in
:attr:`SubscriptionPlan.message`, and logged with the rule, the inputs and the
timestamp -- rule 8, which is about rejected *orders* but states a standard, and
a dropped subscription is a rejection with the same shape.

Priority, and what a slot is spent on
-------------------------------------

Three tiers, ordered, from the design spec: **position contracts**, then their
**underlyings**, then **recommended trades** (Phase 4). The third has no
producer yet and is structurally present rather than speculative -- it costs one
enum member and one constructor, and leaving it out would mean the Phase 4
change is to this module's *logic* rather than to its inputs.

Within a tier the caller's order is preserved exactly. The sort is stable and
nothing here re-orders, so two contracts at the same priority keep the sequence
they arrived in and the same book produces the same plan every poll.

Two decisions that could have gone the other way, stated rather than implied
---------------------------------------------------------------------------

**A logical position's legs are all-or-nothing.** A unit is admitted whole or
refused whole; the budget never takes three legs of an iron condor and leaves
the fourth. Splitting is tempting because it uses the last slots, and it is
wrong for the reason the mixed feed is wrong: a position's market value is the
sum of its legs, so three live legs and one stale one produce a net that
*ticks* -- and therefore reads as live -- while being neither the live figure
nor the stale one. A missing position is visible; a position that is quietly
three-quarters live is not. :func:`contract_unit` takes a whole logical
position's symbols for exactly this reason.

**The drop is a strict prefix cut -- on slot consumption, not on units.**
Spending stops at the first unit that does not fit, and every unit behind it
that needs a *new* symbol is refused with :attr:`DropRule.BEHIND_A_DROP`,
including units small enough to have fitted in the slots the refused one left
behind. Backfilling would use the budget more fully and buy two problems. It
inverts the priority order -- an underlying streaming while a contract you
*hold* does not -- and it makes the plan depend on the *sizes* of the things
ahead of it, so adding a leg to one spread would silently evict an unrelated
symbol and admit a different one, which is exactly the churn the determinism
requirement exists to prevent. The unused slots are not hidden either:
:attr:`SubscriptionPlan.spare_capacity` reports them. What the cut does *not*
do is refuse a unit that spends nothing, which is the next section.

Deduplication is free and does not move anything
------------------------------------------------

A symbol already admitted costs nothing the second time, whichever tier asks
for it. An underlying named by three positions takes one slot, and a Phase 4
recommendation on a contract already held is admitted at zero cost rather than
competing for a slot it does not need. Dedup never reorders: a unit's position
in the priority sequence is fixed before any of this, and a symbol's place in
:attr:`SubscriptionPlan.subscribed` is where it was *first* admitted.

**This holds behind a drop too.** A unit whose every symbol is already
streaming takes no slot, so admitting it neither inverts the priority order nor
makes the plan depend on the sizes of the units ahead of it -- the two things
the prefix cut exists to prevent -- because it cannot deprive anything of a
budget it does not touch. Refusing it would report a position whose every leg
is marking live as *not streamed*, which is the module's opening failure
pointed the other way. Two logical positions on one contract is an ordinary
state: a roll in flight, or two lots of the same strike.

The cap is one number, and where it comes from is the caller's problem
---------------------------------------------------------------------

:data:`STREAM_SYMBOL_CAP` is the only place 30 is written, and
:func:`plan_subscriptions` takes ``cap`` as a parameter defaulting to it, so an
upgrade is one value changing rather than a refactor.

It is **not** read from the environment here, and it is not the same shape as
the three feed-name variables: those are genuinely read from
``ALPACA_OPTIONS_FEED`` / ``ALPACA_STOCK_FEED_HISTORICAL`` /
``ALPACA_STOCK_FEED_REALTIME`` inside the provider, whereas this is a literal
with an override, and calling the two the same thing would claim a property
this module does not have. The account's plan of record is ``ALPACA_DATA_PLAN``
(see ``api/routes/settings.py``), and **deriving the cap from it belongs to the
caller** -- ``engine/runtime.py``, which owns the plan lookup and the
environment. Whoever writes that caller: on Algo Trader Plus the thirty-symbol
limit does not exist, and a stream still cutting at 30 while running full OPRA
reports *"N symbols not streamed"* for a limit that was lifted. This module
cannot notice that, by design; it only knows the number it is handed.

The audit record is the caller's clock and the caller's id
----------------------------------------------------------

``at`` and ``correlation_id`` are required arguments rather than defaults. This
module reads no clock and mints no id, for two different reasons. A wall clock
would make identical inputs give different output, which is the one property
the subscription list has to have. An id minted here would be an id tied to
nothing: the point of it is that the poll or tick that triggered the re-plan
and the N+1 records the plan emits carry the *same* handle, so drop lines from
two interleaved plans can be told apart -- the role ``order_id`` plays in
``grouping``. ``at`` must be timezone-aware and is normalised to UTC; a naive
datetime is refused rather than guessed at, because guessing is a silent
four-or-five-hour error in a record whose whole purpose is to be read back
later against a fill.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Final

__all__ = [
    "STREAM_SYMBOL_CAP",
    "DropRule",
    "DroppedUnit",
    "SubscriptionPlan",
    "SubscriptionPriority",
    "SubscriptionUnit",
    "contract_unit",
    "plan_subscriptions",
    "recommendation_unit",
    "underlying_unit",
]

logger = logging.getLogger(__name__)

#: Alpaca Basic caps a websocket stream at thirty symbols, and every option
#: contract is one of them. Written once: :func:`plan_subscriptions` defaults
#: to it and takes an override. Deliberately not read from the environment --
#: see the module docstring on why deriving it from ``ALPACA_DATA_PLAN`` is
#: the caller's job rather than this module's.
STREAM_SYMBOL_CAP: Final[int] = 30


class SubscriptionPriority(IntEnum):
    """Who gets a slot first. Lower value wins; the order is the spec's.

    An :class:`~enum.IntEnum` so the sort key is the member itself and no
    parallel ranking table exists to fall out of step with it.
    """

    #: The contracts of positions actually held. A held contract with no mark
    #: is a P&L figure that is wrong rather than absent.
    POSITION_CONTRACT = 1
    #: The underlyings behind those positions -- the payoff curve's x-axis, and
    #: what a managed exit is evaluated against.
    POSITION_UNDERLYING = 2
    #: Phase 4. No producer yet; present so that wiring one is a new caller
    #: rather than a change to this module's logic.
    RECOMMENDED_TRADE = 3

    @property
    def label(self) -> str:
        """The value a log record and the API carry. Stable across renames."""
        return self.name.lower()


class DropRule(StrEnum):
    """Why a unit did not get its slots. One value per reason, never a message.

    A rule is a thing you can count, filter and alert on; a sentence is not --
    the same reasoning as ``ledger.RejectionRule`` and
    ``grouping.DeclineRule``.
    """

    #: The budget had room, but not enough of it for this unit's remaining
    #: symbols. Transient: it fits again when the book shrinks.
    NO_ROOM = "no_room"
    #: This unit's own distinct symbols outnumber the whole cap, so no book is
    #: small enough to make it fit. Not transient -- either the cap rises or
    #: this position is never streamed, and the difference is worth its own
    #: rule so an alert can tell them apart. Decided on
    #: :attr:`DroppedUnit.width`, never on :attr:`DroppedUnit.required`: what
    #: is *still needed* shrinks when a symbol is already streaming, so
    #: classifying on that labels a permanently oversized unit transient and
    #: leaves an operator waiting out a shortage that cannot clear.
    EXCEEDS_CAP = "exceeds_cap"
    #: A higher-priority unit was already refused, so spending stopped. See
    #: the module docstring on why the leftover slots are not backfilled. Only
    #: reachable for a unit that needs a symbol it does not already have: one
    #: that spends nothing is admitted behind a drop.
    BEHIND_A_DROP = "behind_a_drop"


@dataclass(frozen=True, slots=True)
class SubscriptionUnit:
    """One all-or-nothing request for stream slots.

    A unit is the granularity at which the budget says yes or no, and that is
    the point of the type: a four-leg spread is **one** unit, so it is admitted
    whole or refused whole and can never be left three-quarters marked. Build
    them with :func:`contract_unit`, :func:`underlying_unit` and
    :func:`recommendation_unit` rather than by hand, so the priority and the
    key convention stay in one place.
    """

    #: Identifies the thing that wanted the slots, for the log and the UI: a
    #: logical position's id, an underlying's ticker, a recommendation's id.
    #: Not required to be unique -- it is a label, not a lookup key.
    key: str
    priority: SubscriptionPriority
    #: Every symbol this unit needs marked, in the caller's order. Repeats are
    #: allowed and cost one slot; :func:`plan_subscriptions` refuses an empty
    #: tuple and an empty string, both of which are caller bugs.
    symbols: tuple[str, ...]


def contract_unit(position_id: str, symbols: Iterable[str]) -> SubscriptionUnit:
    """The contracts of one logical position -- **every** leg, in one unit.

    Pass a spread's legs together. Passing them as separate units would let the
    budget split the position, which is the one thing this module promises not
    to do; see the module docstring.
    """
    return SubscriptionUnit(
        key=position_id,
        priority=SubscriptionPriority.POSITION_CONTRACT,
        symbols=tuple(symbols),
    )


def underlying_unit(symbol: str) -> SubscriptionUnit:
    """One underlying. Keyed on the ticker, because that is what it is."""
    return SubscriptionUnit(
        key=symbol,
        priority=SubscriptionPriority.POSITION_UNDERLYING,
        symbols=(symbol,),
    )


def recommendation_unit(
    recommendation_id: str, symbols: Iterable[str]
) -> SubscriptionUnit:
    """A Phase 4 recommended trade. Same all-or-nothing rule as a position."""
    return SubscriptionUnit(
        key=recommendation_id,
        priority=SubscriptionPriority.RECOMMENDED_TRADE,
        symbols=tuple(symbols),
    )


@dataclass(frozen=True, slots=True)
class DroppedUnit:
    """One unit that did not get its slots, with everything needed to explain it.

    Rule 8's three parts, as fields rather than as prose: the **rule**
    (:attr:`rule`), the **inputs** (:attr:`symbols`, :attr:`required`,
    :attr:`width`, :attr:`remaining`, :attr:`cap`) and the **timestamp**
    (:attr:`at`). The plan that refused it carries the correlation id tying
    this record to the rest of that one decision.
    """

    key: str
    priority: SubscriptionPriority
    #: Every symbol the unit asked for, as it asked for it. Not trimmed to the
    #: ones that were missing -- the unit is the thing that was refused.
    symbols: tuple[str, ...]
    rule: DropRule
    detail: str
    #: Distinct symbols this unit still needed when it was refused, after
    #: discounting anything already admitted. Situational: it shrinks as the
    #: book ahead of this unit grows.
    required: int
    #: Distinct symbols this unit needs on an *empty* stream -- its own size,
    #: independent of everything else asked for. Separate from
    #: :attr:`required` because the two answer different questions, and only
    #: this one decides :attr:`DropRule.EXCEEDS_CAP`: a five-symbol unit
    #: sharing two symbols with an admitted one still needs five slots of its
    #: own, and is still unstreamable at a cap of four however far the book
    #: shrinks.
    width: int
    #: Slots left in the budget at that moment.
    remaining: int
    cap: int
    #: When the caller computed the plan, normalised to UTC. Required, because
    #: rule 8 wants a timestamp on every refusal and a record read back against
    #: a fill cannot be missing one. Supplied rather than read here: a clock
    #: inside this module would make identical inputs give different output.
    at: datetime


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    """What to subscribe to, and -- in the same breath -- what will not be.

    Both halves are named because only one of them is visible from the socket.
    A caller that reads :attr:`subscribed` and ignores :attr:`dropped` has
    built the silent truncation this module exists to prevent.
    """

    #: The symbols to subscribe, deduplicated, in priority order. Never longer
    #: than :attr:`cap`.
    subscribed: tuple[str, ...]
    #: The units that got their slots, in the same order. Includes units that
    #: needed no new symbol, which is what makes this the honest answer to
    #: *"which positions are marked live?"* -- see the module docstring on
    #: zero-cost admission.
    admitted: tuple[SubscriptionUnit, ...]
    #: Every unit that did not, in priority order. **Never truncated** -- a
    #: capped list of what was capped is the same bug one level up.
    dropped: tuple[DroppedUnit, ...]
    cap: int
    #: When the caller computed this plan, normalised to UTC.
    at: datetime
    #: The caller's handle for the decision that produced this plan. One plan
    #: emits one record per drop plus a summary and they are one decision;
    #: this is what says so when a 2s poll and a 400ms tick interleave.
    correlation_id: str

    @property
    def subscribed_set(self) -> frozenset[str]:
        return frozenset(self.subscribed)

    @property
    def dropped_symbols(self) -> tuple[str, ...]:
        """Distinct symbols that were asked for and are **not** being streamed.

        A symbol reaching a dropped unit *and* an admitted one is streaming, so
        it does not appear here. The distinction matters: counting dropped
        units' symbols would over-report whenever a Phase 4 recommendation
        names a contract already held, and an inflated "not streamed" figure
        teaches the reader to ignore it.
        """
        streaming = self.subscribed_set
        missing: dict[str, None] = {}
        for unit in self.dropped:
            for symbol in unit.symbols:
                if symbol not in streaming:
                    missing.setdefault(symbol, None)
        return tuple(missing)

    @property
    def not_streamed(self) -> int:
        """The N the UI renders as *"N symbols not streamed"*."""
        return len(self.dropped_symbols)

    @property
    def spare_capacity(self) -> int:
        """Slots the cap allows and this plan does not use.

        Nonzero alongside a nonempty :attr:`dropped` is normal, not a bug: the
        cut is a strict prefix, so a four-leg spread refused with two slots
        left leaves those two slots unused. Reported so that it is a stated
        consequence rather than an unexplained gap.
        """
        return self.cap - len(self.subscribed)

    @property
    def message(self) -> str | None:
        """The sentence for the UI, or ``None`` when everything is streaming.

        ``None`` rather than *"0 symbols not streamed"*: a banner that is
        always present is a banner nobody reads.
        """
        count = self.not_streamed
        if count == 0:
            return None
        return f"{count} symbol{'' if count == 1 else 's'} not streamed"


def plan_subscriptions(
    units: Iterable[SubscriptionUnit],
    *,
    at: datetime,
    correlation_id: str,
    cap: int = STREAM_SYMBOL_CAP,
) -> SubscriptionPlan:
    """Fit ``units`` into ``cap`` stream slots, and say what did not fit.

    Units are taken in priority order -- position contracts, then underlyings,
    then recommended trades -- with the caller's order preserved within each
    tier. Each is admitted whole or refused whole, a symbol already admitted
    costs nothing, and spending stops at the first refusal. The module
    docstring gives the reasoning for all three.

    ``at`` is the moment the caller computed the plan, carried onto every
    :class:`DroppedUnit` and into every log record. It is an argument rather
    than a clock read so that identical inputs still give identical output, it
    is required because rule 8 wants a timestamp on every refusal, and it must
    be timezone-aware -- a naive datetime is refused rather than assumed to
    mean anything.

    ``correlation_id`` is the caller's handle for the decision that triggered
    this plan, threaded through every record it emits so that N+1 lines read as
    the one decision they are. Supplied rather than minted here for the same
    reason as ``at``: an id this module invented would tie the plan to nothing
    upstream of it.

    Raises ``ValueError`` on a negative ``cap``, a naive ``at``, an empty
    ``correlation_id``, a unit with no symbols, and an empty symbol string. All
    five are caller bugs that would otherwise fail quietly -- an empty unit is
    admitted for free and streams nothing, and an empty symbol would be sent to
    the transport as a subscription.
    """
    if cap < 0:
        raise ValueError(
            f"a stream symbol cap of {cap} is not a budget; the cap is a count "
            "of slots and cannot be negative"
        )
    moment = _utc(at)
    if not correlation_id:
        raise ValueError(
            "a subscription plan needs a correlation id; an empty one puts a "
            "field in every record that correlates nothing, which is the "
            "untraceable plan it exists to prevent"
        )

    ordered = _ordered(units)

    subscribed: dict[str, None] = {}
    admitted: list[SubscriptionUnit] = []
    dropped: list[DroppedUnit] = []
    stopped = False

    for unit in ordered:
        remaining = cap - len(subscribed)
        wanted = _new_symbols(unit, subscribed)
        required = len(wanted)
        # The unit's own size, not what is left of it. `required` shrinks when
        # something ahead of this unit already streams a symbol it shares;
        # `width` does not, and `width` is what EXCEEDS_CAP is decided on. A
        # set is safe where a dict is used elsewhere because a count has no
        # order to be nondeterministic about.
        width = len(set(unit.symbols))

        if required == 0:
            # Every symbol is already streaming, so this spends nothing. It is
            # admitted even behind a drop: it takes nothing from the budget,
            # so it can neither invert priority nor make the plan depend on
            # the sizes ahead of it, and refusing it would report a position
            # whose every leg is marked live as not streamed.
            admitted.append(unit)
            continue

        if stopped:
            dropped.append(
                _drop(
                    unit,
                    DropRule.BEHIND_A_DROP,
                    "a higher-priority subscription was already dropped, so "
                    "spending stopped here. Filling its leftover slots from "
                    "further down the list would stream a lower-priority "
                    "symbol while a higher-priority one goes unmarked",
                    required=required,
                    width=width,
                    remaining=remaining,
                    cap=cap,
                    at=moment,
                )
            )
            continue

        if required <= remaining:
            for symbol in wanted:
                subscribed[symbol] = None
            admitted.append(unit)
            continue

        if width > cap:
            rule = DropRule.EXCEEDS_CAP
            detail = (
                f"this subscription's {width} distinct symbols outnumber the "
                f"whole budget of {cap}, so it does not fit an empty stream "
                f"either and no shrinking of the book will change that "
                f"({required} of them are not already streaming). It is "
                "dropped whole rather than split: a partly-marked position "
                "nets a value that ticks and is wrong"
            )
        else:
            rule = DropRule.NO_ROOM
            detail = (
                f"{required} symbols are needed and {remaining} slots are "
                f"left of {cap}. The unit is dropped whole rather than split, "
                "because a position marked on some legs and stale on others "
                "reports a net that looks live and is not"
            )

        dropped.append(
            _drop(
                unit,
                rule,
                detail,
                required=required,
                width=width,
                remaining=remaining,
                cap=cap,
                at=moment,
            )
        )
        stopped = True

    plan = SubscriptionPlan(
        subscribed=tuple(subscribed),
        admitted=tuple(admitted),
        dropped=tuple(dropped),
        cap=cap,
        at=moment,
        correlation_id=correlation_id,
    )
    _log(plan)
    return plan


def _utc(moment: datetime) -> datetime:
    """UTC, refusing a naive datetime rather than guessing what it meant.

    The conversion is ``ingest._utc``'s. The refusal is the addition: that
    helper normalises instants that already arrived aware, whereas this one
    takes a caller's argument, and ``astimezone`` on a naive datetime silently
    reads it as *this machine's* local time. On an audit record reconciled
    against a fill that is a four-or-five-hour error with no symptom -- the
    same reason ``db.types.UtcDateTime`` and ``wire.require_aware`` each
    refuse one at their own boundary.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            "a subscription plan's `at` must be timezone-aware; a naive "
            f"datetime would be read as this machine's local time. Got "
            f"{moment!r}"
        )
    return moment.astimezone(timezone.utc)


def _ordered(units: Iterable[SubscriptionUnit]) -> Sequence[SubscriptionUnit]:
    """Validate, then sort by priority. Stable, so a tier keeps caller order."""
    materialised = list(units)
    for unit in materialised:
        if not unit.symbols:
            raise ValueError(
                f"subscription unit {unit.key!r} has no symbols. An empty unit "
                "would be admitted for free and stream nothing, which is a "
                "caller bug that hides itself"
            )
        for symbol in unit.symbols:
            if not symbol:
                raise ValueError(
                    f"subscription unit {unit.key!r} carries an empty symbol; "
                    "it would be sent to the transport as a subscription"
                )
    return sorted(materialised, key=lambda unit: unit.priority)


def _new_symbols(
    unit: SubscriptionUnit, subscribed: dict[str, None]
) -> tuple[str, ...]:
    """The unit's symbols that are not already streaming, deduplicated.

    Insertion-ordered, so a unit's symbols enter the plan in the order it
    named them. ``dict`` rather than ``set`` throughout for that reason: a set
    would make the plan depend on hash ordering, which is the determinism the
    subscription list has to have.
    """
    wanted: dict[str, None] = {}
    for symbol in unit.symbols:
        if symbol not in subscribed:
            wanted.setdefault(symbol, None)
    return tuple(wanted)


def _drop(
    unit: SubscriptionUnit,
    rule: DropRule,
    detail: str,
    *,
    required: int,
    width: int,
    remaining: int,
    cap: int,
    at: datetime,
) -> DroppedUnit:
    return DroppedUnit(
        key=unit.key,
        priority=unit.priority,
        symbols=unit.symbols,
        rule=rule,
        detail=detail,
        required=required,
        width=width,
        remaining=remaining,
        cap=cap,
        at=at,
    )


def _log(plan: SubscriptionPlan) -> None:
    """Rule 8, applied to a dropped subscription rather than a rejected order.

    Both a line per drop and one summary. The per-drop line is what tells you
    *which* position stopped marking; the summary is the number worth alerting
    on, and computing it from a scan of the log would mean reconstructing the
    dedup rule in whatever reads it. Every record carries
    :attr:`SubscriptionPlan.correlation_id`, because the N+1 of them are one
    decision and two plans a poll apart otherwise interleave with nothing to
    attribute them.

    A plan that fits logs nothing. The ordinary case is silent so that the
    extraordinary one is not.
    """
    if not plan.dropped:
        return

    for unit in plan.dropped:
        logger.warning(
            "stream subscription dropped (%s): %s -- %s",
            unit.rule.value,
            unit.key,
            unit.detail,
            extra={
                "event": "stream_subscription_dropped",
                "correlation_id": plan.correlation_id,
                "rule": unit.rule.value,
                "key": unit.key,
                "priority": unit.priority.label,
                "symbols": list(unit.symbols),
                "required": unit.required,
                "width": unit.width,
                "remaining": unit.remaining,
                "cap": unit.cap,
                "detail": unit.detail,
                "at": unit.at.isoformat(),
            },
        )

    logger.warning(
        "%d symbols not streamed: %d of %d stream slots are in use across %d "
        "subscriptions, and %d were dropped",
        plan.not_streamed,
        len(plan.subscribed),
        plan.cap,
        len(plan.admitted),
        len(plan.dropped),
        extra={
            "event": "stream_subscription_budget_exceeded",
            "correlation_id": plan.correlation_id,
            "not_streamed": plan.not_streamed,
            "dropped_symbols": list(plan.dropped_symbols),
            "dropped_units": len(plan.dropped),
            "subscribed_count": len(plan.subscribed),
            "spare_capacity": plan.spare_capacity,
            "cap": plan.cap,
            "at": plan.at.isoformat(),
        },
    )
