"""Labels persisted at ingest: the name book, the write-once insert, and the prune's predicate.

Phase 3 decision 4: every article stored is labelled by every source that can
reach it -- the rules tier (:func:`~corollary.data.news.rules.rules_labels`)
on every headline, Massive (:func:`~corollary.data.news.vendor.vendor_labels`)
on the Massive articles that carry ``insights[]`` -- and each label is its own
``sentiment_label`` row, keyed by ``(article_id, ticker, source)`` and written once.

Decisions this unit made, each pinned by ``tests/data/news/test_labelling.py``
-------------------------------------------------------------------------------

* **At ingest, and only at ingest.** :func:`label_stored` runs inside the news
  store's own write, in the same transaction as the articles: an article and
  its labels commit together or not at all. **No backfill** -- the spec's *Out
  of scope* says "No label backfill (Q7)", so articles stored before this unit
  stay unlabelled. Massive's labels could not be rebuilt later in any case:
  ``insights`` are read off the fetched article and never persisted.
* **Every article passed in is labelled** -- new, a linked duplicate, or
  re-seen. A re-seen article whose labels are unchanged writes nothing (the
  labellers are deterministic, so that is the usual case; it is counted
  ``unchanged``).
* **The first-written label for ``(article_id, ticker, source)`` is
  immutable.** Massive is re-fetched over a two-hour overlap and Finnhub's
  watch tier over two days, so a vendor revising an insight or a publisher
  editing a headline re-labels an article whose label was already live. That
  later label is **not written**: it is counted (``revised_ignored``) and
  logged (``news_label_revision_ignored``) with the kept and the revised
  direction, rule and reasoning, the article's inputs and the time -- never
  silently. Q7: the first audit run grades nothing it did not see published
  live, so no label in the audit is produced with knowledge of its own
  outcome; overwriting in place would lose the label that was displayed.
* **A label is never deleted here.** If a re-label of an article produces no
  label where it once did (an edit, or a cycle whose name book was degraded),
  the earlier row stays: labels are kept indefinitely (decision 21) and the
  self-audit grades what was published at the time. No label, no row --
  this module stores labels, never their absence.
* **A vendor label is written only for a ticker ingest keeps as a tag.**
  Massive's ``insights[]`` are filtered through the same
  :func:`~corollary.data.news.ingest.filter_tags`, over the same asset
  directory, that ingest applies to the article's tags (decision 21), and
  take its spelling -- so every vendor label has a ``news_article_ticker``
  row behind it. A crypto pair, an index or a delisted symbol is dropped
  before labelling, counted (``vendor_dropped_non_equity``) and logged
  (``news_label_vendor_dropped``).
* **A label the table would refuse is refused here, counted and logged**, and
  the rest of the batch is stored: a vendor ticker outside the ``ticker``
  CHECK must not roll back a page of articles. Anything else that fails
  raises, and the whole store rolls back -- a labeller bug is a failed job
  the scheduler records, never news silently stored without labels.
* **One structured line per store** (``news_labels_written``) counts what was
  written per source, revisions ignored, unchanged, and every way a label was
  not produced -- conflicts, unattributed matches, vetoes, suppressed names,
  truncated headlines, unmapped, duplicate and non-equity insights, refusals
  -- and whether the name book's inputs were available. Never silent.

The name book
-------------

:class:`NameBooks` builds the rules tier's :class:`~corollary.data.news.rules.NameBook`
once per poll cycle from the inputs as they stand: the watch universe (an
injected source), the hand-kept :data:`~corollary.data.news.rules.WATCH_COMPANY_NAMES`,
the held asset list's names, and ``funds`` -- the curated universe's ``fund``
flag, passed in by the caller because ``data/`` may not import the API. Each
cycle re-reads the inputs and reuses the compiled book while they are
unchanged, since compiling one pattern per listed equity is not free. An
unavailable input is logged and the book is built from what is available. A
build that fails outright raises out of :meth:`NameBooks.current`; the news
store catches it, skips the rules tier for that store and says so
(``news_label_book_unavailable``), and stores the articles and their Massive
labels regardless -- a name book is one tier's input, never ingestion's.

Rule 9 / decision 1: nothing here imports the engine or the API. The
synchronous functions take a ``Session`` and do not commit; the news store
runs them in ``asyncio.to_thread``.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from corollary.data.news.article import NewsArticle, NewsFeed, VendorInsight
from corollary.data.news.assets import AssetDirectoryHolder
from corollary.data.news.ingest import StoredArticle, filter_tags
from corollary.data.news.labels import LabelSource, SentimentLabel
from corollary.data.news.rules import WATCH_COMPANY_NAMES, NameBook, rules_labels
from corollary.data.news.vendor import vendor_labels
from corollary.data.providers.interface import AssetDirectory
from corollary.db.models import SentimentLabelRow
from corollary.wire import require_aware

__all__ = [
    "BookBuild",
    "LabelWrite",
    "NameBooks",
    "WatchSource",
    "label_stored",
    "labelled_article_ids",
    "log_label_write",
]

logger = logging.getLogger(__name__)

#: ``sentiment_label.ticker``'s declared width.
_TICKER_WIDTH: Final = 32

#: The longest exception, headline or reasoning text a log line carries --
#: the same bound as ``pollers._describe`` (which this module may not import:
#: ``pollers`` imports this one).
_LOG_TEXT_WIDTH: Final = 300

#: At most this many dropped vendor tickers are listed on one log line.
_DROPPED_LISTED: Final = 20

_IMMUTABLE_RULE: Final = (
    "the first-written label for (article_id, ticker, source) is immutable: "
    "Q7 grades only labels seen published live"
)
_VENDOR_EQUITY_RULE: Final = (
    "decision 21: a vendor label is written only for a ticker ingest keeps as an active US equity tag"
)


def _bounded(text: str) -> str:
    return text[:_LOG_TEXT_WIDTH]


def _describe(exc: BaseException) -> str:
    """An exception as bounded text, as ``pollers._describe`` does it."""
    return _bounded(f"{type(exc).__name__}: {exc}")

#: The watch universe's symbols for this cycle.
WatchSource = Callable[[], Awaitable[Iterable[str]]]


# --------------------------------------------------------------------------
# The name book
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookBuild:
    """This cycle's name book, and which of its inputs it was built without."""

    book: NameBook
    #: False when the watch-universe source failed; the book has no watch names.
    watch_available: bool
    #: False when no asset list is held; the book knows only the watch universe.
    assets_available: bool


class NameBooks:
    """The rules tier's name book, built once per poll cycle and reused while
    its inputs hold. Build one per news store."""

    def __init__(
        self,
        *,
        watch: WatchSource,
        assets: AssetDirectoryHolder,
        funds: Iterable[str],
        watch_names: Mapping[str, Sequence[str]] = WATCH_COMPANY_NAMES,
    ) -> None:
        self._watch = watch
        self._assets = assets
        self._funds: frozenset[str] = frozenset(funds)
        self._watch_names = watch_names
        self._lock = asyncio.Lock()
        self._key: tuple[frozenset[str], AssetDirectory | None, bool] | None = None
        self._built: BookBuild | None = None

    @property
    def funds(self) -> frozenset[str]:
        """The tickers the book treats as funds: the universe's ``fund`` flag."""
        return self._funds

    async def current(self) -> BookBuild:
        """The book for this cycle: inputs re-read, rebuilt only if one changed."""
        async with self._lock:
            watch_available = True
            watched: frozenset[str] = frozenset()
            try:
                watched = frozenset(await self._watch())
            except Exception as exc:
                watch_available = False
                error = _describe(exc)
                logger.warning(
                    "news labelling built its name book without the watch universe this cycle: %s",
                    error,
                    extra={
                        "event": "news_label_watch_unavailable",
                        "error": error,
                        "rule": "an unavailable input leaves it out of one cycle's name book, never the labelling",
                    },
                )
            directory = self._assets.current()
            if directory is None:
                logger.warning(
                    "news labelling built its name book without the asset list this cycle",
                    extra={
                        "event": "news_label_assets_unavailable",
                        "rule": "an unavailable input leaves it out of one cycle's name book, never the labelling",
                        "watch_symbols": len(watched),
                    },
                )
            key = (watched, directory, watch_available)
            built = self._built
            if (
                built is not None
                and self._key is not None
                and self._key[0] == watched
                and self._key[1] is directory
                and self._key[2] == watch_available
            ):
                return built
            book = await asyncio.to_thread(self._build, watched, directory)
            built = BookBuild(book=book, watch_available=watch_available, assets_available=directory is not None)
            self._key = key
            self._built = built
            return built

    def _build(self, watched: frozenset[str], directory: AssetDirectory | None) -> NameBook:
        asset_names = {} if directory is None else {asset.symbol: asset.name for asset in directory.assets}
        return NameBook(
            watch=watched,
            watch_names=self._watch_names,
            asset_names=asset_names,
            funds=self._funds,
        )


# --------------------------------------------------------------------------
# The write-once insert
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabelWrite:
    """What one :func:`label_stored` call wrote, and every label it did not."""

    #: Stored articles labelled (every one passed in).
    articles: int
    #: False when no name book was given: the rules tier did not run.
    rules_available: bool
    #: New ``sentiment_label`` rows, per source.
    rules_written: int
    massive_written: int
    #: Labels that differed from the row already stored, and were not
    #: written: the first-written label is immutable.
    revised_ignored: int
    #: Labels identical to the row already stored.
    unchanged: int
    #: Articles where bullish and bearish rules both fired (no label).
    rules_conflicting: int
    #: Rule matches that bound no ticker.
    rules_unattributed: int
    #: Phrases a veto, preview, negation or near-miss turned away.
    rules_vetoed: int
    #: Names found in a headline and deliberately not used.
    rules_suppressed: int
    #: Headlines too long to match.
    rules_truncated: int
    #: Tickers given disagreeing Massive insights (no label).
    massive_conflicting: int
    #: Massive insights whose sentiment is not one of the four known values.
    massive_unmapped: int
    #: Massive insights collapsed into an agreeing one for the same ticker.
    massive_duplicates: int
    #: Massive insights on a ticker ingest does not keep as a tag (no label).
    vendor_dropped_non_equity: int
    #: Labels the table would refuse, logged and not written.
    refused: int


def labelled_article_ids(session: Session) -> Set[int]:
    """The id of every article carrying a label -- the retention prune's predicate.

    Decision 21: the prune deletes only canonical groups of which no member
    carries a label. Read under the prune's write lock (``retention.prune``).
    """
    return frozenset(session.scalars(select(SentimentLabelRow.article_id).distinct()))


def _unstorable(label: SentimentLabel) -> str | None:
    """Why ``sentiment_label``'s CHECKs would refuse ``label``, or ``None``."""
    if label.ticker != label.ticker.upper():
        return "ticker is not uppercase"
    if len(label.ticker) > _TICKER_WIDTH:
        return f"ticker is longer than {_TICKER_WIDTH} characters"
    return None


def _equity_insights(
    article: NewsArticle, assets: AssetDirectory | None
) -> tuple[NewsArticle, tuple[str, ...]]:
    """``article`` keeping only the insights ingest would keep as tags, and the
    tickers of those it dropped.

    The same :func:`filter_tags`, over the same directory, that ingest applies
    to the article's tags, so a vendor label never names a ticker with no
    ``news_article_ticker`` row; kept insights take the directory's spelling.
    Filtering *before* :func:`vendor_labels` means two insights that
    normalise to one ticker still meet its conflict and duplicate rules.
    """
    kept: list[VendorInsight] = []
    dropped: list[str] = []
    for insight in article.insights:
        tags, _ = filter_tags((insight.ticker,), assets)
        if not tags:
            dropped.append(insight.ticker)
        elif tags[0] == insight.ticker:
            kept.append(insight)
        else:
            kept.append(replace(insight, ticker=tags[0]))
    if len(kept) == len(article.insights) and all(a is b for a, b in zip(kept, article.insights)):
        return article, ()
    return replace(article, insights=tuple(kept)), tuple(dropped)


def _same(row: SentimentLabelRow, label: SentimentLabel) -> bool:
    return (
        row.tier == label.tier.value
        and row.direction == label.direction.value
        and row.reasoning == label.reasoning
        and row.rule_id == label.rule_id
    )


def label_stored(
    session: Session,
    stored: Sequence[StoredArticle],
    book: NameBook | None,
    *,
    assets: AssetDirectory | None,
    now: datetime,
) -> LabelWrite:
    """Label every stored article and write its new rows. Does not commit.

    ``book`` is ``None`` when no name book is available: the rules tier is
    skipped (and :attr:`LabelWrite.rules_available` says so); Massive's labels
    need none. ``assets`` must be the directory ingest filtered these
    articles' tags with (``None`` when it had none): vendor insights are
    filtered the same way. ``now`` must be aware and becomes ``labeled_at``
    on every row written. A row already stored is never changed.
    """
    require_aware(now, "now")
    stamp = now.astimezone(timezone.utc)
    written = {LabelSource.RULES: 0, LabelSource.MASSIVE: 0}
    revised_ignored = unchanged = refused = 0
    r_conflicting = r_unattributed = r_vetoed = r_suppressed = r_truncated = 0
    m_conflicting = m_unmapped = m_duplicates = 0
    vendor_dropped: list[tuple[int, str]] = []

    for item in stored:
        labels: list[SentimentLabel] = []
        if book is not None:
            rules = rules_labels(item.article, book)
            labels.extend(rules.labels)
            r_conflicting += 1 if rules.conflicting else 0
            r_unattributed += len(rules.unattributed)
            r_vetoed += len(rules.vetoed)
            r_suppressed += len(rules.suppressed)
            r_truncated += 1 if rules.truncated else 0
        if item.article.feed is NewsFeed.MASSIVE_NEWS:
            equities, dropped = _equity_insights(item.article, assets)
            vendor_dropped.extend((item.article_id, ticker) for ticker in dropped)
            vendor = vendor_labels(equities)
            labels.extend(vendor.labels)
            m_conflicting += len(vendor.conflicting)
            m_unmapped += len(vendor.unmapped)
            m_duplicates += len(vendor.duplicates)
        if not labels:
            continue

        existing = {
            (row.ticker, row.source): row
            for row in session.scalars(
                select(SentimentLabelRow).where(SentimentLabelRow.article_id == item.article_id)
            )
        }
        for label in labels:
            problem = _unstorable(label)
            if problem is not None:
                refused += 1
                logger.warning(
                    "news label refused for article %d: %s",
                    item.article_id,
                    problem,
                    extra={
                        "event": "news_label_refused",
                        "rule": f"sentiment_label CHECK: {problem}",
                        "article_id": item.article_id,
                        "ticker": label.ticker[: _TICKER_WIDTH * 2],
                        "source": label.source.value,
                        "direction": label.direction.value,
                        "at": stamp.isoformat(),
                    },
                )
                continue
            key = (label.ticker, label.source.value)
            row = existing.get(key)
            if row is None:
                row = SentimentLabelRow(
                    article_id=item.article_id,
                    ticker=label.ticker,
                    source=label.source.value,
                    tier=label.tier.value,
                    direction=label.direction.value,
                    reasoning=label.reasoning,
                    rule_id=label.rule_id,
                    labeled_at=stamp,
                )
                session.add(row)
                existing[key] = row
                written[label.source] += 1
            elif _same(row, label):
                unchanged += 1
            else:
                revised_ignored += 1
                _log_revision_ignored(item, row, label, stamp)

    if vendor_dropped:
        logger.warning(
            "news labelling dropped %d Massive insight(s) on tickers that are not active US equities",
            len(vendor_dropped),
            extra={
                "event": "news_label_vendor_dropped",
                "rule": _VENDOR_EQUITY_RULE,
                "dropped": len(vendor_dropped),
                "insights": [
                    {"article_id": article_id, "ticker": ticker[: _TICKER_WIDTH * 2]}
                    for article_id, ticker in vendor_dropped[:_DROPPED_LISTED]
                ],
                "assets_available": assets is not None,
                "at": stamp.isoformat(),
            },
        )
    session.flush()
    return LabelWrite(
        articles=len(stored),
        rules_available=book is not None,
        rules_written=written[LabelSource.RULES],
        massive_written=written[LabelSource.MASSIVE],
        revised_ignored=revised_ignored,
        unchanged=unchanged,
        rules_conflicting=r_conflicting,
        rules_unattributed=r_unattributed,
        rules_vetoed=r_vetoed,
        rules_suppressed=r_suppressed,
        rules_truncated=r_truncated,
        massive_conflicting=m_conflicting,
        massive_unmapped=m_unmapped,
        massive_duplicates=m_duplicates,
        vendor_dropped_non_equity=len(vendor_dropped),
        refused=refused,
    )


def _log_revision_ignored(
    item: StoredArticle, row: SentimentLabelRow, label: SentimentLabel, stamp: datetime
) -> None:
    """The one line for a later label that differed from the stored one."""
    logger.warning(
        "news label revision ignored for article %d %s (%s): kept %s %s, not %s %s",
        item.article_id,
        row.ticker,
        row.source,
        row.direction,
        row.rule_id,
        label.direction.value,
        label.rule_id,
        extra={
            "event": "news_label_revision_ignored",
            "rule": _IMMUTABLE_RULE,
            "article_id": item.article_id,
            "ticker": row.ticker,
            "source": row.source,
            "kept_tier": row.tier,
            "kept_direction": row.direction,
            "kept_rule_id": row.rule_id,
            "kept_reasoning": None if row.reasoning is None else _bounded(row.reasoning),
            "kept_labeled_at": row.labeled_at.isoformat(),
            "revised_tier": label.tier.value,
            "revised_direction": label.direction.value,
            "revised_rule_id": label.rule_id,
            "revised_reasoning": None if label.reasoning is None else _bounded(label.reasoning),
            "vendor": item.article.vendor,
            "vendor_id": _bounded(item.article.vendor_id),
            "feed": item.article.feed.value,
            "headline": _bounded(item.article.headline),
            "at": stamp.isoformat(),
        },
    )


def log_label_write(write: LabelWrite, build: BookBuild | None) -> None:
    """The one ``news_labels_written`` line for one store. ``build`` is ``None``
    when the store has no name-book source (the rules tier did not run)."""
    logger.info(
        "news labels: %d rules and %d massive written, %d revision(s) ignored, %d unchanged over %d article(s)",
        write.rules_written,
        write.massive_written,
        write.revised_ignored,
        write.unchanged,
        write.articles,
        extra={
            "event": "news_labels_written",
            "articles": write.articles,
            "rules_available": write.rules_available,
            "watch_available": None if build is None else build.watch_available,
            "assets_available": None if build is None else build.assets_available,
            "rules_written": write.rules_written,
            "massive_written": write.massive_written,
            "revised_ignored": write.revised_ignored,
            "unchanged": write.unchanged,
            "rules_conflicting": write.rules_conflicting,
            "rules_unattributed": write.rules_unattributed,
            "rules_vetoed": write.rules_vetoed,
            "rules_suppressed": write.rules_suppressed,
            "rules_truncated": write.rules_truncated,
            "massive_conflicting": write.massive_conflicting,
            "massive_unmapped": write.massive_unmapped,
            "massive_duplicates": write.massive_duplicates,
            "vendor_dropped_non_equity": write.vendor_dropped_non_equity,
            "refused": write.refused,
        },
    )
