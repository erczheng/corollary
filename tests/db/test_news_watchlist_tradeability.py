"""Phase 3 step 4's schema: ``news_article``, ``news_article_ticker``,
``watch_symbol``, ``ticker_tradeability``, the ``watchlist`` audit category
and the ``watchlist_changed`` routes (migration 0008).

Half of these run against ``create_all()`` (the models) and half against a
database built by ``alembic upgrade head`` (the migration). The CHECKs are
tested against **both**, because Alembic's autogenerate comparison does not
compare CHECK constraint text: ``test_upgrade_head_matches_the_models`` would
pass with a migration whose ``feed`` list had drifted from the enum.
"""

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, delete, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.data.news.article import FEED_VENDOR, NewsFeed
from corollary.db.models import (
    AUDIT_CATEGORIES,
    NEWS_FEEDS,
    NEWS_VENDORS,
    AuditLog,
    NewsArticle,
    NewsArticleTicker,
    NotificationRoute,
    TickerTradeability,
    WatchSymbol,
)
from corollary.db.seed import NOTIFICATION_ROUTE_DEFAULTS, seed
from corollary.db.session import create_db_engine, sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

T0 = datetime(2026, 9, 24, 14, 30, tzinfo=timezone.utc)


def _config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def migrated(db_path: Path) -> Iterator[Engine]:
    url = sqlite_url(db_path)
    command.upgrade(_config(url), "head")
    eng = create_db_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture(params=["models", "migration"])
def any_engine(request: pytest.FixtureRequest, db_path: Path) -> Iterator[Engine]:
    """The same assertions against ``create_all()`` and against ``upgrade head``."""
    url = sqlite_url(db_path)
    if request.param == "migration":
        command.upgrade(_config(url), "head")
        eng = create_db_engine(url)
    else:
        from corollary.db.models import Base

        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _article(
    vendor_id: str = "1",
    *,
    feed: str = "alpaca_news",
    vendor: str = "alpaca",
    canonical_id: int | None = None,
) -> NewsArticle:
    return NewsArticle(
        vendor=vendor,
        vendor_id=vendor_id,
        feed=feed,
        canonical_id=canonical_id,
        url=f"https://example.com/story/{vendor_id}",
        url_key=f"example.com/story/{vendor_id}",
        headline="Acme beats estimates",
        headline_key="acme beats estimates",
        summary="A summary.",
        publisher="Benzinga",
        published_at=T0,
        ingested_at=T0 + timedelta(minutes=1),
    )


# --------------------------------------------------------------- vocabularies


def test_feed_vocabulary_is_the_news_feed_enum() -> None:
    """One source of truth: the CHECK list is derived from ``NewsFeed``."""
    assert NEWS_FEEDS == tuple(feed.value for feed in NewsFeed)


def test_vendor_vocabulary_is_the_feed_vendor_map() -> None:
    assert set(NEWS_VENDORS) == set(FEED_VENDOR.values())
    assert len(NEWS_VENDORS) == len(set(NEWS_VENDORS))


def test_audit_categories_gain_watchlist_only() -> None:
    assert AUDIT_CATEGORIES == ("risk", "feed", "notification", "watchlist")


# --------------------------------------------------------------- news_article


def test_every_feed_with_its_vendor_is_accepted(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        for i, (feed, vendor) in enumerate(FEED_VENDOR.items()):
            sess.add(_article(str(i), feed=feed.value, vendor=vendor))
        sess.commit()
        assert sess.query(NewsArticle).count() == len(FEED_VENDOR)


def test_an_unknown_feed_is_refused(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_article(feed="benzinga_pro", vendor="alpaca"))
        with pytest.raises(IntegrityError, match="ck_news_article_feed"):
            sess.commit()


def test_an_unknown_vendor_is_refused(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_article(feed="alpaca_news", vendor="polygon"))
        with pytest.raises(IntegrityError, match="ck_news_article_vendor"):
            sess.commit()


def test_a_feed_claimed_by_the_wrong_vendor_is_refused(any_engine: Engine) -> None:
    """``NewsArticle`` (the record) refuses this at construction; so does the row."""
    with Session(any_engine) as sess:
        sess.add(_article(feed="finnhub_market", vendor="massive"))
        with pytest.raises(IntegrityError, match="ck_news_article_feed_vendor"):
            sess.commit()


def test_vendor_and_vendor_id_are_unique(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_article("dup"))
        sess.commit()
        sess.add(_article("dup"))
        with pytest.raises(IntegrityError):
            sess.commit()


def test_the_same_vendor_id_under_another_vendor_is_a_different_row(
    any_engine: Engine,
) -> None:
    with Session(any_engine) as sess:
        sess.add(_article("42", feed="alpaca_news", vendor="alpaca"))
        sess.add(_article("42", feed="finnhub_company", vendor="finnhub"))
        sess.commit()
        assert sess.query(NewsArticle).count() == 2


def test_timestamps_round_trip_as_aware_utc(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_article())
        sess.commit()
        sess.expire_all()
        row = sess.scalars(select(NewsArticle)).one()
        assert row.published_at == T0
        assert row.published_at.tzinfo is not None
        assert row.ingested_at.utcoffset() == timedelta(0)


def test_summary_may_be_nulled(any_engine: Engine) -> None:
    """Decision 21: ``summary`` is nulled after 90 days; the row stays."""
    with Session(any_engine) as sess:
        sess.add(_article())
        sess.commit()
        row = sess.scalars(select(NewsArticle)).one()
        row.summary = None
        sess.commit()
        sess.expire_all()
        assert sess.scalars(select(NewsArticle)).one().summary is None


def test_a_row_may_not_name_itself_canonical(any_engine: Engine) -> None:
    """NULL is how a row says it is canonical; a self-pointer is a second spelling."""
    with Session(any_engine) as sess:
        row = _article()
        sess.add(row)
        sess.commit()
        row.canonical_id = row.id
        with pytest.raises(IntegrityError, match="ck_news_article_canonical"):
            sess.commit()


def test_canonical_id_must_name_a_real_article(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_article(canonical_id=999))
        with pytest.raises(IntegrityError):
            sess.commit()


def test_a_canonical_row_cannot_be_deleted_out_from_under_its_duplicates(
    any_engine: Engine,
) -> None:
    """Decision 21: *a group goes as a unit, so no canonical_id dangles.*"""
    with Session(any_engine) as sess:
        canonical = _article("c")
        sess.add(canonical)
        sess.commit()
        sess.add(_article("d", feed="finnhub_company", vendor="finnhub",
                          canonical_id=canonical.id))
        sess.commit()
        canonical_id = canonical.id
        with pytest.raises(IntegrityError):
            sess.execute(delete(NewsArticle).where(NewsArticle.id == canonical_id))
            sess.commit()


def test_a_whole_group_deletes_in_one_statement(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        canonical = _article("c")
        sess.add(canonical)
        sess.commit()
        dup = _article("d", feed="finnhub_company", vendor="finnhub",
                       canonical_id=canonical.id)
        sess.add(dup)
        sess.commit()
        group = [canonical.id, dup.id]
        sess.execute(delete(NewsArticle).where(NewsArticle.id.in_(group)))
        sess.commit()
        assert sess.query(NewsArticle).count() == 0


def test_a_duplicate_alone_may_be_deleted(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        canonical = _article("c")
        sess.add(canonical)
        sess.commit()
        dup = _article("d", feed="finnhub_company", vendor="finnhub",
                       canonical_id=canonical.id)
        sess.add(dup)
        sess.commit()
        sess.execute(delete(NewsArticle).where(NewsArticle.id == dup.id))
        sess.commit()
        assert sess.query(NewsArticle).count() == 1


def test_news_article_indexes_exist(migrated: Engine) -> None:
    indexes = {ix["name"]: ix["column_names"]
               for ix in inspect(migrated).get_indexes("news_article")}
    assert indexes["ix_news_article_published_at"] == ["published_at"]
    assert indexes["ix_news_article_canonical_id"] == ["canonical_id"]
    assert indexes["ix_news_article_url_key"] == ["url_key"]
    assert indexes["ix_news_article_headline_key_published_at"] == [
        "headline_key",
        "published_at",
    ]


# --------------------------------------------------------- news_article_ticker


def test_tickers_cascade_with_their_article(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        article = _article()
        sess.add(article)
        sess.commit()
        sess.add_all([
            NewsArticleTicker(article_id=article.id, ticker="ACME"),
            NewsArticleTicker(article_id=article.id, ticker="MARKET"),
        ])
        sess.commit()
        sess.execute(delete(NewsArticle).where(NewsArticle.id == article.id))
        sess.commit()
        assert sess.query(NewsArticleTicker).count() == 0


def test_a_ticker_is_tagged_once_per_article(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        article = _article()
        sess.add(article)
        sess.commit()
        sess.add(NewsArticleTicker(article_id=article.id, ticker="ACME"))
        sess.commit()
        sess.add(NewsArticleTicker(article_id=article.id, ticker="ACME"))
        with pytest.raises(IntegrityError):
            sess.commit()


@pytest.mark.parametrize("bad", ["", "acme"])
def test_a_ticker_is_non_blank_uppercase(any_engine: Engine, bad: str) -> None:
    with Session(any_engine) as sess:
        article = _article()
        sess.add(article)
        sess.commit()
        sess.add(NewsArticleTicker(article_id=article.id, ticker=bad))
        with pytest.raises(IntegrityError, match="ck_news_article_ticker_ticker"):
            sess.commit()


def test_a_ticker_row_needs_a_real_article(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(NewsArticleTicker(article_id=12345, ticker="ACME"))
        with pytest.raises(IntegrityError):
            sess.commit()


def test_ticker_index_exists(migrated: Engine) -> None:
    indexes = {ix["name"]: ix["column_names"]
               for ix in inspect(migrated).get_indexes("news_article_ticker")}
    assert indexes["ix_news_article_ticker_ticker"] == ["ticker"]


# ----------------------------------------------------------------- watch_symbol


def test_two_active_watches_for_one_ticker_are_refused(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(WatchSymbol(ticker="ACME", added_at=T0))
        sess.commit()
        sess.add(WatchSymbol(ticker="ACME", added_at=T0 + timedelta(hours=1)))
        with pytest.raises(IntegrityError):
            sess.commit()


def test_a_removed_watch_keeps_its_row_and_a_re_add_is_a_new_row(
    any_engine: Engine,
) -> None:
    with Session(any_engine) as sess:
        first = WatchSymbol(ticker="ACME", added_at=T0)
        sess.add(first)
        sess.commit()
        first.removed_at = T0 + timedelta(days=1)
        sess.commit()
        sess.add(WatchSymbol(ticker="ACME", added_at=T0 + timedelta(days=2)))
        sess.commit()
        rows = sess.scalars(select(WatchSymbol).where(WatchSymbol.ticker == "ACME")).all()
        assert len(rows) == 2
        assert sum(1 for r in rows if r.removed_at is None) == 1


def test_two_removed_watches_for_one_ticker_are_history_not_a_conflict(
    any_engine: Engine,
) -> None:
    with Session(any_engine) as sess:
        sess.add(WatchSymbol(ticker="ACME", added_at=T0, removed_at=T0 + timedelta(1)))
        sess.add(WatchSymbol(ticker="ACME", added_at=T0 + timedelta(2),
                             removed_at=T0 + timedelta(3)))
        sess.commit()
        assert sess.query(WatchSymbol).count() == 2


def test_a_watch_cannot_be_removed_before_it_was_added(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(WatchSymbol(ticker="ACME", added_at=T0, removed_at=T0 - timedelta(1)))
        with pytest.raises(IntegrityError, match="ck_watch_symbol_removed_after_added"):
            sess.commit()


@pytest.mark.parametrize("bad", ["", "acme", "Acme"])
def test_a_watched_ticker_is_non_blank_uppercase(any_engine: Engine, bad: str) -> None:
    """The active-watch index compares case-sensitively, so without this CHECK
    ``acme`` and ``ACME`` could both be active watches of one symbol."""
    with Session(any_engine) as sess:
        sess.add(WatchSymbol(ticker=bad, added_at=T0))
        with pytest.raises(IntegrityError, match="ck_watch_symbol_ticker"):
            sess.commit()


def test_a_class_share_ticker_may_be_watched(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(WatchSymbol(ticker="BRK.B", added_at=T0))
        sess.commit()
        assert sess.scalars(select(WatchSymbol.ticker)).all() == ["BRK.B"]


def test_the_active_index_is_partial(migrated: Engine) -> None:
    with migrated.connect() as conn:
        sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE name = 'ux_watch_symbol_active_ticker'")
        ).scalar_one()
    assert "UNIQUE" in sql.upper()
    assert "removed_at IS NULL" in sql


# ---------------------------------------------------------- ticker_tradeability


def _tradeability(**overrides: object) -> TickerTradeability:
    fields: dict[str, object] = {
        "ticker": "ACME",
        "session_date": date(2026, 9, 24),
        "has_options": True,
        "standard_root": True,
        "avg_volume_20d": 2_500_000,
        "last_close": Decimal("12.34"),
        "sessions_available": 20,
        "passes": True,
        "failures": "",
        "checked_at": T0,
    }
    fields.update(overrides)
    return TickerTradeability(**fields)


def test_last_close_round_trips_as_an_exact_decimal(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_tradeability(last_close=Decimal("5.0100")))
        sess.commit()
        sess.expire_all()
        row = sess.scalars(select(TickerTradeability)).one()
        assert type(row.last_close) is Decimal
        assert row.last_close == Decimal("5.0100")


def test_every_result_the_filter_can_produce_fits_a_row(any_engine: Engine) -> None:
    """``standard_root``, ``avg_volume_20d`` and ``last_close`` are all ``None``
    in some ``TradeabilityResult``: not checked, under 20 sessions, no bars."""
    with Session(any_engine) as sess:
        sess.add(_tradeability(
            standard_root=None,
            avg_volume_20d=None,
            last_close=None,
            sessions_available=0,
            passes=False,
            failures="standard_root_unchecked,no_completed_session",
        ))
        sess.commit()
        sess.expire_all()
        row = sess.scalars(select(TickerTradeability)).one()
        assert row.standard_root is None
        assert row.avg_volume_20d is None
        assert row.last_close is None
        assert row.failures == "standard_root_unchecked,no_completed_session"


def test_one_row_per_ticker_per_session(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_tradeability())
        sess.commit()
        sess.add(_tradeability(checked_at=T0 + timedelta(hours=1)))
        with pytest.raises(IntegrityError):
            sess.commit()
        sess.rollback()
        sess.add(_tradeability(session_date=date(2026, 9, 25)))
        sess.commit()
        assert sess.query(TickerTradeability).count() == 2


@pytest.mark.parametrize(
    ("passes", "failures"),
    [(True, "low_volume"), (False, "")],
)
def test_passes_agrees_with_failures(
    any_engine: Engine, passes: bool, failures: str
) -> None:
    with Session(any_engine) as sess:
        sess.add(_tradeability(passes=passes, failures=failures))
        with pytest.raises(IntegrityError, match="ck_ticker_tradeability_passes"):
            sess.commit()


def test_a_failing_row_is_accepted(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(_tradeability(passes=False, failures="low_volume,low_close",
                               avg_volume_20d=10, last_close=Decimal("1.5")))
        sess.commit()


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("sessions_available", -1, "ck_ticker_tradeability_sessions_available"),
        ("avg_volume_20d", -5, "ck_ticker_tradeability_avg_volume_20d"),
    ],
)
def test_counts_are_non_negative_integers(
    any_engine: Engine, column: str, value: int, constraint: str
) -> None:
    with Session(any_engine) as sess:
        sess.add(_tradeability(**{column: value}))
        with pytest.raises(IntegrityError, match=constraint):
            sess.commit()


def test_a_non_numeric_close_is_refused_at_the_write_boundary(
    any_engine: Engine,
) -> None:
    with any_engine.begin() as conn, pytest.raises(
        IntegrityError, match="ck_ticker_tradeability_last_close"
    ):
        conn.execute(text(
            "INSERT INTO ticker_tradeability (ticker, session_date, has_options, "
            "standard_root, avg_volume_20d, last_close, sessions_available, passes, "
            "failures, checked_at) VALUES ('ACME', '2026-09-24', 1, 1, 10, 'NaN', "
            "20, 1, '', '2026-09-24 14:30:00.000000')"
        ))


# ------------------------------------------------------------------ audit_log


def test_a_watchlist_audit_row_is_accepted(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(AuditLog(at=T0, category="watchlist", field="ACME",
                          previous_value="", new_value="watched"))
        sess.commit()


def test_an_unknown_audit_category_is_refused(any_engine: Engine) -> None:
    with Session(any_engine) as sess:
        sess.add(AuditLog(at=T0, category="sentiment", field="x",
                          previous_value="", new_value=""))
        with pytest.raises(IntegrityError, match="ck_audit_log_category"):
            sess.commit()


def test_audit_log_keeps_its_index_through_the_rebuild(migrated: Engine) -> None:
    indexes = {ix["name"] for ix in inspect(migrated).get_indexes("audit_log")}
    assert "ix_audit_log_at" in indexes


def test_downgrade_restores_the_old_audit_check(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0007")
    eng = create_db_engine(url)
    try:
        with Session(eng) as sess:
            sess.add(AuditLog(at=T0, category="risk", field="x",
                              previous_value="1", new_value="2"))
            sess.commit()
            sess.add(AuditLog(at=T0, category="watchlist", field="ACME",
                              previous_value="", new_value="watched"))
            with pytest.raises(IntegrityError, match="ck_audit_log_category"):
                sess.commit()
        assert "ix_audit_log_at" in {
            ix["name"] for ix in inspect(eng).get_indexes("audit_log")
        }
    finally:
        eng.dispose()


def test_downgrade_refuses_while_a_watchlist_audit_row_exists(db_path: Path) -> None:
    """Restoring the old CHECK over a ``watchlist`` row cannot succeed, and must
    not succeed by deleting the audit record."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add(AuditLog(at=T0, category="watchlist", field="ACME",
                          previous_value="", new_value="watched"))
        sess.commit()
    eng.dispose()
    with pytest.raises(RuntimeError, match="refusing to downgrade 0008"):
        command.downgrade(cfg, "0007")

    # Refused before anything moved: still at 0008, no half-built rebuild
    # table left to break a retry, the audit row and the routes intact.
    eng = create_db_engine(url)
    try:
        tables = set(inspect(eng).get_table_names())
        assert "_alembic_tmp_audit_log" not in tables
        assert _NEW_TABLES <= tables
        with eng.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0008"
        with Session(eng) as sess:
            assert sess.query(AuditLog).filter_by(category="watchlist").count() == 1
        assert ("watchlist_changed", "discord") in _routes(eng)
    finally:
        eng.dispose()


# ------------------------------------------------------- 0008 up and down


_NEW_TABLES = {"news_article", "news_article_ticker", "watch_symbol",
               "ticker_tradeability"}


def _routes(eng: Engine) -> dict[tuple[str, str], bool]:
    with Session(eng) as sess:
        return {(r.event, r.channel): r.enabled
                for r in sess.query(NotificationRoute).all()}


def test_0008_downgrades_to_0007_and_back(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0007")
    eng = create_db_engine(url)
    try:
        assert not set(inspect(eng).get_table_names()) & _NEW_TABLES
        assert "fred_observation" in inspect(eng).get_table_names()
        assert not {e for e, _c in _routes(eng)} & {"watchlist_changed"}
    finally:
        eng.dispose()

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    try:
        assert _NEW_TABLES <= set(inspect(eng).get_table_names())
        routes = _routes(eng)
        assert routes[("watchlist_changed", "bell")] is False
        assert routes[("watchlist_changed", "discord")] is True
    finally:
        eng.dispose()


def test_seed_carries_the_watchlist_changed_rows() -> None:
    assert ("watchlist_changed", "bell", False) in NOTIFICATION_ROUTE_DEFAULTS
    assert ("watchlist_changed", "discord", True) in NOTIFICATION_ROUTE_DEFAULTS


def test_0008_leaves_a_watchlist_route_the_seed_already_wrote(db_path: Path) -> None:
    """``seed.py`` runs on every startup; an edited row must survive 0008."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "0007")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add(NotificationRoute(event="watchlist_changed", channel="bell",
                                   enabled=True))
        sess.commit()
    eng.dispose()

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    try:
        routes = _routes(eng)
        assert routes[("watchlist_changed", "bell")] is True  # the human's edit
        assert routes[("watchlist_changed", "discord")] is True  # filled in
    finally:
        eng.dispose()


def test_seed_leaves_a_watchlist_route_the_migration_wrote_and_a_human_edited(
    migrated: Engine,
) -> None:
    with Session(migrated) as sess:
        row = sess.get(NotificationRoute, ("watchlist_changed", "discord"))
        assert row is not None
        row.enabled = False
        sess.commit()
        assert seed(sess) == 0
        sess.commit()
    assert _routes(migrated)[("watchlist_changed", "discord")] is False


# ------------------------------------- create_all and upgrade head agree


def test_audit_rows_written_before_0008_survive_the_rebuild(db_path: Path) -> None:
    """0008 rebuilds ``audit_log`` to widen its CHECK. Every row already there
    -- ids, timestamps, values -- must come through byte for byte."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "0007")
    eng = create_db_engine(url)
    try:
        with Session(eng) as sess:
            sess.add_all(
                [
                    AuditLog(at=T0, category="risk", field="max_daily_loss_pct",
                             previous_value="20", new_value="15"),
                    AuditLog(at=T0 + timedelta(minutes=1), category="feed",
                             field="ALPACA_STOCK_FEED_HISTORICAL",
                             previous_value="sip", new_value="iex"),
                    AuditLog(at=T0 + timedelta(minutes=2), category="notification",
                             field="order_filled.bell",
                             previous_value="on", new_value="off"),
                ]
            )
            sess.commit()
        with eng.connect() as conn:
            before = conn.execute(
                text("SELECT * FROM audit_log ORDER BY id")
            ).all()
    finally:
        eng.dispose()
    assert len(before) == 3

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    try:
        with eng.connect() as conn:
            after = conn.execute(text("SELECT * FROM audit_log ORDER BY id")).all()
    finally:
        eng.dispose()
    assert after == before


def _top_level_items(body: str) -> list[str]:
    """Split a ``CREATE TABLE`` body at commas outside parentheses and quotes."""
    items: list[str] = []
    depth = 0
    quoted = False
    current: list[str] = []
    for char in body:
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
        if char == "," and depth == 0 and not quoted:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return items


def _normalised(sql: str) -> tuple[str, list[str]]:
    """``(head, sorted clauses)`` of one ``sqlite_master.sql``.

    Whitespace collapsed and identifier double-quotes dropped (a batch rebuild
    renames ``_alembic_tmp_audit_log`` and SQLite quotes the new name), and
    clauses sorted, because only their order legitimately differs -- the
    auditor saw FK and UNIQUE swap places in ``news_article``. Everything else,
    every column type and every CHECK body, must match exactly.
    """
    flat = " ".join(sql.replace('"', "").split())
    if not flat.upper().startswith("CREATE TABLE"):
        return flat, []
    head, _, rest = flat.partition("(")
    body = rest[: rest.rindex(")")]
    return head.strip(), sorted(item.strip() for item in _top_level_items(body))


_STEP_4_TABLES = ("audit_log", *sorted(_NEW_TABLES))


def _master(eng: Engine) -> dict[tuple[str, str], tuple[str, list[str]]]:
    names = ", ".join(f"'{name}'" for name in _STEP_4_TABLES)
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT type, name, sql FROM sqlite_master "
                f"WHERE tbl_name IN ({names}) AND sql IS NOT NULL"
            )
        ).all()
    return {(kind, name): _normalised(sql) for kind, name, sql in rows}


def test_the_normaliser_still_sees_check_bodies() -> None:
    """A normaliser that discarded CHECK text would make the next test vacuous."""
    head, clauses = _normalised(
        'CREATE TABLE "t" (\n  a TEXT,\n  CONSTRAINT ck CHECK (a IN (\'x\', \'y\'))\n)'
    )
    assert head == "CREATE TABLE t"
    assert clauses == ["CONSTRAINT ck CHECK (a IN ('x', 'y'))", "a TEXT"]


def test_create_all_and_upgrade_head_write_the_same_step_4_ddl(
    tmp_path: Path,
) -> None:
    """The models and 0008 each restate the CHECK text, so compare what SQLite
    actually stored for both -- tables and indexes, 0008's four plus the
    rebuilt ``audit_log``."""
    from corollary.db.models import Base

    models_url = sqlite_url(tmp_path / "models.db")
    migration_url = sqlite_url(tmp_path / "migration.db")
    command.upgrade(_config(migration_url), "head")
    models_eng = create_db_engine(models_url)
    migration_eng = create_db_engine(migration_url)
    try:
        Base.metadata.create_all(models_eng)
        from_models = _master(models_eng)
        from_migration = _master(migration_eng)
    finally:
        models_eng.dispose()
        migration_eng.dispose()

    assert {name for kind, name in from_models if kind == "table"} == set(
        _STEP_4_TABLES
    )
    assert from_models == from_migration
