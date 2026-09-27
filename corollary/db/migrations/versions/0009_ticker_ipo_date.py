"""ticker_ipo_date: one vendor-stated IPO date per ticker, cached across sessions

Phase 3 step 4, owner decision Q12 (2026-09-26). The tradeability filter reads
a ticker's IPO date only when its history starts inside the ADV window, and
the date does not change, so one row per ticker is kept for good: each such
ticker costs at most one Finnhub ``/stock/profile2`` call ever.

``ipo_date`` is ``NOT NULL`` because only a successfully parsed date is ever
written -- an empty answer or a failed request is never stored as a permanent
answer. ``fetched_at`` is UTC. ``ticker`` is CHECK-constrained non-blank
uppercase, as ``watch_symbol``'s is. See ``models.TickerIpoDate``.

Downgrade drops the table and the dates it holds; the next refresh asks the
vendor again for any ticker that still needs one.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticker_ipo_date",
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("ipo_date", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "ticker <> '' AND ticker = upper(ticker)",
            name="ck_ticker_ipo_date_ticker",
        ),
        sa.PrimaryKeyConstraint("ticker"),
    )


def downgrade() -> None:
    op.drop_table("ticker_ipo_date")
