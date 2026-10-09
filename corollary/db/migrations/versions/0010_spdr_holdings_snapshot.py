"""spdr_holdings_snapshot, spdr_holding: the SPDR sector seed, built from SEC N-PORT

Phase 3 step 4, owner decision: decision 6's hand-downloaded State Street seed
is replaced by the eleven Select Sector SPDRs' quarterly NPORT-P filings, each
holding's CUSIP resolved to a ticker by Alpaca. The runtime must not write into
the source tree, so the seed lives here.

* ``spdr_holdings_snapshot`` -- one row per build attempt: the quarter's
  ``report_date``, the latest ``filed_date``, ``built_at`` (UTC), ``status``
  (``accepted`` / ``refused``), and for a refusal the ``rule`` and ``reason``
  (CHECK: an accepted row carries neither, a refused one both).
  ``skipped_lines`` counts equity lines logged and skipped.
* ``spdr_holding`` -- the resolved common-equity holdings of an **accepted**
  snapshot, keyed ``(snapshot_id, etf, symbol)``, with the fund's
  ``series_id`` and ``accession``, the ``cusip`` (NULL for a line N-PORT
  filed with only an ISIN) and ``isin`` (CHECK: at least one), the filing's ``name`` and
  ``weight`` (``pctVal``, Money TEXT, a non-negative plain decimal). Rows go
  with their snapshot (``ON DELETE CASCADE``).

Downgrade drops both tables and every snapshot they hold; the loader then
answers "no snapshot" and the next quarterly run builds one again.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A copy, not an import, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
_MONEY_TEXT_WIDTH = 40


def _money_shape(column: str) -> str:
    """A signed plain decimal string: ``-7``, ``0.00``, ``123.45``. See 0002."""
    body = (
        f"(CASE WHEN substr({column}, 1, 1) = '-' "
        f"THEN substr({column}, 2) ELSE {column} END)"
    )
    return (
        f"typeof({column}) = 'text' "
        f"AND length({column}) BETWEEN 1 AND {_MONEY_TEXT_WIDTH} "
        f"AND {body} GLOB '[0-9]*' "
        f"AND NOT {body} GLOB '*[^0-9.]*' "
        f"AND NOT {body} GLOB '*.' "
        f"AND length({body}) - length(replace({body}, '.', '')) <= 1"
    )


def upgrade() -> None:
    op.create_table(
        "spdr_holdings_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("filed_date", sa.Date(), nullable=False),
        sa.Column("built_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rule", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("skipped_lines", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('accepted', 'refused')",
            name="ck_spdr_holdings_snapshot_status",
        ),
        sa.CheckConstraint(
            "(status = 'accepted' AND rule IS NULL AND reason IS NULL) OR "
            "(status = 'refused' AND rule IS NOT NULL AND rule <> '' "
            "AND reason IS NOT NULL AND reason <> '')",
            name="ck_spdr_holdings_snapshot_verdict",
        ),
        sa.CheckConstraint(
            "typeof(skipped_lines) = 'integer' AND skipped_lines >= 0",
            name="ck_spdr_holdings_snapshot_skipped_lines",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_spdr_holdings_snapshot_status_report_date",
        "spdr_holdings_snapshot",
        ["status", "report_date"],
    )
    op.create_table(
        "spdr_holding",
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("etf", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("sector", sa.String(length=64), nullable=False),
        sa.Column("series_id", sa.String(length=10), nullable=False),
        sa.Column("accession", sa.String(length=20), nullable=False),
        sa.Column("cusip", sa.String(length=9), nullable=True),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("weight", sa.String(length=_MONEY_TEXT_WIDTH), nullable=False),
        sa.CheckConstraint("etf <> '' AND etf = upper(etf)", name="ck_spdr_holding_etf"),
        sa.CheckConstraint(
            "symbol <> '' AND symbol = upper(symbol)", name="ck_spdr_holding_symbol"
        ),
        sa.CheckConstraint(
            "cusip IS NULL OR (length(cusip) = 9 AND cusip = upper(cusip))",
            name="ck_spdr_holding_cusip",
        ),
        sa.CheckConstraint(
            "isin IS NULL OR (length(isin) = 12 AND isin = upper(isin))",
            name="ck_spdr_holding_isin",
        ),
        sa.CheckConstraint(
            "cusip IS NOT NULL OR isin IS NOT NULL", name="ck_spdr_holding_identifier"
        ),
        sa.CheckConstraint(
            f"({_money_shape('weight')}) AND substr(weight, 1, 1) <> '-'",
            name="ck_spdr_holding_weight",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["spdr_holdings_snapshot.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "etf", "symbol"),
    )


def downgrade() -> None:
    op.drop_table("spdr_holding")
    op.drop_index(
        "ix_spdr_holdings_snapshot_status_report_date", table_name="spdr_holdings_snapshot"
    )
    op.drop_table("spdr_holdings_snapshot")
