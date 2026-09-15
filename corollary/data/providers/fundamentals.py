"""``FundamentalsProvider`` -- the vendor boundary for company reference data.

The same rule ``interface.py`` states for market data, applied to the one
figure Alpaca does not carry. CLAUDE.md: *"Provider abstraction is mandatory.
Swapping Alpaca for ThetaData or Polygon should be a config change, not a
refactor."* Nothing in this module knows that Finnhub exists; the one
implementation that does lives next door in ``finnhub.py``.

Why this is a second interface rather than three methods on
:class:`~corollary.data.providers.interface.MarketDataProvider`
------------------------------------------------------------------

Because they are two vendors with two keys, two hosts and two rate budgets,
and they fail independently. Market cap is **supplementary**: the Markets
table is a table of prices and volumes that happens to carry one column of
company reference data, and the price feed going down is a different event
from the reference feed going down. Folding them into one interface would
make ``AlpacaProvider`` either implement a method it cannot answer or raise
from one -- and a caller holding a single object has no way to degrade one
half of it.

The three answers, and why "absent" is not one of them
------------------------------------------------------

:class:`MarketCap` carries a :class:`MarketCapStatus` alongside the value
because **null is the wire's answer to three different questions** and the
engine must not lose the distinction before it reaches a log:

``REPORTED``
    A figure. The only case with a value.
``NOT_FILED``
    The vendor answered and has no market capitalisation for this symbol. A
    **fund** is the case that matters: an ETF has no shares outstanding to
    multiply, files none, and its market cap is legitimately ``null``.
    CLAUDE.md, on the frontend that already renders this: *"a fund's
    ``marketCap`` is ``null``, not 0, and it sorts last in both directions
    rather than being coerced -- coerced to zero it would sort SPY to the
    top of an ascending list and state, in a column of dollars, that a fund
    is worth nothing."*
``UNAVAILABLE``
    The question could not be asked. No key, a 429, a 500, a timeout, a body
    that did not parse. The reason travels in :attr:`MarketCap.note`.

All three serialize to the same ``null``, and that is correct -- the wire
cannot say "not fetched yet" in a column of dollars without inventing a
vocabulary the client does not have. What the distinction buys is the two
things rule 8 is about: **a failure is logged as a failure**, naming its
cause, where a fund's absence is not logged as one at all; and a caller's
cache can hold an answer while retrying a failure, instead of blanking a
column for a day over a transient 500.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

logger = logging.getLogger(__name__)

#: The unit :meth:`MarketCap.reported` rounds to. See its docstring.
WHOLE_DOLLAR: Final = Decimal(1)

__all__ = [
    "WHOLE_DOLLAR",
    "FundamentalsError",
    "FundamentalsProvider",
    "MarketCap",
    "MarketCapStatus",
    "UnavailableFundamentals",
]


class FundamentalsError(RuntimeError):
    """The fundamentals vendor could not answer, and the caller must not guess.

    Its own type rather than
    :class:`~corollary.data.providers.interface.ProviderError`: the two
    vendors fail independently, and a caller that degrades one column on a
    fundamentals outage must not swallow a market-data outage by accident.

    Note that :meth:`FundamentalsProvider.market_caps` does **not** raise this
    for a per-symbol failure -- that is a :attr:`MarketCapStatus.UNAVAILABLE`
    entry, so one bad symbol cannot cost the other twenty-five their answers.
    It is raised for a condition that makes the whole call meaningless, such
    as a malformed batch request.
    """


class MarketCapStatus(StrEnum):
    """Why a market cap is what it is. See the module docstring."""

    #: A figure, in :attr:`MarketCap.value`.
    REPORTED = "reported"
    #: The vendor answered and has none -- a fund, or a symbol it does not
    #: cover. Legitimately null.
    NOT_FILED = "not_filed"
    #: The question could not be asked. :attr:`MarketCap.note` says why.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MarketCap:
    """One symbol's market capitalisation, in **whole units of USD**.

    ``Decimal``, never ``float``, and the unit is stated because the vendor's
    is not: Finnhub reports millions. The conversion happens at the vendor
    boundary so that no caller has to remember, and every caller of this type
    is reading dollars.

    :attr:`value` is non-``None`` **only** when :attr:`status` is
    :attr:`MarketCapStatus.REPORTED`; the constructors below are the
    supported way to build one and they enforce that.
    """

    symbol: str
    value: Decimal | None
    status: MarketCapStatus
    #: Why the value is absent, when it is absent for a reason worth logging.
    #: Empty on :attr:`MarketCapStatus.REPORTED`.
    note: str = ""

    @classmethod
    def reported(cls, symbol: str, value: Decimal) -> "MarketCap":
        """A figure, **rounded to whole dollars here rather than by the caller**.

        The rounding is not cosmetic, and it is on the constructor rather
        than in one vendor's adapter so that a second fundamentals vendor
        cannot reintroduce it by forgetting.

        **A non-positive or non-finite figure is refused outright**, for
        exactly that reason and with more at stake. ``REPORTED`` is the one
        status carrying a value, so this is the only place a number enters
        the dollar column, and ``0`` is the number that renders, sorts to the
        top of an ascending list and states that a live company is worth
        nothing -- the fund error quoted in the module docstring, one row
        lower down. ``finnhub.market_cap_from_profile`` declines a
        non-positive vendor figure before it gets here, with the
        vendor-specific logging that answer needs; the *rule* lives on the
        type so decision 12's SEC EDGAR path cannot arrive without it. A
        caller that trips this has a bug, and a ``ValueError`` out of a
        constructor is how it finds out; at the vendor boundary the residual
        catch records it as an internal fault rather than a vendor outage.

        Finnhub computes market cap in a double and emits up to seventeen
        significant digits -- ``2913456.7891234567`` millions is a real
        shape. Carried through to the API boundary, a value with that many
        digits cannot round-trip a ``float``, and
        ``schemas._money_to_json`` logs ``money_serialization_lossy`` at
        WARNING every time it serialises one. That check is justified on the
        premise that *"every figure this app serves is far inside"* a
        double's range; a market cap is the first figure in the app that is
        not, and a warning on every poll for a display-only column is how
        the log a rule-8 rejection has to be findable in stops being read.

        Fractional dollars on a market capitalisation are fake precision
        anyway -- the underlying share count is a quarterly filing -- so the
        fix is to stop manufacturing the digits rather than to exempt the
        column from the check. Whole dollars up to about nine quadrillion
        round-trip a double exactly, and the largest real market cap is
        three orders of magnitude below that.

        Raises :class:`decimal.InvalidOperation` for a value too large to
        express as an integer in the current context. Callers at a vendor
        boundary catch ``ArithmeticError`` and report it as a
        :class:`FundamentalsError`; see ``finnhub.market_cap_from_profile``.
        """
        # ``ROUND_HALF_UP``, and both halves of that are deliberate.
        #
        # *Nearest*, rather than ``ROUND_DOWN``: this column sorts, and
        # truncation biases every figure the same direction, which is the
        # bias that can order two near-equal caps wrongly. Half-*up* rather
        # than ``decimal``'s default half-even, because half-even's
        # bias-cancelling only pays when rounded values are summed, and these
        # are displayed one per row and never added together -- while half-up
        # is what a reader re-checking the arithmetic by hand computes. Every
        # value reaching the ``quantize`` is positive, because zero and below
        # are refused immediately below, so half-up and half-ceiling coincide
        # and there is no sign asymmetry to reason about.
        #
        # Named at the call site rather than left to the default because a
        # bare ``quantize`` reads ``decimal.getcontext().rounding`` -- global,
        # mutable process state. A money figure that moves because something
        # else in the process changed a global is not reproducible.
        #
        # Finite first, and not only for tidiness: ``Decimal("NaN") <= 0``
        # raises ``InvalidOperation`` rather than answering, so the ordering
        # is what makes the comparison below safe to write as one. ``NaN``
        # also survives ``quantize`` untouched -- it is returned, not raised
        # on -- and would serialise as ``null``, which is both harmless and
        # indistinguishable from a fund. ``Infinity`` would have overflowed
        # the ``quantize``; refusing it here means both non-finite shapes
        # fail on one stated line instead of one line and one accident.
        if not value.is_finite():
            raise ValueError(
                f"market cap for {symbol} is {value}, which is not a finite "
                "figure. Absence is MarketCapStatus.NOT_FILED or UNAVAILABLE, "
                "never a value."
            )
        if value <= 0:
            raise ValueError(
                f"market cap for {symbol} is {value}, which is not a value. A "
                "dollar column may not carry a zero or a negative: absence is "
                "MarketCapStatus.NOT_FILED, which serialises as null and sorts "
                "last, and a figure nobody could fetch is UNAVAILABLE."
            )
        return cls(
            symbol=symbol,
            value=value.quantize(WHOLE_DOLLAR, rounding=ROUND_HALF_UP),
            status=MarketCapStatus.REPORTED,
        )

    @classmethod
    def not_filed(cls, symbol: str, note: str = "") -> "MarketCap":
        """No market cap exists for this symbol. A fund is the usual case."""
        return cls(
            symbol=symbol, value=None, status=MarketCapStatus.NOT_FILED, note=note
        )

    @classmethod
    def unavailable(cls, symbol: str, note: str) -> "MarketCap":
        """The question could not be asked. ``note`` is the cause, for the log."""
        return cls(
            symbol=symbol, value=None, status=MarketCapStatus.UNAVAILABLE, note=note
        )

    @property
    def is_answered(self) -> bool:
        """Whether the vendor answered -- with a figure or with "none".

        The predicate a cache wants: an answer is good for the trading date,
        a failure is worth retrying. Reading ``value is not None`` instead
        would retry every fund forever.
        """
        return self.status is not MarketCapStatus.UNAVAILABLE


class FundamentalsProvider(ABC):
    """Company reference data, vendor-agnostically.

    Async for the reason ``MarketDataProvider`` is: one process, one event
    loop, and a synchronous call here would block the loop that answers the
    API.

    Deliberately small. PRD section 7 gives this vendor three more jobs -- news,
    the economic and earnings calendar, and analyst consensus -- and each of
    those is its own method on this interface when its phase arrives, rather
    than a generic ``fetch(endpoint)`` that would put the vendor's URL shapes
    back in front of the engine.
    """

    @abstractmethod
    async def market_caps(self, symbols: Sequence[str]) -> dict[str, MarketCap]:
        """Market capitalisation per symbol, in whole USD.

        **Answers for every symbol handed in**, including the ones that
        failed: a missing key would be indistinguishable from a fund at the
        call site, and the caller's cache is entitled to know the difference.
        Implementations never raise for a single symbol's failure -- that is a
        :attr:`MarketCapStatus.UNAVAILABLE` entry with a stated cause.
        """


class UnavailableFundamentals(FundamentalsProvider):
    """A provider that cannot answer, and says so once per call.

    The shape of "there is no ``FINNHUB_API_KEY``". A null object rather than
    a ``None`` dependency, for two reasons:

    * The market-cap column is **supplementary**. A stock table that 503s
      because a reference-data key is unset would take prices, volumes and
      the whole screener down over one column -- and the null path for that
      column is designed, per decision 7.
    * ``None`` would put an ``if provider is not None`` at every call site,
      and the one that forgot it would be the one that crashed.

    Every entry is ``UNAVAILABLE``, never ``NOT_FILED``: an unset key is not
    a statement that Apple has no market cap.

    One log line per call rather than one per symbol. Twenty-six identical
    warnings a poll is how a log stops being read, and the condition is a
    single fact about the process, not twenty-six facts about symbols.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def market_caps(self, symbols: Sequence[str]) -> dict[str, MarketCap]:
        wanted = tuple(dict.fromkeys(s.strip() for s in symbols if s.strip()))
        if wanted:
            logger.warning(
                "no fundamentals provider: %d symbol(s) have no market cap. %s",
                len(wanted),
                self.reason,
                extra={
                    "event": "fundamentals_unavailable",
                    "rule": (
                        "an unconfigured fundamentals vendor serves null and "
                        "logs it; it never claims a symbol has no market cap"
                    ),
                    "symbols": list(wanted),
                    "cause": self.reason,
                },
            )
        return {
            symbol: MarketCap.unavailable(symbol, self.reason) for symbol in wanted
        }
