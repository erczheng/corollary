"""Decision 21's retention prune: null old summaries, delete old unlabelled groups.

The rule (Phase 3 spec, decision 21 *Storage and retention*):

* **Full rows for 90 days.**
* Then **nightly at 03:00 ET** the ``summary`` of every article older than 90
  days is nulled, and every **canonical group** of which no member carries a
  label is deleted. A group goes as a unit, so no ``canonical_id`` dangles and
  no labelled duplicate loses its canonical row.
* Labels, outcomes, audit runs and every labelled article's headline, URL,
  publisher and time are kept indefinitely. **No ``VACUUM``** is scheduled.

This module is the job's *body* only -- :func:`prune` -- and takes no part in
scheduling it. It is a context job (decision 1), so it is never a rule-9
producer: nothing here imports the engine runtime, its state, the sockets or
the API.

Decisions this unit made, each pinned by ``tests/data/news/test_retention.py``
-------------------------------------------------------------------------------

* **Age is ``published_at``'s**, never ``ingested_at``'s. A late backfill of a
  story published four months ago is four months old.
* **The cutoff is exclusive.** ``cutoff = now - 90 days``; an article is
  *older than 90 days* when ``published_at < cutoff``. One published exactly
  at the cutoff keeps its summary and its row tonight, and loses them
  tomorrow. The same comparison governs both halves of the rule.
* **A group is deleted only if every member is past the cutoff and no member
  is labelled.** One recent member keeps the whole group (its old members
  still lose their summaries -- that rule is per article). "Group" means a
  canonical row (``canonical_id IS NULL``) and every row naming it.
* **Deletes are batched by whole group**: groups are packed, in ascending
  canonical id, into ``DELETE ... WHERE id IN (...)`` statements of at most
  ``chunk_ids`` ids -- except that a single group larger than the budget gets
  a statement of its own rather than being split. The self-FK has **no**
  delete action (``db/models.py``, ``NewsArticle``), and SQLite checks it at
  the end of each statement, so a statement holding a canonical row without
  all its duplicates *fails*; a split group is not a performance problem but
  an error. ``news_article_ticker`` rows go by ``ON DELETE CASCADE``.
* **A chain is skipped, logged, and does not stop the rest.** Ingest promises
  no chains (a duplicate always names a canonical row), and the schema cannot
  state it -- SQLite admits no subquery in a CHECK. If one appears anyway --
  a row naming a row that itself names another, cycles included -- every
  candidate group it touches is left alone, reported in
  :attr:`PruneResult.groups_skipped` and logged as
  ``news_retention_group_skipped``. Deleting either end could trip the
  self-FK, and guessing which row was meant to be canonical is ingest's
  business, not the prune's.
* **The summary UPDATE runs first, and that is load-bearing.** Python's
  ``sqlite3`` opens its transaction lazily, before the first write, so the
  prune's reads would otherwise run outside any transaction and a concurrent
  writer -- ingest adding a duplicate to a group about to be deleted, or step
  5's labeller labelling a row about to be deleted -- could land between the
  read and the delete. The UPDATE takes SQLite's database-wide write lock,
  held to the caller's commit, so everything read after it is what gets
  deleted. The consequence for the counts: ``summaries_nulled`` includes rows
  nulled and then deleted in the same run.
* **Idempotent.** A second run over the same table at the same ``now`` finds
  no summary to null and no group to delete, and returns zeros.
* **It never commits.** The caller owns the transaction, and a failure
  anywhere rolls the whole prune back -- summaries included.

Labels
------

``sentiment_label`` is step 5's table and does not exist yet, so which
articles carry a label is **injected**: ``labelled_article_ids`` is called
once, under the write lock, and returns the ids of every labelled article. It
defaults to :func:`no_labels`. **Step 5 wires ``sentiment_label`` in** by
passing a predicate that selects its distinct article ids; until then a
default run deletes every old group, which is correct only while nothing can
be labelled.
"""

import logging
from collections.abc import Callable, Iterable, Sequence, Set
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.orm import Session, aliased

from corollary.db.models import NewsArticle
from corollary.wire import require_aware

__all__ = [
    "DELETE_CHUNK_IDS",
    "RETENTION",
    "LabelledArticleIds",
    "PruneResult",
    "SkippedGroup",
    "no_labels",
    "prune",
]

logger = logging.getLogger(__name__)

#: How long an article keeps its full row (decision 21, *assumption: 90 days*).
RETENTION = timedelta(days=90)

#: Most article ids one ``DELETE ... WHERE id IN (...)`` carries. The member
#: query binds each chunk twice (``id IN`` or ``canonical_id IN``), so 1,000
#: parameters: past the 999 limit of SQLite builds before 3.32, inside the
#: 32,766 of the bundled 3.50. A group larger than this is never split -- it
#: gets a statement of its own.
DELETE_CHUNK_IDS = 500

#: The reason a candidate group is skipped when ``canonical_id`` links do not
#: form flat canonical groups -- a chain or a cycle.
REASON_CANONICAL_CHAIN = "canonical_chain"

#: The reason a candidate group is skipped when its members name a canonical
#: row that is not in the table. Unreachable while ``PRAGMA foreign_keys`` is
#: on, which ``create_db_engine`` sets; checked because it is cheap.
REASON_CANONICAL_MISSING = "canonical_missing"

#: Returns the id of every labelled article. Step 5 supplies the real one.
LabelledArticleIds = Callable[[Session], Set[int]]


def no_labels(session: Session) -> Set[int]:
    """The default predicate: no article carries a label.

    True until step 5 creates ``sentiment_label``; step 5 must then pass a
    predicate reading it, or the prune deletes labelled groups.
    """
    return frozenset()


@dataclass(frozen=True)
class SkippedGroup:
    """A candidate group the prune declined to decide on, and why."""

    #: The id every member names (or, for the canonical row, its own id).
    root_id: int
    #: The group's members, ascending.
    member_ids: tuple[int, ...]
    #: :data:`REASON_CANONICAL_CHAIN` or :data:`REASON_CANONICAL_MISSING`.
    reason: str


@dataclass(frozen=True)
class PruneResult:
    """What one :func:`prune` run did."""

    #: ``now - retention`` in UTC; an article is old when published before it.
    cutoff: datetime
    #: Rows whose summary this run nulled, including rows it then deleted.
    summaries_nulled: int
    #: Canonical groups deleted, each as a unit.
    groups_deleted: int
    #: Article rows deleted (ticker rows cascade and are not counted).
    articles_deleted: int
    #: Groups entirely past the cutoff that were kept because a member is
    #: labelled.
    groups_kept_labelled: int
    #: Groups entirely past the cutoff that were kept because their
    #: ``canonical_id`` links are malformed.
    groups_skipped: tuple[SkippedGroup, ...]
    #: ``DELETE`` statements issued.
    delete_statements: int


def prune(
    session: Session,
    *,
    now: datetime,
    labelled_article_ids: LabelledArticleIds,
    retention: timedelta = RETENTION,
    chunk_ids: int = DELETE_CHUNK_IDS,
) -> PruneResult:
    """Apply decision 21's retention rule to ``news_article`` as of ``now``.

    ``labelled_article_ids`` is required, with no default, on purpose: until
    step 5 the caller passes :func:`no_labels` explicitly, and step 5 must
    replace that call with the ``sentiment_label`` predicate. A default would
    let the nightly job outlive step 5 still deleting labelled groups -- or,
    under a NO ACTION label FK, failing every night and silently stopping
    retention.

    Does not commit: the caller commits, or rolls the whole prune back.
    Raises ``ValueError`` on a naive ``now``, a non-positive ``retention`` or
    a non-positive ``chunk_ids``.
    """
    require_aware(now, "now")
    if retention <= timedelta(0):
        raise ValueError(f"retention must be positive; got {retention!r}")
    if chunk_ids <= 0:
        raise ValueError(f"chunk_ids must be positive; got {chunk_ids!r}")
    cutoff = now.astimezone(timezone.utc) - retention

    # First, and first on purpose: this UPDATE takes the write lock (module
    # docstring). Everything below reads under it.
    # ``cast``: a DML statement's result is a ``CursorResult``, which carries
    # ``rowcount``; ``Session.execute`` is typed as the general ``Result``.
    nulled = cast(
        CursorResult[Any],
        session.execute(
            update(NewsArticle)
            .where(NewsArticle.published_at < cutoff)
            .where(NewsArticle.summary.is_not(None))
            .values(summary=None)
            .execution_options(synchronize_session=False)
        ),
    )
    summaries_nulled = nulled.rowcount

    labelled = labelled_article_ids(session)
    candidate_roots = _fully_old_roots(session, cutoff)
    tainted_roots = _chain_tainted_roots(session)
    members_by_root = _members(session, candidate_roots, chunk_ids)

    to_delete: list[tuple[int, ...]] = []
    skipped: list[SkippedGroup] = []
    kept_labelled = 0
    for root in candidate_roots:
        members = members_by_root.get(root, ())
        member_ids = tuple(sorted(article_id for article_id, _ in members))
        reason = _malformed_reason(root, members, tainted_roots)
        if reason is not None:
            skipped.append(SkippedGroup(root, member_ids, reason))
            continue
        if any(article_id in labelled for article_id in member_ids):
            kept_labelled += 1
            continue
        to_delete.append(member_ids)

    for group in skipped:
        logger.warning(
            "news retention skipped a group whose canonical links are malformed; "
            "ingest's no-chains invariant does not hold for it",
            extra={
                "event": "news_retention_group_skipped",
                "reason": group.reason,
                "root_id": group.root_id,
                "member_ids": list(group.member_ids),
                "cutoff": cutoff.isoformat(),
            },
        )

    articles_deleted = 0
    batches = _pack(to_delete, chunk_ids)
    for batch in batches:
        deleted = cast(
            CursorResult[Any],
            session.execute(
                delete(NewsArticle)
                .where(NewsArticle.id.in_(batch))
                .execution_options(synchronize_session=False)
            ),
        )
        articles_deleted += deleted.rowcount

    # The ORM identity map may hold rows these bulk statements changed or
    # removed; expire them so the caller's session re-reads rather than
    # trusting stale objects.
    session.expire_all()

    result = PruneResult(
        cutoff=cutoff,
        summaries_nulled=summaries_nulled,
        groups_deleted=len(to_delete),
        articles_deleted=articles_deleted,
        groups_kept_labelled=kept_labelled,
        groups_skipped=tuple(skipped),
        delete_statements=len(batches),
    )
    logger.info(
        "news retention prune ran",
        extra={
            "event": "news_retention_pruned",
            "cutoff": cutoff.isoformat(),
            "summaries_nulled": result.summaries_nulled,
            "groups_deleted": result.groups_deleted,
            "articles_deleted": result.articles_deleted,
            "groups_kept_labelled": result.groups_kept_labelled,
            "groups_skipped": len(result.groups_skipped),
            "delete_statements": result.delete_statements,
        },
    )
    return result


def _fully_old_roots(session: Session, cutoff: datetime) -> list[int]:
    """Every group root whose newest member is published before ``cutoff``.

    A row's root is its ``canonical_id``, or its own id when canonical. With
    well-formed data that is exactly the canonical group; a chain makes a
    "group" here that :func:`_chain_tainted_roots` then flags. Ascending, so
    runs are deterministic. ``published_at`` is stored as fixed-width UTC
    text (``UtcDateTime``), so ``MAX`` and ``<`` order correctly in SQL.
    """
    root = func.coalesce(NewsArticle.canonical_id, NewsArticle.id)
    rows = session.execute(
        select(root)
        .group_by(root)
        .having(func.max(NewsArticle.published_at) < cutoff)
        .order_by(root)
    ).scalars()
    return [int(r) for r in rows]


def _chain_tainted_roots(session: Session) -> set[int]:
    """Roots of every group a chain touches.

    A chain is a row ``d`` naming ``c`` where ``c`` itself names ``r``. Both
    ``c``'s group (rows naming ``c``) and ``r``'s group (which holds ``c``)
    are tainted: deleting ``r``'s group would remove ``c`` while ``d`` still
    names it. A cycle is a chain that never reaches a canonical row, and is
    caught by the same join.
    """
    duplicate = aliased(NewsArticle)
    target = aliased(NewsArticle)
    rows = session.execute(
        select(duplicate.canonical_id, target.canonical_id)
        .join(target, duplicate.canonical_id == target.id)
        .where(target.canonical_id.is_not(None))
    ).all()
    tainted: set[int] = set()
    for named, named_names in rows:
        tainted.add(int(named))
        tainted.add(int(named_names))
    return tainted


def _members(
    session: Session, roots: Sequence[int], chunk_ids: int
) -> dict[int, list[tuple[int, int | None]]]:
    """``(id, canonical_id)`` of each root's group members, keyed by root.

    A member of root ``r`` is a row naming ``r``, or ``r`` itself if it is
    canonical. Loaded ``chunk_ids`` roots at a time.
    """
    by_root: dict[int, list[tuple[int, int | None]]] = {}
    for start in range(0, len(roots), chunk_ids):
        chunk = list(roots[start : start + chunk_ids])
        rows = session.execute(
            select(NewsArticle.id, NewsArticle.canonical_id).where(
                NewsArticle.id.in_(chunk) | NewsArticle.canonical_id.in_(chunk)
            )
        ).all()
        wanted = set(chunk)
        for article_id, canonical_id in rows:
            root = article_id if canonical_id is None else canonical_id
            if root in wanted:
                by_root.setdefault(root, []).append((article_id, canonical_id))
    return by_root


def _malformed_reason(
    root: int,
    members: Iterable[tuple[int, int | None]],
    tainted_roots: Set[int],
) -> str | None:
    """Why ``root``'s group cannot be decided on, or ``None`` if it is flat."""
    if root in tainted_roots:
        return REASON_CANONICAL_CHAIN
    root_is_canonical = any(
        article_id == root and canonical_id is None
        for article_id, canonical_id in members
    )
    if not root_is_canonical:
        return REASON_CANONICAL_MISSING
    return None


def _pack(groups: Sequence[tuple[int, ...]], chunk_ids: int) -> list[list[int]]:
    """Pack whole groups, in order, into batches of at most ``chunk_ids`` ids.

    A group is never split. One larger than ``chunk_ids`` is a batch alone.
    """
    batches: list[list[int]] = []
    current: list[int] = []
    for group in groups:
        if current and len(current) + len(group) > chunk_ids:
            batches.append(current)
            current = []
        current.extend(group)
    if current:
        batches.append(current)
    return batches
