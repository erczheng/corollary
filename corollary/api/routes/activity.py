"""The ledger surface: the realized-trade table, and the header cards above it.

Everything on this page is **Corollary's arithmetic, not Alpaca's.** There is
no realized-P&L field on any endpoint the broker publishes -- no P&L on an
activity, no ``position_intent`` on a fill -- so lifetime P&L, average win,
average loss and every per-row figure here is folded from the account's own
``fill`` and ``realized_trade`` rows. That is what makes this the one place in
Phase 2 where a wrong number is not a wrong *display* of something true.

Two rules decide the whole module.

**Decision 11 -- the cards fold every row, the table pages.**
``realized_trade.pnl`` is a :class:`~corollary.db.types.Money` column, which
is TEXT on SQLite, so ``SUM``, ``AVG``, ``MIN``, ``MAX``, ``ORDER BY`` and
every comparison over it raise ``MoneyComparisonError`` rather than answering
lexicographically -- against 7, 20, 8, 25 and 40 the database calls ``MAX``
eight. So :func:`ledger_stats` loads **all** of one book's realized trades and
folds them in Python, via ``engine/ledger.py``'s own
:func:`~corollary.engine.ledger.summarise`, and the *table* is what paginates.
A capped window would make four figures labelled "lifetime" mean something
narrower than the word. Nothing in this file asks SQL a question about money;
``tests/api/test_activity_routes.py`` pins that structurally, because the bug
is silent when it lands.

**Open question 4, resolved -- refuse, and state the gap.** An adjusted root
reaching an ``OPEXC``/``OPASN`` branch books no realized trade: a real
``GME1`` delivers 100 GME *plus* 10 GME.WS while ``multiplier`` and ``size``
both report ``100``, and no field on the contract says what the dollars are.
The contracts still left the book, so the lifetime figure is genuinely short
one trade. **Its Price cell is empty for the same reason**: those settle at
intrinsic value against the OCC strike, and the adjustment is precisely the
finding that the strike is no longer what the deliverable changes hands at, so
``fill.price`` is NULL and the row shows no price rather than an estimate in a
column of prices paid. :attr:`ActivityStats.not_booked` is that count, and
:attr:`ActivityStats.not_booked_symbols` names the contracts so it can be
reconciled by hand. **A known-incomplete figure, never a quietly wrong one** --
and at Phase 6 this stops being a display question, because
``max_daily_loss_pct`` is enforced against realized P&L and a phantom gain
suppresses a halt that should have fired.

What the count can and cannot say
---------------------------------

:func:`unbooked_closes` counts *closings the realized-trade table does not
fully account for* -- the test is on quantity, so a wholly refused close and a
partly matched one both land in it. That is an arithmetic fact about the two
tables and needs no guess. It does **not** assert each one's cause.
``RejectionRule`` is logged by
``engine/ledger.py`` and no Phase 2 table stores it, and the adjusted
deliverable is the case this count exists for rather than provably the only
thing that can produce it. Attributing the reason with authority wants the
matcher's refusals persisted -- a table this dispatch does not own. Guessing
instead, from ``deliverables``, ``allocation_percentage``, ``size`` or a split
factor, is exactly what open question 4 forbade.

Three smaller things that are easy to get backwards
---------------------------------------------------

**``side`` has three values, not two.** ``sell_short`` opens a short (STO) and
``sell`` closes a long (STC), so those two decide their own action. ``buy`` is
*both* BTO and BTC, and only the order's ``position_intent`` separates them --
which is why ``fill.position_intent`` is a column rather than something
derived. A ``buy`` whose join never landed is **refused and logged**, never
guessed: calling it BTO reports a position opened where one may have been
closed.

**A close that consumed two lots is two ``realized_trade`` rows.** The dollar
figures add. The percentages do not -- the slices have different bases -- so
:func:`_weighted_pct` weights by ``open_price x qty``. The multiplier is
common to every slice of one contract and cancels out of the ratio, which is
what lets an honest percentage be stated from stored columns alone, with no
multiplier to look up and none to get wrong.

**The broker dependency is the account boundary, not a call.** These rows come
from SQLite -- that is what ``fill`` exists for, since ``page_size`` maxes at
100 and re-fetching all history per request is untenable. :data:`BrokerDep` is
here because a book with no credentials must be **refused with a stated
reason, never substituted and never served as an honest-looking empty
ledger**. It is also what keeps CLAUDE.md rule 1 structural: the type a route
can name is ``BrokerAccount``, the read half, so ``submit_order`` is not
reachable from here even by accident.
"""

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Query
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from corollary.api.deps import AccountModeDep, BrokerDep, SessionDep
from corollary.api.schemas import (
    AccountMode,
    ActivityAction,
    ActivityItem,
    ActivityStats,
    ActivityStatus,
    Page,
)
from corollary.db.models import Fill, RealizedTrade
from corollary.engine.ledger import PCT_QUANTUM, summarise
from corollary.instruments import OptionType, parse_occ_symbol

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "LedgerGap",
    "LedgerRow",
    "MAX_PAGE_SIZE",
    "activity_items",
    "contract_label",
    "fills_query",
    "ledger_rows",
    "ledger_stats",
    "router",
    "trades_query",
    "unbooked_closes",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/activity", tags=["activity"])

#: Matches ``Activity.tsx``'s own ``PAGE_SIZE``, so the first request a
#: migrated page makes asks for the rows it is about to render.
DEFAULT_PAGE_SIZE: Final = 15

#: A ceiling rather than a guard against load -- this is a single-user
#: account. It exists so a client cannot turn the table endpoint into the
#: whole-history endpoint by accident and then fold a "lifetime" figure out
#: of it, which is the one thing decision 11 says the table must not be used
#: for.
MAX_PAGE_SIZE: Final = 200


# --------------------------------------------------------------------------
# Action resolution -- the order decides, never the side
# --------------------------------------------------------------------------

#: The four actions, keyed by the order's own ``position_intent``. This is the
#: authoritative map: intent states all four unambiguously.
_ACTION_FOR_INTENT: Final[Mapping[str, ActivityAction]] = {
    "buy_to_open": "BTO",
    "buy_to_close": "BTC",
    "sell_to_open": "STO",
    "sell_to_close": "STC",
}

#: The two ``side`` values that decide an action on their own, used only when
#: the fill-to-order join never landed. ``buy`` is deliberately absent: it is
#: BTO *and* BTC, and a map with three entries here is how a buy-to-close
#: becomes an opening row.
_ACTION_FOR_SIDE: Final[Mapping[str, ActivityAction]] = {
    "sell_short": "STO",
    "sell": "STC",
}

#: The two actions that can realize a P&L. An open realizes nothing, so there
#: is nothing for it to be missing either -- which is what keeps an opening
#: fill out of :func:`unbooked_closes`.
_CLOSING_ACTIONS: Final[frozenset[str]] = frozenset({"STC", "BTC"})

#: Every stored row is a fill that happened. Working orders, cancellations and
#: rejections have no ``fill`` row at all -- they are the positions route's
#: surface -- so the status filter is real and, in Phase 2, only ``filled``
#: ever matches.
_FILL_STATUS: Final[ActivityStatus] = "filled"

_MONTHS: Final[tuple[str, ...]] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _action_for(fill: Fill) -> ActivityAction | None:
    """BTO, STC, STO or BTC -- or ``None`` where the row cannot say.

    Pure and silent. The refusal is logged by :func:`ledger_rows`, which is
    the point at which a row actually disappears from the page.
    """
    if fill.position_intent is not None:
        return _ACTION_FOR_INTENT.get(fill.position_intent)
    return _ACTION_FOR_SIDE.get(fill.side)


def _strike(value: Decimal) -> str:
    """``280`` and ``280.5``, never ``280.00`` and never ``28``.

    The right-strip is guarded on the point being there at all: an OCC strike
    of ``230`` formats as ``"230"`` with no fractional part, and stripping
    zeros off that would print ``23``.
    """
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def contract_label(symbol: str) -> str:
    """``IWM260918P00280000`` -> ``IWM $280 Put Sep 18``.

    An OCC symbol is a key; a table cell is a label. The expiry is a **date**
    and is rendered as one -- there is no instant here to put in a timezone,
    which is the same trap ``formatExpiry`` documents on the frontend.

    An unparseable symbol is returned as it arrived rather than replaced with
    a placeholder: it is still the truest thing available about that row, and
    a row that says ``--`` where a contract belongs is a row nobody can trace.
    """
    try:
        occ = parse_occ_symbol(symbol)
    except ValueError:
        return symbol
    right = "Call" if occ.option_type is OptionType.CALL else "Put"
    month = _MONTHS[occ.expiration.month - 1]
    return f"{occ.root} ${_strike(occ.strike)} {right} {month} {occ.expiration.day}"


# --------------------------------------------------------------------------
# Matching a close to the slices it realized
# --------------------------------------------------------------------------

#: ``(symbol, closed_at, close_price)``. Every one of the three comes off the
#: closing movement itself -- ``engine/ledger.py``'s ``_realize`` sets
#: ``closed_at = closing.at`` and ``close_price = closing.price`` -- so a
#: closing ``fill`` row and the trades it produced carry the same triple.
#:
#: ``Decimal`` is safe in a key here for the reason floats are not:
#: ``Decimal('8.14') == Decimal('8.140')`` and the two hash alike, so the
#: column's canonical ``format(value, "f")`` spelling cannot split a key.
#:
#: The price is **nullable on the fill and never null on the trade**, so a key
#: whose third element is ``None`` matches nothing -- which is the right
#: answer rather than a gap. ``fill.price`` is NULL on exactly one kind of
#: row: an exercise or assignment whose deliverable the ledger could not
#: verify, which books no realized trade by the same refusal. The join finding
#: nothing and the ledger having booked nothing are the same fact.
_CloseKey = tuple[str, datetime, Decimal | None]


def _slices_by_close(
    trades: Sequence[RealizedTrade],
) -> dict[_CloseKey, list[RealizedTrade]]:
    index: dict[_CloseKey, list[RealizedTrade]] = {}
    for trade in trades:
        index.setdefault(
            (trade.symbol, trade.closed_at, trade.close_price), []
        ).append(trade)
    return index


def _weighted_pct(slices: Sequence[RealizedTrade]) -> Decimal | None:
    """Return on cost basis across every slice one close realized.

    A mean of the slices' percentages would be wrong whenever their bases
    differ: one contract at 2.00 closed for 5.00 is +150%, three at 4.00
    closed for the same 5.00 is +25%, and the trade as a whole made 600 on
    1400 of basis -- 42.86%, not 87.5%.

    The weight is ``open_price x qty`` rather than the true basis
    ``open_price x qty x multiplier``, and that is exact rather than an
    approximation: the multiplier is a property of the contract, so it is the
    same on every slice of one close and cancels out of a ratio of sums. Which
    is just as well, because ``realized_trade`` does not store it -- and
    reconstructing one would be precisely the arithmetic-from-inference this
    module refuses elsewhere.

    ``None`` when every weight is zero, because a percentage of nothing is not
    zero percent.
    """
    numerator = Decimal(0)
    weight = Decimal(0)
    for trade in slices:
        if trade.pnl_pct is None:
            continue
        basis = trade.open_price * trade.qty
        if basis == 0:
            continue
        numerator += trade.pnl_pct * basis
        weight += basis
    if weight == 0:
        return None
    return (numerator / weight).quantize(PCT_QUANTUM, rounding=ROUND_HALF_EVEN)


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One ledger row, with the OCC symbol kept beside its display label.

    PRD §8.2 makes the feed *"searchable by symbol or contract"*, and
    :class:`~corollary.api.schemas.ActivityItem` carries only the label --
    ``IWM $280 Put Sep 18``. Somebody pasting ``IWM260918P00280000`` must not
    be told there is no such trade, so the symbol travels alongside rather
    than being re-derived by a second parse at filter time.
    """

    item: ActivityItem
    symbol: str
    action: ActivityAction
    #: Contracts the fill moved, and the key its realized slices carry.
    #: Both are re-stated here rather than read back off :attr:`item`, whose
    #: ``quantity`` and ``price`` are nullable on the wire: the comparison
    #: :func:`unbooked_closes` makes is about money, and "unknown" is not a
    #: number it may silently take as zero.
    qty: int
    close_key: _CloseKey

    def matches(self, needle: str) -> bool:
        """Substring, case-insensitive, over symbol, label and action.

        The same three haystacks ``Activity.tsx`` already searches, so the
        migration is a data-source swap rather than a behaviour change.
        """
        return (
            needle in self.symbol.lower()
            or needle in self.item.contract.lower()
            or needle in self.action.lower()
        )


@dataclass(frozen=True, slots=True)
class LedgerGap:
    """Closings the realized-trade table does not fully account for.

    :attr:`count` is a number of **closings**, not of contracts: the header
    card says *"N trades not booked"* and a trade is the thing a person
    remembers making. :attr:`symbols` names them, because a bare count cannot
    be reconciled against the broker's own history by hand.
    """

    count: int
    symbols: tuple[str, ...]


def ledger_rows(
    fills: Sequence[Fill],
    trades: Sequence[RealizedTrade],
    *,
    correlation_id: str | None = None,
) -> list[LedgerRow]:
    """Fills and their realized P&L, in the order they arrived.

    Pure: no session, no clock beyond the one the log line stamps. Order is
    the caller's -- the route asks SQL for newest first -- because a fold that
    re-sorted would quietly disagree with the page it feeds.
    """
    index = _slices_by_close(trades)
    rows: list[LedgerRow] = []
    for fill in fills:
        action = _action_for(fill)
        if action is None:
            _refuse(fill, correlation_id)
            continue
        close_key: _CloseKey = (fill.symbol, fill.at, fill.price)
        pnl: Decimal | None = None
        pnl_pct: Decimal | None = None
        if action in _CLOSING_ACTIONS:
            slices = index.get(close_key)
            if slices:
                pnl = sum((trade.pnl for trade in slices), Decimal(0))
                pnl_pct = _weighted_pct(slices)
        rows.append(
            LedgerRow(
                item=ActivityItem(
                    id=fill.activity_id,
                    time=fill.at,
                    contract=contract_label(fill.symbol),
                    action=action,
                    price=fill.price,
                    quantity=fill.qty,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    # Money moved into the account is not money the account
                    # made. Deposits and withdrawals are the Account page's
                    # surface and have no `fill` row to arrive on here.
                    amount=None,
                    status=_FILL_STATUS,
                    rejection_reason=None,
                ),
                symbol=fill.symbol,
                action=action,
                qty=fill.qty,
                close_key=close_key,
            )
        )
    return rows


def _refuse(fill: Fill, correlation_id: str | None) -> None:
    """Rule 8: the rule, the inputs, and the timestamp. Never silent.

    In practice the only row that reaches here is a ``buy`` whose
    fill-to-order join never landed, so BTO and BTC are indistinguishable --
    ``ck_fill_position_intent`` keeps any *other* unreadable intent out of the
    column. It is dropped from the page rather than named, because a row
    asserting the wrong one of those is worse than a row that is missing and
    logged.

    Dropped, and therefore absent from ``total`` as well: a page whose count
    does not match its rows is its own quiet bug, and the record of what was
    left out is this line.
    """
    logger.warning(
        "activity row refused: %s on %s cannot state whether it opened or "
        "closed a position",
        fill.side,
        fill.symbol,
        extra={
            "event": "activity_row_refused",
            "rule": "unknown_intent",
            "detail": (
                "the four actions come from the order's position_intent, and "
                "this fill has none that names one -- `side` alone leaves "
                "`buy` as both buy-to-open and buy-to-close"
            ),
            "account": fill.account,
            "activity_id": fill.activity_id,
            "symbol": fill.symbol,
            "side": fill.side,
            "order_id": fill.order_id,
            "position_intent": fill.position_intent,
            "fill_at": fill.at.isoformat(),
            "at": datetime.now(timezone.utc).isoformat(),
            "correlation_id": correlation_id,
        },
    )


def activity_items(
    fills: Sequence[Fill],
    trades: Sequence[RealizedTrade],
    *,
    correlation_id: str | None = None,
) -> list[ActivityItem]:
    """:func:`ledger_rows` without the search fields. Deterministic."""
    return [
        row.item
        for row in ledger_rows(fills, trades, correlation_id=correlation_id)
    ]


# --------------------------------------------------------------------------
# The folds
# --------------------------------------------------------------------------


def unbooked_closes(
    fills: Sequence[Fill],
    trades: Sequence[RealizedTrade],
    *,
    correlation_id: str | None = None,
) -> LedgerGap:
    """Closings the ``realized_trade`` table does not fully account for.

    The gap open question 4 accepted, counted rather than hidden.

    The test is on **quantity**, not on presence, and that matters: a close
    of three contracts against two open lots books two and leaves one
    unaccounted for, and a presence test would call that ledger complete
    while a contract's worth of P&L is missing from every figure above it.
    Wholly refused and partly matched are the same answer to the only
    question the card asks -- *is the lifetime figure short*.

    An ``OPEXP`` is **not** one of these: it closes at zero, which is a price
    rather than an absence, and premium times the multiplier is right on any
    contract -- so it books in full and its quantity reconciles.
    """
    index = _slices_by_close(trades)
    missing: list[LedgerRow] = []
    for row in ledger_rows(fills, trades, correlation_id=correlation_id):
        if row.action not in _CLOSING_ACTIONS:
            continue
        booked = sum(trade.qty for trade in index.get(row.close_key, ()))
        if booked < row.qty:
            missing.append(row)
    return LedgerGap(
        count=len(missing), symbols=tuple(sorted({row.symbol for row in missing}))
    )


def ledger_stats(
    fills: Sequence[Fill],
    trades: Sequence[RealizedTrade],
    *,
    correlation_id: str | None = None,
) -> ActivityStats:
    """The header cards, folded over **every** realized trade. Decision 11.

    ``summarise`` is ``engine/ledger.py``'s own fold, reused rather than
    restated: the matcher's output and the database's rows are the same
    numbers, and a second implementation of "average loss" is a second one to
    be wrong.
    """
    folded = summarise(trades)
    gap = unbooked_closes(fills, trades, correlation_id=correlation_id)
    return ActivityStats(
        avg_win=folded.average_win,
        avg_win_pct=folded.average_win_pct,
        avg_loss=folded.average_loss,
        avg_loss_pct=folded.average_loss_pct,
        lifetime_pnl=folded.realized_pnl,
        wins=folded.wins,
        losses=folded.losses,
        not_booked=gap.count,
        not_booked_symbols=list(gap.symbols),
    )


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def fills_query(mode: AccountMode) -> Select[tuple[Fill]]:
    """One book's fills, newest first, on a **total** order.

    ``at`` alone is not one, and the tie-break is the whole reason this is a
    named function a test can compile rather than an expression inlined into
    the route. The composite activity id does not sort chronologically --
    stamps repeat and the UUID half then breaks ties arbitrarily -- and
    ``transaction_time`` does not either at microsecond resolution, so two
    rows really can share an instant. Page on an order that is only *nearly*
    total and one row appears on two pages while another appears on none.

    SQLite happens to answer a fully-tied ``ORDER BY`` in rowid order, so the
    bug does not reproduce here on a behavioural test -- which is exactly why
    the guarantee is asserted on the statement instead. The primary key is
    the tie-break: it is stable across re-ingestion, because ingestion
    upserts on ``activity_id`` and deletes nothing.
    """
    return (
        select(Fill)
        .where(Fill.account == mode.value)
        .order_by(Fill.at.desc(), Fill.id.desc())
    )


def trades_query(mode: AccountMode) -> Select[tuple[RealizedTrade]]:
    """**All** of one book's realized trades. Decision 11, deliberately.

    No ``LIMIT`` and no ``ORDER BY`` on money: the sort that matters here
    happens in Python, over loaded ``Decimal`` values, because ``'-41' <
    '-7'`` as text and the database would name the wrong trade with no error
    anywhere.
    """
    return select(RealizedTrade).where(RealizedTrade.account == mode.value)


def _fills(session: Session, mode: AccountMode) -> list[Fill]:
    return list(session.scalars(fills_query(mode)))


def _trades(session: Session, mode: AccountMode) -> list[RealizedTrade]:
    return list(session.scalars(trades_query(mode)))


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("", summary="The ledger -- paginated, searchable and filterable")
def read_activity(
    # Depended on for the account boundary it enforces, never called. Drop it
    # and `?account=cash` answers 200 with paper's empty ledger instead of
    # 409 with a stated reason -- see the module docstring.
    broker: BrokerDep,
    mode: AccountModeDep,
    session: SessionDep,
    page: Annotated[int, Query(ge=0, description="Zero-based.")] = 0,
    page_size: Annotated[
        int,
        Query(
            alias="pageSize",
            ge=1,
            le=MAX_PAGE_SIZE,
            description="Rows per page. The header cards are not computed from this.",
        ),
    ] = DEFAULT_PAGE_SIZE,
    search: Annotated[
        str | None,
        Query(
            max_length=64,
            description="Substring of the contract, its OCC symbol, or the action.",
        ),
    ] = None,
    status: Annotated[
        ActivityStatus | None,
        Query(description="Combines with `search` rather than replacing it."),
    ] = None,
) -> Page[ActivityItem]:
    """One page of the ledger.

    Search and status **combine**. *"Everything I did in AAPL"* is the
    question a fifty-row ledger raises and a status filter cannot answer, and
    a filter that silently replaced the other would answer a question nobody
    asked.

    Filtering happens in Python rather than in SQL, and that is the same
    decision as decision 11 rather than a shortcut around it: the ``action``
    a row is searched by does not exist as a column -- it comes from the
    fill-to-order join, and one of its four values cannot be stated at all
    for some rows -- so a SQL ``WHERE`` would count rows the page then drops,
    and ``total`` would be a number no page adds up to.
    """
    correlation_id = str(uuid.uuid4())
    rows = ledger_rows(
        _fills(session, mode), _trades(session, mode), correlation_id=correlation_id
    )
    needle = (search or "").strip().lower()
    matching = [
        row.item
        for row in rows
        if (status is None or row.item.status == status)
        and (not needle or row.matches(needle))
    ]
    start = page * page_size
    window = matching[start : start + page_size]
    return Page[ActivityItem](
        items=window,
        total=len(matching),
        page=page,
        page_size=page_size,
        has_more=start + len(window) < len(matching),
    )


@router.get("/stats", summary="The header cards, folded over every trade")
def read_stats(
    # The account boundary, as above. Not called.
    broker: BrokerDep,
    mode: AccountModeDep,
    session: SessionDep,
) -> ActivityStats:
    """Lifetime realized P&L, average win, average loss, and what is missing.

    Loads every realized trade in the book. Decision 11 weighed that against
    a trailing window and a maintained running total and took it: a single
    user will not approach a painful row count for years, a window makes
    "lifetime" mean something narrower than the word, and a running total is
    the one option that can drift from the rows it claims to summarise --
    drift in a P&L figure being exactly the kind that goes unnoticed.
    """
    correlation_id = str(uuid.uuid4())
    stats = ledger_stats(
        _fills(session, mode), _trades(session, mode), correlation_id=correlation_id
    )
    if stats.not_booked:
        logger.warning(
            "lifetime realized P&L is missing %d closing(s): %s",
            stats.not_booked,
            ", ".join(stats.not_booked_symbols),
            extra={
                "event": "activity_stats_incomplete",
                "rule": "a known-incomplete figure is stated, never quietly wrong",
                "detail": (
                    "a closing fill produced no realized_trade row; open "
                    "question 4 refuses to book an adjusted contract's "
                    "settlement rather than guess its deliverable"
                ),
                "account": mode.value,
                "not_booked": stats.not_booked,
                "symbols": list(stats.not_booked_symbols),
                "at": datetime.now(timezone.utc).isoformat(),
                "correlation_id": correlation_id,
            },
        )
    return stats
