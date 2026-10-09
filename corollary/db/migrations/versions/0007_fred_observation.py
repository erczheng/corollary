"""fred_observation: FRED series observations, first the risk-free rate's DGS3MO

Phase 3 step 3, decision 19. One row per ``(series_id, date)``, upserted on
every fetch. ``value`` is a signed plain decimal string (``Money``), ``NULL``
where FRED reported ``"."`` -- no observation on that date. ``fetched_at`` is
when the row was last written, UTC.

Not exported: ICE's notice on ``BAMLH0A0HYM2``, which later steps store here,
prohibits reproducing the raw series.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A copy, not an import, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
_MONEY_TEXT_WIDTH = 40


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


def upgrade() -> None:
    op.create_table(
        "fred_observation",
        sa.Column("series_id", sa.String(length=32), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("value", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _money_shape("value", nullable=True), name="ck_fred_observation_value"
        ),
        sa.PrimaryKeyConstraint("series_id", "date"),
    )


def downgrade() -> None:
    op.drop_table("fred_observation")
