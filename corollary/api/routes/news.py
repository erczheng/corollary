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
it, and whether that source is demoted"*. Step 4 labels nothing, so every
item is ``unclassified`` with no tier and no source, and not demoted (decision
4's labelling is step 5). No label is invented to fill the column.

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
list. ... at most 100 symbols before position underlyings. One past the
ceiling is refused with a 409 that names it. Audit-logged, under a new
``watchlist`` category ... It notifies ``watchlist_changed`` ... Removal keeps
everything already stored."*

* **Rule 4: the ticker is validated here**, never trusted from the client --
  normalised, shape-checked and membership-checked by ``watchlist.py``'s
  :func:`can_add_manual` / :func:`can_remove_manual`, and on add looked up in
  the cached asset list. **No asset list yet is a 503**, not an accepted
  ticker: an unvalidated watch is a symbol the watch tier would poll and the
  self-audit would grade on nobody's say-so.
* **Status codes.** Ceiling: 409 naming the ticker and the 100. Not an active
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
  the threadpool, so two adds racing could both pass the ceiling check; the
  partial unique index stops a duplicate *ticker*, not a 101st symbol. One
  process, one writer (the design spec), so an in-process lock is sufficient.

Seed missing: the leaders are absent from the universe, so the cap counts
fewer symbols than it will once the seed is built. Adds are still permitted --
refusing would block every manual watch until the owner runs a quarterly
script -- and ``seedMissing`` says the count is provisional. Watches already
held when the seed arrives are kept; only further adds are refused if the
count then exceeds the ceiling.
"""

import logging
import threading
import uuid
from collections.abc import Iterable
from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Any, Final
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, Request
from sqlalchemy import Select, and_, exists, false, func, or_, select
from sqlalchemy.orm import Session, aliased

from corollary.api.deps import (
    ApiError,
    AssetDirectoryDep,
    PositionUnderlyings,
    PositionUnderlyingsDep,
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
    ManualWatch,
    NewsFeed,
    NewsItem,
    NewsLookback,
    NewsScope,
    NewsSentiment,
    NewsSort,
    WatchList,
)
from corollary.data.news.assets import AssetDirectoryHolder
from corollary.data.news.watchlist import (
    MARKET_TICKER,
    WATCH_UNIVERSE_CAP,
    WatchChangeCheck,
    WatchRefusal,
    WatchUniverse,
    can_add_manual,
    can_remove_manual,
    leaders_from_seed,
    watch_universe,
)
from corollary.data.seeds import SpdrSeed, normalize_symbol
from corollary.db.models import AuditLog, NewsArticle, NewsArticleTicker, WatchSymbol

__all__ = [
    "LOOKBACK_DAYS",
    "MACRO_SECTOR",
    "NOT_WATCHED",
    "OTHER_SECTOR",
    "WATCHED",
    "lookback_start",
    "request_now",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])

EASTERN: Final = ZoneInfo("America/New_York")

#: ``MACRO_SECTOR`` in ``types.ts``: where ``MARKET`` stories file.
MACRO_SECTOR: Final = "Macro"
#: Decision 3: a ticker outside the seed, or any ticker with no seed.
OTHER_SECTOR: Final = "Other"

#: Calendar days per lookback, ``None`` for no bound -- ``LOOKBACK_DAYS`` in
#: ``web/src/lib/news.ts``, value for value.
LOOKBACK_DAYS: Final[dict[str, int | None]] = {
    "today": 1,
    "3d": 3,
    "1w": 7,
    "2w": 14,
    "all": None,
}

#: Step 4 labels nothing (decision 4's sources arrive in step 5).
_UNLABELLED: Final[NewsSentiment] = "unclassified"

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


def lookback_start(lookback: str, now: datetime) -> datetime | None:
    """The first instant inside ``lookback``, as aware UTC, or ``None`` for ``all``.

    The start of the ET calendar date ``days - 1`` days before ``now``'s ET
    date. Midnight is never inside a DST transition in New York (they happen
    at 02:00), so ``datetime.combine`` with the zone is unambiguous.
    """
    days = LOOKBACK_DAYS[lookback]
    if days is None:
        return None
    first = now.astimezone(EASTERN).date() - timedelta(days=days - 1)
    return datetime.combine(first, time.min, tzinfo=EASTERN).astimezone(timezone.utc)


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
    counted = universe.count_before_positions
    return WatchList(
        manual=[
            ManualWatch(ticker=row.ticker, added_at=row.added_at)
            for row in _active_manual(session)
        ],
        symbols=len(universe.polled_symbols),
        count_before_positions=counted,
        cap=WATCH_UNIVERSE_CAP,
        remaining=max(WATCH_UNIVERSE_CAP - counted, 0),
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

    stmt: Select[Any] = (
        select(
            canonical.id,
            canonical.published_at,
            canonical.headline,
            canonical.url,
            canonical.publisher,
            tags.c.ticker,
        )
        .join(tags, tags.c.gid == canonical.id)
        .where(canonical.canonical_id.is_(None))
        # MARKET means "no tag": not served for a group that has a real one.
        .where(
            or_(
                tags.c.ticker != MARKET_TICKER,
                ~exists().where(
                    and_(other.c.gid == tags.c.gid, other.c.ticker != MARKET_TICKER)
                ),
            )
        )
    )
    if since is not None:
        stmt = stmt.where(canonical.published_at >= since)
    if tickers_in is not None:
        stmt = stmt.where(tags.c.ticker.in_(sorted(tickers_in)))
    if ticker is not None:
        stmt = stmt.where(tags.c.ticker == ticker)
    if publisher is not None:
        stmt = stmt.where(canonical.publisher == publisher)
    if sector is not None:
        if sector == MACRO_SECTOR:
            stmt = stmt.where(tags.c.ticker == MARKET_TICKER)
        elif sector == OTHER_SECTOR:
            stmt = stmt.where(tags.c.ticker != MARKET_TICKER)
            if seed is not None:
                stmt = stmt.where(tags.c.ticker.not_in(sorted(seed.symbols())))
        else:
            in_sector = (
                sorted(symbol for symbol in seed.symbols() if seed.sector_of(symbol) == sector)
                if seed is not None
                else []
            )
            stmt = stmt.where(tags.c.ticker.in_(in_sector))
    # Every item is unclassified in step 4, so any other label matches nothing.
    if sentiment is not None and sentiment != _UNLABELLED:
        stmt = stmt.where(false())
    return stmt, canonical, tags.c.ticker


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

    items = [
        NewsItem(
            id=f"{row.id}:{row.ticker}",
            time=row.published_at,
            ticker=row.ticker,
            headline=row.headline,
            sentiment=_UNLABELLED,
            publisher=row.publisher,
            sector=_sector(row.ticker, seed),
            tier=None,
            url=row.url,
            source=None,
            demoted=False,
        )
        for row in rows
    ]
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
    "an active US equity in the cached asset list; at most "
    f"{WATCH_UNIVERSE_CAP} symbols before position underlyings"
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
        # A real equity before a full ceiling: a garbage ticker is told it is
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
