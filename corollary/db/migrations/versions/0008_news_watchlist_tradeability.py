"""news_article, news_article_ticker, watch_symbol, ticker_tradeability; watchlist audit

Phase 3 step 4's whole schema, in one revision (decisions 3, 20 and 21):

* ``news_article`` -- one row per vendor article, UNIQUE ``(vendor,
  vendor_id)``. ``feed``, ``vendor`` and the feed/vendor pairing are
  CHECK-constrained. ``canonical_id`` is NULL on a canonical row and names
  the canonical row on a duplicate; the self-FK carries no ``ON DELETE``
  action, so SQLite's statement-end check refuses deleting a canonical row
  its duplicates still name, and admits deleting a whole group in one
  statement. ``url_key`` and ``headline_key`` are the normalised forms
  decision 3's dedupe looks up, indexed. See ``models.NewsArticle``.
* ``news_article_ticker`` -- ``(article_id, ticker)``, cascading with the
  article. ``MARKET`` is a ticker value.
* ``watch_symbol`` -- manual watches only; a removed watch keeps its row, and
  a partial unique index admits one *active* row per ticker. ``ticker`` is
  CHECK-constrained non-blank uppercase, so that index cannot be dodged by
  case.
* ``ticker_tradeability`` -- one cached verdict per ticker per session date.
  ``standard_root``, ``avg_volume_20d`` and ``last_close`` are nullable
  because the filter returns ``None`` for each; ``failures`` carries the
  comma-joined ``TradeabilityFailure`` values, ``''`` exactly when it passes.
* ``audit_log.category`` admits ``watchlist``. SQLite cannot alter a CHECK in
  place, so this is a batch rebuild with ``copy_from`` restating 0001's table
  -- SQLite does not reflect CHECK constraints reliably, and a rebuild from
  reflection could drop one silently. ``ix_audit_log_at`` is restated with it.
* ``notification_route`` gains ``watchlist_changed`` -- bell off, Discord on
  (decision 20) -- insert-if-missing, exactly as 0006: ``seed.py`` carries the
  same rows and may have written them first, and a human's edit stands.

**Downgrade refuses rather than deletes** if any ``watchlist`` audit row
exists: the old CHECK cannot hold it, and the only way to make the downgrade
succeed would be to destroy an audit record. The refusal is an explicit
check *before* anything moves, raising ``RuntimeError`` -- left to the
rebuild, the old CHECK would fail mid-copy and strand
``_alembic_tmp_audit_log``. Otherwise the new tables and the two route rows
are dropped, discarding what they hold.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day. The feed and vendor
# lists mirror ``corollary.data.news.article.NewsFeed`` and ``FEED_VENDOR`` as
# they stand today; tests/db/test_news_watchlist_tradeability.py inserts every
# enum value into a migrated database, so drift fails a test.
_MONEY_TEXT_WIDTH = 40

_FEED_VENDOR: tuple[tuple[str, str], ...] = (
    ("alpaca_news", "alpaca"),
    ("finnhub_company", "finnhub"),
    ("finnhub_market", "finnhub"),
    ("massive_news", "massive"),
)
_FEEDS: tuple[str, ...] = tuple(feed for feed, _vendor in _FEED_VENDOR)
_VENDORS: tuple[str, ...] = ("alpaca", "finnhub", "massive")

_AUDIT_CATEGORIES_BEFORE: tuple[str, ...] = ("risk", "feed", "notification")
_AUDIT_CATEGORIES_AFTER: tuple[str, ...] = (
    "risk",
    "feed",
    "notification",
    "watchlist",
)

_ROUTES: tuple[tuple[str, str, bool], ...] = (
    ("watchlist_changed", "bell", False),
    ("watchlist_changed", "discord", True),
)

_notification_route = sa.table(
    "notification_route",
    sa.column("event", sa.String(length=64)),
    sa.column("channel", sa.String(length=16)),
    sa.column("enabled", sa.Boolean()),
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _money_shape(column: str, *, nullable: bool = False) -> str:
    """A signed plain decimal string: ``-7``, ``0.00``, ``123.45``. See 0002."""
    body = (
        f"(CASE WHEN substr({column}, 1, 1) = '-' "
        f"THEN substr({column}, 2) ELSE {column} END)"
    )
    shape = (
        f"typeof({column}) = 'text' "
        f"AND length({column}) BETWEEN 1 AND {_MONEY_TEXT_WIDTH} "
        f"AND {body} GLOB '[0-9]*' "
        f"AND NOT {body} GLOB '*[^0-9.]*' "
        f"AND NOT {body} GLOB '*.' "
        f"AND length({body}) - length(replace({body}, '.', '')) <= 1"
    )
    if nullable:
        return f"{column} IS NULL OR ({shape})"
    return shape


def _feed_vendor_pairing() -> str:
    return " OR ".join(
        f"(feed = '{feed}' AND vendor = '{vendor}')" for feed, vendor in _FEED_VENDOR
    )


def _audit_log_table(categories: tuple[str, ...]) -> sa.Table:
    """``audit_log`` as 0001 created it, with ``categories`` in its CHECK."""
    return sa.Table(
        "audit_log",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("previous_value", sa.String(length=128), nullable=False),
        sa.Column("new_value", sa.String(length=128), nullable=False),
        sa.CheckConstraint(
            _in_list("category", categories), name="ck_audit_log_category"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_audit_log_at", "at"),
    )


def _set_audit_categories(
    before: tuple[str, ...], after: tuple[str, ...]
) -> None:
    with op.batch_alter_table(
        "audit_log", copy_from=_audit_log_table(before)
    ) as batch_op:
        batch_op.drop_constraint("ck_audit_log_category", type_="check")
        batch_op.create_check_constraint(
            "ck_audit_log_category", _in_list("category", after)
        )


def upgrade() -> None:
    op.create_table(
        "news_article",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor", sa.String(length=16), nullable=False),
        sa.Column("vendor_id", sa.String(length=128), nullable=False),
        sa.Column("feed", sa.String(length=32), nullable=False),
        sa.Column("canonical_id", sa.Integer(), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("url_key", sa.String(length=2048), nullable=False),
        sa.Column("headline", sa.String(length=1024), nullable=False),
        sa.Column("headline_key", sa.String(length=1024), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("publisher", sa.String(length=128), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_in_list("feed", _FEEDS), name="ck_news_article_feed"),
        sa.CheckConstraint(
            _in_list("vendor", _VENDORS), name="ck_news_article_vendor"
        ),
        sa.CheckConstraint(
            _feed_vendor_pairing(), name="ck_news_article_feed_vendor"
        ),
        sa.CheckConstraint(
            "canonical_id IS NULL OR canonical_id <> id",
            name="ck_news_article_canonical",
        ),
        # No ON DELETE action, deliberately: see the module docstring.
        sa.ForeignKeyConstraint(["canonical_id"], ["news_article.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vendor", "vendor_id", name="uq_news_article_vendor_id"),
    )
    op.create_index(
        "ix_news_article_published_at", "news_article", ["published_at"]
    )
    op.create_index(
        "ix_news_article_canonical_id", "news_article", ["canonical_id"]
    )
    op.create_index("ix_news_article_url_key", "news_article", ["url_key"])
    op.create_index(
        "ix_news_article_headline_key_published_at",
        "news_article",
        ["headline_key", "published_at"],
    )

    op.create_table(
        "news_article_ticker",
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "ticker <> '' AND ticker = upper(ticker)",
            name="ck_news_article_ticker_ticker",
        ),
        sa.ForeignKeyConstraint(
            ["article_id"], ["news_article.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("article_id", "ticker"),
    )
    op.create_index(
        "ix_news_article_ticker_ticker", "news_article_ticker", ["ticker"]
    )

    op.create_table(
        "watch_symbol",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        # Uppercase and non-blank: the partial unique index below compares
        # case-sensitively, so ``acme`` and ``ACME`` could otherwise both be
        # active. See ``models.WatchSymbol``.
        sa.CheckConstraint(
            "ticker <> '' AND ticker = upper(ticker)",
            name="ck_watch_symbol_ticker",
        ),
        sa.CheckConstraint(
            "removed_at IS NULL OR removed_at >= added_at",
            name="ck_watch_symbol_removed_after_added",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_watch_symbol_active_ticker",
        "watch_symbol",
        ["ticker"],
        unique=True,
        sqlite_where=sa.text("removed_at IS NULL"),
    )

    op.create_table(
        "ticker_tradeability",
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("has_options", sa.Boolean(), nullable=False),
        sa.Column("standard_root", sa.Boolean(), nullable=True),
        sa.Column("avg_volume_20d", sa.Integer(), nullable=True),
        sa.Column(
            "last_close", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True
        ),
        sa.Column("sessions_available", sa.Integer(), nullable=False),
        sa.Column("passes", sa.Boolean(), nullable=False),
        sa.Column("failures", sa.String(length=256), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "typeof(sessions_available) = 'integer' AND sessions_available >= 0",
            name="ck_ticker_tradeability_sessions_available",
        ),
        sa.CheckConstraint(
            "avg_volume_20d IS NULL OR "
            "(typeof(avg_volume_20d) = 'integer' AND avg_volume_20d >= 0)",
            name="ck_ticker_tradeability_avg_volume_20d",
        ),
        sa.CheckConstraint(
            _money_shape("last_close", nullable=True),
            name="ck_ticker_tradeability_last_close",
        ),
        sa.CheckConstraint(
            "(passes = 1 AND failures = '') OR (passes = 0 AND failures <> '')",
            name="ck_ticker_tradeability_passes",
        ),
        sa.PrimaryKeyConstraint("ticker", "session_date"),
    )

    _set_audit_categories(_AUDIT_CATEGORIES_BEFORE, _AUDIT_CATEGORIES_AFTER)

    bind = op.get_bind()
    existing = {
        (event, channel)
        for event, channel in bind.execute(
            sa.select(_notification_route.c.event, _notification_route.c.channel)
        )
    }
    missing = [
        {"event": event, "channel": channel, "enabled": enabled}
        for event, channel, enabled in _ROUTES
        if (event, channel) not in existing
    ]
    if missing:
        op.bulk_insert(_notification_route, missing)


def downgrade() -> None:
    # Refuse before touching anything. Alembic's SQLite impl is not
    # transactional-DDL, so work done before a failure stays done -- and
    # letting the rebuild's INSERT hit the old CHECK instead would fail with a
    # bare IntegrityError *and* leave ``_alembic_tmp_audit_log`` behind, which
    # then breaks every retry with "table already exists".
    bind = op.get_bind()
    blocking = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM audit_log WHERE NOT "
            + _in_list("category", _AUDIT_CATEGORIES_BEFORE)
        )
    ).scalar_one()
    if blocking:
        raise RuntimeError(
            f"refusing to downgrade 0008: audit_log holds {blocking} row(s) in a "
            f"category 0007 does not admit (watchlist). Deleting audit records "
            f"to make a downgrade fit is not this migration's call."
        )

    _set_audit_categories(_AUDIT_CATEGORIES_AFTER, _AUDIT_CATEGORIES_BEFORE)

    events = sorted({event for event, _channel, _enabled in _ROUTES})
    op.execute(
        _notification_route.delete().where(_notification_route.c.event.in_(events))
    )

    op.drop_table("ticker_tradeability")
    op.drop_index("ux_watch_symbol_active_ticker", table_name="watch_symbol")
    op.drop_table("watch_symbol")
    op.drop_index("ix_news_article_ticker_ticker", table_name="news_article_ticker")
    op.drop_table("news_article_ticker")
    op.drop_index(
        "ix_news_article_headline_key_published_at", table_name="news_article"
    )
    op.drop_index("ix_news_article_url_key", table_name="news_article")
    op.drop_index("ix_news_article_canonical_id", table_name="news_article")
    op.drop_index("ix_news_article_published_at", table_name="news_article")
    op.drop_table("news_article")
