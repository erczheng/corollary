"""Ingestion — broker activity in, ledger rows out. The only thing that writes.

``ledger.py`` and ``grouping.py`` are pure: activities in, realized trades and
logical positions out, no I/O anywhere. That purity is what makes them
testable and it is also why, until this module existed, **nothing wrote a
single row**. This is the service that closes the gap: it pulls from a
:class:`~corollary.engine.execution.interface.BrokerAccount`, fetches the
contract terms the matcher cannot do without, runs
:func:`~corollary.engine.ledger.build_ledger`, and persists ``fill``,
``realized_trade``, ``mleg_group`` and ``mleg_leg``.

It places no orders and it cannot: it depends on ``BrokerAccount``, which has
no ``submit_order`` in it at all, and on ``MarketDataProvider``. Rule 1 is
structural here rather than disciplinary, and nothing in this file imports
``alpaca``.

Idempotency is the whole design, not a nicety
---------------------------------------------

Ingestion runs on startup and on an interval, and **re-pulling an overlapping
window is the normal case rather than a failure**. ``fill(activity_id
UNIQUE)`` is the entire mechanism that makes that safe, so every write here is
an upsert against what the table already holds: a second run over the same
history leaves the same rows, under the same ids, and raises nothing.

The resume cursor is ``since_id``, which is the vendor's own page token — the
composite id of the last activity the broker returned. It is emphatically
**not** a ``MAX`` over a sorted column. Measured against the real recording
neither the composite activity id nor ``transaction_time`` orders reliably:
stamps repeat and the UUID half then breaks ties arbitrarily, and
``…438268`` came back before ``…438263`` at microsecond resolution even with
``direction=asc``. The invariant that does hold is **per contract symbol**,
which is what the matcher's open-lot queue is built on and the only ordering
anything here depends on.

The history is held in process, because the matcher needs all of it
-------------------------------------------------------------------

FIFO is not incremental. A closing fill pulled today has to meet the opening
lot pulled last week, so the matcher must see the **whole** per-symbol
sequence or it books an ``OVER_CLOSE`` refusal against a lot it already
consumed. The ``fill`` table cannot supply that sequence — it has no
``activity_type`` column, so a row read back cannot say whether it was a
``FILL``, an ``OPEXP``, an ``OPEXC`` or an ``OPASN``, and ``close_kind`` would
collapse to ``fill`` for all four.

So the service accumulates the activities it has pulled, keyed by id, and
re-runs :func:`build_ledger` over the accumulation each time. A cold start
pulls everything (the cursor is empty, which is what makes it a cold start);
every run after that pulls only the tail and folds it into what is already
held. ``fill`` remains the durable record the API pages over — which is what
it is for, since ``page_size`` maxes at 100 and re-fetching all history per
HTTP request is untenable.

Two consequences worth stating rather than discovering:

* The accumulated history grows for the life of the process. For a
  single-user terminal that is nothing; it is written down because it is the
  kind of thing that stops being nothing.
* Fees are **not** persisted — the ten-table schema has no fee table, and a
  ``FEE`` row carries no symbol, no quantity and no ``order_id``, so it
  cannot be a ``fill`` either. :attr:`IngestResult.fees` therefore reports the
  fees in the history this process has seen, which is complete from a cold
  start and partial in no case a caller can currently reach. It is never
  presented as a lifetime figure by this module.

Contract terms are load-bearing, not an optimisation
----------------------------------------------------

The matcher takes ``multiplier`` **per contract** and refuses to book a P&L it
cannot state correctly, so a symbol whose terms were never fetched produces
**no realized trade at all** when it expires or is exercised. That is the same
"terminal that believes you never win" failure as a wrong ``net_amount``,
arriving by refusal rather than by bad arithmetic — visible rather than
silent, and still a gap in lifetime P&L.

``/v2/positions`` returns no multiplier field at all, so ``/v2/options/
contracts`` is the only source and **100 is never substituted**. An adjusted
contract reports ``multiplier: "100"`` and ``size: "100"`` exactly like a
standard one while delivering something else entirely, so the multiplier
cannot detect its own wrongness; only ``root_symbol != underlying_symbol``
can, and that comparison needs the terms to have been fetched.

Terms are cached in process, keyed by symbol, and a cached symbol is never
re-fetched. The cache is a budget saving and nothing more, because **the
retired list is asked as well as the active one**: a contract leaves the
active list at expiry, which is precisely the moment the matcher needs its
multiplier to book that expiry, so a process started afterwards would
otherwise refuse every expired contract forever. ``status`` is Alpaca's own
parameter and its enum is ``active``/``inactive`` — read from the vendor
schema, because ``expired`` is the word the concept invites and is not a value
the endpoint accepts.

An exercise and an assignment close at intrinsic, so they need a price
--------------------------------------------------------------------

``OPEXC`` and ``OPASN`` carry ``net_amount: "0"`` and no price of their own.
What the lot was worth at settlement is its **intrinsic value**, which needs
the strike — on the OCC symbol — *and* the underlying's settlement price,
which this module sources from the underlying's **daily bar on the contract's
expiration date** through ``MarketDataProvider.stock_bars``.

That is the historical surface, so it is **SIP** by the provider's own feed
configuration, and no feed is named here: CLAUDE.md's point about IEX being
~2.5% of US volume applies to a close exactly as it does to a volume
threshold, and the one place that decision lives is
``ALPACA_STOCK_FEED_HISTORICAL`` inside the provider.

**The settlement session is ``min(event_date, expiration)``**, and both
halves of that are load-bearing:

* An expiry auto-exercise can *post late*. A Friday expiry lands as a Monday
  activity, and Monday's close is a different number about a different day, so
  the expiration caps the event's own date.
* An exercise or assignment can happen *early*, which is ordinary on
  American-style equity options -- a short call called away before an
  ex-dividend date, a deep-ITM long put exercised for the interest. Its close
  comes from the session it happened on, days before the contract expires.

Taking the expiration unconditionally is a **look-ahead bug**: it prices a
Tuesday assignment against Friday's close, three sessions in that event's
future. It books silently, because the bar exists and the arithmetic is
well-formed -- the same disease as an indicator reading an unclosed bar,
arriving on the ledger instead of in a backtest.

**When the close cannot be fetched the event is refused, never priced from
something else.** The three tempting substitutes are all well-formed numbers
and all wrong: the strike books a phantom five-figure gain (an exercised NVDA
205 call bought at 14.20 would report ``(205 - 14.20) x 100 = +$19,080``
against an equity move of -$103), ``net_amount`` books a total loss, and the
last quote prices a contract that no longer exists.

Every refusal names its rule, its inputs and its timestamp
----------------------------------------------------------

Rule 8. A refusal is returned on :class:`IngestResult` *and* logged: returning
it is what makes it un-droppable, logging it is what makes it visible without
a caller remembering to look. Two distinct rules cover the terms, because the
remedies differ — "the endpoint does not carry this contract" and "the
endpoint did not answer" are not the same problem and only one of them heals
on the next run.
"""

import hashlib
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ
from corollary.data.providers.interface import (
    Bar,
    BarTimeframe,
    ContractStatus,
    MarketDataProvider,
    OptionContract,
    ProviderError,
)
from corollary.db.models import (
    ACCOUNT_MODES,
    Fill,
    MlegGroup,
    MlegLeg,
    RealizedTrade,
    RejectionRecord,
)
from corollary.db.session import session_scope
from corollary.engine.execution.interface import (
    Activity,
    BrokerAccount,
    NonTradeActivity,
    Order,
    OrderClass,
    OrderQueryStatus,
    TradeActivity,
)
from corollary.engine.ledger import (
    BY_DESIGN_RULES,
    FILL_ACTIVITY_TYPES,
    OPTION_EVENT_TYPES,
    CloseKind,
    FeeRecord,
    Ledger,
    LedgerRejection,
    RealizedTradeRecord,
    as_row_kwargs,
    build_ledger,
    intents_from_orders,
)
from corollary.instruments import OccSymbol, parse_occ_symbol
from corollary.wire import STORED_DETAIL_MAX, vendor_detail

__all__ = [
    "IngestRefusal",
    "IngestResult",
    "IngestRule",
    "IngestService",
]

logger = logging.getLogger(__name__)

#: Every digit, for stripping the numeric suffix off an adjusted OCC root.
_DIGITS = "0123456789"

#: The option events whose close price comes from the **underlying**, and
#: which therefore need a settlement price before anything can be booked.
#:
#: Derived from the ledger's own table rather than restated, so a fourth event
#: type added there arrives here rather than being silently unpriced.
#: ``OPEXP`` is excluded because it closes at **zero** — a price, not an
#: absence — so fetching a settlement for one would spend a request on a
#: number that branch never reads, and would manufacture a refusal for a trade
#: that books perfectly well without it.
_SETTLED_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    kind
    for kind, close_kind in OPTION_EVENT_TYPES.items()
    if close_kind is not CloseKind.EXPIRY
)

#: How far either side of the settlement session to ask for daily bars. One
#: day absorbs any stamping convention the vendor might use without widening
#: the request into a range whose right bar has to be *chosen* rather than
#: *found* — the bar is still matched on its Eastern session date, never taken
#: as "the first" or "the last" of the window.
_SETTLEMENT_WINDOW = timedelta(days=1)


class IngestRule(StrEnum):
    """Why ingestion refused to do something. One value per reason.

    A rule is a thing you can count, filter and alert on; a sentence is not.
    These are ingestion's own refusals — the matcher's live on
    :class:`~corollary.engine.ledger.RejectionRule` and are passed through
    untouched, because a refusal to *fetch* and a refusal to *book* are
    different failures with different remedies.
    """

    #: The contracts endpoint answered and did not carry this symbol. A
    #: **gap in lifetime P&L**, not a transient error: the matcher has no
    #: multiplier, so nothing this contract does will ever be booked.
    CONTRACT_TERMS_UNAVAILABLE = "contract_terms_unavailable"
    #: The contracts endpoint did not answer. Same consequence for this run,
    #: different remedy — this one heals on the next one.
    CONTRACT_TERMS_FETCH_FAILED = "contract_terms_fetch_failed"
    #: An ``OPEXC`` or ``OPASN`` whose underlying has no daily bar on the
    #: settlement session, so the contract's intrinsic value at settlement
    #: cannot be computed. **Refused rather than substituted**: the strike
    #: books a five-figure phantom gain, ``net_amount`` is ``"0"`` and books a
    #: total loss, and the last quote prices a contract that no longer exists.
    #: All three are well-formed numbers and all three are wrong.
    SETTLEMENT_PRICE_UNAVAILABLE = "settlement_price_unavailable"
    #: The bars endpoint did not answer. Heals on the next run, unlike the
    #: one above.
    SETTLEMENT_PRICE_FETCH_FAILED = "settlement_price_fetch_failed"
    #: One symbol whose exercises and assignments settled on **more than one
    #: session** -- part of a short called away early and the rest at expiry,
    #: say. ``settlements`` is keyed by symbol and therefore holds one price,
    #: which cannot be right for both dates; whichever were chosen, the other
    #: event would book a well-formed wrong number with nothing to show it.
    #: So the symbol is refused whole. The raw ``fill`` and activity rows are
    #: kept either way, so the gap is recomputable once a per-event price has
    #: somewhere to live; a booked wrong number is only reversible once
    #: somebody notices.
    SETTLEMENT_SESSION_AMBIGUOUS = "settlement_session_ambiguous"
    #: An ``mleg`` order arrived with no ``legs[]``. Either the history was
    #: fetched without ``?nested=true`` — in which case the join is one-hop
    #: and **nothing will ever group** — or the row is a leg rather than a
    #: parent. Loud either way, because the one-hop failure is otherwise
    #: completely silent.
    MLEG_ORDER_HAS_NO_LEGS = "mleg_order_has_no_legs"
    #: A leg whose ratio is absent, zero, negative or fractional, or which
    #: carries no ``position_intent``. ``mleg_leg`` refuses all four at the
    #: column, and a fractional ratio is not a spread.
    MLEG_LEG_NOT_WRITABLE = "mleg_leg_not_writable"


#: The two that mean "this symbol has no contract terms", whatever the cause.
_TERMS_RULES = frozenset(
    {IngestRule.CONTRACT_TERMS_UNAVAILABLE, IngestRule.CONTRACT_TERMS_FETCH_FAILED}
)

#: The three that mean "this exercise or assignment has no settlement price".
_SETTLEMENT_RULES = frozenset(
    {
        IngestRule.SETTLEMENT_PRICE_UNAVAILABLE,
        IngestRule.SETTLEMENT_PRICE_FETCH_FAILED,
        IngestRule.SETTLEMENT_SESSION_AMBIGUOUS,
    }
)


@dataclass(frozen=True, slots=True)
class IngestRefusal:
    """One thing ingestion declined, with everything needed to explain it.

    Rule 8: *"A rejected order records the rule that rejected it, the inputs,
    and the timestamp."* The same standard applies to a refused **symbol** —
    you will need this the first time lifetime P&L is missing a trade you
    remember making.
    """

    rule: IngestRule
    detail: str
    at: datetime
    symbol: str | None = None
    #: The ticker the contracts endpoint was asked about, which is **not** the
    #: OCC root on an adjusted contract.
    underlying: str | None = None
    order_id: str | None = None
    activity_ids: tuple[str, ...] = ()
    activity_types: tuple[str, ...] = ()
    inputs: Mapping[str, str] = field(default_factory=dict)

    @property
    def carries_option_event(self) -> bool:
        """Whether an ``OPEXP``, ``OPEXC`` or ``OPASN`` is among the activities.

        The distinction the step-7 amendment turns on. A closing **fill** with
        no terms is refused too, but an option event is the case where the
        refusal is the *only* thing between a position ending and a realized
        loss going unrecorded.
        """
        return any(kind in OPTION_EVENT_TYPES for kind in self.activity_types)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What one run did, and what it could not do.

    Returned rather than logged-and-forgotten because two callers need it: the
    API route that reports ingestion status, and step 8's scheduler, which
    runs this on an interval and has to surface a gap rather than absorb one.
    """

    account: str
    #: One id per run, on every log line the run emits, so a trade can be
    #: traced from the pull through the refusals to the rows.
    correlation_id: str
    at: datetime

    #: Activities the broker returned **this run**, not the accumulated total.
    activities_pulled: int
    #: The ``since_id`` the next run resumes from: the id of the last activity
    #: the vendor returned. ``None`` when nothing has ever been pulled.
    cursor: str | None

    fills_written: int
    fills_updated: int
    trades_written: int
    trades_removed: int
    mleg_groups_written: int
    mleg_legs_written: int
    #: ``FEE`` rows in the history this process holds. Not persisted — there
    #: is no fee table — so this is a report, never a stored total.
    fees_seen: int

    fees: tuple[FeeRecord, ...] = ()
    #: The matcher's refusals, passed through unchanged.
    rejections: tuple[LedgerRejection, ...] = ()
    #: Ingestion's own refusals.
    refusals: tuple[IngestRefusal, ...] = ()

    #: Refusal rows written to ``ledger_rejection`` this pass. Not the number
    #: of refusals: the by-design declines are not stored, and a refusal this
    #: table already held is updated rather than written again.
    rejections_written: int = 0
    #: Rows cleared because this pass re-examined what they were about and no
    #: longer refuses it. **The number that matters for trusting the rest of
    #: them** -- a persisted refusal that outlives its cause claims a gap that
    #: is not there, which decision 14 rates as worse than no table at all.
    rejections_cleared: int = 0

    @property
    def unfetched_terms(self) -> tuple[IngestRefusal, ...]:
        """Symbols with no contract terms — the gap a caller must surface.

        A view over :attr:`refusals` rather than a second stored list, so the
        two can never disagree about how many there are.
        """
        return tuple(item for item in self.refusals if item.rule in _TERMS_RULES)

    @property
    def unpriced_settlements(self) -> tuple[IngestRefusal, ...]:
        """Exercises and assignments with no settlement price. The same gap.

        Kept separate from :attr:`unfetched_terms` because the two say
        different things to whoever reads them: one is *"this contract is
        unknown"* and the other is *"this contract is known and the day it
        settled is not"*. Both end in no realized trade.
        """
        return tuple(item for item in self.refusals if item.rule in _SETTLEMENT_RULES)


@dataclass(slots=True)
class _Demand:
    """Why a symbol's terms are wanted, and what to say if they never arrive."""

    occ: OccSymbol
    activity_ids: list[str] = field(default_factory=list)
    activity_types: set[str] = field(default_factory=set)
    at: datetime | None = None
    #: The **Eastern session dates** of this symbol's ``OPEXC`` and ``OPASN``
    #: rows, and of nothing else. Not :attr:`at`, which is the latest stamp
    #: over every activity on the symbol including the opening fill: what a
    #: settlement needs is the day the *event* happened, and an opening fill
    #: is not one. Undated events contribute nothing, because the matcher
    #: refuses them on the missing timestamp before it ever asks for a price.
    settled_dates: set[date] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Settlement:
    """Which session one symbol's exercise or assignment is priced against.

    Carries the derivation as well as the answer, because a refusal has to be
    able to say *why* it wanted the day it wanted -- rule 8's "the inputs".
    """

    underlying: str
    #: ``min(event_date, expiration)``. See :meth:`_ensure_settlements`.
    session: date
    expiration: date
    #: The event dates this collapsed from, sorted. More than one only when
    #: they all cap to the same session.
    event_dates: tuple[date, ...]


def _activity_stamp(activity: Activity) -> datetime | None:
    """When an activity happened, whichever shape it arrived in."""
    if isinstance(activity, TradeActivity):
        return activity.transaction_time
    return activity.created_at


def _event_session(activity: Activity) -> date | None:
    """The **Eastern** session date an activity belongs to, or ``None``.

    ``NonTradeActivity.activity_date`` is the vendor's own ``date`` field and
    is preferred where it is present: it is a *date* the vendor stated, not
    one derived here from an instant. The fallback converts the stamp to
    Eastern rather than reading its UTC date, because a 20:30 ET event is
    already tomorrow in UTC and that is a whole session of error.

    Which of the two wins matters much less than it looks, and that is worth
    knowing rather than trusting: :meth:`_ensure_settlements` caps the answer
    at the contract's expiration, so a late-posted expiry comes out at the
    expiry under either reading, and an early exercise is stamped on its own
    day under both.
    """
    if isinstance(activity, NonTradeActivity) and activity.activity_date is not None:
        return activity.activity_date
    stamp = _activity_stamp(activity)
    if stamp is None:
        return None
    return stamp.astimezone(NYSE_TZ).date()


def _utc(moment: datetime) -> datetime:
    """UTC, so two instants that are the same instant compare and hash alike."""
    return moment.astimezone(timezone.utc)


#: The natural key of a realized trade: everything the matcher emits except
#: the surrogate id. Used to reconcile the table against a rebuilt ledger
#: without an ``ORDER BY`` or an aggregate anywhere near a ``Money`` column.
_TradeKey = tuple[
    str, datetime, datetime, int, Decimal, Decimal, Decimal, Decimal | None, str
]


def _record_key(record: RealizedTradeRecord) -> _TradeKey:
    return (
        record.symbol,
        _utc(record.opened_at),
        _utc(record.closed_at),
        record.qty,
        record.open_price,
        record.close_price,
        record.pnl,
        record.pnl_pct,
        # `.value`, not the member: `StrEnum.__hash__` is the Enum one, so an
        # enum and its own string are equal without hashing alike -- which
        # makes them different dict keys while comparing True.
        record.close_kind.value,
    )


def _row_key(row: RealizedTrade) -> _TradeKey:
    return (
        row.symbol,
        _utc(row.opened_at),
        _utc(row.closed_at),
        row.qty,
        row.open_price,
        row.close_price,
        row.pnl,
        row.pnl_pct,
        row.close_kind,
    )


class IngestService:
    """Pull, upsert, match, persist. One account, one broker, one book.

    The account is fixed at construction rather than passed per call: a
    ``BrokerAccount`` **is** one account, since its credentials decide which
    book it reads. Two books are two brokers and two services, which is also
    what stops a cash fill being written under a paper group.
    """

    def __init__(
        self,
        *,
        broker: BrokerAccount,
        provider: MarketDataProvider,
        engine: Engine,
        account: str = "paper",
        secrets: Sequence[str] = (),
        correlation_id: Callable[[], str] | None = None,
    ) -> None:
        if account not in ACCOUNT_MODES:
            raise ValueError(
                f"account {account!r} is not one of {ACCOUNT_MODES}. Refused "
                "here rather than at the first INSERT, where the CHECK fires "
                "after a whole run's work and names a column rather than this "
                "argument."
            )
        self._broker = broker
        self._provider = provider
        self._engine = engine
        self._account = account
        #: Both halves of the vendor key pair, for ``vendor_detail`` to
        #: substitute out of a stored refusal's free text. Passed in rather
        #: than read from the environment here: ``os.environ`` belongs to the
        #: composition root, and an engine module that read it would also read
        #: it inside the backtest worker rule 2 scrubs.
        #:
        #: **The default is empty and that is a real gap, not a safe one.**
        #: A service built without them redacts against the paper
        #: account-number shape alone, which is the state every pass was in
        #: before this argument existed. It is a default because the
        #: alternative -- a required argument -- would make the far commoner
        #: call sites (tests, a backfill, a book with no vendor error text at
        #: all) pass ``()`` explicitly to say nothing, and a ceremony that
        #: usually means nothing is one people stop reading. The read path in
        #: ``api/routes/activity.py`` re-redacts against the process's own
        #: credentials for exactly this case, and cannot repair a cut this
        #: pass makes: see :meth:`_rejection_values`.
        self._secrets: tuple[str, ...] = tuple(secrets)
        self._new_correlation_id = correlation_id or (lambda: uuid4().hex)

        #: The vendor's page token, not a sort key. See the module docstring.
        self._cursor: str | None = None
        #: Every activity this process has pulled, keyed by the composite id.
        #: FIFO is not incremental, so the matcher needs all of it.
        self._history: dict[str, Activity] = {}
        #: Contract terms, keyed by OCC symbol. A cached symbol is never
        #: re-fetched.
        self._terms: dict[str, OptionContract] = {}
        #: The underlying's official close, keyed by ``(ticker, session)``.
        #: Keyed on the *underlying* rather than on the contract so a spread
        #: settling on one day costs one request rather than one per leg. A
        #: settled close never changes, so a hit is never re-fetched.
        self._settlements: dict[tuple[str, date], Decimal] = {}

    @property
    def account(self) -> str:
        return self._account

    @property
    def cursor(self) -> str | None:
        return self._cursor

    async def run(
        self, *, settlements: Mapping[str, Decimal] | None = None
    ) -> IngestResult:
        """One ingestion pass. Safe to call again immediately.

        ``settlements`` maps an option symbol to the **underlying's**
        settlement price, and entries passed here are **authoritative**: they
        are used as given and never re-fetched. Anything not supplied is
        sourced from the underlying's daily bar on the settlement session —
        see :meth:`_ensure_settlements`. The parameter mirrors
        ``build_ledger``'s own argument of the same name, one convention
        rather than two, and it is the seam a backfill over a window the bars
        endpoint no longer covers would use.
        """
        correlation = self._new_correlation_id()
        started = datetime.now(timezone.utc)

        pulled = await self._broker.activities(since_id=self._cursor)
        if pulled:
            self._cursor = pulled[-1].id
        for activity in pulled:
            self._history[activity.id] = activity

        # ALL, and nested — the two-hop join has no other source for the
        # leg-id -> parent-id map, and a one-hop join groups nothing at all
        # while looking exactly like a history of single-leg orders.
        orders = await self._broker.orders(status=OrderQueryStatus.ALL)

        refusals: list[IngestRefusal] = []
        demand = self._demand()
        terms = await self._ensure_terms(demand, refusals, correlation)
        prices = await self._ensure_settlements(
            demand, terms, settlements or {}, refusals, correlation
        )

        ledger = build_ledger(
            self._history.values(),
            account=self._account,
            intents=intents_from_orders(orders),
            multipliers={
                symbol: contract.multiplier for symbol, contract in terms.items()
            },
            settlements=prices,
            contracts=terms,
        )

        # Any symbol this run could not fully account for. Its existing rows
        # are left alone below: a provider outage must not erase lifetime P&L.
        #
        # Canonicalised, because the two sides of that comparison are written
        # by different hands. `_write_trades` tests `RealizedTrade.symbol`,
        # which is always the parser's form -- it comes from
        # `LotMovement.symbol`, which is `parse_occ_symbol(...).symbol`,
        # stripped and upper-cased. This side is the **vendor's** spelling:
        # `_demand` keys on `activity.symbol`, and seven ledger rules reject
        # before a contract is resolved and pass `activity.symbol` through
        # verbatim -- FRACTIONAL_QUANTITY (on a fill; the event path's copy
        # carries `contract.symbol`), INTENT_CONTRADICTS_SIDE, UNKNOWN_INTENT,
        # MISSING_SYMBOL, NOT_AN_OPTION, NOT_A_LEDGER_ACTIVITY and
        # MISSING_FEE_AMOUNT. The last three add nothing to the guard in
        # practice -- a non-OCC symbol, a cash journal's, and a FEE row's,
        # which Alpaca sends with no symbol at all -- but they are inputs to
        # it all the same, and the fold below is what keeps any of them from
        # mattering if that stops being true.
        #
        # Left raw, a padded or lower-cased spelling misses the held row and a
        # **booked trade is deleted**: a short closed by a `buy` books while
        # the order is still in the orders window, and on the pass after that
        # window rolls off `implied_intent` is None, the close is refused with
        # UNKNOWN_INTENT under the vendor's spelling, no trade is re-derived,
        # and lifetime P&L silently loses one it had already stated. Folding
        # can only *widen* the guard, which is the direction to be wrong in:
        # too broad leaves a row that should have gone, too narrow erases
        # money.
        guarded: set[str] = set()
        for spelling in [item.symbol for item in refusals] + [
            rejection.symbol for rejection in ledger.rejections
        ]:
            canonical = _canonical_symbol(spelling)
            if canonical is not None:
                guarded.add(canonical)

        with session_scope(self._engine) as session:
            fills_written, fills_updated = self._write_fills(session, ledger)
            trades_written, trades_removed = self._write_trades(
                session, ledger, guarded
            )
            groups_written, legs_written = self._write_mleg(
                session, orders, refusals, correlation
            )
            # Last, and inside the same transaction: `_write_mleg` appends to
            # `refusals` as it goes, so anything written before it would miss
            # the mleg refusals entirely. One transaction, because a run that
            # wrote rows and then failed to record what it would not write is
            # a gap with no stated cause -- the exact thing decision 14 is
            # about.
            rejections_written, rejections_cleared = self._write_rejections(
                session,
                ledger.rejections,
                refusals,
                correlation=correlation,
                at=started,
                examined_activities=set(self._history),
                examined_orders={order.id for order in orders},
                examined_symbols=self._examined_symbols(),
            )

        result = IngestResult(
            account=self._account,
            correlation_id=correlation,
            at=started,
            activities_pulled=len(pulled),
            cursor=self._cursor,
            fills_written=fills_written,
            fills_updated=fills_updated,
            trades_written=trades_written,
            trades_removed=trades_removed,
            mleg_groups_written=groups_written,
            mleg_legs_written=legs_written,
            fees_seen=len(ledger.fees),
            fees=ledger.fees,
            rejections=ledger.rejections,
            refusals=tuple(refusals),
            rejections_written=rejections_written,
            rejections_cleared=rejections_cleared,
        )
        logger.info(
            "ingest pulled %d activities and wrote %d fills, %d trades",
            result.activities_pulled,
            result.fills_written,
            result.trades_written,
            extra={
                "event": "ingest_complete",
                "correlation_id": correlation,
                "account": self._account,
                "activities_pulled": result.activities_pulled,
                "history_held": len(self._history),
                "fills_written": result.fills_written,
                "fills_updated": result.fills_updated,
                "trades_written": result.trades_written,
                "trades_removed": result.trades_removed,
                "mleg_groups_written": result.mleg_groups_written,
                "rejections": len(result.rejections),
                "rejections_written": result.rejections_written,
                "rejections_cleared": result.rejections_cleared,
                "unfetched_terms": len(result.unfetched_terms),
                "cursor": result.cursor,
            },
        )
        return result

    # ------------------------------------------------------------------
    # Contract terms
    # ------------------------------------------------------------------

    async def _ensure_terms(
        self,
        demand: Mapping[str, _Demand],
        refusals: list[IngestRefusal],
        correlation: str,
    ) -> dict[str, OptionContract]:
        """Terms for every option symbol in the held history, cached by symbol.

        Fetched per ``(underlying, expiration)`` rather than per symbol, which
        is one request for a whole vertical instead of two. Strike bounds are
        deliberately *not* narrowed: every contract is matched back by exact
        symbol, so a filter that were subtly wrong would cost a real trade its
        P&L, and the payload saving is not worth that.

        Up to four queries, in a deliberate order, each one asked only for the
        symbols the previous one did not answer:

        1. the OCC root, **active** — the overwhelmingly common case;
        2. the root with its numeric suffix stripped, active — an adjusted
           contract is filed under the underlying, and ``AAPL1`` is a modified
           root rather than a ticker;
        3. the OCC root, **inactive** — a contract leaves the active list when
           it **expires**, which is exactly when the matcher needs its
           multiplier to book that expiry. Without this pass a process started
           after an expiry could never book it, and no amount of retrying
           would help because the answer never changes;
        4. the stripped root, inactive — both exceptions at once.

        Active before inactive, and verbatim before stripped, because almost
        every symbol is answered by the first query and the others are the
        exceptions. Reversing the order would spend an extra request per live
        symbol on every cycle against a 200/min budget.

        Every pass matches its answer back by **exact symbol** before caching,
        which is what makes a speculative query safe: it can find the right
        contract or nothing, never the wrong terms.
        """
        outstanding = sorted(symbol for symbol in demand if symbol not in self._terms)
        failures: dict[str, str] = {}

        for underlying_of, status in _TERM_QUERIES:
            if not outstanding:
                break
            if underlying_of is _stripped_root:
                # Only where the root really carries a suffix, so the stripped
                # query is never the same request twice.
                candidates = [
                    symbol
                    for symbol in outstanding
                    if demand[symbol].occ.root[-1:].isdigit()
                ]
            else:
                candidates = outstanding
            if not candidates:
                continue
            failures.update(
                await self._fetch(candidates, demand, underlying_of, status)
            )
            outstanding = [s for s in outstanding if s not in self._terms]

        for symbol in sorted(demand):
            if symbol in self._terms:
                continue
            self._refuse_terms(refusals, correlation, symbol, demand[symbol], failures)

        return {symbol: self._terms[symbol] for symbol in demand if symbol in self._terms}

    def _demand(self) -> dict[str, _Demand]:
        """Every OCC symbol in the held history, with why it is wanted."""
        demand: dict[str, _Demand] = {}
        for activity in self._history.values():
            symbol = activity.symbol
            if not symbol:
                continue
            try:
                occ = parse_occ_symbol(symbol)
            except ValueError:
                # A stock trade, or the `OPTRD` leg of an option event against
                # the underlying. Neither is a contract with a multiplier; the
                # matcher declines them with a reason of its own.
                continue
            entry = demand.get(symbol)
            if entry is None:
                entry = demand[symbol] = _Demand(occ=occ)
            entry.activity_ids.append(activity.id)
            entry.activity_types.add(activity.activity_type)
            stamp = _activity_stamp(activity)
            if stamp is not None and (entry.at is None or stamp > entry.at):
                entry.at = stamp
            if activity.activity_type in _SETTLED_EVENT_TYPES:
                session = _event_session(activity)
                if session is not None:
                    entry.settled_dates.add(session)
        return demand

    async def _fetch(
        self,
        symbols: Sequence[str],
        demand: Mapping[str, _Demand],
        underlying_of: Callable[[OccSymbol], str],
        status: ContractStatus,
    ) -> dict[str, str]:
        """Fill the cache for ``symbols``. Returns the ones whose fetch failed.

        A failure is per *query*, so every symbol in a failed group is
        reported: the alternative is one refusal naming an underlying, which
        is not the thing a reader is missing a P&L for.
        """
        groups: dict[tuple[str, date], list[str]] = {}
        for symbol in symbols:
            occ = demand[symbol].occ
            groups.setdefault((underlying_of(occ), occ.expiration), []).append(symbol)

        failures: dict[str, str] = {}
        for (underlying, expiration), members in sorted(groups.items()):
            try:
                found = await self._provider.option_contracts(
                    underlying,
                    expiration_gte=expiration,
                    expiration_lte=expiration,
                    status=status,
                    # Asked for by name, and this caller has thought about the
                    # multiplier. Excluding them here would leave a held
                    # adjusted contract with no terms at all, which refuses
                    # its closing *fills* too -- and a premium difference
                    # times the multiplier is right whatever the deliverable.
                    # With the terms present the matcher refuses the narrower
                    # thing: the settlement, which really is unstateable.
                    include_adjusted=True,
                    # `ShareDelivery` names the shares an exercise moved from
                    # these and from nothing else; `multiplier` and `size` are
                    # both the wrong question.
                    show_deliverables=True,
                )
            except ProviderError as exc:
                detail = f"{type(exc).__name__}: {exc}"
                for symbol in members:
                    failures[symbol] = detail
                continue
            wanted = set(members)
            for contract in found:
                if contract.symbol in wanted:
                    self._terms[contract.symbol] = contract
        return failures

    # ------------------------------------------------------------------
    # Settlement prices
    # ------------------------------------------------------------------

    async def _ensure_settlements(
        self,
        demand: Mapping[str, _Demand],
        terms: Mapping[str, OptionContract],
        supplied: Mapping[str, Decimal],
        refusals: list[IngestRefusal],
        correlation: str,
    ) -> dict[str, Decimal]:
        """The underlying's official close for every exercise and assignment.

        ``OPEXC`` and ``OPASN`` carry ``net_amount: "0"`` and no price of
        their own, so what the lot was worth when it settled is its
        **intrinsic value** — which needs the strike (on the OCC symbol) *and*
        the underlying's settlement price (here). Booking the strike itself as
        the close price is the reading the design spec's own wording invites
        and it is wrong by five figures: an exercised NVDA 205 call bought at
        14.20 would report ``(205 - 14.20) x 100 = +$19,080`` against an
        equity move of -$103.

        **The settlement session is ``min(event_date, expiration)``.** One
        rule, and it is the same rule in both directions rather than a special
        case bolted onto either:

        * A **late-posted expiry** — Alpaca auto-exercises an ITM contract at
          a Friday expiry and the activity row lands on Monday. ``min(Mon,
          Fri)`` is Friday, and Monday's close is a different number about a
          different day.
        * An **early exercise or assignment** — ordinary on American-style
          equity options: a short call called away before an ex-dividend date,
          a deep-ITM long put exercised for the interest. ``min(Tue, Fri)`` is
          Tuesday, which is the day the lot actually settled.

        Taking the expiration unconditionally is a **look-ahead bug**, and
        naming it as one is the point: it prices a Tuesday assignment against
        a close three sessions in that event's future. It fails silently in
        the common direction — ingestion runs on an interval, so the future
        bar usually exists by the time anyone asks, and a well-formed wrong
        number is booked with no refusal and no log line. Run before the
        expiry it fails loudly instead, naming a date the event had nothing to
        do with.

        A symbol whose events settled on **more than one** session is refused
        whole: ``settlements`` is keyed by symbol and holds one price, and one
        price cannot be right for two days.

        Bars go through :meth:`MarketDataProvider.stock_bars`, which is the
        historical surface and therefore **SIP** by the provider's own feed
        configuration. No feed is named here and none should be: CLAUDE.md's
        point about IEX being ~2.5% of volume applies to a close as much as to
        a volume threshold, and the one place that decision lives is
        ``ALPACA_STOCK_FEED_HISTORICAL`` inside the provider.

        Symbols whose *terms* are missing are skipped rather than refused
        twice: the terms refusal already says that contract books nothing, and
        without the terms the underlying cannot even be named, since an
        adjusted root is not a ticker.

        **A close that cannot be fetched is refused, never substituted.**
        """
        settlements: dict[str, Decimal] = dict(supplied)

        wanted: dict[str, _Settlement] = {}
        # Sorted, so two runs over the same history emit refusals in the same
        # order. Unsorted it would follow the order the vendor happened to
        # page the activities in, which is the ordering the module docstring
        # records as unreliable.
        for symbol in sorted(demand):
            entry = demand[symbol]
            if symbol in settlements:
                continue
            if entry.activity_types.isdisjoint(_SETTLED_EVENT_TYPES):
                continue
            contract = terms.get(symbol)
            if contract is None:
                continue
            event_dates = tuple(sorted(entry.settled_dates))
            if not event_dates:
                # Every settled event on this symbol is undated. The matcher
                # refuses those on the missing timestamp before it reaches the
                # price, so fetching one would spend a request to produce a
                # second refusal about the same rows.
                continue
            sessions = {min(day, contract.expiration) for day in event_dates}
            if len(sessions) > 1:
                self._refuse_ambiguous_session(
                    refusals, correlation, symbol, entry, contract, sessions
                )
                continue
            wanted[symbol] = _Settlement(
                underlying=contract.underlying_symbol,
                session=sessions.pop(),
                expiration=contract.expiration,
                event_dates=event_dates,
            )

        by_session: dict[date, set[str]] = {}
        for entry_settlement in wanted.values():
            key = (entry_settlement.underlying, entry_settlement.session)
            if key in self._settlements:
                continue
            by_session.setdefault(entry_settlement.session, set()).add(
                entry_settlement.underlying
            )

        failures: dict[tuple[str, date], str] = {}
        for session, underlyings in sorted(by_session.items()):
            start = _session_midnight(session - _SETTLEMENT_WINDOW)
            end = _session_midnight(session + _SETTLEMENT_WINDOW)
            try:
                series = await self._provider.stock_bars(
                    sorted(underlyings),
                    timeframe=BarTimeframe.DAY,
                    start=start,
                    end=end,
                )
            except ProviderError as exc:
                detail = f"{type(exc).__name__}: {exc}"
                for underlying in underlyings:
                    failures[(underlying, session)] = detail
                continue
            for underlying in sorted(underlyings):
                close = _session_close(series.get(underlying, ()), session)
                if close is not None:
                    self._settlements[(underlying, session)] = close

        for symbol in sorted(wanted):
            found = wanted[symbol]
            key = (found.underlying, found.session)
            close = self._settlements.get(key)
            if close is not None:
                settlements[symbol] = close
                continue
            self._refuse_settlement(
                refusals, correlation, symbol, demand[symbol], found, failures.get(key)
            )
        return settlements

    def _refuse_settlement(
        self,
        refusals: list[IngestRefusal],
        correlation: str,
        symbol: str,
        entry: _Demand,
        found: _Settlement,
        failure: str | None,
    ) -> None:
        """No close for the session this event actually settled on.

        The session is named, and it is the session **needed** rather than the
        contract's expiration. A refusal that names the wrong day sends
        whoever reads it looking for a bar that was never the answer.
        """
        underlying, session = found.underlying, found.session
        rule = (
            IngestRule.SETTLEMENT_PRICE_FETCH_FAILED
            if failure
            else IngestRule.SETTLEMENT_PRICE_UNAVAILABLE
        )
        kinds = tuple(sorted(entry.activity_types))
        events = ",".join(day.isoformat() for day in found.event_dates)
        detail = (
            f"no closing price for {underlying} on {session.isoformat()}, so "
            f"{symbol} has no intrinsic value at settlement and the exercise "
            "or assignment books nothing. That session is "
            f"min(event {events}, expiration {found.expiration.isoformat()}) "
            "-- the day the lot settled, which is the expiration only when the "
            "event happened at it. Refused rather than substituted: the strike "
            "books a phantom gain, net_amount is '0' and books a total loss, "
            "and the last quote prices a contract that no longer exists"
        )
        if failure:
            detail = f"{detail}. The bars endpoint did not answer: {failure}"
        self._refuse(
            refusals,
            correlation,
            rule,
            detail,
            at=entry.at or datetime.now(timezone.utc),
            symbol=symbol,
            underlying=underlying,
            activity_ids=tuple(entry.activity_ids),
            activity_types=kinds,
            inputs={
                "activity_types": ",".join(kinds),
                "settlement_date": session.isoformat(),
                "event_date": events,
                "expiration": found.expiration.isoformat(),
                "underlying": underlying,
            },
        )

    def _refuse_ambiguous_session(
        self,
        refusals: list[IngestRefusal],
        correlation: str,
        symbol: str,
        entry: _Demand,
        contract: OptionContract,
        sessions: set[date],
    ) -> None:
        """One symbol, two settlement sessions, one place to put a price.

        Part of a short called away early and the rest at expiry is an
        ordinary way for a position to end, and it needs two different closes.
        ``settlements`` is keyed by symbol and holds one, so whichever session
        were chosen the other event would book a well-formed wrong number with
        nothing to show it -- the same silent, directional failure the
        ``min(event_date, expiration)`` rule exists to stop.

        So the symbol is refused whole rather than half-priced. The raw
        ``fill`` and activity rows are kept, which is what makes the gap
        recomputable once a per-event price has somewhere to live; a booked
        wrong number is only reversible once somebody notices.
        """
        kinds = tuple(sorted(entry.activity_types))
        days = ",".join(day.isoformat() for day in sorted(sessions))
        detail = (
            f"{symbol} has exercises or assignments settling on {len(sessions)} "
            f"different sessions ({days}), and one settlement price per symbol "
            "cannot be right for all of them. Priced at any one of them the "
            "others would book a well-formed wrong number with nothing to show "
            "it, so the symbol books nothing and is reported instead. The rows "
            "are kept: this is a gap in lifetime P&L, not a lost record"
        )
        self._refuse(
            refusals,
            correlation,
            IngestRule.SETTLEMENT_SESSION_AMBIGUOUS,
            detail,
            at=entry.at or datetime.now(timezone.utc),
            symbol=symbol,
            underlying=contract.underlying_symbol,
            activity_ids=tuple(entry.activity_ids),
            activity_types=kinds,
            inputs={
                "activity_types": ",".join(kinds),
                "settlement_sessions": days,
                "event_dates": ",".join(
                    day.isoformat() for day in sorted(entry.settled_dates)
                ),
                "expiration": contract.expiration.isoformat(),
                "underlying": contract.underlying_symbol,
            },
        )

    def _refuse_terms(
        self,
        refusals: list[IngestRefusal],
        correlation: str,
        symbol: str,
        entry: _Demand,
        failures: Mapping[str, str],
    ) -> None:
        failure = failures.get(symbol)
        rule = (
            IngestRule.CONTRACT_TERMS_FETCH_FAILED
            if failure
            else IngestRule.CONTRACT_TERMS_UNAVAILABLE
        )
        kinds = tuple(sorted(entry.activity_types))
        event = any(kind in OPTION_EVENT_TYPES for kind in kinds)
        detail = (
            f"no contract terms for {symbol}, so its multiplier is unknown and "
            "the matcher will book no P&L for it. 100 is the standard "
            "deliverable, not a safe default, and /v2/positions carries no "
            "multiplier at all"
        )
        if failure:
            detail = f"{detail}. The contracts endpoint did not answer: {failure}"
        if event:
            detail = (
                f"{detail}. This symbol carries an option event "
                f"({', '.join(k for k in kinds if k in OPTION_EVENT_TYPES)}), "
                "so the position ended and the realized loss or gain is "
                "missing from lifetime P&L rather than merely delayed"
            )
        self._refuse(
            refusals,
            correlation,
            rule,
            detail,
            at=entry.at or datetime.now(timezone.utc),
            symbol=symbol,
            underlying=entry.occ.root,
            activity_ids=tuple(entry.activity_ids),
            activity_types=kinds,
            inputs={
                "activity_types": ",".join(kinds),
                "expiration": entry.occ.expiration.isoformat(),
                "carries_option_event": str(event).lower(),
                # The ticker the contracts endpoint was asked about, which is
                # the **OCC root** and not necessarily the underlying:
                # `_stripped_root` asks about `GME` for a `GME1` contract, and
                # the whole reason this row exists is that nothing came back
                # to say which. Named `root_symbol` rather than `underlying`
                # for that reason -- `_refuse_settlement` has the contract in
                # hand and can carry a real `underlying`, this cannot, and a
                # key claiming the root *is* the underlying would be wrong on
                # exactly the adjusted contracts this table is here for.
                #
                # `IngestRefusal.underlying` below carries the same value, but
                # `ledger_rejection` has no column for it: without this key
                # the root never reaches the wire on the two terms rules, and
                # a reader holding only the OCC symbol cannot tell an
                # adjustment from a fetch that failed.
                "root_symbol": entry.occ.root,
            },
        )

    def _refuse(
        self,
        refusals: list[IngestRefusal],
        correlation: str,
        rule: IngestRule,
        detail: str,
        *,
        at: datetime,
        symbol: str | None = None,
        underlying: str | None = None,
        order_id: str | None = None,
        activity_ids: tuple[str, ...] = (),
        activity_types: tuple[str, ...] = (),
        inputs: Mapping[str, str] | None = None,
    ) -> None:
        """Record a refusal, and log it. Both, never either.

        The record is what makes the refusal un-droppable; the log line is
        what makes it visible without a caller remembering to look.
        """
        refusal = IngestRefusal(
            rule=rule,
            detail=detail,
            at=at,
            symbol=symbol,
            underlying=underlying,
            order_id=order_id,
            activity_ids=activity_ids,
            activity_types=activity_types,
            inputs=dict(inputs or {}),
        )
        refusals.append(refusal)
        logger.warning(
            "ingestion refused: %s -- %s",
            rule.value,
            detail,
            extra={
                "event": "ingest_refusal",
                "rule": rule.value,
                "correlation_id": correlation,
                "account": self._account,
                "refusal_symbol": symbol,
                "underlying": underlying,
                "order_id": order_id,
                "at": at.isoformat(),
                "activity_ids": list(activity_ids),
                "inputs": dict(inputs or {}),
            },
        )

    # ------------------------------------------------------------------
    # fill
    # ------------------------------------------------------------------

    def _fill_values(self, ledger: Ledger) -> dict[str, dict[str, object]]:
        """The rows ``fill`` should hold, keyed by activity id.

        Built from the matcher's normalised :class:`LotMovement`\\ s, which is
        what turns two conventions into one: a ``FILL`` arrives with an
        unsigned ``qty`` and a ``side``, a non-trade row with a **signed**
        ``qty`` and no side at all, and the column stores the magnitude with
        the direction in ``side``.

        A trade activity the matcher refused for want of an **intent** still
        gets a row, with ``position_intent`` NULL. That is not a gap being
        papered over — the column is nullable for precisely this state, since
        ``buy`` is both buy-to-open and buy-to-close and the join to the order
        can fail.

        ``price`` is nullable on the same principle and for one case only: an
        ``OPEXC``/``OPASN`` whose deliverable the ledger could not verify comes
        back from :func:`~corollary.engine.ledger.build_ledger` with no price,
        because the intrinsic it would carry is derived from the strike the
        adjustment invalidated. The row is still written — the contracts left
        the book, and ``not_booked`` on the Activity page counts it — with the
        price absent rather than estimated.

        What gets **no row at all** is an activity the matcher never turned
        into a movement: an ``OPEXC`` with no settlement price for the
        underlying is refused at normalisation, so there is nothing here to
        write, and a zero would have booked it as a total loss.
        """
        values: dict[str, dict[str, object]] = {}
        for movement in ledger.movements:
            values[movement.activity_id] = {
                "account": self._account,
                "activity_id": movement.activity_id,
                "order_id": movement.order_id,
                "group_id": movement.group_id,
                "symbol": movement.symbol,
                "side": movement.side.value,
                "position_intent": movement.intent.value,
                "qty": movement.qty,
                "price": movement.price,
                "at": movement.at,
            }

        for activity in self._history.values():
            if not isinstance(activity, TradeActivity):
                continue
            if activity.id in values:
                continue
            if activity.activity_type not in FILL_ACTIVITY_TYPES:
                continue
            try:
                contract = parse_occ_symbol(activity.symbol)
            except ValueError:
                continue
            quantity = activity.quantity
            if quantity <= 0 or quantity != quantity.to_integral_value():
                continue
            values[activity.id] = {
                "account": self._account,
                "activity_id": activity.id,
                "order_id": activity.order_id,
                "group_id": None,
                "symbol": contract.symbol,
                "side": activity.side.value,
                "position_intent": None,
                "qty": int(quantity),
                "price": activity.price,
                "at": activity.transaction_time,
            }
        return values

    def _write_fills(self, session: Session, ledger: Ledger) -> tuple[int, int]:
        """Upsert on ``activity_id``, which is the whole idempotency mechanism.

        An existing row is **updated** rather than left alone, because a value
        can legitimately improve between runs: a fill whose order had not yet
        appeared carries a NULL ``position_intent``, and the run that finally
        joins it should fill that in rather than leaving the table permanently
        less certain than the process is.
        """
        desired = self._fill_values(ledger)
        existing = {
            row.activity_id: row
            for row in session.scalars(
                select(Fill).where(Fill.account == self._account)
            )
        }
        written = 0
        updated = 0
        for activity_id, values in desired.items():
            row = existing.get(activity_id)
            if row is None:
                session.add(Fill(**values))
                written += 1
                continue
            changed = False
            for column, value in values.items():
                if getattr(row, column) != value:
                    setattr(row, column, value)
                    changed = True
            if changed:
                updated += 1
        session.flush()
        return written, updated

    # ------------------------------------------------------------------
    # realized_trade
    # ------------------------------------------------------------------

    def _write_trades(
        self, session: Session, ledger: Ledger, guarded: set[str]
    ) -> tuple[int, int]:
        """Reconcile the table against the rebuilt ledger, in Python.

        Decision 11: ``pnl`` and ``pnl_pct`` are ``Money``, which raises on
        ``SUM``, ``AVG``, ``MIN``, ``MAX``, ``ORDER BY`` and every comparison
        operator, so nothing here asks SQL a question about money. The rows
        are loaded and compared as ``Decimal``.

        A **multiset** rather than a set: two identical slices are possible in
        principle — same symbol, same instants, same prices — and collapsing
        them would quietly halve a realized figure.

        ``guarded`` holds every symbol this run could not fully account for,
        and its rows are never deleted. The ledger is rebuilt from the whole
        history each run, so a run that cannot state a P&L simply produces
        fewer trades; deleting the difference would make a provider outage
        indistinguishable from a busted fill, and would erase lifetime P&L for
        a reason that heals on the next run.
        """
        existing = list(
            session.scalars(
                select(RealizedTrade).where(RealizedTrade.account == self._account)
            )
        )
        held: dict[_TradeKey, list[RealizedTrade]] = {}
        for row in existing:
            held.setdefault(_row_key(row), []).append(row)

        wanted: Counter[_TradeKey] = Counter()
        records: dict[_TradeKey, RealizedTradeRecord] = {}
        for record in ledger.trades:
            key = _record_key(record)
            wanted[key] += 1
            records[key] = record

        written = 0
        for key, count in wanted.items():
            for _ in range(count - len(held.get(key, []))):
                session.add(RealizedTrade(**_trade_row_kwargs(records[key])))
                written += 1

        removed = 0
        for key, rows in held.items():
            keep = wanted.get(key, 0)
            for row in rows[keep:]:
                if row.symbol in guarded:
                    continue
                session.delete(row)
                removed += 1

        session.flush()
        return written, removed

    # ------------------------------------------------------------------
    # mleg_group / mleg_leg
    # ------------------------------------------------------------------

    def _write_mleg(
        self,
        session: Session,
        orders: Iterable[Order],
        refusals: list[IngestRefusal],
        correlation: str,
    ) -> tuple[int, int]:
        """Write the ``mleg`` order history as grouping evidence.

        A group here **proposes** a logical position; it does not assert one
        still exists. Liveness — every leg still held, on the expected side,
        in quantities consistent with the ratios — is a question about today's
        positions and is answered by ``grouping.group_positions``, not stored.
        So an unfilled or cancelled ``mleg`` order is recorded too: it is
        still the evidence of what was proposed, and the grouper declines it
        with ``NOT_FILLED`` on its own terms.

        ``net_price`` is written **signed**, because the sign *is* the
        direction: debit long, credit short. Taking an absolute value anywhere
        on this path inverts every credit spread's risk class.
        """
        groups = {
            row.order_id: row
            for row in session.scalars(
                select(MlegGroup).where(MlegGroup.account == self._account)
            )
        }
        written = 0
        legs_written = 0

        for order in orders:
            if order.order_class is not OrderClass.MLEG:
                continue
            at = order.filled_at or order.created_at
            if not order.legs:
                self._refuse(
                    refusals,
                    correlation,
                    IngestRule.MLEG_ORDER_HAS_NO_LEGS,
                    "an mleg order carries no legs[]. Either this history was "
                    "fetched without ?nested=true -- in which case the join is "
                    "one-hop and nothing will ever group -- or this row is a "
                    "leg rather than a parent",
                    at=at,
                    order_id=order.id,
                    inputs={"status": order.status, "leg_count": "0"},
                )
                continue

            legs = _leg_values(order)
            if legs is None:
                self._refuse(
                    refusals,
                    correlation,
                    IngestRule.MLEG_LEG_NOT_WRITABLE,
                    "a leg carries no position_intent, or a ratio_qty that is "
                    "absent, zero, negative or fractional. Ratios arrive in "
                    "simplest form, so a fractional one is not a spread but a "
                    "parse error, and mleg_leg refuses all of them at the "
                    "column",
                    at=at,
                    order_id=order.id,
                    inputs={
                        "status": order.status,
                        "leg_count": str(len(order.legs)),
                    },
                )
                continue

            group = groups.get(order.id)
            if group is None:
                group = MlegGroup(
                    account=self._account,
                    order_id=order.id,
                    opened_at=at,
                    net_price=order.filled_avg_price,
                )
                session.add(group)
                written += 1
            else:
                group.opened_at = at
                group.net_price = order.filled_avg_price

            legs_written += _sync_legs(group, legs)

        session.flush()
        return written, legs_written


    # ------------------------------------------------------------------
    # ledger_rejection
    # ------------------------------------------------------------------

    def _examined_symbols(self) -> set[str]:
        """Every contract this pass has activity-level information about.

        The held history, and deliberately **not** the orders' symbols as
        well: this set is what licenses a *delete*, so it names only what the
        pass could actually have re-derived a refusal from.

        Canonicalised on the way in, because the other side of the comparison
        is: see :func:`_canonical_symbol`. This side is whatever the vendor
        sent. The stored side is canonical because :meth:`_rejection_values`
        folds it on the way in -- **not** because every matcher rule supplies
        the parser's form. Four do not: ``FRACTIONAL_QUANTITY``,
        ``INTENT_CONTRADICTS_SIDE``, ``UNKNOWN_INTENT`` and ``MISSING_SYMBOL``
        reject before a contract is resolved and carry ``activity.symbol``
        verbatim. The conclusion holds, but the fold is the reason for it, and
        a guard justified by a premise that is not true is a guard someone
        deletes later.
        """
        symbols: set[str] = set()
        for activity in self._history.values():
            canonical = _canonical_symbol(activity.symbol)
            if canonical is not None:
                symbols.add(canonical)
        return symbols

    def _write_rejections(
        self,
        session: Session,
        rejections: Sequence[LedgerRejection],
        refusals: Sequence[IngestRefusal],
        *,
        correlation: str,
        at: datetime,
        examined_activities: set[str],
        examined_orders: set[str],
        examined_symbols: set[str],
    ) -> tuple[int, int]:
        """Reconcile ``ledger_rejection`` against what *this* pass refused.

        Decision 14 persists the refusals so a gap in lifetime P&L can state
        its cause after the process that found it has gone. The hazard that
        creates is staleness: an in-memory refusal is rebuilt from current
        logic every pass and cannot go stale, a stored one can, and a row
        claiming a gap the matcher no longer has is a confident wrong reason
        on a money figure. So this reconciles rather than appends.

        Three rules, and the third is the one that makes the table honest:

        * **Upsert on the fingerprint.** Ingestion is idempotent on
          ``activity_id`` and its refusals are idempotent the same way, so a
          second identical pass leaves the same rows rather than a second copy
          of them. ``at`` and ``correlation_id`` are refreshed on the way
          through, because a row is a statement about the latest pass that
          made it, not about the first.
        * **Do not store the by-design declines.** ``BY_DESIGN_RULES`` -- a
          cash journal is not a fill, a stock trade is not an option -- are
          expected on every pass and are not missing trades. They are still
          logged and still in :attr:`IngestResult.rejections`; what they are
          not is rows, because nineteen of them a pass would drown the count.
        * **Clear what this pass re-examined and no longer refuses**, and
          nothing else. ``_re_examined`` is deliberately narrow: a pass that
          pulled ten activities may not delete a refusal about an eleventh it
          never saw, and a pass that saw nothing but NVDA may not delete a
          refusal about AAPL -- a symbol is a subject, not decoration. That is a guard against where the cursor is *going* --
          ``self._cursor`` is in memory today, so every pass re-derives
          everything, but the ``fill`` table exists precisely because
          re-fetching all history per request is untenable, and on the day the
          cursor is persisted a wholesale delete here would erase every
          refusal the pass did not happen to re-derive.

        A fourth thing, which is a *non*-write: a refusal naming no subject at
        all is skipped, with a WARNING so rule 8 still holds. See
        :func:`_identity`.

        **Where this over-states, and why that is the direction chosen.** A
        rule flip plus a shrinking history can leave two rows for one gap. If
        pass 1 stores ``contract_terms_unavailable`` for a symbol with N
        activity ids and pass 2 refuses the same symbol under
        ``contract_terms_fetch_failed``, the rule is part of the fingerprint,
        so the second refusal is a **new row** -- and if pass 2 also holds
        *fewer* activities than pass 1 recorded, ``_re_examined`` correctly
        declines to clear the first. The Activity page then reports two
        missing trades where there is one. It takes the vendor's activity
        window shrinking across a restart, which is why this is written down
        rather than coded around: the error is an **over**-statement, and this
        design prefers over-stating a gap to erasing a stated one. A duplicate
        row is visible and reconcilable against ``fill``; a deleted row is a
        gap with no stated cause, which is the state decision 14 exists to get
        out of.

        Returns ``(written, cleared)``.
        """
        desired: dict[str, dict[str, Any]] = {}
        for rejection in rejections:
            if rejection.rule in BY_DESIGN_RULES:
                continue
            named = (rejection.activity_id,) if rejection.activity_id else ()
            # Through `_canonical_symbol`, so the gate and the value it admits
            # read the same string rather than agreeing by the vendor's
            # habits. They part on exactly one class of input: whitespace-only,
            # which `_identity` counts as a subject and `_rejection_values`
            # folds to NULL. A refusal admitted on a subject its row does not
            # carry is stored **subject-less**, and every subject-less refusal
            # of one rule digests identically -- two genuinely different gaps
            # collapse into one row and the count under-states, which is the
            # one direction of error nothing about the row reveals.
            if not _identity(
                "ledger", named, None, _canonical_symbol(rejection.symbol)
            ):
                self._unattributable(
                    "ledger", rejection.rule.value, rejection.inputs, correlation
                )
                continue
            values = self._rejection_values(
                source="ledger",
                rule=rejection.rule.value,
                symbol=rejection.symbol,
                order_id=None,
                activity_ids=(
                    (rejection.activity_id,) if rejection.activity_id else ()
                ),
                detail=rejection.detail,
                inputs=rejection.inputs,
                activity_at=rejection.at,
                at=at,
                correlation=correlation,
            )
            desired[str(values["fingerprint"])] = values
        for refusal in refusals:
            # Canonicalised for the reason given on the ledger gate above.
            if not _identity(
                "ingest",
                refusal.activity_ids,
                refusal.order_id,
                _canonical_symbol(refusal.symbol),
            ):
                self._unattributable(
                    "ingest", refusal.rule.value, refusal.inputs, correlation
                )
                continue
            values = self._rejection_values(
                source="ingest",
                rule=refusal.rule.value,
                symbol=refusal.symbol,
                order_id=refusal.order_id,
                activity_ids=refusal.activity_ids,
                detail=refusal.detail,
                inputs=refusal.inputs,
                activity_at=refusal.at,
                at=at,
                correlation=correlation,
            )
            desired[str(values["fingerprint"])] = values

        held = list(
            session.scalars(
                select(RejectionRecord).where(
                    RejectionRecord.account == self._account
                )
            )
        )
        cleared = 0
        for row in held:
            # A fresh name rather than reusing `values`: mypy binds `pop`'s
            # overload from the target's declared type, and the one above is
            # already a plain dict.
            reasserted: dict[str, Any] | None = desired.pop(row.fingerprint, None)
            if reasserted is None:
                if _re_examined(
                    row, examined_activities, examined_orders, examined_symbols
                ):
                    session.delete(row)
                    cleared += 1
                continue
            for column, value in reasserted.items():
                if getattr(row, column) != value:
                    setattr(row, column, value)

        written = 0
        for values in desired.values():
            # ``first_seen`` is set here and nowhere else. The update loop
            # above refreshes ``at`` -- *still true as of* -- and a row that
            # refreshed both could not say how long its gap has persisted,
            # which is what makes a stale row indistinguishable from a fresh
            # one. A row is deleted only when the pass did *not* re-derive its
            # fingerprint, so nothing inserted here can carry a deleted row's
            # key: the unit of work emits every INSERT before any DELETE, and
            # a collision would be an IntegrityError rolling back the whole
            # pass, ``fill`` rows included.
            session.add(RejectionRecord(**values, first_seen=values["at"]))
            written += 1

        session.flush()
        if written or cleared:
            logger.info(
                "ingest recorded %d refusals and cleared %d",
                written,
                cleared,
                extra={
                    "event": "ingest_rejections_written",
                    "correlation_id": correlation,
                    "account": self._account,
                    "rejections_written": written,
                    "rejections_cleared": cleared,
                },
            )
        return written, cleared

    def _rejection_values(
        self,
        *,
        source: str,
        rule: str,
        symbol: str | None,
        order_id: str | None,
        activity_ids: Sequence[str],
        detail: str,
        inputs: Mapping[str, str],
        activity_at: datetime | None,
        at: datetime,
        correlation: str,
    ) -> dict[str, Any]:
        """One row's columns, de-identified on the way in.

        Rule 6 reaches a persistence boundary here. Alpaca embeds the account
        number in prose -- *"CAT fee for proceed of 15 trades on <date> by
        PA..."* -- which a field-name redactor cannot see inside, so
        ``vendor_detail``'s substring pass runs on ``detail`` and on every
        input value **before** they reach the database rather than on the way
        out: a redactor on the read path leaves the row itself holding the
        number. The same call bounds the length, which a stored string wants
        anyway; the unabridged text is in the log record this refusal already
        emitted, under the same ``correlation_id``.

        The bound is ``STORED_DETAIL_MAX`` and **not** the log's
        ``ERROR_BODY_MAX``. One scrubber, two limits: redaction still runs
        before truncation, so the longer cut is no weaker, but a stored
        refusal is mostly our own prose and its closing clause is the severity
        qualifier -- at 300 characters the sentence saying *the position ended
        and its P&L is permanently gone* was cut deterministically whenever a
        fetch failure was present, leaving the row saying only that a
        multiplier is unknown.

        **The credentials are substituted here, before the cut, and the
        ordering is the point.** ``self._secrets`` goes to both calls below,
        so ``vendor_detail`` replaces a key and *then* truncates. The other
        order -- truncate here, redact on the read path -- reads as
        equivalent and is not: a credential straddling ``STORED_DETAIL_MAX``
        is cut in half in the committed row, redaction is literal
        substitution, so the later pass matches nothing and the surviving
        prefix is served. The audit of step 8c-2 demonstrated exactly that,
        with the first ten characters of a key reaching the response, and it
        is reachable from ordinary prose: :meth:`_refuse_terms` writes some
        366 characters of our own sentence and then appends *"The contracts
        endpoint did not answer: {failure}"*, so a 600-character vendor body
        puts the tail of that answer past offset 1024.

        ``api/routes/activity.py`` redacts again on the way out, against the
        API process's own environment. That is defence in depth for a row
        written by a process holding fewer credentials than the reader holds,
        and it is **not** a substitute for this pass, because no substitution
        can re-join an identifier already cut.

        **Three limits of this boundary, stated where they bite.**

        * ``secrets`` defaults to empty, and a service built that way
          redacts against the account-number shape alone. The constructor
          says why the default exists; what it means here is that "redacted"
          is a property of the *caller*, not of this method.
        * ``wire._ACCOUNT_NUMBER`` matches an Alpaca **paper** account number
          (``PA`` plus ten characters). A live account number is bare digits
          and is deliberately uncovered -- no digit-run rule can tell one from
          a quantity, a price or an epoch. ``self._account`` may be ``cash``,
          and on that book this pass is uncovered. Nothing here creates that
          gap, but this is where a log line that rotates away becomes a
          committed row that does not.
        * ``inputs`` carries **stringified money** on several rules --
          ``strike``, ``net_amount``, ``paired_price``, ``multiplier``. There
          is no money *column* here, but there is money in the row, as TEXT
          inside a JSON blob where neither ``Money``'s refusing comparator nor
          ``guard_money_sql`` can see it. A future
          ``WHERE json_extract(inputs, '$.strike') > ...`` gets SQLite's
          lexicographic answer with no raise. Load the row and compare as
          ``Decimal`` in Python.

        ``at`` is when the refusal was recorded and ``activity_at`` is the
        vendor's own stamp, which may be absent -- an activity with no usable
        timestamp is itself something the matcher refuses, so rule 8's
        timestamp cannot be the vendor's.
        """
        ids = sorted(activity_ids)
        # A subject, or nothing -- never an empty string, and never two
        # spellings of one contract. ``_identity`` tests both of these for
        # truthiness, so ``""`` already contributes nothing to the
        # fingerprint; folding it here is what makes the **column** mean
        # "NULL is no subject" rather than agree with the digest by luck.
        # Alpaca really does send ``symbol: ""`` on structure rows, and
        # ``MISSING_SYMBOL`` passes it straight through.
        symbol = _canonical_symbol(symbol)
        order_id = order_id or None
        return {
            "account": self._account,
            "source": source,
            "rule": rule,
            "fingerprint": _fingerprint(source, rule, ids, order_id, symbol),
            "symbol": symbol,
            "order_id": order_id,
            "activity_ids": ids,
            # ``secrets`` on both, because both are free text and both are
            # truncated here. Redaction happens inside ``vendor_detail`` and
            # therefore before its cut -- the ordering is the whole fix; see
            # this method's docstring.
            "detail": vendor_detail(
                detail, secrets=self._secrets, limit=STORED_DETAIL_MAX
            ),
            "inputs": {
                key: vendor_detail(
                    value, secrets=self._secrets, limit=STORED_DETAIL_MAX
                )
                for key, value in inputs.items()
            },
            "at": at,
            "activity_at": activity_at,
            "correlation_id": correlation,
        }

    def _unattributable(
        self,
        source: str,
        rule: str,
        inputs: Mapping[str, str],
        correlation: str,
    ) -> None:
        """A refusal that named no subject: logged, never stored.

        Rule 8 is satisfied here rather than by a row -- the rule, the inputs
        and the timestamp all reach the log. What cannot be satisfied is the
        *reader*: see :func:`_identity` for why a row nobody can attribute to
        a contract would make the count worse rather than better.
        """
        logger.warning(
            "ingest refusal has no subject and cannot be stored: %s/%s",
            source,
            rule,
            extra={
                "event": "ingest_rejection_unattributable",
                "correlation_id": correlation,
                "account": self._account,
                "source": source,
                "rule": rule,
                "inputs": dict(inputs),
            },
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _identity(
    source: str,
    activity_ids: Sequence[str],
    order_id: str | None,
    symbol: str | None,
) -> tuple[str, ...]:
    """What a refusal is *about*, as distinct from the evidence it names.

    The distinction is the whole of this function, and getting it wrong
    double-counts money. An **ingest** refusal lists every held activity for
    the symbol it refuses -- and that list is a property of how much history
    the pass happened to pull, not of the refusal. Hash it and the key moves
    between passes: pass two over a shorter history derives a *different*
    fingerprint, so the upsert misses, the old row is not cleared (its
    activities were not re-examined, correctly) and a second row lands beside
    it. ``UNIQUE (account, fingerprint)`` cannot catch that, because the two
    keys differ by construction. The Activity page then reports two missing
    trades where there is one.

    So for an ingest refusal the identity is the **contract**, or the order on
    the ``mleg`` rules -- the thing the refusal concerns. The activity ids are
    evidence; they are stored in the row, where a reader can see them, and
    kept out of the digest.

    A **ledger** rejection is the other shape: it names exactly one activity,
    and that activity *is* the subject. Two rejections of one rule about two
    different fills are two missing trades, not one, so the id stays in.

    An empty result means the refusal names no subject at all, and the writer
    skips it rather than storing it. That is not tidiness: every subject-less
    refusal of a given rule digests identically, so two genuinely different
    gaps would collapse into one row and the count would *under*-state -- the
    one direction of error nothing about the row would reveal. It is logged
    instead, with its rule and its inputs, so rule 8 still holds.
    """
    subjects: list[str] = []
    if source != "ingest":
        subjects.extend(f"activity:{value}" for value in sorted(activity_ids))
    if order_id:
        subjects.append(f"order:{order_id}")
    if symbol:
        subjects.append(f"symbol:{symbol}")
    return tuple(subjects)


def _canonical_symbol(symbol: str | None) -> str | None:
    """One symbol's storable form: stripped, upper-cased, ``""`` to ``None``.

    **The canonical form is the parser's.** ``parse_occ_symbol`` returns
    ``symbol.strip().upper()``, and a matcher rejection raised *after* the
    contract is resolved carries that form via ``contract.symbol``. Seven are
    raised before it and do not: ``FRACTIONAL_QUANTITY`` (the fill path's),
    ``INTENT_CONTRADICTS_SIDE``, ``UNKNOWN_INTENT``, ``MISSING_SYMBOL``,
    ``NOT_AN_OPTION``, ``NOT_A_LEDGER_ACTIVITY`` and ``MISSING_FEE_AMOUNT``
    pass ``activity.symbol`` through verbatim. The last one is complete only
    because Alpaca's ``FEE`` rows carry no symbol; this fold is what keeps a
    fee that one day does from comparing in a second spelling. Ingestion's own refusals name
    the vendor's spelling, and ``examined_symbols`` is built from the vendor's
    spelling too, so today the forms agree by **observation** --
    Alpaca sends canonical uppercase -- rather than by construction. On the day
    they do not, a row written in one form and compared against the other can
    never be cleared, and it states a gap that is not there for as long as the
    database lives. That is the same failure an empty-string symbol caused,
    arriving by a different route, so it is closed the same way: compare
    normalised forms on both sides rather than trust the vendor's casing.

    ``""`` folds to ``None`` because an empty string is not a subject.
    :func:`_identity` already treats it as none at all, and a column
    disagreeing with the digest about what a row is about is that same
    stranding by the shorter route.
    """
    if symbol is None:
        return None
    return symbol.strip().upper() or None


def _fingerprint(
    source: str,
    rule: str,
    activity_ids: Sequence[str],
    order_id: str | None,
    symbol: str | None,
) -> str:
    """The identity of one refusal: its rule, and the subjects it is about.

    Everything hashed here is stored in the row beside it, so the digest is
    derived rather than authored and nothing depends on being able to read it
    back. It is a digest instead of the readable join because that join can
    grow -- a ledger rejection carries an activity id per row -- and a unique
    index should not have to.

    The subjects are ordered, not merely concatenated: two passes deriving the
    same refusal must land on the same fingerprint, or the upsert becomes an
    append and the table double-counts the gap it is reporting. *Which*
    subjects count is :func:`_identity`, and is the part that is easy to get
    wrong.
    """
    subjects = _identity(source, activity_ids, order_id, symbol)
    payload = "\n".join([source, rule, *subjects])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _re_examined(
    row: RejectionRecord,
    examined_activities: set[str],
    examined_orders: set[str],
    examined_symbols: set[str],
) -> bool:
    """Whether this pass looked at everything the row is about.

    Only then may a row that the pass did not re-derive be deleted. A refusal
    the pass never re-examined is not disproved by that pass's silence, and
    deleting it would turn a stated gap back into an unexplained one -- which
    is the state decision 14 exists to get out of.

    All three of ``activity_ids``, ``order_id`` and ``symbol`` are subjects,
    and the third is the one that reads like decoration and is not:
    :func:`_identity` builds an ingest fingerprint out of the symbol alone, so
    a row can name a symbol and nothing else. Treated as subject-less it would
    be *vacuously* re-examined -- deletable by any pass at all, including one
    whose entire history is a different underlying. That is one step away from
    ordinary: ``LedgerRejection.activity_id`` defaults to ``None``, so the
    first whole-symbol matcher refusal lands in exactly this shape.

    **Truthiness, not ``is not None``, and the gap between them is permanent.**
    These predicates have to mean by ``symbol`` and ``order_id`` exactly what
    :func:`_identity` means by them, because the fingerprint is built from that
    reading: ``""`` is no subject there. Tested for ``is not None`` instead, a
    row carrying ``symbol=""`` failed this check on **every** pass, forever --
    ``examined_symbols`` holds truthy symbols and can never contain it -- so
    the refusal stopped being derived, the row survived anyway, and the table
    went on claiming lifetime P&L was short a trade that had in fact been
    booked, with ``at`` frozen at the last pass that asserted it. That is
    decision 14's own *worse than no table at all*, and it was reachable:
    Alpaca sends ``symbol: ""`` on structure rows, and ``MISSING_SYMBOL`` --
    which fires exactly when the symbol is empty, and is not by-design -- is
    stored with what it was given. ``_rejection_values`` now folds it to
    ``NULL`` on the way in; this half also covers the rows an older build
    already wrote.

    Both sides of the symbol comparison are canonicalised for a second reason
    of the same shape -- see :func:`_canonical_symbol`.
    """
    if any(value not in examined_activities for value in row.activity_ids):
        return False
    if row.order_id and row.order_id not in examined_orders:
        return False
    symbol = _canonical_symbol(row.symbol)
    if symbol and symbol not in examined_symbols:
        return False
    return True


def _occ_root(occ: OccSymbol) -> str:
    return occ.root


def _stripped_root(occ: OccSymbol) -> str:
    """The OCC root with its adjusted-contract suffix removed.

    Only ever used as a *query key*, and only after the verbatim root has
    already come back empty: the answer is matched by exact symbol before
    anything is cached, so a wrong guess finds nothing rather than the wrong
    terms.
    """
    return occ.root.rstrip(_DIGITS) or occ.root


#: The four contract-terms queries, in the order they are tried. See
#: :meth:`IngestService._ensure_terms` for why this order and not another.
_TERM_QUERIES: Final[
    tuple[tuple[Callable[[OccSymbol], str], ContractStatus], ...]
] = (
    (_occ_root, ContractStatus.ACTIVE),
    (_stripped_root, ContractStatus.ACTIVE),
    (_occ_root, ContractStatus.INACTIVE),
    (_stripped_root, ContractStatus.INACTIVE),
)


def _session_midnight(day: date) -> datetime:
    """Midnight **Eastern** on ``day``, as an aware UTC instant.

    Eastern because market data is Eastern and the server clock is whatever
    the machine says; UTC because that is what crosses the provider boundary.
    Building this from a naive ``datetime`` and letting the platform supply a
    zone is the silent multi-hour error CLAUDE.md warns about, and on a
    one-day window it is enough to move which session the bar belongs to.
    """
    return datetime.combine(day, time(0), tzinfo=NYSE_TZ).astimezone(timezone.utc)


def _session_close(bars: Iterable[Bar], session: date) -> Decimal | None:
    """The close of the daily bar whose **Eastern** session date is ``session``.

    Found, not chosen: the bar is matched on its own date rather than taken as
    the first or the last of the window, so widening the window can never
    change the answer. A window with no bar for that session returns ``None``
    — a holiday, a halt, or a date the vendor has no data for, and all three
    mean the same thing to the caller, which is *refuse*.
    """
    for bar in bars:
        if bar.at.astimezone(NYSE_TZ).date() == session:
            return bar.close
    return None


def _trade_row_kwargs(record: RealizedTradeRecord) -> dict[str, object]:
    """``realized_trade`` column arguments, with ``close_kind`` as a plain str.

    :func:`~corollary.engine.ledger.as_row_kwargs` mirrors the record column
    for column, which is what makes a column added to one and not the other a
    test failure. The one conversion is ``close_kind``: it arrives as a
    :class:`~corollary.engine.ledger.CloseKind`, and storing the member rather
    than its value would put an enum through the DBAPI for no reason.
    """
    values = as_row_kwargs(record)
    values["close_kind"] = record.close_kind.value
    return values


def _leg_values(order: Order) -> list[tuple[str, int, str, str]] | None:
    """``(symbol, ratio, side, position_intent)`` per leg, or ``None`` if any
    leg cannot be written.

    All-or-nothing on purpose: half a spread recorded as grouping evidence is
    worse than none, because the grouper would then propose a structure with a
    leg missing and the risk class would be wrong in the undefined-risk
    direction.
    """
    values: list[tuple[str, int, str, str]] = []
    for leg in order.legs:
        ratio = leg.ratio_qty
        if ratio is None or ratio <= 0 or ratio != ratio.to_integral_value():
            return None
        if leg.position_intent is None or leg.side is None:
            return None
        values.append(
            (leg.symbol, int(ratio), leg.side.value, leg.position_intent.value)
        )
    return values


def _sync_legs(group: MlegGroup, legs: list[tuple[str, int, str, str]]) -> int:
    """Bring one group's legs into line, in place. Returns how many were added.

    In place rather than by replacing the collection: ``(group_id, symbol)``
    is the primary key, so deleting and re-inserting the same leg in one flush
    risks the insert landing before the delete. It would also make a
    no-op run churn every row for nothing.
    """
    current = {leg.symbol: leg for leg in group.legs}
    added = 0
    for symbol, ratio, side, intent in legs:
        leg = current.pop(symbol, None)
        if leg is None:
            group.legs.append(
                MlegLeg(
                    symbol=symbol,
                    ratio=ratio,
                    side=side,
                    position_intent=intent,
                )
            )
            added += 1
            continue
        leg.ratio = ratio
        leg.side = side
        leg.position_intent = intent
    for orphan in current.values():
        group.legs.remove(orphan)
    return added
