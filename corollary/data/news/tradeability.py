"""Decision 21's tradeability filter -- pure -- and the session cache around it.

Everything down to :func:`assess_tradeability` is the pure filter. The
section after it, *The session cache*, is not pure: it reads and writes
``ticker_tradeability`` and awaits the provider, and its own docstrings carry
its rules. Nothing in either half imports the engine runtime, its state or its
sockets (decision 1), or the vendor file -- the provider arrives as a
parameter satisfying :class:`TradeabilityInputs`.

A discovery candidate must be a name the owner could actually trade options
on. All of these must hold, and every threshold is a named constant because
three of them are *(assumption)*s the owner can override:

* the asset list carries ``has_options`` for it;
* the ticker is **not an adjusted root**, and a **standard contract** exists
  -- one whose ``root_symbol`` is the ticker and whose ``size`` is 100.
  ``has_options`` alone is not enough: an underlying whose only listed chains
  are adjusted after a reverse split is exactly what secondary-offering
  headlines produce, and CLAUDE.md's ``AAPL1`` warning applies. (``size`` is
  used here as decision 21 states it, as a *filter*, never as a multiplier.)
* average daily volume >= 1,000,000 shares over the trailing 20 completed
  sessions *(assumption)*;
* last close >= $5 *(assumption)*;
* at least 20 sessions of history *(assumption: a recent IPO fails rather
  than being judged on a partial average)*;
* the newest bar is the previous session's -- a halted or suspended name is
  not judged on its pre-halt tape.

**Volume and close come from daily bars on the historical feed**
(``ALPACA_STOCK_FEED_HISTORICAL``, ``sip`` on this plan), never the realtime
``iex`` feed and never snapshot volume. That is the caller's job -- this module
never sees a feed name -- and the reason is CLAUDE.md's ``min_avg_volume``
warning: computed from IEX, 1,000,000 filters on a fortieth of real volume.

**The window is 20 calendar sessions, not 20 bars.** It is the
:data:`ADV_SESSIONS` exchange sessions immediately before ``session_date``, as
the market calendar lists them (``corollary.calendars``; half-days are
sessions, holidays are not, and nothing here hardcodes either). Counting bars
instead is wrong twice over: a name halted for six weeks still has 20 bars,
all pre-halt, and a thin name whose no-trade days produce no bar would be
averaged over only the days it traded. So:

* a window session with **no bar contributes zero volume** -- no trades is
  zero volume -- and the average is ``floor(sum / 20)`` whatever the bar count;
* **history** means a bar on or before the window's first session. A bar on
  the first session itself proves it; so does any older bar;
* **staleness**: if the newest bar before ``session_date`` is not dated the
  window's last session (the previous session), the ticker fails
  :attr:`TradeabilityFailure.STALE_BARS`. ``last_close`` is still the newest
  bar's close, so the cache row shows the price it would have been judged on.
  A bar dated on a day the calendar does not list as a session is not dropped
  or trusted: it is simply not a window session, and if it is the newest bar
  the ticker reads stale -- a visible disagreement, failing closed.

**What the caller must fetch.** Enough calendar days to cover 20 sessions --
about 30 calendar days, more across holidays. Do not guess the span:
:func:`adv_window_start` returns the window's first session, and a request
starting there covers every bar the window reads. A name that did not trade
on that one day then reports ``INSUFFICIENT_HISTORY`` unless an older bar was
also passed; any older bar the caller has is welcome and counts. (A name
that skips whole sessions is nowhere near a 1,000,000 average in practice, so
this only changes which reason is reported.)

**The calendar is a parameter.** ``is_session`` answers "is this date an
exchange session" and defaults to :func:`nyse_is_session`, which reads
``corollary.calendars``. That calendar is static published data, so the
filter stays deterministic; the first call warms it (about half a second, then
cached for the process), and a test may pass a fake instead.

**Which bars count.** Always the sessions *strictly before* ``session_date``.
The result is cached one row per ticker per session date, so it must not
depend on when in the day it was computed: the bar for ``session_date`` itself
is incomplete until the close, and a result computed at 09:00 and one computed
at 17:00 must agree. The caller may pass every bar it has; a bar dated
``session_date`` or later is dropped here, not trusted. A bar's session is its
opening timestamp read in ``America/New_York`` -- Alpaca stamps a daily bar at
midnight New York, which is 04:00 or 05:00 UTC.

**Nothing is guessed.** ``standard_root=None`` means "not checked" and fails
closed. Without 20 sessions of history no average is computed at all
(``avg_volume_20d`` is ``None``): recording a 5-session mean in a field named
for 20 would be judging on a partial average by another route.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ, nyse_session_close
from corollary.data.news.watchlist import MARKET_TICKER, WatchUniverse
from corollary.data.providers.interface import AssetDirectory, Bar, OptionContract, ProviderError
from corollary.data.seeds import EQUITY_SYMBOL_RE, normalize_symbol
from corollary.db.models import NewsArticle, NewsArticleTicker, TickerTradeability
from corollary.instruments import is_adjusted_root
from corollary.wire import require_aware

__all__ = [
    "ADV_BATCH_SIZE",
    "ADV_SESSIONS",
    "MAX_TICKERS_PER_RUN",
    "MIN_AVG_DAILY_VOLUME",
    "MIN_LAST_CLOSE",
    "MIN_SESSIONS_OF_HISTORY",
    "STANDARD_CONTRACT_SIZE",
    "CheckStage",
    "IsSession",
    "RefreshResult",
    "TickerCheckError",
    "TradeabilityFailure",
    "TradeabilityInputs",
    "TradeabilityResult",
    "adv_window",
    "adv_window_start",
    "assess_tradeability",
    "has_standard_contract",
    "is_adjusted_root_ticker",
    "nyse_is_session",
    "recent_article_tickers",
    "refresh_tradeability",
    "store_tradeability",
    "tickers_needing_check",
]

logger = logging.getLogger(__name__)

#: Average daily volume floor, in shares *(assumption)*. Inclusive: exactly
#: 1,000,000 passes.
MIN_AVG_DAILY_VOLUME: Final = 1_000_000

#: Last-close floor, in dollars *(assumption)*. Inclusive: exactly $5 passes.
MIN_LAST_CLOSE: Final = Decimal("5")

#: Sessions of history required before any judgement *(assumption)*.
MIN_SESSIONS_OF_HISTORY: Final = 20

#: The trailing window the average is taken over, in exchange sessions.
ADV_SESSIONS: Final = 20

#: The ``size`` a standard contract carries. A filter criterion from decision
#: 21, **never a multiplier** -- Alpaca's own spec says ``size`` must not be
#: used as one, and ``OptionContract.multiplier`` is the field for that.
STANDARD_CONTRACT_SIZE: Final = Decimal(100)

#: How far back :func:`adv_window` walks the calendar before concluding it
#: cannot answer. 20 sessions span about 28 calendar days, 32 across the
#: holiday season; 90 is a bound on a loop, not an estimate of the window.
_MAX_WINDOW_CALENDAR_DAYS: Final = 90

_DIGITS: Final = "0123456789"

#: "Is this date an exchange session?" -- the one thing the filter asks of a
#: market calendar.
IsSession = Callable[[date], bool]


def nyse_is_session(day: date) -> bool:
    """True when NYSE has a session on ``day`` -- half-days included.

    Reads ``corollary.calendars``. ``False`` covers weekends, holidays *and*
    dates outside the published schedule; :func:`adv_window` bounds its walk so
    the last of those raises rather than looping or shortening the window.
    """
    return nyse_session_close(day) is not None


class TradeabilityFailure(StrEnum):
    """Why a ticker failed. Declared in the order checks are reported."""

    MALFORMED_TICKER = "malformed_ticker"
    NO_OPTIONS = "no_options"
    ADJUSTED_ROOT = "adjusted_root"
    NO_STANDARD_CONTRACT = "no_standard_contract"
    STANDARD_ROOT_UNCHECKED = "standard_root_unchecked"
    INSUFFICIENT_HISTORY = "insufficient_history"
    STALE_BARS = "stale_bars"
    LOW_VOLUME = "low_volume"
    LOW_CLOSE = "low_close"


@dataclass(frozen=True, slots=True)
class TradeabilityResult:
    """Every field of a ``ticker_tradeability`` cache row, plus why it failed.

    ``avg_volume_20d`` is ``None`` without :data:`MIN_SESSIONS_OF_HISTORY`
    sessions of history; ``last_close`` is ``None`` with no completed bar at
    all. The cache columns for both must therefore be nullable.

    ``sessions_available`` is the number of the window's :data:`ADV_SESSIONS`
    sessions that carry a bar -- 0 to 20. It is *not* the length of the
    ticker's history: a long-listed name that did not trade on two window
    sessions reads 18 and still has its history.
    """

    ticker: str
    session_date: date
    has_options: bool
    standard_root: bool | None
    avg_volume_20d: int | None
    last_close: Decimal | None
    sessions_available: int
    #: Every failing check, in :class:`TradeabilityFailure` declaration order.
    #: Empty exactly when the ticker passes.
    failures: tuple[TradeabilityFailure, ...]

    @property
    def passes(self) -> bool:
        return not self.failures

    @property
    def first_failure(self) -> TradeabilityFailure | None:
        return self.failures[0] if self.failures else None


def is_adjusted_root_ticker(ticker: str) -> bool:
    """True when the ticker itself is an adjusted OCC root such as ``AAPL1``.

    An adjusted root is a standard root with a numeric suffix, so the
    candidate standard root is the ticker with trailing digits removed, and
    :func:`corollary.instruments.is_adjusted_root` answers whether the two
    differ. The empty string and an all-digit string have no root to be an
    adjustment *of*, so they answer ``False`` here and
    :func:`assess_tradeability` reports them as ``MALFORMED_TICKER`` -- which
    is what they are.
    """
    text = normalize_symbol(ticker)
    base = text.rstrip(_DIGITS)
    if not base:
        return False
    return is_adjusted_root(text, base)


def has_standard_contract(ticker: str, contracts: Iterable[OptionContract]) -> bool:
    """True when some contract on ``ticker`` has the ticker as its root and size 100.

    The contract must also be written on ``ticker`` as its underlying: a
    contract fetched for some other name is not evidence about this one.
    Anything short of an exact match answers ``False`` -- this feeds a filter
    that fails closed.

    **Class shares always answer ``False``, deliberately.** OCC writes a
    class-share root without the dot (``BRKB`` for ``BRK.B``), so the root
    never equals the ticker and ``BRK.B`` is silently excluded from discovery.
    That is a known, accepted exclusion: treating ``BRKB`` as ``BRK.B``'s
    standard root is a mapping to add explicitly, with a test, not to infer.
    """
    symbol = normalize_symbol(ticker)
    for contract in contracts:
        if normalize_symbol(contract.underlying_symbol) != symbol:
            continue
        if is_adjusted_root(contract.root_symbol, symbol):
            continue
        if contract.size == STANDARD_CONTRACT_SIZE:
            return True
    return False


def adv_window(
    session_date: date, *, is_session: IsSession = nyse_is_session
) -> tuple[date, ...]:
    """The :data:`ADV_SESSIONS` exchange sessions immediately before ``session_date``.

    Oldest first. ``session_date`` itself is never in it, whether or not it is
    a session. Raises ``ValueError`` when the calendar cannot find 20 sessions
    within :data:`_MAX_WINDOW_CALENDAR_DAYS` -- a date outside the published
    schedule, which must not be answered with a shorter window.
    """
    found: list[date] = []
    day = session_date
    for _ in range(_MAX_WINDOW_CALENDAR_DAYS):
        day -= timedelta(days=1)
        if is_session(day):
            found.append(day)
            if len(found) == ADV_SESSIONS:
                return tuple(reversed(found))
    raise ValueError(
        f"the market calendar lists only {len(found)} sessions in the "
        f"{_MAX_WINDOW_CALENDAR_DAYS} days before {session_date}; the average needs "
        f"{ADV_SESSIONS}"
    )


def adv_window_start(session_date: date, *, is_session: IsSession = nyse_is_session) -> date:
    """The first session of :func:`adv_window` -- the date a bar request should start at."""
    return adv_window(session_date, is_session=is_session)[0]


def _completed_bars(
    ticker: str, daily_bars: Sequence[Bar], session_date: date
) -> dict[date, Bar]:
    """The bars for sessions strictly before ``session_date``, keyed by NY session date.

    Raises on input that cannot be a single symbol's daily series: a bar for
    another symbol, a naive timestamp, two bars on one session, a float close,
    or a negative volume. Those are bugs upstream, and a filter quietly
    averaging them would be judging on data nobody meant to give it.
    """
    by_session: dict[date, Bar] = {}
    for item in daily_bars:
        if normalize_symbol(item.symbol) != ticker:
            raise ValueError(f"a bar for {item.symbol!r} was passed to the {ticker} filter")
        if item.at.tzinfo is None or item.at.utcoffset() is None:
            raise ValueError(f"the {ticker} bar at {item.at} has no timezone")
        if not isinstance(item.close, Decimal):
            raise TypeError(
                f"the {ticker} bar at {item.at} carries a {type(item.close).__name__} "
                "close; money is Decimal"
            )
        if item.volume < 0:
            raise ValueError(f"the {ticker} bar at {item.at} carries a negative volume")
        session = item.at.astimezone(NYSE_TZ).date()
        if session in by_session:
            raise ValueError(f"two daily bars for {ticker} on the {session} session")
        by_session[session] = item
    return {day: item for day, item in by_session.items() if day < session_date}


def assess_tradeability(
    ticker: str,
    *,
    has_options: bool,
    standard_root: bool | None,
    daily_bars: Sequence[Bar],
    session_date: date,
    is_session: IsSession = nyse_is_session,
) -> TradeabilityResult:
    """Run every tradeability check on one ticker for one session date.

    ``has_options`` is the asset list's flag. ``standard_root`` is
    :func:`has_standard_contract`'s answer, or ``None`` when it was not
    checked -- which fails. ``daily_bars`` are the ticker's daily bars from the
    historical feed, in any order, starting no later than
    :func:`adv_window_start`; only sessions before ``session_date`` count.
    ``is_session`` is the market calendar (see the module docstring).

    Every check runs, so the result names every reason, and the first is
    what a caller logs. A malformed ticker fails rather than raising: tickers
    reach this from vendor tags, and one bad tag must not stop a batch.
    """
    symbol = normalize_symbol(ticker)
    failures: list[TradeabilityFailure] = []

    adjusted = is_adjusted_root_ticker(symbol)
    if not adjusted and not EQUITY_SYMBOL_RE.fullmatch(symbol):
        failures.append(TradeabilityFailure.MALFORMED_TICKER)

    window = adv_window(session_date, is_session=is_session)
    completed = _completed_bars(symbol, daily_bars, session_date)
    newest = max(completed) if completed else None
    oldest = min(completed) if completed else None

    last_close = completed[newest].close if newest is not None else None
    in_window = [completed[day] for day in window if day in completed]
    sessions_available = len(in_window)
    has_history = oldest is not None and oldest <= window[0]
    stale = newest is not None and newest != window[-1]

    avg_volume: int | None = None
    if has_history:
        # A window session with no bar is a session with no trades: it adds
        # zero to the sum and still counts in the divisor. Floor, in
        # integers: a mean of 999,999.95 is 999,999 and fails.
        avg_volume = sum(item.volume for item in in_window) // ADV_SESSIONS

    if not has_options:
        failures.append(TradeabilityFailure.NO_OPTIONS)
    if adjusted:
        failures.append(TradeabilityFailure.ADJUSTED_ROOT)
    if standard_root is False:
        failures.append(TradeabilityFailure.NO_STANDARD_CONTRACT)
    elif standard_root is None:
        failures.append(TradeabilityFailure.STANDARD_ROOT_UNCHECKED)
    if not has_history:
        failures.append(TradeabilityFailure.INSUFFICIENT_HISTORY)
    if stale:
        failures.append(TradeabilityFailure.STALE_BARS)
    if avg_volume is not None and avg_volume < MIN_AVG_DAILY_VOLUME:
        failures.append(TradeabilityFailure.LOW_VOLUME)
    if last_close is not None and last_close < MIN_LAST_CLOSE:
        failures.append(TradeabilityFailure.LOW_CLOSE)

    return TradeabilityResult(
        ticker=symbol,
        session_date=session_date,
        has_options=has_options,
        standard_root=standard_root,
        avg_volume_20d=avg_volume,
        last_close=last_close,
        sessions_available=sessions_available,
        failures=tuple(failures),
    )


# ==========================================================================
# The session cache -- ``ticker_tradeability``
# ==========================================================================
#
# Decision 21: one row per ticker per session date, filled lazily and only for
# off-watch tickers that carry a qualifying signal -- which in step 4 means any
# off-watch ticker an article names, since labels arrive in step 5. The
# ``has_options`` list is the asset directory, refreshed daily elsewhere
# (``corollary.data.news.assets``); the standard-root check and the ADV run
# once per ticker per session date, and a *failure* is cached for the session
# like a pass, so a failing ticker is not re-checked every cycle.
#
# Four rules the code below implements rather than works around:
#
# * **Unchecked is not failed.** A provider error on a ticker's bars or its
#   root check caches nothing: the ticker stays unchecked, is reported, and is
#   retried next cycle. Caching it would bury a vendor outage as a day of
#   "not tradeable".
# * **No request whose answer cannot change the verdict.** The root check
#   costs one request on the trading host; it is asked only when every other
#   check already passes. A ticker failing elsewhere is cached with
#   ``standard_root = NULL`` (not asked) and its real reasons -- see
#   :func:`_verdict_without_root_check`.
# * **Bars for every well-formed ticker**, in batches of at most
#   :data:`ADV_BATCH_SIZE`, even for one without options: a batch costs one
#   request whatever its size, and a cached failure should state the ticker's
#   real volume and close rather than an invented "insufficient history".
#   Always ``adv_daily_bars`` -- the historical feed -- never a snapshot.
# * **No sleeps.** The provider's shared host limiter paces every request;
#   the work per run is bounded by :data:`MAX_TICKERS_PER_RUN` instead.

#: Most tickers one :func:`refresh_tradeability` run checks. Each can cost a
#: standard-root request on the trading host, whose 200/min bucket is shared
#: with every account read, so a run is bounded to half of one minute's
#: bucket. The remainder is reported as deferred and stays unchecked, so the
#: next cycle takes it up.
MAX_TICKERS_PER_RUN: Final = 100

#: Symbols per ADV bars request. The provider's own ceiling
#: (``ADV_MAX_SYMBOLS``: 200 symbols x 20 sessions = 4,000 points, under the
#: 10,000-point page), restated here because this module must not import the
#: vendor file; a test pins the two equal.
ADV_BATCH_SIZE: Final = 200

_NO_ASSET_DIRECTORY: Final = (
    "no asset directory has been fetched yet, so has_options is unknown; "
    "nothing was checked or cached"
)

_NO_OPTIONABLE_NAME: Final = (
    "the asset directory names no optionable equity, which is a vendor fault "
    "(an empty list, or attributes dropped), not a day on which every name lost "
    "its options; nothing was checked or cached"
)

UtcClock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TradeabilityInputs(Protocol):
    """The two provider requests the refresh makes. ``AlpacaProvider`` satisfies it.

    Both raise ``ProviderError`` when the vendor cannot answer, and the
    refresh reads that as *unchecked*, never as *no*.
    """

    async def has_standard_root(self, ticker: str) -> bool: ...

    async def adv_daily_bars(
        self, symbols: Sequence[str], *, session_date: date
    ) -> Mapping[str, Sequence[Bar]]: ...


class CheckStage(StrEnum):
    """Which input the vendor failed to give."""

    DAILY_BARS = "daily_bars"
    STANDARD_ROOT = "standard_root"


@dataclass(frozen=True, slots=True)
class TickerCheckError:
    """A ticker left unchecked this run because an input could not be had."""

    ticker: str
    stage: CheckStage
    error: str


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """What one :func:`refresh_tradeability` run did.

    ``skipped`` is ``None`` for a run that ran, else why it did not -- a
    skipped run checked and cached nothing. ``results`` are the verdicts
    cached, in the order checked; ``errors`` the tickers left unchecked by a
    provider failure; ``deferred`` the tickers beyond the per-run bound,
    unchecked, in the order given.
    """

    session_date: date
    skipped: str | None
    results: tuple[TradeabilityResult, ...]
    errors: tuple[TickerCheckError, ...]
    deferred: tuple[str, ...]
    bar_requests: int
    root_requests: int

    @property
    def passed(self) -> tuple[str, ...]:
        return tuple(result.ticker for result in self.results if result.passes)


def _ordered_unique(tickers: Iterable[str]) -> list[str]:
    """Normalised, blanks dropped, first occurrence kept."""
    return list(dict.fromkeys(t for t in (normalize_symbol(raw) for raw in tickers) if t))


def recent_article_tickers(session: Session, since: datetime) -> list[str]:
    """Every ticker tagged on an article published at or after ``since``.

    Step 4's candidate set: a tag is the only signal before step 5's labels.
    Ordered by each ticker's latest article, newest first, ties by symbol, so
    a run bounded by :data:`MAX_TICKERS_PER_RUN` checks the freshest news
    first. ``MARKET`` is returned like any other tag;
    :func:`tickers_needing_check` is what excludes it.
    """
    require_aware(since, "since")
    latest = func.max(NewsArticle.published_at).label("latest")
    statement = (
        select(NewsArticleTicker.ticker, latest)
        .join(NewsArticle, NewsArticle.id == NewsArticleTicker.article_id)
        .where(NewsArticle.published_at >= since)
        .group_by(NewsArticleTicker.ticker)
        .order_by(latest.desc(), NewsArticleTicker.ticker)
    )
    return [row.ticker for row in session.execute(statement)]


def tickers_needing_check(
    session: Session,
    *,
    candidates: Iterable[str],
    watch: WatchUniverse,
    session_date: date,
) -> list[str]:
    """The candidates the cache has no ``session_date`` row for, off-watch only.

    Normalised and de-duplicated, caller order kept. A watched ticker is never
    checked -- it is already polled, and it can never be a discovery
    candidate -- and neither is ``MARKET``, which is a tag value, not a ticker.
    A row for any *other* session date does not count: the verdict is per
    session.
    """
    off_watch = [
        ticker
        for ticker in _ordered_unique(candidates)
        if ticker != MARKET_TICKER and ticker not in watch
    ]
    if not off_watch:
        return []
    cached = set(
        session.scalars(
            select(TickerTradeability.ticker).where(
                TickerTradeability.session_date == session_date
            )
        )
    )
    return [ticker for ticker in off_watch if ticker not in cached]


#: Rows per upsert statement: at 10 bound parameters a row this keeps a
#: statement far inside SQLite's parameter limit whatever ``max_tickers`` a
#: caller passes.
_UPSERT_CHUNK: Final = 200

_UPDATED_COLUMNS: Final = (
    "has_options",
    "standard_root",
    "avg_volume_20d",
    "last_close",
    "sessions_available",
    "passes",
    "failures",
    "checked_at",
)


def store_tradeability(
    session: Session, results: Sequence[TradeabilityResult], *, checked_at: datetime
) -> int:
    """Upsert ``results`` into ``ticker_tradeability``. Does not commit.

    ``INSERT ... ON CONFLICT DO UPDATE`` on the ``(ticker, session_date)``
    primary key, so a second write for the same session replaces the first
    rather than colliding. ``failures`` is the result's failures comma-joined
    in declaration order, ``''`` exactly when it passes. Returns rows written.

    Raises ``ValueError``, writing nothing, if any passing result lacks
    ``has_options is True and standard_root is True``. A
    :class:`TradeabilityResult` is a plain dataclass, so a hand-built pass
    whose standard-contract check never ran is constructible; stored, it
    would be a discovery candidate that could be an adjusted root with a
    deliverable other than 100 shares. The whole batch is refused because
    one such row means its caller is wrong about all of them.
    """
    for result in results:
        if result.passes and not (result.has_options is True and result.standard_root is True):
            raise ValueError(
                f"{result.ticker} is marked passing with has_options={result.has_options!r} "
                f"and standard_root={result.standard_root!r}; a pass needs both True, so "
                "nothing was stored"
            )
    require_aware(checked_at, "checked_at")
    stamp = checked_at.astimezone(timezone.utc)
    rows = [
        {
            "ticker": result.ticker,
            "session_date": result.session_date,
            "has_options": result.has_options,
            "standard_root": result.standard_root,
            "avg_volume_20d": result.avg_volume_20d,
            "last_close": result.last_close,
            "sessions_available": result.sessions_available,
            "passes": result.passes,
            "failures": ",".join(failure.value for failure in result.failures),
            "checked_at": stamp,
        }
        for result in results
    ]
    for start in range(0, len(rows), _UPSERT_CHUNK):
        statement = sqlite_insert(TickerTradeability).values(rows[start : start + _UPSERT_CHUNK])
        statement = statement.on_conflict_do_update(
            index_elements=[TickerTradeability.ticker, TickerTradeability.session_date],
            set_={column: statement.excluded[column] for column in _UPDATED_COLUMNS},
        )
        session.execute(statement)
    return len(rows)


def _store(
    session_factory: Callable[[], Session],
    results: Sequence[TradeabilityResult],
    checked_at: datetime,
) -> None:
    """The blocking half of a refresh. Only ever called through ``to_thread``."""
    with session_factory() as session:
        store_tradeability(session, results, checked_at=checked_at)
        session.commit()


def _verdict_without_root_check(preliminary: TradeabilityResult) -> TradeabilityResult:
    """The cached verdict for a ticker that fails on something other than its root.

    ``preliminary`` was assessed *as if* the standard root existed, and failed
    anyway, so no answer to the root question could make it pass: the request
    is not spent. The row records ``standard_root = None`` -- not asked -- and
    the failures that actually decided it. ``STANDARD_ROOT_UNCHECKED`` is not
    added: it means *fails closed because the check could not be made*, which
    is not what happened, and as the first failure it would displace the real
    reason (a low close, no options) from the head of the list a caller logs.
    """
    if preliminary.passes:
        raise ValueError(f"{preliminary.ticker} passes; its root check is not moot")
    return replace(preliminary, standard_root=None)


def _describe_error(exc: BaseException) -> str:
    # Provider errors are scrubbed by the provider before they are raised.
    return f"{type(exc).__name__}: {exc}"


def _log_unavailable(
    tickers: Sequence[str], stage: CheckStage, error: str, session_date: date
) -> None:
    logger.warning(
        "tradeability %s unavailable for %d ticker(s); left unchecked, retried next cycle",
        stage.value,
        len(tickers),
        extra={
            "event": "tradeability_check_unavailable",
            "rule": (
                "a provider error is not a failure: nothing is cached, so the "
                "ticker is checked again next cycle"
            ),
            "stage": stage.value,
            "tickers": list(tickers),
            "session_date": session_date.isoformat(),
            "error": error,
        },
    )


def _log_verdicts(results: Sequence[TradeabilityResult], session_date: date) -> None:
    for result in results:
        logger.debug(
            "tradeability %s for %s: %s",
            "pass" if result.passes else "fail",
            result.ticker,
            ",".join(result.failures) or "every check held",
            extra={
                "event": "tradeability_checked",
                "ticker": result.ticker,
                "session_date": session_date.isoformat(),
                "passes": result.passes,
                "failures": [failure.value for failure in result.failures],
                "has_options": result.has_options,
                "standard_root": result.standard_root,
                "avg_volume_20d": result.avg_volume_20d,
                "last_close": None if result.last_close is None else str(result.last_close),
            },
        )


def _directory_unusable(assets: AssetDirectory) -> str | None:
    """Why ``assets`` cannot answer ``has_options`` for a run, or ``None`` if it can.

    Two shapes of the same vendor fault: no optionable name at all (an empty
    list, or every asset read as ``has_options=False``), and more than half
    the directory arriving without ``attributes`` -- the field the answer is
    read from. Exactly half still runs. Either way, running would cache every
    candidate as "no options" and hide it for the session.
    """
    if not assets.optionable():
        return _NO_OPTIONABLE_NAME
    if assets.missing_attributes * 2 > len(assets):
        return (
            f"{assets.missing_attributes} of the asset directory's {len(assets)} rows "
            "carried no attributes, more than half, so has_options cannot be trusted; "
            "nothing was checked or cached"
        )
    return None


def _skipped_run(
    reason: str, assets: AssetDirectory | None, tickers: Sequence[str], session_date: date
) -> RefreshResult:
    """Log a run that checked nothing, and say why in its result."""
    logger.warning(
        "tradeability refresh skipped: %s",
        reason,
        extra={
            "event": "tradeability_refresh_skipped",
            "rule": "no trustworthy has_options answer, so no verdict is cached",
            "session_date": session_date.isoformat(),
            "tickers": len(tickers),
            "asset_directory_size": None if assets is None else len(assets),
            "optionable": None if assets is None else len(assets.optionable()),
            "missing_attributes": None if assets is None else assets.missing_attributes,
        },
    )
    return RefreshResult(
        session_date=session_date,
        skipped=reason,
        results=(),
        errors=(),
        deferred=(),
        bar_requests=0,
        root_requests=0,
    )


async def refresh_tradeability(
    *,
    provider: TradeabilityInputs,
    assets: AssetDirectory | None,
    tickers: Sequence[str],
    session_date: date,
    session_factory: Callable[[], Session],
    now: UtcClock = _utc_now,
    max_tickers: int = MAX_TICKERS_PER_RUN,
    is_session: IsSession = nyse_is_session,
) -> RefreshResult:
    """Check ``tickers`` for ``session_date`` and cache every verdict reached.

    ``tickers`` should be :func:`tickers_needing_check`'s answer; this function
    does not consult the watch universe or the cache itself, and re-checking a
    cached ticker simply replaces its row. ``MARKET`` and blanks are dropped
    here regardless. The first ``max_tickers`` are checked and the rest
    returned as deferred.

    ``assets=None`` -- no directory fetched yet -- skips the run: without it
    ``has_options`` is unknown, and caching every ticker as "no options" would
    hide them for the session. The same holds for a directory that exists but
    cannot answer -- no optionable name at all, or more than half its rows
    missing ``attributes`` -- and the run is skipped with the reason. A ticker
    absent from a usable directory reads as ``has_options = False``: the
    directory lists every active US equity, so absence is an answer.

    Staleness is not checked here. The caller (the scheduler) decides whether
    a directory older than :data:`corollary.data.news.assets.MAX_DIRECTORY_AGE`
    may still be used,
    and must log it when it does.

    A ticker that is not a well-formed equity symbol (a crypto pair, an
    adjusted root such as ``AIFU1``) is assessed with no bars and no root
    check and cached as the failure it is -- no request could be made for it,
    and one bad symbol in a bars batch would fail the other 199.
    """
    if max_tickers < 1:
        raise ValueError(f"max_tickers must be at least 1, not {max_tickers}")
    if assets is None:
        return _skipped_run(_NO_ASSET_DIRECTORY, assets, tickers, session_date)
    unusable = _directory_unusable(assets)
    if unusable is not None:
        return _skipped_run(unusable, assets, tickers, session_date)
    # A calendar that cannot list the window raises here, once, rather than
    # once per ticker after the bars have already been paid for.
    adv_window(session_date, is_session=is_session)

    ordered = [t for t in _ordered_unique(tickers) if t != MARKET_TICKER]
    batch, deferred = ordered[:max_tickers], ordered[max_tickers:]

    errors: list[TickerCheckError] = []
    unavailable: set[str] = set()
    bars: dict[str, Sequence[Bar]] = {}
    bar_requests = 0
    requestable = [t for t in batch if EQUITY_SYMBOL_RE.fullmatch(t)]
    for start in range(0, len(requestable), ADV_BATCH_SIZE):
        chunk = requestable[start : start + ADV_BATCH_SIZE]
        bar_requests += 1
        try:
            answer = await provider.adv_daily_bars(chunk, session_date=session_date)
        except ProviderError as exc:
            error = _describe_error(exc)
            errors.extend(TickerCheckError(t, CheckStage.DAILY_BARS, error) for t in chunk)
            unavailable.update(chunk)
            _log_unavailable(chunk, CheckStage.DAILY_BARS, error, session_date)
            continue
        for ticker in chunk:
            # The vendor omits a symbol with no bars in the range: no bars.
            bars[ticker] = answer.get(ticker, ())

    results: list[TradeabilityResult] = []
    root_requests = 0
    for ticker in batch:
        if ticker in unavailable:
            continue
        asset = assets.get(ticker)
        has_options = asset is not None and asset.has_options
        daily = bars.get(ticker, ())
        try:
            preliminary = assess_tradeability(
                ticker,
                has_options=has_options,
                standard_root=True,
                daily_bars=daily,
                session_date=session_date,
                is_session=is_session,
            )
        except (ValueError, TypeError) as exc:
            # The filter refuses bars that cannot be one symbol's daily series.
            # That is a defect in the vendor's data, not a verdict on the ticker.
            error = _describe_error(exc)
            errors.append(TickerCheckError(ticker, CheckStage.DAILY_BARS, error))
            _log_unavailable([ticker], CheckStage.DAILY_BARS, error, session_date)
            continue
        if not preliminary.passes:
            results.append(_verdict_without_root_check(preliminary))
            continue
        root_requests += 1
        try:
            standard_root = await provider.has_standard_root(ticker)
        except ProviderError as exc:
            error = _describe_error(exc)
            errors.append(TickerCheckError(ticker, CheckStage.STANDARD_ROOT, error))
            _log_unavailable([ticker], CheckStage.STANDARD_ROOT, error, session_date)
            continue
        results.append(
            assess_tradeability(
                ticker,
                has_options=has_options,
                standard_root=standard_root,
                daily_bars=daily,
                session_date=session_date,
                is_session=is_session,
            )
        )

    checked_at = now()
    require_aware(checked_at, "now")
    if results:
        await asyncio.to_thread(_store, session_factory, results, checked_at)
    _log_verdicts(results, session_date)

    outcome = RefreshResult(
        session_date=session_date,
        skipped=None,
        results=tuple(results),
        errors=tuple(errors),
        deferred=tuple(deferred),
        bar_requests=bar_requests,
        root_requests=root_requests,
    )
    logger.log(
        logging.WARNING if errors or deferred else logging.INFO,
        "tradeability refresh for %s: %d cached (%d pass), %d unchecked on a provider "
        "error, %d deferred",
        session_date.isoformat(),
        len(results),
        len(outcome.passed),
        len(errors),
        len(deferred),
        extra={
            "event": "tradeability_refreshed",
            "session_date": session_date.isoformat(),
            "cached": len(results),
            "passed": len(outcome.passed),
            "unchecked": len(errors),
            "deferred": len(deferred),
            "bar_requests": bar_requests,
            "root_requests": root_requests,
            "asset_directory_size": len(assets),
        },
    )
    return outcome
