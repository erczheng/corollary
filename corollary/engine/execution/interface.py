"""``BrokerAccount`` — the vendor boundary for reading the book.

CLAUDE.md names two files as the entire Alpaca surface and splits them by
*market data* and *order placement*. Reading positions, orders, activities and
balances is neither, so the rule does not say who owns it. The Phase 2 design
resolves that, and the resolution is the reason this file exists:

    The trading API *is* the broker, so ``BrokerInterface`` splits:

    * **BrokerAccount** — account, positions, orders, activities. Implemented
      in Phase 2 by ``engine/execution/alpaca.py``.
    * **BrokerExecution** — submit, cancel, replace. **Does not exist until
      Phase 6.**

    API routes depend on ``BrokerAccount`` only, so ``submit_order`` is not in
    a type they can reach. That keeps rule 1 structural rather than
    disciplinary.

**The second half is absent, not stubbed, and that is load-bearing.** An empty
``Protocol`` is a type a route can reach; a method raising
``NotImplementedError`` is a method a route can call. Either turns the
guarantee back into a convention, and rule 1 exists precisely so that there is
no convention to rely on. ``tests/engine/execution/test_no_order_path.py``
pins it.

Nothing in this module knows that Alpaca exists. The types below are the
vocabulary the rest of the engine speaks — the ledger, the grouper and the API
routes see these and never a vendor payload.

Four things this file is deliberate about
-----------------------------------------

**Money is ``Decimal`` and signs are preserved, never normalised.** A short
position reports ``qty: "-1"`` *alongside* ``side: "short"``, with
``cost_basis`` and ``market_value`` both negative; an ``mleg`` order sold for a
credit reports ``filled_avg_price: "-2.01"``. Those signs are the fact that a
credit is a liability — the same rule ``web/src/lib/orders.ts`` states as a
short's ``openUnitValue`` being negative. Take an absolute value anywhere on
this path and every credit spread's payoff curve inverts.

**Intent comes from the order, never from ``side``.** On the activities
endpoint ``side`` takes three values: ``sell_short`` opens a short, ``sell``
closes a long, and ``buy`` is *both* buy-to-open and buy-to-close. Two of the
four actions are indistinguishable from ``side`` alone, which is what makes
the fill-to-order join mandatory rather than convenient — see
:attr:`FillSide.implied_intent`, which returns ``None`` for ``buy`` rather
than guessing.

**An order's legs are carried, because the join is two-hop.** A fill carries
its *leg's* order id. For a simple order the leg id and the parent id
coincide, which is what makes the mistake invisible; for an ``mleg`` order
each leg is a full order object with its own ``id`` and the parent id appears
nowhere on the fill. So reaching the parent needs ``?nested=true`` and a
leg-id → parent-id map built from :attr:`Order.leg_ids`. A one-hop join groups
nothing at all, and does so silently.

**Activities are two shapes, not one.** The ``200`` response is ``oneOf
[TradingActivities, NonTradeActivities]``, discriminated on ``activity_type``,
and the non-trade branch is modelled *permissively* because the published
schema is demonstrably incomplete: ``description``, ``price`` and
``execution_id`` all appear in live responses and none of the three is in it.
An unnamed field is therefore kept in :attr:`NonTradeActivity.extra` rather
than dropped at the boundary.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "Account",
    "Activity",
    "ActivityCategory",
    "BrokerAccount",
    "BrokerAuthError",
    "BrokerError",
    "BrokerPosition",
    "BrokerRateLimitedError",
    "EquityPoint",
    "FillSide",
    "MarginClass",
    "NonTradeActivity",
    "Order",
    "OrderClass",
    "OrderQueryStatus",
    "OrderSide",
    "PortfolioHistory",
    "PositionIntent",
    "PositionSide",
    "TradeActivity",
]

#: The composite activity id's separator. ``20260910131125598::68cda3e9-…`` is
#: a 17-digit timestamp concatenated with a UUID. **It is not a bare UUID and
#: must not be typed as one.**
#:
#: The whole id is a sound **unique key** — it is what ``fill(activity_id
#: UNIQUE)`` rests on — and a sound **resume cursor for *fetching***, because
#: Alpaca's ``page_token`` is the last row's id and the vendor consumes it
#: opaquely. It is **not** a chronological **sort** key: only the 17-digit
#: stamp half orders, and stamps repeat. Never ``ORDER BY`` it to sequence
#: fills. :attr:`_ActivityBase.stamp` carries the measurement and the
#: consequence, and is the one authority for this.
ACTIVITY_ID_SEPARATOR: Final = "::"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class BrokerError(RuntimeError):
    """The broker could not answer, and the caller must not guess.

    Every failure on this surface is one of these or a subclass, including a
    dropped connection. Rule 9's watchdog halts the engine on a broker
    failure and should not have to know what transport is underneath.
    """


class BrokerAuthError(BrokerError):
    """The credentials were rejected — 401 or 403.

    Its own type because the remedy is different from every other failure:
    nothing in the code is wrong. This is not hypothetical. The Alpaca MCP
    server is configured with non-paper keys and answers ``401 {'code':
    40110000, 'message': 'request is not authorized'}`` on every trading
    endpoint, which cost an afternoon once already.
    """


class BrokerRateLimitedError(BrokerError):
    """The vendor returned 429 despite the local budget.

    Reaching this means the local :class:`~corollary.ratelimit.TokenBucket`
    and the server disagree — another process sharing the key, or a restart
    that reset the bucket while the server's window had not rolled. Worth
    surfacing rather than retrying blind.
    """


# --------------------------------------------------------------------------
# The account
# --------------------------------------------------------------------------


class MarginClass(StrEnum):
    """What the account's ``multiplier`` says about its buying power.

    Named rather than left as a number because the number is the thing that
    was wrong in prose: PRD §8.6 asserted *"Paper is a margin account at 2×
    cash"* and the paper account reports ``multiplier: '4'`` — a pattern
    day-trader margin account, and wrong in the direction that **overstates**
    capacity. The figure on screen comes from here; nothing asserts a
    constant.

    :attr:`UNKNOWN` exists so an unexpected multiplier is not silently read as
    one of the three. A wrong margin class is a wrong sizing ceiling.
    """

    CASH = "cash"
    REG_T = "reg_t"
    PATTERN_DAY_TRADER = "pdt"
    UNKNOWN = "unknown"

    @classmethod
    def for_multiplier(cls, multiplier: Decimal) -> "MarginClass":
        if multiplier == 1:
            return cls.CASH
        if multiplier == 2:
            return cls.REG_T
        if multiplier == 4:
            return cls.PATTERN_DAY_TRADER
        return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class Account:
    """One account's balances, margin class and options entitlement.

    **No identifier is carried.** Not the account number, not the account
    UUID. Rule 6 keeps keys out of logs, and the account number is the one
    genuinely sensitive field on any of these responses; the API serves
    exactly one account, so nothing downstream needs it. Not modelling it is
    what stops it reaching a log line.

    **``pending_transfer_in`` and ``pending_transfer_out`` are deliberately
    absent.** They are not in the response on this plan. Nothing reads them,
    so nothing breaks — but modelling them as required is how an absent field
    becomes a 500 on every account request, and modelling them as optional
    would invite a caller to read a value the broker never sends.

    There is also **no settled/unsettled split**, confirmed against the live
    object rather than merely absent from the docs. See PRD §8.6.
    """

    status: str
    currency: str
    created_at: datetime

    cash: Decimal
    equity: Decimal
    #: Equity at the previous close — a day change for free.
    last_equity: Decimal
    portfolio_value: Decimal

    buying_power: Decimal
    #: The figure that actually binds an options trader, and which is never
    #: the margin figure because options are not marginable. Supplied on both
    #: paper and cash accounts, which is what makes it the right thing to size
    #: against on both.
    options_buying_power: Decimal | None
    effective_buying_power: Decimal | None
    non_marginable_buying_power: Decimal | None
    regt_buying_power: Decimal | None

    long_market_value: Decimal
    #: Negative when shorts are held. A credit is a liability.
    short_market_value: Decimal
    #: The broker's own figure for "the market value of open positions", so
    #: §8.6's equity reconciliation can be checked against it rather than only
    #: against a sum over the position rows.
    position_market_value: Decimal | None

    initial_margin: Decimal
    maintenance_margin: Decimal
    last_maintenance_margin: Decimal | None
    sma: Decimal
    accrued_fees: Decimal
    pending_reg_taf_fees: Decimal | None
    intraday_adjustments: Decimal | None

    #: 1 cash, 2 Reg T, 4 PDT. Read, never assumed — see :class:`MarginClass`.
    multiplier: Decimal

    #: ``None`` means the account object did not report a level, which is
    #: **not** the same as level 0. Say so rather than substituting a number;
    #: the same reasoning as ``riskLimitFor`` returning ``number | null`` on
    #: the frontend. Note these two arrive as JSON *integers* while every
    #: other numeric field on the object is a string.
    options_approved_level: int | None
    options_trading_level: int | None

    balance_asof: date | None

    trading_blocked: bool
    account_blocked: bool
    transfers_blocked: bool
    trade_suspended_by_user: bool
    shorting_enabled: bool

    @property
    def margin_class(self) -> MarginClass:
        return MarginClass.for_multiplier(self.multiplier)

    @property
    def day_change(self) -> Decimal:
        """Equity move since the previous close."""
        return self.equity - self.last_equity


# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """One *contract's* position. Not a logical position.

    Named ``BrokerPosition`` rather than ``Position`` on purpose: the broker
    keys on the OCC symbol and carries no leg grouping, no order linkage and
    no open date, so a four-leg iron condor is four of these. Assembling the
    logical position — with ``legs[]``, a payoff curve and a risk class — is
    step 6's job, from the ``mleg`` order history. Collapsing the two names
    would invite a caller to render a short leg alone, which reports as an
    *undefined-risk* naked short where the truth is a defined-risk credit
    spread: the exact failure CLAUDE.md rule 4 exists to prevent.

    :attr:`quantity` is **signed**, and agrees with :attr:`side`. The broker
    sends both; the implementation refuses a response where they disagree,
    because either choice would mis-state the risk class and there is no way
    to tell which one is wrong.
    """

    symbol: str
    asset_class: str
    #: Signed: negative on a short.
    quantity: Decimal
    quantity_available: Decimal
    side: PositionSide
    average_entry_price: Decimal
    #: Signed. Negative on a short, because the credit received is a debt.
    cost_basis: Decimal
    #: Signed, same reason.
    market_value: Decimal
    current_price: Decimal | None
    lastday_price: Decimal | None
    change_today: Decimal | None
    unrealized_pl: Decimal | None
    unrealized_plpc: Decimal | None
    unrealized_intraday_pl: Decimal | None
    unrealized_intraday_plpc: Decimal | None
    asset_marginable: bool | None
    #: An empty string on options. Present, and empty — which is a different
    #: statement from absent.
    exchange: str

    @property
    def is_short(self) -> bool:
        return self.side is PositionSide.SHORT


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


class OrderClass(StrEnum):
    """Alpaca spells a simple order both ``"simple"`` and ``""``.

    Both map to :attr:`SIMPLE` at the boundary, so nothing downstream has to
    remember that two spellings mean one thing.
    """

    SIMPLE = "simple"
    BRACKET = "bracket"
    OCO = "oco"
    OTO = "oto"
    MLEG = "mleg"


class OrderSide(StrEnum):
    """The order's own two-valued side.

    Not the same enum as :class:`FillSide`, and the difference is real rather
    than cosmetic: an ``mleg`` leg opening a short reports ``side: "sell"``
    with ``position_intent: "sell_to_open"``, while the *activity* for that
    same fill reports ``side: "sell_short"``. Two endpoints, two vocabularies.
    """

    BUY = "buy"
    SELL = "sell"


class PositionIntent(StrEnum):
    """The four-way action, and the only field that states it unambiguously."""

    BUY_TO_OPEN = "buy_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_OPEN = "sell_to_open"
    SELL_TO_CLOSE = "sell_to_close"


class OrderQueryStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class Order:
    """One order, with its legs nested where it has any.

    On an ``mleg`` parent Alpaca sends ``symbol: ""``, ``side: ""`` and
    ``asset_class: ""``, and no ``position_intent`` at all — the parent is the
    *structure* and the legs are the instruments. The empty side becomes
    ``None`` here rather than an empty enum member, so reading it as an action
    is a type error rather than an empty string.
    """

    id: str
    #: Empty on an ``mleg`` parent.
    symbol: str
    asset_class: str
    order_class: OrderClass
    #: ``None`` on an ``mleg`` parent.
    side: OrderSide | None
    #: ``None`` on an ``mleg`` parent; present on every leg.
    position_intent: PositionIntent | None
    order_type: str
    time_in_force: str
    status: str
    quantity: Decimal | None
    filled_quantity: Decimal
    #: **Signed on an ``mleg`` parent**: a negative average fill price is a
    #: net *credit*. The grouping rule reads direction off this — debit long,
    #: credit short — so the sign is the fact, not a presentation choice.
    filled_avg_price: Decimal | None
    limit_price: Decimal | None
    stop_price: Decimal | None
    #: The leg's proportional quantity within the parent. Ratios arrive in
    #: simplest form: the GCD across a parent's legs is 1.
    ratio_qty: Decimal | None
    created_at: datetime
    submitted_at: datetime | None
    filled_at: datetime | None
    canceled_at: datetime | None
    expired_at: datetime | None
    updated_at: datetime | None
    extended_hours: bool
    legs: tuple["Order", ...] = ()

    @property
    def is_multi_leg(self) -> bool:
        return self.order_class is OrderClass.MLEG

    @property
    def leg_ids(self) -> tuple[str, ...]:
        """The ids a fill on this order's legs would carry.

        The second hop, at the level of one order. The map across a whole
        history is ``{leg: order.id for order in orders for leg in
        order.leg_ids}`` — four lines, and the only route from a fill to its
        parent ``mleg`` order.
        """
        return tuple(leg.id for leg in self.legs)

    @property
    def net_price(self) -> Decimal | None:
        """The order's average fill price, signed. Negative is a credit."""
        return self.filled_avg_price

    @property
    def is_credit(self) -> bool:
        """Whether this order was filled for a net credit.

        ``False`` when unfilled — an order with no fill price has no
        direction, and answering ``True`` on a ``None`` would make every
        working order a credit.
        """
        return self.filled_avg_price is not None and self.filled_avg_price < 0


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------


class FillSide(StrEnum):
    """``side`` on a trade activity, which takes **three** values, not two.

    Observed on the paper account: ``buy`` ×9, ``sell_short`` ×5, ``sell`` ×1.
    """

    BUY = "buy"
    SELL = "sell"
    SELL_SHORT = "sell_short"

    @property
    def implied_intent(self) -> PositionIntent | None:
        """The action this side determines, or ``None`` when it does not.

        ``sell_short`` is an opening sale and ``sell`` a closing one, so those
        two are decidable. ``buy`` is **both** buy-to-open and buy-to-close,
        so it returns ``None`` — the join to the order's ``position_intent``
        is the only thing that can answer it.

        Returning ``None`` rather than guessing ``BUY_TO_OPEN`` is the whole
        point: a ledger that guessed would book every buy-to-close as a new
        lot and double the position it already held.
        """
        if self is FillSide.SELL_SHORT:
            return PositionIntent.SELL_TO_OPEN
        if self is FillSide.SELL:
            return PositionIntent.SELL_TO_CLOSE
        return None


class ActivityCategory(StrEnum):
    """The coarse filter. **Mutually exclusive with a type list.**"""

    TRADE = "trade_activity"
    NON_TRADE = "non_trade_activity"


@dataclass(frozen=True)
class _ActivityBase:
    """What both activity shapes share: a composite id and a type."""

    #: ``<17-digit stamp>::<uuid>``. A ``str``, deliberately — see
    #: :data:`ACTIVITY_ID_SEPARATOR`.
    id: str
    activity_type: str

    @property
    def stamp(self) -> str:
        """The timestamp half of the composite id.

        **Not a unique key.** Two legs of one spread fill inside the same
        millisecond and share a stamp, so ``fill(activity_id UNIQUE)`` must
        key on the whole composite; keyed on the stamp alone it would drop one
        leg of every spread.

        **And the whole id does not order rows chronologically**, which
        corrects the design spec. The spec says the composite id *"sorts
        chronologically as a string"*; measured against the real recording,
        the *stamp* does and the full id does not — inside a repeated stamp
        the UUID half orders arbitrarily, and the first divergence is two legs
        of one vertical. Resuming ingestion is unaffected because the vendor's
        own cursor does the paging. What would be affected is FIFO lot order:
        sequence fills by :attr:`TradeActivity.transaction_time`, never by
        ``id``.
        """
        head, separator, _ = self.id.partition(ACTIVITY_ID_SEPARATOR)
        return head if separator else self.id


@dataclass(frozen=True)
class TradeActivity(_ActivityBase):
    """A ``FILL`` or ``PARTIAL_FILL``.

    :attr:`quantity` is **unsigned** and the direction lives in :attr:`side`.
    The non-trade branch uses the opposite convention — a signed quantity and
    no side at all — so the matcher normalises the two on ingest and sees only
    one.

    :attr:`order_id` is the **leg's** order id on a multi-leg order. See the
    module docstring.
    """

    order_id: str
    order_status: str
    symbol: str
    side: FillSide
    #: Unsigned. Direction is :attr:`side`.
    quantity: Decimal
    price: Decimal
    transaction_time: datetime
    cumulative_quantity: Decimal | None = None
    leaves_quantity: Decimal | None = None
    #: ``fill`` or ``partial_fill``.
    fill_type: str = "fill"

    @property
    def is_partial(self) -> bool:
        return self.fill_type == "partial_fill"


@dataclass(frozen=True)
class NonTradeActivity(_ActivityBase):
    """Everything that is not a fill: fees, journals, option events, dividends.

    Modelled permissively, and not out of caution — the published
    ``NonTradeActivities`` schema is **incomplete as a matter of observed
    fact**. Three fields appear in live responses or in Alpaca's own doc
    examples and none is in it: ``description`` (on every row of this
    account's history), ``price`` (on the ``OPTRD`` examples) and
    ``execution_id`` (on 15 of 19 ``FEE`` rows here). Anything this model does
    not name is kept in :attr:`extra` rather than discarded, because there is
    no reason to think the fourth will be different.

    Two absences worth stating rather than leaving to be discovered:

    * **There is no ``order_id`` at all.** The schema offers ``group_id``,
      *"ID used to link activities who share a sibling relationship"*, as the
      only linkage — and no row in the recorded history carries one. On this
      account a fee's only route back to what caused it is the undocumented
      ``execution_id``.
    * **There is no ``side``.** :attr:`quantity` is **signed** instead: ``-2``
      when two contracts leave a long, ``+2`` when a short is assigned away.
    """

    activity_sub_type: str | None = None
    #: From the wire's ``date``: when the activity occurred, or when the
    #: transaction associated with it settled. A **date**, not an instant.
    #: Renamed because a field called ``date`` would shadow the ``date``
    #: type inside this class body, which is a trap rather than a bug.
    activity_date: date | None = None
    created_at: datetime | None = None
    net_amount: Decimal | None = None
    per_share_amount: Decimal | None = None
    #: **Signed**, where present. Absent on most types.
    quantity: Decimal | None = None
    #: Not in the published schema; present on Alpaca's ``OPTRD`` examples,
    #: where it carries the strike an option event settled at.
    price: Decimal | None = None
    symbol: str | None = None
    cusip: str | None = None
    #: The only linkage the schema offers, and null on every recorded row.
    group_id: str | None = None
    #: Not in the published schema. The only per-fill linkage a fee gets.
    execution_id: str | None = None
    status: str | None = None
    currency: str | None = None
    #: Not in the published schema. Present on every recorded row.
    description: str | None = None
    #: Whatever this model does not name, carried rather than dropped.
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


#: Discriminated on ``activity_type``. Ingestion needs **two branches rather
#: than one**, and a single permissive model covering both would make
#: ``order_id`` optional on a fill — where it is the join key.
Activity = TradeActivity | NonTradeActivity


# --------------------------------------------------------------------------
# Portfolio history
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One point on the account equity curve.

    A record rather than four parallel arrays, which is how Alpaca sends it:
    ``timestamp``, ``equity``, ``profit_loss`` and ``profit_loss_pct`` are
    only a time series because their indices line up, so a length mismatch is
    a silent off-by-one across the whole chart. Zipping them once, at the
    boundary, with a length check, is the only place that can be caught.

    Every value is ``Decimal | None`` and the ``None`` is load-bearing: a gap
    in the curve is a gap. Coerced to zero it would draw a line to the x-axis
    and assert the account was worth nothing that day.
    """

    at: datetime
    equity: Decimal | None
    profit_loss: Decimal | None
    profit_loss_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class PortfolioHistory:
    """The broker's own equity curve.

    Decision 6: the Dashboard draws this rather than reconstructing anything,
    with ``engine_state.t0`` marked so the chart does not claim credit for
    pre-Corollary trading.

    **Money arrives here as bare JSON numbers, not strings** — the one
    endpoint on the trading host where that is true. A parser written on the
    reasonable belief that "the trading API sends strings" would put the whole
    curve through doubles and nothing would say so.
    """

    timeframe: str
    base_value: Decimal | None
    #: The trading date ``base_value`` is the closing equity of. Absent when
    #: the baseline is the first returned point instead — a new account, or a
    #: ``1D`` query.
    base_value_asof: date | None
    points: tuple[EquityPoint, ...]
    #: Accumulated cash movement per activity type, aligned index-for-index
    #: with :attr:`points`.
    cashflow: Mapping[str, tuple[Decimal | None, ...]]


# --------------------------------------------------------------------------
# The interface
# --------------------------------------------------------------------------


class BrokerAccount(ABC):
    """Read the account, its positions, its orders and its activity history.

    Async because the design spec runs one process with an asyncio event loop
    and a 15-second account poll; a synchronous broker would block the same
    loop that answers the API.

    Not in this interface, deliberately:

    * **Anything that places, cancels or replaces an order.** That is
      ``BrokerExecution``, and it does not exist until Phase 6. See the module
      docstring for why absence rather than a stub.
    * **Streaming.** ``trade_updates`` belongs to ``engine/stream.py`` along
      with the two websocket budgets -- equity symbols and option quotes are
      metered separately -- and their priority ordering; folding a
      subscription in here would put budget policy behind a vendor interface.
    """

    @abstractmethod
    async def account(self) -> Account:
        """Balances, margin class and options entitlement.

        The options level and the margin multiplier come from here and
        nowhere else — never from a constant, and never from prose.
        """

    @abstractmethod
    async def positions(self) -> list[BrokerPosition]:
        """One row per contract, in the broker's own order.

        No grouping, no order linkage, no open date. Signs are preserved.
        """

    @abstractmethod
    async def orders(
        self,
        *,
        status: OrderQueryStatus = OrderQueryStatus.ALL,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[Order]:
        """Order history, **with legs nested**.

        An implementation must request the nested form. The two-hop join
        described in the module docstring has no other source for the leg-id →
        parent-id map, and a one-hop join fails silently rather than loudly.

        ``after`` and ``until`` must be timezone-aware; a naive datetime is a
        ``ValueError`` rather than a guess between Eastern and local.
        """

    @abstractmethod
    async def activities(
        self,
        *,
        types: Sequence[str] | None = None,
        category: ActivityCategory | None = None,
        after: datetime | None = None,
        until: datetime | None = None,
        since_id: str | None = None,
        page_size: int | None = None,
    ) -> list[Activity]:
        """Activity history, paginated to completion, oldest first.

        Returns both shapes; discriminate on ``activity_type``, or on the
        concrete type.

        ``types`` and ``category`` are **mutually exclusive** — the vendor
        refuses them together, so an implementation must refuse them at the
        call site rather than sending both and surfacing a 400 three layers
        away.

        ``since_id`` is the composite id of the newest activity already held,
        which is exactly what the vendor's page token is. It is the resume
        cursor for incremental ingestion.
        """

    @abstractmethod
    async def portfolio_history(
        self, *, period: str = "1M", timeframe: str = "1D"
    ) -> PortfolioHistory:
        """The account equity curve, as the broker computed it."""
