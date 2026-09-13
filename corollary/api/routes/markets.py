"""Markets: the stock table, the option chain, and the quoted underlyings.

Three read-only routes over :class:`~corollary.data.providers.interface.MarketDataProvider`.
Nothing here imports a vendor SDK, names a feed, or reaches a broker -- the
provider is the whole surface, which is what keeps swapping Alpaca for
ThetaData a config change rather than a refactor.

Decision 10, as amended, is the reason this module exists in the shape it does
----------------------------------------------------------------------------

The first probe sampled two contracts and generalised. Both its conclusions
were wrong with one shape: ``/v2/options/contracts`` and the snapshots endpoint
both return contracts **ordered by strike**, so a small ``limit`` returns the
deep-ITM tail -- the least liquid, most recently listed end of the chain -- and
nothing about it generalises.

* **``impliedVolatility`` and ``greeks`` are served on ``indicative``** for the
  contracts where Alpaca's own solve succeeds: 19 of 100 on NVDA, 12 of 100 on
  AAPL, concentrated near the money. Partial, not absent. So vendor analytics
  pass through where they exist, are derived locally where they do not, and
  **every row records which of the two it is** -- :attr:`OptionContract.iv_source`.
  A chain silently mixing measured and derived values is worse than either
  alone.
* **``open_interest`` is populated** -- 98 of 100 NVDA, 79 of 100 AAPL -- so
  PRD 8.4's "highest open interest" screen has a ranking key and it is served
  here. A null still means absent, survives as null, and is never coerced and
  never ranked on.

The honest cost of the derived half: greeks and IV computed from a
15-minute-delayed mid, solved against an IEX spot, are *delayed* analytics.
Correct method, thin and stale inputs. Fine for a column. Not fine for sizing,
which is why the label travels with the number.

**The one thing that is never an option is inventing values.** That principle
decides every awkward case in this file, and the awkward cases are not rare:

* 49 of the 100 contracts in ``option_chain_nvda_page1`` have **no bid at all**
  (``bp: 0``, which Alpaca documents as *"the security has no active bid"*).
  They serve ``null``, never ``0``.
* 40 of the 100 in ``page2`` have no previous daily bar, so they have no
  change to report.
* A stock with no price at all is **omitted** rather than served as a row of
  nulls, and the omission is logged. That is the one absence handled by
  removal rather than by ``null``: a stock table row whose entire purpose is a
  price has nothing to show without one, and the empty table is a state the
  page already draws.

Two budgets, and why anything is cached
---------------------------------------

``data.alpaca.markets`` and ``paper-api.alpaca.markets`` carry **separate**
200/min buckets. The Markets page polls ``/stocks`` every 2 seconds -- 30
requests a minute -- and a chain is on demand, costing one request against each
bucket plus one for spot.

What would break that budget is the *daily* series: an average daily volume
needs 90 days of bars and an underlying's chart needs 400, and re-downloading
either on a 2s poll multiplies 30/min by the page count. Both move once a
session, so both are cached with the **trading date in New York** as the key.
The cache lives on ``app.state`` rather than in a module global, so two apps in
one test process cannot see each other's.

A third figure is cached for a different reason. Today's session volume is
the numerator of relative volume, and it has to be measured on the same feed
as the average it is divided by -- so it is a bars request too, not the daily
bar already sitting on the snapshot. It moves through the session, so the
trading date is the wrong key; it also cannot move faster than the historical
feed's fifteen-minute embargo, so re-reading it every 2s would buy nothing.
:class:`IntradayCache` holds it for :data:`SESSION_VOLUME_TTL`, and
:func:`_fetch_session_volumes` carries the measurement.

That TTL stops applying the moment the figure stops moving. The market is
shut more than it is open -- two days in seven, plus nine holidays and the
hours either side of every bell -- and a closed market's volume is final, so
holding it for the trading date instead turns a request a minute into a
request a day. Which of the two a given moment is in comes from the market
calendar, in :func:`_session_state`, and never from a clock reading of 16:00:
NYSE closes at 13:00 ET on the Friday after Thanksgiving.

The same closure is why the daily series is cached as a *series* rather than
as the average it folds down to. Outside a session there is no bar for today,
and a Volume column keyed strictly on today is blank for every symbol on the
page -- honest, and useless on a Saturday. The last completed session is
equally honest and more use, and it is already in hand as the last entry of
that series, so the fallback costs no request at all. Both halves of the
ratio then come out of one response, which is the strongest form of the
same-feed rule there is. :func:`_volume_reading` assembles it, and keeps the
session that supplied the numerator out of its own denominator.

Two shapes of the response that are choices rather than defaults
----------------------------------------------------------------

**No ``?account=``.** These three routes take no account mode and depend on no
broker. A quote is a quote: Paper and Cash hold different money and different
positions, but not different prices, and adding the parameter would suggest
otherwise. ``AccountModeDep`` belongs to the routes that read a book.

**No ``Page``.** All three return plain lists. The chain is a *ladder* -- sorted
by expiry, then right, then strike -- and a server-side page boundary cut
through it would arrive at the client as a sorted fragment that cannot be
re-sorted without fetching the rest. The window (60 DTE, 15% of spot) is what
bounds the size, which is the bound that also saves the requests.

Things that are deliberately not here
-------------------------------------

**Market cap.** Finnhub, step 9. Every ``marketCap`` this module serves is
``None``. A fund's is legitimately null and a company's is merely not fetched
yet; the wire cannot distinguish them, and inventing either is the failure
PRD 8.5 names.

**Greeks.** The provider computes them and the schema does not carry them:
PRD 8.2 deferred greeks in the ticket, and a delayed greek is a display value
rather than a sizing input. The IV column is the one piece decision 10 asks
for, and it arrives labelled.

**``ChainSpec``.** Mirrored in ``schemas.py`` for contract completeness, and it
has no server-side meaning -- ``baseIv``, ``liquidity`` and ``seed`` are Phase 1
fixture-generator parameters. Serving them would be inventing values in the
most literal sense available.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Annotated, Final, Generic, TypeVar
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Path, Query, Request

from corollary.api.deps import ApiError, ProviderDep
from corollary.api.schemas import (
    AnalyticsSource,
    IntradayPoint,
    OptionContract,
    OptionRight,
    PricePoint,
    SessionState,
    StockQuote,
    UnderlyingQuote,
)
from corollary.calendars import nyse_session_close
from corollary.data.providers.interface import (
    AnalyticsSource as ProviderAnalyticsSource,
)
from corollary.data.providers.interface import (
    Bar,
    BarTimeframe,
    MarketDataProvider,
    OptionSnapshot,
    StockSnapshot,
)
from corollary.data.providers.interface import (
    OptionContract as ContractTerms,
)
from corollary.instruments import OccSymbol, OptionType, parse_occ_symbol

__all__ = [
    "UNDERLYING_SYMBOLS",
    "UNIVERSE",
    "UNIVERSE_SYMBOLS",
    "MarketCaches",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/markets", tags=["markets"])


# --------------------------------------------------------------------------
# The universe
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    """One tradable name and the label the table shows for it."""

    symbol: str
    name: str


#: The screener's universe, and the only source of a company name in this
#: codebase.
#:
#: **Curated reference data, not a measurement.** No market-data endpoint
#: carries a name; Alpaca's ``/v2/assets`` does, and it is a trading-host call
#: this phase has no other reason to make. Until something fetches it, the
#: names are written down once, here, and they mirror the Phase 1 fixture's
#: list symbol for symbol so the migration is a data-source swap rather than a
#: change of what is on screen.
#:
#: It is also why ``/stocks`` refuses a symbol it does not know: a symbol
#: outside this list has no name, so it has no row. The chain route makes the
#: opposite choice for the opposite reason -- see :func:`chain`.
UNIVERSE: Final[tuple[UniverseEntry, ...]] = (
    UniverseEntry("AAPL", "Apple Inc."),
    UniverseEntry("MSFT", "Microsoft Corp."),
    UniverseEntry("NVDA", "NVIDIA Corp."),
    UniverseEntry("AMZN", "Amazon.com Inc."),
    UniverseEntry("GOOGL", "Alphabet Inc. Class A"),
    UniverseEntry("META", "Meta Platforms Inc."),
    UniverseEntry("AVGO", "Broadcom Inc."),
    UniverseEntry("TSLA", "Tesla Inc."),
    UniverseEntry("LLY", "Eli Lilly and Co."),
    UniverseEntry("WMT", "Walmart Inc."),
    UniverseEntry("JPM", "JPMorgan Chase & Co."),
    UniverseEntry("UNH", "UnitedHealth Group Inc."),
    UniverseEntry("XOM", "Exxon Mobil Corp."),
    UniverseEntry("COST", "Costco Wholesale Corp."),
    UniverseEntry("HD", "Home Depot Inc."),
    UniverseEntry("SPY", "SPDR S&P 500 ETF Trust"),
    UniverseEntry("QQQ", "Invesco QQQ Trust"),
    UniverseEntry("IWM", "iShares Russell 2000 ETF"),
    UniverseEntry("XLE", "Energy Select Sector SPDR Fund"),
    UniverseEntry("ARKK", "ARK Innovation ETF"),
    UniverseEntry("ARM", "Arm Holdings plc"),
    UniverseEntry("ALAB", "Astera Labs Inc."),
    UniverseEntry("RDDT", "Reddit Inc."),
    UniverseEntry("RBRK", "Rubrik Inc."),
    UniverseEntry("CRWV", "CoreWeave Inc."),
    UniverseEntry("CRCL", "Circle Internet Group Inc."),
)

UNIVERSE_BY_SYMBOL: Final[Mapping[str, UniverseEntry]] = {
    entry.symbol: entry for entry in UNIVERSE
}
UNIVERSE_SYMBOLS: Final[tuple[str, ...]] = tuple(
    entry.symbol for entry in UNIVERSE
)

#: The names a position can be written on -- the ones with a listed chain in
#: this app. A subset of :data:`UNIVERSE`, because one symbol has one price and
#: a second list is how the Markets table and an Activity row end up
#: disagreeing about AAPL.
UNDERLYING_SYMBOLS: Final[tuple[str, ...]] = (
    "AAPL",
    "MSFT",
    "NVDA",
    "QQQ",
    "SPY",
    "TSLA",
)


# --------------------------------------------------------------------------
# Windows, bands and precision
# --------------------------------------------------------------------------

EASTERN: Final = ZoneInfo("America/New_York")

#: Sessions in the average-volume window. The denominator of relative volume,
#: which is what "trending" means -- a different question from "most active",
#: where raw volume finds the same mega caps every session.
AVG_VOLUME_SESSIONS: Final = 30

#: How stale today's partial session volume may be before it is re-fetched.
#:
#: The **numerator** of relative volume, and it comes from the same
#: ``/v2/stocks/bars`` request family as the denominator -- see
#: :func:`_fetch_session_volumes` for why that is the whole point.
#:
#: Bounded below by the data rather than chosen for comfort: the provider
#: resolves a historical ``end`` to fifteen minutes ago, so today's partial
#: daily bar cannot move faster than that. Re-reading it on every 2s poll
#: would spend 30 requests a minute against ``data.alpaca.markets``'s 200 to
#: return the same integer.
#:
#: It expires only while the figure can still move. Once the session it
#: covers has finished -- and on a Saturday it finished before the page was
#: opened -- the entry is held for the trading date instead; see
#: :func:`_session_state` and :class:`IntradayCache`.
SESSION_VOLUME_TTL: Final = timedelta(seconds=60)

#: How long after a session's close its daily bar covers the whole session.
#:
#: The historical feed serves up to fifteen minutes ago, so a bar read at
#: 16:05 ET is short of the close it appears to report. Fifteen minutes is
#: therefore both when the figure stops moving and when it becomes worth
#: calling final.
SESSION_SETTLES_AFTER: Final = timedelta(minutes=15)

#: Calendar days fetched to find those sessions. Wide enough to cover
#: weekends and a holiday run without asking a calendar: the bars that come
#: back *are* the sessions, so the count is taken from the response rather
#: than from an assumption about the month.
AVG_VOLUME_LOOKBACK_DAYS: Final = 90

#: Calendar days of daily closes kept per underlying. 400 because at a
#: quarter the 3M, YTD, 1Y and All ranges all return the same points, and at a
#: year 1Y and All still do.
HISTORY_DAYS: Final = 400

#: The default chain window. CLAUDE.md's own download filter: 60 DTE and the
#: strikes within 15% of spot.
DEFAULT_MAX_DTE: Final = 60
DEFAULT_MONEYNESS_PCT: Final = 15

#: A window, spelled the way ``/api/account/history`` spells one: a count and
#: a unit. Same regex, deliberately -- two endpoints answering "how far back?"
#: in two grammars is how a client learns one and gets a vendor 400 from the
#: other. The unit for a year is ``A``, not ``Y``.
_PERIOD: Final = re.compile(r"^([1-9][0-9]{0,2})([DWMA])$")

#: The five resolutions this API serves, in Alpaca's portfolio-history
#: spelling, mapped to the provider's bar spelling.
#:
#: **The wire vocabulary is the account endpoint's, not the provider's.**
#: ``BarTimeframe`` says ``1Hour`` and ``1Day`` where portfolio history says
#: ``1H`` and ``1D``; the client should not have to know which route it is
#: talking to, so the translation happens here, once. Ordered fine to coarse,
#: which is what lets a refusal name the finest resolution that would fit.
_TIMEFRAMES: Final[Mapping[str, BarTimeframe]] = {
    "1Min": BarTimeframe.MINUTE,
    "5Min": BarTimeframe.FIVE_MINUTE,
    "15Min": BarTimeframe.FIFTEEN_MINUTE,
    "1H": BarTimeframe.HOUR,
    "1D": BarTimeframe.DAY,
}

#: Minutes covered by one bar, for the size estimate. ``1D`` is absent because
#: a daily bar is one per session whatever the session's length.
_TIMEFRAME_MINUTES: Final[Mapping[str, int]] = {
    "1Min": 1,
    "5Min": 5,
    "15Min": 15,
    "1H": 60,
}

#: The daily timeframe, spelled for the wire. The default, and the only one
#: that fills :attr:`UnderlyingQuote.history` rather than ``intraday``.
DAILY_TIMEFRAME: Final = "1D"

#: The default window: the 400 calendar days the daily cache already holds,
#: so the default response is exactly what it was before ``period`` existed.
DEFAULT_PERIOD: Final = f"{HISTORY_DAYS}D"

#: Minutes in a session **as the bars feed reports one**, not as the regular
#: session runs.
#:
#: Measured rather than assumed, and the measurement is the whole point: one
#: full session of NVDA at ``5Min`` comes back as **192 bars**, not the 78 that
#: 09:30--16:00 would give. Alpaca includes extended hours, roughly
#: 04:00--20:00 ET, so an estimate built on regular hours is 2.5x too
#: generous and every ceiling derived from it is 2.5x too loose.
#:
#: An upper bound rather than an exact figure: a half-day's extended session
#: is shorter, so the estimate over-counts there, which is the safe direction
#: for a ceiling. The *session count* underneath it is exact and comes from
#: the market calendar -- see :func:`_sessions_in`.
EXTENDED_SESSION_MINUTES: Final = 960

#: The most points one symbol's series may contain.
#:
#: **Expressed as "more points than any chart can render."** A chart in this
#: app is about 900 pixels wide, so 2,000 points is already better than two
#: per pixel and past the resolution a screen can show. Everything the range
#: control actually needs sits well inside it: a day at ``5Min`` is 192
#: points, a day at ``1Min`` is 960, a week at ``1H`` is 80, a year at ``1D``
#: is 252. What it refuses is only the *combination* of a long period with a
#: fine timeframe -- a year at ``5Min`` is ~48,000 points, which is fifty per
#: pixel, tens of megabytes across a list of symbols, and invisible data paid
#: for in full.
MAX_SERIES_POINTS: Final = 2_000

#: The most points one **response** may contain, across every symbol in it.
#:
#: A per-symbol ceiling is not a bound on this endpoint, because this endpoint
#: takes a list: twenty-six symbols each just inside the per-symbol limit is a
#: 52,000-point response. 20,000 is two things at once -- roughly 2 MB at the
#: measured 110 bytes per bar, and **two pages** at Alpaca's 10,000-bar
#: response cap, so a chart view costs at most two round trips against a
#: ``data.alpaca.markets`` budget the Markets page is already polling into.
#:
#: It clears every current caller: the six default underlyings over 400 days
#: of daily closes is ~1,650 points, and all twenty-six is ~7,150.
MAX_RESPONSE_POINTS: Final = 20_000

_CENTS: Final = Decimal("0.01")
_HUNDRED: Final = Decimal(100)

#: An equity symbol, optionally with a class suffix (``BRK.B``). Deliberately
#: *not* the OCC root pattern: an adjusted root such as ``AAPL1`` is a
#: property of a contract, never of an underlying, and accepting one here
#: would invite a chain request for a symbol that cannot have one.
_SYMBOL_RE: Final = re.compile(r"^[A-Z]{1,6}(\.[A-Z])?$")

#: The provider's enum spelled the way the wire spells it. A mapping rather
#: than ``.value``, so the literal type survives to mypy instead of widening
#: to ``str``.
_RIGHT: Final[Mapping[OptionType, OptionRight]] = {
    OptionType.CALL: "call",
    OptionType.PUT: "put",
}

_ANALYTICS_SOURCE: Final[Mapping[ProviderAnalyticsSource, AnalyticsSource]] = {
    ProviderAnalyticsSource.VENDOR: "vendor",
    ProviderAnalyticsSource.DERIVED: "derived",
}


@dataclass(frozen=True, slots=True)
class SessionVolume:
    """One session's share volume, carrying the session it covers.

    The date travels with the number because the number alone cannot say
    which of the two things the Volume column means. It is also what keeps
    the average honest when the market is closed: the session that supplied
    the numerator has to be the one session the denominator leaves out.
    """

    session: date
    volume: int


# --------------------------------------------------------------------------
# Per-session caches
# --------------------------------------------------------------------------

ValueT = TypeVar("ValueT")

FetchMany = Callable[[tuple[str, ...]], Awaitable[Mapping[str, ValueT]]]


class SessionCache(Generic[ValueT]):
    """One value per symbol, valid for one trading date in New York.

    The key is the *date* rather than a duration because that is what the
    values actually depend on: an average daily volume and a series of daily
    closes both change at a session boundary and at no other moment. A TTL
    would expire them mid-session for no gain and hold yesterday's across the
    open.

    ``fetch`` must answer for **every** symbol it is handed, using whatever
    "nothing" is for its value type -- ``None`` for an average, an empty tuple
    for a series. A cached absence is still an answer, and the symbols with no
    data are exactly the ones where re-asking every two seconds buys least.

    The lock is held across the fetch on purpose: the second of two concurrent
    polls waits for the first rather than issuing the same request again.
    """

    def __init__(self) -> None:
        self._day: date | None = None
        self._values: dict[str, ValueT] = {}
        self._lock = asyncio.Lock()

    async def resolve(
        self,
        symbols: Sequence[str],
        *,
        today: date,
        fetch: FetchMany[ValueT],
    ) -> dict[str, ValueT]:
        async with self._lock:
            if self._day != today:
                self._values.clear()
                self._day = today
            missing = tuple(
                symbol for symbol in symbols if symbol not in self._values
            )
            if missing:
                self._values.update(await fetch(missing))
            return {
                symbol: self._values[symbol]
                for symbol in symbols
                if symbol in self._values
            }


class IntradayCache(Generic[ValueT]):
    """One value per symbol, valid for a bounded slice of *this* session.

    A second cache rather than a second use of :class:`SessionCache`, because
    the value has a different lifetime. An average over completed sessions
    changes at a session boundary and at no other moment; today's volume so
    far changes all session long, and a key of "the trading date" would freeze
    it at whatever the first poll of the morning saw.

    The trading date is still part of the key, so the value is dropped at the
    boundary as well as on expiry -- yesterday's total served as today's
    volume is the same error the session key exists to prevent, arriving a day
    late.

    ``settled`` is the other half of "a bounded slice". It is asked about the
    instant an entry was **read**, and answering ``True`` means nothing that
    happens before the date rolls can change what was read -- so the entry
    keeps its value and the TTL stops applying to it. Today's volume settles
    when today's session finishes, which on a weekend or a holiday is before
    the first poll of the day; without it a closed market spends a request a
    minute to be told the same integer until midnight.

    **Asked about the read, not about the question.** An entry read at 15:59
    holds a figure short of the close; judging it by the clock at 16:20 would
    freeze that short number in place and call it the day's volume. So the
    entry read before the session settled expires normally, and its
    replacement -- read after -- is the one that is held.

    Expiry is per entry rather than per cache for the same reason the value
    is: two symbols fetched a minute apart have two different ages, and
    dropping the newer one with the older costs a request to re-read what was
    already current.

    ``fetch`` and the lock behave exactly as :class:`SessionCache`'s do: it
    must answer for every symbol handed to it, a cached absence is an answer,
    and the second of two concurrent polls waits rather than re-asking.
    """

    def __init__(self, ttl: timedelta) -> None:
        self._ttl = ttl
        self._day: date | None = None
        self._values: dict[str, ValueT] = {}
        self._read_at: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def resolve(
        self,
        symbols: Sequence[str],
        *,
        today: date,
        now: datetime,
        fetch: FetchMany[ValueT],
        settled: Callable[[datetime], bool] | None = None,
    ) -> dict[str, ValueT]:
        async with self._lock:
            if self._day != today:
                self._values.clear()
                self._read_at.clear()
                self._day = today
            else:
                self._drop_expired(now=now, settled=settled)
            missing = tuple(
                symbol for symbol in symbols if symbol not in self._values
            )
            if missing:
                fetched = await fetch(missing)
                self._values.update(fetched)
                for symbol in fetched:
                    self._read_at[symbol] = now
            return {
                symbol: self._values[symbol]
                for symbol in symbols
                if symbol in self._values
            }

    def _drop_expired(
        self, *, now: datetime, settled: Callable[[datetime], bool] | None
    ) -> None:
        for symbol, read_at in list(self._read_at.items()):
            if settled is not None and settled(read_at):
                continue
            if now - read_at >= self._ttl:
                self._values.pop(symbol, None)
                self._read_at.pop(symbol, None)


class MarketCaches:
    """The clock and the per-session caches these routes share.

    The clock lives with the caches because the cache key *is* a reading of
    it, and because every other time-dependent decision in this module -- the
    DTE window, which bar counts as today's -- has to agree with it. One
    injected callable rather than four calls to ``datetime.now`` is what makes
    "asked on the 14th" a testable condition.
    """

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self.now: Callable[[], datetime] = now if now is not None else _utc_now
        self.daily_volume: SessionCache[tuple[SessionVolume, ...]] = SessionCache()
        self.session_volume: IntradayCache[int | None] = IntradayCache(
            SESSION_VOLUME_TTL
        )
        self.history: SessionCache[tuple[PricePoint, ...]] = SessionCache()

    def today(self) -> date:
        """The trading date in New York.

        Market data is Eastern. Read from the instant rather than from a
        stored date so a process that runs across midnight does not serve
        yesterday's window all night.
        """
        return self.now().astimezone(EASTERN).date()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def market_caches(request: Request) -> MarketCaches:
    """The app's caches, built on first use.

    On ``app.state`` rather than in a module global: two applications in one
    process -- which is every test file here -- must not share a cache, and a
    global would hand the second one the first one's prices.
    """
    caches = getattr(request.app.state, "market_caches", None)
    if caches is None:
        caches = MarketCaches()
        request.app.state.market_caches = caches
    assert isinstance(caches, MarketCaches)
    return caches


CachesDep = Annotated[MarketCaches, Depends(market_caches)]

SymbolsQuery = Annotated[
    str | None,
    Query(
        description=(
            "Comma-separated symbols to narrow the response to. Every symbol "
            "must be in the configured universe; an unknown one is refused "
            "rather than skipped."
        ),
    ),
]


# --------------------------------------------------------------------------
# Symbols in, symbols refused
# --------------------------------------------------------------------------


def _requested_symbols(raw: str | None, *, default: Sequence[str]) -> tuple[str, ...]:
    """Parse ``?symbols=``, or fall back to the whole default set.

    Unknown symbols are a 400 naming every one of them, not a silent
    intersection. A response that quietly dropped a symbol would read as "that
    stock has no data" when the truth is "this server has never heard of it",
    and the two want different remedies.
    """
    if raw is None or not raw.strip():
        return tuple(default)

    requested: list[str] = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if symbol and symbol not in requested:
            requested.append(symbol)

    unknown = [symbol for symbol in requested if symbol not in UNIVERSE_BY_SYMBOL]
    if unknown:
        raise _refuse_symbols(
            unknown,
            code="unknown_symbol",
            message=(
                f"{', '.join(unknown)} is not in the configured universe of "
                f"{len(UNIVERSE_SYMBOLS)} symbols. The stock table serves a "
                "company name, and this plan has no source for one, so a "
                "symbol outside the list has no row to render."
            ),
            rule=(
                "a symbol outside the configured universe is refused, never "
                "silently dropped from the response"
            ),
        )
    if not requested:
        return tuple(default)
    return tuple(requested)


def _validated_underlying(raw: str) -> str:
    """Shape-check a path parameter before it reaches the vendor.

    The chain deliberately accepts any well-formed equity symbol rather than
    only the curated universe: a chain is market data keyed by symbol and
    needs no name, so there is nothing this server would have to invent. What
    it will not do is forward an arbitrary string -- that is three requests
    against two rate-limit buckets for a typo.
    """
    symbol = raw.strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise _refuse_symbols(
            [raw],
            code="invalid_symbol",
            message=(
                f"{raw!r} is not an equity symbol. Expected one to six letters, "
                "optionally with a class suffix such as BRK.B."
            ),
            rule="a malformed symbol is refused before it costs a vendor request",
        )
    return symbol


def _refuse_symbols(
    symbols: Sequence[str], *, code: str, message: str, rule: str
) -> ApiError:
    """Record the rule, the inputs and the timestamp, then refuse (rule 8)."""
    logger.warning(
        "refused a markets request: %s",
        message,
        extra={
            "event": "symbol_rejected",
            "rule": rule,
            "code": code,
            "symbols": list(symbols),
            "at": _utc_now().isoformat(),
        },
    )
    return ApiError(status_code=400, code=code, message=message)


# --------------------------------------------------------------------------
# Arithmetic, in exact decimals
# --------------------------------------------------------------------------


def _change(last: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if last is None or previous is None:
        return None
    return last - previous


def _change_pct(last: Decimal | None, previous: Decimal | None) -> Decimal | None:
    """Percent, to two places, or ``None`` when there is nothing to measure from.

    Quantized rather than left at the division's full precision: a repeating
    decimal would trip the money serializer's round-trip check on every
    response, and two places is what the column renders anyway.
    """
    if last is None or previous is None or previous == 0:
        return None
    return ((last - previous) / previous * _HUNDRED).quantize(
        _CENTS, rounding=ROUND_HALF_EVEN
    )


def _strike_band(
    spot: Decimal | None, moneyness_pct: int
) -> tuple[Decimal | None, Decimal | None]:
    """The strike window around spot, widened to whole cents.

    Widened rather than rounded: narrowing by a fraction of a cent could drop
    a listed strike sitting exactly on the boundary, and the band exists to
    bound the page count, not to be exact.
    """
    if spot is None or spot <= 0:
        return None, None
    low = (spot * (_HUNDRED - moneyness_pct) / _HUNDRED).quantize(
        _CENTS, rounding=ROUND_FLOOR
    )
    high = (spot * (_HUNDRED + moneyness_pct) / _HUNDRED).quantize(
        _CENTS, rounding=ROUND_CEILING
    )
    return low, high


def _session_date(bar: Bar) -> date:
    """The bar's session in New York.

    A daily bar is stamped at the session's **open** -- ``04:00Z``, which is
    midnight Eastern -- so reading the UTC date directly would be right by
    accident and wrong the moment the stamp convention changes.
    """
    return bar.at.astimezone(EASTERN).date()


def _session_state(day: date, *, now: datetime) -> SessionState:
    """Whether ``day``'s own volume can still move, per the market calendar.

    One function, two uses, and they are the same question asked twice:

    * **The label.** ``volume`` means *traded so far today* during a session
      and *traded last session* outside one, and a column that can mean two
      things has to say which -- otherwise a partial day is compared against
      a full one and a stock reads as quiet at ten in the morning.
    * **The cache.** :data:`SESSION_VOLUME_TTL` exists because the figure
      climbs all session. A figure that has stopped climbing does not need
      re-reading a minute later, for the rest of the day.

    The close comes from the calendar, never from a hardcoded 16:00. NYSE
    closes at **13:00 ET** on the Friday after Thanksgiving and on a handful
    of other half-days, which sit in the busiest weekly-expiry season of the
    year; measured against 16:00 the column would call those sessions "in
    progress" for three hours after they ended. ``None`` from the calendar is
    a day with no session at all -- a weekend, a holiday, or a date outside
    the published schedule -- and on every one of those nothing is in
    progress.

    :data:`SESSION_SETTLES_AFTER` is added because the bar lags the tape: for
    fifteen minutes after the close the historical feed still serves a bar
    that is short of it.
    """
    close = nyse_session_close(day)
    if close is None:
        return "completed"
    return "completed" if now >= close + SESSION_SETTLES_AFTER else "in_progress"


# --------------------------------------------------------------------------
# The series window: a period, a timeframe, and a ceiling on the two together
# --------------------------------------------------------------------------
#
# `/api/account/history` already answered "how far back, at what resolution?"
# and this is the same question, so it gets the same grammar and the same
# refusal style: validate here, 422 naming the field, never let the vendor
# 400 three layers away. The one thing added is a ceiling on the *pair*,
# which the account endpoint does not need because it serves one account and
# this one serves a list of symbols.


@dataclass(frozen=True, slots=True)
class SeriesSize:
    """How large a window would come back, before it is asked for.

    An estimate, and deliberately an over-estimate: :data:`
    EXTENDED_SESSION_MINUTES` is the longest a session's bars can run, so a
    window that passes this bound cannot exceed it in fact. The session count
    is exact.
    """

    sessions: int
    per_symbol: int
    total: int


def _months_before(day: date, months: int) -> date:
    """``day`` less ``months`` calendar months, clamped to a real date.

    31 March less one month is 28 or 29 February, not the 31st of a month
    that has no 31st. Written out rather than taken from ``dateutil``, which
    is not a dependency here.
    """
    index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    first_of_next = date(year + month // 12, month % 12 + 1, 1)
    last_day = (first_of_next - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def _period_start(period: str, *, today: date) -> date:
    """The first day of the window, inclusive of both ends.

    ``400D`` resolves to ``today - 399 days``, which is exactly what
    ``history_days=400`` resolved to -- the default response is the one that
    was already being served, not a new one that happens to look similar.

    The caller has validated the shape; an unmatched period here is a
    programming error rather than a request.
    """
    match = _PERIOD.match(period)
    if match is None:  # pragma: no cover - guarded by `_check_series_window`
        raise ValueError(f"unvalidated period {period!r}")
    count, unit = int(match.group(1)), match.group(2)
    if unit == "D":
        return today - timedelta(days=count - 1)
    if unit == "W":
        return today - timedelta(days=count * 7 - 1)
    months = count if unit == "M" else count * 12
    return _months_before(today, months) + timedelta(days=1)


def _sessions_in(start: date, end: date) -> int:
    """Sessions in ``[start, end]``, from the market calendar.

    Never a ratio of calendar days. 6--10 September 2026 is five days and
    **three** sessions -- a weekend and Labor Day -- and the difference decides
    whether a window is refused. Half-days count as sessions, which is right:
    they are shorter, and :data:`EXTENDED_SESSION_MINUTES` is an upper bound
    that already covers them.

    A day the calendar has nothing to say about -- past the end of the
    published schedule -- is not a session here. The window is bounded to 400
    days back from today, so that case is always a *future* date and counting
    it would be inventing a session.
    """
    day, count = start, 0
    while day <= end:
        if nyse_session_close(day) is not None:
            count += 1
        day += timedelta(days=1)
    return count


def _bars_per_session(timeframe: str) -> int:
    """Bars one session yields at ``timeframe``, rounded up.

    960, 192, 64 and 16 for the four intraday resolutions; one for ``1D``.
    """
    if timeframe == DAILY_TIMEFRAME:
        return 1
    minutes = _TIMEFRAME_MINUTES[timeframe]
    return -(-EXTENDED_SESSION_MINUTES // minutes)


def _series_size(sessions: int, timeframe: str, *, symbol_count: int) -> SeriesSize:
    per_symbol = sessions * _bars_per_session(timeframe)
    return SeriesSize(
        sessions=sessions,
        per_symbol=per_symbol,
        total=per_symbol * symbol_count,
    )


def _fits(size: SeriesSize) -> bool:
    return (
        size.per_symbol <= MAX_SERIES_POINTS and size.total <= MAX_RESPONSE_POINTS
    )


def _finest_timeframe_that_fits(sessions: int, *, symbol_count: int) -> str | None:
    """The most detail this window can carry, or ``None`` if even ``1D`` cannot.

    ``_TIMEFRAMES`` runs fine to coarse, so the first that fits is the best
    answer available -- which is what the refusal names. ``None`` means the
    period itself is the problem, or there are simply too many symbols, and
    the message says so instead of suggesting a timeframe that would also be
    refused.
    """
    for name in _TIMEFRAMES:
        if _fits(_series_size(sessions, name, symbol_count=symbol_count)):
            return name
    return None


def _refuse_window(
    message: str, *, rule: str, period: str, timeframe: str, symbol_count: int
) -> ApiError:
    """Record the rule, the inputs and the timestamp, then refuse (rule 8)."""
    logger.warning(
        "refused a series window: %s",
        message,
        extra={
            "event": "series_window_rejected",
            "rule": rule,
            "code": "invalid_series_window",
            "period": period,
            "timeframe": timeframe,
            "symbols": symbol_count,
            "at": _utc_now().isoformat(),
        },
    )
    return ApiError(status_code=422, code="invalid_series_window", message=message)


def _check_series_window(
    period: str, timeframe: str, *, symbol_count: int, today: date
) -> None:
    """Four refusals, in the order that costs least to decide.

    Shape, then vocabulary, then depth, then size. Every one of them happens
    **before** a request leaves for either bucket, which is the point of
    validating here rather than letting the vendor answer.
    """
    if not _PERIOD.match(period):
        raise _refuse_window(
            f"period must be a count followed by D, W, M or A — {period!r} is "
            "not. Note the unit for a year is A, not Y.",
            rule=(
                "a window is validated here rather than sent and 400'd by the "
                "vendor three layers away"
            ),
            period=period,
            timeframe=timeframe,
            symbol_count=symbol_count,
        )
    if timeframe not in _TIMEFRAMES:
        raise _refuse_window(
            f"timeframe must be one of {', '.join(sorted(_TIMEFRAMES))} — "
            f"{timeframe!r} is not.",
            rule=(
                "a window is validated here rather than sent and 400'd by the "
                "vendor three layers away"
            ),
            period=period,
            timeframe=timeframe,
            symbol_count=symbol_count,
        )

    start = _period_start(period, today=today)
    earliest = today - timedelta(days=HISTORY_DAYS - 1)
    if start < earliest:
        raise _refuse_window(
            f"period reaches back to {start.isoformat()} and this endpoint "
            f"serves {DEFAULT_PERIOD} at most, to {earliest.isoformat()}. Ask "
            f"for {DEFAULT_PERIOD} or less.",
            rule=(
                "a window deeper than the series behind it is refused rather "
                "than answered short — a silently truncated chart reports on a "
                "window nobody asked for"
            ),
            period=period,
            timeframe=timeframe,
            symbol_count=symbol_count,
        )

    size = _series_size(
        _sessions_in(start, today), timeframe, symbol_count=symbol_count
    )
    if _fits(size):
        return
    # Which half is over decides what the caller should do about it. Too many
    # points per symbol is a timeframe that is too fine; a per-symbol series
    # that fits while the response does not is a list that is too long, and
    # suggesting a coarser timeframe there would be answering a question
    # nobody asked.
    remedy = "Shorten the period, or narrow ?symbols= to fewer names."
    if size.per_symbol > MAX_SERIES_POINTS:
        suggestion = _finest_timeframe_that_fits(
            size.sessions, symbol_count=symbol_count
        )
        if suggestion is not None:
            remedy = f"Ask for {period} at {suggestion} instead, or shorten the period."
    raise _refuse_window(
        f"{period} at {timeframe} is about {size.per_symbol:,} points per "
        f"symbol over {size.sessions:,} session(s), {size.total:,} across "
        f"{symbol_count} symbol(s) — more points than any chart can render. "
        f"The ceiling is {MAX_SERIES_POINTS:,} per symbol and "
        f"{MAX_RESPONSE_POINTS:,} per request. {remedy}",
        rule=(
            "a period and a timeframe are bounded as a pair, because the "
            "product is what costs round trips and bytes"
        ),
        period=period,
        timeframe=timeframe,
        symbol_count=symbol_count,
    )


def _resolve_period(
    period: str | None,
    history_days: int | None,
    *,
    timeframe: str,
    symbol_count: int,
) -> str:
    """One window out of the parameter and its deprecated predecessor.

    ``history_days`` is the spelling the frontend sends today. Dropping it
    would not have *errored* -- FastAPI ignores a query parameter no route
    declares -- so a caller asking for 90 days would have been handed 400 and
    told nothing, which is the quiet wrongness this codebase exists to avoid.
    It is accepted, translated, and will be removed once no caller sends it.

    Both together is refused rather than ranked: there is no correct answer to
    two windows in one request, and picking one silently is how a client ends
    up sure it asked for something it did not get.
    """
    if history_days is None:
        return DEFAULT_PERIOD if period is None else period
    if period is not None:
        raise _refuse_window(
            f"period={period!r} and history_days={history_days} are two "
            "windows in one request. Send period alone; history_days is "
            "deprecated and means the same as "
            f"'{history_days}D'.",
            rule=(
                "two spellings of one window are refused rather than ranked — "
                "a silent preference is a request the caller did not make"
            ),
            period=period,
            timeframe=timeframe,
            symbol_count=symbol_count,
        )
    return f"{history_days}D"


# --------------------------------------------------------------------------
# The daily series, fetched once a session
# --------------------------------------------------------------------------


def _completed_sessions(bars: Sequence[Bar], *, today: date) -> list[Bar]:
    """Every bar before today's, oldest first.

    Today's daily bar is **partial** until the close -- the historical feed
    serves it up to fifteen minutes ago -- so including it makes an "average
    daily volume" that falls through the morning and rises through the
    afternoon. An average that moves with the clock is not an average.
    """
    return [bar for bar in bars if _session_date(bar) < today]


async def _fetch_daily_volumes(
    provider: MarketDataProvider, symbols: tuple[str, ...], *, today: date
) -> Mapping[str, tuple[SessionVolume, ...]]:
    """Completed sessions per symbol, oldest first, with their dates.

    The series rather than the average it folds down to, for one reason: when
    the market is closed the **last entry is the numerator** of relative
    volume and the entries before it are the denominator. Folding here would
    throw away the one figure the closed-market case needs, and re-fetching it
    separately would put the two halves of a ratio in two responses -- which
    is the shape the same-feed rule exists to prevent.

    An empty tuple for a symbol with no bars in the window; the average is
    ``None`` downstream and never 0, because zero is a denominator that would
    divide and rank first on the screen built to find unusual activity.

    Trimmed to one more session than the average spans: thirty for the
    denominator plus the one that may have to serve as the numerator.
    """
    start = datetime.combine(
        today - timedelta(days=AVG_VOLUME_LOOKBACK_DAYS),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    bars = await provider.stock_bars(
        symbols, timeframe=BarTimeframe.DAY, start=start
    )
    return {
        symbol: tuple(
            SessionVolume(session=_session_date(bar), volume=bar.volume)
            for bar in _completed_sessions(bars.get(symbol, []), today=today)
        )[-(AVG_VOLUME_SESSIONS + 1) :]
        for symbol in symbols
    }


async def _fetch_session_volumes(
    provider: MarketDataProvider, symbols: tuple[str, ...], *, today: date
) -> Mapping[str, int | None]:
    """Today's volume so far -- **from the same feed as the average**.

    This is the numerator of relative volume and
    :func:`_fetch_daily_volumes` supplies the denominator; the only thing that
    makes their ratio mean anything is that both are measured the same way.
    They are: both are ``stock_bars``, which the provider always serves from
    the historical feed.

    The obvious other source is the snapshot's daily bar, which is already
    fetched and free. It is wrong here, and quietly: snapshots are a *latest*
    endpoint, which on the Basic plan is IEX-only, and IEX is about 2.5% of US
    equity volume. Measured on this repository's own recordings that put NVDA
    at 2,162,109 over an average of 119,238,467 -- a relative volume of 0.018
    -- so the screen built to find unusual activity was ranking IEX routing
    share and every name read as near-dead.

    Today's bar is reachable on the historical feed because the request's
    ``end`` resolves to fifteen minutes ago, which is old enough for any feed
    the plan carries. The cost is that the first fifteen minutes of a session
    have no servable bar yet -- and outside a session there is no bar at all,
    which used to empty the whole column every weekend.

    ``None`` here no longer means the column is blank. It means *this* session
    has nothing to report yet, and :func:`_volume_reading` then falls back to
    the last completed session out of the series already fetched for the
    average. What it never means is zero.
    """
    start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    bars = await provider.stock_bars(
        symbols, timeframe=BarTimeframe.DAY, start=start
    )
    volumes: dict[str, int | None] = {}
    for symbol in symbols:
        session = [
            bar for bar in bars.get(symbol, []) if _session_date(bar) == today
        ]
        volumes[symbol] = session[-1].volume if session else None
    return volumes


@dataclass(frozen=True, slots=True)
class VolumeReading:
    """The Volume column for one symbol: the figure, and which session it is.

    Assembled in one place because the four values are one decision. Split
    across the response builder they drift: a numerator taken from one
    session against a denominator that includes it is biased toward 1, which
    is precisely the reading the ratio exists to make visible.
    """

    volume: int | None
    session: date | None
    state: SessionState | None
    average: int | None


#: Nothing measured. A symbol with no daily bar anywhere in the window gets
#: this, and every field of it is null rather than 0 -- a zero volume claims
#: the symbol did not trade, and a zero average divides.
NO_VOLUME: Final = VolumeReading(volume=None, session=None, state=None, average=None)


def _volume_reading(
    completed: Sequence[SessionVolume],
    *,
    today_volume: int | None,
    today: date,
    now: datetime,
) -> VolumeReading:
    """Relative volume's two halves, and the session the numerator covers.

    **The market is not always open, and the column still has to say
    something.** During a session the numerator is today's partial bar and
    the denominator is the completed sessions before it. Outside one -- a
    weekend, a holiday, the hours either side of the bell, the first fifteen
    minutes before the feed will serve today's bar -- there is no bar for
    today, and a column keyed strictly on today reads blank for every symbol
    on the page. The last completed session is equally honest and answers the
    question the reader actually has, so that is what is served, labelled
    with the session it covers.

    **The denominator never contains the numerator.** Self-inclusion pulls
    every ratio toward 1 and so quietly masks exactly the outlier the ratio
    is for. During a session that falls out of ``completed`` excluding today;
    when the numerator moves back a day, the denominator has to move with it,
    which is the ``[:-1]`` below.

    Both halves come out of ``stock_bars`` -- in the closed-market case out of
    the same *response* -- so the feed they were measured on is the same feed
    by construction.
    """
    if today_volume is not None:
        measured, session, window = today_volume, today, completed
    elif completed:
        measured, session, window = (
            completed[-1].volume,
            completed[-1].session,
            completed[:-1],
        )
    else:
        return NO_VOLUME
    average = window[-AVG_VOLUME_SESSIONS:]
    return VolumeReading(
        volume=measured,
        session=session,
        state=_session_state(session, now=now),
        average=(
            sum(entry.volume for entry in average) // len(average)
            if average
            else None
        ),
    )


async def _fetch_histories(
    provider: MarketDataProvider, symbols: tuple[str, ...], *, today: date
) -> Mapping[str, tuple[PricePoint, ...]]:
    """Daily closes per symbol, completed sessions only, oldest first.

    Today's point is deliberately absent: it is appended at response time from
    the live quote, so the last point of the chart and the quoted price are
    the same number rather than two readings a poll apart.
    """
    start = datetime.combine(
        today - timedelta(days=HISTORY_DAYS - 1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    bars = await provider.stock_bars(
        symbols, timeframe=BarTimeframe.DAY, start=start
    )
    return {
        symbol: tuple(
            PricePoint(date=_session_date(bar), value=bar.close)
            for bar in _completed_sessions(bars.get(symbol, []), today=today)
        )
        for symbol in symbols
    }


async def _fetch_intraday(
    provider: MarketDataProvider,
    symbols: tuple[str, ...],
    *,
    start: datetime,
    timeframe: BarTimeframe,
) -> Mapping[str, tuple[IntradayPoint, ...]]:
    """Closes at a resolution finer than a day, oldest first.

    **Deliberately not cached.** :class:`SessionCache` keys on the trading
    date, which is exactly right for a series that changes only at a session
    boundary and exactly wrong for one that moves all session long -- the
    first poll of the morning would be served until midnight. A second cache
    would also have to key on the window as well as the symbol, since ``1D``
    at ``5Min`` and ``1W`` at ``1H`` are different series for the same name,
    and a cache that got that wrong would serve one as the other.

    The cost of not caching is bounded by what asks for this: a chart the user
    expanded, not the 2s poll. The poll is ``/stocks``, which does not come
    here at all, and the client's own query cache is the right place for the
    rest.

    ``start`` is **midnight Eastern**, not midnight UTC. UTC midnight on the
    window's first day is 20:00 ET the evening before, which would pull in the
    previous session's post-market bars and open the chart a day early.

    Bars carry the feed the provider always uses for history -- SIP on this
    plan -- because ``stock_bars`` is a historical endpoint and resolves its
    own ``end``. They therefore stop fifteen minutes short of now; the live
    point that closes that gap is appended at response time, from the quote.
    """
    bars = await provider.stock_bars(symbols, timeframe=timeframe, start=start)
    return {
        symbol: tuple(
            IntradayPoint(at=bar.at, value=bar.close)
            for bar in bars.get(symbol, [])
        )
        for symbol in symbols
    }


def _eastern_midnight(day: date) -> datetime:
    """Midnight in New York on ``day``, as a UTC instant.

    Market data is Eastern. A window that begins at UTC midnight begins at
    20:00 the previous evening in New York, which is inside the previous
    session's extended hours.
    """
    return datetime.combine(day, time.min, tzinfo=EASTERN).astimezone(timezone.utc)


def _log_unpriced(symbols: Sequence[str], *, route: str) -> None:
    if not symbols:
        return
    logger.warning(
        "omitted %d symbol(s) from %s with no price on any source: %s",
        len(symbols),
        route,
        ", ".join(symbols),
        extra={
            "event": "symbol_unpriced",
            "rule": (
                "a symbol with no quote, no print and no daily bar is omitted "
                "rather than served at zero -- absence is never a price"
            ),
            "symbols": list(symbols),
            "route": route,
            "at": _utc_now().isoformat(),
        },
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/stocks", summary="The stock table: price, change, volume, relative volume")
async def stocks(
    provider: ProviderDep,
    caches: CachesDep,
    symbols: SymbolsQuery = None,
) -> list[StockQuote]:
    """One row per symbol, in the order asked for.

    One snapshot request covers the whole universe, which is what keeps the 2s
    poll at one request against ``data.alpaca.markets`` rather than one per
    name. The average-volume series behind it is fetched once a session; see
    :class:`SessionCache`.

    **Volume and average volume are one measurement over two windows**, both
    out of ``stock_bars`` and therefore both on the historical feed. The
    snapshot in hand carries a daily bar and it is deliberately not read: it
    is IEX, the average is SIP, and their ratio is a routing share rather than
    a relative volume. Today's half has its own short-lived cache, because it
    moves through the session where the average does not; see
    :class:`IntradayCache` and :func:`_fetch_session_volumes`.

    **When the market is closed the column shows the last completed session**,
    labelled as such -- ``volumeSession`` and ``volumeDate`` say which of the
    two things the number is, so the page can title the column without
    re-deriving a New York session boundary from a browser clock. See
    :func:`_volume_reading`, which also keeps that session out of its own
    average.

    Every column that can be absent is served as ``null``: no previous close
    means no change, no daily bar anywhere in the window means no volume and
    no session to name, fewer than two sessions means no average. The one
    absence handled by omission instead is a symbol with no price at all --
    it is dropped and logged.
    """
    requested = _requested_symbols(symbols, default=UNIVERSE_SYMBOLS)
    today = caches.today()
    now = caches.now()

    snapshots = await provider.stock_snapshots(requested)
    daily = await caches.daily_volume.resolve(
        requested,
        today=today,
        fetch=lambda missing: _fetch_daily_volumes(provider, missing, today=today),
    )
    volumes = await caches.session_volume.resolve(
        requested,
        today=today,
        now=now,
        fetch=lambda missing: _fetch_session_volumes(provider, missing, today=today),
        # Today's figure climbs all session and then stops. Past the close it
        # is re-read once and held, so a closed market costs one request a day
        # rather than one a minute.
        settled=lambda read_at: _session_state(today, now=read_at) == "completed",
    )

    table: list[StockQuote] = []
    unpriced: list[str] = []
    for symbol in requested:
        snapshot = snapshots.get(symbol)
        price = snapshot.price if snapshot is not None else None
        if snapshot is None or price is None:
            unpriced.append(symbol)
            continue
        previous = snapshot.previous_close
        # Both halves of relative volume, from one feed. Never the snapshot's
        # daily bar, which is IEX on this plan while the series is SIP -- see
        # `_fetch_session_volumes`.
        measured = _volume_reading(
            daily.get(symbol, ()),
            today_volume=volumes.get(symbol),
            today=today,
            now=now,
        )
        table.append(
            StockQuote(
                symbol=symbol,
                name=UNIVERSE_BY_SYMBOL[symbol].name,
                price=price,
                change=_change(price, previous),
                change_pct=_change_pct(price, previous),
                volume=measured.volume,
                volume_session=measured.state,
                volume_date=measured.session,
                avg_volume=measured.average,
                # Finnhub, step 9. Null for a fund is the truth; null for a
                # company is "not fetched yet", and both are better than a
                # number nobody computed.
                market_cap=None,
            )
        )
    _log_unpriced(unpriced, route="/api/markets/stocks")
    return table


@router.get("/underlyings", summary="Quoted underlyings, with a price series")
async def underlyings(
    provider: ProviderDep,
    caches: CachesDep,
    symbols: SymbolsQuery = None,
    period: Annotated[
        str | None,
        Query(
            description=(
                "How far back, as a count plus D, W, M or A — the same "
                "grammar as /api/account/history. A is a year, not Y. "
                f"Defaults to {DEFAULT_PERIOD}, which is also the deepest "
                "window this endpoint serves."
            ),
        ),
    ] = None,
    timeframe: Annotated[
        str,
        Query(
            description=(
                "Resolution: 1Min, 5Min, 15Min, 1H or 1D. 1D fills history; "
                "the other four fill intraday."
            ),
        ),
    ] = DAILY_TIMEFRAME,
    history_days: Annotated[
        int | None,
        Query(
            ge=2,
            le=HISTORY_DAYS,
            deprecated=True,
            description=(
                "Deprecated: calendar days of daily closes. Equivalent to "
                "period=<n>D, and refused if period is also sent."
            ),
        ),
    ] = None,
) -> list[UnderlyingQuote]:
    """Price, previous close and the chart series for each underlying.

    A list rather than a map, so the order is the server's and the client can
    key it however it likes.

    **The resolution is asked for, not inferred.** This route served daily
    closes and nothing else, while the chart's range control offered ``1D``
    and ``1W`` -- so ``1D`` drew one point and ``1W`` drew about five, and the
    control implied a resolution the data could not supply. ``timeframe`` is
    what fixes that, and it carries the same five values, the same spelling
    and the same refusals as ``/api/account/history``: one grammar for one
    question across the API.

    ``period`` replaces ``history_days`` for the same reason -- a count of
    *days* cannot express the window an intraday request wants -- and
    ``400D``, its default, is the window ``history_days`` defaulted to. A
    caller that sends neither gets exactly the response it got before either
    existed.

    **The series ends at today's live price at every timeframe.** Bars are a
    historical endpoint and stop fifteen minutes short of now, so the last
    point is appended from the quote: the range control reports the move over
    the window on screen, and a chart that stops a quarter of an hour short
    reports on a window nobody is looking at. It is appended only when today
    has actually traded -- on a Saturday the last bar is Friday's and a point
    stamped now would draw a flat line across the weekend.

    **One field carries the answer and the other is empty.** ``history`` is
    daily closes, ``intraday`` is everything finer, and they are never both
    populated -- a day cannot carry a five-minute stamp, so they cannot be one
    field. Both are empty when the window holds no session, which is a real
    condition rather than a failure: ``1D`` asked on a Sunday has nothing in
    it, and the honest answer is nothing.
    """
    requested = _requested_symbols(symbols, default=UNDERLYING_SYMBOLS)
    today = caches.today()
    now = caches.now()

    resolved_period = _resolve_period(
        period, history_days, timeframe=timeframe, symbol_count=len(requested)
    )
    _check_series_window(
        resolved_period, timeframe, symbol_count=len(requested), today=today
    )
    window_start = _period_start(resolved_period, today=today)
    daily = timeframe == DAILY_TIMEFRAME

    snapshots = await provider.stock_snapshots(requested)
    histories: Mapping[str, tuple[PricePoint, ...]] = {}
    intradays: Mapping[str, tuple[IntradayPoint, ...]] = {}
    if daily:
        # Cached per trading date: 400 days of closes move once a session, and
        # re-downloading them on every poll is what would break the budget.
        histories = await caches.history.resolve(
            requested,
            today=today,
            fetch=lambda missing: _fetch_histories(provider, missing, today=today),
        )
    else:
        intradays = await _fetch_intraday(
            provider,
            requested,
            start=_eastern_midnight(window_start),
            timeframe=_TIMEFRAMES[timeframe],
        )

    quotes: list[UnderlyingQuote] = []
    unpriced: list[str] = []
    for symbol in requested:
        snapshot = snapshots.get(symbol)
        price = snapshot.price if snapshot is not None else None
        if snapshot is None or price is None:
            unpriced.append(symbol)
            continue
        previous = snapshot.previous_close
        traded_today = _has_traded_today(snapshot, today=today)
        history = [
            point
            for point in histories.get(symbol, ())
            if point.date >= window_start
        ]
        intraday = list(intradays.get(symbol, ()))
        if traded_today and daily:
            history.append(PricePoint(date=today, value=price))
        elif traded_today:
            intraday.append(IntradayPoint(at=now, value=price))
        quotes.append(
            UnderlyingQuote(
                symbol=symbol,
                price=price,
                previous_close=previous,
                change=_change(price, previous),
                change_pct=_change_pct(price, previous),
                history=history,
                intraday=intraday,
            )
        )
    _log_unpriced(unpriced, route="/api/markets/underlyings")
    return quotes


def _has_traded_today(snapshot: StockSnapshot, *, today: date) -> bool:
    """Whether the session has produced a bar yet.

    Asked of the data rather than of a calendar: a daily bar stamped today is
    proof the market opened and traded, where a calendar lookup would claim a
    session on a day the feed has nothing for.
    """
    return snapshot.daily_bar is not None and _session_date(snapshot.daily_bar) == today


@router.get("/chain/{underlying}", summary="One underlying's option chain")
async def chain(
    underlying: Annotated[str, Path(description="Equity symbol, e.g. NVDA.")],
    provider: ProviderDep,
    caches: CachesDep,
    option_type: Annotated[
        OptionRight | None,
        Query(alias="type", description="Narrow to calls or puts."),
    ] = None,
    expiration: Annotated[
        date | None,
        Query(description="One expiry (YYYY-MM-DD). Replaces the DTE window."),
    ] = None,
    max_dte: Annotated[
        int, Query(ge=1, le=365, description="Days to expiry, at most.")
    ] = DEFAULT_MAX_DTE,
    moneyness_pct: Annotated[
        int,
        Query(
            ge=1,
            le=100,
            description="Half-width of the strike band around spot, in percent.",
        ),
    ] = DEFAULT_MONEYNESS_PCT,
) -> list[OptionContract]:
    """The chain, bounded by a window and labelled by analytics source.

    **The bound is a window, never a ``limit``.** Both endpoints return
    contracts ordered by strike, so a small limit returns the deep-ITM tail --
    which is how decision 10's first probe reached two wrong conclusions from
    real data. The window is pushed to the server as ``expiration_date_*``,
    ``strike_price_*`` and ``type``, so the pagination loop is short rather
    than truncated.

    The cost is three *operations*, not three requests: a spot snapshot, the
    chain (paginated, on ``data.alpaca.markets``), and the contract terms
    (paginated, on ``paper-api.alpaca.markets``, which is where open interest
    lives). The two hosts carry separate 200/min budgets, so the pair costs
    one bucket each rather than two of one.

    The spot snapshot is fetched **twice** -- once here for the strike band,
    and once inside ``option_chain`` for the Black-Scholes spot. That
    redundancy is stated rather than hidden: removing it means giving the
    provider a way to return the spot it already fetched, and the provider
    interface is not this dispatch's to change. It is one extra request per
    *chain view*, not per contract, which is the distinction that matters to
    the budget.

    The snapshots drive the result and the contract terms only join onto it. A
    contract the reference endpoint did not return still gets a row -- strike,
    expiry and right come from the OCC symbol -- with a null open interest,
    because what is missing is the reference data, not the market.
    """
    symbol = _validated_underlying(underlying)
    today = caches.today()
    expiration_gte, expiration_lte = (
        (expiration, expiration)
        if expiration is not None
        else (today, today + timedelta(days=max_dte))
    )
    right = None if option_type is None else OptionType(option_type)

    spot_snapshot = (await provider.stock_snapshots([symbol])).get(symbol)
    spot = spot_snapshot.price if spot_snapshot is not None else None
    strike_gte, strike_lte = _strike_band(spot, moneyness_pct)
    if spot is None:
        logger.warning(
            "no spot for %s; the chain is fetched unbanded and carries no "
            "derived analytics",
            symbol,
            extra={
                "event": "chain_unbanded",
                "rule": (
                    "a strike band needs a spot; without one the window is "
                    "widened rather than guessed"
                ),
                "underlying": symbol,
                "at": _utc_now().isoformat(),
            },
        )

    snapshots = await provider.option_chain(
        symbol,
        expiration_gte=expiration_gte,
        expiration_lte=expiration_lte,
        strike_gte=strike_gte,
        strike_lte=strike_lte,
        option_type=right,
    )
    terms = {
        contract.symbol: contract
        for contract in await provider.option_contracts(
            symbol,
            expiration_gte=expiration_gte,
            expiration_lte=expiration_lte,
            strike_gte=strike_gte,
            strike_lte=strike_lte,
            option_type=right,
        )
    }

    rows: list[OptionContract] = []
    unreadable: list[str] = []
    for occ_symbol, snapshot in snapshots.items():
        try:
            occ = parse_occ_symbol(occ_symbol)
        except ValueError:
            # Unreachable through ``AlpacaProvider``, which drops unparseable
            # keys itself -- and kept anyway, because the *interface* is what
            # this route is written against and the next provider may not. The
            # parse has to happen regardless: strike, expiry and right come
            # from the symbol and nowhere else. One bad key must not take down
            # a 200-row chain.
            unreadable.append(occ_symbol)
            continue
        rows.append(_chain_row(occ_symbol, occ, snapshot, terms.get(occ_symbol)))

    if unreadable:
        logger.warning(
            "dropped %d chain entry/entries whose symbol is not OCC: %s",
            len(unreadable),
            ", ".join(sorted(unreadable)),
            extra={
                "event": "chain_symbol_unreadable",
                "rule": (
                    "a symbol this app cannot parse is a contract it cannot "
                    "size, so it is dropped rather than partially rendered"
                ),
                "symbols": sorted(unreadable),
                "underlying": symbol,
                "at": _utc_now().isoformat(),
            },
        )

    # Expiry, then right, then strike -- the ladder the page draws. Snapshots
    # arrive in a map and a map's order is not an ordering, so this is what
    # makes identical inputs produce identical output.
    rows.sort(key=lambda row: (row.expiration, row.type, row.strike))
    _log_chain_analytics(symbol, rows)
    return rows


def _chain_row(
    occ_symbol: str,
    occ: OccSymbol,
    snapshot: OptionSnapshot,
    terms: ContractTerms | None,
) -> OptionContract:
    """One chain row. Every absent input stays absent."""
    quote = snapshot.latest_quote
    previous = (
        None
        if snapshot.previous_daily_bar is None
        else snapshot.previous_daily_bar.close
    )
    last = _contract_last(snapshot)
    iv, iv_source = _analytics(snapshot)
    return OptionContract(
        symbol=occ_symbol,
        strike=occ.strike,
        expiration=occ.expiration,
        type=_RIGHT[occ.option_type],
        last=last,
        previous_close=previous,
        change=_change(last, previous),
        change_pct=_change_pct(last, previous),
        # `bp: 0` means "no active bid", not "the bid is zero dollars". 49 of
        # the 100 contracts on a recorded NVDA page are bid-less; rendering
        # them at $0.00 invents a price on half the chain.
        bid=None if quote is None else quote.bid,
        ask=None if quote is None else quote.ask,
        volume=snapshot.volume,
        # Decision 10: populated on most contracts, genuinely absent on
        # newly-listed ones, and never coerced. A null is a claim about the
        # data; a zero would be a claim about the market.
        open_interest=None if terms is None else terms.open_interest,
        iv=iv,
        iv_source=iv_source,
    )


def _contract_last(snapshot: OptionSnapshot) -> Decimal | None:
    """The contract's last price, in a stated order of preference.

    The last print first, then the session's close, then the quote mid. Stated
    here rather than left to each call site, because two call sites choosing
    differently is how one screen disagrees with another about the same
    contract.

    Note what does **not** follow: a chain row's ``last`` need not sit between
    its bid and ask. A print is a fact about the past and a spread is a fact
    about now, and on an illiquid contract they are hours apart. (A
    *position's* ``last`` is a different number with a different invariant.)
    """
    if snapshot.latest_trade is not None:
        return snapshot.latest_trade.price
    if snapshot.daily_bar is not None:
        return snapshot.daily_bar.close
    if snapshot.latest_quote is not None:
        return snapshot.latest_quote.mid
    return None


def _analytics(
    snapshot: OptionSnapshot,
) -> tuple[Decimal | None, AnalyticsSource | None]:
    """The implied volatility and where it came from, together or not at all.

    Decision 10's requirement in one function: pass the vendor's number
    through where the feed supplied one, take the locally derived one where it
    did not, and label which. ``UNAVAILABLE`` carries no number by definition,
    and a number arriving under it would be a contradiction rather than a
    value worth rescuing -- the reason travels in the provider's
    ``analytics_note`` and is summarised in the log.
    """
    source = _ANALYTICS_SOURCE.get(snapshot.analytics_source)
    if source is None or snapshot.implied_volatility is None:
        return None, None
    return snapshot.implied_volatility, source


def _log_chain_analytics(underlying: str, rows: Sequence[OptionContract]) -> None:
    """Say, once per chain, how much of it was measured and how much derived.

    A per-row note would be the same sentence two hundred times. The mix is
    the thing worth knowing: a chain that is 90% derived on a day it is
    usually 20% is a feed telling you something.
    """
    vendor = sum(1 for row in rows if row.iv_source == "vendor")
    derived = sum(1 for row in rows if row.iv_source == "derived")
    logger.info(
        "served %d %s contracts: %d vendor IV, %d derived, %d without",
        len(rows),
        underlying,
        vendor,
        derived,
        len(rows) - vendor - derived,
        extra={
            "event": "chain_served",
            "rule": (
                "vendor analytics pass through, gaps are derived locally, and "
                "every number says which it is (decision 10)"
            ),
            "underlying": underlying,
            "contracts": len(rows),
            "iv_vendor": vendor,
            "iv_derived": derived,
            "iv_absent": len(rows) - vendor - derived,
            "open_interest_absent": sum(
                1 for row in rows if row.open_interest is None
            ),
            "at": _utc_now().isoformat(),
        },
    )
