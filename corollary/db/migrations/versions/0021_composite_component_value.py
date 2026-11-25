"""composite_component_value: one raw Fear & Greed component per NYSE session

**Revises 0020** (Q24a's ``off_watch_unverdicted``). Spec
``2026-11-17-phase-3-step-8-composite.md``, *Design -> Database* and unit U3;
numbered at merge per Q22's rule.

* ``composite_component_value`` -- primary key ``(date, component)``: one raw
  value per NYSE session per component (C1). ``component`` is CHECKed to C2's
  seven keys. ``raw_value`` is ``Money`` TEXT, signed (the 0002 shape), and
  quantized to ``1E-10`` by the store before it gets here. ``source`` names
  the feed or series and must be non-blank. ``members`` is the count behind
  the value (names counted for strength and breadth, contracts for put/call,
  else NULL) and is never negative. ``universe_as_of`` is the SPDR seed's
  report date, set exactly when the component is strength or breadth (C3).
  ``computed_at`` is the last write, UTC.

Nothing here enforces the five-session recheck window (C1): that is a rule
about the table's own newest row, which a CHECK cannot see, and it lives in
``corollary.data.macro.store.upsert_values``.

Downgrade drops the table. Every value is recomputable from vendor history
(Alpaca bars, FRED series) except put/call, which is accumulated forward
(C4); dropping the table loses that history.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
# tests/db/test_composite_component_value.py runs every CHECK against both
# this and ``create_all()``, so drift fails a test.
_MONEY_TEXT_WIDTH = 40
_COMPONENTS: tuple[str, ...] = (
    "momentum",
    "strength",
    "breadth",
    "put_call",
    "volatility",
    "safe_haven",
    "junk_bond",
)
_UNIVERSE_COMPONENTS: tuple[str, ...] = ("strength", "breadth")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


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


_UNIVERSE_RULE = (
    f"({_in_list('component', _UNIVERSE_COMPONENTS)} AND universe_as_of IS NOT NULL) "
    f"OR (NOT {_in_list('component', _UNIVERSE_COMPONENTS)} AND universe_as_of IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "composite_component_value",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("component", sa.String(length=16), nullable=False),
        sa.Column("raw_value", sa.String(length=_MONEY_TEXT_WIDTH), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("members", sa.Integer(), nullable=True),
        sa.Column("universe_as_of", sa.Date(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _in_list("component", _COMPONENTS),
            name="ck_composite_component_value_component",
        ),
        sa.CheckConstraint(
            _money_shape("raw_value"), name="ck_composite_component_value_raw_value"
        ),
        sa.CheckConstraint(
            "trim(source) <> ''", name="ck_composite_component_value_source"
        ),
        sa.CheckConstraint(
            "members IS NULL OR members >= 0",
            name="ck_composite_component_value_members",
        ),
        sa.CheckConstraint(
            _UNIVERSE_RULE, name="ck_composite_component_value_universe_as_of"
        ),
        sa.PrimaryKeyConstraint("date", "component"),
    )


def downgrade() -> None:
    op.drop_table("composite_component_value")
