"""Logical positions, and the orders that are still working.

``GET /v2/positions`` keys on the OCC symbol and carries **no leg grouping, no
order linkage and no open date**. A four-leg iron condor is four rows and
nothing on any of them says so. This module turns those rows into the thing
the payoff curve, the DTE column, max loss and the risk class all assume
exists -- one *logical* position per structure -- by calling
:func:`corollary.engine.grouping.group_positions`, which is where the rule
lives. Nothing here re-implements it.

**Why grouping is rule 4 rather than tidiness.** A short leg rendered alone
reports as an *undefined-risk naked short*, so a defined-risk credit spread
would state the wrong risk class and the risk manager would size against the
wrong maximum loss. That is the exact failure rule 4 exists to prevent, which
is why a heuristic on same-underlying/same-expiry/offsetting-sides was
rejected: it is wrong on iron condors, on ratio spreads, and on this very
account, which holds three unrelated NVDA singles sharing one expiry.

Two rules decide everything else in here
----------------------------------------

**The broker answers what is held and what it is worth. The provider answers
what it is quoted at and what it did.** Cost basis, market value and P&L are
the broker's own numbers, netted across legs with **every sign preserved** --
a short's ``cost_basis`` and ``market_value`` really are negative, because a
credit received is a liability, and ``orders.ts`` says the same thing from the
other end. Bid, ask, the underlying's spot and the value series come from
:class:`~corollary.data.providers.interface.MarketDataProvider`. Losing the
second must never remove the first, so a quote or a bar outage degrades with a
logged record and the book still renders.

The one exception is :attr:`Position.underlying`, which is what the payoff
curve is drawn against. There is no honest substitute for a spot price -- a
zero draws a catastrophe and a guess is the invented number PRD §8.5 names --
so a missing one is a **stated 502** naming the symbol rather than a row with
a plausible figure in it.

**Every write control is inert, and there is no write here to make inert.**
Decision 2: Close, Add, Attach/Edit exit and Working Orders' Cancel all render
disabled with a one-line reason naming Phase 6. This module exposes two GETs.
Cancel-only was considered and rejected -- a cancel is not an order, but it
puts the first broker write before the risk manager exists. The routes depend
on :class:`~corollary.engine.execution.interface.BrokerAccount`, the read half
of the vendor surface. The write half does not exist until Phase 6, so no
order-placing method is reachable from any type named in this file -- rule 1
holds structurally rather than by discipline, and
``tests/api/test_positions_routes.py`` greps this module to keep it that way.

What is legitimately absent
---------------------------

``strategyId``, ``openedByStrategyId``, ``managedExit`` and ``attachedExit``
are ``None``. Nothing Corollary opened exists yet, so every position is
genuinely *detached* -- that is the true state, not a gap, and an id that
resolves to nothing would be worse than an admitted absence.

``valueHistory`` is derived from the contract's daily bars, and it starts at
the **opening fill's date** read from the ``fill`` table. A broker position
carries no open date at all, so without an ingested fill the honest series is
an empty one. It is also multiplied by the contract's **own** multiplier from
``/v2/options/contracts``, never by 100: ``/v2/positions`` returns no
multiplier field, and on an adjusted contract (``root_symbol != underlying``)
the deliverable is not 100 shares. A multiplier that is not known produces no
series, which is the refusal the ledger makes for the same reason.
"""

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

from corollary.api.deps import (
    AccountModeDep,
    ApiError,
    BrokerDep,
    ProviderDep,
    SessionDep,
)
from corollary.api.schemas import (
    AccountMode,
    CalendarDate,
    Direction,
    OptionRight,
    OrderSide as ApiOrderSide,
    Position,
    PositionLeg as ApiPositionLeg,
    PricePoint,
    TimeInForce,
    WorkingOrder,
    WorkingOrderType,
)
from corollary.calendars import NYSE_TZ
from corollary.data.providers.interface import (
    Bar,
    BarTimeframe,
    MarketDataProvider,
    OptionContract,
    ProviderError,
    Quote,
)
from corollary.db.models import Fill
from corollary.engine.execution.interface import (
    BrokerPosition,
    Order,
    OrderQueryStatus,
    PositionIntent,
    PositionSide,
)
from corollary.engine.grouping import (
    GroupingResult,
    LogicalPosition,
    PositionLeg,
    group_positions,
)
from corollary.instruments import OccSymbol, OptionType, parse_occ_symbol

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/positions", tags=["positions"])


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Order statuses that mean *placed and still resting*. Spelled out rather
#: than derived from a terminal list, because the safe direction on an
#: unrecognised status is to report it rather than to guess it is live: a
#: filled order rendered as working is a phantom order in the market.
_WORKING_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "new",
        "accepted",
        "accepted_for_bidding",
        "partially_filled",
        "pending_new",
        "pending_cancel",
        "pending_replace",
        "held",
    }
)

#: Statuses that mean the order is over. Kept beside the working set so that
#: anything in *neither* is a status this code has never seen, which is a log
#: line rather than a silent inclusion or a silent drop.
_SETTLED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "filled",
        "canceled",
        "expired",
        "rejected",
        "replaced",
        "done_for_day",
        "stopped",
        "calculated",
        "suspended",
    }
)

#: The four-way action, spelled as the UI spells it.
_SIDE_FOR_INTENT: Final[Mapping[PositionIntent, ApiOrderSide]] = {
    PositionIntent.BUY_TO_OPEN: "BTO",
    PositionIntent.BUY_TO_CLOSE: "BTC",
    PositionIntent.SELL_TO_OPEN: "STO",
    PositionIntent.SELL_TO_CLOSE: "STC",
}

_OPENING_INTENTS: Final[frozenset[PositionIntent]] = frozenset(
    {PositionIntent.BUY_TO_OPEN, PositionIntent.SELL_TO_OPEN}
)

#: ``types.ts`` says ``Exclude<OrderType, 'market'>`` and this is that
#: exclusion written out: a market order fills, it does not sit and work.
_WORKING_ORDER_TYPES: Final[Mapping[str, WorkingOrderType]] = {
    "limit": "limit",
    "stop": "stop",
    "stop_limit": "stop_limit",
}

#: Alpaca accepts six time-in-force values; an *option* order accepts two.
_TIME_IN_FORCE: Final[Mapping[str, TimeInForce]] = {"day": "day", "gtc": "gtc"}

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

_HUNDRED: Final = Decimal(100)

#: Four decimal places on a percentage -- 0.0001% of a position, which is
#: below anything the page renders. It is not cosmetic: ``-275/340`` is a
#: repeating decimal, and an unrounded one trips the API boundary's
#: lost-precision warning on **every** position of **every** poll. A log that
#: cries wolf nine times every fifteen seconds is a log nobody reads on the
#: day it is right.
_PCT_PLACES: Final = Decimal("0.0001")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------


def _strike_label(strike: Decimal) -> str:
    if strike == strike.to_integral_value():
        return f"${int(strike)}"
    return f"${strike:.2f}"


def _expiry_label(day: date) -> str:
    """``Jan 15``. Written out rather than ``strftime``: ``%-d`` is not
    portable to Windows and ``%d`` pads a single digit to ``05``."""
    return f"{_MONTHS[day.month - 1]} {day.day}"


def _right_label(option_type: OptionType) -> str:
    return "Call" if option_type is OptionType.CALL else "Put"


def _right(option_type: OptionType) -> OptionRight:
    return "call" if option_type is OptionType.CALL else "put"


def _side(side: PositionSide) -> Direction:
    return "long" if side is PositionSide.LONG else "short"


def _describe(
    contracts: Sequence[tuple[OccSymbol, PositionSide]],
    *,
    direction: Direction,
    expiry: date,
) -> str:
    """A human label for a structure -- ``$470/$460 Put Credit Spread Jan 15``.

    Named only where the shape is unambiguous. Two legs of one right on one
    expiry is a vertical and is called one; anything else is reported as an
    ``N-Leg`` with its strikes, because a label that guesses "Iron Condor" at
    a butterfly is worse than one that does not guess at all.
    """
    strikes = "/".join(_strike_label(occ.strike) for occ, _ in contracts)
    tail = _expiry_label(expiry)
    if len(contracts) == 1:
        return f"{strikes} {_right_label(contracts[0][0].option_type)} {tail}"
    rights = {occ.option_type for occ, _ in contracts}
    expirations = {occ.expiration for occ, _ in contracts}
    sides = {side for _, side in contracts}
    vertical = (
        len(contracts) == 2
        and len(rights) == 1
        and len(expirations) == 1
        and len(sides) == 2
    )
    if vertical:
        kind = "Credit" if direction == "short" else "Debit"
        right = _right_label(next(iter(rights)))
        return f"{strikes} {right} {kind} Spread {tail}"
    return f"{strikes} {len(contracts)}-Leg {tail}"


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------


def _is_crossed(quote: Quote) -> bool:
    """A two-sided quote whose bid is above its ask. Never a market.

    The one thing :attr:`~corollary.data.providers.interface.Quote.mid` and
    this module have to agree on, so it is asked once and in one place.
    """
    return quote.bid is not None and quote.ask is not None and quote.bid > quote.ask


class _Marks:
    """One leg's mark, bid and ask, with every substitution on the record.

    ``bid <= last <= ask`` holds on every leg this class prices. That is worth
    stating precisely, because it takes three rules rather than one: the
    broker's ``current_price`` and the indicative quote are two sources read
    at two instants, and a ``last`` outside its own spread is the kind of
    number a reader stops trusting the rest of the row over.

    * **Two-sided quote** -- the mark is the **mid**, so the invariant is
      arithmetic.
    * **One-sided quote** -- ordinary on the indicative feed, where 49 of 100
      recorded NVDA contracts had no bid at all. The broker's mark fills the
      missing side, *and is itself held inside the side that was quoted*. A
      stale mark of 26.27 against a quoted ask of 26.00 would otherwise print
      a last price above its own ask: clamping only the substituted side
      protects bid-versus-ask and leaves last outside both.
    * **No quote, or a crossed one** -- the mark stands on both sides. A bid
      above an ask is a data error rather than a market, which is why
      :attr:`~corollary.data.providers.interface.Quote.mid` refuses to average
      one; no arithmetic can satisfy the invariant against it, and choosing a
      side would launder the error into a plausible spread.

    Every substitution above is logged with the rule, the symbol and the
    timestamp. None of them touches ``value``, ``cost_basis`` or ``pnl``,
    which are the broker's own numbers.
    """

    def __init__(
        self,
        quotes: Mapping[str, Quote],
        positions: Mapping[str, BrokerPosition],
        *,
        correlation_id: str,
    ) -> None:
        self._mark: dict[str, Decimal] = {}
        self._bid: dict[str, Decimal] = {}
        self._ask: dict[str, Decimal] = {}
        self.unpriced: list[str] = []
        for symbol, position in positions.items():
            quote = quotes.get(symbol)
            mark = self._resolve_mark(symbol, quote, position, correlation_id)
            if mark is None:
                self.unpriced.append(symbol)
                continue
            self._mark[symbol] = mark
            self._bid[symbol] = self._resolve_bid(symbol, quote, mark, correlation_id)
            self._ask[symbol] = self._resolve_ask(symbol, quote, mark, correlation_id)

    @staticmethod
    def _missing(symbol: str, part: str, correlation_id: str) -> None:
        logger.warning(
            "no %s quoted for %s; the broker's mark stands in",
            part,
            symbol,
            extra={
                "event": "position_quote_missing",
                "rule": (
                    "a spread nobody measured is never invented; the mark "
                    "stands on both sides and the substitution is recorded"
                ),
                "symbol": symbol,
                "missing": part,
                "at": _now(),
                "correlation_id": correlation_id,
            },
        )

    @staticmethod
    def _outside(
        symbol: str,
        mark: Decimal,
        side: str,
        bound: Decimal,
        correlation_id: str,
    ) -> None:
        logger.warning(
            "the mark %s for %s is outside the quoted %s %s; the %s stands",
            mark,
            symbol,
            side,
            bound,
            side,
            extra={
                "event": "position_mark_outside_quote",
                "rule": (
                    "bid <= last <= ask on every row; a mark read at another "
                    "instant is held inside the side that was quoted rather "
                    "than printed beyond it"
                ),
                "symbol": symbol,
                "mark": str(mark),
                "side": side,
                "bound": str(bound),
                "at": _now(),
                "correlation_id": correlation_id,
            },
        )

    @staticmethod
    def _crossed(symbol: str, quote: Quote, correlation_id: str) -> None:
        logger.warning(
            "the quote for %s is crossed (bid %s above ask %s); the broker's "
            "mark stands on both sides",
            symbol,
            quote.bid,
            quote.ask,
            extra={
                "event": "position_quote_crossed",
                "rule": (
                    "a bid above an ask is a data error rather than a market; "
                    "it is never shown as a spread and never averaged"
                ),
                "symbol": symbol,
                "bid": str(quote.bid),
                "ask": str(quote.ask),
                "at": _now(),
                "correlation_id": correlation_id,
            },
        )

    def _resolve_mark(
        self,
        symbol: str,
        quote: Quote | None,
        position: BrokerPosition,
        correlation_id: str,
    ) -> Decimal | None:
        if quote is not None and quote.mid is not None:
            return quote.mid
        if quote is None:
            self._missing(symbol, "quote", correlation_id)
            return position.current_price
        if _is_crossed(quote):
            # Both sides quoted and no mid means exactly one thing: `Quote.mid`
            # returns None on a crossed market. Nothing below can make the
            # invariant true of a quote whose bid is already above its ask, so
            # neither side is used at all -- `_resolve_bid` and `_resolve_ask`
            # take the same branch.
            self._crossed(symbol, quote, correlation_id)
            return position.current_price
        return self._inside_the_quoted_side(
            symbol, position.current_price, quote, correlation_id
        )

    def _inside_the_quoted_side(
        self,
        symbol: str,
        mark: Decimal | None,
        quote: Quote,
        correlation_id: str,
    ) -> Decimal | None:
        """The broker's mark, held inside whichever side the feed did quote.

        At most one side is set by the time this is reached -- two would have
        produced a mid, or been crossed and handled above -- so at most one
        bound can bind. Both are checked anyway rather than inferred from
        that: the reasoning lives in the caller, and a future caller is not
        bound by it.
        """
        if mark is None:
            return None
        if quote.ask is not None and mark > quote.ask:
            self._outside(symbol, mark, "ask", quote.ask, correlation_id)
            return quote.ask
        if quote.bid is not None and mark < quote.bid:
            self._outside(symbol, mark, "bid", quote.bid, correlation_id)
            return quote.bid
        return mark

    def _resolve_bid(
        self, symbol: str, quote: Quote | None, mark: Decimal, correlation_id: str
    ) -> Decimal:
        if quote is None or _is_crossed(quote):
            # No usable market: the mark stands on both sides. Already logged
            # by `_resolve_mark`, which took the same decision.
            return mark
        if quote.bid is not None:
            return quote.bid
        self._missing(symbol, "bid", correlation_id)
        # Clamped so the substituted side can never cross the quoted one: a
        # bid above the ask is not a market, it is a data error wearing a
        # price. `mark` is already inside that side -- see
        # `_inside_the_quoted_side` -- so this is belt and braces rather than
        # the thing keeping the invariant.
        return min(mark, quote.ask) if quote.ask is not None else mark

    def _resolve_ask(
        self, symbol: str, quote: Quote | None, mark: Decimal, correlation_id: str
    ) -> Decimal:
        if quote is None or _is_crossed(quote):
            return mark
        if quote.ask is not None:
            return quote.ask
        self._missing(symbol, "ask", correlation_id)
        return max(mark, quote.bid) if quote.bid is not None else mark

    def net(self, legs: Sequence[PositionLeg]) -> tuple[Decimal, Decimal, Decimal]:
        """``(last, bid, ask)`` per unit of the structure, signed.

        A long leg adds and a short leg subtracts, which is the same sign
        convention the broker uses on ``cost_basis`` and ``market_value``. So
        **selling hits the bid and buying lifts the ask** on every leg: the
        structure's own bid negates each short leg's *ask*, because that is
        what closing it costs. Reversed, every credit position would quote a
        better exit than the market offers.
        """
        last = bid = ask = Decimal(0)
        for leg in legs:
            ratio = Decimal(leg.ratio)
            if leg.is_short:
                last -= ratio * self._mark[leg.symbol]
                bid -= ratio * self._ask[leg.symbol]
                ask -= ratio * self._bid[leg.symbol]
            else:
                last += ratio * self._mark[leg.symbol]
                bid += ratio * self._bid[leg.symbol]
                ask += ratio * self._ask[leg.symbol]
        return last, bid, ask


async def _option_quotes(
    provider: MarketDataProvider, symbols: Sequence[str], *, correlation_id: str
) -> dict[str, Quote]:
    """Latest option quotes, or an empty book and a log line.

    A quote outage degrades the spread column. It does not get to remove the
    positions, which came from the broker and are still held.
    """
    if not symbols:
        return {}
    try:
        return await provider.latest_option_quotes(symbols)
    except ProviderError as exc:
        logger.warning(
            "option quotes are unavailable; marks stand in for the spread: %s",
            exc,
            extra={
                "event": "position_quotes_unavailable",
                "rule": (
                    "the broker answers what is held; losing the quote feed "
                    "degrades the spread and never empties the book"
                ),
                "symbols": list(symbols),
                "detail": f"{type(exc).__name__}: {exc}",
                "at": _now(),
                "correlation_id": correlation_id,
            },
        )
        return {}


async def _underlying_prices(
    provider: MarketDataProvider, underlyings: Sequence[str], *, correlation_id: str
) -> dict[str, Decimal]:
    """Spot per underlying, or a stated failure naming what is missing.

    The one place this module refuses to degrade. ``Position.underlying`` is
    what the payoff curve is drawn against, and a zero there draws a
    catastrophe that nothing on the page marks as unknown.
    """
    if not underlyings:
        return {}
    snapshots = await provider.stock_snapshots(underlyings)
    prices: dict[str, Decimal] = {}
    missing: list[str] = []
    for symbol in underlyings:
        snapshot = snapshots.get(symbol)
        price = None if snapshot is None else snapshot.price
        if price is None:
            missing.append(symbol)
            continue
        prices[symbol] = price
    if missing:
        message = (
            "No price is available for "
            + ", ".join(missing)
            + ". Positions on it are held but cannot be shown against a spot "
            "price, and a substituted one would misdraw every payoff curve."
        )
        logger.warning(
            "refused to serve positions without a spot price: %s",
            ", ".join(missing),
            extra={
                "event": "underlying_price_unavailable",
                "rule": (
                    "an underlying's price is stated or the request fails; "
                    "it is never substituted"
                ),
                "symbols": missing,
                "at": _now(),
                "correlation_id": correlation_id,
            },
        )
        raise ApiError(
            status_code=502,
            code="underlying_price_unavailable",
            message=message,
        )
    return prices


# --------------------------------------------------------------------------
# Contract terms -- the only source of a multiplier
# --------------------------------------------------------------------------


async def _contract_terms(
    provider: MarketDataProvider,
    held: Mapping[str, OccSymbol],
    *,
    correlation_id: str,
) -> dict[str, OptionContract]:
    """Reference data per held contract, queried by underlying and expiry.

    ``/v2/positions`` returns **no multiplier field at all**, so this endpoint
    is not an optimisation -- it is the only place a per-contract multiplier
    exists. ``include_adjusted=True`` because a held adjusted contract must
    come back *with* its terms; excluding it would leave the one contract
    whose deliverable is not 100 shares as the one contract with no terms.

    An outage is degraded rather than fatal: grouping still works without
    terms, and the consequence -- no value series -- is stated in the log and
    visible as an empty chart.
    """
    terms: dict[str, OptionContract] = {}
    if not held:
        return terms

    async def fetch(root_of: Mapping[str, str], outstanding: Sequence[str]) -> None:
        groups: dict[tuple[str, date], list[str]] = {}
        for symbol in outstanding:
            occ = held[symbol]
            groups.setdefault((root_of[symbol], occ.expiration), []).append(symbol)
        for (underlying, expiration), members in sorted(groups.items()):
            try:
                found = await provider.option_contracts(
                    underlying,
                    expiration_gte=expiration,
                    expiration_lte=expiration,
                    include_adjusted=True,
                )
            except ProviderError as exc:
                logger.warning(
                    "contract terms are unavailable for %s %s: %s",
                    underlying,
                    expiration.isoformat(),
                    exc,
                    extra={
                        "event": "position_contract_terms_unavailable",
                        "rule": (
                            "a multiplier that cannot be read is never "
                            "assumed to be 100"
                        ),
                        "symbols": members,
                        "detail": f"{type(exc).__name__}: {exc}",
                        "at": _now(),
                        "correlation_id": correlation_id,
                    },
                )
                continue
            wanted = set(members)
            for contract in found:
                if contract.symbol in wanted:
                    terms[contract.symbol] = contract

    await fetch({symbol: occ.root for symbol, occ in held.items()}, sorted(held))
    # An adjusted root carries a numeric suffix -- `AAPL1` -- and the
    # contracts endpoint keys on the *underlying*, so the first pass finds
    # nothing for exactly the contracts whose multiplier matters most.
    retry: dict[str, str] = {}
    for symbol, occ in held.items():
        if symbol in terms or not occ.root[-1:].isdigit():
            continue
        stripped = occ.root.rstrip("0123456789")
        if stripped:
            retry[symbol] = stripped
    if retry:
        await fetch(retry, sorted(retry))
    return terms


# --------------------------------------------------------------------------
# Value history -- the entry date is the fill's, never the position's
# --------------------------------------------------------------------------


def _entry_times(
    session: Session, mode: AccountMode, symbols: Sequence[str]
) -> dict[str, datetime]:
    """When each held contract's *current* position was opened.

    A broker position has no open date, so this is the ``fill`` table or
    nothing. The walk matters: a symbol bought, closed and bought again is a
    **new** position, and starting the series at the first purchase ever would
    chart a position that did not exist through the middle of its own graph.
    Running quantity back to zero clears the entry; the next fill sets it
    again.

    Ordering is on ``at``, never on ``activity_id``: only the 17-digit stamp
    half of that id orders, stamps repeat, and the UUID half then breaks ties
    arbitrarily -- on this account's own data the first pair it inverts is the
    two legs of one vertical.
    """
    if not symbols:
        return {}
    rows = list(
        session.scalars(
            select(Fill).where(
                Fill.account == mode.value, Fill.symbol.in_(list(symbols))
            )
        )
    )
    by_symbol: dict[str, list[Fill]] = {}
    for row in rows:
        by_symbol.setdefault(row.symbol, []).append(row)

    entries: dict[str, datetime] = {}
    for symbol, fills in by_symbol.items():
        running = 0
        entry: datetime | None = None
        for fill in sorted(fills, key=lambda row: (row.at, row.activity_id)):
            if running == 0:
                entry = fill.at
            running += fill.qty if fill.side == "buy" else -fill.qty
            if running == 0:
                entry = None
        if entry is not None:
            entries[symbol] = entry
    return entries


def _session_date(at: datetime) -> CalendarDate:
    """The Eastern session a bar belongs to.

    Market data is Eastern and a daily bar is stamped at the session open in
    UTC. Reading ``.date()`` off the UTC instant is right today and wrong the
    day a vendor stamps a bar at 00:00 UTC.
    """
    return at.astimezone(NYSE_TZ).date()


def _value_history(
    logical: LogicalPosition,
    entries: Mapping[str, datetime],
    bars: Mapping[str, list[Bar]],
    *,
    correlation_id: str,
) -> list[PricePoint]:
    """The position's value per session, from the opening fill to today.

    Every leg must price on a day for that day to have a value: a partial sum
    over a spread is not a position's value, it is one leg's.

    Refuses -- with a log line, never a default -- if any leg's multiplier is
    unknown. ``/v2/positions`` carries no multiplier and an adjusted contract
    does not deliver 100 shares, so assuming 100 would state a number that is
    wrong by the deliverable on exactly the contracts where being wrong costs
    the most.
    """
    starts = [entries[leg.symbol] for leg in logical.legs if leg.symbol in entries]
    if len(starts) != len(logical.legs):
        return []
    missing = [leg.symbol for leg in logical.legs if leg.multiplier is None]
    if missing:
        logger.warning(
            "no value series for %s: %d leg(s) have no multiplier",
            logical.id,
            len(missing),
            extra={
                "event": "position_value_history_refused",
                "rule": (
                    "a multiplier comes from the contracts endpoint or the "
                    "series is not drawn; it is never assumed to be 100"
                ),
                "position_id": logical.id,
                "symbols": missing,
                "at": _now(),
                "correlation_id": correlation_id,
            },
        )
        return []

    opened = min(_session_date(start) for start in starts)
    per_leg: list[dict[CalendarDate, Decimal]] = []
    for leg in logical.legs:
        multiplier = leg.multiplier
        assert multiplier is not None  # refused above
        quantity = leg.quantity
        closes: dict[CalendarDate, Decimal] = {}
        for bar in bars.get(leg.symbol, ()):
            session = _session_date(bar.at)
            if session < opened:
                continue
            closes[session] = bar.close * quantity * multiplier
        per_leg.append(closes)

    if not per_leg:
        return []
    common = set(per_leg[0])
    for closes in per_leg[1:]:
        common &= set(closes)
    return [
        PricePoint(date=day, value=sum((closes[day] for closes in per_leg), Decimal(0)))
        for day in sorted(common)
    ]


async def _daily_bars(
    provider: MarketDataProvider,
    symbols: Sequence[str],
    start: datetime,
    *,
    correlation_id: str,
) -> dict[str, list[Bar]]:
    try:
        return await provider.option_bars(
            symbols, timeframe=BarTimeframe.DAY, start=start
        )
    except ProviderError as exc:
        logger.warning(
            "option bars are unavailable; value series are empty: %s",
            exc,
            extra={
                "event": "position_bars_unavailable",
                "rule": (
                    "a missing series renders as missing; the positions "
                    "themselves come from the broker and still render"
                ),
                "symbols": list(symbols),
                "detail": f"{type(exc).__name__}: {exc}",
                "at": _now(),
                "correlation_id": correlation_id,
            },
        )
        return {}


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def _direction_of(
    logical: LogicalPosition, *, correlation_id: str
) -> Direction:
    """Debit long, credit short, from the order's own net price.

    ``group_positions`` answers ``None`` for a structure filled at exactly
    zero, because a costless structure is neither. The wire type has no third
    value, so the fallback is the **broker's** signed cost basis -- a second
    piece of evidence rather than an inference -- and the fallback is logged
    where it is used.
    """
    if logical.direction is not None:
        return _side(logical.direction)
    basis = logical.cost_basis
    resolved: Direction = "short" if basis < 0 else "long"
    logger.info(
        "%s reported no net price; direction read from the signed cost basis",
        logical.id,
        extra={
            "event": "position_direction_from_basis",
            "rule": (
                "debit long, credit short -- from the order's net price, or "
                "from the broker's signed basis when the order reports none"
            ),
            "position_id": logical.id,
            "cost_basis": str(basis),
            "direction": resolved,
            "at": _now(),
            "correlation_id": correlation_id,
        },
    )
    return resolved


def _leg_model(leg: PositionLeg) -> ApiPositionLeg:
    return ApiPositionLeg(
        symbol=leg.symbol,
        strike=leg.strike,
        right=_right(leg.option_type),
        side=_side(leg.side),
        ratio=leg.ratio,
    )


def _pnl_pct(pnl: Decimal, cost_basis: Decimal) -> Decimal:
    """Return on cost basis, as a percentage.

    Denominated on the **magnitude** of the basis. A short's basis is
    negative, so a signed denominator would report every credit spread's gain
    as a loss and every loss as a gain -- sign error in the one column a
    reader uses to decide whether to intervene.
    """
    if cost_basis == 0:
        return Decimal(0)
    return (pnl / abs(cost_basis) * _HUNDRED).quantize(
        _PCT_PLACES, rounding=ROUND_HALF_UP
    )


def _position_model(
    logical: LogicalPosition,
    *,
    marks: _Marks,
    underlying: Decimal,
    value_history: list[PricePoint],
    correlation_id: str,
) -> Position:
    last, bid, ask = marks.net(logical.legs)
    cost_basis = logical.cost_basis
    unrealized = logical.unrealized_pl
    value = logical.market_value
    pnl = unrealized if unrealized is not None else value - cost_basis
    direction = _direction_of(logical, correlation_id=correlation_id)
    return Position(
        id=logical.id,
        symbol=logical.underlying,
        contract=_describe(
            [(leg.occ, leg.side) for leg in logical.legs],
            direction=direction,
            expiry=logical.expiry,
        ),
        last=last,
        underlying=underlying,
        cost_basis=cost_basis,
        value=value,
        quantity=logical.units,
        pnl=pnl,
        pnl_pct=_pnl_pct(pnl, cost_basis),
        bid=bid,
        ask=ask,
        direction=direction,
        legs=[_leg_model(leg) for leg in logical.legs],
        expiry=logical.expiry,
        strategy_id=None,
        opened_by_strategy_id=None,
        managed_exit=None,
        attached_exit=None,
        value_history=value_history,
    )


def _log_assembly(
    result: GroupingResult,
    *,
    mode: AccountMode,
    rows: int,
    without_entry: int,
    correlation_id: str,
) -> None:
    """One record per request, so a position on screen traces to its evidence.

    Rule 8's standard applied to an assembly rather than a rejection: the
    declines are already logged one by one by ``grouping.py``; this is the
    line that says how many there were and which book they belong to, so a
    screen showing fewer positions than expected has somewhere to start.
    """
    logger.info(
        "assembled %d logical positions from %d broker rows (%s)",
        len(result.positions),
        rows,
        mode.value,
        extra={
            "event": "positions_assembled",
            "rule": (
                "group only where an mleg order proves it; everything else is "
                "a single-leg position labelled ungrouped"
            ),
            "account": mode.value,
            "rows": rows,
            "logical": len(result.positions),
            "grouped": len(result.groups),
            "declined": len(result.declined),
            "decline_rules": [decision.rule.value for decision in result.declined],
            "unknown_multipliers": list(result.unknown_multipliers),
            "adjusted_contracts": list(result.adjusted_contracts),
            "excluded": dict(result.excluded),
            "without_entry_fill": without_entry,
            "at": _now(),
            "correlation_id": correlation_id,
        },
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("", summary="Logical positions, grouped only where an order proves it")
async def read_positions(
    mode: AccountModeDep,
    broker: BrokerDep,
    provider: ProviderDep,
    session: SessionDep,
) -> list[Position]:
    """The book, as positions rather than as contract rows.

    Four broker rows and one ``mleg`` order are one iron condor here, and the
    evidence is the order -- never the shape of the rows.
    """
    correlation_id = str(uuid.uuid4())
    rows = await broker.positions()
    orders = await broker.orders(status=OrderQueryStatus.ALL)

    held: dict[str, OccSymbol] = {}
    for row in rows:
        try:
            held[row.symbol] = parse_occ_symbol(row.symbol)
        except ValueError:
            # Shares delivered by an exercise or an assignment. The grouper
            # names them in `excluded`; Corollary is not a stock app.
            continue

    terms = await _contract_terms(provider, held, correlation_id=correlation_id)
    result = group_positions(rows, orders, contracts=terms)

    by_symbol = {row.symbol: row for row in rows if row.symbol in held}
    quotes = await _option_quotes(
        provider, sorted(by_symbol), correlation_id=correlation_id
    )
    marks = _Marks(quotes, by_symbol, correlation_id=correlation_id)
    if marks.unpriced:
        raise ApiError(
            status_code=502,
            code="position_mark_unavailable",
            message=(
                "No price is available for "
                + ", ".join(sorted(marks.unpriced))
                + ". The broker reported none and none is quoted, and a "
                "position priced from nothing is worse than one not shown."
            ),
        )

    prices = await _underlying_prices(
        provider,
        sorted({logical.underlying for logical in result.positions}),
        correlation_id=correlation_id,
    )

    entries = _entry_times(session, mode, sorted(held))
    bars: Mapping[str, list[Bar]] = {}
    if entries:
        bars = await _daily_bars(
            provider,
            sorted(entries),
            min(entries.values()),
            correlation_id=correlation_id,
        )

    positions: list[Position] = []
    without_entry = 0
    for logical in result.positions:
        history = _value_history(
            logical, entries, bars, correlation_id=correlation_id
        )
        if not history:
            without_entry += 1
        positions.append(
            _position_model(
                logical,
                marks=marks,
                underlying=prices[logical.underlying],
                value_history=history,
                correlation_id=correlation_id,
            )
        )

    _log_assembly(
        result,
        mode=mode,
        rows=len(rows),
        without_entry=without_entry,
        correlation_id=correlation_id,
    )
    return positions


@router.get("/working", summary="Placed orders that have not filled")
async def read_working_orders(
    mode: AccountModeDep,
    broker: BrokerDep,
) -> list[WorkingOrder]:
    """Orders resting at the broker. **No cancel, and no modify.**

    Decision 2: the Cancel control renders disabled with a reason naming
    Phase 6, and the reason there is no cancel *endpoint* is that a cancel
    would be the first broker write in the codebase, landing before the risk
    manager exists.

    Reads no market data. A resting order has no mark, no spread and no
    chart; asking for one would spend both rate-limit buckets to display
    nothing.
    """
    correlation_id = str(uuid.uuid4())
    rows = await broker.positions()
    orders = await broker.orders(status=OrderQueryStatus.ALL)
    owner = _position_owners(rows, orders)

    working: list[WorkingOrder] = []
    for order in orders:
        if not _is_working(order, correlation_id=correlation_id):
            continue
        model = _working_model(order, owner, correlation_id=correlation_id)
        if model is not None:
            working.append(model)
    logger.info(
        "%d working order(s) on %s",
        len(working),
        mode.value,
        extra={
            "event": "working_orders_read",
            "account": mode.value,
            "orders": len(orders),
            "working": len(working),
            "at": _now(),
            "correlation_id": correlation_id,
        },
    )
    return working


# --------------------------------------------------------------------------
# Working orders
# --------------------------------------------------------------------------


def _position_owners(
    rows: Sequence[BrokerPosition], orders: Sequence[Order]
) -> dict[str, str]:
    """Held symbol -> the id of the logical position that contains it.

    Grouped without contract terms on purpose: a logical id is the parent
    order id or the OCC symbol, and neither needs a multiplier. Fetching terms
    here would spend a rate-limit bucket on a number this route never prints.
    """
    result = group_positions(rows, orders)
    return {
        leg.symbol: logical.id
        for logical in result.positions
        for leg in logical.legs
    }


def _is_working(order: Order, *, correlation_id: str) -> bool:
    if order.status in _WORKING_STATUSES:
        return True
    if order.status in _SETTLED_STATUSES:
        return False
    logger.warning(
        "order %s reports an unrecognised status %r and is not shown as working",
        order.id,
        order.status,
        extra={
            "event": "working_order_status_unknown",
            "rule": (
                "an unrecognised status is reported rather than assumed live; "
                "a filled order rendered as working is a phantom in the market"
            ),
            "order_id": order.id,
            "status": order.status,
            "at": _now(),
            "correlation_id": correlation_id,
        },
    )
    return False


def _refuse(order: Order, reason: str, *, correlation_id: str) -> None:
    logger.warning(
        "working order %s cannot be stated: %s",
        order.id,
        reason,
        extra={
            "event": "working_order_unrepresentable",
            "rule": (
                "an order whose action, type or size cannot be stated is "
                "reported as a gap, never guessed at"
            ),
            "order_id": order.id,
            "symbol": order.symbol,
            "status": order.status,
            "detail": reason,
            "at": _now(),
            "correlation_id": correlation_id,
        },
    )


def _order_side(order: Order) -> ApiOrderSide | None:
    """BTO / BTC / STO / STC, or ``None`` when the order does not say.

    ``side`` alone cannot answer it: ``buy`` is **both** buy-to-open and
    buy-to-close. Guessing is how a ledger books every close as a new lot, and
    the same guess here labels an exit as an entry on the screen somebody
    checks before intervening.

    An ``mleg`` parent carries no intent of its own -- the parent is the
    structure and the legs are the instruments -- so its action comes from its
    legs' intents plus the **sign of its limit price**: negative is a credit.
    """
    if order.position_intent is not None:
        return _SIDE_FOR_INTENT[order.position_intent]
    if not order.legs:
        return None
    intents = [leg.position_intent for leg in order.legs]
    if any(intent is None for intent in intents):
        return None
    opening = [intent in _OPENING_INTENTS for intent in intents]
    if len(set(opening)) != 1:
        # A roll is an open and a close in one order; it has no single action
        # and PRD §8.2 already defers rolling.
        return None
    price = order.limit_price
    if price is None or price == 0:
        return None
    credit = price < 0
    if opening[0]:
        return "STO" if credit else "BTO"
    return "STC" if credit else "BTC"


def _contract_label(order: Order) -> str | None:
    """The display label for an order's instrument, or ``None`` if unreadable."""
    symbols = (
        [leg.symbol for leg in order.legs] if order.legs else [order.symbol]
    )
    contracts: list[tuple[OccSymbol, PositionSide]] = []
    for index, symbol in enumerate(symbols):
        try:
            occ = parse_occ_symbol(symbol)
        except ValueError:
            return None
        leg = order.legs[index] if order.legs else order
        intent = leg.position_intent
        side = (
            PositionSide.SHORT
            if intent in (PositionIntent.SELL_TO_OPEN, PositionIntent.BUY_TO_CLOSE)
            else PositionSide.LONG
        )
        contracts.append((occ, side))
    price = order.limit_price
    direction: Direction = "short" if price is not None and price < 0 else "long"
    return _describe(
        contracts,
        direction=direction,
        expiry=min(occ.expiration for occ, _ in contracts),
    )


def _working_model(
    order: Order, owner: Mapping[str, str], *, correlation_id: str
) -> WorkingOrder | None:
    side = _order_side(order)
    if side is None:
        _refuse(
            order,
            "no position intent: `buy` is both buy-to-open and buy-to-close",
            correlation_id=correlation_id,
        )
        return None
    order_type = _WORKING_ORDER_TYPES.get(order.order_type)
    if order_type is None:
        _refuse(
            order,
            f"order type {order.order_type!r} does not rest in the market",
            correlation_id=correlation_id,
        )
        return None
    time_in_force = _TIME_IN_FORCE.get(order.time_in_force)
    if time_in_force is None:
        _refuse(
            order,
            f"time in force {order.time_in_force!r} is not one an option takes",
            correlation_id=correlation_id,
        )
        return None
    quantity = order.quantity
    if quantity is None or quantity <= 0 or quantity != quantity.to_integral_value():
        _refuse(
            order,
            f"quantity {quantity!r} is not a whole number of contracts",
            correlation_id=correlation_id,
        )
        return None
    label = _contract_label(order)
    if label is None:
        _refuse(
            order,
            f"symbol {order.symbol!r} is not an option contract",
            correlation_id=correlation_id,
        )
        return None

    symbols = [leg.symbol for leg in order.legs] if order.legs else [order.symbol]
    owners = {owner[symbol] for symbol in symbols if symbol in owner}
    closing = side in ("STC", "BTC")
    position_id = owners.pop() if closing and len(owners) == 1 else None
    return WorkingOrder(
        id=order.id,
        position_id=position_id,
        # Exactly one of the two is set: an opening order has no position to
        # belong to, which is the whole difference between them. On a
        # multi-leg opening order this keys on the first leg -- the field is
        # one string and a structure is several, so the full shape lives in
        # `contract` and this stays a key rather than a description.
        contract_key=None if position_id is not None else symbols[0],
        contract=label,
        side=side,
        order_type=order_type,
        quantity=int(quantity),
        limit_price=order.limit_price,
        stop_price=order.stop_price,
        time_in_force=time_in_force,
        placed_at=order.submitted_at or order.created_at,
        # Alpaca writes an activity row on a **fill**, so an unfilled order
        # has none. The order's own id is the handle a pending row would key
        # on -- a real id for the same event rather than an invented one.
        activity_id=order.id,
    )
