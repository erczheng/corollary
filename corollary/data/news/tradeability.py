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
  sessions, or over every completed session a recent listing has
  *(assumption)* -- and since owner decision Q12 (2026-09-26) a ticker is a
  recent listing only on a real IPO date (below);
* last close >= $5 *(assumption)*;
* at least **one** completed session -- a real close and a real volume.
  Owner decision Q10 (2026-09-26): recent IPOs reach discovery, so decision
  21's "at least 20 sessions of history" is gone. ``has_options`` is what
  gates a fresh IPO until its options list. The specifics -- the floor of
  one, the partial average below -- are *parent-session assumptions the
  owner can override*;
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
  zero volume -- and still counts in the divisor, whatever the bar count;
* an **established** name -- any completed bar dated before the window's
  first session -- is averaged over all 20: ``floor(sum / 20)``. This is the
  pre-Q10 rule unchanged, and it is what keeps a partial window from being
  confused with missing bars: a long-listed name with no bar on ten window
  sessions is ten zero-volume sessions, never a ten-session listing;
* a first completed bar **on** the window's first session is a full window
  -- 20 sessions either way -- and needs nothing more;
* a **partial window** -- first completed bar *after* the window's first
  session, none before it -- is settled by the ticker's **IPO date** (owner
  decision Q12), passed in as ``ipo_date``:

  - on or after the window's first session, and not after the first bar: a
    **recent listing**, averaged over the window sessions from the earlier
    of the IPO date and the first bar, gaps counting zero:
    ``floor(sum / N)``, N from 1 to 20;
  - before the window's first session: an established name **missing
    bars**, averaged over all 20 as above -- it fails as it did before Q10;
  - none (not looked up, not on file, missing/empty/malformed in the
    vendor's answer), or one *later* than the first bar, which the tape
    contradicts: :attr:`TradeabilityFailure.IPO_DATE_UNAVAILABLE`, with no
    average at all. An IPO is never assumed;
* **no completed session** -- no bar before ``session_date``, or none on or
  before the window's last session -- fails
  :attr:`TradeabilityFailure.NO_COMPLETED_SESSION` with no average at all;
  never a division by zero, never a pass;
* **staleness**: if the newest bar before ``session_date`` is not dated the
  window's last session (the previous session), the ticker fails
  :attr:`TradeabilityFailure.STALE_BARS`. ``last_close`` is still the newest
  bar's close, so the cache row shows the price it would have been judged on.
  A bar dated on a day the calendar does not list as a session is not dropped
  or trusted: it is simply not a window session, and if it is the newest bar
  the ticker reads stale -- a visible disagreement, failing closed.

**What the caller must fetch.** The window *and* the
:data:`ADV_LOOKBACK_SESSIONS` sessions before it -- 272 sessions, about 395
calendar days. Do not guess the span: :func:`adv_request_start` returns its
first session. The lookback is what tells established from recently listed:
a request starting at the window would make every name that skipped the
window's first session look like a listing, and judge it on a shorter,
kinder average. Any older bar the caller has is welcome and counts.

**The residual ambiguity, and how Q12 closed it.** Bars alone cannot tell
a recent IPO from a long-listed name with no bar anywhere in the lookback --
silent for more than 252 sessions (a year), then resumed inside the window.
Alpaca's asset record carries no listing date, so Q10 left that case judged
as a recent listing, an open owner question. Owner decision Q12 answered it:
a partial window is a recent listing only when Finnhub's ``/stock/profile2``
``ipo`` date falls on or after the window's first session. The one-year
lookback stays, because it keeps the question rare -- any name with a bar in
the past year is established without asking. The refresh below asks only
for a partial window that would otherwise pass, caches each date for good in
``ticker_ipo_date``, and fails closed when no date can be had.

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
closed; ``ipo_date=None`` on a partial window means "no usable date" and
fails closed. With no completed session, or a partial window without a
usable IPO date, no average is computed at all (``avg_volume_20d`` is
``None``). A recent listing's partial average *is*
recorded in ``avg_volume_20d`` -- the field keeps its name -- and
``sessions_available`` carries the divisor, so a reader never mistakes a
5-session mean for a 20-session one.
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

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ, nyse_session_close
from corollary.data.news.watchlist import MARKET_TICKER, WatchUniverse
from corollary.data.providers.fundamentals import FundamentalsError
from corollary.data.providers.interface import AssetDirectory, Bar, OptionContract, ProviderError
from corollary.data.seeds import EQUITY_SYMBOL_RE, normalize_symbol
from corollary.db.models import TickerIpoDate, TickerTradeability
from corollary.instruments import is_adjusted_root
from corollary.wire import require_aware

__all__ = [
    "ADV_BATCH_SIZE",
    "ADV_LOOKBACK_SESSIONS",
    "ADV_SESSIONS",
    "MAX_IPO_LOOKUPS_PER_RUN",
    "MAX_TICKERS_PER_RUN",
    "MIN_AVG_DAILY_VOLUME",
    "MIN_LAST_CLOSE",
    "MIN_COMPLETED_SESSIONS",
    "STANDARD_CONTRACT_SIZE",
    "CheckStage",
    "IpoDateSource",
    "IsSession",
    "RefreshResult",
    "TickerCheckError",
    "TradeabilityFailure",
    "TradeabilityInputs",
    "TradeabilityResult",
    "adv_request_start",
    "adv_window",
    "adv_window_start",
    "assess_tradeability",
    "has_standard_contract",
    "is_adjusted_root_ticker",
    "nyse_is_session",
    "partial_window_first_session",
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

#: Completed sessions required before any judgement -- Q10, 2026-09-26:
#: recent IPOs reach discovery, so one real close and one real volume are
#: enough *(parent-session assumption the owner can override)*. Stated as a
#: constant so the rule has a name; the code below reads "at least one" as
#: "the ADV divisor is not zero", which is the same condition.
MIN_COMPLETED_SESSIONS: Final = 1

#: The trailing window the average is taken over, in exchange sessions.
ADV_SESSIONS: Final = 20

#: Sessions before the window the bar request also covers, so the filter can
#: tell a **recent listing** (first bar inside the window) from an
#: **established name with missing bars** (a bar before the window) -- Q10.
#: 252 -- one year of sessions -- so that only a name silent for more than a
#: year and then resumed can read as a recent listing (see the module
#: docstring; raised from 40 in the Q10 fix round, when an audit showed a
#: two-month suspension passing as a five-session IPO). The request spans 272
#: sessions: at the refresh's 100-ticker bound that is 27,200 points, three
#: 10,000-point pages -- two extra requests on the data bucket per refresh run;
#: at the provider's 200-symbol ceiling, 54,400 points, six pages.
ADV_LOOKBACK_SESSIONS: Final = 252

#: The ``size`` a standard contract carries. A filter criterion from decision
#: 21, **never a multiplier** -- Alpaca's own spec says ``size`` must not be
#: used as one, and ``OptionContract.multiplier`` is the field for that.
STANDARD_CONTRACT_SIZE: Final = Decimal(100)

#: How far back the 20-session window walk goes before concluding it cannot
#: answer. 20 sessions span about 28 calendar days; 180 is a bound on a loop,
#: not an estimate. Kept separate from the lookback's bound on purpose: a
#: bound wide enough for a year would let a date far outside the published
#: schedule find a window rather than raise.
_MAX_WINDOW_CALENDAR_DAYS: Final = 180

#: The same bound for the 272-session request walk. 272 sessions span about
#: 395 calendar days (the widest in the published schedule is 398); 450 is a
#: bound on a loop with room for an unscheduled closure or two, not an
#: estimate.
_MAX_LOOKBACK_CALENDAR_DAYS: Final = 450

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
    #: No completed session to judge on: no bar before ``session_date`` at
    #: all, or none on or before the window's last session. Replaced
    #: ``insufficient_history`` (fewer than 20 sessions) on 2026-09-26 (Q10):
    #: a new token for a new meaning, so a stored row never reads under the
    #: wrong one.
    NO_COMPLETED_SESSION = "no_completed_session"
    #: A partial ADV window with no usable IPO date (owner decision Q12,
    #: 2026-09-26): none was looked up or on file, the vendor's answer had
    #: none (missing, empty, malformed), or it is later than the ticker's own
    #: first bar. Nothing is averaged -- neither divisor can be defended.
    IPO_DATE_UNAVAILABLE = "ipo_date_unavailable"
    STALE_BARS = "stale_bars"
    LOW_VOLUME = "low_volume"
    LOW_CLOSE = "low_close"


@dataclass(frozen=True, slots=True)
class TradeabilityResult:
    """Every field of a ``ticker_tradeability`` cache row, plus why it failed.

    ``avg_volume_20d`` is ``None`` with no completed session to average over
    (:attr:`TradeabilityFailure.NO_COMPLETED_SESSION`); ``last_close`` is
    ``None`` with no completed bar at all. The cache columns for both must
    therefore be nullable. The field keeps its ``_20d`` name for a recent
    listing, whose average runs over fewer sessions; ``sessions_available``
    says how many.

    ``sessions_available`` is **the divisor the average was taken over** --
    what the movers panel prints as "ADV over N sessions". 20 for an
    established name, whatever its bar count: one that did not trade on two
    window sessions still reads 20, because those sessions are in the average
    as zeros. 1 to 20 for a recent listing: the window sessions from its first
    bar's date on (or from its IPO date, if earlier -- Q12), gaps included.
    0 exactly when nothing could be averaged: no completed session, or a
    partial window with no usable IPO date.
    (Before Q10, 2026-09-26, it counted the window sessions carrying a bar;
    rows are per session date, so rows under that meaning age out.)
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
    return _sessions_before(
        session_date, ADV_SESSIONS, is_session, "the average", _MAX_WINDOW_CALENDAR_DAYS
    )


def adv_window_start(session_date: date, *, is_session: IsSession = nyse_is_session) -> date:
    """The first session of :func:`adv_window`. Not where a bar request starts --
    that is :func:`adv_request_start`, which reaches back past it."""
    return adv_window(session_date, is_session=is_session)[0]


def adv_request_start(session_date: date, *, is_session: IsSession = nyse_is_session) -> date:
    """The date a bar request must start at: :data:`ADV_LOOKBACK_SESSIONS` before the window.

    The first of the ``ADV_LOOKBACK_SESSIONS + ADV_SESSIONS`` sessions before
    ``session_date``. Bars from here on let :func:`assess_tradeability` see
    whether a name traded before its window (established) or first traded
    inside it (a recent listing). Raises ``ValueError`` as :func:`adv_window`
    does.
    """
    span = _sessions_before(
        session_date,
        ADV_LOOKBACK_SESSIONS + ADV_SESSIONS,
        is_session,
        "the listing lookback",
        _MAX_LOOKBACK_CALENDAR_DAYS,
    )
    return span[0]


def _sessions_before(
    session_date: date, count: int, is_session: IsSession, purpose: str, max_days: int
) -> tuple[date, ...]:
    """The ``count`` sessions within ``max_days`` before ``session_date``, oldest first, or raise."""
    found: list[date] = []
    day = session_date
    for _ in range(max_days):
        day -= timedelta(days=1)
        if is_session(day):
            found.append(day)
            if len(found) == count:
                return tuple(reversed(found))
    raise ValueError(
        f"the market calendar lists only {len(found)} sessions in the "
        f"{max_days} days before {session_date}; {purpose} needs {count}"
    )


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


def partial_window_first_session(
    ticker: str,
    daily_bars: Sequence[Bar],
    session_date: date,
    *,
    is_session: IsSession = nyse_is_session,
) -> date | None:
    """The first completed session, when it falls *after* the window's first session.

    That is a **partial window** -- fewer than :data:`ADV_SESSIONS` window
    sessions from the first bar on, and no bar before the window -- and it is
    the one case an IPO date settles (Q12). ``None`` for everything else: an
    established name (a bar before the window), a first bar on the window's
    first session (a full window), or no completed session at all. Raises as
    :func:`assess_tradeability` does on bars that cannot be one symbol's
    daily series.
    """
    symbol = normalize_symbol(ticker)
    window = adv_window(session_date, is_session=is_session)
    completed = _completed_bars(symbol, daily_bars, session_date)
    if not completed:
        return None
    oldest = min(completed)
    return oldest if window[0] < oldest <= window[-1] else None


def assess_tradeability(
    ticker: str,
    *,
    has_options: bool,
    standard_root: bool | None,
    daily_bars: Sequence[Bar],
    session_date: date,
    is_session: IsSession = nyse_is_session,
    ipo_date: date | None = None,
) -> TradeabilityResult:
    """Run every tradeability check on one ticker for one session date.

    ``has_options`` is the asset list's flag. ``standard_root`` is
    :func:`has_standard_contract`'s answer, or ``None`` when it was not
    checked -- which fails. ``daily_bars`` are the ticker's daily bars from the
    historical feed, in any order, starting no later than
    :func:`adv_request_start`; only sessions before ``session_date`` count.
    Bars starting later are not refused, but a name whose pre-window bars
    were left out reads as a partial window (see the module docstring).
    ``is_session`` is the market calendar (see the module docstring).
    ``ipo_date`` is the ticker's IPO date, consulted only for a partial
    window -- first completed bar after the window's first session -- and
    ignored otherwise; ``None`` there fails closed (Q12).

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
    stale = newest is not None and newest != window[-1]

    # The sessions the average is taken over (Q10, Q12). Established -- a bar
    # before the window -- or a first bar on the window's first session is
    # all 20, exactly the pre-Q10 rule. A first bar *after* the window's first
    # session is a partial window, and only the IPO date settles it: none
    # usable, nothing is averaged and the ticker fails closed; one before the
    # window, it is an established name missing bars, judged over all 20; one
    # inside the window and not after the first bar, a recent listing,
    # averaged from the earlier of the two -- a listed session with no bar
    # counts zero, as it does for any listed name.
    ipo_unavailable = False
    if oldest is None or oldest > window[-1]:
        adv_sessions: tuple[date, ...] = ()
    elif oldest <= window[0]:
        adv_sessions = window
    elif ipo_date is None or ipo_date > oldest:
        # No usable date: none looked up, none on file, or one later than the
        # ticker's own first bar -- a date the tape contradicts is not
        # evidence of a listing. Never assume an IPO.
        adv_sessions = ()
        ipo_unavailable = True
    elif ipo_date < window[0]:
        adv_sessions = window
    else:
        adv_sessions = tuple(day for day in window if day >= ipo_date)
    sessions_available = len(adv_sessions)
    has_completed_session = oldest is not None and oldest <= window[-1]

    avg_volume: int | None = None
    if sessions_available >= MIN_COMPLETED_SESSIONS:
        # A session with no bar is a session with no trades: it adds zero to
        # the sum and still counts in the divisor. Floor, in integers: a mean
        # of 999,999.95 is 999,999 and fails. The divisor is never zero here.
        total = sum(completed[day].volume for day in adv_sessions if day in completed)
        avg_volume = total // sessions_available

    if not has_options:
        failures.append(TradeabilityFailure.NO_OPTIONS)
    if adjusted:
        failures.append(TradeabilityFailure.ADJUSTED_ROOT)
    if standard_root is False:
        failures.append(TradeabilityFailure.NO_STANDARD_CONTRACT)
    elif standard_root is None:
        failures.append(TradeabilityFailure.STANDARD_ROOT_UNCHECKED)
    if not has_completed_session:
        failures.append(TradeabilityFailure.NO_COMPLETED_SESSION)
    if ipo_unavailable:
        failures.append(TradeabilityFailure.IPO_DATE_UNAVAILABLE)
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
# Five rules the code below implements rather than works around (the fifth
# is owner decision Q12's):
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
#   request (a second page past 10,000 points) whatever its size, and a
#   cached failure should state the ticker's real volume and close rather
#   than an invented "no completed session".
#   Always ``adv_daily_bars`` -- the historical feed -- never a snapshot.
# * **No sleeps.** The provider's shared host limiter paces every request;
#   the work per run is bounded by :data:`MAX_TICKERS_PER_RUN` instead.
# * **An IPO date is asked for at most once per ticker, ever.** Only for a
#   partial window that passes on its kindest reading; a parsed date is
#   stored in ``ticker_ipo_date`` for good; "no date" fails the ticker closed
#   for the session and is asked again next session; a failed request stores
#   nothing and is retried next cycle; :data:`MAX_IPO_LOOKUPS_PER_RUN` bounds
#   the requests per run.

#: Most tickers one :func:`refresh_tradeability` run checks. Each can cost a
#: standard-root request on the trading host, whose 200/min bucket is shared
#: with every account read, so a run is bounded to half of one minute's
#: bucket. The remainder is reported as deferred and stays unchecked, so the
#: next cycle takes it up.
MAX_TICKERS_PER_RUN: Final = 100

#: Most IPO-date lookups one :func:`refresh_tradeability` run makes (owner
#: decision Q12). Each is one ``/stock/profile2`` request on the ``finnhub.io``
#: bucket -- 60/min, shared with market cap and the news polls -- so a run
#: takes at most a third of one minute's bucket. A ticker past the bound is
#: deferred, unchecked, and asked on a later cycle. A date that comes back is
#: cached for good, so steady state is close to zero requests.
MAX_IPO_LOOKUPS_PER_RUN: Final = 20

#: Symbols per ADV bars request. The provider's own ceiling
#: (``ADV_MAX_SYMBOLS``), restated here because this module must not import the
#: vendor file; a test pins the two equal. Since Q10 a request spans 272
#: sessions (window plus the one-year lookback), so a full 200-symbol batch is
#: 54,400 points -- six 10,000-point pages. A run checks at most
#: :data:`MAX_TICKERS_PER_RUN` (100) tickers, 27,200 points, so in practice
#: every run is one batch of three pages on the data bucket.
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
    ) -> Mapping[str, Sequence[Bar]]:
        """Each symbol's daily bars on the historical feed, before ``session_date``.

        **Contract: the bars must start at or before**
        ``adv_request_start(session_date)`` -- the ADV window *and* the
        :data:`ADV_LOOKBACK_SESSIONS` sessions before it. Nothing here can
        check that a name's older bars were merely not asked for, so a short
        request is not refused: it is silently wrong. A name whose pre-window
        bars were left out has its first bar inside the window, so it reads as
        a partial window: an IPO lookup is spent on it (Q12), and only its
        IPO date then keeps it from being averaged over the sessions since
        that bar -- a smaller divisor that can lift an established name with
        gaps over the volume floor. Any older bar is
        welcome and counts. Bars for ``session_date`` or later may be
        included; they are dropped.
        """
        ...


class IpoDateSource(Protocol):
    """Where a partial window's IPO date comes from (owner decision Q12).

    ``FinnhubProvider`` satisfies it, reading ``/stock/profile2``'s ``ipo``.
    Two outcomes, never confused:

    * a ``date``, or ``None`` -- **the vendor answered**. ``None`` means the
      answer had no usable date (missing, empty, malformed), and the ticker
      fails closed with ``ipo_date_unavailable`` for this session;
    * a raised :class:`FundamentalsError` or ``ProviderError`` -- **the vendor
      could not be asked** (a 403, a timeout). The ticker stays unchecked,
      nothing is cached, and the next cycle asks again.
    """

    async def ipo_date(self, symbol: str) -> date | None: ...


class CheckStage(StrEnum):
    """Which input the vendor failed to give."""

    DAILY_BARS = "daily_bars"
    STANDARD_ROOT = "standard_root"
    IPO_DATE = "ipo_date"


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
    unchecked, in the order given -- a ticker deferred for want of an IPO
    lookup this run (:data:`MAX_IPO_LOOKUPS_PER_RUN`) among them.
    """

    session_date: date
    skipped: str | None
    results: tuple[TradeabilityResult, ...]
    errors: tuple[TickerCheckError, ...]
    deferred: tuple[str, ...]
    bar_requests: int
    root_requests: int
    #: ``/stock/profile2`` requests made for an IPO date (Q12); a date on file
    #: costs none.
    ipo_requests: int = 0

    @property
    def passed(self) -> tuple[str, ...]:
        return tuple(result.ticker for result in self.results if result.passes)


def _ordered_unique(tickers: Iterable[str]) -> list[str]:
    """Normalised, blanks dropped, first occurrence kept."""
    return list(dict.fromkeys(t for t in (normalize_symbol(raw) for raw in tickers) if t))


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
    ipo_dates: Mapping[str, date],
    checked_at: datetime,
) -> None:
    """The blocking half of a refresh. Only ever called through ``to_thread``."""
    with session_factory() as session:
        if results:
            store_tradeability(session, results, checked_at=checked_at)
        if ipo_dates:
            _store_ipo_dates(session, ipo_dates, fetched_at=checked_at)
        session.commit()


def _store_ipo_dates(
    session: Session, ipo_dates: Mapping[str, date], *, fetched_at: datetime
) -> None:
    """Insert newly fetched IPO dates into ``ticker_ipo_date``. Does not commit.

    Only parsed dates reach here -- ``None`` and failures are never stored as
    a permanent answer (Q12). ``ON CONFLICT DO NOTHING``: the date does not
    change, and a row already there was fetched first.
    """
    require_aware(fetched_at, "fetched_at")
    stamp = fetched_at.astimezone(timezone.utc)
    rows = [
        {"ticker": ticker, "ipo_date": ipo, "fetched_at": stamp}
        for ticker, ipo in ipo_dates.items()
    ]
    statement = sqlite_insert(TickerIpoDate).values(rows).on_conflict_do_nothing(
        index_elements=[TickerIpoDate.ticker]
    )
    session.execute(statement)


def _load_ipo_dates(
    session_factory: Callable[[], Session], tickers: Sequence[str]
) -> dict[str, date]:
    """The IPO dates already on file for ``tickers``. Only called through ``to_thread``."""
    with session_factory() as session:
        found = session.execute(
            select(TickerIpoDate.ticker, TickerIpoDate.ipo_date).where(
                TickerIpoDate.ticker.in_(tickers)
            )
        )
        return {row.ticker: row.ipo_date for row in found}


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


def _log_ipo_date_unavailable(
    ticker: str, cause: str, session_date: date, first_session: date, ipo: date | None
) -> None:
    """Rule 8 for Q12: a partial window failed closed for want of a usable IPO date."""
    logger.warning(
        "tradeability: %s has a partial ADV window and no usable IPO date (%s); failed closed",
        ticker,
        cause,
        extra={
            "event": "tradeability_ipo_date_unavailable",
            "rule": (
                "owner decision Q12: a partial ADV window is a recent listing only "
                "on an IPO date on or after the window's start; an IPO is never "
                "assumed, so the ticker fails ipo_date_unavailable for this session"
            ),
            "ticker": ticker,
            "session_date": session_date.isoformat(),
            "first_completed_session": first_session.isoformat(),
            "ipo_date": None if ipo is None else ipo.isoformat(),
            "cause": cause,
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
    ipo_dates: IpoDateSource | None = None,
    max_ipo_lookups: int = MAX_IPO_LOOKUPS_PER_RUN,
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

    **A partial ADV window needs an IPO date (owner decision Q12).** A ticker
    whose first completed bar falls after the window's first session is
    first judged on the kindest reading -- listed at its first bar. Failing
    even that, it is cached with no IPO question asked, since no answer could
    make it pass. Otherwise its date is read from ``ticker_ipo_date``, else
    asked of ``ipo_dates`` -- at most ``max_ipo_lookups`` requests a run, the
    rest deferred -- and the ticker re-judged on it, before any root check. A
    date that comes back is stored for good; ``None`` fails the ticker closed
    (``ipo_date_unavailable``) for this session only, and is asked again next
    session; a raised error caches nothing and is retried next cycle.
    ``ipo_dates=None`` fails every such ticker closed: nothing to ask, and an
    IPO is never assumed.
    """
    if max_tickers < 1:
        raise ValueError(f"max_tickers must be at least 1, not {max_tickers}")
    if max_ipo_lookups < 0:
        raise ValueError(f"max_ipo_lookups must not be negative, not {max_ipo_lookups}")
    if assets is None:
        return _skipped_run(_NO_ASSET_DIRECTORY, assets, tickers, session_date)
    unusable = _directory_unusable(assets)
    if unusable is not None:
        return _skipped_run(unusable, assets, tickers, session_date)
    # A calendar that cannot list the window and its lookback raises here,
    # once, rather than once per ticker after the bars have been paid for.
    adv_request_start(session_date, is_session=is_session)

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

    # Pass 1: every ticker judged on the kindest reading of its history -- a
    # partial window taken as listed at its first bar. A failure here cannot
    # be rescued by an IPO date or a root check, so neither is asked for.
    results: list[TradeabilityResult] = []
    pending: list[tuple[str, bool, Sequence[Bar], date | None]] = []
    for ticker in batch:
        if ticker in unavailable:
            continue
        asset = assets.get(ticker)
        has_options = asset is not None and asset.has_options
        daily = bars.get(ticker, ())
        try:
            first = partial_window_first_session(
                ticker, daily, session_date, is_session=is_session
            )
            preliminary = assess_tradeability(
                ticker,
                has_options=has_options,
                standard_root=True,
                daily_bars=daily,
                session_date=session_date,
                is_session=is_session,
                ipo_date=first,
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
        pending.append((ticker, has_options, daily, first))

    # The IPO dates already on file, read only when some ticker needs one.
    needing = [ticker for ticker, _, _, first in pending if first is not None]
    known: dict[str, date] = (
        await asyncio.to_thread(_load_ipo_dates, session_factory, needing) if needing else {}
    )

    # Pass 2: settle each partial window on its IPO date (Q12), then the root.
    fetched: dict[str, date] = {}
    ipo_deferred: list[str] = []
    ipo_requests = 0
    root_requests = 0
    for ticker, has_options, daily, first in pending:
        ipo: date | None = None
        if first is not None:
            cause = ""
            if ticker in known:
                ipo = known[ticker]
            elif ipo_dates is None:
                cause = "no IPO-date source was given to this refresh"
            elif ipo_requests >= max_ipo_lookups:
                ipo_deferred.append(ticker)
                continue
            else:
                ipo_requests += 1
                try:
                    ipo = await ipo_dates.ipo_date(ticker)
                except (FundamentalsError, ProviderError) as exc:
                    error = _describe_error(exc)
                    errors.append(TickerCheckError(ticker, CheckStage.IPO_DATE, error))
                    _log_unavailable([ticker], CheckStage.IPO_DATE, error, session_date)
                    continue
                if ipo is None:
                    cause = "the vendor answered with no usable ipo date"
                elif ipo <= first:
                    # Stored for good only when the tape agrees with it: a date
                    # after the first completed bar is a bad vendor answer, and
                    # caching it would refuse the ticker until the window moves
                    # past it instead of re-asking next session.
                    fetched[ticker] = ipo
            if ipo is not None and ipo > first:
                cause = (
                    f"the IPO date {ipo.isoformat()} is later than the ticker's first "
                    f"completed session {first.isoformat()}, which the tape contradicts"
                )
            settled = assess_tradeability(
                ticker,
                has_options=has_options,
                standard_root=True,
                daily_bars=daily,
                session_date=session_date,
                is_session=is_session,
                ipo_date=ipo,
            )
            if TradeabilityFailure.IPO_DATE_UNAVAILABLE in settled.failures:
                _log_ipo_date_unavailable(ticker, cause, session_date, first, ipo)
            if not settled.passes:
                results.append(_verdict_without_root_check(settled))
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
                ipo_date=ipo,
            )
        )

    # Verdicts in the order the tickers were given, whichever pass reached them.
    position = {ticker: index for index, ticker in enumerate(batch)}
    results.sort(key=lambda result: position[result.ticker])
    deferred = ipo_deferred + deferred

    checked_at = now()
    require_aware(checked_at, "now")
    if results or fetched:
        await asyncio.to_thread(_store, session_factory, results, fetched, checked_at)
    _log_verdicts(results, session_date)

    outcome = RefreshResult(
        session_date=session_date,
        skipped=None,
        results=tuple(results),
        errors=tuple(errors),
        deferred=tuple(deferred),
        bar_requests=bar_requests,
        root_requests=root_requests,
        ipo_requests=ipo_requests,
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
            "ipo_requests": ipo_requests,
            "ipo_deferred": len(ipo_deferred),
            "asset_directory_size": len(assets),
        },
    )
    return outcome
