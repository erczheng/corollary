"""``MarketDataProvider`` — the vendor boundary for market data.

CLAUDE.md: *"Provider abstraction is mandatory. Everything goes through
``MarketDataProvider``. Swapping Alpaca for ThetaData or Polygon should be a
config change, not a refactor."*

Nothing in this module knows that Alpaca exists. The types below are the
vocabulary the rest of the engine speaks — the scanner, the risk manager and
the API routes see these and never a vendor payload — and the one
implementation that does know about Alpaca lives next door in ``alpaca.py``.

Three things this file is deliberate about
------------------------------------------

**Money is ``Decimal``, and a missing price is ``None``, never zero.** Alpaca
documents ``bp: 0`` as *"the security has no active bid"*, which is a
different statement from "the bid is zero dollars". Collapsing the two makes
:attr:`Quote.mid` half the ask on a one-sided market, and that invented mid
would then be the input to a derived implied volatility — an invented number
built on an invented number, printed under the word "IV".

**Open interest is ``int | None`` and the ``None`` is load-bearing.** Absent
is never 0: a zero says "nobody holds this contract", which is a claim about
the market, where a null is a claim about the data. Note that the Phase 2
design's stronger claim -- that ``open_interest`` is null on *every* contract
on this plan -- **did not survive re-probing on 2026-09-10**: 98 of 100 NVDA
contracts came back populated, along with ``open_interest_date`` and
``close_price``. The spec's own "Not verified" list anticipated this, asking
whether the nulls were the plan or a settlement cycle the account had not yet
had; the answer is the latter. PRD §8.4's "highest open interest" screen has a
ranking key after all, and decision 10's "report open interest as absent" is
now over-conservative. It is still right to render a null *as* null on the
contracts that have one.

**The multiplier is per contract and comes from the vendor.** Never the
frontend's ``CONTRACT_MULTIPLIER = 100``. After a split or special dividend an
adjusted contract deliverable is not 100 shares, and sizing that assumes it is
computes max loss wrong — the failure rule 4 exists to prevent. ``size`` is a
*separate* field and Alpaca's spec says explicitly that it must not be used as
a multiplier; both are carried here so the distinction survives.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from corollary.instruments import OptionType, is_adjusted_root
from corollary.pricing.blackscholes import Greeks

__all__ = [
    "AnalyticsSource",
    "Bar",
    "BarTimeframe",
    "FeedAccessError",
    "MarketDataProvider",
    "OptionContract",
    "OptionDeliverable",
    "OptionSnapshot",
    "OptionType",
    "ProviderError",
    "Quote",
    "RateLimitedError",
    "StockSnapshot",
    "Trade",
]


class ProviderError(RuntimeError):
    """The provider could not answer, and the caller must not guess."""


class FeedAccessError(ProviderError):
    """The plan does not entitle this feed.

    Its own type because the remedy is different from every other failure:
    ``feed=opra`` inside the last 15 minutes returns an auth error rather than
    empty data, and the fix is a subscription, not a retry. CLAUDE.md: *"if a
    call fails on feed access, check the plan before debugging the code."*
    """


class RateLimitedError(ProviderError):
    """The vendor returned 429 despite the local budget.

    Reaching this means the local :class:`~corollary.ratelimit.TokenBucket`
    and the server disagree — another process sharing the key, or a restart
    that reset the bucket while the server's window had not rolled. Worth
    surfacing rather than retrying blind.
    """


class BarTimeframe(StrEnum):
    """The aggregations this app asks for, in Alpaca's spelling.

    A ``StrEnum`` rather than free-form strings so a typo is a collection
    error rather than an HTTP 400 three layers down. The vendor accepts far
    more than these; the list is what the scanner and the charts need.
    """

    MINUTE = "1Min"
    FIVE_MINUTE = "5Min"
    FIFTEEN_MINUTE = "15Min"
    HOUR = "1Hour"
    DAY = "1Day"
    WEEK = "1Week"
    MONTH = "1Month"


class AnalyticsSource(StrEnum):
    """Where an option's IV and greeks came from.

    Carried on every :class:`OptionSnapshot` so the UI can say which, in
    §8.5's words. The three cases are genuinely different claims and the
    screen should not present them identically:

    ``VENDOR``
        The feed supplied them. Only reachable on OPRA.
    ``DERIVED``
        Computed locally with Black-Scholes from the quote mid, per decision
        10. Correct method, **thin and delayed inputs** on the Basic plan: the
        contract mid is 15 minutes stale on the ``indicative`` feed, and the
        spot it is solved against is an **IEX** mid — roughly 2.5% of US
        volume rather than a consolidated price. On a thin underlying that is
        a materially different caveat from "delayed", and it is the reason a
        derived greek is a display value and not a sizing input.
    ``UNAVAILABLE``
        Neither was possible — no two-sided quote, no underlying price, or the
        mid sits outside the no-arbitrage bracket. The reason travels in
        :attr:`OptionSnapshot.analytics_note`.
    """

    VENDOR = "vendor"
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Quote:
    """A best bid and offer at an instant.

    ``bid`` and ``ask`` are ``None`` when that side of the book is empty.
    """

    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: int
    ask_size: int
    at: datetime

    @property
    def mid(self) -> Decimal | None:
        """The midpoint, or ``None`` on a one-sided or crossed market.

        A crossed quote (bid above ask) is a data error rather than a
        tradeable market; returning its midpoint would launder the error into
        a plausible price. On the ``indicative`` feed, which is a *modified*
        derivative of OPRA rather than OPRA itself, this is worth checking
        rather than assuming.
        """
        if self.bid is None or self.ask is None:
            return None
        if self.bid > self.ask:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class Trade:
    """A single print."""

    symbol: str
    price: Decimal
    size: int
    at: datetime


@dataclass(frozen=True, slots=True)
class Bar:
    """An OHLCV aggregate over one interval.

    ``at`` is the interval's **opening** timestamp, which is Alpaca's
    convention and the one the no-look-ahead rule depends on: an indicator
    reading a bar stamped ``09:30`` must not be able to see what happened at
    09:31.
    """

    symbol: str
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trade_count: int
    vwap: Decimal | None


@dataclass(frozen=True, slots=True)
class StockSnapshot:
    """Everything the snapshot endpoint returns for one equity.

    Every member is optional. A symbol halted all session has no
    ``minute_bar``; a newly listed one has no ``previous_daily_bar``.
    """

    symbol: str
    latest_quote: Quote | None
    latest_trade: Trade | None
    minute_bar: Bar | None
    daily_bar: Bar | None
    previous_daily_bar: Bar | None

    @property
    def price(self) -> Decimal | None:
        """The best available spot, in falling order of freshness.

        Quote mid first, then the last print, then the daily close. Stated as
        an explicit order rather than left to each call site, because two call
        sites picking differently is how the Markets table and an Activity row
        end up disagreeing about AAPL — the same failure the single
        ``underlyings`` map exists to prevent on the frontend.
        """
        if self.latest_quote is not None and self.latest_quote.mid is not None:
            return self.latest_quote.mid
        if self.latest_trade is not None:
            return self.latest_trade.price
        if self.daily_bar is not None:
            return self.daily_bar.close
        return None

    @property
    def previous_close(self) -> Decimal | None:
        if self.previous_daily_bar is None:
            return None
        return self.previous_daily_bar.close


@dataclass(frozen=True, slots=True)
class OptionSnapshot:
    """One option contract's latest market state, plus derived analytics."""

    symbol: str
    latest_quote: Quote | None
    latest_trade: Trade | None
    minute_bar: Bar | None
    daily_bar: Bar | None
    previous_daily_bar: Bar | None
    implied_volatility: Decimal | None
    greeks: Greeks | None
    analytics_source: AnalyticsSource
    #: Why analytics are absent, when they are. Empty otherwise.
    analytics_note: str = ""

    @property
    def volume(self) -> int | None:
        """Session volume, from the daily bar. Absent before the first print."""
        return None if self.daily_bar is None else self.daily_bar.volume


@dataclass(frozen=True, slots=True)
class OptionDeliverable:
    """What one contract actually delivers on exercise.

    Standard contracts have exactly one of these: 100 shares of the
    underlying. Adjusted contracts can have several, and that is the only
    place the adjustment is visible in Alpaca's data — see
    :attr:`OptionContract.has_nonstandard_deliverable`.
    """

    type: str
    symbol: str
    #: For a cash deliverable this is the cash amount. ``None`` when
    #: settlement is delayed and the amount is not yet determined.
    amount: Decimal | None
    allocation_percentage: Decimal | None
    settlement_type: str
    settlement_method: str
    delayed_settlement: bool


@dataclass(frozen=True, slots=True)
class OptionContract:
    """The reference data for one contract. Not a price.

    Sourced from the *trading* API rather than the market data API, which is
    why a chain costs a request against each of the two rate-limit buckets.
    """

    symbol: str
    underlying_symbol: str
    root_symbol: str
    expiration: date
    option_type: OptionType
    strike: Decimal
    style: str
    #: Contracts per unit of premium. **100 for standard contracts and
    #: something else for adjusted ones.** Never assume.
    multiplier: Decimal
    #: Shares deliverable on exercise. Alpaca's spec: *"This field should
    #: **not** be used as a multiplier."* Carried so the distinction survives
    #: to anyone tempted to use it.
    size: Decimal
    #: Absent, not zero — see the module docstring. Populated on the great
    #: majority of contracts on this plan: 98 of the 100 in
    #: ``tests/fixtures/alpaca/option_contracts_nvda.json``.
    open_interest: int | None
    open_interest_date: date | None
    close_price: Decimal | None
    close_price_date: date | None
    tradable: bool
    status: str
    name: str

    #: Empty unless the request asked for them (``show_deliverables=true``).
    deliverables: tuple[OptionDeliverable, ...] = ()

    @property
    def is_adjusted(self) -> bool:
        """True when this is not a standard contract.

        ``root_symbol != underlying_symbol``, and **that test is the only one
        that works.** Checked against live data on 2026-09-10: all 270 active
        adjusted contracts reachable from this account — every ``GME1`` and
        ``XRX1`` — report ``multiplier: "100"`` and ``size: "100"``, identical
        to a standard contract.

        So reading ``multiplier`` per contract is *necessary but not
        sufficient*, which is sharper than CLAUDE.md's phrasing. A ``GME1``
        contract delivers 100 GME shares **plus 10 GME.WS warrants**, and
        neither ``multiplier`` nor ``size`` says so — only ``root_symbol``
        and :attr:`deliverables` do. Sizing against the reported 100 would
        value the deliverable at the share leg alone and compute max loss
        wrong, which is the failure rule 4 exists to prevent.
        """
        return is_adjusted_root(self.root_symbol, self.underlying_symbol)

    @property
    def has_nonstandard_deliverable(self) -> bool | None:
        """Whether the deliverable departs from 100 shares of the underlying.

        ``None`` when deliverables were not requested — *unknown* is a third
        answer and must not collapse into ``False``. Asking a question the
        data cannot answer and getting "no" is how an adjusted contract gets
        sized as a standard one.
        """
        if not self.deliverables:
            return None
        if len(self.deliverables) != 1:
            return True
        only = self.deliverables[0]
        return (
            only.type != "equity"
            or only.symbol != self.underlying_symbol
            or only.amount != Decimal(100)
        )


class MarketDataProvider(ABC):
    """Quotes, snapshots, bars, chains and contracts, vendor-agnostically.

    Async because the design spec runs one process with an asyncio event loop
    and a 2s market poll; a synchronous provider would block the same loop
    that answers the API.

    Not in this interface, deliberately:

    * **Streaming.** The 30-symbol websocket budget manager is its own
      component (``engine/stream.py``) with its own priority ordering and its
      own "N symbols not streamed" surface. Folding a subscription method in
      here would put budget policy behind a vendor interface.
    * **Anything that places an order.** Rule 1 — there is exactly one path to
      ``submit_order`` and it is not a data provider.
    """

    @abstractmethod
    async def latest_stock_quotes(
        self, symbols: Sequence[str]
    ) -> dict[str, Quote]:
        """Best bid/offer per symbol. Symbols with no quote are absent."""

    @abstractmethod
    async def stock_snapshots(
        self, symbols: Sequence[str]
    ) -> dict[str, StockSnapshot]:
        """Latest trade, quote and bars per symbol."""

    @abstractmethod
    async def stock_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:
        """Historical equity bars, oldest first, paginated to completion.

        ``limit`` is a **cap on the total number of bars returned across every
        symbol**, not a page size. That distinction is the whole point of
        stating it here: Alpaca's own ``limit`` parameter is per page, so an
        implementation that passes it through and then follows the page token
        to exhaustion has a parameter that reads like a bound and bounds
        nothing — a caller writing ``limit=200`` to be careful gets an
        unbounded request instead. An implementation must make at most
        ``ceil(limit / its page size)`` requests. ``None`` means the window
        decides; a non-positive value is a ``ValueError``.

        ``end`` is *historical*. A provider whose free tier embargoes recent
        data must not silently answer from a thinner feed — see
        :meth:`option_bars` and CLAUDE.md on ``min_avg_volume``.
        """

    @abstractmethod
    async def latest_option_quotes(
        self, symbols: Sequence[str]
    ) -> dict[str, Quote]:
        """Best bid/offer per OCC contract symbol."""

    @abstractmethod
    async def option_bars(
        self,
        symbols: Sequence[str],
        *,
        timeframe: BarTimeframe = BarTimeframe.DAY,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, list[Bar]]:
        """Historical option bars.

        Bars are all there is: Alpaca has no historical options *quotes*
        endpoint at all, and trades reach back only 7 days. Anything older
        than a week is bars and nothing else, which is why the backtester
        needs the explicit spread model in ``backtest/spread.py``.

        ``limit`` means what it means on :meth:`stock_bars`: a cap on the
        total bars returned, not a page size. One name, one meaning.
        """

    @abstractmethod
    async def option_chain(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
    ) -> dict[str, OptionSnapshot]:
        """The chain for one underlying, keyed by OCC symbol.

        Includes derived IV and greeks where a mid and a spot exist. Adjusted
        contracts are excluded — their deliverable is not 100 shares and the
        analytics would be quoted against the wrong contract size.
        """

    @abstractmethod
    async def option_contracts(
        self,
        underlying: str,
        *,
        expiration_lte: date | None = None,
        expiration_gte: date | None = None,
        strike_gte: Decimal | None = None,
        strike_lte: Decimal | None = None,
        option_type: OptionType | None = None,
        include_adjusted: bool = False,
        show_deliverables: bool = False,
    ) -> list[OptionContract]:
        """Reference data for an underlying's contracts.

        ``include_adjusted`` defaults to ``False``: an adjusted contract is
        excluded from the universe unless a caller asks for it by name and has
        therefore thought about the multiplier.
        """
