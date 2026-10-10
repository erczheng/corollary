"""sentiment_label: one label per (article, ticker, source), both labelling tiers

Phase 3 step 5, unit 5.2 (decisions 4, 17 and 21). Design, *Database*:
``sentiment_label(id, article_id, ticker, source, tier, direction, reasoning,
rule_id, labeled_at)``, UNIQUE ``(article_id, ticker, source)``. ``direction``
in ``bullish | bearish | neutral``; ``tier`` in ``rules | vendor``; ``source``
in ``rules | massive``, and the source/tier pairing -- all CHECK-constrained.
``rule_id`` is non-null exactly when ``source = 'rules'``. No ``confidence``
column. An article with no row is ``Unclassified``: the table stores labels,
never their absence.

**The article FK has no ``ON DELETE`` action.** Decision 21: the prune
"deletes every canonical group of which no member carries a label", and
"labels, outcomes, audit runs and every labelled article's headline, URL,
publisher and time are kept indefinitely". So the prune never deletes a
labelled article, and SQLite's statement-end check refuses any delete that
tried -- a wrong retention predicate fails the prune loudly instead of
destroying labels. See ``models.SentimentLabelRow``.

**Provisional number (Q22).** Step 7 also writes a ``0013``; whichever merges
second renumbers onto the other. No merge revision.

The downgrade drops the table, discarding every label, and nothing rebuilds
them: labelling happens at ingest only, with no backfill ("No label backfill
(Q7)"), and Massive's insights are not persisted in any case.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sentiment_label",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("rule_id", sa.String(length=64), nullable=True),
        sa.Column("labeled_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "direction IN ('bullish', 'bearish', 'neutral')",
            name="ck_sentiment_label_direction",
        ),
        sa.CheckConstraint("tier IN ('rules', 'vendor')", name="ck_sentiment_label_tier"),
        sa.CheckConstraint(
            "source IN ('rules', 'massive')", name="ck_sentiment_label_source"
        ),
        sa.CheckConstraint(
            "(source = 'rules' AND tier = 'rules') OR (source = 'massive' AND tier = 'vendor')",
            name="ck_sentiment_label_source_tier",
        ),
        sa.CheckConstraint(
            "(source = 'rules' AND rule_id IS NOT NULL AND rule_id <> '') "
            "OR (source <> 'rules' AND rule_id IS NULL)",
            name="ck_sentiment_label_rule_id",
        ),
        sa.CheckConstraint(
            "ticker <> '' AND ticker = upper(ticker)", name="ck_sentiment_label_ticker"
        ),
        sa.ForeignKeyConstraint(["article_id"], ["news_article.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "article_id", "ticker", "source", name="uq_sentiment_label_article_ticker_source"
        ),
    )


def downgrade() -> None:
    op.drop_table("sentiment_label")
