"""The stored-signal read: one label per (group, ticker, source), and which tickers it signals.

Two readers ask the same question of ``sentiment_label`` and must get the same
answer: ``GET /api/news/movers``, which ranks discovery candidates, and the
tradeability refresh, which decides which tickers to spend a check on
(decision 21: *"filled lazily, only for off-watch tickers that already carry a
qualifying signal"*). Two copies of that question drift -- the refresh once
read the *tag* table while the labeller attaches labels to any company a
headline names, so a rules signal on an untagged ticker was never checked and
sat in Movers' ``awaitingCheck`` for good. So both read through here.

Decisions this module makes
---------------------------

* **One label per (duplicate group, ticker, source)**, picked the way the
  feed picks it (:func:`group_labels`): the canonical row's label when it
  carries one, else the lowest member id's. The feed, Movers and the refresh
  therefore agree on which copy's label a group *has*. The pick runs over
  every label, **then** the directional filter applies -- filtering first
  would let a member's directional label stand in for a canonical row's
  neutral one, and Movers would show a label the feed does not.
* **"Qualifying" is** :func:`~corollary.data.news.discovery.qualifies`, never
  restated here.
* **The refresh's window is the longest bounded lookback** the page offers
  (``2w``, :data:`SIGNAL_WINDOW_DAYS`), from :data:`LOOKBACK_DAYS` itself.
  ``all`` is unbounded and labels are never pruned, so a candidate set over
  ``all`` would grow without limit; a signal older than two weeks that never
  got a verdict reads ``awaitingCheck`` under ``all`` only.
* **Order: newest qualifying label first, ties by ticker** -- the per-run cap
  (:data:`~corollary.data.news.tradeability.MAX_TICKERS_PER_RUN`) then checks
  the freshest news first, and the same rows always give the same order.

**The tradeability cache's read** (:func:`latest_verdicts`) lives here too,
for the same reason: Movers and the sentiment audit's runner both ask "what
is this off-watch ticker's newest verdict", and the runner -- a context job --
may not import ``corollary.api``, where the read used to live.

Reads only; writes nothing, calls no vendor.
"""

import logging
from collections.abc import Container, Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased

from corollary.data.news.discovery import (
    DIRECTIONAL,
    CachedVerdict,
    FLAGGED_PATTERNS,
    DiscoverySignal,
    qualifies,
)
from corollary.data.news.labels import Direction, LabelSource
from corollary.data.news.rules import RulePattern
from corollary.data.news.watchlist import MARKET_TICKER
from corollary.db.models import NewsArticle, SentimentLabelRow, TickerTradeability
from corollary.wire import require_aware

__all__ = [
    "EASTERN",
    "LOOKBACK_DAYS",
    "SIGNAL_WINDOW_DAYS",
    "VERDICT_CHUNK",
    "group_labels",
    "latest_verdicts",
    "verdict_histories",
    "lookback_start",
    "signal_window_start",
    "signalled_tickers",
    "stored_signals",
]

logger = logging.getLogger(__name__)

EASTERN: Final = ZoneInfo("America/New_York")

#: Tickers per ``IN (...)`` when reading the tradeability cache.
VERDICT_CHUNK: Final = 500

#: Calendar days per lookback, ``None`` for no bound -- ``LOOKBACK_DAYS`` in
#: ``web/src/lib/news.ts``, value for value.
LOOKBACK_DAYS: Final[Mapping[str, int | None]] = {
    "today": 1,
    "3d": 3,
    "1w": 7,
    "2w": 14,
    "all": None,
}

#: The longest *bounded* lookback, in calendar days: the tradeability
#: refresh's signal window. Derived, never re-typed.
SIGNAL_WINDOW_DAYS: Final[int] = max(days for days in LOOKBACK_DAYS.values() if days is not None)


def _days_start(days: int, now: datetime) -> datetime:
    """Midnight ET at the start of the ET date ``days - 1`` before ``now``'s, as aware UTC.

    Midnight is never inside a DST transition in New York (they happen at
    02:00), so ``datetime.combine`` with the zone is unambiguous.
    """
    require_aware(now, "now")
    first = now.astimezone(EASTERN).date() - timedelta(days=days - 1)
    return datetime.combine(first, time.min, tzinfo=EASTERN).astimezone(timezone.utc)


def lookback_start(lookback: str, now: datetime) -> datetime | None:
    """The first instant inside ``lookback``, as aware UTC, or ``None`` for ``all``."""
    days = LOOKBACK_DAYS[lookback]
    if days is None:
        return None
    return _days_start(days, now)


def signal_window_start(now: datetime) -> datetime:
    """The first instant of the refresh's signal window: :data:`SIGNAL_WINDOW_DAYS` ET dates."""
    return _days_start(SIGNAL_WINDOW_DAYS, now)


def group_labels() -> Any:
    """One label per (duplicate group, ticker, source), as a CTE with ``pick = 1``.

    Labels sit on whichever stored copy each source read, so they are mapped
    to the group (``coalesce(canonical_id, id)``) the way tags are. When one
    source labelled several copies for one ticker -- a rules match on both
    the Alpaca and the Finnhub copy of a headline -- the canonical row's
    label is picked, else the lowest member id's: deterministic, and never
    the order rows happened to be written in. **The one selector**: the feed,
    Movers, the tradeability refresh and the sentiment audit (which grades
    only the pick, joining on ``label_id``) all read labels through it.
    """
    member = aliased(NewsArticle, name="label_member")
    group_id = func.coalesce(member.canonical_id, member.id)
    pick = func.row_number().over(
        partition_by=(group_id, SentimentLabelRow.ticker, SentimentLabelRow.source),
        order_by=(case((member.canonical_id.is_(None), 0), else_=1), member.id),
    )
    return (
        select(
            SentimentLabelRow.id.label("label_id"),
            group_id.label("gid"),
            SentimentLabelRow.ticker.label("ticker"),
            SentimentLabelRow.source.label("source"),
            SentimentLabelRow.direction.label("direction"),
            SentimentLabelRow.rule_id.label("rule_id"),
            SentimentLabelRow.reasoning.label("reasoning"),
            pick.label("pick"),
        )
        .join(member, member.id == SentimentLabelRow.article_id)
        .cte("group_labels")
    )


def stored_signals(session: Session, since: datetime | None) -> list[DiscoverySignal]:
    """Every picked directional label in the window, joined to its group's canonical article.

    ``since`` bounds the **canonical** article's ``published_at`` (``None``:
    everything stored). Only ``bullish``/``bearish`` picks are returned --
    ``neutral`` (Massive's ``neutral`` and ``mixed``) never qualifies -- and
    never ``MARKET``. Whether a rules label's pattern is discovery-flagged is
    :func:`~corollary.data.news.discovery.qualifies`'s question, not SQL's.
    At most one signal per (source, ticker, canonical article), by
    :func:`group_labels`. Ordered by canonical id, ticker and source, so the
    list is the same whatever order the rows were written in.
    """
    if since is not None:
        require_aware(since, "since")
    labels = group_labels()
    canonical = aliased(NewsArticle, name="canonical")
    stmt = (
        select(
            canonical.id,
            canonical.published_at,
            canonical.headline,
            canonical.url,
            canonical.publisher,
            labels.c.ticker,
            labels.c.source,
            labels.c.direction,
            labels.c.rule_id,
            labels.c.reasoning,
        )
        .join(canonical, canonical.id == labels.c.gid)
        .where(labels.c.pick == 1)
        .where(labels.c.direction.in_(sorted(d.value for d in DIRECTIONAL)))
        .where(labels.c.ticker != MARKET_TICKER)
        .order_by(canonical.id, labels.c.ticker, labels.c.source)
    )
    if since is not None:
        stmt = stmt.where(canonical.published_at >= since)
    return [
        DiscoverySignal(
            article_id=row.id,
            published_at=row.published_at,
            headline=row.headline,
            url=row.url,
            publisher=row.publisher,
            ticker=row.ticker,
            source=LabelSource(row.source),
            direction=Direction(row.direction),
            rule_id=row.rule_id,
            reasoning=row.reasoning,
        )
        for row in session.execute(stmt)
    ]


def signalled_tickers(
    session: Session,
    *,
    since: datetime,
    watch: Container[str],
    patterns: Mapping[str, RulePattern] = FLAGGED_PATTERNS,
) -> list[str]:
    """Off-watch tickers carrying a qualifying signal since ``since``, freshest first.

    Decision 21's candidate set for the tradeability refresh. Ordered by each
    ticker's newest qualifying signal (its canonical article's time),
    newest first, ties by ticker. A watched ticker is left out: it is already
    polled and can never be a discovery candidate.
    """
    newest: dict[str, datetime] = {}
    for found in stored_signals(session, since):
        if found.ticker in watch or not qualifies(found, patterns):
            continue
        held = newest.get(found.ticker)
        if held is None or found.published_at > held:
            newest[found.ticker] = found.published_at
    # Two passes of a stable sort: ticker ascending, then newest first.
    by_ticker = sorted(newest)
    return sorted(by_ticker, key=lambda ticker: newest[ticker], reverse=True)


# --------------------------------------------------------------------------
# The tradeability cache's read (decision 21)
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


def latest_verdicts(
    session: Session, tickers: Iterable[str], today: date, correlation_id: str
) -> dict[str, CachedVerdict]:
    """Each ticker's newest cached verdict for a session on or before ``today``.

    Read from ``ticker_tradeability`` only -- never a vendor. The newest row
    is chosen in Python, by ``session_date``, which is a real ``Date``
    column; nothing here compares ``Money``. A ticker with no row is absent
    (no verdict), never a failing one. Movers and the sentiment audit's
    runner both read through here.
    """
    wanted = sorted(set(tickers))
    newest: dict[str, TickerTradeability] = {}
    for start in range(0, len(wanted), VERDICT_CHUNK):
        chunk = wanted[start : start + VERDICT_CHUNK]
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


def verdict_histories(
    session: Session,
    tickers: Iterable[str],
    since: date,
    until: date,
    correlation_id: str,
) -> dict[str, list[CachedVerdict]]:
    """Each ticker's cached verdicts that can be "newest on or before" a session in ``[since, until]``.

    For the sentiment audit, whose labels each read the newest verdict on or
    before their own reference session (``audit.py``'s decision 25): every
    row dated ``(since, until]``, plus the newest dated on or before
    ``since`` -- older rows can never be the answer for a session at or after
    ``since``. Ascending by ``session_date``. An unreadable row fails closed
    as in :func:`latest_verdicts`; a ticker with no row is absent.
    """
    wanted = sorted(set(tickers))
    rows_by_ticker: dict[str, list[TickerTradeability]] = {}
    for start in range(0, len(wanted), VERDICT_CHUNK):
        chunk = wanted[start : start + VERDICT_CHUNK]
        rows = session.scalars(
            select(TickerTradeability)
            .where(TickerTradeability.ticker.in_(chunk))
            .where(TickerTradeability.session_date <= until)
        )
        for row in rows:
            rows_by_ticker.setdefault(row.ticker, []).append(row)
    histories: dict[str, list[CachedVerdict]] = {}
    for ticker, held in rows_by_ticker.items():
        ordered = sorted(held, key=lambda row: row.session_date)
        at_or_before = [i for i, row in enumerate(ordered) if row.session_date <= since]
        first = at_or_before[-1] if at_or_before else 0
        histories[ticker] = [_verdict(row, correlation_id) for row in ordered[first:]]
    return histories
