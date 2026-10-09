"""Decision 21's retention prune (``corollary.data.news.retention``).

The spec's four bullets (*Testing* -> *Retention*): a labelled article is never
deleted, and neither is any member of its canonical group; an unlabelled group
older than 90 days is deleted as a unit; ``summary`` is nulled, not the row;
re-running the prune is a no-op. Plus the details this unit decided: the age is
``published_at``'s, the cutoff is exclusive (exactly 90 days old is *not*
older than 90 days), a group with one recent member stays whole, a chain is
skipped and logged without stopping the rest, and chunking never splits a
group.

Labels do not exist until step 5, so every test drives the "is labelled"
predicate by injection.

Every test runs against both a ``create_all()`` database and an
``alembic upgrade head`` one: the group-as-a-unit delete leans on the self-FK
having **no** delete action, and that is the migration's text as much as the
model's.
"""

import logging
from collections.abc import Iterator, Set
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from corollary.data.news.retention import (
    RETENTION,
    PruneResult,
    no_labels,
    prune,
)
from corollary.db.models import Base, NewsArticle, NewsArticleTicker
from corollary.db.session import create_db_engine, sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

NOW = datetime(2026, 9, 24, 7, 0, tzinfo=timezone.utc)  # 03:00 ET
CUTOFF = NOW - timedelta(days=90)
OLD = CUTOFF - timedelta(days=1)
RECENT = CUTOFF + timedelta(days=1)


@pytest.fixture(params=["models", "migration"])
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    url = sqlite_url(tmp_path / "corollary.db")
    if request.param == "migration":
        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        eng = create_db_engine(url)
    else:
        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        yield sess


_counter = iter(range(1, 1_000_000))


def add(
    session: Session,
    published_at: datetime,
    *,
    canonical_id: int | None = None,
    tickers: tuple[str, ...] = (),
    summary: str | None = "A summary.",
) -> int:
    """Insert one article (and its ticker rows); return its id."""
    n = next(_counter)
    article = NewsArticle(
        vendor="alpaca",
        vendor_id=f"v{n}",
        feed="alpaca_news",
        canonical_id=canonical_id,
        url=f"https://example.com/story/{n}",
        url_key=f"example.com/story/{n}",
        headline=f"Headline {n}",
        headline_key=f"headline {n}",
        summary=summary,
        publisher="Benzinga",
        published_at=published_at,
        ingested_at=published_at + timedelta(minutes=1),
    )
    session.add(article)
    session.flush()
    for ticker in tickers:
        session.add(NewsArticleTicker(article_id=article.id, ticker=ticker))
    session.flush()
    return article.id


def labels(*ids: int) -> "LabelSet":
    return LabelSet(frozenset(ids))


class LabelSet:
    """An injected ``labelled_article_ids`` predicate over a fixed id set."""

    def __init__(self, ids: frozenset[int]) -> None:
        self.ids = ids

    def __call__(self, session: Session) -> Set[int]:
        return self.ids


def ids_in_table(session: Session) -> set[int]:
    return set(session.scalars(select(NewsArticle.id)))


def summary_of(session: Session, article_id: int) -> str | None:
    return session.scalar(
        select(NewsArticle.summary).where(NewsArticle.id == article_id)
    )


def run(session: Session, **kwargs: object) -> PruneResult:
    kwargs.setdefault("labelled_article_ids", no_labels)
    result = prune(session, now=NOW, **kwargs)  # type: ignore[arg-type]
    session.commit()
    session.expire_all()
    return result


# --- the four spec bullets ------------------------------------------------------


def test_a_labelled_article_is_never_deleted_nor_any_member_of_its_group(
    session: Session,
) -> None:
    canonical = add(session, OLD - timedelta(days=100))
    labelled_dup = add(session, OLD, canonical_id=canonical)
    other_dup = add(session, OLD, canonical_id=canonical)
    session.commit()

    result = run(session, labelled_article_ids=labels(labelled_dup))

    assert ids_in_table(session) == {canonical, labelled_dup, other_dup}
    assert result.groups_deleted == 0
    assert result.articles_deleted == 0
    assert result.groups_kept_labelled == 1
    # Kept, but still past 90 days: the summary goes, headline/url/etc. stay.
    for article_id in (canonical, labelled_dup, other_dup):
        assert summary_of(session, article_id) is None
    kept = session.get(NewsArticle, labelled_dup)
    assert kept is not None
    assert kept.headline and kept.url and kept.publisher and kept.published_at


def test_a_labelled_canonical_keeps_its_unlabelled_duplicates(session: Session) -> None:
    canonical = add(session, OLD)
    dup = add(session, OLD, canonical_id=canonical)
    session.commit()

    run(session, labelled_article_ids=labels(canonical))

    assert ids_in_table(session) == {canonical, dup}


def test_an_unlabelled_group_older_than_90_days_is_deleted_as_a_unit(
    session: Session,
) -> None:
    canonical = add(session, OLD)
    dup_a = add(session, OLD - timedelta(hours=3), canonical_id=canonical)
    dup_b = add(session, OLD + timedelta(hours=3), canonical_id=canonical)
    survivor = add(session, RECENT)
    session.commit()

    result = run(session)

    assert ids_in_table(session) == {survivor}
    assert result.groups_deleted == 1
    assert result.articles_deleted == 3
    assert not ids_in_table(session) & {canonical, dup_a, dup_b}
    assert result.groups_skipped == ()


def test_summary_is_nulled_not_the_row(session: Session) -> None:
    old_labelled = add(session, OLD)
    recent = add(session, RECENT)
    session.commit()

    result = run(session, labelled_article_ids=labels(old_labelled))

    assert ids_in_table(session) == {old_labelled, recent}
    assert summary_of(session, old_labelled) is None
    assert summary_of(session, recent) == "A summary."
    assert result.summaries_nulled == 1


def test_re_running_the_prune_is_a_no_op(session: Session) -> None:
    kept = add(session, OLD)
    gone = add(session, OLD)
    add(session, OLD, canonical_id=gone)
    add(session, RECENT)
    session.commit()
    first = run(session, labelled_article_ids=labels(kept))
    assert first.groups_deleted == 1
    before = ids_in_table(session)

    second = run(session, labelled_article_ids=labels(kept))

    assert ids_in_table(session) == before
    assert second.summaries_nulled == 0
    assert second.groups_deleted == 0
    assert second.articles_deleted == 0
    assert second.groups_skipped == ()


# --- the details this unit decided ------------------------------------------------


def test_a_group_with_one_recent_member_stays_whole(session: Session) -> None:
    canonical = add(session, OLD - timedelta(days=30))
    old_dup = add(session, OLD, canonical_id=canonical)
    recent_dup = add(session, RECENT, canonical_id=canonical)
    session.commit()

    result = run(session)

    assert ids_in_table(session) == {canonical, old_dup, recent_dup}
    assert result.groups_deleted == 0
    # The old members still lose their summaries: that rule is per article.
    assert summary_of(session, canonical) is None
    assert summary_of(session, old_dup) is None
    assert summary_of(session, recent_dup) == "A summary."


def test_a_recent_canonical_with_an_old_duplicate_stays_whole(session: Session) -> None:
    # Published order and canonical order need not agree: ingest makes the
    # first row *it saw* canonical.
    canonical = add(session, RECENT)
    old_dup = add(session, OLD, canonical_id=canonical)
    session.commit()

    run(session)

    assert ids_in_table(session) == {canonical, old_dup}


def test_age_is_published_at_not_ingested_at(session: Session) -> None:
    # Published long ago, ingested yesterday (a late backfill): it is old.
    article = NewsArticle(
        vendor="alpaca",
        vendor_id="late",
        feed="alpaca_news",
        canonical_id=None,
        url="https://example.com/late",
        url_key="example.com/late",
        headline="Late",
        headline_key="late",
        summary="s",
        publisher=None,
        published_at=OLD,
        ingested_at=NOW - timedelta(days=1),
    )
    session.add(article)
    session.commit()

    result = run(session)

    assert ids_in_table(session) == set()
    assert result.articles_deleted == 1


def test_the_cutoff_is_exclusive_exactly_90_days_old_is_kept(session: Session) -> None:
    at_cutoff = add(session, CUTOFF)
    just_past = add(session, CUTOFF - timedelta(microseconds=1))
    session.commit()

    result = run(session)

    assert ids_in_table(session) == {at_cutoff}
    assert summary_of(session, at_cutoff) == "A summary."
    assert just_past not in ids_in_table(session)
    assert result.cutoff == CUTOFF
    assert RETENTION == timedelta(days=90)


def test_the_cutoff_is_exclusive_for_the_summary_too(session: Session) -> None:
    at_cutoff = add(session, CUTOFF)
    just_past = add(session, CUTOFF - timedelta(microseconds=1))
    session.commit()

    run(session, labelled_article_ids=labels(at_cutoff, just_past))

    assert summary_of(session, at_cutoff) == "A summary."
    assert summary_of(session, just_past) is None


def test_tickers_cascade_away_with_deleted_articles(session: Session) -> None:
    gone = add(session, OLD, tickers=("AAPL", "MSFT"))
    add(session, OLD, canonical_id=gone, tickers=("AAPL",))
    kept = add(session, OLD, tickers=("NVDA",))
    session.commit()

    run(session, labelled_article_ids=labels(kept))

    rows = session.execute(
        select(NewsArticleTicker.article_id, NewsArticleTicker.ticker)
    ).all()
    assert [tuple(r) for r in rows] == [(kept, "NVDA")]


def test_a_chain_is_skipped_and_logged_and_the_rest_still_pruned(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Ingest promises no chains. If one appears anyway, the groups it touches
    # are left alone -- deleting either could hit the self-FK -- and the prune
    # carries on with everything else.
    c = add(session, OLD)
    b = add(session, OLD, canonical_id=c)
    a = add(session, OLD, canonical_id=b)
    clean = add(session, OLD)
    clean_dup = add(session, OLD, canonical_id=clean)
    session.commit()

    with caplog.at_level(logging.WARNING, logger="corollary.data.news.retention"):
        result = run(session)

    assert ids_in_table(session) == {a, b, c}
    assert clean not in ids_in_table(session) and clean_dup not in ids_in_table(session)
    assert result.groups_deleted == 1
    assert result.articles_deleted == 2
    assert result.groups_skipped
    assert all(s.reason == "canonical_chain" for s in result.groups_skipped)
    skipped_members = {m for s in result.groups_skipped for m in s.member_ids}
    assert skipped_members == {a, b, c}
    records = [
        r for r in caplog.records if getattr(r, "event", None) == "news_retention_group_skipped"
    ]
    assert records
    assert all(getattr(r, "reason", None) == "canonical_chain" for r in records)


def test_chunking_never_splits_a_group(session: Session) -> None:
    # Groups of 3, 1, 4, 2 with a two-id budget: a naive id batcher would cut
    # the 3- and 4-member groups across statements, and the statement holding
    # a canonical row without all its duplicates fails the self-FK.
    expected_deleted = 0
    for size in (3, 1, 4, 2):
        canonical = add(session, OLD)
        for _ in range(size - 1):
            add(session, OLD, canonical_id=canonical)
        expected_deleted += size
    session.commit()

    result = run(session, chunk_ids=2)

    assert ids_in_table(session) == set()
    assert result.groups_deleted == 4
    assert result.articles_deleted == expected_deleted
    # 3 alone (over budget), 1, 4 alone, 2 -- never a statement holding a
    # partial group; the 1 cannot join the 4 or the 2 without exceeding 2.
    assert result.delete_statements == 4


def test_small_groups_share_a_statement_up_to_the_budget(session: Session) -> None:
    for _ in range(4):
        canonical = add(session, OLD)
        add(session, OLD, canonical_id=canonical)
    session.commit()

    result = run(session, chunk_ids=4)

    assert result.groups_deleted == 4
    assert result.articles_deleted == 8
    assert result.delete_statements == 2


def test_the_default_predicate_is_no_labels(session: Session) -> None:
    assert no_labels(session) == frozenset()
    add(session, OLD)
    session.commit()

    result = run(session)

    assert result.articles_deleted == 1
    assert result.groups_kept_labelled == 0


def test_the_prune_does_not_commit(session: Session) -> None:
    add(session, OLD)
    session.commit()

    prune(session, labelled_article_ids=no_labels, now=NOW)
    session.rollback()
    session.expire_all()

    assert len(ids_in_table(session)) == 1


def test_an_empty_table_prunes_nothing(session: Session) -> None:
    result = run(session)
    assert result == PruneResult(
        cutoff=CUTOFF,
        summaries_nulled=0,
        groups_deleted=0,
        articles_deleted=0,
        groups_kept_labelled=0,
        groups_skipped=(),
        delete_statements=0,
    )


def test_a_naive_now_is_refused(session: Session) -> None:
    with pytest.raises(ValueError):
        prune(session, labelled_article_ids=no_labels, now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("retention", [timedelta(0), timedelta(days=-1)])
def test_a_non_positive_retention_is_refused(
    session: Session, retention: timedelta
) -> None:
    with pytest.raises(ValueError):
        prune(session, labelled_article_ids=no_labels, now=NOW, retention=retention)


def test_a_non_positive_chunk_is_refused(session: Session) -> None:
    with pytest.raises(ValueError):
        prune(session, labelled_article_ids=no_labels, now=NOW, chunk_ids=0)


def test_an_eastern_now_means_the_same_instant(session: Session) -> None:
    from zoneinfo import ZoneInfo

    add(session, CUTOFF)
    session.commit()

    result = prune(session, labelled_article_ids=no_labels, now=NOW.astimezone(ZoneInfo("America/New_York")))
    session.commit()

    assert result.cutoff == CUTOFF
    assert result.cutoff.tzinfo == timezone.utc
    assert result.articles_deleted == 0
    assert session.scalar(select(func.count()).select_from(NewsArticle)) == 1


def test_a_cycle_is_skipped_as_a_chain(session: Session) -> None:
    # x -> y -> x: neither row is canonical. The schema cannot refuse it (no
    # subquery in a CHECK), so the prune must not trip over it.
    x = add(session, OLD)
    y = add(session, OLD, canonical_id=x)
    session.get_one(NewsArticle, x).canonical_id = y
    clean = add(session, OLD)
    session.commit()

    result = run(session)

    assert ids_in_table(session) == {x, y}
    assert clean not in ids_in_table(session)
    assert result.groups_deleted == 1
    assert {s.reason for s in result.groups_skipped} == {"canonical_chain"}
    assert {m for s in result.groups_skipped for m in s.member_ids} == {x, y}


def test_summaries_nulled_counts_every_row_it_nulled_even_one_then_deleted(
    session: Session,
) -> None:
    # The summary UPDATE runs first (it is what takes the write lock), so a
    # row later deleted was nulled first and is counted. Stated here so the
    # number is never read as "rows kept with no summary".
    kept = add(session, OLD)
    add(session, OLD)
    add(session, OLD, summary=None)  # already null: not counted
    session.commit()

    result = run(session, labelled_article_ids=labels(kept))

    assert result.summaries_nulled == 2
    assert result.articles_deleted == 2


def test_the_write_lock_is_held_while_labels_and_groups_are_read(
    session: Session, engine: Engine
) -> None:
    # The race this closes: ingest adds a duplicate to a group between the
    # prune reading it and deleting it, and the one-statement group delete
    # then fails the self-FK (or, worse, a labeller labels a row the prune
    # already decided to delete). The prune takes SQLite's write lock with its
    # first statement, before it reads labels, so a second writer cannot land
    # anything in between. Proved from inside the injected predicate.
    canonical = add(session, OLD)
    session.commit()
    other = create_engine(engine.url, connect_args={"timeout": 0.1})
    attempts: list[str] = []

    def labelled_while_a_second_writer_tries(sess: Session) -> Set[int]:
        with pytest.raises(OperationalError, match="locked"):
            with other.begin() as conn:
                conn.execute(
                    NewsArticle.__table__.insert().values(
                        vendor="finnhub",
                        vendor_id="late-dup",
                        feed="finnhub_company",
                        canonical_id=canonical,
                        url="https://example.com/late-dup",
                        url_key="example.com/late-dup",
                        headline="Late dup",
                        headline_key="late dup",
                        summary=None,
                        publisher=None,
                        published_at=RECENT,
                        ingested_at=NOW,
                    )
                )
        attempts.append("refused")
        return frozenset()

    try:
        result = run(session, labelled_article_ids=labelled_while_a_second_writer_tries)
    finally:
        other.dispose()

    assert attempts == ["refused"]
    assert result.articles_deleted == 1
    assert ids_in_table(session) == set()


def test_the_label_predicate_has_no_default() -> None:
    """Step 5 must replace the caller's ``no_labels``; a default would let the
    nightly job keep deleting labelled groups after labels exist."""
    import inspect

    parameter = inspect.signature(prune).parameters["labelled_article_ids"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_write_lock_holds_when_no_summary_needed_nulling(
    session: Session, engine: Engine
) -> None:
    # The opening UPDATE matches nothing here (the old article never had a
    # summary); the lock must still be taken before labels are read.
    add(session, OLD, summary=None)
    session.commit()
    other = create_engine(engine.url, connect_args={"timeout": 0.1})
    attempts: list[str] = []

    def labelled_while_a_second_writer_tries(sess: Session) -> Set[int]:
        with pytest.raises(OperationalError, match="locked"):
            with other.begin() as conn:
                conn.execute(
                    NewsArticle.__table__.update().values(headline="changed")
                )
        attempts.append("refused")
        return frozenset()

    try:
        result = run(session, labelled_article_ids=labelled_while_a_second_writer_tries)
    finally:
        other.dispose()

    assert attempts == ["refused"]
    assert result.summaries_nulled == 0
    assert result.articles_deleted == 1
