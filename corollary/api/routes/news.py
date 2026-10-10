"""News: the canonical-row feed and the manual watch list (Phase 3 step 4).

Four routes, and none of them depends on a broker or a provider. The spec's
*API* section: *"All read routes are plain ``GET``s depending on no broker"*,
and of the watch routes, *"They depend on no broker: the asset list they
validate against is the cached one."* Everything a route here needs from a
vendor is read off ``app.state`` -- the daily asset list
(:class:`~corollary.data.news.assets.AssetDirectoryHolder`) and the last-known
position underlyings (:class:`~corollary.api.deps.PositionUnderlyings`) --
which the scheduler fills. ``tests/api/test_news_routes.py`` walks each
route's dependency tree to keep it so.

``GET /api/news``
-----------------

**Canonical rows only** (decision 3). A cross-vendor duplicate group is shown
once, as its canonical row, naming the canonical row's publisher. The group's
tickers are the **union of every member's tags** -- a duplicate keeps its own
tags at ingest (Massive tags one story to eight symbols on *its* copy), and a
story tagged to TSLA on any copy is a TSLA story. ``MARKET`` means *no tag*,
so a group with any real tag is not also served under ``MARKET``: the same
rule the ingest applies within one row.

**One item per (canonical article, ticker)**, shaped as ``types.ts``'s
``NewsItem`` plus ``url`` and the spec's *"the tier and source that produced
it, and whether that source is demoted"*. The tickers are the group's tags
**union its label tickers**: the rules tier labels any company a headline
names, tagged or not, and such an item carries ``attributedBy: "headline"``
(a tagged one ``"tag"``). The ``ticker``, ``sentiment`` and scope filters and
``total`` all include them. A label ticker is not a tag, so a story tagged
only ``MARKET`` keeps its ``MARKET`` item beside the headline one.

**Labels (step 5, decision 4): rules, then vendor.** A group's labels for a
ticker are read across every member of the group -- the same union the tags
use -- one per source: the canonical row's when it carries one, else the
lowest member id's (:func:`corollary.data.news.signals.group_labels`, the one
selector Movers and the tradeability refresh also read). The displayed label is the rules one if any, else
Massive's, else ``unclassified`` with no tier, no source and no status
(decision 17). The other label, when both sources labelled, is served as
``otherLabel``. The ``sentiment`` filter applies to the *displayed* label, in
SQL, so ``total`` and the pages agree with it. Each source's status is
``unaudited`` (Q22) until step 6's ``sentiment_source_status`` exists.

``GET /api/news/movers``
------------------------

Decision 21's discovery candidates, computed by the pure
:func:`corollary.data.news.discovery.discover` from what is stored: every
picked directional label in the lookback
(:func:`corollary.data.news.signals.stored_signals`), mapped to its group's
canonical article; the watch universe at read time; and, per signalled
ticker, the **latest** ``ticker_tradeability`` row on or before today's ET
date. The cache is filled per session date, by the tradeability refresh,
for exactly the off-watch tickers this route would call signalled -- the
same read and the same ``qualifies`` -- over the longest bounded lookback
(``2w``), tagged or not, newest qualifying label first under the per-run cap;
so yesterday's verdict is the newest answer until today's check runs, and its
session date is served beside it. **No vendor is called**: a ticker the cache
has not judged is counted under ``awaitingCheck``, never fetched here. Under
``all`` a signal older than two weeks that was never judged stays there.

The response separates the spec's two empty states server-side (see
:class:`~corollary.api.schemas.NewsMovers`): the three discovery feeds'
freshness, from the scheduler's job records and the newest stored row per
feed -- stale on a failure or skip, a stopped job, or
:data:`STALE_AFTER_SLOTS` of the job's own slots passing with no delivery --
and the tradeability cache's last evaluation.

**Sector** from the SPDR seed (decision 6); ``Other`` outside it; ``MARKET``
under ``Macro``. Before the owner builds the seed every ticker is ``Other``
and the response says so (``sectorsAvailable: false``, ``seedAsOf: null``),
so the UI can caption it rather than imply the market is all "Other".

**Lookbacks** are ``web/src/lib/news.ts``'s: *today* is since the start of
today's **ET** calendar date, ``3d`` since the start of the ET date two days
before, and so on -- calendar days, not sessions, never a trailing 24 hours,
and never a hardcoded offset (EST and EDT differ by the hour that would
misfile every winter headline). ``all`` is everything retained.

**Scope** ``watch`` (the default, decision 21) is the watch universe plus
``MARKET``; ``all`` is everything.

**Pagination is offset/limit**, ``limit`` at most 200. See
:class:`~corollary.api.schemas.NewsFeed` for why not a cursor.

The watch routes
----------------

Decision 21, verbatim: *"Only manual watches are removable: seed, Markets and
position members are not. The ticker must be an active US equity in the asset
list. ... One past the ceiling is refused with a 409 that names it.
Audit-logged, under a new
``watchlist`` category ... It notifies ``watchlist_changed`` ... Removal keeps
everything already stored."*

* **Rule 4: the ticker is validated here**, never trusted from the client --
  normalised, shape-checked and membership-checked by ``watchlist.py``'s
  :func:`can_add_manual` / :func:`can_remove_manual`, and on add looked up in
  the cached asset list. **No asset list yet is a 503**, not an accepted
  ticker: an unvalidated watch is a symbol the watch tier would poll and the
  self-audit would grade on nobody's say-so.
* **The cap is on manual watches only** (owner decision Q13, which replaced
  decision 21's "at most 100 symbols before position underlyings"): at most
  ``MANUAL_WATCH_CAP`` = 34 active manual watches *(parent-session
  assumption)*, independent of the seed, the Markets list and positions.
  Removing a watch frees a slot; nothing is ever removed automatically.
* **Status codes.** Cap: 409 naming the ticker and the 34. Not an active
  US equity: 422. Malformed symbol: 422. Already watched (manually, or by
  another membership, or ``MARKET``): 409. Removing a non-manual member: 409
  naming its memberships. Removing a non-member: 404.
* **One change, one ``audit_log`` row, one notice.** The row is written in the
  same transaction as the ``watch_symbol`` change, committed, and only then is
  one ``watchlist_changed`` notice scheduled after the response -- built from
  that audit row's values (decision 20), through ``operator.py``'s one path.
  A refused request writes nothing and emits nothing.
* **Removal keeps everything already stored**: the ``watch_symbol`` row gets a
  ``removed_at`` and is never deleted, and no article is touched. A re-add is
  a new row.
* **Check and write under one lock.** The routes are synchronous and run in
  the threadpool, so two adds racing could both pass the cap check; the
  partial unique index stops a duplicate *ticker*, not a 35th watch. One
  process, one writer (the design spec), so an in-process lock is sufficient.

Seed missing: the leaders are absent from the universe, so ``symbols`` is
smaller than it will be once the seed is built, and ``seedMissing`` says so.
The cap does not move with it: it counts manual watches only, so the seed's
arrival can never push the count past the cap or take a slot back.
"""

import logging
import threading
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Any, Final

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, Request
from sqlalchemy import Select, and_, exists, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, aliased

from corollary.api.deps import (
    ApiError,
    AssetDirectoryDep,
    PositionUnderlyings,
    PositionUnderlyingsDep,
    SchedulerStatusDep,
    SessionDep,
    SpdrSeedDep,
)
from corollary.api.operator import (
    AuditChange,
    OperatorEvent,
    notify_after_response,
    settings_notice,
)
from corollary.api.routes.markets import UNIVERSE_SYMBOLS
from corollary.api.schemas import (
    DiscoveryEvaluation,
    DiscoveryFeedFreshness,
    DiscoveryFeedName,
    ManualWatch,
    MoverDirection,
    NewsDirection,
    NewsFeed,
    NewsItem,
    NewsLabel,
    NewsLabelSource,
    NewsLookback,
    NewsMover,
    NewsMoverArticle,
    NewsMoverEvidence,
    NewsMoverReason,
    NewsMovers,
    NewsScope,
    NewsSentiment,
    NewsSort,
    NewsSourceStatus,
    WatchList,
)
from corollary.data.news.assets import AssetDirectoryHolder
from corollary.data.news.discovery import CachedVerdict, Candidate, discover
from corollary.data.news.labels import SOURCE_TIER, Direction, LabelSource, SentimentTier
from corollary.data.news.signals import (
    EASTERN,
    LOOKBACK_DAYS,
    group_labels,
    lookback_start,
    stored_signals,
)
from corollary.data.news.watchlist import (
    MANUAL_WATCH_CAP,
    MARKET_TICKER,
    WatchChangeCheck,
    WatchRefusal,
    WatchUniverse,
    can_add_manual,
    can_remove_manual,
    leaders_from_seed,
    watch_universe,
)
from corollary.data.seeds import SpdrSeed, normalize_symbol
from corollary.db.models import (
    AuditLog,
    NewsArticle,
    NewsArticleTicker,
    TickerTradeability,
    WatchSymbol,
)
from corollary.engine.scheduler import JobStatus

__all__ = [
    "DISCOVERY_FEED_JOBS",
    "LOOKBACK_DAYS",
    "MACRO_SECTOR",
    "NOT_WATCHED",
    "OTHER_SECTOR",
    "SOURCE_STATUS",
    "STALE_AFTER_SLOTS",
    "TRADEABILITY_JOB",
    "WATCHED",
    "lookback_start",
    "request_now",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])

#: ``MACRO_SECTOR`` in ``types.ts``: where ``MARKET`` stories file.
MACRO_SECTOR: Final = "Macro"
#: Decision 3: a ticker outside the seed, or any ticker with no seed.
OTHER_SECTOR: Final = "Other"

#: ``LOOKBACK_DAYS`` and :func:`lookback_start` live in
#: :mod:`corollary.data.news.signals`, which the tradeability refresh reads
#: too; re-exported here for the route's callers.

#: Decision 4: no source labelled the item for that ticker.
_UNLABELLED: Final[NewsSentiment] = "unclassified"

#: Each labelling source's audit status (Q22). **``unaudited`` for both until
#: step 6**, which replaces this table with ``sentiment_source_status``. Never
#: ``active`` before an audit has run: that would claim a pass nobody graded.
SOURCE_STATUS: Final[dict[LabelSource, NewsSourceStatus]] = {
    LabelSource.RULES: "unaudited",
    LabelSource.MASSIVE: "unaudited",
}

#: Decision 21's three discovery firehoses and the scheduler job polling each.
#: ``test_movers_route.py`` pins these names against the shipped job set.
DISCOVERY_FEED_JOBS: Final[dict[DiscoveryFeedName, str]] = {
    "alpaca_news": "news_alpaca",
    "finnhub_market": "news_finnhub_market",
    "massive_news": "news_massive",
}
#: The job that fills ``ticker_tradeability`` -- discovery's last evaluation.
TRADEABILITY_JOB: Final = "tradeability_cache"

#: A discovery feed is stale once this many of its job's **own scheduled
#: slots** have passed since it last delivered -- three times its cadence,
#: measured on the job definition's schedule (``JobStatus.cadence``), so a
#: two-rate job is judged at the rate it was actually meant to run at.
STALE_AFTER_SLOTS: Final = 3

#: Tickers per ``IN (...)`` when reading the tradeability cache.
_VERDICT_CHUNK: Final = 500

#: The audit values for a watch change. ``web/src/lib/settings.ts``'s
#: ``auditFieldLabel`` and ``mockData.ts``'s audit fixture assume exactly
#: these, with the ticker as the field.
WATCHED: Final = "watched"
NOT_WATCHED: Final = "not watched"
_AUDIT_CATEGORY: Final = "watchlist"

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200

#: See the module docstring: the ceiling check and the write are one step.
_WATCH_WRITE_LOCK: Final = threading.Lock()


def request_now() -> datetime:
    """The request's clock, aware UTC. A dependency so a test can fix it."""
    return datetime.now(timezone.utc)


NowDep = Annotated[datetime, Depends(request_now)]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def _sector(ticker: str, seed: SpdrSeed | None) -> str:
    if ticker == MARKET_TICKER:
        return MACRO_SECTOR
    if seed is None:
        return OTHER_SECTOR
    return seed.sector_of(ticker) or OTHER_SECTOR


def _log_skipped_positions(universe: WatchUniverse, correlation_id: str) -> None:
    for skipped in universe.skipped_positions:
        logger.warning(
            "a position underlying cannot be a watch-universe member and was skipped",
            extra={
                "event": "watch_position_skipped",
                "rule": "a bad position symbol is skipped and reported, never raised",
                "symbol": skipped.raw,
                "reason": skipped.reason,
                "correlation_id": correlation_id,
            },
        )


def _active_manual(session: Session) -> list[WatchSymbol]:
    return list(
        session.scalars(
            select(WatchSymbol)
            .where(WatchSymbol.removed_at.is_(None))
            .order_by(WatchSymbol.ticker)
        )
    )


def _universe(
    session: Session,
    seed: SpdrSeed | None,
    positions: PositionUnderlyings,
    correlation_id: str,
) -> tuple[WatchUniverse, bool]:
    """The watch universe from its four sources, and whether the seed is missing."""
    leaders = leaders_from_seed(seed)
    universe = watch_universe(
        markets=UNIVERSE_SYMBOLS,
        leaders=leaders.symbols,
        positions=positions.current(),
        manual=[row.ticker for row in _active_manual(session)],
    )
    _log_skipped_positions(universe, correlation_id)
    return universe, leaders.seed_missing


def _watch_list(
    session: Session,
    seed: SpdrSeed | None,
    positions: PositionUnderlyings,
    directory: AssetDirectoryHolder,
    correlation_id: str,
) -> WatchList:
    universe, seed_missing = _universe(session, seed, positions, correlation_id)
    counted = universe.manual_count
    return WatchList(
        manual=[
            ManualWatch(ticker=row.ticker, added_at=row.added_at)
            for row in _active_manual(session)
        ],
        symbols=len(universe.polled_symbols),
        manual_count=counted,
        cap=MANUAL_WATCH_CAP,
        remaining=max(MANUAL_WATCH_CAP - counted, 0),
        position_underlyings=len(positions.current()),
        positions_as_of=positions.as_of,
        seed_missing=seed_missing,
        asset_list_available=directory.current() is not None,
        asset_list_fetched_at=directory.fetched_at,
    )


# --------------------------------------------------------------------------
# GET /api/news
# --------------------------------------------------------------------------


def _feed_query(
    *,
    since: datetime | None,
    tickers_in: Iterable[str] | None,
    ticker: str | None,
    publisher: str | None,
    sector: str | None,
    sentiment: NewsSentiment | None,
    seed: SpdrSeed | None,
) -> tuple[Select[Any], Any, Any]:
    """The (canonical row, ticker) rows matching every filter, unordered.

    Returns the statement plus the canonical-row alias and the ticker column,
    for the caller's ordering.
    """
    member = aliased(NewsArticle, name="member")
    group_id = func.coalesce(member.canonical_id, member.id)
    # Every (group, ticker) pair, from the canonical row's tags and every
    # duplicate's. ``DISTINCT`` because two copies usually share a tag.
    tags = (
        select(group_id.label("gid"), NewsArticleTicker.ticker.label("ticker"))
        .join(NewsArticleTicker, NewsArticleTicker.article_id == member.id)
        .distinct()
        .cte("group_tags")
    )
    other = tags.alias("other_tags")
    canonical = aliased(NewsArticle, name="canonical")
    labels = group_labels()
    rules = labels.alias("rules_label")
    vendor = labels.alias("vendor_label")
    # The items: tags union label tickers, one row per (group, ticker), with
    # ``tagged`` 1 when any copy carries the ticker as a tag. A label on a
    # company the headline names, untagged, is an item of its own
    # (``attributedBy: headline``).
    pairs = union_all(
        select(tags.c.gid, tags.c.ticker, literal(1).label("tagged")),
        select(labels.c.gid, labels.c.ticker, literal(0).label("tagged")),
    ).subquery("item_pairs")
    items = (
        select(pairs.c.gid, pairs.c.ticker, func.max(pairs.c.tagged).label("tagged"))
        .group_by(pairs.c.gid, pairs.c.ticker)
        .cte("group_items")
    )

    stmt: Select[Any] = (
        select(
            canonical.id,
            canonical.published_at,
            canonical.headline,
            canonical.url,
            canonical.publisher,
            items.c.ticker,
            items.c.tagged,
            rules.c.direction.label("rules_direction"),
            rules.c.rule_id.label("rules_rule_id"),
            rules.c.reasoning.label("rules_reasoning"),
            vendor.c.direction.label("vendor_direction"),
            vendor.c.reasoning.label("vendor_reasoning"),
        )
        .join(items, items.c.gid == canonical.id)
        .outerjoin(
            rules,
            and_(
                rules.c.gid == items.c.gid,
                rules.c.ticker == items.c.ticker,
                rules.c.source == LabelSource.RULES.value,
                rules.c.pick == 1,
            ),
        )
        .outerjoin(
            vendor,
            and_(
                vendor.c.gid == items.c.gid,
                vendor.c.ticker == items.c.ticker,
                vendor.c.source == LabelSource.MASSIVE.value,
                vendor.c.pick == 1,
            ),
        )
        .where(canonical.canonical_id.is_(None))
        # MARKET means "no tag": not served for a group that has a real
        # *tag*. A label-only ticker is not a tag, so a MARKET-only story
        # whose headline names a company keeps its MARKET item.
        .where(
            or_(
                items.c.ticker != MARKET_TICKER,
                ~exists().where(
                    and_(other.c.gid == items.c.gid, other.c.ticker != MARKET_TICKER)
                ),
            )
        )
    )
    if since is not None:
        stmt = stmt.where(canonical.published_at >= since)
    if tickers_in is not None:
        stmt = stmt.where(items.c.ticker.in_(sorted(tickers_in)))
    if ticker is not None:
        stmt = stmt.where(items.c.ticker == ticker)
    if publisher is not None:
        stmt = stmt.where(canonical.publisher == publisher)
    if sector is not None:
        if sector == MACRO_SECTOR:
            stmt = stmt.where(items.c.ticker == MARKET_TICKER)
        elif sector == OTHER_SECTOR:
            stmt = stmt.where(items.c.ticker != MARKET_TICKER)
            if seed is not None:
                stmt = stmt.where(items.c.ticker.not_in(sorted(seed.symbols())))
        else:
            in_sector = (
                sorted(symbol for symbol in seed.symbols() if seed.sector_of(symbol) == sector)
                if seed is not None
                else []
            )
            stmt = stmt.where(items.c.ticker.in_(in_sector))
    # On the *displayed* label: rules, then vendor (decision 4).
    if sentiment == _UNLABELLED:
        stmt = stmt.where(rules.c.direction.is_(None), vendor.c.direction.is_(None))
    elif sentiment is not None:
        stmt = stmt.where(func.coalesce(rules.c.direction, vendor.c.direction) == sentiment)
    return stmt, canonical, items.c.ticker


def _label(
    source: LabelSource, direction: str, rule_id: str | None, reasoning: str | None
) -> NewsLabel:
    return NewsLabel(
        sentiment=_direction(direction),
        tier="rules" if SOURCE_TIER[source] is SentimentTier.RULES else "vendor",
        source=_wire_source(source),
        source_status=SOURCE_STATUS[source],
        rule_id=rule_id,
        reasoning=reasoning,
    )


def _direction(stored: str) -> NewsDirection:
    """A stored ``sentiment_label.direction`` as the wire literal (CHECK-constrained)."""
    direction = Direction(stored)
    if direction is Direction.BULLISH:
        return "bullish"
    if direction is Direction.BEARISH:
        return "bearish"
    return "neutral"


def _feed_item(row: Any, seed: SpdrSeed | None) -> NewsItem:
    """One feed row, its displayed label by precedence rules -> vendor."""
    rules = (
        None
        if row.rules_direction is None
        else _label(LabelSource.RULES, row.rules_direction, row.rules_rule_id, row.rules_reasoning)
    )
    vendor = (
        None
        if row.vendor_direction is None
        else _label(LabelSource.MASSIVE, row.vendor_direction, None, row.vendor_reasoning)
    )
    shown = rules if rules is not None else vendor
    other = vendor if rules is not None else None
    return NewsItem(
        id=f"{row.id}:{row.ticker}",
        time=row.published_at,
        ticker=row.ticker,
        headline=row.headline,
        sentiment=_UNLABELLED if shown is None else shown.sentiment,
        publisher=row.publisher,
        sector=_sector(row.ticker, seed),
        tier=None if shown is None else shown.tier,
        url=row.url,
        source=None if shown is None else shown.source,
        source_status=None if shown is None else shown.source_status,
        other_label=other,
        attributed_by="tag" if row.tagged else "headline",
    )


@router.get("", summary="Canonical news rows, one per ticker, newest first")
def read_news(
    session: SessionDep,
    seed: SpdrSeedDep,
    positions: PositionUnderlyingsDep,
    now: NowDep,
    lookback: Annotated[NewsLookback, Query()] = "all",
    scope: Annotated[
        NewsScope, Query(description="`watch`: the watch universe plus MARKET.")
    ] = "watch",
    ticker: Annotated[str | None, Query(max_length=32)] = None,
    sector: Annotated[str | None, Query(max_length=64)] = None,
    publisher: Annotated[str | None, Query(max_length=128)] = None,
    sentiment: Annotated[NewsSentiment | None, Query()] = None,
    sort: Annotated[NewsSort, Query()] = "newest",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NewsFeed:
    correlation_id = str(uuid.uuid4())
    since = lookback_start(lookback, now)
    tickers_in: frozenset[str] | None = None
    if scope == "watch":
        universe, _ = _universe(session, seed, positions, correlation_id)
        tickers_in = frozenset(universe.symbols)

    stmt, canonical, ticker_col = _feed_query(
        since=since,
        tickers_in=tickers_in,
        ticker=normalize_symbol(ticker) if ticker is not None else None,
        publisher=publisher,
        sector=sector,
        sentiment=sentiment,
        seed=seed,
    )
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    if sort == "newest":
        ordering = (canonical.published_at.desc(), canonical.id.desc(), ticker_col.asc())
    else:
        ordering = (canonical.published_at.asc(), canonical.id.asc(), ticker_col.asc())
    rows = session.execute(stmt.order_by(*ordering).offset(offset).limit(limit)).all()

    items = [_feed_item(row, seed) for row in rows]
    return NewsFeed(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        lookback=lookback,
        scope=scope,
        sort=sort,
        since=since,
        sectors_available=seed is not None,
        seed_as_of=seed.as_of if seed is not None else None,
    )


# --------------------------------------------------------------------------
# GET /api/news/movers
# --------------------------------------------------------------------------


def _verdict(row: TickerTradeability, correlation_id: str) -> CachedVerdict:
    """A cache row as the read path needs it. An unreadable row fails closed, logged."""
    failures = tuple(token for token in row.failures.split(",") if token)
    try:
        return CachedVerdict(
            ticker=row.ticker,
            session_date=row.session_date,
            passes=row.passes,
            avg_volume_20d=row.avg_volume_20d,
            last_close=row.last_close,
            sessions_available=row.sessions_available,
            failures=failures,
            checked_at=row.checked_at,
        )
    except ValueError as exc:
        logger.error(
            "a tradeability cache row cannot be read; the ticker is treated as failing",
            extra={
                "event": "discovery_verdict_unreadable",
                "rule": "decision 21: a ticker is a candidate only on a verdict that passes",
                "ticker": row.ticker,
                "session_date": row.session_date.isoformat(),
                "error": str(exc),
                "correlation_id": correlation_id,
            },
        )
        return CachedVerdict(
            ticker=row.ticker,
            session_date=row.session_date,
            passes=False,
            avg_volume_20d=None,
            last_close=None,
            sessions_available=row.sessions_available,
            failures=("unreadable_cache_row",),
            checked_at=row.checked_at,
        )


def _latest_verdicts(
    session: Session, tickers: Iterable[str], today: date, correlation_id: str
) -> dict[str, CachedVerdict]:
    """Each ticker's newest cached verdict for a session on or before ``today``.

    Read from ``ticker_tradeability`` only -- never a vendor. The newest row
    is chosen in Python, by ``session_date``, which is a real ``Date``
    column; nothing here compares ``Money``.
    """
    wanted = sorted(set(tickers))
    newest: dict[str, TickerTradeability] = {}
    for start in range(0, len(wanted), _VERDICT_CHUNK):
        chunk = wanted[start : start + _VERDICT_CHUNK]
        rows = session.scalars(
            select(TickerTradeability)
            .where(TickerTradeability.ticker.in_(chunk))
            .where(TickerTradeability.session_date <= today)
        )
        for row in rows:
            held = newest.get(row.ticker)
            if held is None or row.session_date > held.session_date:
                newest[row.ticker] = row
    return {ticker: _verdict(row, correlation_id) for ticker, row in newest.items()}


def _job_failing_or_skipped(status: JobStatus) -> bool:
    """The job's most recent completed outcome was a failure or a skip.

    A skip fetched nothing (no key configured, say), so the feed is not being
    read; a failure is a fault. A job that has completed nothing yet is
    neither.
    """
    if status.failing:
        return True
    if status.last_skipped is None:
        return False
    return status.last_success is None or status.last_skipped > status.last_success


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _overdue(record: JobStatus, since: datetime | None, now: datetime) -> bool:
    """:data:`STALE_AFTER_SLOTS` of the job's own slots have passed since ``since``.

    ``since`` is the feed's last known-good instant, else the start of a run
    that never finished; ``None`` -- nothing delivered and nothing started --
    is no evidence either way. A slot "has passed" when it is strictly
    before ``now``. A schedule with nothing more to say stops the count:
    that job's ``stopped`` flag is what reports it.
    """
    if since is None:
        return False
    slot: datetime | None = since
    for _ in range(STALE_AFTER_SLOTS):
        assert slot is not None
        slot = record.cadence.next_run(slot)
        if slot is None:
            return False
    assert slot is not None
    return slot < now


def _feed_stale(record: JobStatus | None, delivered: datetime | None, now: datetime) -> bool:
    """Decision 21's *stale* for one discovery feed. See :class:`~corollary.api.schemas.NewsMovers`.

    Stale when there is no scheduler or no such job; when its latest outcome
    failed or skipped; when its job has stopped and will never run again;
    or when :data:`STALE_AFTER_SLOTS` of its slots have passed since it last
    delivered (``max(last_success, newest row ingested)``) -- a hung run, a
    dead task, and a restart into a days-old feed all read stale that way.
    """
    if record is None or _job_failing_or_skipped(record):
        return True
    if record.stopped:
        return True
    reference = delivered if delivered is not None else record.last_started
    return _overdue(record, reference, now)


def _feed_freshness(
    session: Session, status: Mapping[str, JobStatus] | None, now: datetime
) -> list[DiscoveryFeedFreshness]:
    newest: dict[str, datetime] = {
        feed: at
        for feed, at in session.execute(
            select(NewsArticle.feed, func.max(NewsArticle.ingested_at))
            .where(NewsArticle.feed.in_(sorted(DISCOVERY_FEED_JOBS)))
            .group_by(NewsArticle.feed)
        ).tuples()
    }
    feeds: list[DiscoveryFeedFreshness] = []
    for feed, job in DISCOVERY_FEED_JOBS.items():
        record = None if status is None else status.get(job)
        ingested = newest.get(feed)
        last_success = None if record is None else record.last_success
        delivered = _later(last_success, ingested)
        stale = _feed_stale(record, delivered, now)
        feeds.append(
            DiscoveryFeedFreshness(
                feed=feed,
                job=job,
                scheduled=record is not None,
                last_success=last_success,
                last_failure=None if record is None else record.last_failure,
                failing=False if record is None else record.failing,
                last_ingested_at=ingested,
                stale=stale,
                stale_since=delivered if stale else None,
            )
        )
    return feeds


def _evaluation(
    session: Session, status: Mapping[str, JobStatus] | None
) -> DiscoveryEvaluation:
    record = None if status is None else status.get(TRADEABILITY_JOB)
    last_checked: datetime | None = session.scalar(
        select(func.max(TickerTradeability.checked_at))
    )
    return DiscoveryEvaluation(
        job=TRADEABILITY_JOB,
        scheduled=record is not None,
        last_success=None if record is None else record.last_success,
        last_failure=None if record is None else record.last_failure,
        failing=False if record is None else record.failing,
        last_checked_at=last_checked,
    )


def _mover_direction(direction: Direction) -> MoverDirection:
    if direction is Direction.BULLISH:
        return "bullish"
    if direction is Direction.BEARISH:
        return "bearish"
    raise ValueError(f"a discovery reason is directional, not {direction.value}")


def _wire_source(source: LabelSource) -> NewsLabelSource:
    return "rules" if source is LabelSource.RULES else "massive"


def _mover(candidate: Candidate, seed: SpdrSeed | None) -> NewsMover:
    reasons = [
        NewsMoverReason(
            source=_wire_source(reason.source),
            tier="rules" if reason.tier is SentimentTier.RULES else "vendor",
            rule_id=reason.rule_id,
            family=None if reason.family is None else reason.family.value,
            direction=_mover_direction(reason.direction),
            article_ids=list(reason.article_ids),
            evidence=[
                NewsMoverEvidence(
                    article_id=item.article_id,
                    time=item.published_at,
                    reasoning=item.reasoning,
                )
                for item in reason.evidence
            ],
            latest_at=reason.latest_at,
        )
        for reason in candidate.reasons
    ]
    return NewsMover(
        ticker=candidate.ticker,
        sector=_sector(candidate.ticker, seed),
        rank_group="rules" if candidate.has_rules_reason else "massive_only",
        reasons=reasons,
        directions=[_mover_direction(d) for d in candidate.directions],
        articles=[
            NewsMoverArticle(
                id=article.id,
                time=article.published_at,
                headline=article.headline,
                url=article.url,
                publisher=article.publisher,
            )
            for article in candidate.articles
        ],
        article_count=len(candidate.articles),
        latest_at=candidate.latest_at,
        avg_volume=candidate.avg_volume_20d,
        adv_sessions=candidate.sessions_available,
        # ``format(..., "f")``: the exact stored digits, never an exponent.
        last_close=format(candidate.last_close, "f"),
        tradeability_session_date=candidate.tradeability_session_date,
        tradeability_checked_at=candidate.tradeability_checked_at,
    )


@router.get("/movers", summary="Discovery candidates: off-watch names flagged by news, ranked")
def read_movers(
    session: SessionDep,
    seed: SpdrSeedDep,
    positions: PositionUnderlyingsDep,
    status: SchedulerStatusDep,
    now: NowDep,
    lookback: Annotated[NewsLookback, Query()] = "today",
) -> NewsMovers:
    """Decision 21's *Movers in the news*, derived on read and served in rank order.

    Flagged by news, not confirmed by price: no snapshot and no vendor call.
    """
    correlation_id = str(uuid.uuid4())
    since = lookback_start(lookback, now)
    today = now.astimezone(EASTERN).date()
    universe, _ = _universe(session, seed, positions, correlation_id)
    signals = stored_signals(session, since)
    off_watch = {s.ticker for s in signals if s.ticker not in universe}
    verdicts = _latest_verdicts(session, off_watch, today, correlation_id)
    found = discover(signals, watched=universe, verdicts=verdicts, since=since)
    feeds = _feed_freshness(session, status, now)
    stale = [feed for feed in feeds if feed.stale]
    known_good = [feed.stale_since for feed in stale if feed.stale_since is not None]

    logger.info(
        "discovery evaluated: %d movers from %d signalled off-watch names",
        len(found.candidates),
        found.signalled,
        extra={
            "event": "discovery_evaluated",
            "lookback": lookback,
            "since": None if since is None else since.isoformat(),
            "signalled": found.signalled,
            "movers": len(found.candidates),
            "watched_excluded": found.watched_excluded,
            "failed_tradeability": found.failed_tradeability,
            "awaiting_check": len(found.awaiting_check),
            "stale_feeds": [feed.feed for feed in stale],
            "at": now.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return NewsMovers(
        items=[_mover(candidate, seed) for candidate in found.candidates],
        lookback=lookback,
        since=since,
        evaluated_at=now,
        signalled=found.signalled,
        watched_excluded=found.watched_excluded,
        failed_tradeability=found.failed_tradeability,
        awaiting_check=len(found.awaiting_check),
        feeds=feeds,
        feeds_stale=bool(stale),
        stale_since=min(known_good) if known_good else None,
        evaluation=_evaluation(session, status),
        sectors_available=seed is not None,
    )


# --------------------------------------------------------------------------
# The watch routes
# --------------------------------------------------------------------------

TickerPath = Annotated[str, Path(max_length=32, description="Normalised server-side.")]

#: Refusals from ``watchlist.py`` and the status each maps to.
_REFUSAL_STATUS: Final[dict[WatchRefusal, int]] = {
    WatchRefusal.INVALID_SYMBOL: 422,
    WatchRefusal.RESERVED: 409,
    WatchRefusal.ALREADY_MANUAL: 409,
    WatchRefusal.ALREADY_MEMBER: 409,
    WatchRefusal.CAP_REACHED: 409,
    WatchRefusal.NOT_MEMBER: 404,
    WatchRefusal.NOT_MANUAL: 409,
}

_RULE: Final = (
    "decision 21: only manual watches are added or removed; an added ticker is "
    "an active US equity in the cached asset list; Q13: at most "
    f"{MANUAL_WATCH_CAP} active manual watches"
)


def _refuse(
    *,
    action: str,
    raw: str,
    status: int,
    code: str,
    message: str,
    at: datetime,
    correlation_id: str,
) -> ApiError:
    """Log a refused watch change (rule, inputs, time) and build its error."""
    logger.warning(
        "watch %s refused: %s",
        action,
        message,
        extra={
            "event": "watch_change_refused",
            "rule": _RULE,
            "action": action,
            "ticker": raw[:32],
            "status": status,
            "code": code,
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return ApiError(status_code=status, code=code, message=message)


def _refuse_check(
    check: WatchChangeCheck, *, action: str, raw: str, at: datetime, correlation_id: str
) -> ApiError:
    assert check.refusal is not None
    return _refuse(
        action=action,
        raw=raw,
        status=_REFUSAL_STATUS[check.refusal],
        code=f"watch_{check.refusal.value}",
        message=check.detail,
        at=at,
        correlation_id=correlation_id,
    )


def _write_audit(
    session: Session, *, ticker: str, previous: str, new: str, at: datetime
) -> AuditChange:
    session.add(
        AuditLog(
            at=at,
            category=_AUDIT_CATEGORY,
            field=ticker,
            previous_value=previous,
            new_value=new,
        )
    )
    return AuditChange(category=_AUDIT_CATEGORY, field=ticker, previous=previous, new=new)


def _announce(
    request: Request,
    background: BackgroundTasks,
    change: AuditChange,
    *,
    action: str,
    at: datetime,
    correlation_id: str,
) -> None:
    """After the commit: log the change and schedule its one notice."""
    logger.info(
        "manual watch %s: %s",
        action,
        change.field,
        extra={
            "event": "watch_changed",
            "action": action,
            "ticker": change.field,
            "previous": change.previous,
            "new": change.new,
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    notify_after_response(
        request,
        background,
        settings_notice(
            OperatorEvent.WATCHLIST_CHANGED,
            [change],
            at=at,
            correlation_id=correlation_id,
        ),
    )


@router.get("/watch", summary="Manual watches and the watch universe they sit in")
def read_watch(
    session: SessionDep,
    seed: SpdrSeedDep,
    positions: PositionUnderlyingsDep,
    directory: AssetDirectoryDep,
) -> WatchList:
    return _watch_list(session, seed, positions, directory, str(uuid.uuid4()))


@router.post("/watch/{ticker}", summary="Add a manual watch. Audit-logged.")
def add_watch(
    ticker: TickerPath,
    session: SessionDep,
    seed: SpdrSeedDep,
    positions: PositionUnderlyingsDep,
    directory: AssetDirectoryDep,
    now: NowDep,
    request: Request,
    background: BackgroundTasks,
) -> WatchList:
    correlation_id = str(uuid.uuid4())
    with _WATCH_WRITE_LOCK:
        universe, _ = _universe(session, seed, positions, correlation_id)
        check = can_add_manual(universe, ticker)
        if check.refusal is not None and check.refusal is not WatchRefusal.CAP_REACHED:
            raise _refuse_check(
                check, action="add", raw=ticker, at=now, correlation_id=correlation_id
            )
        # A real equity before a full cap: a garbage ticker is told it is
        # garbage, not that there is no room for it.
        assets = directory.current()
        if assets is None:
            raise _refuse(
                action="add",
                raw=ticker,
                status=503,
                code="asset_list_unavailable",
                message=(
                    f"{check.ticker} cannot be validated: the asset list has not been "
                    "fetched yet, and a watch is never added unvalidated. It is "
                    "refreshed daily by the scheduler; try again once it has run."
                ),
                at=now,
                correlation_id=correlation_id,
            )
        if check.ticker not in assets:
            raise _refuse(
                action="add",
                raw=ticker,
                status=422,
                code="not_an_active_us_equity",
                message=f"{check.ticker} is not an active US equity in the asset list",
                at=now,
                correlation_id=correlation_id,
            )
        if check.refusal is WatchRefusal.CAP_REACHED:
            raise _refuse_check(
                check, action="add", raw=ticker, at=now, correlation_id=correlation_id
            )

        session.add(WatchSymbol(ticker=check.ticker, added_at=now))
        change = _write_audit(
            session, ticker=check.ticker, previous=NOT_WATCHED, new=WATCHED, at=now
        )
        session.commit()

    _announce(request, background, change, action="added", at=now, correlation_id=correlation_id)
    return _watch_list(session, seed, positions, directory, correlation_id)


@router.delete("/watch/{ticker}", summary="Remove a manual watch. Audit-logged.")
def remove_watch(
    ticker: TickerPath,
    session: SessionDep,
    seed: SpdrSeedDep,
    positions: PositionUnderlyingsDep,
    directory: AssetDirectoryDep,
    now: NowDep,
    request: Request,
    background: BackgroundTasks,
) -> WatchList:
    """Close the manual watch. Needs no asset list: removing validates nothing."""
    correlation_id = str(uuid.uuid4())
    with _WATCH_WRITE_LOCK:
        universe, _ = _universe(session, seed, positions, correlation_id)
        check = can_remove_manual(universe, ticker)
        if check.refusal is not None:
            raise _refuse_check(
                check, action="remove", raw=ticker, at=now, correlation_id=correlation_id
            )
        row = session.scalars(
            select(WatchSymbol).where(
                WatchSymbol.ticker == check.ticker, WatchSymbol.removed_at.is_(None)
            )
        ).one()
        # ``ck_watch_symbol_removed_after_added``: a clock that stepped back
        # must not turn a removal into an integrity error.
        row.removed_at = max(now, row.added_at)
        change = _write_audit(
            session, ticker=check.ticker, previous=WATCHED, new=NOT_WATCHED, at=now
        )
        session.commit()

    _announce(
        request, background, change, action="removed", at=now, correlation_id=correlation_id
    )
    return _watch_list(session, seed, positions, directory, correlation_id)
