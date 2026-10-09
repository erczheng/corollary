"""isin_ticker: the OpenFIGI ISIN -> ticker cache for the SPDR seed's ISIN-only lines

Phase 3 spec Q17 (option A, unit U3a). The 29 N-PORT equity lines the
2026-06-30 SPDR filings identify by ISIN alone are resolved through OpenFIGI
by :class:`corollary.data.seeds.isin.OpenFigiIsinResolver`, and every
**accepted** answer is cached here so the weekly ``spdr_holdings`` job asks
OpenFIGI only about ISINs it has not already resolved.

What the table holds, and what it never holds:

* **Only accepted answers.** An ISIN the acceptance rule refused (a warning,
  an error, no US composite equity listing, more than one ticker, a ticker
  the broker does not list, or no directory to check against) is never
  written, so it is asked again on the next run.
* **A cached ticker is not trusted on its own.** It is re-checked against the
  day's asset directory on every use; a ticker the broker no longer lists
  makes the line unresolved for that run and its row is deleted, so the next
  run asks OpenFIGI afresh.
* **No money.** ``resolved_at`` is aware UTC (``UtcDateTime``), stored the
  way every other timestamp column is.

``source`` is CHECK-constrained to ``'openfigi'``: one source exists, and a
second would be a deliberate change that widens the CHECK with it.

The downgrade drops the table, discarding the cache; the next run rebuilds
it from OpenFIGI.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "isin_ticker",
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("composite_figi", sa.String(length=12), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(isin) = 12 AND isin = upper(isin)", name="ck_isin_ticker_isin"
        ),
        sa.CheckConstraint(
            "ticker <> '' AND ticker = upper(ticker)", name="ck_isin_ticker_ticker"
        ),
        sa.CheckConstraint(
            "composite_figi IS NULL OR length(composite_figi) = 12",
            name="ck_isin_ticker_composite_figi",
        ),
        sa.CheckConstraint("source IN ('openfigi')", name="ck_isin_ticker_source"),
        sa.PrimaryKeyConstraint("isin"),
    )


def downgrade() -> None:
    op.drop_table("isin_ticker")
