"""Ledger tables: fill, realized_trade, mleg_group, mleg_leg

Four of the design spec's remaining five tables. The fifth — ``notification``
— belongs to step 8 and is deliberately not created here.

**One revision, not two, and that is the reason this migration exists as a
unit.** Steps 5 (the FIFO matcher) and 6 (multi-leg grouping) both want new
tables and both run against this schema. Two revisions each declaring
``down_revision = "0001"`` would give Alembic two heads and break
``alembic upgrade head`` outright — not subtly, and not only for whoever
wrote the second one. So the schema for both lands here, ahead of either.

**Why these tables exist at all: Alpaca computes no realized P&L.** There is
no P&L field on any activity, no ``position_intent`` on a fill, and no leg
grouping on a position — a four-leg iron condor is four unrelated position
rows. Every figure the Activity page shows is therefore Corollary's
arithmetic over the broker's raw record, and these four tables hold that
record and that arithmetic. ``fill`` in particular exists because
``page_size`` on the activities endpoint maxes at 100, so re-fetching all
history per request is untenable.

Six choices here are not obvious, and each one is a way to be wrong about
money:

* **Every money column is VARCHAR(40), not NUMERIC or REAL** — the same
  choice ``0001`` made for ``risk_limit.value``, for the same reason. SQLite
  has no exact-decimal storage class and applies NUMERIC *affinity* to the
  declared type, so a NUMERIC column converts ``'8.21'`` to an IEEE double on
  the way in. ``corollary.db.types.Money`` resolves to ``String(40)`` on
  SQLite and converts back to ``Decimal`` on read, so no float touches the
  path. The storage types are written out **literally** here rather than
  imported, because a migration is a frozen record of the schema on the day;
  if they drift from the models, ``tests/db/test_migrations.py`` fails.

* **The money CHECKs are a *shape* constraint, deliberately weaker than
  ``ck_risk_limit_value``.** That constraint requires the text to start with a
  digit, because a negative or zero risk ceiling silently stops the bot
  trading. Copied onto ``pnl`` it would reject **every losing trade**, which
  is the normal case. So these admit a leading ``-``, admit zero (an
  ``OPEXP`` closes at zero; a scratch trade nets zero) and bound no
  magnitude. What they still refuse is text that is not a number — because
  ``Money`` reads the column back through ``Decimal(value)`` and
  ``Decimal('NaN')`` *succeeds*. A stored NaN then **raises**
  ``InvalidOperation`` on any ordering comparison -- unlike ``float('nan')``,
  which answers False -- so the "biggest loser" sort fails outright rather
  than mis-sorting. The silent half is the dangerous one: ``==`` is False
  without raising, and ``sum()`` folds lifetime P&L to NaN. ``Infinity`` is
  worse again, propagating through arithmetic without ever raising. Nothing
  on the read path refuses either value, so this CHECK is the only guard.

* **``pnl`` is signed, and SQL may not order, compare or aggregate it.** TEXT
  storage buys exactness and costs ordering. That cost is handled in
  ``corollary.db.types``, where the column raises ``MoneyComparisonError``
  rather than answering, not here — it is a property of the type rather than
  of the schema. The schema-level consequence worth knowing before step 7:
  lifetime realized P&L, average win, average loss and win rate are **all
  Python folds over loaded rows**, because ``SUM``, ``AVG``, ``MIN`` and
  ``ORDER BY`` are unavailable to every one of them.

* **``fill.activity_id`` is VARCHAR(128) and UNIQUE.** The activity id is
  **composite** — ``20260910131125598::68cda3e9-…``, a 17-digit timestamp
  concatenated with a UUID, 55 characters. It is not a bare UUID and a column
  sized for one would truncate it. The UNIQUE is the whole of idempotent
  ingestion: ingestion runs on startup and on an interval, re-pulling an
  overlapping window every time, and without it the second pass doubles every
  fill and the matcher books every position twice.

* **``fill.order_id``, ``fill.group_id`` and ``fill.position_intent`` are all
  nullable, and each absence means something specific.** Intent lives on the
  *order*, not the fill, so it arrives through a join that can fail — and
  guessing would defeat the column, since ``side`` takes three values
  (``sell_short`` opens a short, ``sell`` closes a long) and ``buy`` is
  *both* BTO and BTC. A non-trade activity has no ``order_id`` at all;
  ``group_id`` is the only linkage it gets, and therefore the only way to
  attribute a fee to an expiry, an assignment or an exercise.

* **``fill.qty`` and ``mleg_leg.ratio`` assert ``typeof() = 'integer'``, not
  merely ``> 0``.** SQLite's INTEGER *affinity* converts only when the
  conversion is lossless, so ``VALUES (1.5)`` into an INTEGER column stores a
  REAL and reports no error. Options trade in whole contracts, and a leg
  ratio in simplest form is whole by definition. ``> 0`` because both columns
  store the **normalised, unsigned** convention: a ``FILL`` carries an
  unsigned ``qty`` with a separate ``side``, while a non-trade row carries a
  *signed* ``qty`` and no ``side``, so ingestion turns the sign into a side
  and keeps the magnitude. A negative value here means that did not happen,
  which is how a matcher books an exercise backwards.

``mleg_leg`` keys on ``(group_id, symbol)`` and carries no ``account`` of its
own. Leg ratios arrive in simplest form — Alpaca requires the GCD across a
group's ``ratio_qty`` values to be 1 — so two legs on one symbol would
already have been combined into one, and a duplicate is a parse error rather
than something to merge. The book comes from the group, because a
denormalised copy is a second answer to "which book is this in", and two
answers is how a cash leg ends up under a paper group. ``ON DELETE CASCADE``
is on the foreign key as well as ``delete-orphan`` on the relationship, so
raw SQL cannot leave an orphan either; an orphan leg is grouping evidence
pointing at nothing, which is worse than no evidence.

**No seed rows.** ``0001`` seeds because a risk ceiling absent is a ceiling
unenforced. Every row here is observed history, and a seeded fill would be an
invented trade.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The CHECK bodies below are generated by these three helpers rather than
# pasted out six times, because the money shape alone is ~600 characters of
# SQL and six hand-copied instances differing in one identifier is a diff
# nobody can read and a constraint nobody can verify.
#
# They are **copies of the helpers in corollary.db.models, deliberately not
# imports of them.** Same rule 0001 states for the storage types and the seed
# values: a migration is a frozen record of the schema on the day, and one
# that imported live helpers would silently rewrite its own history the moment
# a helper changed. Frozen here means frozen in this file, which a private
# function satisfies and an import does not.
#
# `tests/db/test_migrations.py::test_upgrade_head_matches_the_models` is what
# holds the two copies to each other.

_MONEY_TEXT_WIDTH = 40


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _in_list_or_null(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IS NULL OR {_in_list(column, values)}"


def _money_shape(column: str, *, nullable: bool = False) -> str:
    """A signed plain decimal string: ``-7``, ``0.00``, ``123.45``.

    Refuses ``''``, ``'-'``, ``'+7'``, ``'--7'``, ``' 7'``, ``'7.'``,
    ``'7.5.5'``, ``'1E+2'``, ``'NaN'``, ``'Infinity'``, ``'-Infinity'``.
    ``'1E+2'`` is refused rather than parsed because ``Money`` binds with
    ``format(value, 'f')`` and so never writes an exponent; a value in that
    form arrived some other way, and one canonical spelling per number is what
    a text column owes its own constraints.
    """
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
_CLOSE_KINDS = ("fill", "expiry", "exercise", "assignment")


def upgrade() -> None:
    op.create_table(
        "fill",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account", sa.String(length=8), nullable=False),
        sa.Column("activity_id", sa.String(length=128), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("group_id", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("position_intent", sa.String(length=16), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("price", sa.String(length=40), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _in_list("account", _ACCOUNT_MODES), name="ck_fill_account"
        ),
        sa.CheckConstraint(_in_list("side", _FILL_SIDES), name="ck_fill_side"),
        sa.CheckConstraint(
            _in_list_or_null("position_intent", _POSITION_INTENTS),
            name="ck_fill_position_intent",
        ),
        sa.CheckConstraint(_positive_count("qty"), name="ck_fill_qty"),
        sa.CheckConstraint(_money_shape("price"), name="ck_fill_price"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activity_id", name="uq_fill_activity_id"),
    )
    # Ingestion resumes from the newest row it already holds, per book.
    op.create_index("ix_fill_account_at", "fill", ["account", "at"], unique=False)
    # The matcher's open-lot queue is per contract symbol.
    op.create_index("ix_fill_symbol", "fill", ["symbol"], unique=False)
    # The fill -> order join for `position_intent`, and fee attribution.
    op.create_index("ix_fill_order_id", "fill", ["order_id"], unique=False)
    # Fee attribution for the option events, which have no `order_id`.
    op.create_index("ix_fill_group_id", "fill", ["group_id"], unique=False)

    op.create_table(
        "realized_trade",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("open_price", sa.String(length=40), nullable=False),
        # NOT NULL, including on an expiry: an OPEXP closes at zero, which is
        # a price rather than an absence.
        sa.Column("close_price", sa.String(length=40), nullable=False),
        sa.Column("pnl", sa.String(length=40), nullable=False),
        # NULL where the cost basis is zero. A percentage of nothing is not
        # zero percent, and reporting 0% would read as a flat trade.
        sa.Column("pnl_pct", sa.String(length=40), nullable=True),
        sa.Column("close_kind", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            _in_list("account", _ACCOUNT_MODES), name="ck_realized_trade_account"
        ),
        sa.CheckConstraint(
            _in_list("close_kind", _CLOSE_KINDS), name="ck_realized_trade_close_kind"
        ),
        sa.CheckConstraint(_positive_count("qty"), name="ck_realized_trade_qty"),
        sa.CheckConstraint(
            _money_shape("open_price"), name="ck_realized_trade_open_price"
        ),
        sa.CheckConstraint(
            _money_shape("close_price"), name="ck_realized_trade_close_price"
        ),
        sa.CheckConstraint(_money_shape("pnl"), name="ck_realized_trade_pnl"),
        sa.CheckConstraint(
            _money_shape("pnl_pct", nullable=True), name="ck_realized_trade_pnl_pct"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # The Activity page reads one book, newest close first. That ordering is
    # on `closed_at`, a timestamp, which orders fine -- it is only the money
    # columns SQL may not sort.
    op.create_index(
        "ix_realized_trade_account_closed_at",
        "realized_trade",
        ["account", "closed_at"],
        unique=False,
    )
    op.create_index(
        "ix_realized_trade_symbol", "realized_trade", ["symbol"], unique=False
    )

    op.create_table(
        "mleg_group",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account", sa.String(length=8), nullable=False),
        # The *parent* order id, reached by a two-hop join -- a fill carries
        # its leg's id and the parent appears nowhere on it.
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        # Signed, and the sign is the direction: debit long, credit short.
        # NULL means the direction could not be established, and NULL is what
        # must then be reported.
        sa.Column("net_price", sa.String(length=40), nullable=True),
        sa.CheckConstraint(
            _in_list("account", _ACCOUNT_MODES), name="ck_mleg_group_account"
        ),
        sa.CheckConstraint(
            _money_shape("net_price", nullable=True), name="ck_mleg_group_net_price"
        ),
        sa.PrimaryKeyConstraint("id"),
        # One parent mleg order proposes exactly one group. Which also makes
        # re-ingestion idempotent for groups, and supplies the index the
        # grouper's lookup-by-order_id needs.
        sa.UniqueConstraint("order_id", name="uq_mleg_group_order_id"),
    )
    op.create_index(
        "ix_mleg_group_account_opened_at",
        "mleg_group",
        ["account", "opened_at"],
        unique=False,
    )

    op.create_table(
        "mleg_leg",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("ratio", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        # NOT NULL here and nullable on `fill`, deliberately: a leg is read
        # straight off the order object where the field is always present,
        # whereas a fill's intent comes from a join that can fail.
        sa.Column("position_intent", sa.String(length=16), nullable=False),
        sa.CheckConstraint(_in_list("side", _FILL_SIDES), name="ck_mleg_leg_side"),
        sa.CheckConstraint(
            _in_list("position_intent", _POSITION_INTENTS),
            name="ck_mleg_leg_position_intent",
        ),
        sa.CheckConstraint(_positive_count("ratio"), name="ck_mleg_leg_ratio"),
        sa.ForeignKeyConstraint(["group_id"], ["mleg_group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "symbol"),
    )


def downgrade() -> None:
    # mleg_leg before mleg_group: the foreign key points that way, and
    # `PRAGMA foreign_keys=ON` is set on every connection this app opens.
    op.drop_table("mleg_leg")
    op.drop_index("ix_mleg_group_account_opened_at", table_name="mleg_group")
    op.drop_table("mleg_group")
    op.drop_index("ix_realized_trade_symbol", table_name="realized_trade")
    op.drop_index(
        "ix_realized_trade_account_closed_at", table_name="realized_trade"
    )
    op.drop_table("realized_trade")
    op.drop_index("ix_fill_group_id", table_name="fill")
    op.drop_index("ix_fill_order_id", table_name="fill")
    op.drop_index("ix_fill_symbol", table_name="fill")
    op.drop_index("ix_fill_account_at", table_name="fill")
    op.drop_table("fill")
