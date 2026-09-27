"""The news poll drivers: the async functions the scheduler's news jobs call.

Phase 3 decision 21 in two tiers, plus the jobs that keep the tiers honest:

* **Watch tier** -- :class:`WatchTierPoller`. Finnhub ``/company-news``, one
  symbol per call, round-robin over the watch universe. The cadence (one
  symbol every 900/W s in the window, 3600/W s otherwise) is the scheduler's;
  :func:`watch_interval` is the pure arithmetic it schedules on.
* **Discovery tier** -- :class:`AlpacaNewsPoller`, :class:`FinnhubMarketNewsPoller`,
  :class:`MassiveNewsPoller`. One call each, from an in-memory cursor that is
  recovered at the first run.
* **Housekeeping** -- :func:`refresh_assets` (the day's asset directory),
  :func:`refresh_tradeability_cache` (off-watch tickers seen today), and
  :func:`prune_news` (decision 21's retention).

**One store, one lock.** :func:`corollary.data.news.ingest.store_articles`
requires its callers to be serialised: two unserialised calls can link a
duplicate to the *wrong* canonical row, or collide on the
``(vendor, vendor_id)`` unique constraint. Every write here -- every poller's
store and the prune -- goes through one :class:`NewsStore`, which holds the
``asyncio.Lock`` around the write. The write itself runs in a worker thread
(``asyncio.to_thread``, a fresh session, committed there), and a
``threading.Lock`` is held inside that thread as well, because cancelling an
``await`` cannot stop the thread it is waiting on: without it, a cancelled
store would release the asyncio lock while its thread was still writing, and
the next store would run beside it. Build **one** ``NewsStore`` per process
and hand it to every poller; two stores are two locks, which is no lock.

**Outcomes.** Every job returns a small result for work done, or
:class:`PollSkipped` with a reason when it completed without fault and
fetched nothing -- an unconfigured vendor, no asset directory yet, nothing to
check. The scheduler maps ``PollSkipped(reason)`` to its ``JobSkipped(reason)``
(this module must not import the scheduler: ``data/`` never imports
``engine/``). A vendor failure is logged here and **re-raised**, so the
scheduler records it as a failure; the scheduler already isolates a failing
job from every other job and from the engine.

**Rule 9 / decision 1.** No context job is a rule-9 producer. Nothing here
imports ``corollary.engine`` or ``corollary.api``, calls
``EngineRuntime.record_message``, touches the watchdog, or can halt anything;
a test imports this module in a fresh interpreter and asserts neither package
was loaded. Positions reach the watch universe through an injected callable,
so this module never sees the broker either.

**Rule 3.** Vendor HTTP happens only inside the provider objects passed in.
No MCP.

**Cursors never move backwards.** Each discovery cursor becomes
``max(current, returned)``, where ``current`` is the poller's cursor read at
commit time -- not the value the call started from -- so the invariant holds
even if the scheduler ever let one poller's job overlap itself (the call that
finishes second cannot set back what the first committed). That is
load-bearing for Massive: the poller asks from ``cursor - 2h`` (late
arrivals), and a call that reads nothing hands back the value it was given, so
without the ``max`` every quiet poll would slide the cursor two more hours into
the past. A cursor advances only after its articles were committed; a fetch or
store failure leaves it where it was, so the next run re-reads rather than
skipping what was never stored.

**Cursors never pass now.** A cursor later than ``now`` skips rather than
re-reads: every article between ``now`` and the stamp is never requested. The
Alpaca provider clamps its own cursor; Massive's -- returned and recovered --
is clamped here and logged as ``massive_news_cursor_clamped`` with the raw
value. The recovered stored max is clamped the same way for both feeds.

**Restart backfill, capped.** A first run (Alpaca, Massive) reads from
``min(newest stored published_at of that feed, now) - 2h``, floored at
``now - MASSIVE_BACKFILL_CAP`` (3 days) or ``now - ALPACA_BACKFILL_CAP``
(1 day); with nothing stored, ``now - 2h``. The effective start is logged as
``news_first_run_start`` with its reason (``stored`` / ``default`` / ``cap``).
An outage longer than the cap leaves the older part of the gap unread by the
discovery tier; the watch tier's previous-day window covers the watch
universe. Finnhub's market feed recovers its ``minId`` from the stored ids and
has only one page to give.

**For step 5 (sentiment audit).** Backfilled and restart-late articles are
ingested *after* publication -- up to the cap, hours or days late. Step 5's
audit must grade from first-seen (``ingested_at``), or exclude articles whose
first-seen minus ``published_at`` gap is large; grading them from
``published_at`` would score news the engine could not have acted on when it
was published (Q7's no-backfill rule).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Final, Protocol, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corollary.calendars import NYSE_TZ, nyse_session_close
from corollary.data.news.article import NewsArticle, NewsFeed
from corollary.data.news.assets import AssetDirectoryHolder, AssetSource
from corollary.data.news.ingest import IngestResult, store_articles
from corollary.data.news.retention import PruneResult, no_labels, prune
from corollary.data.news.tradeability import (
    RefreshResult,
    TradeabilityInputs,
    recent_article_tickers,
    refresh_tradeability,
    tickers_needing_check,
)
from corollary.data.news.watchlist import WatchUniverse, leaders_from_seed, watch_universe
from corollary.data.providers.finnhub import (
    MARKET_NEWS_ID_PREFIX,
    CompanyNews,
    MarketNews,
)
from corollary.data.providers.massive import MassiveNews
from corollary.data.seeds import SeedError, SpdrSeed, load_spdr_seed
from corollary.db.models import NewsArticle as ArticleRow
from corollary.db.models import WatchSymbol
from corollary.wire import require_aware

if TYPE_CHECKING:
    # Type-only: the vendor file is not loaded at runtime by this module.
    from corollary.data.providers.alpaca import AlpacaNews

__all__ = [
    "ALPACA_BACKFILL_CAP",
    "FIRST_RUN_LOOKBACK",
    "MASSIVE_BACKFILL_CAP",
    "MASSIVE_OVERLAP",
    "WATCH_CYCLE_IN_WINDOW",
    "WATCH_CYCLE_OTHERWISE",
    "WATCH_WINDOW_AFTER_CLOSE",
    "WATCH_WINDOW_START",
    "AlpacaNewsPoller",
    "AssetsRefreshed",
    "BuiltUniverse",
    "DiscoveryPollResult",
    "FinnhubMarketNewsPoller",
    "MassiveNewsPoller",
    "NewsStore",
    "PollSkipped",
    "WatchPollResult",
    "WatchTierPoller",
    "build_watch_universe",
    "in_watch_window",
    "prune_news",
    "refresh_assets",
    "refresh_tradeability_cache",
    "watch_interval",
]

logger = logging.getLogger(__name__)

#: How far back a discovery feed reads on its first run. The store upserts on
#: ``(vendor, vendor_id)``, so re-reading what an earlier process stored is
#: harmless.
FIRST_RUN_LOOKBACK: Final = timedelta(hours=2)

#: The furthest back a first run backfills, however old the newest stored row
#: of its feed (the owner's decision after the step-4 audit). An outage longer
#: than the cap leaves the older part of the gap unread by the discovery tier;
#: the watch tier's previous-day window still covers the watch universe.
MASSIVE_BACKFILL_CAP: Final = timedelta(days=3)
ALPACA_BACKFILL_CAP: Final = timedelta(days=1)

#: Massive is asked from ``cursor - MASSIVE_OVERLAP``: articles arrive late
#: (step 4P's finding), and ``published_utc.gt`` would never return one stamped
#: before the cursor.
MASSIVE_OVERLAP: Final = timedelta(hours=2)

#: Decision 21's watch window opens at 06:00 ET on trading days, to catch
#: before-open earnings releases. A poll-window start the spec states as a
#: wall-clock time -- *not* a session boundary; the session's close comes from
#: the calendar, so half-days are right.
WATCH_WINDOW_START: Final = time(6, 0)

#: ... and closes this long after the calendar's close.
WATCH_WINDOW_AFTER_CLOSE: Final = timedelta(hours=1)

#: Every watch symbol once per this, inside the window ...
WATCH_CYCLE_IN_WINDOW: Final = timedelta(minutes=15)

#: ... and once per this outside it (nights, weekends, holidays).
WATCH_CYCLE_OTHERWISE: Final = timedelta(hours=1)

_ERROR_TEXT_WIDTH: Final = 300

_MASSIVE_CLAMP_EVENT: Final = "massive_news_cursor_clamped"

_T = TypeVar("_T")
SessionFactory = Callable[[], Session]


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PollSkipped:
    """A job that completed without fault and fetched nothing, and why.

    The scheduler records it as ``JobSkipped(reason)`` -- never as a success.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class WatchPollResult:
    """One watch-tier request: which symbol, which UTC days, what was stored."""

    symbol: str
    from_date: date
    to_date: date
    #: ``len(polled_symbols)`` this call -- the W in :func:`watch_interval`.
    universe_size: int
    fetched: int
    ingest: IngestResult
    retried_today: bool
    overflow_date: date | None
    seed_missing: bool


@dataclass(frozen=True, slots=True)
class DiscoveryPollResult:
    """One discovery-feed call. ``cursor_*`` is a datetime (Alpaca, Massive) or an id (Finnhub)."""

    feed: NewsFeed
    fetched: int
    ingest: IngestResult
    cursor_before: datetime | int | None
    cursor_after: datetime | int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class AssetsRefreshed:
    """The asset directory now held."""

    assets: int
    optionable: int
    fetched_at: datetime | None


@dataclass(frozen=True, slots=True)
class BuiltUniverse:
    """The watch universe for one cycle, and what was missing from it.

    ``seed_missing``: the SPDR seed has not been built, so there are no sector
    leaders. ``seed_error``: the seed exists but did not parse (also no
    leaders). ``positions_error``: the position source failed this cycle, so
    no position underlyings are in it. None of these fails the build.
    """

    universe: WatchUniverse
    seed_missing: bool
    seed_error: str | None
    positions_error: str | None


UniverseSource = Callable[[], Awaitable[BuiltUniverse]]


# --------------------------------------------------------------------------
# Provider shapes (structural, so tests pass fakes)
# --------------------------------------------------------------------------


class CompanyNewsSource(Protocol):
    async def company_news(self, symbol: str, from_date: date, to_date: date) -> CompanyNews: ...


class MarketNewsSource(Protocol):
    async def market_news(self, min_id: int | None) -> MarketNews: ...


class MassiveNewsSource(Protocol):
    async def news_since(self, published_after: datetime) -> MassiveNews: ...


class AlpacaNewsSource(Protocol):
    async def news(self, *, start: datetime) -> AlpacaNews: ...


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _describe(exc: BaseException) -> str:
    """An exception as bounded text. Provider errors arrive already scrubbed."""
    return f"{type(exc).__name__}: {exc}"[:_ERROR_TEXT_WIDTH]


def _log_failure(event: str, feed: str, exc: Exception, **fields: object) -> None:
    logger.warning(
        "news poll failed (%s): %s",
        feed,
        _describe(exc),
        extra={
            "event": event,
            "feed": feed,
            "error": _describe(exc),
            "rule": "decision 1: a context failure is recorded by the scheduler and never halts the engine",
            **fields,
        },
    )


def _later(previous: _T | None, returned: _T | None) -> _T | None:
    """``max(previous, returned)``, either possibly ``None``: a cursor never moves back."""
    if previous is None:
        return returned
    if returned is None:
        return previous
    return returned if returned > previous else previous  # type: ignore[operator]


# --------------------------------------------------------------------------
# The one store
# --------------------------------------------------------------------------


class NewsStore:
    """The single write path into the news tables. Build one per process.

    ``store`` and ``prune`` both run under :attr:`lock` (and a thread lock
    inside the worker; see the module docstring), so no two writes overlap.
    """

    def __init__(self, *, session_factory: SessionFactory, assets: AssetDirectoryHolder) -> None:
        self._session_factory = session_factory
        self._assets = assets
        self._lock = asyncio.Lock()
        self._thread_lock = threading.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def session_factory(self) -> SessionFactory:
        return self._session_factory

    @property
    def assets(self) -> AssetDirectoryHolder:
        return self._assets

    async def store(self, articles: Sequence[NewsArticle], *, now: datetime) -> IngestResult:
        """Upsert ``articles`` and commit, serialised with every other write.

        Uses whatever asset directory is held -- a stale one filters tags a
        day late, which beats keeping every tag by shape; ``None`` degrades
        (ingest logs it). A failure rolls the whole batch back and raises.
        """
        require_aware(now, "now")
        directory = self._assets.current()
        batch = list(articles)

        def write(session: Session) -> IngestResult:
            return store_articles(session, batch, assets=directory, now=now)

        async with self._lock:
            return await asyncio.to_thread(self._in_session, write)

    async def prune(self, *, now: datetime) -> PruneResult:
        """Decision 21's retention as of ``now``, committed, serialised with every ingest."""
        require_aware(now, "now")

        def write(session: Session) -> PruneResult:
            # Step 5 replaces ``no_labels`` with the ``sentiment_label``
            # predicate; until then no article carries a label.
            return prune(session, now=now, labelled_article_ids=no_labels)

        async with self._lock:
            return await asyncio.to_thread(self._in_session, write)

    def _in_session(self, work: Callable[[Session], _T]) -> _T:
        """Run ``work`` in a fresh session and commit; roll back on any failure."""
        with self._thread_lock:
            with self._session_factory() as session:
                result = work(session)
                session.commit()
                return result


# --------------------------------------------------------------------------
# Watch tier: cadence
# --------------------------------------------------------------------------


def in_watch_window(now: datetime) -> bool:
    """True from 06:00 ET until one hour after the calendar's close, on a trading day.

    Judged on the ET date of ``now``. A day the calendar has no session for
    (weekend, holiday, outside the published schedule) is never in the window.
    Inclusive at 06:00, exclusive at close + 1h.
    """
    require_aware(now, "now")
    local = now.astimezone(NYSE_TZ)
    close = nyse_session_close(local.date())
    if close is None:
        return False
    opens = datetime.combine(local.date(), WATCH_WINDOW_START, tzinfo=NYSE_TZ)
    return opens <= now < close + WATCH_WINDOW_AFTER_CLOSE


def watch_interval(symbols: int, now: datetime) -> timedelta:
    """The gap between two watch-tier requests, for a universe of ``symbols``.

    ``WATCH_CYCLE_IN_WINDOW / W`` inside :func:`in_watch_window`, else
    ``WATCH_CYCLE_OTHERWISE / W``: 13.6 s at W = 66 in the window. Integer
    division of a ``timedelta``, so no float is involved. Raises
    ``ValueError`` for ``symbols < 1`` -- an empty universe has no interval;
    the poller skips instead.
    """
    if symbols < 1:
        raise ValueError(f"watch_interval needs at least one symbol, got {symbols}")
    cycle = WATCH_CYCLE_IN_WINDOW if in_watch_window(now) else WATCH_CYCLE_OTHERWISE
    return cycle / symbols


# --------------------------------------------------------------------------
# Watch tier: the universe
# --------------------------------------------------------------------------


def _active_manual_watches(session_factory: SessionFactory) -> list[str]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(WatchSymbol.ticker)
                .where(WatchSymbol.removed_at.is_(None))
                .order_by(WatchSymbol.ticker)
            )
        )


async def build_watch_universe(
    *,
    markets: Iterable[str],
    position_underlyings: Callable[[], Awaitable[Iterable[str]]],
    session_factory: SessionFactory,
    seed_loader: Callable[[], SpdrSeed | None] = load_spdr_seed,
) -> BuiltUniverse:
    """Decision 12's watch universe for this cycle.

    Leaders from the SPDR seed (absent: none, ``seed_missing``; malformed:
    none, ``seed_error``, logged), manual watches from the active
    ``watch_symbol`` rows, positions from ``position_underlyings``. A failure
    of that callable is logged and this cycle has no position underlyings --
    the build never fails for it. The caller supplies it (positions come from
    the broker via the app; this module does not import the broker).
    """
    markets = list(markets)
    positions: list[str] = []
    positions_error: str | None = None
    try:
        positions = list(await position_underlyings())
    except Exception as exc:
        positions_error = _describe(exc)
        logger.warning(
            "watch universe built without position underlyings this cycle: %s",
            positions_error,
            extra={
                "event": "watch_universe_positions_unavailable",
                "error": positions_error,
                "rule": "a failed position source leaves positions out of one cycle, never the universe",
            },
        )

    seed_error: str | None = None
    seed: SpdrSeed | None = None
    try:
        seed = await asyncio.to_thread(seed_loader)
    except SeedError as exc:
        seed_error = _describe(exc)
        logger.error(
            "SPDR seed is malformed; the watch universe has no sector leaders: %s",
            seed_error,
            extra={"event": "watch_universe_seed_error", "error": seed_error},
        )
    leaders = leaders_from_seed(seed)
    manual = await asyncio.to_thread(_active_manual_watches, session_factory)

    universe = watch_universe(markets, leaders.symbols, positions, manual)
    for skipped in universe.skipped_positions:
        logger.warning(
            "position underlying %r left out of the watch universe: %s",
            skipped.raw,
            skipped.reason,
            extra={
                "event": "watch_universe_position_skipped",
                "symbol": skipped.raw,
                "reason": skipped.reason,
            },
        )
    return BuiltUniverse(
        universe=universe,
        seed_missing=seed is None and seed_error is None,
        seed_error=seed_error,
        positions_error=positions_error,
    )


# --------------------------------------------------------------------------
# Watch tier: the poller
# --------------------------------------------------------------------------


class WatchTierPoller:
    """Round-robin over the watch universe, one symbol per :meth:`poll_next`.

    The universe is rebuilt on every call, so a manual add or remove takes
    effect on the next request. The next symbol is the first in
    ``polled_symbols`` (sorted) strictly after the last one polled, wrapping
    to the start -- so when the universe changes the rotation continues from
    where it was, and a symbol present for a whole cycle is polled within it.
    The position advances *before* the request: a symbol whose requests keep
    failing is retried next cycle, not on every call.
    """

    def __init__(
        self,
        *,
        provider: CompanyNewsSource | None,
        universe: UniverseSource,
        store: NewsStore,
    ) -> None:
        self._provider = provider
        self._universe = universe
        self._store = store
        self._last: str | None = None
        self._last_size = 0

    @property
    def last_polled(self) -> str | None:
        return self._last

    @property
    def last_universe_size(self) -> int:
        """W as of the latest call: what the scheduler passes to :func:`watch_interval`."""
        return self._last_size

    def _next_symbol(self, symbols: Sequence[str]) -> str:
        if self._last is not None:
            for symbol in symbols:
                if symbol > self._last:
                    return symbol
        return symbols[0]

    async def poll_next(self, now: datetime) -> WatchPollResult | PollSkipped:
        """Request one symbol's company news over the previous and current **UTC** day."""
        require_aware(now, "now")
        if self._provider is None:
            return PollSkipped(
                "Finnhub is unavailable (FINNHUB_API_KEY is unset); the watch tier fetched nothing"
            )
        built = await self._universe()
        symbols = built.universe.polled_symbols
        self._last_size = len(symbols)
        if not symbols:
            return PollSkipped("the watch universe has no symbol to poll")
        symbol = self._next_symbol(symbols)
        if symbol == symbols[0] and built.seed_missing:
            # Once per rotation, not once per request.
            logger.warning(
                "watch universe has no sector leaders: the SPDR seed has not been built",
                extra={
                    "event": "watch_universe_seed_missing",
                    "universe_size": len(symbols),
                    "remedy": "run scripts/build_spdr_seed.py",
                },
            )
        self._last = symbol

        # Finnhub's from/to are UTC days. An ET "today" would miss the UTC day
        # that has already begun after 20:00 EDT / 19:00 EST.
        to_date = now.astimezone(timezone.utc).date()
        from_date = to_date - timedelta(days=1)
        try:
            news = await self._provider.company_news(symbol, from_date, to_date)
        except Exception as exc:
            _log_failure(
                "watch_news_poll_failed",
                NewsFeed.FINNHUB_COMPANY.value,
                exc,
                symbol=symbol,
                from_date=from_date.isoformat(),
                to_date=to_date.isoformat(),
            )
            raise
        ingest = await self._store.store(news.articles, now=now)
        return WatchPollResult(
            symbol=symbol,
            from_date=from_date,
            to_date=to_date,
            universe_size=len(symbols),
            fetched=len(news.articles),
            ingest=ingest,
            retried_today=news.retried_today,
            overflow_date=news.overflow_date,
            seed_missing=built.seed_missing,
        )


# --------------------------------------------------------------------------
# Discovery tier
# --------------------------------------------------------------------------


def _max_finnhub_market_id(session_factory: SessionFactory) -> int | None:
    """The largest numeric id among stored ``finnhub_market`` rows (``general:<id>``).

    Parsed in Python: ``vendor_id`` is text, and as text ``general:99`` sorts
    after ``general:1000``.
    """
    with session_factory() as session:
        ids = session.scalars(
            select(ArticleRow.vendor_id).where(ArticleRow.feed == NewsFeed.FINNHUB_MARKET.value)
        )
        best: int | None = None
        for vendor_id in ids:
            if not vendor_id.startswith(MARKET_NEWS_ID_PREFIX):
                continue
            digits = vendor_id[len(MARKET_NEWS_ID_PREFIX) :]
            if not digits.isdigit():
                continue
            number = int(digits)
            best = number if best is None else max(best, number)
        return best


def _max_published(session_factory: SessionFactory, feed: NewsFeed) -> datetime | None:
    """The newest stored ``published_at`` among ``feed``'s rows.

    ``UtcDateTime`` stores a fixed-width UTC ISO string, so SQL ``MAX`` orders
    it correctly.
    """
    with session_factory() as session:
        return session.scalar(
            select(func.max(ArticleRow.published_at)).where(ArticleRow.feed == feed.value)
        )


def _clamp_to_now(
    value: datetime, now: datetime, *, feed: NewsFeed, source: str, event: str
) -> datetime:
    """``min(value, now)``, logged at WARNING with the raw value when it binds.

    A cursor stamped after ``now`` (a vendor's future ``published_utc``, a
    skewed clock) would make the next request start past every article
    between ``now`` and that stamp -- a skip, not a re-read. ``source`` says
    where the value came from: ``returned`` (the provider's cursor) or
    ``stored`` (the newest stored row, at a first run).
    """
    if value <= now:
        return value
    logger.warning(
        "%s cursor clamped from %s to %s: a %s value later than now",
        feed.value,
        value.isoformat(),
        now.isoformat(),
        source,
        extra={
            "event": event,
            "feed": feed.value,
            "source": source,
            "raw": value.isoformat(),
            "clamped_to": now.isoformat(),
            "rule": (
                "a discovery cursor never passes now -- a future-stamped article "
                "would otherwise skip every article published before its stamp"
            ),
        },
    )
    return now


def _backfill_start(
    stored: datetime | None,
    now: datetime,
    *,
    feed: NewsFeed,
    cap: timedelta,
    clamp_event: str,
) -> datetime:
    """A first run's start: ``min(stored, now) - FIRST_RUN_LOOKBACK``, floored at ``now - cap``.

    ``stored`` is the newest stored ``published_at`` of ``feed``; ``None``
    (nothing stored) gives ``now - FIRST_RUN_LOOKBACK``. Logged once with the
    effective start and why: ``default`` (nothing stored), ``stored`` (from
    the stored row) or ``cap`` (the stored row was older than the cap allows).
    The floor is inclusive: a start exactly at ``now - cap`` is ``stored``.
    """
    if stored is None:
        start = now - FIRST_RUN_LOOKBACK
        reason = "default"
    else:
        start = (
            _clamp_to_now(stored, now, feed=feed, source="stored", event=clamp_event)
            - FIRST_RUN_LOOKBACK
        )
        reason = "stored"
        floor = now - cap
        if start < floor:
            start = floor
            reason = "cap"
    logger.info(
        "%s first run reads from %s (%s)",
        feed.value,
        start.isoformat(),
        reason,
        extra={
            "event": "news_first_run_start",
            "feed": feed.value,
            "start": start.isoformat(),
            "reason": reason,
            "stored": None if stored is None else stored.isoformat(),
            "cap_seconds": int(cap.total_seconds()),
        },
    )
    return start


class AlpacaNewsPoller:
    """Alpaca ``/v1beta1/news``, untickered. First start per :func:`_backfill_start`, then the returned cursor.

    First run: ``min(newest stored alpaca_news published_at, now) - 2h``,
    floored at ``now - ALPACA_BACKFILL_CAP``; nothing stored, ``now - 2h``.
    ``start`` is inclusive and the provider's cursor re-reads its own second,
    so there is no overlap to add here. The provider already clamps its
    returned cursor to its clock (``alpaca_news_cursor_clamped``).
    """

    def __init__(self, *, provider: AlpacaNewsSource | None, store: NewsStore) -> None:
        self._provider = provider
        self._store = store
        self._cursor: datetime | None = None
        #: The first run's start, fixed at the first attempt: a failed first
        #: run is retried from the same instant rather than sliding forward.
        self._first_start: datetime | None = None

    @property
    def cursor(self) -> datetime | None:
        return self._cursor

    async def poll(self, now: datetime) -> DiscoveryPollResult | PollSkipped:
        require_aware(now, "now")
        if self._provider is None:
            return PollSkipped("Alpaca news is unavailable; the discovery poll fetched nothing")
        before = self._cursor
        if before is not None:
            start = before
        else:
            if self._first_start is None:
                stored = await asyncio.to_thread(
                    _max_published, self._store.session_factory, NewsFeed.ALPACA_NEWS
                )
                first = _backfill_start(
                    stored,
                    now,
                    feed=NewsFeed.ALPACA_NEWS,
                    cap=ALPACA_BACKFILL_CAP,
                    clamp_event="alpaca_news_cursor_clamped",
                )
                # An overlapping call may have fixed it while this one read.
                if self._first_start is None:
                    self._first_start = first
            start = self._first_start
        try:
            news = await self._provider.news(start=start)
        except Exception as exc:
            _log_failure(
                "discovery_news_poll_failed",
                NewsFeed.ALPACA_NEWS.value,
                exc,
                start=start.isoformat(),
            )
            raise
        ingest = await self._store.store(news.articles, now=now)
        # Against the cursor as it is *now*, not ``before``: an overlapping
        # call that committed first must not be set back by this one.
        baseline = self._cursor if self._cursor is not None else start
        self._cursor = _later(baseline, news.cursor)
        return DiscoveryPollResult(
            feed=NewsFeed.ALPACA_NEWS,
            fetched=len(news.articles),
            ingest=ingest,
            cursor_before=before,
            cursor_after=self._cursor,
            complete=news.complete,
        )


class FinnhubMarketNewsPoller:
    """Finnhub ``/news?category=general`` with the ``minId`` cursor.

    First run: the largest stored ``general:<id>``, else ``None`` (a cold
    start page). Then the returned ``min_id``.
    """

    def __init__(self, *, provider: MarketNewsSource | None, store: NewsStore) -> None:
        self._provider = provider
        self._store = store
        self._min_id: int | None = None
        self._recovered = False

    @property
    def min_id(self) -> int | None:
        return self._min_id

    async def poll(self, now: datetime) -> DiscoveryPollResult | PollSkipped:
        require_aware(now, "now")
        if self._provider is None:
            return PollSkipped(
                "Finnhub is unavailable (FINNHUB_API_KEY is unset); the market-news poll fetched nothing"
            )
        if not self._recovered:
            recovered = await asyncio.to_thread(
                _max_finnhub_market_id, self._store.session_factory
            )
            # ``_later``: an overlapping call may have advanced it meanwhile.
            self._min_id = _later(self._min_id, recovered)
            self._recovered = True
        before = self._min_id
        try:
            news = await self._provider.market_news(before)
        except Exception as exc:
            _log_failure(
                "discovery_news_poll_failed",
                NewsFeed.FINNHUB_MARKET.value,
                exc,
                min_id=before,
            )
            raise
        ingest = await self._store.store(news.articles, now=now)
        # Against the id as it is now, not ``before`` (see AlpacaNewsPoller).
        self._min_id = _later(self._min_id, news.min_id)
        return DiscoveryPollResult(
            feed=NewsFeed.FINNHUB_MARKET,
            fetched=len(news.articles),
            ingest=ingest,
            cursor_before=before,
            cursor_after=self._min_id,
            complete=True,
        )


class MassiveNewsPoller:
    """Massive ``/v2/reference/news``, untickered, from ``cursor - MASSIVE_OVERLAP``.

    First run: ``min(newest stored massive_news published_at, now) - 2h``,
    floored at ``now - MASSIVE_BACKFILL_CAP``; nothing stored, ``now - 2h``.
    Every cursor -- the recovered stored max and every returned one -- is
    clamped to ``now`` first and logged as ``massive_news_cursor_clamped``
    with the raw value: one article stamped at T+6h would otherwise make the
    next call ask ``published_utc.gt = T+4h`` and never request (T, T+4h].
    """

    def __init__(self, *, provider: MassiveNewsSource | None, store: NewsStore) -> None:
        self._provider = provider
        self._store = store
        self._cursor: datetime | None = None
        #: The first run's ``published_after``, fixed at the first attempt
        #: (see :class:`AlpacaNewsPoller`).
        self._first_after_at: datetime | None = None

    @property
    def cursor(self) -> datetime | None:
        return self._cursor

    async def _first_after(self, now: datetime) -> datetime:
        stored = await asyncio.to_thread(
            _max_published, self._store.session_factory, NewsFeed.MASSIVE_NEWS
        )
        return _backfill_start(
            stored,
            now,
            feed=NewsFeed.MASSIVE_NEWS,
            cap=MASSIVE_BACKFILL_CAP,
            clamp_event=_MASSIVE_CLAMP_EVENT,
        )

    async def poll(self, now: datetime) -> DiscoveryPollResult | PollSkipped:
        require_aware(now, "now")
        if self._provider is None:
            return PollSkipped(
                "Massive is unavailable (MASSIVE_API_KEY is unset); the discovery poll fetched nothing"
            )
        before = self._cursor
        if before is not None:
            after = before - MASSIVE_OVERLAP
        else:
            if self._first_after_at is None:
                first = await self._first_after(now)
                # An overlapping call may have fixed it while this one read.
                if self._first_after_at is None:
                    self._first_after_at = first
            after = self._first_after_at
        try:
            news = await self._provider.news_since(after)
        except Exception as exc:
            _log_failure(
                "discovery_news_poll_failed",
                NewsFeed.MASSIVE_NEWS.value,
                exc,
                published_after=after.isoformat(),
            )
            raise
        ingest = await self._store.store(news.articles, now=now)
        returned = _clamp_to_now(
            news.cursor,
            now,
            feed=NewsFeed.MASSIVE_NEWS,
            source="returned",
            event=_MASSIVE_CLAMP_EVENT,
        )
        # The first run's baseline is the instant asked from; later runs keep
        # the cursor, so an empty answer (which returns ``after``, two hours
        # behind it) cannot move it back. The cursor is read *now*, not from
        # ``before``: an overlapping call that committed first is not undone.
        baseline = self._cursor if self._cursor is not None else after
        self._cursor = _later(baseline, returned)
        return DiscoveryPollResult(
            feed=NewsFeed.MASSIVE_NEWS,
            fetched=len(news.articles),
            ingest=ingest,
            cursor_before=before,
            cursor_after=self._cursor,
            complete=news.complete,
        )


# --------------------------------------------------------------------------
# Housekeeping jobs
# --------------------------------------------------------------------------


async def refresh_assets(
    holder: AssetDirectoryHolder, source: AssetSource | None
) -> AssetsRefreshed | PollSkipped:
    """Fetch and hold the day's asset directory. A failure raises (the holder logged it)."""
    if source is None:
        return PollSkipped("the asset source is unavailable; the directory was not refreshed")
    directory = await holder.refresh(source)
    return AssetsRefreshed(
        assets=len(directory),
        optionable=len(directory.optionable()),
        fetched_at=holder.fetched_at,
    )


def _session_start_utc(now: datetime) -> tuple[date, datetime]:
    """Today's ET date and its midnight ET, as a UTC instant."""
    session_date = now.astimezone(NYSE_TZ).date()
    start = datetime.combine(session_date, time(0), tzinfo=NYSE_TZ).astimezone(timezone.utc)
    return session_date, start


def _candidates(
    session_factory: SessionFactory, since: datetime, watch: WatchUniverse, session_date: date
) -> list[str]:
    with session_factory() as session:
        recent = recent_article_tickers(session, since)
        return tickers_needing_check(
            session, candidates=recent, watch=watch, session_date=session_date
        )


async def refresh_tradeability_cache(
    *,
    holder: AssetDirectoryHolder,
    provider: TradeabilityInputs | None,
    universe: UniverseSource,
    session_factory: SessionFactory,
    now: datetime,
) -> RefreshResult | PollSkipped:
    """Check the off-watch tickers tagged since midnight ET today, for today's ET date.

    Skips with a reason when there is no provider, no asset directory, or no
    ticker needing a check. A directory older than ``MAX_DIRECTORY_AGE`` is
    still used -- yesterday's optionable list beats none -- and logged at
    WARNING with its age, which is the caller's duty per
    :func:`~corollary.data.news.tradeability.refresh_tradeability`. Not under
    the store lock: it writes only ``ticker_tradeability``, and holding the
    lock across its vendor requests would stall every ingest.
    """
    require_aware(now, "now")
    if provider is None:
        return PollSkipped("the tradeability inputs are unavailable; nothing was checked")
    directory = holder.current()
    if directory is None:
        return PollSkipped(
            "no asset directory has been fetched yet, so has_options is unknown; nothing was checked"
        )
    if holder.is_stale():
        age = holder.age()
        logger.warning(
            "tradeability refresh is using an asset directory %s old",
            age,
            extra={
                "event": "asset_directory_stale_used",
                "age_seconds": None if age is None else int(age.total_seconds()),
                "fetched_at": None if holder.fetched_at is None else holder.fetched_at.isoformat(),
                "rule": "decision 21: a stale directory may be used for tradeability, and says so",
            },
        )
    built = await universe()
    session_date, since = _session_start_utc(now)
    tickers = await asyncio.to_thread(
        _candidates, session_factory, since, built.universe, session_date
    )
    if not tickers:
        return PollSkipped(
            f"no off-watch ticker tagged since {since.isoformat()} needs a check for {session_date}"
        )
    result = await refresh_tradeability(
        provider=provider,
        assets=directory,
        tickers=tickers,
        session_date=session_date,
        session_factory=session_factory,
        now=lambda: now,
    )
    if result.skipped is not None:
        return PollSkipped(result.skipped)
    return result


async def prune_news(store: NewsStore, now: datetime) -> PruneResult:
    """Decision 21's retention, committed, under the store lock so it never races an ingest.

    Passes :func:`~corollary.data.news.retention.no_labels`; step 5 replaces
    it with the ``sentiment_label`` predicate (see :meth:`NewsStore.prune`).
    """
    result = await store.prune(now=now)
    logger.info(
        "news retention ran: %d summaries nulled, %d groups deleted",
        result.summaries_nulled,
        result.groups_deleted,
        extra={
            "event": "news_pruned",
            "cutoff": result.cutoff.isoformat(),
            "summaries_nulled": result.summaries_nulled,
            "groups_deleted": result.groups_deleted,
            "articles_deleted": result.articles_deleted,
            "groups_kept_labelled": result.groups_kept_labelled,
            "groups_skipped": len(result.groups_skipped),
        },
    )
    return result
