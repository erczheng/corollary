"""The FIFO realized-P&L matcher. Pure: activities in, realized trades out.

**Alpaca computes no realized P&L at all.** There is no field for it on any
endpoint, so every figure the Activity page shows -- lifetime P&L, average
win, average loss, win rate, the biggest loser -- is arithmetic this module
performs over the account's own activity history. That is what makes it worth
the care: it is the one place in Phase 2 where a wrong number is not a wrong
*display* of something true.

No I/O happens here. Nothing opens a socket, touches the database or reads the
clock; everything the matcher needs arrives as an argument, including the
per-contract multiplier and the settlement price of an exercised contract.
Persisting the output to ``fill`` and ``realized_trade`` is a caller's job,
and :class:`RealizedTradeRecord` mirrors that table column for column so the
write is ``RealizedTrade(**dataclasses.asdict(record))``.

What the matcher is given, and why each piece has to be given
------------------------------------------------------------

**Intent comes from the order, never from ``side``.** The activities endpoint
spells ``side`` three ways: ``sell_short`` opens a short, ``sell`` closes a
long, and ``buy`` is *both* buy-to-open and buy-to-close. Two of the four
actions are therefore indistinguishable from the row alone, so ``intents``
carries the fill-to-order join. A ``buy`` with no join is **refused and
reported**, never guessed: a matcher that assumed buy-to-open would book every
buy-to-close as a fresh lot and double the position it already held. Note
also that an order's ``side`` and an activity's ``side`` are different
vocabularies -- an mleg leg opening a short reports order ``side: "sell"``
with ``position_intent: "sell_to_open"`` while the activity for the same fill
reports ``side: "sell_short"`` -- which is why :class:`OrderSide` and
:class:`FillSide` are two enums and why only ``position_intent`` is compared
across the join.

**``multiplier`` is per contract and arrives as an input.** ``/v2/positions``
returns no multiplier field at all, so there is no fallback even if one were
wanted: it comes from ``/v2/options/contracts``, cached. Adjusted contracts
are the reason it matters -- after a split or special dividend OCC issues a
modified root (``AAPL1``) and the deliverable is no longer 100 shares, so a
hardcoded 100 computes max loss wrong on exactly those, which is the failure
CLAUDE.md rule 4 exists to prevent. A symbol with no multiplier is refused,
not defaulted.

**``contracts`` is optional, and an exercise without it books no money.** The
terms are read in two places, and only one of them is about dollars.

:class:`ShareDelivery` reads them for the underlying's real ticker and the
deliverable behind a share count -- neither of which any P&L figure depends
on. Omitting them leaves both unstated; it never substitutes a number.

The **matcher** reads one field, on the ``OPEXC``/``OPASN`` branch alone:
``root_symbol != underlying_symbol``, which the design spec names as the
detection test for an adjusted contract. An exercise settles against the
*underlying's* price, so booking it as ``(close - open) x qty x multiplier``
asserts that the contract delivers ``multiplier`` shares of the underlying at
the strike -- and on an adjusted contract that is false by construction. It is
the same premise :func:`_deliverable_shares` already declines to make thirty
lines away, for the same reason: a ``GME1`` reports ``multiplier: "100"``
while delivering 100 GME **plus** 10 GME.WS, so the multiplier cannot detect
its own wrongness. Declining to state a share count from it and then using it
to state a *dollar* P&L would be one rule applied in one direction.

So an ``OPEXC`` or ``OPASN`` on an adjusted contract -- **or on one whose
terms are absent, because then the comparison has only one side** -- books no
trade and produces a :attr:`RejectionRule.UNVERIFIED_DELIVERABLE` instead.
Two paths are deliberately *not* refused, because the hazard is not on them:
a closing **fill** on any contract, where a premium difference times the
multiplier is right whatever the deliverable, and ``OPEXP``, which closes at
zero and asks the underlying nothing. See :func:`_unverified_deliverable` for
what booking a correct number here would require, which is an open spec
question rather than an omission.

**Two quantity conventions normalise on the way in.** A ``FILL`` row carries
an unsigned ``qty`` with a separate ``side``; a non-trade row carries a
**signed** ``qty`` and no ``side`` at all (``-2`` when contracts leave a long,
``+2`` when a short is assigned away). :func:`normalise_activities` turns both
into one :class:`LotMovement`, so the matcher sees a single convention.

**No global chronological order exists, so none is assumed.** The design spec
says the composite activity id *"sorts chronologically as a string"*;
measured against the real recording only the 17-digit stamp does -- stamps
repeat and the UUID half then breaks ties arbitrarily -- and
``transaction_time`` does not order rows either at microsecond resolution,
even with ``direction=asc`` (``...438268`` came back before ``...438263``).
The invariant that *does* hold, asserted on real data, is **per contract
symbol**: within one symbol the fills are ordered and no symbol has two fills
inside a second. The open-lot queue is per contract symbol anyway, so that is
what the matcher builds on.

An option expiring is not a fill
--------------------------------

``OPEXP``, ``OPEXC`` and ``OPASN`` are their own activity types, and they are
how the most common ending of an option position arrives.

* **``OPEXP`` is the out-of-the-money case only.** Alpaca auto-exercises ITM
  contracts absent a do-not-exercise instruction, so an ITM expiry arrives as
  an ``OPEXC`` pair and never reaches this branch. ``OPEXP`` closes the
  remaining lots at **zero** -- a full loss on a long, the full credit kept on
  a short.
* **``OPEXC`` and ``OPASN`` carry no price of their own.** ``net_amount`` is
  ``"0"`` on the event row, and *"a matcher that trusts ``net_amount`` books
  every exercise as a total loss"*. The money sits on a paired ``OPTRD`` row
  against the **underlying**, whose ``price`` is the strike.

  Here the design spec and the arithmetic part company, and the resolution is
  the one deliberate deviation in this module. The spec says these *"close at
  the strike"*; taken literally that books an exercised NVDA 205 call bought
  at 14.20 as a **+$19,080 gain** -- ``(205 - 14.20) x 100`` -- where the
  account's equity moved by -$103. The strike is an *input* to the close
  price, not the close price: what the lot is worth at settlement is its
  **intrinsic value**, which needs the strike *and* the underlying's
  settlement price. With NVDA closing at 218.17 the 205 call is worth 13.17
  and the loss is ``(13.17 - 14.20) x 100 = -$103``, which reconciles against
  cash and shares exactly. The spec's earlier wording -- *"close at
  intrinsic"* -- was right, and the amendment that replaced it was correcting
  where the *number* comes from, not what the number is.

  So: the strike is read from the OCC symbol, which is always present and
  needs no pairing, and corroborated against the paired ``OPTRD``'s ``price``
  where a ``group_id`` links the two. The settlement price arrives in
  ``settlements``. **When it is absent the event is refused and reported** --
  never booked at zero, which is the failure the spec names, and never at the
  strike, which is the failure the spec's own wording would cause.
* **The shares are named, not tracked.** An exercise or assignment delivers
  stock; Corollary is not a stock app. Each one emits a
  :class:`ShareDelivery` stating the underlying, the signed share count and
  the strike it settled at, and no lot is opened for it.

  Two of those three come off the *contract terms* rather than off the OCC
  symbol, because the symbol answers both questions wrongly on an adjusted
  contract. ``AAPL1`` is a modified root, not a ticker, so the underlying is
  ``contract.underlying_symbol`` -- the same preference ``grouping._leg``
  makes, one convention rather than two. And the deliverable is
  ``contract.deliverables``, never ``qty x multiplier``: a ``GME1`` delivers
  100 GME **plus** 10 GME.WS while reporting ``multiplier: "100"`` and
  ``size: "100"`` like anything else, so the multiplier cannot detect its own
  wrongness, and Alpaca's spec says of ``size`` in as many words that it
  *"should not be used as a multiplier"*. Absent terms both go unstated --
  ``shares`` is ``Decimal | None`` -- rather than being answered with 100.
  Nothing derives P&L from this record, so the cost of saying "unknown" is a
  quieter sentence; the cost of the alternative was a confident wrong one.

**These three branches have no real data yet, and that is stated rather than
hidden.** Every ``OPEXP``/``OPEXC``/``OPASN`` fixture in
``tests/engine/test_ledger_option_events.py`` is authored from Alpaca's
published doc examples, so it proves this module is self-consistent and
proves nothing about what Alpaca sends. The reconciliation checkpoint is
already dated: three NVDA contracts expiring **2026-09-11** are held on the
paper account -- ``NVDA260911C00205000`` long and ITM (auto-exercise, so
``OPEXC``), ``NVDA260911C00240000`` long and OTM (``OPEXP``), and
``NVDA260911P00230000`` **short** and ITM (``OPASN``) -- and their activity
rows land by **2026-09-14**. Whoever records them should replace the authored
fixtures named after those symbols, in that file, first.

The failure mode being guarded is silent and directional: nothing raises,
because ``net_amount: 0`` is a well-formed number. Every ITM expiry would
book as a total loss, and lifetime P&L, average win, average loss and win
rate would then all be wrong in the same direction with nothing on screen to
say so. The first symptom is a terminal that believes you never win.

Fees attribute down a chain, and the tail is reported rather than dropped
------------------------------------------------------------------------

``order_id``, then ``execution_id``, then ``group_id``. The design spec routes
this through ``group_id`` alone, on the grounds that a non-trade activity has
no ``order_id``; measured against the real account **``group_id`` is null on
every non-trade row**, and what is actually present is ``execution_id``, on 15
of the 19 ``FEE`` rows -- a third field absent from the published
``NonTradeActivities`` schema, after ``description`` and ``price``.
``group_id`` stays in the chain because it is still the documented linkage for
an option-event pair, which this account has never produced.

Two things about the hops that are easy to get wrong:

* ``NonTradeActivity`` has **no ``order_id`` field**, so the first hop is
  reachable only through ``extra`` -- which is exactly what ``extra`` is for,
  the published schema being demonstrably incomplete.
* ``TradeActivity`` has no ``execution_id`` field either, and that looks like
  a dead end for the second hop until you notice where Alpaca puts the
  execution's identity: **the uuid half of the composite activity id is the
  execution**. So hop 2 joins ``fee.execution_id`` against
  ``fill.id.rpartition("::")[2]`` and needs no new field at all. Measured on
  the recording, 15 of 15 match across 15 distinct tails -- a relation, not a
  coincidence. See :func:`_execution_of`.

  This was nearly recorded as "hop 2 is unreachable". It read that way only
  because the fixture recorder pseudonymised ``id`` and left ``execution_id``
  raw, so the two sides could never match; the measurement was an artifact of
  the scrubber rather than a fact about Alpaca. Both halves are fixed.

That leaves **15 of 19 recorded fees attributed and 4 not**, and the four are
not a failure of the chain: ``CAT``, ``REG``, ``ORF`` and ``TAF`` are charged
per batch rather than per execution ("CAT fee for proceed of 15 trades"), so
they genuinely belong to no single fill. Hence *reported separately, never
dropped* -- :attr:`Ledger.unattributed_fees` is a number the Activity page can
show rather than a rounding error it absorbs.

**Attribution labels a fee; it does not move money.** ``realized_trade`` has
no fee column and ``pnl`` is the price difference, so an attributed fee stays
in :attr:`Ledger.total_fees` exactly as an unattributed one does. That is what
keeps the reconciliation invariant (realized + unrealized + fees == the
account's own P&L) closing to the cent regardless of how many of them the
chain manages to place.

**A fee with no ``net_amount`` is refused, never booked at zero.**
``net_amount`` is ``Decimal | None`` and ``wire.as_decimal("")`` answers
``None``, so an absent field and an empty string reach this module as the same
thing: *unknown*, which is not a number. Substituting ``Decimal(0)`` would
drop the charge out of :attr:`Ledger.total_fees` in full and stop the
reconciliation closing with nothing anywhere to say why -- the failure is
silent in both directions at once, which is the worst kind here. All 19
recorded ``FEE`` rows carry an amount, so this is unobserved rather than
impossible, and unobserved is the reason to write it down rather than a reason
to wait.

Every refusal is recorded
-------------------------

Silent rejection is a bug. Each declined activity produces a
:class:`LedgerRejection` carrying the rule, the inputs and the timestamp, and
each one is also logged -- returning it is the contract, logging it is so a
human sees it without a caller remembering to look. Nothing is dropped on the
floor, including the activities this module deliberately does not model: a
cash journal is declined with a reason, not ignored.
"""

import dataclasses
import logging
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Final, Protocol, TypeVar

from corollary.data.providers.interface import OptionContract
from corollary.engine.execution.interface import (
    ACTIVITY_ID_SEPARATOR,
    Activity,
    FillSide,
    NonTradeActivity,
    Order,
    PositionIntent,
    TradeActivity,
)
from corollary.instruments import OccSymbol, OptionType, parse_occ_symbol
from corollary.wire import vendor_detail

__all__ = [
    "CloseKind",
    "FeeLink",
    "FeeRecord",
    "Ledger",
    "LedgerRejection",
    "LedgerStats",
    "LotMovement",
    "MatchResult",
    "Normalisation",
    "OpenLot",
    "RealizedPnl",
    "RealizedTradeRecord",
    "RejectionRule",
    "ShareDelivery",
    "as_row_kwargs",
    "biggest_loser",
    "build_ledger",
    "intents_from_orders",
    "match_movements",
    "normalise_activities",
    "realized_pnl",
    "summarise",
]

logger = logging.getLogger(__name__)


class CloseKind(StrEnum):
    """The four ways a lot stops existing.

    Values mirror ``corollary.db.models.CLOSE_KINDS`` exactly, and
    ``tests/engine/test_ledger.py`` pins the two together rather than this
    module importing SQLAlchemy to stay pure.
    """

    FILL = "fill"
    EXPIRY = "expiry"
    EXERCISE = "exercise"
    ASSIGNMENT = "assignment"


#: A fill and a partial fill are the same lot movement; ``leaves_qty`` is the
#: order's business, not the ledger's.
FILL_ACTIVITY_TYPES: Final[frozenset[str]] = frozenset({"FILL", "PARTIAL_FILL"})

#: The three option events, and what each one does to a lot. Every one of them
#: *closes*; none opens, which is what makes their intent decidable from the
#: sign of ``qty`` alone with no order to join to.
OPTION_EVENT_TYPES: Final[Mapping[str, CloseKind]] = {
    "OPEXP": CloseKind.EXPIRY,
    "OPEXC": CloseKind.EXERCISE,
    "OPASN": CloseKind.ASSIGNMENT,
}

#: The row that carries the money for an option event, against the
#: **underlying**. Not a lot movement -- the shares are named, not tracked --
#: but its ``price`` is the strike, so it corroborates what the OCC symbol
#: already says.
PAIRED_TRADE_ACTIVITY_TYPE: Final = "OPTRD"

FEE_ACTIVITY_TYPE: Final = "FEE"

#: ``pnl_pct`` is stored in **percent** units, matching the frontend's
#: ``pnlPct`` (17.71 means 17.71%), and quantized to a **ten-thousandth of a
#: percentage point**. Four places is past anything a screen shows and short
#: of implying the division was exact.
PCT_QUANTUM: Final = Decimal("0.0001")

#: Dollars and cents, for the averaged folds. The per-trade ``pnl`` is *not*
#: quantized: a difference of two prices times two integers is already exact,
#: and rounding an exact figure is how a total stops matching its parts.
MONEY_QUANTUM: Final = Decimal("0.01")


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


class RejectionRule(StrEnum):
    """Why an activity was not matched. One value per reason, never a message.

    A rule is a thing you can count, filter and alert on; a sentence is not.
    """

    #: Not a fill, not an option event, not a fee. A cash journal, a dividend,
    #: a transfer. Declined with a reason rather than ignored.
    NOT_A_LEDGER_ACTIVITY = "not_a_ledger_activity"
    #: The symbol is not an OCC option symbol -- a stock trade, or the
    #: ``OPTRD`` leg of an option event against the underlying.
    NOT_AN_OPTION = "not_an_option_symbol"
    MISSING_SYMBOL = "missing_symbol"
    #: A ``buy`` with no fill-to-order join. BTO and BTC are both ``buy``.
    UNKNOWN_INTENT = "unknown_intent"
    #: The joined ``position_intent`` and the row's ``side`` disagree.
    INTENT_CONTRADICTS_SIDE = "intent_contradicts_side"
    MISSING_EVENT_QUANTITY = "missing_event_quantity"
    FRACTIONAL_QUANTITY = "fractional_quantity"
    #: An ``OPEXC``/``OPASN`` with no settlement price for the underlying.
    #: Refused rather than booked at zero or at the strike.
    #:
    #: Also what the matcher answers when a movement reaches it carrying no
    #: price at all. Normalisation cannot produce one -- a fill always has a
    #: price and an unpriced event is refused above -- but
    #: :func:`match_movements` is public and a caller may build its own, and a
    #: lot opened at an unknown basis, or closed against one, is a P&L nobody
    #: can stand behind.
    UNPRICED_OPTION_EVENT = "unpriced_option_event"
    #: An ``OPEXC``/``OPASN`` on a contract whose deliverable is not known to
    #: be ``multiplier`` shares of the underlying -- either the terms say
    #: ``root_symbol != underlying_symbol``, or the terms are absent so the
    #: question cannot be asked. The *settlement* P&L is refused; the closing
    #: fill path is untouched. See :func:`_unverified_deliverable`.
    UNVERIFIED_DELIVERABLE = "unverified_deliverable"
    #: The paired ``OPTRD``'s price is not the strike the OCC symbol names.
    PAIRED_STRIKE_DISAGREES = "paired_strike_disagrees"
    #: A ``FEE`` row with no ``net_amount``. Refused rather than booked at
    #: zero, which would understate :attr:`Ledger.total_fees` by the whole
    #: charge with nothing anywhere to say so.
    MISSING_FEE_AMOUNT = "missing_fee_amount"
    #: Two *different* rows share one ``activity_id``. Reported because
    #: ``fill(activity_id UNIQUE)`` would silently drop one of them.
    DUPLICATE_ACTIVITY_ID = "duplicate_activity_id"
    UNKNOWN_MULTIPLIER = "unknown_multiplier"
    #: More contracts closed than the queue holds open.
    OVER_CLOSE = "over_close"
    #: An open or close against the direction the live queue already holds.
    DIRECTION_CONFLICT = "direction_conflict"


@dataclass(frozen=True, slots=True)
class LedgerRejection:
    """One activity the ledger declined, with everything needed to explain it.

    Rule 8: *"A rejected order records the rule that rejected it, the inputs,
    and the timestamp."* The same standard applies to a rejected *activity* --
    you will need this the first time the terminal reports a P&L that is
    missing a trade you remember making.
    """

    rule: RejectionRule
    detail: str
    activity_id: str | None = None
    symbol: str | None = None
    at: datetime | None = None
    inputs: Mapping[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Normalised lot movements
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LotMovement:
    """One movement of contracts, in the single convention the matcher sees.

    ``qty`` is **unsigned** and whole; the direction is entirely in
    :attr:`intent`, which is the only field that states the four-way action
    unambiguously. ``side`` is kept because the ``fill`` table has a ``side``
    column and an option event has to supply one -- derived from the sign of
    the non-trade ``qty``, which is where that sign goes.

    :attr:`price` is **nullable, and null means the ledger could not establish
    it** -- never zero, which is a price and a total loss. A fill always
    carries one. An exercise or assignment does not: its price is an intrinsic
    value synthesised from the strike and the underlying's settlement, and on
    an adjusted contract the adjustment is exactly what invalidates the strike
    as an input. :func:`build_ledger` nulls those, on the matcher's own
    refusal rather than on a second opinion -- see
    :attr:`RejectionRule.UNVERIFIED_DELIVERABLE`.
    """

    activity_id: str
    activity_type: str
    symbol: str
    side: FillSide
    intent: PositionIntent
    qty: int
    price: Decimal | None
    at: datetime
    order_id: str | None = None
    group_id: str | None = None
    execution_id: str | None = None

    @property
    def close_kind(self) -> CloseKind:
        """How this movement would close a lot. ``FILL`` unless an option event."""
        return OPTION_EVENT_TYPES.get(self.activity_type, CloseKind.FILL)

    @property
    def is_opening(self) -> bool:
        return self.intent in (
            PositionIntent.BUY_TO_OPEN,
            PositionIntent.SELL_TO_OPEN,
        )

    @property
    def opens_short(self) -> bool:
        return self.intent is PositionIntent.SELL_TO_OPEN

    @property
    def closes_short(self) -> bool:
        return self.intent is PositionIntent.BUY_TO_CLOSE


class FeeLink(StrEnum):
    """Which hop of the attribution chain matched."""

    ORDER_ID = "order_id"
    EXECUTION_ID = "execution_id"
    GROUP_ID = "group_id"


@dataclass(frozen=True, slots=True)
class FeeRecord:
    """One ``FEE`` row, attributed where it can be and counted where it cannot.

    :attr:`amount` is signed as the broker sends it -- negative is a charge.
    Nothing here takes an absolute value: the sign is the fact.
    """

    activity_id: str
    amount: Decimal
    activity_sub_type: str | None = None
    #: The vendor's own prose, **de-identified**. Rule 6: a ``FEE`` row's
    #: ``description`` is the one field known to carry the account number in
    #: running text -- *"CAT fee for proceed of 15 trades on <date> by PA..."*
    #: -- where a rule written about field *names* cannot see it. This record
    #: is returned outward on ``IngestResult.fees`` and from there to an API
    #: route, so it is cleaned at construction rather than at each reader.
    #: :func:`~corollary.wire.vendor_detail` is that cleaning, reused rather
    #: than reimplemented: one redactor, or the second one is the one that
    #: misses something.
    description: str | None = None
    at: datetime | None = None
    #: The hop that matched, or ``None`` for unattributed.
    link: FeeLink | None = None
    #: The movement this fee attaches to, when exactly one matched.
    movement_activity_id: str | None = None
    #: The contract the fee belongs to, when the matched movements agree on one.
    symbol: str | None = None

    @property
    def is_attributed(self) -> bool:
        return self.link is not None


@dataclass(frozen=True, slots=True)
class ShareDelivery:
    """Stock that arrived or left because an option was exercised or assigned.

    **Named, not tracked.** Corollary is an options terminal: it has no stock
    position model, no stock cost basis and no stock P&L, and inventing one
    here would be a second ledger nobody asked for. What it can do honestly is
    say what happened -- "200 NVDA shares were delivered to you at 205" -- and
    leave the shares to the broker's own position list.

    Honestly is the operative word, because this is the one record in the
    module that asserts a *share* count, and on an adjusted contract both
    halves of that sentence are easy to get wrong at once: the OCC root is not
    the underlying's ticker, and the deliverable is not ``qty x multiplier``.
    Both are read from the contract terms where they are supplied and left
    unstated where they are not.
    """

    activity_id: str
    contract_symbol: str
    #: The underlying, from the contracts endpoint where supplied. Falls back
    #: to the symbol's OCC root, which is the same string **except on an
    #: adjusted contract** -- ``AAPL1`` has an underlying of ``AAPL``, and
    #: ``AAPL1`` is not a ticker that trades. Same fallback and same caveat as
    #: ``grouping.PositionLeg.underlying``, deliberately: one convention.
    underlying: str
    #: ``root_symbol != underlying_symbol``. ``None`` when the terms were not
    #: supplied, so a reader can tell a checked "standard" from an unchecked
    #: one -- unknown is a third answer and must not collapse into ``False``.
    is_adjusted: bool | None
    option_type: OptionType
    #: Signed: positive when shares arrive, negative when they leave.
    #:
    #: ``None`` when the deliverable is not known, which is **not** the same
    #: as zero. It comes from :attr:`OptionContract.deliverables` and from
    #: nowhere else: ``multiplier`` is contracts per unit of premium and
    #: ``size`` carries Alpaca's own warning that it *"should not be used as a
    #: multiplier"*, so neither is a share count. Every live adjusted contract
    #: reports ``multiplier: "100"`` while delivering 100 shares **plus**
    #: warrants, so the multiplier cannot even detect its own wrongness --
    #: only the deliverables can. A count no data supports is worse here than
    #: an absent one, for the same reason
    #: :attr:`RejectionRule.UNPRICED_OPTION_EVENT` refuses rather than guesses.
    shares: Decimal | None
    #: Per share, and it is the strike.
    price: Decimal
    at: datetime
    close_kind: CloseKind


@dataclass(frozen=True, slots=True)
class OpenLot:
    """A lot still open after everything in the sequence has been applied.

    The matcher's other output, and the one that reconciles against
    ``/v2/positions``: sum these per symbol and the broker should agree.
    """

    symbol: str
    qty: int
    price: Decimal
    opened_at: datetime
    activity_id: str
    is_short: bool


@dataclass(frozen=True, slots=True)
class RealizedTradeRecord:
    """One closed round trip. Mirrors ``realized_trade`` column for column.

    Field names and order match the table with ``id`` dropped, so the write is
    ``RealizedTrade(**dataclasses.asdict(record))`` and a column added to one
    without the other is a test failure rather than a silent mismatch.

    A row per matched *slice*, not per position: a close consuming two lots at
    different prices realises two trades, because each had its own basis and
    averaging them loses the only thing the rows are for.
    """

    account: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    qty: int
    open_price: Decimal
    close_price: Decimal
    #: Signed. ``(close - open) x qty x multiplier`` on a long, inverted on a
    #: short.
    pnl: Decimal
    #: Percent units, denominated on cost basis -- so a short's basis is the
    #: credit received. ``None`` where that basis is zero: a percentage of
    #: nothing is not zero percent, and 0% would read as a flat trade.
    pnl_pct: Decimal | None
    close_kind: CloseKind


@dataclass(frozen=True, slots=True)
class Normalisation:
    """What :func:`normalise_activities` produces: movements, fees, refusals."""

    movements: tuple[LotMovement, ...] = ()
    fees: tuple[FeeRecord, ...] = ()
    rejections: tuple[LedgerRejection, ...] = ()


@dataclass(frozen=True, slots=True)
class MatchResult:
    """What :func:`match_movements` produces."""

    trades: tuple[RealizedTradeRecord, ...] = ()
    open_lots: tuple[OpenLot, ...] = ()
    deliveries: tuple[ShareDelivery, ...] = ()
    rejections: tuple[LedgerRejection, ...] = ()


@dataclass(frozen=True, slots=True)
class Ledger:
    """The whole answer for one account: trades, what is still open, and why not."""

    account: str
    trades: tuple[RealizedTradeRecord, ...] = ()
    open_lots: tuple[OpenLot, ...] = ()
    deliveries: tuple[ShareDelivery, ...] = ()
    fees: tuple[FeeRecord, ...] = ()
    movements: tuple[LotMovement, ...] = ()
    rejections: tuple[LedgerRejection, ...] = ()

    @property
    def realized_pnl(self) -> Decimal:
        return sum((trade.pnl for trade in self.trades), Decimal(0))

    @property
    def total_fees(self) -> Decimal:
        """Signed, so a charge is negative. Every fee, attributed or not."""
        return sum((charge.amount for charge in self.fees), Decimal(0))

    @property
    def attributed_fees(self) -> Decimal:
        return sum(
            (charge.amount for charge in self.fees if charge.is_attributed), Decimal(0)
        )

    @property
    def unattributed_fees(self) -> Decimal:
        """Reported, never dropped. On the recorded history this is all of them."""
        return sum(
            (charge.amount for charge in self.fees if not charge.is_attributed),
            Decimal(0),
        )


# --------------------------------------------------------------------------
# The fill-to-order join
# --------------------------------------------------------------------------


def intents_from_orders(orders: Iterable[Order]) -> dict[str, PositionIntent]:
    """Map order id to ``position_intent``, indexing **legs** as well as parents.

    A fill's ``order_id`` is the *leg's* id on an mleg order -- each leg is a
    full order object with its own id and the parent's id appears nowhere on
    the fill -- so indexing only the top level would resolve the intent of
    every single-leg order and none of a spread's.

    An mleg *parent* has no ``position_intent`` at all (it is the structure,
    the legs are the instruments) and is simply absent from the result. That
    is deliberate: nothing should be able to read an intent off a parent.
    """
    intents: dict[str, PositionIntent] = {}

    def walk(order: Order) -> None:
        if order.position_intent is not None:
            intents[order.id] = order.position_intent
        for leg in order.legs:
            walk(leg)

    for order in orders:
        walk(order)
    return intents


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def _activity_at(activity: Activity) -> datetime | None:
    if isinstance(activity, TradeActivity):
        return activity.transaction_time
    if activity.created_at is not None:
        return activity.created_at
    if activity.activity_date is not None:
        return datetime.combine(activity.activity_date, time.min, tzinfo=timezone.utc)
    return None


def _reject(
    sink: list[LedgerRejection],
    rule: RejectionRule,
    detail: str,
    *,
    activity_id: str | None = None,
    symbol: str | None = None,
    at: datetime | None = None,
    **inputs: str,
) -> None:
    """Record a refusal, and log it.

    Both, not either. The returned record is what makes the refusal
    un-droppable; the log line is what makes it visible without a caller
    remembering to look. Everything that indicates a *problem* logs at
    warning; the by-design declines -- a stock trade, a cash journal -- log at
    info, because they are expected and still not silent.
    """
    rejection = LedgerRejection(
        rule=rule,
        detail=detail,
        activity_id=activity_id,
        symbol=symbol,
        at=at,
        inputs=dict(inputs),
    )
    sink.append(rejection)
    expected = rule in (
        RejectionRule.NOT_A_LEDGER_ACTIVITY,
        RejectionRule.NOT_AN_OPTION,
    )
    logger.log(
        logging.INFO if expected else logging.WARNING,
        "ledger declined an activity: %s -- %s",
        rule.value,
        detail,
        extra={
            "rule": rule.value,
            "activity_id": activity_id,
            "activity_symbol": symbol,
            "at": at.isoformat() if at is not None else None,
            "inputs": dict(inputs),
        },
    )


def _redacted(text: str | None) -> str | None:
    """Vendor free text, fit to leave this module in a log line or a return.

    Rule 6 -- *"no keys in code, in tests, in fixtures, or in log output"* --
    and the account identifiers are in this project's scanned set for a
    specific reason: **field-name redaction cannot see inside prose**. A
    ``FEE`` row's ``description`` reads *"CAT fee for proceed of 15 trades on
    <date> by PA..."*, so a rule that blanks a field called ``account_number``
    never fires on it. The first fixture recording wrote the real number into
    eight rows in plain text before this was caught, which is why redaction
    became a substring pass rather than a field pass.

    :func:`~corollary.wire.vendor_detail` already owns that pass and is
    deliberately vendor-neutral -- no ``alpaca`` import, no ``.json()`` -- so
    it is reused here rather than reimplemented. A second redactor is a second
    thing to keep current, and the one that falls behind is the one that
    leaks. It also collapses whitespace and bounds the length, both of which a
    string bound for a log record wants anyway.

    ``None`` stays ``None``: an absent description is not an empty one, and
    the two say different things about what the vendor sent.
    """
    if text is None:
        return None
    return vendor_detail(text)


def _whole(value: Decimal) -> int | None:
    """A whole contract count, or ``None`` if the value is fractional."""
    if value != value.to_integral_value():
        return None
    return int(value)


def _option(symbol: str | None) -> OccSymbol | None:
    if not symbol:
        return None
    try:
        return parse_occ_symbol(symbol)
    except ValueError:
        return None


def _intrinsic(contract: OccSymbol, settlement: Decimal) -> Decimal:
    """What the contract is worth at settlement, **per underlying share**.

    A call is in the money above its strike and a put below it, and at the
    strike exactly both are worth zero because intrinsic value is zero.

    Per *share* is the unit, and it is the whole reason
    :func:`_unverified_deliverable` exists: turning this into dollars means
    multiplying by the shares the contract delivers, and the matcher spells
    that ``x multiplier``. That step is sound only where the deliverable has
    been checked, so the check gates the multiplication rather than this
    function, which is right on any contract.
    """
    if contract.option_type is OptionType.CALL:
        return max(settlement - contract.strike, Decimal(0))
    return max(contract.strike - settlement, Decimal(0))


def _distinct_by_activity_id(
    activities: Iterable[Activity], rejections: list[LedgerRejection]
) -> list[Activity]:
    """Drop exact repeats; **report** rows that share an id and differ.

    Ingestion is idempotent on ``activity_id``, and re-pulling an overlapping
    window is the normal case -- so an identical repeat is deduped quietly.

    A *disagreeing* repeat is a different animal. Alpaca's doc examples give
    both rows of an option-event pair the same ``id``, which is probably a
    copy-paste artifact and is unverified either way; if it is real then
    ``fill(activity_id UNIQUE)`` silently drops the second row of every option
    event. So both rows are kept and passed on -- dropping either is the
    silent loss -- and the collision is reported so the constraint failure
    that follows has an explanation waiting for it.
    """
    grouped: dict[str, list[Activity]] = {}
    for activity in activities:
        bucket = grouped.setdefault(activity.id, [])
        if not any(activity == seen for seen in bucket):
            bucket.append(activity)

    distinct: list[Activity] = []
    for activity_id, bucket in grouped.items():
        if len(bucket) > 1:
            _reject(
                rejections,
                RejectionRule.DUPLICATE_ACTIVITY_ID,
                f"{len(bucket)} different activities share the id {activity_id!r}; "
                "fill(activity_id UNIQUE) can hold only one of them, so one will "
                "be lost on write unless the ingest keys on something wider",
                activity_id=activity_id,
                at=_activity_at(bucket[0]),
                rows=str(len(bucket)),
                activity_types=",".join(row.activity_type for row in bucket),
                symbols=",".join(str(getattr(row, "symbol", "") or "") for row in bucket),
            )
        distinct.extend(bucket)
    return distinct


def _movement_from_fill(
    activity: TradeActivity,
    intents: Mapping[str, PositionIntent],
    rejections: list[LedgerRejection],
) -> LotMovement | None:
    contract = _option(activity.symbol)
    if contract is None:
        _reject(
            rejections,
            RejectionRule.NOT_AN_OPTION,
            f"{activity.symbol!r} is not an OCC option symbol, so it is not a "
            "contract this ledger holds lots for",
            activity_id=activity.id,
            symbol=activity.symbol,
            at=activity.transaction_time,
            activity_type=activity.activity_type,
            symbol_seen=activity.symbol,
        )
        return None

    qty = _whole(activity.quantity)
    if qty is None or qty <= 0:
        _reject(
            rejections,
            RejectionRule.FRACTIONAL_QUANTITY,
            f"a fill quantity of {activity.quantity} is not a whole number of "
            "contracts",
            activity_id=activity.id,
            symbol=activity.symbol,
            at=activity.transaction_time,
            qty=str(activity.quantity),
        )
        return None

    implied = activity.side.implied_intent
    joined = intents.get(activity.order_id)
    if implied is not None and joined is not None and implied is not joined:
        _reject(
            rejections,
            RejectionRule.INTENT_CONTRADICTS_SIDE,
            f"side {activity.side.value!r} implies {implied.value!r} but the "
            f"order says {joined.value!r}; there is no way to tell which is "
            "wrong, so neither is used",
            activity_id=activity.id,
            symbol=activity.symbol,
            at=activity.transaction_time,
            side=activity.side.value,
            implied_intent=implied.value,
            joined_intent=joined.value,
            order_id=activity.order_id,
        )
        return None

    intent = joined or implied
    if intent is None:
        _reject(
            rejections,
            RejectionRule.UNKNOWN_INTENT,
            "side 'buy' is both buy-to-open and buy-to-close and no order was "
            "joined, so the intent is unknown; guessing would book a "
            "buy-to-close as a new lot and double the position",
            activity_id=activity.id,
            symbol=activity.symbol,
            at=activity.transaction_time,
            side=activity.side.value,
            order_id=activity.order_id,
        )
        return None

    return LotMovement(
        activity_id=activity.id,
        activity_type=activity.activity_type,
        symbol=contract.symbol,
        side=activity.side,
        intent=intent,
        qty=qty,
        price=activity.price,
        at=activity.transaction_time,
        order_id=activity.order_id,
    )


def _movement_from_option_event(
    activity: NonTradeActivity,
    close_kind: CloseKind,
    settlements: Mapping[str, Decimal],
    paired: Mapping[str, NonTradeActivity],
    rejections: list[LedgerRejection],
) -> LotMovement | None:
    when = _activity_at(activity)
    contract = _option(activity.symbol)
    if contract is None:
        rule = (
            RejectionRule.MISSING_SYMBOL
            if not activity.symbol
            else RejectionRule.NOT_AN_OPTION
        )
        _reject(
            rejections,
            rule,
            f"an {activity.activity_type} row needs an OCC option symbol; got "
            f"{activity.symbol!r}",
            activity_id=activity.id,
            symbol=activity.symbol,
            at=when,
            activity_type=activity.activity_type,
        )
        return None

    if activity.quantity is None or activity.quantity == 0:
        _reject(
            rejections,
            RejectionRule.MISSING_EVENT_QUANTITY,
            f"an {activity.activity_type} row carries a signed quantity and this "
            f"one has {activity.quantity!r}, so which lots it closes is unknown",
            activity_id=activity.id,
            symbol=contract.symbol,
            at=when,
            activity_type=activity.activity_type,
            qty=str(activity.quantity),
        )
        return None

    signed = _whole(activity.quantity)
    if signed is None:
        _reject(
            rejections,
            RejectionRule.FRACTIONAL_QUANTITY,
            f"a quantity of {activity.quantity} is not a whole number of contracts",
            activity_id=activity.id,
            symbol=contract.symbol,
            at=when,
            qty=str(activity.quantity),
        )
        return None

    if when is None:
        _reject(
            rejections,
            RejectionRule.MISSING_EVENT_QUANTITY,
            f"an {activity.activity_type} row carries no timestamp, so it cannot "
            "be placed in a lot sequence",
            activity_id=activity.id,
            symbol=contract.symbol,
            activity_type=activity.activity_type,
        )
        return None

    # The sign is the change in contract position, and an option event always
    # closes: negative means contracts left a long, positive means a short was
    # bought back or assigned away. That makes the intent decidable with no
    # order to join to -- which is just as well, because a non-trade activity
    # has no `order_id` at all.
    if signed < 0:
        side, intent = FillSide.SELL, PositionIntent.SELL_TO_CLOSE
    else:
        side, intent = FillSide.BUY, PositionIntent.BUY_TO_CLOSE

    if close_kind is CloseKind.EXPIRY:
        price = Decimal(0)
    else:
        settlement = settlements.get(contract.symbol)
        if settlement is None:
            _reject(
                rejections,
                RejectionRule.UNPRICED_OPTION_EVENT,
                f"an {activity.activity_type} on {contract.symbol} settles at its "
                "intrinsic value, which needs the underlying's settlement price; "
                "none was supplied. net_amount is 0 on this row and booking that "
                "would report a total loss, and booking the strike would report "
                "an absurd gain, so the event is left unmatched",
                activity_id=activity.id,
                symbol=contract.symbol,
                at=when,
                activity_type=activity.activity_type,
                strike=str(contract.strike),
                net_amount=str(activity.net_amount),
            )
            return None
        price = _intrinsic(contract, settlement)

        sibling = paired.get(activity.group_id) if activity.group_id else None
        if (
            sibling is not None
            and sibling.price is not None
            and sibling.price != contract.strike
        ):
            _reject(
                rejections,
                RejectionRule.PAIRED_STRIKE_DISAGREES,
                f"the paired {PAIRED_TRADE_ACTIVITY_TYPE} says the event settled "
                f"at {sibling.price} and the OCC symbol says the strike is "
                f"{contract.strike}; the symbol is used, and the disagreement "
                "means one of the two assumptions about this pair is wrong",
                activity_id=activity.id,
                symbol=contract.symbol,
                at=when,
                paired_price=str(sibling.price),
                strike=str(contract.strike),
                group_id=activity.group_id or "",
            )

    return LotMovement(
        activity_id=activity.id,
        activity_type=activity.activity_type,
        symbol=contract.symbol,
        side=side,
        intent=intent,
        qty=abs(signed),
        price=price,
        at=when,
        group_id=activity.group_id,
        execution_id=activity.execution_id,
    )


def _execution_of(movement: LotMovement) -> str | None:
    """The execution this movement *is*, as a fee's ``execution_id`` spells it.

    Two sources, and the second is the one that makes the chain's middle hop
    reach a fill at all. A non-trade row carries ``execution_id`` outright. A
    ``FILL`` carries no such field -- which looks like a dead end until you
    notice where Alpaca puts the execution's identity: the composite activity
    id is ``<17-digit stamp>::<uuid>`` and that **uuid is the execution**.
    Measured on the recording, 15 of the 15 fees carrying an ``execution_id``
    match the tail of a fill's id, one to one, with 15 distinct tails -- so
    the join is a relation rather than a coincidence.

    An id with no separator has no uuid half and returns ``None`` rather than
    the whole id. Falling back to the whole id would let a fee attribute to a
    row whose *identifier* merely happened to collide with an execution id,
    which is an attribution built on nothing.
    """
    if movement.execution_id:
        return movement.execution_id
    head, separator, tail = movement.activity_id.rpartition(ACTIVITY_ID_SEPARATOR)
    return tail if separator and head else None


def _fee_order_id(activity: NonTradeActivity) -> str | None:
    """The chain's first hop, which only ``extra`` can supply.

    ``NonTradeActivity`` has no ``order_id`` field because the published
    schema has none and this account has never sent one. ``extra`` exists
    precisely because that schema is demonstrably incomplete -- three fields
    appear in live responses and none is in it -- so reading through it here
    is the sanctioned path rather than a workaround.
    """
    value = activity.extra.get("order_id")
    return value if isinstance(value, str) and value else None


def _attribute_fees(
    fee_rows: Sequence[NonTradeActivity],
    movements: Sequence[LotMovement],
    rejections: list[LedgerRejection],
) -> list[FeeRecord]:
    """Attribute each fee down the chain: order_id, execution_id, group_id.

    A row with no ``net_amount`` never reaches the chain. ``net_amount`` is
    ``Decimal | None`` and ``wire.as_decimal("")`` answers ``None``, so an
    absent field and an empty string arrive identically; either way *what the
    broker charged is unknown*, and the one thing that must not happen is
    substituting a number for it. Booked at zero the charge vanishes from
    :attr:`Ledger.total_fees` and the reconciliation invariant stops closing
    with nothing in :attr:`Ledger.rejections` to explain the gap -- the same
    ``?? 7`` failure CLAUDE.md names, one layer down. So it is refused and
    reported, exactly as :attr:`RejectionRule.UNPRICED_OPTION_EVENT`,
    :attr:`RejectionRule.UNKNOWN_MULTIPLIER` and
    :attr:`RejectionRule.MISSING_EVENT_QUANTITY` refuse the absences they meet.
    """
    by_order: dict[str, list[LotMovement]] = {}
    by_execution: dict[str, list[LotMovement]] = {}
    by_group: dict[str, list[LotMovement]] = {}
    for movement in movements:
        if movement.order_id:
            by_order.setdefault(movement.order_id, []).append(movement)
        execution = _execution_of(movement)
        if execution:
            by_execution.setdefault(execution, []).append(movement)
        if movement.group_id:
            by_group.setdefault(movement.group_id, []).append(movement)

    records: list[FeeRecord] = []
    for activity in fee_rows:
        amount = activity.net_amount
        if amount is None:
            _reject(
                rejections,
                RejectionRule.MISSING_FEE_AMOUNT,
                f"a {FEE_ACTIVITY_TYPE} row carries no net_amount, so what it "
                "charged is unknown. Booking it at zero would understate "
                "total_fees by the whole charge and break the reconciliation "
                "silently, so the row is left uncounted and reported instead",
                activity_id=activity.id,
                symbol=activity.symbol,
                at=_activity_at(activity),
                activity_type=activity.activity_type,
                activity_sub_type=activity.activity_sub_type or "",
                # Rule 6: this reaches a WARNING log record verbatim, and a
                # FEE description is where the account number lives in prose.
                description=_redacted(activity.description) or "",
                net_amount=str(activity.net_amount),
            )
            continue

        matched: list[LotMovement] = []
        link: FeeLink | None = None
        for candidate_link, key, index in (
            (FeeLink.ORDER_ID, _fee_order_id(activity), by_order),
            (FeeLink.EXECUTION_ID, activity.execution_id, by_execution),
            (FeeLink.GROUP_ID, activity.group_id, by_group),
        ):
            if key and key in index:
                matched = index[key]
                link = candidate_link
                break

        symbols = {movement.symbol for movement in matched}
        records.append(
            FeeRecord(
                activity_id=activity.id,
                amount=amount,
                activity_sub_type=activity.activity_sub_type,
                description=_redacted(activity.description),
                at=_activity_at(activity),
                link=link,
                # Exactly one movement means the fee has a home; several
                # sharing a key means it belongs to the order or the group
                # rather than to one execution, and saying so beats picking.
                movement_activity_id=matched[0].activity_id if len(matched) == 1 else None,
                symbol=symbols.pop() if len(symbols) == 1 else None,
            )
        )
    return records


def normalise_activities(
    activities: Iterable[Activity],
    *,
    intents: Mapping[str, PositionIntent],
    settlements: Mapping[str, Decimal] | None = None,
) -> Normalisation:
    """Turn both activity shapes into one sequence of lot movements, plus fees.

    ``intents`` maps an order id to its ``position_intent`` -- build it with
    :func:`intents_from_orders`. Only ``buy`` fills actually need it; the other
    two ``side`` values decide themselves.

    ``settlements`` maps an **option symbol** to the underlying's settlement
    price, and is required only for ``OPEXC`` and ``OPASN``. Keyed on the
    contract rather than on ``(underlying, date)`` because the contract is the
    thing the event is about, and because resolving an adjusted root back to
    its underlying is the caller's problem, not a guess this module should
    make. An event with no entry is refused and reported.
    """
    prices = settlements or {}
    rejections: list[LedgerRejection] = []
    movements: list[LotMovement] = []
    fee_rows: list[NonTradeActivity] = []

    rows = _distinct_by_activity_id(activities, rejections)

    # The money row for an option event may arrive either side of the event
    # itself, so it is indexed before anything is matched against it.
    paired: dict[str, NonTradeActivity] = {
        row.group_id: row
        for row in rows
        if isinstance(row, NonTradeActivity)
        and row.activity_type == PAIRED_TRADE_ACTIVITY_TYPE
        and row.group_id
    }

    for activity in rows:
        if isinstance(activity, TradeActivity):
            if activity.activity_type not in FILL_ACTIVITY_TYPES:
                _reject(
                    rejections,
                    RejectionRule.NOT_A_LEDGER_ACTIVITY,
                    f"{activity.activity_type!r} arrived on the trade branch but is "
                    "not a fill, so what it does to a lot is unknown",
                    activity_id=activity.id,
                    symbol=activity.symbol,
                    at=activity.transaction_time,
                    activity_type=activity.activity_type,
                )
                continue
            movement = _movement_from_fill(activity, intents, rejections)
            if movement is not None:
                movements.append(movement)
            continue

        if activity.activity_type == FEE_ACTIVITY_TYPE:
            fee_rows.append(activity)
            continue

        close_kind = OPTION_EVENT_TYPES.get(activity.activity_type)
        if close_kind is None:
            _reject(
                rejections,
                RejectionRule.NOT_A_LEDGER_ACTIVITY,
                f"{activity.activity_type!r} is not a fill, an option event or a "
                "fee. It may still be money -- a journal, a transfer, a dividend "
                "-- but it is not a lot movement and this ledger does not model it",
                activity_id=activity.id,
                symbol=activity.symbol,
                at=_activity_at(activity),
                activity_type=activity.activity_type,
                net_amount=str(activity.net_amount),
            )
            continue

        movement = _movement_from_option_event(
            activity, close_kind, prices, paired, rejections
        )
        if movement is not None:
            movements.append(movement)

    fees = _attribute_fees(fee_rows, movements, rejections)

    return Normalisation(
        movements=tuple(movements),
        fees=tuple(fees),
        rejections=tuple(rejections),
    )


# --------------------------------------------------------------------------
# The matcher
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Lot:
    """A mutable open lot. Private: the queue shrinks it as closes consume it."""

    qty: int
    price: Decimal
    at: datetime
    activity_id: str
    is_short: bool


def _pnl_pct(pnl: Decimal, basis: Decimal) -> Decimal | None:
    if basis == 0:
        return None
    return (pnl / basis * 100).quantize(PCT_QUANTUM, rounding=ROUND_HALF_EVEN)


def _realize(
    account: str,
    symbol: str,
    lot: _Lot,
    closing: LotMovement,
    close_price: Decimal,
    qty: int,
    multiplier: Decimal,
) -> RealizedTradeRecord:
    """One matched slice, in dollars.

    ``close_price`` is passed rather than read off ``closing`` because
    :attr:`LotMovement.price` is nullable and a trade may not be booked
    against an unknown one. The caller narrows it; there is no fallback here
    to narrow it wrongly.

    **Precondition on the exercise/assignment path:** ``close_price`` there is
    intrinsic value per underlying share, so ``x multiplier`` below is
    asserting that the contract delivers ``multiplier`` shares of the
    underlying at the strike. The caller establishes that with
    :func:`_unverified_deliverable` before calling and refuses instead where
    it cannot. On a closing fill and on ``OPEXP`` there is nothing to
    establish -- both are premium, and premium times the multiplier is right
    on any contract, adjusted or not.
    """
    move = (close_price - lot.price) * qty * multiplier
    pnl = -move if lot.is_short else move
    # The basis is a magnitude: for a short it is the credit received, which is
    # what makes a gain on a short a positive percentage of it. `orders.ts`
    # states the same fact from the other side, as a negative `openUnitValue`.
    basis = lot.price * qty * multiplier
    return RealizedTradeRecord(
        account=account,
        symbol=symbol,
        opened_at=lot.at,
        closed_at=closing.at,
        qty=qty,
        open_price=lot.price,
        close_price=close_price,
        pnl=pnl,
        pnl_pct=_pnl_pct(pnl, basis),
        close_kind=closing.close_kind,
    )


def _unverified_deliverable(
    terms: OptionContract | None,
) -> tuple[str, dict[str, str]] | None:
    """Why an exercise's settlement P&L cannot be booked, or ``None`` if it can.

    Two answers refuse and one permits, and the permitting one requires
    evidence rather than silence:

    * **terms absent** -> refused. Detection is
      ``root_symbol != underlying_symbol`` and that comparison needs both
      sides, so with no terms the question cannot be asked at all. Reading
      "not adjusted" off the absence of an answer is the same mistake as
      defaulting an absent multiplier to 100, one field over.
    * **``root_symbol != underlying_symbol``** -> refused. The contract is
      adjusted, so the deliverable is not ``multiplier`` shares of the
      underlying and the settlement arithmetic's premise is false.
    * otherwise -> ``None``, and the trade books normally.

    **Booking a correct number for the refused cases is an open spec
    question, not an omission.** The design spec
    (``docs/superpowers/specs/2026-09-10-phase-2-real-data-design.md``,
    *"The adjusted-contract warning has an exact answer"*) fixes *detection*
    and stops there: it says nothing about how an adjusted contract's
    exercise should be valued. Deriving a ratio from ``deliverables``,
    ``size`` or an assumed split factor is available and is deliberately not
    done -- ``size`` carries Alpaca's own warning against exactly that use,
    and the adjustment style ("deliverable changed" vs "cash in lieu" vs a
    mixed basket) decides the arithmetic in ways none of those fields state.
    A wrong guess here is a wrong dollar figure on the Activity page with
    nothing to mark it, which is the failure class this module exists to
    avoid. Whoever answers the spec question should replace this refusal with
    the arithmetic, and until then the boundary is here rather than buried in
    a multiplication.
    """
    if terms is None:
        return (
            "no contract terms were supplied, and detection is "
            "root_symbol != underlying_symbol -- a comparison with only one "
            "side, so the question cannot be asked",
            {"terms": "absent", "root_symbol": "", "underlying_symbol": ""},
        )
    if terms.is_adjusted:
        return (
            f"the root {terms.root_symbol!r} is not the underlying "
            f"{terms.underlying_symbol!r}, so this is an adjusted contract and "
            "its deliverable is not multiplier shares of the underlying",
            {
                "terms": "adjusted",
                "root_symbol": terms.root_symbol,
                "underlying_symbol": terms.underlying_symbol,
            },
        )
    return None


def _deliverable_shares(terms: OptionContract | None) -> Decimal | None:
    """Shares **one** contract delivers, or ``None`` when that is not knowable.

    The only field that answers this is ``deliverables``, and it is populated
    only when the request carried ``show_deliverables=true`` -- so an empty
    tuple means *unknown*, never *standard*. Three answers, not two:

    * one equity deliverable in the underlying -> its ``amount``, whatever that
      is. 100 on a standard contract, 150 after a 3:2 split, and both are
      statable.
    * several deliverables, or one that is not the underlying's stock -- a
      ``GME1`` delivering 100 GME **plus** 10 GME.WS -- -> ``None``. No single
      share count says that, and the number that fits would silently drop the
      warrants.
    * no deliverables at all, or an ``amount`` Alpaca has not determined yet
      -> ``None``.

    ``multiplier`` is deliberately not consulted. It is contracts per unit of
    premium; reading it as a share count is Alpaca's warning about ``size``
    run backwards, and it reports ``100`` on every live adjusted contract.
    """
    if terms is None or len(terms.deliverables) != 1:
        return None
    only = terms.deliverables[0]
    if only.type != "equity" or only.symbol != terms.underlying_symbol:
        return None
    return only.amount


def _delivery(
    contract: OccSymbol,
    lot: _Lot,
    closing: LotMovement,
    qty: int,
    terms: OptionContract | None,
) -> ShareDelivery:
    """The shares an exercise or assignment moved. Named, not tracked.

    A call's holder receives shares and a put's holder delivers them, and a
    short is the other side of whichever it is -- so the sign is positive
    exactly when "is a call" and "was long" agree. The *count* is the
    deliverable and the sign applies to it; where the count is unknown the
    sign has nothing to attach to and the whole field is ``None``.
    """
    receiving = (contract.option_type is OptionType.CALL) != lot.is_short
    per_contract = _deliverable_shares(terms)
    shares = (
        None if per_contract is None else qty * per_contract * (1 if receiving else -1)
    )
    return ShareDelivery(
        activity_id=closing.activity_id,
        contract_symbol=contract.symbol,
        underlying=terms.underlying_symbol if terms is not None else contract.root,
        is_adjusted=terms.is_adjusted if terms is not None else None,
        option_type=contract.option_type,
        shares=shares,
        price=contract.strike,
        at=closing.at,
        close_kind=closing.close_kind,
    )


def match_movements(
    movements: Iterable[LotMovement],
    *,
    account: str,
    multipliers: Mapping[str, Decimal],
    contracts: Mapping[str, OptionContract] | None = None,
) -> MatchResult:
    """Run the open-lot queues. One queue per contract symbol, FIFO.

    ``multipliers`` maps an option symbol to its contract multiplier, read per
    contract from ``/v2/options/contracts`` and cached. A symbol absent from it
    is **refused**: 100 is the standard deliverable, not a safe default, and an
    adjusted contract that silently used it would make every figure derived
    from this row wrong by the ratio.

    ``contracts`` is the reference data from the same endpoint, and it is read
    for two different purposes. :class:`ShareDelivery` takes the underlying's
    real ticker and, from ``deliverables``, how many shares an exercise
    actually moved; omitting it costs a delivery its share count and its
    adjusted flag and never substitutes 100, for the same reason
    ``multipliers`` has no fallback.

    The ``OPEXC``/``OPASN`` **money** path reads one field from it --
    ``root_symbol`` against ``underlying_symbol`` -- and refuses the trade
    where they differ or where the terms are absent, because the settlement
    price is synthesised from the underlying and that arithmetic assumes a
    deliverable an adjusted contract does not have. See
    :func:`_unverified_deliverable`, and the module docstring for why the
    multiplier cannot check this for itself.

    So ``contracts`` stays optional, but "optional" now costs an exercise its
    P&L rather than costing a delivery a share count: omitting it is still
    safe, in the sense that nothing wrong is reported, and it is no longer
    free. Closing fills and ``OPEXP`` are unaffected either way. The name
    matches ``grouping.group_positions`` deliberately, one convention rather
    than two.
    """
    terms: Mapping[str, OptionContract] = contracts if contracts is not None else {}
    trades: list[RealizedTradeRecord] = []
    open_lots: list[OpenLot] = []
    deliveries: list[ShareDelivery] = []
    rejections: list[LedgerRejection] = []

    by_symbol: dict[str, list[LotMovement]] = {}
    for movement in movements:
        by_symbol.setdefault(movement.symbol, []).append(movement)

    for symbol in sorted(by_symbol):
        multiplier = multipliers.get(symbol)
        if multiplier is None:
            for movement in by_symbol[symbol]:
                _reject(
                    rejections,
                    RejectionRule.UNKNOWN_MULTIPLIER,
                    f"no contract multiplier for {symbol}; it comes per contract "
                    "from the contracts endpoint and 100 is not a safe default, "
                    "because an adjusted contract's deliverable is not 100 shares",
                    activity_id=movement.activity_id,
                    symbol=symbol,
                    at=movement.at,
                    activity_type=movement.activity_type,
                )
            continue

        contract = _option(symbol)
        if contract is None:  # pragma: no cover - normalisation refuses these
            continue

        # Sorted *within* the symbol, which is the ordering the real recording
        # actually guarantees: no global chronological order exists. Stable, so
        # two movements sharing an instant keep the order they arrived in.
        ordered = sorted(by_symbol[symbol], key=lambda movement: movement.at)
        queue: deque[_Lot] = deque()

        for movement in ordered:
            if movement.is_opening:
                if queue and queue[0].is_short != movement.opens_short:
                    _reject(
                        rejections,
                        RejectionRule.DIRECTION_CONFLICT,
                        f"{movement.intent.value!r} opens a "
                        f"{'short' if movement.opens_short else 'long'} lot in "
                        f"{symbol} while the queue already holds "
                        f"{'short' if queue[0].is_short else 'long'} lots; a "
                        "broker nets, so this state should not exist",
                        activity_id=movement.activity_id,
                        symbol=symbol,
                        at=movement.at,
                        intent=movement.intent.value,
                        queue_is_short=str(queue[0].is_short),
                    )
                    continue
                if movement.price is None:
                    _reject(
                        rejections,
                        RejectionRule.UNPRICED_OPTION_EVENT,
                        f"{movement.intent.value!r} opens a lot in {symbol} with "
                        "no price, so it has no cost basis and every P&L "
                        "measured from it would be measured from nothing",
                        activity_id=movement.activity_id,
                        symbol=symbol,
                        at=movement.at,
                        intent=movement.intent.value,
                        activity_type=movement.activity_type,
                    )
                    continue
                queue.append(
                    _Lot(
                        qty=movement.qty,
                        price=movement.price,
                        at=movement.at,
                        activity_id=movement.activity_id,
                        is_short=movement.opens_short,
                    )
                )
                continue

            if queue and queue[0].is_short != movement.closes_short:
                _reject(
                    rejections,
                    RejectionRule.DIRECTION_CONFLICT,
                    f"{movement.intent.value!r} closes a "
                    f"{'short' if movement.closes_short else 'long'} lot in "
                    f"{symbol} but the open lots are "
                    f"{'short' if queue[0].is_short else 'long'}",
                    activity_id=movement.activity_id,
                    symbol=symbol,
                    at=movement.at,
                    intent=movement.intent.value,
                    queue_is_short=str(queue[0].is_short),
                )
                continue

            # An exercise or assignment is the one close whose price is
            # synthesised from the *underlying* rather than paid in premium,
            # so it is the one close whose dollars depend on the deliverable.
            # `_unverified_deliverable` decides whether that dependency is
            # safe; a closing fill and an OPEXP never ask.
            settles_off_underlying = movement.close_kind in (
                CloseKind.EXERCISE,
                CloseKind.ASSIGNMENT,
            )
            unverified = (
                _unverified_deliverable(terms.get(symbol))
                if settles_off_underlying
                else None
            )
            if unverified is not None:
                reason, evidence = unverified
                _reject(
                    rejections,
                    RejectionRule.UNVERIFIED_DELIVERABLE,
                    f"an {movement.activity_type} on {symbol} settles against the "
                    "underlying's price, and booking that as "
                    "(close - open) x qty x multiplier assumes the contract "
                    f"delivers {multiplier} shares of the underlying at the "
                    f"strike -- but {reason}. No realized P&L is booked for it, "
                    "because the number that would be booked is wrong by the "
                    "adjustment ratio and nothing downstream could tell. The "
                    "shares it moved are still reported as a delivery",
                    activity_id=movement.activity_id,
                    symbol=symbol,
                    at=movement.at,
                    activity_type=movement.activity_type,
                    close_kind=movement.close_kind.value,
                    multiplier=str(multiplier),
                    closing_qty=str(movement.qty),
                    **evidence,
                )

            close_price = movement.price
            if unverified is None and close_price is None:
                _reject(
                    rejections,
                    RejectionRule.UNPRICED_OPTION_EVENT,
                    f"{movement.activity_type} closes {movement.qty} contracts of "
                    f"{symbol} at no price, so there is nothing to subtract the "
                    "basis from. The lots are still consumed, because the "
                    "contracts really are gone",
                    activity_id=movement.activity_id,
                    symbol=symbol,
                    at=movement.at,
                    activity_type=movement.activity_type,
                    closing_qty=str(movement.qty),
                )

            remaining = movement.qty
            while remaining > 0 and queue:
                lot = queue[0]
                taken = min(remaining, lot.qty)
                # Refused: no trade, but the lot is still consumed. The
                # contracts really are gone, and leaving a phantom open lot
                # would put `open_lots` at odds with `/v2/positions` -- a
                # second wrong number, and the more misleading one, since it
                # reads as a position you could still act on.
                if unverified is None and close_price is not None:
                    trades.append(
                        _realize(
                            account,
                            symbol,
                            lot,
                            movement,
                            close_price,
                            taken,
                            multiplier,
                        )
                    )
                if settles_off_underlying:
                    deliveries.append(
                        _delivery(contract, lot, movement, taken, terms.get(symbol))
                    )
                lot.qty -= taken
                remaining -= taken
                if lot.qty == 0:
                    queue.popleft()

            if remaining > 0:
                _reject(
                    rejections,
                    RejectionRule.OVER_CLOSE,
                    f"{movement.intent.value!r} closes {movement.qty} contracts of "
                    f"{symbol} but only {movement.qty - remaining} were open; "
                    f"{remaining} are unmatched, which means the opening fill is "
                    "outside the window loaded or was never ingested",
                    activity_id=movement.activity_id,
                    symbol=symbol,
                    at=movement.at,
                    closing_qty=str(movement.qty),
                    unmatched_qty=str(remaining),
                )

        open_lots.extend(
            OpenLot(
                symbol=symbol,
                qty=lot.qty,
                price=lot.price,
                opened_at=lot.at,
                activity_id=lot.activity_id,
                is_short=lot.is_short,
            )
            for lot in queue
        )

    # Deterministic, and ordered the way the Activity page reads: by close.
    # Stable, so within one instant the per-symbol order above survives.
    trades.sort(key=lambda trade: trade.closed_at)
    return MatchResult(
        trades=tuple(trades),
        open_lots=tuple(open_lots),
        deliveries=tuple(deliveries),
        rejections=tuple(rejections),
    )


def build_ledger(
    activities: Iterable[Activity],
    *,
    account: str,
    intents: Mapping[str, PositionIntent],
    multipliers: Mapping[str, Decimal],
    settlements: Mapping[str, Decimal] | None = None,
    contracts: Mapping[str, OptionContract] | None = None,
) -> Ledger:
    """Normalise, then match. The whole module in one call.

    Deterministic: the same activities in any order produce the same
    :class:`Ledger`, because ordering is resolved per contract symbol rather
    than globally.

    ``contracts`` is optional and reaches only :class:`ShareDelivery` -- see
    :func:`match_movements`.

    **The movements it returns are the normalised ones with one amendment**:
    an exercise or assignment the matcher refused for an unverified
    deliverable comes back with :attr:`LotMovement.price` set to ``None``. The
    normaliser derived that price as intrinsic value against the OCC strike,
    and the refusal is precisely the finding that the adjustment invalidates
    that strike -- so the number is the same guess about money that open
    question 4 forbade deriving from ``deliverables`` or ``size``, one column
    over. It reaches ``fill.price`` and the Activity page's Price cell, where
    it would read as a price paid.

    The amendment is driven by the **matcher's own refusals** rather than by a
    second call to :func:`_unverified_deliverable`, so the two answers cannot
    disagree: whatever books no trade carries no price, by construction.
    """
    normalised = normalise_activities(
        activities, intents=intents, settlements=settlements
    )
    matched = match_movements(
        normalised.movements,
        account=account,
        multipliers=multipliers,
        contracts=contracts,
    )
    unpriced = {
        rejection.activity_id
        for rejection in matched.rejections
        if rejection.rule is RejectionRule.UNVERIFIED_DELIVERABLE
        and rejection.activity_id is not None
    }
    return Ledger(
        account=account,
        trades=matched.trades,
        open_lots=matched.open_lots,
        deliveries=matched.deliveries,
        fees=normalised.fees,
        movements=tuple(
            replace(movement, price=None)
            if movement.activity_id in unpriced
            else movement
            for movement in normalised.movements
        ),
        rejections=normalised.rejections + matched.rejections,
    )


# --------------------------------------------------------------------------
# The folds
# --------------------------------------------------------------------------


class RealizedPnl(Protocol):
    """What a fold needs off a realized trade.

    A protocol rather than the concrete record, so the same folds run over
    :class:`RealizedTradeRecord` straight out of the matcher and over
    ``realized_trade`` rows loaded from the database.
    """

    @property
    def pnl(self) -> Decimal: ...

    @property
    def pnl_pct(self) -> Decimal | None: ...


TradeT = TypeVar("TradeT", bound=RealizedPnl)


@dataclass(frozen=True, slots=True)
class LedgerStats:
    """The Activity page's header cards, folded in Python.

    Every one of these is a fold rather than a SQL aggregate, and not by
    preference: ``pnl`` is a ``Money`` column, which is TEXT on SQLite, and
    ``SUM``, ``AVG``, ``MIN``, ``MAX`` and ``ORDER BY`` over it all raise
    ``MoneyComparisonError`` rather than answering lexicographically. Against
    two trades of -7 and -41, ``MIN`` would report -41 as the larger loss only
    by coincidence of the digits and ``ORDER BY`` would invert them.

    ``None`` means "no trades of that kind", never zero -- an average of
    nothing is not 0.00, and a card reading +$0.00 average win would say
    something false about a book that has never won.
    """

    realized_pnl: Decimal
    trades: int
    wins: int
    losses: int
    #: Closed exactly flat. Neither a win nor a loss, and counted so the three
    #: add up to :attr:`trades`.
    scratches: int
    #: Percent units. Wins over *all* closed trades, scratches included in the
    #: denominator, because a scratch is a trade that happened.
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    average_win_pct: Decimal | None
    average_loss_pct: Decimal | None


def _mean(values: Sequence[Decimal], quantum: Decimal) -> Decimal | None:
    if not values:
        return None
    total = sum(values, Decimal(0))
    return (total / len(values)).quantize(quantum, rounding=ROUND_HALF_EVEN)


def summarise(trades: Iterable[RealizedPnl]) -> LedgerStats:
    """Lifetime realized P&L, average win, average loss and win rate."""
    rows = list(trades)
    wins = [row for row in rows if row.pnl > 0]
    losses = [row for row in rows if row.pnl < 0]
    scratches = len(rows) - len(wins) - len(losses)
    return LedgerStats(
        realized_pnl=sum((row.pnl for row in rows), Decimal(0)),
        trades=len(rows),
        wins=len(wins),
        losses=len(losses),
        scratches=scratches,
        win_rate=(
            None
            if not rows
            else (Decimal(len(wins)) / len(rows) * 100).quantize(
                MONEY_QUANTUM, rounding=ROUND_HALF_EVEN
            )
        ),
        average_win=_mean([row.pnl for row in wins], MONEY_QUANTUM),
        average_loss=_mean([row.pnl for row in losses], MONEY_QUANTUM),
        average_win_pct=_mean(
            [row.pnl_pct for row in wins if row.pnl_pct is not None], PCT_QUANTUM
        ),
        average_loss_pct=_mean(
            [row.pnl_pct for row in losses if row.pnl_pct is not None], PCT_QUANTUM
        ),
    )


def biggest_loser(trades: Iterable[TradeT]) -> TradeT | None:
    """The worst closed trade, or ``None`` if nothing lost.

    A Python ``min`` over loaded rows, never ``ORDER BY pnl``: that column is
    TEXT on SQLite, so the database would answer ``'-41' < '-7'`` and name the
    wrong trade with no error anywhere.
    """
    candidates = [trade for trade in trades if trade.pnl < 0]
    if not candidates:
        return None
    return min(candidates, key=lambda trade: trade.pnl)


def realized_pnl(trades: Iterable[RealizedPnl]) -> Decimal:
    """Lifetime realized P&L. A fold, for the same reason as :class:`LedgerStats`."""
    return sum((trade.pnl for trade in trades), Decimal(0))


def as_row_kwargs(trade: RealizedTradeRecord) -> dict[str, object]:
    """``realized_trade`` column keyword arguments for one record.

    One line, and it exists to make the mirroring explicit: the record's field
    names *are* the table's column names, so a column added to one and not the
    other fails a test rather than drifting.
    """
    return dataclasses.asdict(trade)
