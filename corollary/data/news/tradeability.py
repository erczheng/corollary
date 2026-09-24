"""Decision 21's tradeability filter -- pure.

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

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from corollary.calendars import NYSE_TZ, nyse_session_close
from corollary.data.providers.interface import Bar, OptionContract
from corollary.data.seeds import EQUITY_SYMBOL_RE, normalize_symbol
from corollary.instruments import is_adjusted_root

__all__ = [
    "ADV_SESSIONS",
    "MIN_AVG_DAILY_VOLUME",
    "MIN_LAST_CLOSE",
    "MIN_SESSIONS_OF_HISTORY",
    "STANDARD_CONTRACT_SIZE",
    "IsSession",
    "TradeabilityFailure",
    "TradeabilityResult",
    "adv_window",
    "adv_window_start",
    "assess_tradeability",
    "has_standard_contract",
    "is_adjusted_root_ticker",
    "nyse_is_session",
]

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
