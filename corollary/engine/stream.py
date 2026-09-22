"""The two subscription budgets -- pure. Desired symbols in, a plan out.

Alpaca caps the **equity** stream and the **option** stream separately, and
the two numbers are not the same number: on Basic it is 30 equity symbols and
200 option quotes. Two sockets, two budgets, never one shared pool of thirty.
Every option contract is its own symbol, so a *full but ordinary* book -- eight
grouped multi-leg positions, the concurrent-position ceiling this account
actually runs -- is 32 option symbols and at most eight underlyings. Each side
of that fits its own cap with room to spare: 168 option quotes and 22 equity
symbols go unspent.

**This module was written against one cap of thirty, and that was wrong.** The
arithmetic said an ordinary book was ten symbols over budget -- and ten over
cost *twelve* unstreamed, because a unit is refused whole and spending stops at
the first refusal, so four legs of a held spread took the eight underlyings
behind it down as well. The plan dropped position *contracts*, reported
*"12 symbols not streamed"* and left held positions marking at a last known
price, while 170 option quotes sat unused beside them
(``test_the_single_budget_this_replaces_would_have_dropped_held_contracts``
pins both numbers). The allocation was never the problem; the number handed to
it was. Under-spending a budget is only the safe direction when the thing being
rationed is optional, and a held contract's mark is not.

So where is the scarcity now? On the equity stream, a Markets viewport wider
than the ~22 slots a full book leaves. On the option stream, a chain view
(Phase 4), whose rows are contracts by the hundred. And on Algo Trader Plus,
where equities become unlimited and the option stream still stops at 1000 --
which is where this module's arithmetic earns its keep rather than becoming
vestigial.

This module decides what loses. It is the arbiter, not the client: it opens no
socket, imports no vendor, reads no clock and reads no configuration.
``alpaca`` is imported in exactly two files and neither is this one -- what
comes back is a list of symbols a transport is then told to subscribe to, so
swapping vendors changes the transport and leaves the policy alone. Identical
inputs give identical output, which is the standard the scanner is held to and
matters here for a second reason: a plan that reshuffles between polls churns
the socket, and every resubscribe is a gap in the marks it was opened to
deliver.

**The failure this prevents is a silent one.** A stream that quietly accepts
200 of the 204 symbols it was handed, or that accepts all 204 and is silently
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

Four tiers, ordered, from the design spec: **position contracts**, then their
**underlyings**, then **recommended trades** (Phase 4), then the rows a client
says are **visible on the Markets page**. Recommended trades have no producer
yet and are structurally present rather than speculative: the member and the
constructor cost one line each, and leaving them out would mean the later
change is to this module's *logic* rather than to its inputs. The Markets tier
is produced by ``EngineRuntime.markets_visible_units``, from a viewport hint
on ``/api/ws`` -- which is why it is the one tier whose input is a client's.

:attr:`SubscriptionPriority.MARKETS_VISIBLE` is last and can only ever be last.
It is the one tier whose input arrives from the *client* -- a viewport hint on
the websocket -- and a client that could outrank a position contract could make
a held position mark stale by scrolling. That is rule 4's principle (the engine
enforces, the UI displays) applied to a stream budget rather than to a risk
limit. Losing the tier costs freshness and never a price: every Markets row is
polled anyway.

**A unit belongs to one stream, and the call says which stream it is.** A
unit's symbols are all option symbols or all equity symbols, and a unit
straddling the two is refused as a caller bug: the two budgets are two calls,
so a mixed unit has no single budget that can answer it whole. A unit lying
*wholly* on the other side is refused for a sharper reason -- it fits, so it
would be **admitted**, and its symbols returned as a subscription list for a
socket that will never quote them, with nothing dropped and nothing to report.
That is why :func:`plan_subscriptions` requires the ``stream`` it is planning
and checks every unit against it.

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

There are two caps, they are named for their streams, and ``cap`` has no default
--------------------------------------------------------------------------------

:data:`EQUITY_STREAM_SYMBOL_CAP` and :data:`OPTION_STREAM_QUOTE_CAP` are the
only places 30 and 200 are written. There is deliberately **no unqualified
``STREAM_SYMBOL_CAP``**: one name standing for two different budgets is the
habitat the opening bug lived in, and a reader who sees ``cap`` at a call site
should have to say which stream they meant.

For the same reason ``cap`` is a **required** argument of
:func:`plan_subscriptions` rather than one defaulting to either constant. A
default is a silent choice of stream: an option-stream caller who omitted it
would plan 200 contracts against a budget of 30, drop 170 of them with
``no_room``, and report held positions as unstreamed while the option socket
sat 170 quotes below its real limit.

**One call plans one stream.** The option units are fitted against one cap and
the equity units against the other, in two calls, and the caller subscribes
each plan to its own socket. Teaching this function two budgets at once was
considered and rejected twice over: it would put OCC-symbol parsing -- a
vendor-shaped concern -- into the allocation of the one module that is
deliberately ignorant of vendors, and it would turn one prefix cut with one
``remaining`` into two interleaved cuts whose drop ordering has to be reasoned
about again. The symbol *shape* is read here for exactly one purpose, to refuse
a unit that straddles both streams, and never to decide who gets a slot.

Neither cap is read from the environment here, and they are not the same shape
as the three feed-name variables: those are genuinely read from
``ALPACA_OPTIONS_FEED`` / ``ALPACA_STOCK_FEED_HISTORICAL`` /
``ALPACA_STOCK_FEED_REALTIME`` inside the provider, whereas these are literals
the caller may override, and calling the two the same thing would claim a
property this module does not have. Feed names are genuinely per-deployment;
these are facts about a published price list, and a cap in the environment is a
cap that can be raised by someone who has not paid -- with silent truncation as
the failure.

The account's plan of record is ``ALPACA_DATA_PLAN`` (see
``api/routes/settings.py``), and **deriving both caps from it belongs to the
caller** -- ``engine/runtime.py``, which owns the plan lookup and the
environment. Whoever reads that caller: on Algo Trader Plus the equity limit
does not exist at all, while the option stream rises to 1000 -- a higher
ceiling, not the absence of one. Applying an "unlimited" sentinel to the option
cap would subscribe past a limit the server enforces, which is this module's
opening failure arriving from the other direction. This module cannot notice
any of that, by design; it only knows the number it is handed.

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
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Final

__all__ = [
    "EQUITY_STREAM_SYMBOL_CAP",
    "OPTION_STREAM_QUOTE_CAP",
    "AcknowledgedSubscription",
    "DropRule",
    "DroppedUnit",
    "Stream",
    "SubscriptionPlan",
    "SubscriptionPriority",
    "SubscriptionUnit",
    "contract_unit",
    "markets_visible_unit",
    "not_streamed_message",
    "plan_subscriptions",
    "recommendation_unit",
    "reconcile_acknowledgement",
    "replan_at_cap",
    "requested_units",
    "stream_of",
    "underlying_unit",
]

logger = logging.getLogger(__name__)

#: Alpaca Basic caps the **equity** stream at thirty symbols. Spent on
#: underlyings and on Markets rows, never on contracts. Written once, and
#: named for its stream: see the module docstring on why no unqualified cap
#: survives, and on why deriving the live value from ``ALPACA_DATA_PLAN``
#: is ``engine/runtime.py``'s job rather than this module's.
EQUITY_STREAM_SYMBOL_CAP: Final[int] = 30

#: Alpaca Basic caps the **option** stream at two hundred quotes -- a separate
#: budget from the equity one, not a share of it. Eight grouped four-leg
#: positions are 32 of these, so an ordinary full book spends about a sixth of
#: it; a Phase 4 chain view is what makes this scarce.
OPTION_STREAM_QUOTE_CAP: Final[int] = 200

#: An OCC symbol: root, ``YYMMDD``, ``C``/``P``, then the strike ×1000 in
#: eight digits. The root may carry a numeric suffix (``AAPL1``) when a split
#: or special dividend leaves the deliverable something other than 100 shares,
#: so it is matched as alphanumeric after a leading letter.
#:
#: Read for **one** kind of purpose: to refuse a unit, whether its symbols
#: straddle both budgets or sit wholly on the other one (:func:`_stream_of`).
#: It never decides who gets a slot, which is what keeps allocation ignorant
#: of what an instrument is.
#:
#: **Mirrored on the client** as ``OCC_SYMBOL`` in ``web/src/lib/markets.ts``,
#: which uses it to keep contracts off a Markets viewport hint. That mirror is
#: needed *in addition to* the client's width test, because the shortest legal
#: OCC symbol is exactly sixteen characters -- ``A241220C00150000``, a
#: single-letter root -- and so matches the equity shape.
#:
#: Which direction is dangerous is the other way round here, because this
#: pattern *excludes*: matching **fewer** strings asks nothing of the client,
#: and matching **more** is the change that needs the mirror moved in the same
#: commit. :meth:`~corollary.engine.runtime.EngineRuntime.set_markets_visible`
#: reads this through :func:`stream_of` and refuses a hint naming any
#: contract, and a hint is applied whole or not at all -- so one symbol newly
#: matched here, with the old client still sending it, refuses *every* hint
#: that names it, and the client re-sends on refusal into the identical
#: refusal. Whether the ``MARKETS_VISIBLE`` tier then holds **nothing** or
#: something **stale** depends on how often the newly-matched shape is on
#: screen: a refusal returns before the held hint is reassigned, so the
#: previous one stands, and only a session in which every hint is refused
#: leaves the tier genuinely empty. The intermittent case -- streaming rows
#: nobody is looking at -- is the quieter of the two and the harder to
#: notice. The asymmetry that hides both: this side is loud (rule 8 --
#: :meth:`~corollary.engine.runtime.EngineRuntime.set_markets_visible`
#: refuses through a helper that logs the rule, the inputs and the timestamp
#: on every refusal) and the
#: client side is silent, surfacing nothing and polling regardless, so the
#: only symptom on screen is staleness.
_OCC_SYMBOL: Final = re.compile(r"^[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}$")


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
    #: The rows a client reports as visible on the Markets page -- a viewport
    #: hint, debounced, over the websocket. **Lowest, and structurally so.**
    #: The only tier whose input comes from the client, and a client that
    #: could outrank a held contract could make a position mark stale by
    #: scrolling. Churn here is harmless: every Markets row is polled anyway,
    #: so losing the slot costs freshness and never a price.
    MARKETS_VISIBLE = 4

    @property
    def label(self) -> str:
        """The value a log record and the API carry. Stable across renames."""
        return self.name.lower()

    @property
    def client_supplied(self) -> bool:
        """Did a browser choose this tier's symbols? Exactly one tier did.

        Asked rather than assumed, in the three places the answer changes
        what happens: a client-supplied unit is not counted by
        :attr:`SubscriptionPlan.not_streamed`, is not warned about when it is
        dropped, and is not part of :attr:`SubscriptionPlan.engine_subscribed`
        -- so it can neither raise a false banner nor, one module up, open a
        vendor socket the watchdog then judges.

        A tier rather than a per-unit flag, because the priority *is* the
        provenance: :func:`markets_visible_unit` is the only producer of this
        value and ``EngineRuntime.markets_visible_units`` is its only caller.
        A future client tier is a member here and a deliberate answer to this
        property, which ``test_stream.py`` pins as a set of one.
        """
        return self is SubscriptionPriority.MARKETS_VISIBLE


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
    #: The subscribe was sent and the **server** did not acknowledge this
    #: symbol in its ``subscription`` message. Not our arithmetic: the plan
    #: fit, the socket accepted the request, and fewer symbols came back than
    #: went out. Its own rule because its remedy is different -- every other
    #: rule here is answered by a smaller book or a larger cap, and this one
    #: is answered by asking the vendor why.
    NOT_ACKNOWLEDGED = "not_acknowledged"
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
    """A Phase 4 recommended trade. Same all-or-nothing rule as a position.

    **One stream per unit, so Phase 4 builds a recommendation as two units:**
    one option unit carrying its legs and one equity unit carrying its
    underlying, both at this priority. :func:`contract_unit` and
    :func:`underlying_unit` satisfy that by construction and this does not --
    the symbols are whatever the caller passes -- so a recommendation named
    with its underlying alongside its legs is refused by
    :func:`plan_subscriptions` rather than split across two budgets. That
    mirrors what a held position already does, where the contracts and the
    underlying are separate units at separate priorities.
    """
    return SubscriptionUnit(
        key=recommendation_id,
        priority=SubscriptionPriority.RECOMMENDED_TRADE,
        symbols=tuple(symbols),
    )


def markets_visible_unit(symbol: str) -> SubscriptionUnit:
    """One row a client says is on screen. Keyed on the symbol, like an underlying.

    One symbol per unit, because Markets rows are independent of each other:
    a row is marked or it is not, and there is no net value spanning two of
    them to be half-stale. Admitted at the lowest priority there is -- see
    :attr:`SubscriptionPriority.MARKETS_VISIBLE` on why a client-supplied
    list can never be anything else.
    """
    return SubscriptionUnit(
        key=symbol,
        priority=SubscriptionPriority.MARKETS_VISIBLE,
        symbols=(symbol,),
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
    #: Which socket this plan is for. Carried rather than inferred: two plans
    #: of one decision share a correlation id, and telling their records apart
    #: by recognising 30 versus 200 is inference from a number that collapses
    #: the day the two caps coincide.
    stream: Stream
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

    def _missing(self, *, client_supplied: bool) -> tuple[str, ...]:
        """Distinct symbols dropped from one provenance and not streaming.

        A symbol reaching a dropped unit *and* an admitted one is streaming,
        so it does not appear here. The distinction matters: counting dropped
        units' symbols would over-report whenever a Phase 4 recommendation
        names a contract already held, and an inflated figure teaches the
        reader to ignore it.
        """
        streaming = self.subscribed_set
        missing: dict[str, None] = {}
        for unit in self.dropped:
            if unit.priority.client_supplied is not client_supplied:
                continue
            for symbol in unit.symbols:
                if symbol not in streaming:
                    missing.setdefault(symbol, None)
        return tuple(missing)

    @property
    def dropped_symbols(self) -> tuple[str, ...]:
        """Distinct symbols the **engine** asked for and is not streaming.

        Client-supplied tiers are excluded, and that exclusion is the whole
        meaning of the number above it: the reader's question is *"is
        anything I hold unmarked?"*, and a Markets row losing its slot to the
        cut is the design working rather than a fault -- every Markets row is
        polled regardless, so it costs freshness and never a price. Measured
        before the exclusion, eight held underlyings beside a full viewport
        list reported *"42 symbols not streamed"* with every held symbol
        marked live. A banner that is wrong is worse than no banner, because
        the next real one gets ignored.

        What the viewport lost is :attr:`client_dropped_symbols`, which is
        counted, logged and kept out of the banner.
        """
        return self._missing(client_supplied=False)

    @property
    def client_dropped_symbols(self) -> tuple[str, ...]:
        """Distinct client-supplied symbols that lost the cut. Never a banner.

        Kept because "the tail was trimmed" is worth knowing and worth
        counting; kept *separate* because it is not evidence that anything
        held is unmarked. Symbols rather than a bare count so a test can name
        them -- the log records a count, never these strings, since this is
        the one tier whose values came from a browser.
        """
        return self._missing(client_supplied=True)

    @property
    def not_streamed(self) -> int:
        """The N the UI renders as *"N symbols not streamed"*."""
        return len(self.dropped_symbols)

    @property
    def client_not_streamed(self) -> int:
        """How many client-supplied rows lost their slots. Not the banner's N."""
        return len(self.client_dropped_symbols)

    @property
    def engine_subscribed(self) -> tuple[str, ...]:
        """Subscribed symbols that something the **engine** owns asked for.

        The subset a socket decision may be taken on, and the reason it
        exists is rule 9. A quote socket is launched when its plan has
        content and is then judged by the watchdog for ninety seconds of
        silence; if a viewport hint could be that content, a browser scrolling
        on a flat book would open an equity socket, arm the dead-man's switch
        against symbols nobody validated, and halt the engine when they do not
        quote -- a self-inflicted halt, and rule 9 depends on those not
        existing.

        So the hint rides an already-open, already-expected socket or it
        rides nothing, which is exactly what decision 18 says it is: spare
        slots, never a reason to spend.

        A symbol both the engine and a client asked for is engine-owned:
        dedup is not a transfer of ownership, and the position's unit is
        what put it on the socket.
        """
        owned = {
            symbol
            for unit in self.admitted
            if not unit.priority.client_supplied
            for symbol in unit.symbols
        }
        return tuple(symbol for symbol in self.subscribed if symbol in owned)

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
        return not_streamed_message(self.not_streamed)


def not_streamed_message(count: int) -> str | None:
    """The UI's sentence for ``count`` missing symbols, or ``None`` for zero.

    Written once and shared, because there are now **two** plans per decision
    and the reader gets **one** banner: the question is *"is anything I hold
    unmarked?"*, not *"which socket ran out?"*, and which one did is named in
    the log record -- ``stream``, with its ``cap`` beside it, on every drop. A
    caller summing two plans and phrasing the total itself is how the two
    sentences drift apart.

    ``None`` rather than *"0 symbols not streamed"*: a banner that is always
    present is a banner nobody reads.
    """
    if count == 0:
        return None
    return f"{count} symbol{'' if count == 1 else 's'} not streamed"


@dataclass(frozen=True, slots=True)
class AcknowledgedSubscription:
    """A plan, and what the server said it is actually streaming for us.

    The subscribe goes out from :attr:`SubscriptionPlan.subscribed`; Alpaca
    answers with a ``subscription`` message listing, **per channel**, every
    symbol it now holds for this connection. The two lists are compared here
    rather than assumed equal, because a socket that accepts fewer symbols
    than it was handed is exactly the silent truncation this module exists to
    prevent -- arriving from the server instead of from our own sums, and with
    no ``dropped`` record to show for it.

    :attr:`not_streamed` therefore sums two causes into the one figure the UI
    renders: the symbols *we* refused for budget, and the symbols *the server*
    did not confirm. The reader's question is *"is anything I hold unmarked?"*,
    and answering it with only our half answers a question nobody asked.
    """

    plan: SubscriptionPlan
    #: The subscription channel this reconciliation is about -- ``quotes``,
    #: ``trades``, ``bars``. Required, because one ``subscription`` message
    #: carries every channel and comparing the union against one channel's
    #: request reports absentees that are not absent at all.
    channel: str
    #: Exactly what the server listed for :attr:`channel`, in its order.
    acknowledged: tuple[str, ...]
    at: datetime
    #: Subscribed symbols whose ``subscribe`` frame the server has **not yet
    #: answered** -- added by a revision whose replies are still outstanding.
    #: Empty in the steady state, which is every reconciliation but the ones
    #: inside a round trip.
    #:
    #: They are held out of :attr:`absent` because nothing has refused them:
    #: a reply that predates the frame carrying a symbol cannot be evidence
    #: about that symbol, and a record saying otherwise names symbols the
    #: server never saw. They are still counted into :attr:`not_streamed`,
    #: because *unanswered* is *not marking yet* and over-reporting a gap is
    #: the safe direction for a banner. See :attr:`unanswered`.
    in_flight: frozenset[str] = frozenset()

    @property
    def acknowledged_set(self) -> frozenset[str]:
        return frozenset(self.acknowledged)

    @property
    def absent(self) -> tuple[str, ...]:
        """Subscribed, not acknowledged, and answerable. In the plan's order.

        :attr:`in_flight` is excluded: those are unanswered, not absent, and
        the difference is the difference between *"the server refused this"*
        and *"we have not heard yet"*. :attr:`unanswered` carries them.
        """
        acked = self.acknowledged_set
        return tuple(
            symbol
            for symbol in self.plan.subscribed
            if symbol not in acked and symbol not in self.in_flight
        )

    @property
    def unanswered(self) -> tuple[str, ...]:
        """Subscribed, not acknowledged, and not yet answerable.

        Held out of :attr:`absent` and counted into :attr:`not_streamed`.
        **Never logged as symbols** -- a count of these is a statement about
        our own round trip, while the list is whatever the caller last asked
        for, which on the equity stream includes the Markets viewport hint
        (rule 6: a ticker-shape filter is not a redactor).
        """
        acked = self.acknowledged_set
        return tuple(
            symbol
            for symbol in self.plan.subscribed
            if symbol not in acked and symbol in self.in_flight
        )

    @property
    def surplus(self) -> tuple[str, ...]:
        """Acknowledged, and never asked for.

        Reported rather than ignored: it spends a slot of the budget being
        metered here, so an unexplained one makes every later ``no_room`` drop
        arithmetically right and practically wrong. It is also the shape a
        subscription left over from a previous connection would take.
        """
        wanted = self.plan.subscribed_set
        return tuple(symbol for symbol in self.acknowledged if symbol not in wanted)

    @property
    def not_streamed(self) -> int:
        """The N the UI renders, counting every cause.

        Our own drops, the server's refusals, and the symbols still inside a
        round trip: the reader's question is *"is anything I hold unmarked?"*
        and a symbol we have not been told about is not marking yet. It drops
        out of the figure when the reply arrives, which is the direction that
        cannot show *"0 not streamed"* over a gap.
        """
        return self.plan.not_streamed + len(self.absent) + len(self.unanswered)

    @property
    def message(self) -> str | None:
        return not_streamed_message(self.not_streamed)


def reconcile_acknowledgement(
    plan: SubscriptionPlan,
    *,
    channel: str,
    acknowledged: Iterable[str],
    at: datetime,
    in_flight: Iterable[str] = (),
    unanswered_frames: int = 0,
) -> AcknowledgedSubscription:
    """Compare what we subscribed against what the server says it streams.

    Pure but for the log. Rule 8 applies -- a dropped subscription is a
    rejection whoever dropped it -- so an absentee emits the rule, the inputs
    and the timestamp under the plan's correlation id, which is what makes the
    record part of the same decision that produced the plan. A reconciliation
    with nothing missing logs nothing, for :func:`_log`'s reason: the ordinary
    case is silent so that the extraordinary one is not.

    ``in_flight`` names the subscribed symbols whose frame the server has not
    answered yet, and ``unanswered_frames`` how many frames those are. Both
    default to the steady state -- nothing outstanding, every claim
    answerable. Given either, this emits **two** kinds of record and keeps
    them apart: a refusal (:func:`_log_acknowledgement`, symbols and all) for
    what the server has answered about, and a counts-only *out of step*
    (:func:`_log_out_of_step`) for what it has not. The second is rule 8 held
    to what is knowable: silence would hide a divergence, and naming symbols
    nobody has refused is a false record in a log that is read after a loss.
    """
    if not channel:
        raise ValueError(
            "a reconciliation needs the channel it is about; one "
            "subscription message carries quotes, trades and bars together, "
            "and comparing their union against one channel's request reports "
            "absentees that are not absent"
        )
    reconciled = AcknowledgedSubscription(
        plan=plan,
        channel=channel,
        acknowledged=tuple(acknowledged),
        at=_utc(at),
        in_flight=frozenset(in_flight),
    )
    _log_acknowledgement(reconciled)
    _log_out_of_step(reconciled, unanswered_frames=unanswered_frames)
    return reconciled


def requested_units(plan: SubscriptionPlan) -> tuple[SubscriptionUnit, ...]:
    """Every unit ``plan`` considered, admitted or dropped, in priority order.

    A :class:`DroppedUnit` carries exactly a unit's three fields, so this is
    lossless in content. It is *almost* lossless in order: within one priority
    tier it lists the admitted units before the dropped ones, which differs
    from the caller's original order only where a unit was admitted at zero
    cost *behind* a drop. That case changes no tier, so a re-plan built from
    this cannot invert priority -- and the alternative, a ``requested`` field
    on the plan, is a new field on a frozen shape for a reordering that costs
    nothing.
    """
    return tuple(plan.admitted) + tuple(
        SubscriptionUnit(key=unit.key, priority=unit.priority, symbols=unit.symbols)
        for unit in plan.dropped
    )


def replan_at_cap(
    plan: SubscriptionPlan,
    *,
    cap: int,
    at: datetime,
    correlation_id: str,
) -> SubscriptionPlan:
    """Re-run ``plan``'s own units against a **lower** cap, on the same stream.

    The 405 path. Alpaca answers a subscribe that would put the connection
    over its symbol limit with error 405, which is the server correcting the
    number we planned against. The correction is authoritative, so the plan is
    recomputed rather than trimmed: all-or-nothing units stay whole and every
    newly refused one gets its own rule-8 record, neither of which survives
    slicing a symbol list.

    **A correction only ever lowers.** Raising a cap on the strength of a
    refusal would subscribe past a limit the server enforces silently, which
    is the failure the budget exists to prevent, so a higher ``cap`` raises.
    """
    if cap > plan.cap:
        raise ValueError(
            f"a cap correction lowers; got {cap} against the plan's "
            f"{plan.cap}. A 405 means the server refused what we asked for, "
            "and a path that could widen a budget on the strength of a "
            "refusal would subscribe past a limit the server enforces silently"
        )
    return plan_subscriptions(
        requested_units(plan),
        at=at,
        correlation_id=correlation_id,
        cap=cap,
        stream=plan.stream,
    )


def plan_subscriptions(
    units: Iterable[SubscriptionUnit],
    *,
    at: datetime,
    correlation_id: str,
    cap: int,
    stream: Stream,
) -> SubscriptionPlan:
    """Fit ``units`` into ``cap`` stream slots, and say what did not fit.

    **One call plans one stream, and says which.** Pass the option units as
    :attr:`Stream.OPTION` with :data:`OPTION_STREAM_QUOTE_CAP` and the equity
    units as :attr:`Stream.EQUITY` with :data:`EQUITY_STREAM_SYMBOL_CAP` -- or
    with whatever the account's plan allows, which ``engine/runtime.py``
    derives. Neither ``cap`` nor ``stream`` has a default, because either
    default would be a silent choice of which socket this is: a wrong ``cap``
    under-spends a budget, and a wrong ``stream`` hands symbols to a socket
    that will never quote them.

    ``stream`` is checked, not trusted. Every unit's symbols must belong to
    it, so a list filed under the wrong socket is a refusal here rather than
    an admitted plan whose marks never arrive. It is also the label on every
    record this plan emits.

    Units are taken in priority order -- position contracts, then underlyings,
    then recommended trades, then visible Markets rows -- with the caller's
    order preserved within each tier. Each is admitted whole or refused whole,
    a symbol already admitted costs nothing, and spending stops at the first
    refusal. The module docstring gives the reasoning for all three.

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
    ``correlation_id``, a unit with no symbols, an empty symbol string, a unit
    whose symbols straddle both streams, and a unit whose symbols belong to
    the *other* stream. All seven are caller bugs that would otherwise fail
    quietly -- an empty unit is admitted for free and streams nothing, an
    empty symbol would be sent to the transport as a subscription, a mixed
    unit would be half-admitted by one budget and half-dropped by the other,
    which is the partly-marked position the all-or-nothing rule exists to
    prevent, and a wrongly filed one is worse still: it fits, it is admitted,
    and the plan reports nothing missing while the socket quotes none of it.
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

    ordered = _ordered(units, stream)

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
        stream=stream,
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


class Stream(StrEnum):
    """Which socket a plan is for. Two sockets, two budgets, two calls.

    Public because :func:`plan_subscriptions` requires it: a plan that does
    not say which stream it is for cannot refuse a unit filed under the wrong
    one, and cannot label its own drop records. Not a vendor concept -- OCC
    clears the options and every vendor selling both instruments meters them
    apart.

    **Read while validating and never while allocating.** Who gets a slot does
    not depend on what an instrument is, and keeping it that way is what lets
    one proven function plan both streams.
    """

    OPTION = "option"
    EQUITY = "equity"

    @property
    def label(self) -> str:
        """The value a log record and the API carry. Stable across renames."""
        return self.value


def stream_of(symbol: str) -> Stream:
    """Which socket one symbol belongs to. The same judgement, said in public.

    :func:`_stream_of` is read while *validating a unit*, which is too late
    for a caller that must refuse a symbol **before** it becomes one: the
    viewport hint arrives from a browser, and an OCC symbol on it is a caller
    bug that has to be answered with a refusal frame rather than with a
    ``ValueError`` raised out of the planner. So the answer is exported, and
    exported rather than reimplemented -- a second OCC regex elsewhere in the
    tree is two definitions of what an option symbol is, and the one that
    drifts is the one nothing plans against.

    Adds no logic and reads nothing new: this module still owns exactly one
    shape test, still reads no clock, no environment and no vendor library,
    and still never asks what an instrument is while allocating.
    """
    return _stream_of(symbol)


def _stream_of(symbol: str) -> Stream:
    """OCC-shaped symbols stream on the option socket; everything else does not.

    OCC is the clearing corporation's format, not a vendor's, so reading it
    here does not make this module know about Alpaca. The judgement is
    deliberately shape-only and one-directional: anything that is not an
    option symbol is treated as an equity symbol rather than validated as a
    ticker, because a malformed *equity* symbol is the transport's problem to
    report and guessing at one here would refuse subscriptions this module has
    no business refusing.

    Called by :func:`_ordered`, to refuse a unit that straddles both streams
    and to refuse one filed under the wrong stream, and by :func:`stream_of`,
    which is the same answer for a caller that has to refuse a symbol before
    it becomes a unit. The allocation loop never sees a :class:`Stream`.
    """
    return Stream.OPTION if _OCC_SYMBOL.match(symbol) else Stream.EQUITY


def _ordered(
    units: Iterable[SubscriptionUnit], stream: Stream
) -> Sequence[SubscriptionUnit]:
    """Validate against ``stream``, then sort by priority.

    Stable, so a tier keeps caller order. Every symbol read here is read to
    *refuse* a unit; nothing downstream of this asks what an instrument is.
    """
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
        streams = {_stream_of(symbol) for symbol in unit.symbols}
        if len(streams) > 1:
            options = [s for s in unit.symbols if _stream_of(s) is Stream.OPTION]
            equities = [s for s in unit.symbols if _stream_of(s) is Stream.EQUITY]
            raise ValueError(
                f"subscription unit {unit.key!r} spans two streams: option "
                f"symbols {options!r} and equity symbols {equities!r}. The "
                "option and equity budgets are separate and are planned one "
                "call each, so a unit across both could only be half admitted "
                "and half dropped -- the partly-marked position that "
                "all-or-nothing exists to prevent. Build it as two units, one "
                "per stream"
            )
        filed_under = streams.pop()
        if filed_under is not stream:
            raise ValueError(
                f"subscription unit {unit.key!r} carries {filed_under.value} "
                f"symbols {list(unit.symbols)!r} and was filed under the "
                f"{stream.value} stream. Its symbols would be returned as the "
                f"{stream.value} socket's subscription list, which will never "
                "quote them: every one of them marks at a last known price "
                "while the plan reports nothing missing, because the budget "
                "it was measured against had room. Pass it to the "
                f"{filed_under.value} call instead"
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


def _log_acknowledgement(reconciled: AcknowledgedSubscription) -> None:
    """Rule 8 for the half of the truncation the server performs.

    One record, not one per symbol: the symbols share a cause and a remedy,
    and N lines for one server decision is the shape that teaches a reader to
    filter the event out. A surplus acknowledgement rides the same record
    rather than a second one, because it is the same comparison read the other
    way about.
    """
    absent = reconciled.absent
    surplus = reconciled.surplus
    if not absent and not surplus:
        return
    plan = reconciled.plan
    logger.warning(
        "stream subscription not acknowledged (%s): %d of %d %s symbols on the "
        "%s stream are missing from the server's list",
        DropRule.NOT_ACKNOWLEDGED.value,
        len(absent),
        len(plan.subscribed),
        reconciled.channel,
        plan.stream.label,
        extra={
            "event": "stream_subscription_unacknowledged",
            "correlation_id": plan.correlation_id,
            "stream": plan.stream.label,
            "rule": DropRule.NOT_ACKNOWLEDGED.value,
            "channel": reconciled.channel,
            "absent": list(absent),
            "surplus": list(surplus),
            "subscribed_count": len(plan.subscribed),
            "acknowledged_count": len(reconciled.acknowledged),
            "not_streamed": reconciled.not_streamed,
            "cap": plan.cap,
            "detail": (
                "the subscribe was sent and the server confirmed fewer "
                "symbols than it was handed, so these are unmarked with no "
                "budget drop to explain them"
            ),
            "at": reconciled.at.isoformat(),
        },
    )


def _log_out_of_step(
    reconciled: AcknowledgedSubscription, *, unanswered_frames: int
) -> None:
    """Rule 8 for what the server has **not** answered: loud, and counts only.

    A ``subscription`` reply that lands while frames we sent are still owed
    answers cannot tell *"refused"* from *"not processed yet"* for a symbol
    those frames added. :attr:`AcknowledgedSubscription.absent` therefore
    holds those symbols out, and this record exists so that holding them out
    is not silence: it says the divergence happened, when, under which
    correlation id, and how many symbols and frames it covers.

    **It names no symbol, and that is the point twice over.** The honest
    claim here is about our own round trip rather than about any one ticker
    -- we are out of step with the server, which is not the same statement as
    *"the server refused these"* -- and the list on the equity stream is
    whatever the Markets viewport last asked for, which rule 6 keeps out of
    the log because a ticker-shape filter admits a paper account number.

    ``WARNING`` rather than ``INFO`` because the ordinary revision never
    reaches here: its interim reply matches a state we asked for and is spent
    against it. Reaching this means the server's word and our model of the
    wire have diverged, which is rare and worth seeing.
    """
    unanswered = reconciled.unanswered
    if not unanswered:
        return
    plan = reconciled.plan
    logger.warning(
        "out of step with the %s stream: %d symbol(s) across %d unanswered "
        "frame(s) cannot be claimed either way",
        plan.stream.label,
        len(unanswered),
        unanswered_frames,
        extra={
            "event": "stream_subscription_out_of_step",
            "correlation_id": plan.correlation_id,
            "stream": plan.stream.label,
            "rule": "out_of_step",
            "channel": reconciled.channel,
            "unanswered_count": len(unanswered),
            "unanswered_frames": unanswered_frames,
            "subscribed_count": len(plan.subscribed),
            "acknowledged_count": len(reconciled.acknowledged),
            "not_streamed": reconciled.not_streamed,
            "cap": plan.cap,
            "detail": (
                "a reply landed while frames we sent were still owed "
                "answers, so these symbols are unanswered rather than "
                "refused; counts only, because the claim is about the round "
                "trip and not about any symbol"
            ),
            "at": reconciled.at.isoformat(),
        },
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

    **A client-supplied tier is summarised, not warned about, and by count.**
    Two reasons, and either alone would be enough. It is not a fault: a
    viewport row losing its slot costs freshness and never a price, and
    sixty-four WARNINGs saying so in one plan is how the drop records for a
    held contract stop being read. And this is the one tier whose symbols
    arrived from a browser -- the shape filter that admits them also admits
    the account-number pattern ``wire.vendor_detail`` exists to redact, and
    this module may not import a redactor (no vendor import, no clock, no
    environment, so that identical inputs give identical output). A count
    needs no redaction.
    """
    if not plan.dropped:
        return

    _log_client_tier(plan)

    engine_dropped = tuple(
        unit for unit in plan.dropped if not unit.priority.client_supplied
    )
    if not engine_dropped:
        return

    for unit in engine_dropped:
        logger.warning(
            "stream subscription dropped (%s): %s -- %s",
            unit.rule.value,
            unit.key,
            unit.detail,
            extra={
                "event": "stream_subscription_dropped",
                "correlation_id": plan.correlation_id,
                "stream": plan.stream.label,
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
        len(engine_dropped),
        extra={
            "event": "stream_subscription_budget_exceeded",
            "correlation_id": plan.correlation_id,
            "stream": plan.stream.label,
            "not_streamed": plan.not_streamed,
            "dropped_symbols": list(plan.dropped_symbols),
            "dropped_units": len(engine_dropped),
            "client_not_streamed": plan.client_not_streamed,
            "subscribed_count": len(plan.subscribed),
            "spare_capacity": plan.spare_capacity,
            "cap": plan.cap,
            "at": plan.at.isoformat(),
        },
    )


def _log_client_tier(plan: SubscriptionPlan) -> None:
    """One INFO line for every client-supplied row the cut trimmed. Counts only.

    Separate from the warnings above because it answers a different question
    and deserves a different level: *"the viewport tail did not fit"* is the
    budget behaving as designed, and the reader alerting on
    ``stream_subscription_budget_exceeded`` must not be woken by it. Still
    logged, because a tier that vanished with no record at all is the silent
    truncation this module exists to prevent -- and a client that believes it
    watches a list it does not watch is worth being able to reconstruct.

    Nothing client-derived is in the record: units and symbols as numbers,
    the stream, the cap, the correlation id and the timestamp.
    """
    trimmed = tuple(unit for unit in plan.dropped if unit.priority.client_supplied)
    if not trimmed:
        return
    logger.info(
        "%d client-supplied rows were trimmed from the %s stream's %d slots",
        plan.client_not_streamed,
        plan.stream.label,
        plan.cap,
        extra={
            "event": "stream_client_tier_trimmed",
            "correlation_id": plan.correlation_id,
            "stream": plan.stream.label,
            # Derived from the units rather than written as
            # ``markets_visible``: there is one client tier today, and a
            # hard-coded label would quietly misname the second one.
            "priorities": sorted({unit.priority.label for unit in trimmed}),
            "trimmed_units": len(trimmed),
            "not_streamed": plan.client_not_streamed,
            "subscribed_count": len(plan.subscribed),
            "spare_capacity": plan.spare_capacity,
            "cap": plan.cap,
            "detail": (
                "the lowest tier is client-supplied and every row it names "
                "is polled regardless, so a trimmed tail costs freshness and "
                "never a price; it is not counted as a symbol not streamed"
            ),
            "at": plan.at.isoformat(),
        },
    )
