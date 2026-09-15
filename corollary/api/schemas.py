"""The API contract: every response shape the step-7 routes serve.

This module is the Python half of a contract whose other half is
``web/src/lib/types.ts``. The two are checked against each other by
``tests/api/test_schema_contract.py``, which parses the TypeScript and
compares field names -- because the failure mode otherwise is a renamed field
that typechecks on both sides and renders an empty column.

Models for routes this dispatch does not implement are here anyway. The
account, positions, activity, markets and settings routes are separate
dispatches; the *contract* is one document, and splitting it across five
files is how two routes end up disagreeing about what a ``Position`` is.

Three conventions, all of which are load-bearing
------------------------------------------------

**The wire is camelCase, generated rather than hand-written.**
:class:`ApiModel` sets ``alias_generator=to_camel``, so ``pnl_pct`` goes out
as ``pnlPct`` with nothing to keep in sync. There is exactly **one**
hand-written alias in this file -- :attr:`AccountSnapshot.volume24h` -- and it
is there because ``to_camel`` provably cannot produce the name the frontend
uses. It carries a comment saying so.

**Money is :data:`JsonMoney`, and that is the only float in the codebase.**
See its docstring.

**A date-only value is :data:`CalendarDate`, never a datetime.** An option
expiry is a property of a day, and the frontend parses these as UTC midnight
on purpose: ``formatExpiry`` documents that rendering a bare ``YYYY-MM-DD``
in Eastern shows the *previous* day, because ET is behind UTC. Sending an
instant where a date belongs re-opens that off-by-one on the server side,
where nothing on the page can see it.

What is deliberately absent
---------------------------

Corollary's own five ``Position`` fields have no Alpaca source in Phase 2 and
are typed to say so rather than filled in: ``strategyId``, ``managedExit`` and
``attachedExit`` are ``None`` because nothing Corollary opened exists yet, and
every position is legitimately detached. ``openedByStrategyId`` is typed
non-nullable in ``types.ts`` and has no source either; it is ``str | None``
here, and that mismatch is real rather than an oversight -- see the field.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer
from pydantic.alias_generators import to_camel

__all__ = [
    "AccountMode",
    "AccountResponse",
    "AccountSnapshot",
    "ActivityAction",
    "ActivityItem",
    "ActivityStatus",
    "ActivityStats",
    "AnalyticsSource",
    "ApiErrorBody",
    "ApiErrorResponse",
    "ApiKeyPresence",
    "ApiModel",
    "AttachedExit",
    "AuditCategory",
    "AuditLogEntry",
    "CalendarDate",
    "ChainSpec",
    "DataFeed",
    "DataSourceStatus",
    "Direction",
    "EngineStateResponse",
    "EquityCurvePoint",
    "ExitHolder",
    "FeedKey",
    "HaltRequest",
    "IntradayPoint",
    "JsonMoney",
    "ManagedExit",
    "MarginClassName",
    "MarginSummary",
    "NotificationEvent",
    "NotificationRoute",
    "OptionContract",
    "OptionRight",
    "OrderSide",
    "Page",
    "PortfolioHistoryResponse",
    "Position",
    "PositionLeg",
    "PricePoint",
    "RiskLimit",
    "RiskLimitKey",
    "SessionState",
    "StockQuote",
    "TimeInForce",
    "Trend",
    "UnderlyingQuote",
    "WorkingOrder",
    "WorkingOrderType",
    "WsClientFrame",
    "WsErrorFrame",
    "WsQuote",
    "WsQuoteFrame",
    "WsServerFrame",
    "WsSubscribeRequest",
    "WsTradeUpdate",
    "WsTradeUpdateFrame",
]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Money at the display boundary
# --------------------------------------------------------------------------


def _money_to_json(value: Decimal) -> float:
    """Render an exact decimal as a JSON number, and say so if it cannot.

    The check is not ceremony. ``float`` carries 15--17 significant digits and
    every figure this app serves is far inside that -- the largest on this
    account is ``99728.08``, eight digits with the point -- so a lossy
    conversion here means something upstream is not a money figure at all. It
    logs rather than raising: rule 8's standard is that a refusal leaves a
    record, and a response that dies in its serializer leaves the page blank
    with no more information than a wrong last digit would.
    """
    as_float = float(value)
    if Decimal(repr(as_float)) != value:
        logger.warning(
            "money lost precision at the API boundary: %s serialized as %r",
            value,
            as_float,
            extra={
                "event": "money_serialization_lossy",
                "rule": "a JSON number must round-trip the Decimal it came from",
                "value": str(value),
                "serialized": as_float,
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
    return as_float


JsonMoney: TypeAlias = Annotated[
    Decimal,
    PlainSerializer(_money_to_json, return_type=float, when_used="json"),
]
"""An exact ``Decimal`` that serializes as a JSON **number**.

Sanctioned by the Phase 2 design spec, *Three rule reinterpretations,
approved* -> *``Decimal`` serializes as a JSON number*:

    Money is ``Decimal`` everywhere it is *computed* -- Alpaca's strings parse
    straight to ``Decimal`` on ingest, money DB columns are ``Money``, the
    matcher and engine are ``Decimal`` throughout. **The API boundary is a
    display boundary and serializes to a JSON number.**

**This is the single sanctioned float in the codebase, and it exists only
inside serialization.** CLAUDE.md's rule is otherwise absolute: money is
``Decimal``, never ``float``, and SQLite money columns are
``corollary.db.types.Money`` precisely so that no IEEE double touches the
path. Nothing reads this float back. There is no deserializer here, no
request model takes one, and no arithmetic anywhere in ``corollary/`` may
consume one -- the value is produced at the last possible moment, handed to
``json``, and forgotten.

The constraint the spec attaches, restated because it is the half people
drop: **client-side money arithmetic is display-only.** ``account.ts`` sums
position value and equity in the browser; the server computes both
authoritatively and the client's version is the live estimate between
refreshes.

``when_used="json"`` is deliberate. ``model_dump()`` in Python mode still
yields the ``Decimal``, so a test, a log line or any server-side consumer
reading a response model gets the exact value; only the bytes on the wire are
a number.
"""

CalendarDate: TypeAlias = date
"""A day, not an instant -- an option expiry, a bar's session, a settle date.

Aliased rather than written as ``date`` so that a field can be *named* ``date``
without shadowing the type in the class body, and so the intent survives a
skim. The frontend parses every one of these as UTC midnight; see the module
docstring.
"""


# --------------------------------------------------------------------------
# The base model
# --------------------------------------------------------------------------


class ApiModel(BaseModel):
    """camelCase on the wire, snake_case in Python, nothing hand-maintained.

    ``populate_by_name`` keeps construction in Python spelling, which is what
    every route does. ``serialize_by_alias`` means a bare
    ``model_dump_json()`` produces the wire form too, so a test does not have
    to remember ``by_alias=True`` -- forgetting it is how a test passes while
    the browser receives snake_case.

    ``extra="forbid"`` matters on the request models: a halt whose body
    misspells ``reason`` should be a 422, not a halt with no reason.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
    )


# --------------------------------------------------------------------------
# The small unions, spelled exactly as ``types.ts`` spells them
# --------------------------------------------------------------------------


class AccountMode(StrEnum):
    """Which book. Matches ``db.models.ACCOUNT_MODES`` and the TS union.

    An enum rather than a ``Literal`` because it is also a query parameter
    and a dictionary key on the server, and ``AccountMode.PAPER`` reads
    better at a call site than a bare string that a typo would turn into a
    silent miss.
    """

    PAPER = "paper"
    CASH = "cash"


Direction: TypeAlias = Literal["long", "short"]
OptionRight: TypeAlias = Literal["call", "put"]
ExitHolder: TypeAlias = Literal["broker", "corollary"]
TimeInForce: TypeAlias = Literal["day", "gtc"]
OrderSide: TypeAlias = Literal["BTO", "STC", "STO", "BTC"]

#: A market order fills; it does not sit and work. ``types.ts`` says
#: ``Exclude<OrderType, 'market'>`` and this is that exclusion written out.
WorkingOrderType: TypeAlias = Literal["limit", "stop", "stop_limit"]

ActivityAction: TypeAlias = Literal[
    "BTO", "STC", "STO", "BTC", "DEPOSIT", "WITHDRAWAL"
]
ActivityStatus: TypeAlias = Literal["filled", "rejected", "pending", "canceled"]

RiskLimitKey: TypeAlias = Literal[
    "max_risk_per_trade_pct",
    "max_daily_loss_pct",
    "max_concurrent_positions",
    "max_exposure_per_underlying",
    "max_net_directional_pct",
]
RiskLimitUnit: TypeAlias = Literal["%", "count"]
AuditCategory: TypeAlias = Literal["risk", "feed", "notification"]

NotificationEvent: TypeAlias = Literal[
    "order_filled",
    "order_rejected",
    "stop_loss_hit",
    "daily_loss_halt",
    "engine_error",
    "price_alert",
    "recommendations_ready",
    "strategy_promotion",
]

FeedKey: TypeAlias = Literal["options", "stockHistorical", "stockRealtime"]
DataSourceState: TypeAlias = Literal["connected", "degraded", "disconnected"]

MarginClassName: TypeAlias = Literal["cash", "reg_t", "pdt", "unknown"]
"""What the account's ``multiplier`` says about its buying power.

Mirrors :class:`~corollary.engine.execution.interface.MarginClass` value for
value. Spelled as a ``Literal`` here rather than re-exporting the enum so the
wire contract stays readable in one file, and
``test_account_routes.py`` pins the two together.

``unknown`` is a member on purpose: an unexpected multiplier must not be
silently read as one of the other three, because a wrong margin class is a
wrong sizing ceiling.
"""

SessionState: TypeAlias = Literal["in_progress", "completed"]
"""Whether a trading session had finished when a figure was measured over it.

The Volume column means two things and has to say which. During a session it
is *traded so far today*; outside one it is *traded last session*, because a
blank column at the weekend answers nobody -- and a reader who cannot tell
them apart compares a partial day against a full one and concludes a stock is
quiet when it is mid-morning.

``completed`` is decided by the **market calendar**, not by a clock reading of
16:00: NYSE closes at 13:00 ET on the Friday after Thanksgiving and on a
handful of other half-days, and it does not open at all on ~115 days a year.
"""

AnalyticsSource: TypeAlias = Literal["vendor", "derived"]
"""Where an implied volatility or a greek came from.

Design spec, decision 10: Alpaca serves ``impliedVolatility`` and ``greeks``
on the indicative feed for the contracts where its own solve succeeds -- 19 of
100 on NVDA -- and not at all for the rest, so the provider *"passes vendor
analytics through where they exist and derives only where they do not, and
must record which of the two a number came from, because a chain silently
mixing measured and derived values is worse than either alone."*
"""


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ApiErrorBody(ApiModel):
    """The stated condition. One shape, so ``api.ts`` has one thing to parse."""

    #: Stable and machine-readable. The client branches on this, never on the
    #: prose.
    code: str
    #: One sentence a human can act on. **Never carries key material or the
    #: account number** -- see ``api/app.py``, which scrubs before it renders.
    message: str


class ApiErrorResponse(ApiModel):
    """Every non-2xx this API produces, including FastAPI's own 404 and 422."""

    error: ApiErrorBody


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------

ItemT = TypeVar("ItemT")


class Page(ApiModel, Generic[ItemT]):
    """One page of a long list. Used by the activity table.

    Design spec, decision 11: the Activity header cards fold **every** trade,
    because ``Money`` refuses ``SUM`` and ``AVG`` and a capped window makes
    four figures labelled "lifetime" mean something narrower than the word.
    The *table* is what pages. So ``total`` here is the real total and not a
    window size, and a caller must not compute a lifetime figure from
    :attr:`items`.
    """

    items: list[ItemT]
    #: Rows matching the query, not rows on this page.
    total: int
    #: Zero-based.
    page: int
    page_size: int
    has_more: bool


# --------------------------------------------------------------------------
# Prices and positions
# --------------------------------------------------------------------------


class PricePoint(ApiModel):
    """One point on an equity or position-value series."""

    date: CalendarDate
    value: JsonMoney


class IntradayPoint(ApiModel):
    """One point on a price series finer than a day.

    A second point type rather than a nullable field on :class:`PricePoint`,
    for the same reason :class:`PortfolioPoint` is one: ``PricePoint.date`` is
    a **day**, and a day cannot carry a ``5Min`` or ``1H`` stamp at all.
    Seventy-eight five-minute points would share one ``date``, so a chart
    keyed on it would draw them all at the same x -- which looks like working
    code and is not.

    ``at`` is an instant in UTC, stamped at the interval's **open**, which is
    the vendor's convention and the one the no-look-ahead rule depends on.
    Render it in ``America/New_York``; unlike a bare ``YYYY-MM-DD`` it carries
    its own offset, so the date-only trap in ``format.ts#formatExpiry`` does
    not apply.
    """

    at: datetime
    value: JsonMoney


class PositionLeg(ApiModel):
    """One contract inside a logical position."""

    #: OCC: underlying + YYMMDD + C/P + 8-digit strike x1000.
    symbol: str
    strike: JsonMoney
    right: OptionRight
    side: Direction
    #: Alpaca requires leg ratios in simplest form -- the GCD across a
    #: multi-leg order's ratios is 1, or the order is rejected.
    ratio: int


class AttachedExit(ApiModel):
    """A broker-side or Corollary-managed OCO on a position.

    No source in Phase 2: nothing attaches exits because there is no execution
    path. Modelled so the shape is fixed before Phase 6 writes one.
    """

    take_profit: JsonMoney
    stop_price: JsonMoney
    stop_limit_price: JsonMoney | None
    time_in_force: TimeInForce
    held_by: ExitHolder


class ManagedExit(ApiModel):
    """The exits a strategy manages on its own positions (PRD §5.1)."""

    profit_target_pct: JsonMoney
    stop_loss_pct: JsonMoney
    time_stop_dte: int


class Position(ApiModel):
    """One *logical* position -- a single contract, or a grouped structure.

    Not the broker's row. ``/v2/positions`` keys on the OCC symbol and carries
    no leg grouping, so a four-leg iron condor is four broker rows and one of
    these. The assembly is ``engine/grouping.py``, and it matters beyond
    tidiness: a short leg rendered alone reports as an *undefined-risk* naked
    short where the truth is a defined-risk credit spread, which is the exact
    failure CLAUDE.md rule 4 exists to prevent.
    """

    id: str
    symbol: str
    contract: str
    #: The **contract's** last price, between :attr:`bid` and :attr:`ask`.
    last: JsonMoney
    #: The **underlying's** price. Read by the payoff curve and nothing else.
    #: Two instruments, two fields -- one row carrying both under one name was
    #: a bug waiting for a chart.
    underlying: JsonMoney
    #: Signed. Negative on a short, because a credit is a liability.
    cost_basis: JsonMoney
    value: JsonMoney
    #: Contracts held, as a count. ``engine/grouping.py`` calls this ``units``:
    #: a 1:1 vertical held two deep is 2, with both leg ratios still 1.
    quantity: int
    pnl: JsonMoney
    pnl_pct: JsonMoney
    bid: JsonMoney
    ask: JsonMoney
    #: How the position closes -- a long is sold at the bid, a short bought at
    #: the ask -- so nothing can generate a correct exit without it.
    direction: Direction
    legs: list[PositionLeg]
    expiry: CalendarDate
    #: **No Alpaca source this phase.** Nothing Corollary opened exists, so
    #: every position is legitimately detached and this is ``None`` rather
    #: than invented.
    strategy_id: str | None
    #: **Typed ``string`` -- non-nullable -- in ``types.ts``, and it has no
    #: source either.** Modelled nullable here because the alternative is to
    #: put a made-up strategy id on every broker position, and an id that
    #: resolves to nothing is worse than an admitted absence. The frontend
    #: type needs widening to ``string | null``; flagged rather than fixed,
    #: because ``types.ts`` belongs to the frontend dispatch.
    opened_by_strategy_id: str | None
    #: No source this phase. See :attr:`strategy_id`.
    managed_exit: ManagedExit | None
    #: No source this phase. See :attr:`strategy_id`.
    attached_exit: AttachedExit | None
    #: Derived from the contract's daily bars between the opening fill's date
    #: and today. The entry date comes from ``fill``, not from the position --
    #: a broker position carries no open date at all.
    value_history: list[PricePoint]


class WorkingOrder(ApiModel):
    """A placed order that has not filled.

    Attached exits are deliberately not modelled here: they live on the
    position, which is where they are edited and cancelled, and a second home
    is a second thing to disagree with.
    """

    id: str
    #: The position this acts on, or ``None`` for an order that opens a new
    #: one. Exactly one of this and :attr:`contract_key` is set.
    position_id: str | None
    #: The contract an *opening* order rests against, ``None`` once it belongs
    #: to a position.
    contract_key: str | None
    #: Denormalised for display, so the list renders without resolving a
    #: position that may have been closed out from under it.
    contract: str
    side: OrderSide
    order_type: WorkingOrderType
    quantity: int
    limit_price: JsonMoney | None
    stop_price: JsonMoney | None
    time_in_force: TimeInForce
    placed_at: datetime
    #: The pending row this order wrote to the activity feed. Cancelling flips
    #: that row rather than appending a second one.
    activity_id: str


# --------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------


class ActivityItem(ApiModel):
    """One row of the ledger.

    ``pnl`` and ``pnlPct`` are **Corollary's arithmetic, not Alpaca's.** There
    is no realized-P&L field on any activity the broker sends; the FIFO
    matcher in ``engine/ledger.py`` produces these.
    """

    id: str
    time: datetime
    contract: str
    action: ActivityAction
    price: JsonMoney | None
    quantity: int | None
    #: Realized P&L on a closing row. ``None`` on an opening fill, which has
    #: realized nothing, and on a rejection, which never will.
    pnl: JsonMoney | None
    #: Return on cost basis. ``None`` wherever :attr:`pnl` is. Carried beside
    #: it rather than derived: how much and how well are different questions,
    #: and the header averages both separately.
    pnl_pct: JsonMoney | None
    #: Signed cash movement for a deposit or withdrawal. ``None`` on trades.
    #: Money moved into the account is not money the account made, and summing
    #: the two would overstate performance.
    amount: JsonMoney | None
    status: ActivityStatus
    #: **``types.ts`` declares this optional (``rejectionReason?: string``) and
    #: this model always emits the key, as ``null``.** Flagged rather than
    #: worked around: ``string | undefined`` and ``string | null`` are
    #: different types under ``strict``, and the frontend dispatch owns which
    #: one it wants.
    rejection_reason: str | None = None


class ActivityStats(ApiModel):
    """The Activity header cards. A Python fold over every realized trade.

    ``None`` where the account has no trade of that kind yet: an average over
    zero trades is unknown, not zero, and ``$0.00`` would claim a result that
    does not exist.

    Design spec, decision 11: every figure here folds **all** the account's
    ``realized_trade`` rows, never a page of them and never a SQL aggregate.
    ``pnl`` is a :class:`~corollary.db.types.Money` column, which raises
    ``MoneyComparisonError`` on ``SUM``, ``AVG``, ``MIN``, ``MAX`` and
    ``ORDER BY`` rather than answering lexicographically.
    """

    avg_win: JsonMoney | None
    avg_win_pct: JsonMoney | None
    avg_loss: JsonMoney | None
    avg_loss_pct: JsonMoney | None
    lifetime_pnl: JsonMoney
    wins: int
    losses: int
    #: **How many closings above are missing from the four figures above.**
    #: ``0`` on a complete ledger.
    #:
    #: Design spec, open question 4, resolved 2026-09-12: an adjusted root
    #: reaching an ``OPEXC``/``OPASN`` branch is **refused** -- a real
    #: ``GME1`` delivers 100 GME *plus* 10 GME.WS while ``multiplier`` and
    #: ``size`` both report ``100``, so there is no honest dollar figure to
    #: book and inventing one suppresses a Phase 6 ``max_daily_loss_pct``
    #: halt that should have fired. The contracts still left the book, so the
    #: lifetime figure is genuinely incomplete and the page says so beside
    #: the cards: *"N trades not booked"*.
    #:
    #: **Not in ``types.ts`` yet.** The field is new with the activity route;
    #: the frontend half is a separate dispatch's to land.
    #:
    #: What this counts is exactly *"closings that produced no realized
    #: trade"* -- an arithmetic fact about the two tables. It deliberately
    #: does **not** claim each one's *cause*: ``RejectionRule`` is logged by
    #: ``engine/ledger.py`` and no Phase 2 table stores it, and an
    #: unverified-deliverable refusal is the case this exists for rather
    #: than provably the only one that can produce it.
    not_booked: int = 0
    #: The contracts behind :attr:`not_booked`, OCC-spelled and sorted. A
    #: bare count cannot be reconciled by hand; a symbol can be looked up in
    #: the log and in the broker's own history.
    not_booked_symbols: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------


class Trend(ApiModel):
    change_pct: JsonMoney
    compared_to: str


class AccountSnapshot(ApiModel):
    """Everything that belongs to the account rather than to a strategy.

    Account-scoped in full, because Paper and Cash hold different money:
    rendering paper's positions while Cash is live misreports real money
    exactly the way rendering paper's balance would.
    """

    portfolio_history: list[PricePoint]
    #: The one field whose wire name ``to_camel`` cannot generate. It maps
    #: ``volume_24h`` -- and ``volume24h``, and every other snake spelling --
    #: to ``volume24H``, because it uppercases the letter after a digit.
    #: ``types.ts`` says ``volume24h``. The explicit alias wins over the
    #: generator (pydantic gives an explicitly-set alias the higher priority),
    #: and ``test_schema_contract.py`` is what catches it if this is ever
    #: removed.
    volume24h: JsonMoney = Field(alias="volume24h")
    balance_trend: Trend
    volume_trend: Trend
    positions: list[Position]
    activity: list[ActivityItem]
    working_orders: list[WorkingOrder]
    #: Total cash, whatever its settlement state. There is **no settled /
    #: unsettled split**: the account endpoint publishes no settlement
    #: breakdown, confirmed on the live object. See PRD §8.6.
    cash: JsonMoney
    buying_power: JsonMoney
    #: Always <= :attr:`buying_power`, because options are not marginable.
    #: This is the figure that actually binds an options trader, and it is
    #: supplied on both paper and cash accounts -- which is what makes it the
    #: right thing to size against on both.
    options_buying_power: JsonMoney


class MarginSummary(ApiModel):
    """What ``multiplier`` says, in the words the Account card renders.

    This exists because the sentence on screen was **wrong**. PRD §8.6
    asserted *"Paper is a margin account at 2× cash"* and the Account page
    hard-coded ``'Margin account — 2× cash'``; the live paper account reports
    ``multiplier: '4'``, a pattern day-trader margin account. At 4× that
    sentence is wrong in the direction that **overstates capacity**, which is
    the direction that costs money.

    So :attr:`label` and :attr:`note` are **built from** :attr:`multiplier` on
    the server and rendered verbatim. The page must not compose this sentence
    itself, because composing it is how a constant gets into it again.
    """

    #: Read from the account object, never assumed. 1 cash, 2 Reg T, 4 PDT.
    multiplier: JsonMoney
    margin_class: MarginClassName
    #: One noun phrase: "Pattern day-trader margin account".
    label: str
    #: One or two sentences quoting the real multiplier, and naming
    #: ``optionsBuyingPower`` as the figure that binds an options trader --
    #: because options are not marginable and the margin figure never does.
    note: str


class AccountResponse(ApiModel):
    """``GET /api/account``: the account-level half of the Account page.

    **Not** :class:`AccountSnapshot`, deliberately. That model is the
    *frontend store's* composite shape -- it carries ``positions``,
    ``activity`` and ``workingOrders``, which are the positions and activity
    dispatches' surfaces, served at their own routes. Filling those three with
    empty lists from here would state an absence that is not true, which is
    the one thing this codebase will not do with a number. The client
    assembles the snapshot from several queries; this route answers for
    ``GET /v2/account`` and nothing else.

    Where this model and :class:`AccountSnapshot` name the same figure they
    type it the same way, and ``test_account_routes.py`` asserts that field by
    field so the two cannot drift into meaning different things.
    """

    #: Which book this response is about. Echoed rather than assumed, so a
    #: cached response cannot be rendered under the other account's heading.
    account: AccountMode
    status: str
    currency: str

    #: Total cash, whatever its settlement state. **No settled/unsettled
    #: split** -- decision 9, confirmed on the live object. See PRD §8.6.
    cash: JsonMoney
    #: The broker's own equity figure, and the authoritative one.
    equity: JsonMoney
    #: Equity at the previous close -- a day change for free.
    last_equity: JsonMoney
    #: :attr:`equity` less :attr:`last_equity`.
    day_change: JsonMoney
    #: The same move as a percentage, in the shape the page's trend pill
    #: already takes. ``None`` when :attr:`last_equity` is zero: a percentage
    #: change from nothing is undefined, and 0% would claim a flat day.
    balance_trend: Trend | None

    buying_power: JsonMoney
    #: **Nullable here, non-nullable on :class:`AccountSnapshot`.** The
    #: broker's account object makes it optional, and it is the figure an
    #: options trader is actually bound by, so a substituted number would
    #: overstate capacity exactly the way the 2× sentence did. Absent stays
    #: absent, and :attr:`MarginSummary.note` says so in words.
    options_buying_power: JsonMoney | None

    long_market_value: JsonMoney
    #: Negative when shorts are held. A credit is a liability.
    short_market_value: JsonMoney
    #: ``long + short``. **This is §8.6's "market value of open positions"**,
    #: and the term that reconciles against :attr:`equity`.
    net_position_value: JsonMoney
    #: The broker's own ``position_market_value``, which is the **gross**
    #: exposure -- ``|long| + |short|`` -- and **not** the equity term.
    #: Measured on this account: ``11833 + 8991 = 20824``, while the term that
    #: reconciles is ``11833 + (-8991) = 2842``. Substituting it overstates
    #: equity by twice the short market value, and on a book holding no shorts
    #: the error is exactly zero, so nothing surfaces it until the first
    #: credit spread. Carried because gross exposure is worth showing; named
    #: so it cannot be mistaken for the other thing.
    gross_position_value: JsonMoney | None
    #: ``cash + net_position_value`` -- §8.6's definition, computed here.
    derived_equity: JsonMoney
    #: Whether :attr:`derived_equity` equals :attr:`equity`. Two sources for
    #: one figure, and the page says whether they agree rather than picking
    #: one silently.
    equity_reconciles: bool
    #: ``derived_equity - equity``. Zero when they agree.
    equity_difference: JsonMoney

    margin: MarginSummary
    #: ``None`` means the account object reported no level, which is **not**
    #: level 0. These two arrive as JSON *integers* while every money field on
    #: the same object is a string.
    options_approved_level: int | None
    options_trading_level: int | None

    trading_blocked: bool
    account_blocked: bool
    transfers_blocked: bool

    #: Rule 5: Cash requires an explicit confirmation naming the account and
    #: its balance, and there is no balance to name while the live keys are
    #: absent. The server computes this because only the server can see the
    #: environment.
    cash_account_available: bool
    #: One sentence the toggle's tooltip renders. ``None`` when Cash is
    #: available.
    cash_account_unavailable_reason: str | None
    #: Variable **names**, never values. Rule 6: the UI shows masked presence
    #: only, and a name is not a mask of anything.
    missing_live_credential_env_vars: list[str]


class EquityCurvePoint(ApiModel):
    """One point of the broker's equity curve.

    Richer than :class:`PricePoint` for two reasons, both load-bearing.
    ``PricePoint.value`` is non-nullable, and **a gap in this curve is a
    gap**: coerced to zero it draws a line to the x-axis and asserts the
    account was worth nothing that day. And ``PricePoint.date`` is a day,
    which cannot carry a ``5Min`` or ``1H`` timeframe at all.
    """

    at: datetime
    equity: JsonMoney | None
    profit_loss: JsonMoney | None
    profit_loss_pct: JsonMoney | None


class PortfolioHistoryResponse(ApiModel):
    """``GET /api/account/history``: Alpaca's equity curve, with t₀ marked.

    Design spec decision 6. Reading the broker's own record is not
    reconstruction -- nothing is simulated, and §8.6's principle is that
    Alpaca is the source of truth. The marker is what preserves §8.1's *"no
    pre-Corollary reconstruction"*: this chart sits beside a strategy win rate
    and must not claim credit for manual trading that predates the engine.

    There is no ``equity_snapshot`` table and there must not be one.
    """

    account: AccountMode
    period: str
    timeframe: str
    #: The baseline the broker measured ``profitLoss`` from.
    base_value: JsonMoney | None
    #: The trading date :attr:`base_value` closes. Absent when the baseline is
    #: the first returned point instead -- a new account, or a ``1D`` query.
    base_value_asof: CalendarDate | None
    points: list[EquityCurvePoint]
    #: When Corollary first ran. Written once, never rewritten, and ``None``
    #: if the engine has never started.
    t0: datetime | None
    #: How many points predate :attr:`t0` -- the part of this chart Corollary
    #: had no hand in. ``None`` when :attr:`t0` is ``None``, because *unknown*
    #: and *zero* are different claims and zero would say the whole curve is
    #: Corollary's.
    points_before_t0: int | None


# --------------------------------------------------------------------------
# Markets
# --------------------------------------------------------------------------


class UnderlyingQuote(ApiModel):
    """One underlying's price, its daily change, and the chart series.

    :attr:`price` is non-nullable and the three fields after it are not, which
    is the rule the whole Markets surface follows: **a symbol with no price is
    omitted from the response entirely** -- there is no row to draw and the
    empty state is already designed -- while every other column says ``null``
    when the feed had nothing. Nulls, never zeros. See
    ``api/routes/markets.py``.
    """

    symbol: str
    price: JsonMoney
    #: Yesterday's close. The daily change is measured from here, never from
    #: the first point of the series -- a 60-session chart's left edge is two
    #: months ago, and "today" measured against it is not today.
    #:
    #: **Nullable, against ``types.ts``'s ``number``.** A snapshot carries no
    #: ``prevDailyBar`` for a name that had no prior session; asserting a
    #: previous close there would anchor a change to a number nobody measured.
    previous_close: JsonMoney | None
    #: ``None`` wherever :attr:`previous_close` is -- there is nothing to
    #: measure the move from.
    change: JsonMoney | None
    change_pct: JsonMoney | None
    #: Daily closes, oldest first, ending at today's live price. Served when
    #: ``?timeframe=1D`` -- the default -- and **empty at every other
    #: timeframe**, where :attr:`intraday` carries the series instead.
    history: list[PricePoint]
    #: The same series at a resolution finer than a day: served when
    #: ``?timeframe=`` is one of ``1Min``, ``5Min``, ``15Min`` or ``1H``, and
    #: empty at ``1D``.
    #:
    #: **Two fields rather than one, and never both populated.** A day cannot
    #: carry a five-minute stamp, so a single field would have meant a
    #: nullable ``at`` on :class:`PricePoint` -- and a client that kept
    #: reading ``date`` would then draw an entire session's points on one x
    #: and look like it worked. Two fields make the resolution a thing the
    #: response *states*: whichever is non-empty is the answer. Both are empty
    #: when the window holds no session at all, which is honest rather than
    #: ambiguous -- there is nothing to draw.
    intraday: list[IntradayPoint]


class StockQuote(ApiModel):
    """One row of the stock table.

    Same rule as :class:`UnderlyingQuote`: no price, no row; every other
    column is nullable and absence is served as ``null``.
    """

    symbol: str
    name: str
    price: JsonMoney
    #: **Nullable, against ``types.ts``'s ``number``.** No previous daily bar
    #: means no change to state.
    change: JsonMoney | None
    change_pct: JsonMoney | None
    #: Session volume so far, from today's **partial daily bar on the
    #: historical feed** -- the same feed, and the same request family, as
    #: :attr:`avg_volume` below.
    #:
    #: That sameness is the field, not a detail of how it is fetched. These
    #: two are divided by each other, and the snapshot endpoint that serves
    #: every other figure on this row is *latest*, which is IEX-only on the
    #: Basic plan while historical bars are SIP. IEX is ~2.5% of US equity
    #: volume, so a numerator read from the snapshot against this denominator
    #: put NVDA at 0.018 and SPY at 0.023 on the recorded fixtures -- measured
    #: numbers, and every name on the screen read as near-dead.
    #:
    #: **Nullable, and much less often than it was**: a market that is closed
    #: -- a weekend, a holiday, the hours either side of a session -- serves
    #: the last completed session rather than nothing, so what remains null is
    #: a symbol with no daily bar anywhere in the window. A zero would claim
    #: the symbol did not trade.
    volume: int | None
    #: Which of the two things :attr:`volume` means, so the column can be
    #: labelled without the client re-deriving the session from its own clock
    #: -- which is a browser-local clock, where every session boundary in this
    #: app is a New York one.
    #:
    #: Null exactly when :attr:`volume` is null: a session state beside an
    #: absent measurement is a claim about something nobody measured.
    volume_session: SessionState | None
    #: The trading date :attr:`volume` covers, so the column can name the day
    #: rather than say "last session". Per row rather than per response: in
    #: the first minutes of a session one symbol can have today's bar while
    #: another has not printed yet, and they are then reporting different
    #: days.
    volume_date: CalendarDate | None
    #: The denominator of relative volume, which is what "trending" means --
    #: a different question from "most active", where raw volume finds the
    #: same mega caps every session. Completed sessions only, from the same
    #: historical feed as :attr:`volume`.
    #:
    #: **Nullable, against ``types.ts``'s ``number``, and this one is the
    #: load-bearing null of the four.** A zero here is a division by zero in
    #: the relative-volume ranking, so a symbol whose history could not be
    #: fetched would sort *first* on the screen built to find unusual
    #: activity. Absent is not zero.
    avg_volume: int | None
    #: **``None`` for a fund, never 0.** An ETF has no market capitalisation;
    #: coerced to zero it would sort SPY to the top of an ascending list and
    #: state, in a column of dollars, that a fund is worth nothing.
    market_cap: JsonMoney | None


class OptionContract(ApiModel):
    """One row of a chain.

    **Only the contract's terms are guaranteed.** Symbol, strike, expiration
    and type come from the OCC symbol and are always present; every market
    figure below is nullable, and on a real chain a great many of them are
    null. Measured against the recorded NVDA page in
    ``tests/fixtures/alpaca/``: **49 of 100 contracts have no bid at all**
    (``bp: 0``, which Alpaca documents as *"the security has no active bid"*),
    40 of 100 on the second page have no previous daily bar, and 26 have never
    traded. Serving those as ``0.00`` would invent a price on half the ladder,
    and a mid taken from an invented bid is half the ask -- which would then
    be the input to a derived implied volatility. An invented number built on
    an invented number, printed under the word "IV".

    ``types.ts`` declares most of these non-nullable. That mismatch is real
    and is flagged rather than fixed here, matching how
    :attr:`Position.opened_by_strategy_id` is handled: the frontend type
    belongs to the frontend dispatch.
    """

    symbol: str
    strike: JsonMoney
    expiration: CalendarDate
    type: OptionRight
    #: The last print, else the session's close, else the quote mid. **Not
    #: guaranteed to sit between** :attr:`bid` **and** :attr:`ask`: a print is
    #: a fact about the past and a spread is a fact about now, and on an
    #: illiquid contract they are hours apart. (A *position's* ``last`` is a
    #: different number with a different invariant.)
    last: JsonMoney | None
    #: Yesterday's settle. Carried rather than derived, so :attr:`change`
    #: stays anchored while :attr:`last` moves.
    previous_close: JsonMoney | None
    change: JsonMoney | None
    change_pct: JsonMoney | None
    #: **Nullable, and the null is the point.** No bid is not a zero bid.
    bid: JsonMoney | None
    ask: JsonMoney | None
    #: Contracts traded this session. ``None`` before the first print.
    volume: int | None
    #: **Nullable, against ``types.ts``'s ``number``.** Populated on most
    #: contracts (98 of 100 NVDA, 79 of 100 AAPL) and genuinely absent on
    #: newly-listed ones with no settled interest yet. Decision 10: *"A null
    #: still means absent and must survive as null"* -- and the "highest open
    #: interest" screen must not rank on one.
    open_interest: int | None
    #: **Nullable, against ``types.ts``'s ``number``.** Alpaca serves an
    #: implied volatility on the indicative feed only where its own
    #: Black-Scholes solve succeeds; Corollary derives the rest. A contract
    #: where neither is available has no IV, and inventing one is the failure
    #: PRD §8.5 names.
    iv: JsonMoney | None
    #: **Not in ``types.ts`` at all**, and required by decision 10: a chain
    #: silently mixing vendor and derived analytics is worse than either
    #: alone. ``None`` where :attr:`iv` is.
    iv_source: AnalyticsSource | None = None


class ChainSpec(ApiModel):
    """The display parameters of one underlying's chain.

    Mirrored from ``types.ts`` for contract completeness. :attr:`seed` is a
    Phase 1 fixture concept -- the seeded PRNG that generates a mock chain --
    and has **no server-side meaning**; a real chain comes from
    :class:`OptionContract` rows. Carried so the frontend type does not have
    to fork while the Markets page migrates.
    """

    symbol: str
    base_iv: JsonMoney
    #: Scales volume and open interest in the fixture generator.
    liquidity: JsonMoney
    seed: int


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


class RiskLimit(ApiModel):
    """One of the five ceilings in CLAUDE.md rule 4.

    :attr:`min` and :attr:`max` are the **editable** range, not the enforced
    one. The engine enforces (rule 4); this range only stops the field
    accepting a value no ceiling could sensibly take.
    """

    key: RiskLimitKey
    label: str
    #: **Nullable, against ``types.ts``'s ``number``** -- and the nullability
    #: is the point rather than an oversight. ``None`` means **no ceiling is
    #: configured**: not zero, not the shipped default, and not an omitted row
    #: (the key is still served, because a page cannot say "no ceiling
    #: configured" about a row it never received).
    #:
    #: ``riskLimitFor`` already returns ``number | null`` for this reason, and
    #: the reason is not hypothetical: the two order tickets once did this
    #: lookup themselves with different fallbacks, ``?? 7`` in one and ``?? 0``
    #: in the other, so one reported a ceiling nobody had set and the other
    #: reported every trade as over-limit. The server must not be the third
    #: guesser. ``types.ts`` needs widening to ``number | null``; flagged
    #: rather than fixed, because it belongs to the frontend dispatch.
    value: JsonMoney | None
    unit: RiskLimitUnit
    min: JsonMoney
    max: JsonMoney
    #: What this ceiling constrains, in one line. "25%" says nothing about
    #: whether it is measured against equity or against the position.
    help: str


class AuditLogEntry(ApiModel):
    """One row of PRD §8.7's single log across risk, feed and notification.

    One log rather than three, because on a bad day the question is whether
    *anything* changed first.
    """

    id: str
    time: datetime
    category: AuditCategory
    #: The stored key -- ``max_risk_per_trade_pct``, not "Max risk per trade".
    #: The log stays a record of what changed rather than of how it was worded
    #: on the day.
    field: str
    previous_value: str
    new_value: str


class NotificationRoute(ApiModel):
    event: NotificationEvent
    bell: bool
    discord: bool


class DataFeed(ApiModel):
    """One feed setting, mirroring one env var read only inside the provider.

    Feed names are configuration and never literals, which is exactly why they
    are selectable: upgrading the plan sets all three and changes nothing else
    in the code.
    """

    key: FeedKey
    env_var: str
    label: str
    value: str
    help: str


class DataSourceStatus(ApiModel):
    name: str
    status: DataSourceState
    detail: str


class ApiKeyPresence(ApiModel):
    """Presence of one credential, and nothing else about it.

    No value, no mask, no last-four. Rule 6: the UI never renders a key, and
    ``PK****4F2A`` renders four characters of one. Presence is the entire
    answer the server is allowed to give -- and the only one worth having,
    since a wrong key cannot be diagnosed by squinting at its suffix.
    """

    env_var: str
    purpose: str
    present: bool
    #: The live keys are *meant* to be absent until Phase 7, so their absence
    #: renders as a fact rather than a warning.
    optional: bool


# --------------------------------------------------------------------------
# Engine state
# --------------------------------------------------------------------------


class EngineStateResponse(ApiModel):
    """The engine's halt state and its start marker.

    ``halted`` is the cold-start default: the engine comes up halted until the
    opening snapshot succeeds, and rule 9 requires an explicit human resume
    out of any halt -- never an automatic one on reconnect.
    """

    halted: bool
    halted_reason: str | None
    halted_at: datetime | None
    #: First-ever-start marker, written once and never rewritten. The equity
    #: curve is Alpaca's; this is where Corollary starts being responsible for
    #: it, so the chart cannot claim credit for pre-Corollary trading.
    t0: datetime | None


class HaltRequest(ApiModel):
    """Why the engine is being halted.

    Required and non-empty. Rule 8's standard -- record the rule, the inputs
    and the timestamp -- has nothing to record if a halt may be reasonless,
    and "the engine is halted and nobody knows why" is the state this field
    exists to prevent.
    """

    #: Bounded at the width of ``engine_state.halted_reason``. A longer reason
    #: is a 422 rather than a value the database silently reshapes.
    reason: str = Field(min_length=1, max_length=256)


# --------------------------------------------------------------------------
# Websocket frames
# --------------------------------------------------------------------------
#
# The browser socket's contract, decided 2026-09-13 and recorded in the Phase
# 2 design spec under step 8d: **the socket carries quotes and
# ``trade_updates`` only.** Engine state and notifications are polled at 15s
# and must never ride it. Two reasons, both load-bearing:
#
# * Rule 9 halts *because* the socket closed, so a halt notification cannot
#   arrive on the thing that just died.
# * A broken **client** socket has to stay distinguishable from a **halted
#   engine**, or a human presses Resume on an engine that was never halted.
#
# So a third server frame kind is a decision, not an addition. ``error`` is
# the only one here beyond the two, and it is not engine state: it reports
# what *this connection* did wrong, which is what an HTTP 4xx reports and it
# carries the same :class:`ApiErrorBody`.
#
# Every frame is tagged with ``type`` and nests its payload under a key named
# after the tag, so reading the wrong payload is a missing key rather than a
# plausible object with absent fields. ``tests/api/test_ws.py`` pins the set
# of kinds.


class WsQuote(ApiModel):
    """A best bid and offer, as it leaves the vendor stream.

    Mirrors :class:`~corollary.data.providers.interface.Quote` field for
    field, because the vendor half's job is to hand this layer a domain
    ``Quote`` and nothing else. One symbol, one price, one source -- the
    invariant CLAUDE.md states for the frontend's ``underlyings`` map, held
    here by the fan-out rather than by convention.

    **No ``mid``.** The midpoint is a derivation the client can make from the
    two sides it already has, and the judgement worth keeping -- a crossed
    quote has *no* midpoint, because a bid above an ask is a data error rather
    than a tradeable market -- lives on ``Quote.mid``, where the vendor half
    reads it. A second copy on the wire is a second thing to disagree with the
    first.
    """

    #: OCC for a contract, a plain ticker for an underlying. This socket
    #: carries both; which of the two vendor sockets it arrived on is not the
    #: browser's business.
    symbol: str
    #: ``None`` when that side of the book is empty. Alpaca sends ``0``, which
    #: it documents as *"the security has no active bid"* -- a zero here would
    #: claim someone is bidding nothing.
    bid: JsonMoney | None
    ask: JsonMoney | None
    bid_size: int
    ask_size: int
    #: The **vendor's** observation timestamp, never the client's clock and
    #: never the server's receive time. Design spec decision 18: the browser
    #: resolves two writers into one quote map by comparing this, and a
    #: receive-time stamp would make the resolution depend on which hop was
    #: slower.
    at: datetime


class WsTradeUpdate(ApiModel):
    """One ``trade_updates`` event: what happened to an order.

    Alpaca's ``trade_updates`` stream, flattened. Every field below maps to
    something the vendor sends -- the event, its timestamp, and the order it
    concerns -- so the vendor half has nothing to invent.

    Not a notification and not engine state. This is the broker telling us
    about an order we placed; a *halt* is Corollary telling the human about
    itself, and that goes out on the 15s poll for the reason in this section's
    header.
    """

    #: The vendor's event name: ``new``, ``fill``, ``partial_fill``,
    #: ``canceled``, ``rejected``, ``expired`` and a dozen more.
    #:
    #: **A free string, deliberately**, on the same reasoning as
    #: :attr:`~corollary.engine.execution.interface.Order.status`: the vendor's
    #: set is open, and a ``Literal`` that has not heard of ``calculated``
    #: turns a real fill notice into a validation error on the way through.
    event: str
    #: The vendor's timestamp for the event, not our receive time.
    at: datetime
    order_id: str
    #: Empty on an ``mleg`` parent, where the parent is the *structure* and
    #: the legs are the instruments.
    symbol: str
    #: The order's status *after* this event. Free string, same reasoning as
    #: :attr:`event`.
    status: str
    #: Corollary's four-way action, resolved server-side from the vendor's
    #: ``position_intent``. ``None`` on an ``mleg`` parent, which carries no
    #: intent at all -- guessing one there is how a buy-to-close gets booked
    #: as a new lot and doubles a position the account already holds.
    action: OrderSide | None
    #: The order's total quantity in contracts. ``None`` where the vendor
    #: sends none, which an ``mleg`` parent does.
    quantity: int | None
    #: Cumulative filled quantity, which is the figure that says whether a
    #: partial fill is still working.
    filled_quantity: int
    #: This event's own execution price and size, present on a fill or a
    #: partial fill and ``None`` on every other event.
    fill_price: JsonMoney | None
    fill_quantity: int | None
    #: **Signed on an ``mleg`` parent**: a negative average fill price is a
    #: net *credit*. The sign is the fact, not a presentation choice -- see
    #: ``Order.filled_avg_price``.
    filled_avg_price: JsonMoney | None
    #: The resulting position size, signed, as the broker sees it. Carried
    #: because rule 9 exists: reconnecting into an unverified position state
    #: is how a bot doubles a position it already holds, and this is the
    #: broker's own answer to what it holds.
    position_quantity: int | None


class WsQuoteFrame(ApiModel):
    """A quote, tagged."""

    type: Literal["quote"] = "quote"
    quote: WsQuote


class WsTradeUpdateFrame(ApiModel):
    """A ``trade_updates`` event, tagged."""

    type: Literal["trade_update"] = "trade_update"
    update: WsTradeUpdate


class WsErrorFrame(ApiModel):
    """A stated condition on a socket that has no status code to carry one.

    The body is :class:`ApiErrorBody` in the same position it occupies in
    :class:`ApiErrorResponse`, so ``api.ts`` parses one error shape everywhere
    and branches on one vocabulary of codes. That is the whole reason it nests
    under ``error`` rather than flattening: over HTTP the envelope *is* the
    response, and a client holding two error parsers uses the wrong one on the
    day it matters.

    **A refusal does not close the socket.** Closing would make a rejected
    subscription look exactly like a dead connection, which is the confusion
    this contract exists to prevent.
    """

    type: Literal["error"] = "error"
    error: ApiErrorBody


WsServerFrame: TypeAlias = Annotated[
    WsQuoteFrame | WsTradeUpdateFrame | WsErrorFrame,
    Field(discriminator="type"),
]
"""Everything the server may send on ``/api/ws``. Three kinds, discriminated.

Discriminated rather than merely tagged: a client that has to infer the kind
from which fields are present infers wrongly the first time a payload gains a
nullable field, and one of these kinds is about money.
"""


class WsSubscribeRequest(ApiModel):
    """The client naming the symbols whose **quotes** it wants.

    A delivery filter on this connection and nothing more. It does **not**
    reach the vendor sockets: which symbols Corollary streams from Alpaca is
    decided by ``engine/stream.py``'s two budgets in priority order, and a
    client-supplied list is admitted there at the lowest priority there is --
    see ``markets_visible_unit``. Step 15 wires that half; a browser asking
    for a symbol has never been a reason to spend a stream slot.

    ``symbols`` has **no default**, on the same reasoning
    ``plan_stream_subscriptions`` gives for its two lists: an omitted list is
    a silent choice. ``null`` means *every symbol the fan-out publishes* and
    spells itself, which reads differently from a forgotten field.

    Applied **whole or not at all**. One malformed symbol refuses the entire
    message and leaves the previous filter in place, for the reason
    ``stream.py`` refuses a partial subscription: the client would believe it
    is watching a list it is not watching.
    """

    type: Literal["subscribe"] = "subscribe"
    symbols: list[str] | None


WsClientFrame: TypeAlias = WsSubscribeRequest
"""Everything a client may send. One kind today, and named anyway.

An alias rather than a bare model, because step 15's viewport hint is a second
client message on this socket. The name is what the transport dispatches on,
so adding the second kind is a union here rather than a new concept in
``routes/ws.py``.
"""
