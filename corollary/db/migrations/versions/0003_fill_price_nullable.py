"""fill.price becomes nullable: a settlement nobody can derive has no price

One column, one nullability change, and the reason is the same one open
question 4 answered for the P&L beside it.

An ``OPEXC``/``OPASN`` carries no price of its own — ``net_amount`` is
``"0"`` — so the matcher synthesises one: the contract's **intrinsic value**,
``max(close − strike, 0)`` against the strike parsed out of the OCC symbol.
That is right on a standard contract and it reconciles against cash. On an
**adjusted** contract it is not, and the ledger already knows it is not: a
modified root (``AAPL1``, ``GME1``) means the deliverable is no longer
``multiplier`` shares of the underlying, so the strike is no longer the price
at which those shares change hands. ``engine/ledger.py`` refuses the realized
trade for exactly that reason — ``RejectionRule.UNVERIFIED_DELIVERABLE`` —
and the Activity page counts the close in ``not_booked`` rather than reporting
a P&L nobody can stand behind.

The price was surviving that refusal. With the column NOT NULL the intrinsic
was written anyway and rendered in the Activity table's Price cell, which is a
column of prices actually *paid* — an estimate derived from the very premise
the refusal rejects, on a row already flagged as unaccounted for. Deriving a
ratio from ``deliverables``, ``size`` or an assumed split factor was explicitly
forbidden because each is a guess about money; a price derived from the
invalidated strike is the same guess one column over.

So NULL becomes representable, and it means **the ledger could not establish
this price** — never zero, which is a price and, on a long, a total loss.
Nothing else may write it: a fill always carries what was paid, an ``OPEXP``
closes at a real zero, and an option event with no settlement price is refused
before it becomes a row at all.

**Downgrade refuses rather than invents.** Restoring NOT NULL over a table
that holds one of these rows fails, and that is the correct outcome: the only
values available to fill it with are the estimate this revision exists to
remove and a zero that would book a total loss.

SQLite cannot ``ALTER COLUMN``, so this is a batch (table-rebuild) migration.
``copy_from`` carries the whole 0002 definition because SQLite does not
reflect CHECK constraints: without it the rebuild would silently drop all five
of ``fill``'s constraints, which is a schema change nobody asked for and no
test would see.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason 0001 and 0002 both state: a migration is
# a frozen record of the schema on the day, and one that imported live helpers
# would rewrite its own history the moment a helper changed.

_MONEY_TEXT_WIDTH = 40


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _in_list_or_null(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IS NULL OR {_in_list(column, values)}"


def _money_shape(column: str, *, nullable: bool = False) -> str:
    """A signed plain decimal string: ``-7``, ``0.00``, ``123.45``."""
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


def _positive_count(column: str) -> str:
    return f"typeof({column}) = 'integer' AND {column} > 0"


_ACCOUNT_MODES = ("paper", "cash")
_FILL_SIDES = ("buy", "sell", "sell_short")
_POSITION_INTENTS = (
    "buy_to_open",
    "buy_to_close",
    "sell_to_open",
    "sell_to_close",
)


def _fill_table(*, price_nullable: bool) -> sa.Table:
    """``fill`` as it stands *before* the operations in this revision run.

    Restated in full because SQLite reflects neither CHECK constraints nor the
    indexes' intent, and a batch rebuild builds the new table from this object
    rather than from the database.
    """
    metadata = sa.MetaData()
    return sa.Table(
        "fill",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account", sa.String(length=8), nullable=False),
        sa.Column("activity_id", sa.String(length=128), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("group_id", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("position_intent", sa.String(length=16), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("price", sa.String(length=40), nullable=price_nullable),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_in_list("account", _ACCOUNT_MODES), name="ck_fill_account"),
        sa.CheckConstraint(_in_list("side", _FILL_SIDES), name="ck_fill_side"),
        sa.CheckConstraint(
            _in_list_or_null("position_intent", _POSITION_INTENTS),
            name="ck_fill_position_intent",
        ),
        sa.CheckConstraint(_positive_count("qty"), name="ck_fill_qty"),
        sa.CheckConstraint(
            _money_shape("price", nullable=price_nullable), name="ck_fill_price"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activity_id", name="uq_fill_activity_id"),
        sa.Index("ix_fill_account_at", "account", "at"),
        sa.Index("ix_fill_symbol", "symbol"),
        sa.Index("ix_fill_order_id", "order_id"),
        sa.Index("ix_fill_group_id", "group_id"),
    )


def upgrade() -> None:
    with op.batch_alter_table(
        "fill", copy_from=_fill_table(price_nullable=False)
    ) as batch_op:
        batch_op.alter_column(
            "price", existing_type=sa.String(length=40), nullable=True
        )
        # The shape CHECK has to learn about NULL too, or the column is
        # nullable in the type system and refused by the constraint.
        batch_op.drop_constraint("ck_fill_price", type_="check")
        batch_op.create_check_constraint(
            "ck_fill_price", _money_shape("price", nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "fill", copy_from=_fill_table(price_nullable=True)
    ) as batch_op:
        batch_op.alter_column(
            "price", existing_type=sa.String(length=40), nullable=False
        )
        batch_op.drop_constraint("ck_fill_price", type_="check")
        batch_op.create_check_constraint("ck_fill_price", _money_shape("price"))
