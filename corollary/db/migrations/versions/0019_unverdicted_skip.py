"""label_outcome: the off_watch_unverdicted skip reason (owner decision Q24a)

Owner decision Q24a (2026-11-11): an off-watch label with no tradeability
verdict on or before its reference session gets one backfilled by the audit;
when none can be had before decision 14's wait runs out, the label is
written ``off_watch_unverdicted`` -- never ``off_watch_untradeable``, which
only a failing verdict may say (``audit.py``'s decision 27). It is a
**membership** skip, like the untradeable one: the label is in neither
graded set, so ``scope`` is NULL.

Two CHECKs on each of ``label_outcome`` and its archive
``label_outcome_voided`` (0018) name the reason list:

* ``ck_*_skip_reason`` -- ``skip_reason IS NULL OR skip_reason IN (...)``,
  gaining ``off_watch_unverdicted`` at the end;
* ``ck_*_scope`` -- the membership reasons, which carry no scope, gaining it
  at the end too.

Both lists are the models' order (``LABEL_SKIP_REASONS``,
``LABEL_MEMBERSHIP_SKIP_REASONS``), so the stored DDL is ``create_all()``'s.
No row changes: every row written before this revision already satisfies
the wider CHECKs. The rows written ``off_watch_untradeable`` without a
verdict before Q24a are **not** rewritten here -- which of them had no
verdict is a question about verdicts, not about this schema, and a
migration that re-labels audit history would be deciding it silently.

SQLite cannot alter a CHECK in place, so each table is one batch rebuild --
0017's pattern -- with ``copy_from`` restating it exactly as 0014 (and 0018
for the archive) created it: twelve columns (thirteen with ``voided_at``),
eight CHECKs, two foreign keys and the primary key, no secondary index.
Every row is copied across, and every CHECK is re-created in the model's
order (0017's ``_restate_checks``: batch mode otherwise emits them in set
order). Nothing references either table, so the rebuild's ``DROP TABLE`` is
safe under ``PRAGMA foreign_keys=ON`` -- unlike 0018's ``audit_run``. The
copy *is* checked against the tables they reference, so a row naming a
missing label or run would fail it half-way and strand the batch's
``_alembic_tmp_*`` table, failing every retry on the leftover. Both
directions therefore run ``PRAGMA foreign_key_check`` on both tables first
and **refuse before any DDL**, naming the table and the first offending
rows (``_refuse_orphans``). The live database cannot hold one, since both
foreign keys have been enforced since 0014; one written with them off can.

**Downgrade** refuses, before touching anything, while any row in either
table carries ``off_watch_unverdicted``: deleting or re-labelling an audit
record to make a downgrade fit is not this migration's call (0016's and
0018's rule). With none, it is the exact reverse and keeps every row.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from alembic.operations import BatchOperations

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
# tests/db/test_unverdicted_skip_migration.py compares the DDL this writes
# with ``create_all()``'s, so drift fails a test.
_MONEY_TEXT_WIDTH = 40
_WINDOWS: tuple[str, ...] = ("1h", "1d")
_OUTCOME_SCOPES: tuple[str, ...] = ("watch", "discovery")
_UNVERDICTED = "off_watch_unverdicted"
_SKIP_REASONS_BEFORE: tuple[str, ...] = (
    "off_watch_untradeable",
    "discovery_cap",
    "price_unavailable",
    "duplicate_copy",
)
_MEMBERSHIP_BEFORE: tuple[str, ...] = (
    "off_watch_untradeable",
    "discovery_cap",
    "duplicate_copy",
)
_TABLES: tuple[str, ...] = ("label_outcome", "label_outcome_voided")


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


def _price_check(column: str) -> str:
    return (
        f"({_money_shape(column, nullable=True)}) "
        f"AND ({column} IS NULL OR substr({column}, 1, 1) <> '-')"
    )


_LABEL_OUTCOME_SHAPE = (
    "(skip_reason IS NOT NULL "
    "AND reference_at IS NULL AND reference_price IS NULL "
    "AND outcome_at IS NULL AND outcome_price IS NULL "
    "AND benchmark_return IS NULL AND excess_return IS NULL "
    "AND correct IS NULL) "
    "OR (skip_reason IS NULL "
    "AND reference_at IS NOT NULL AND reference_price IS NOT NULL "
    "AND outcome_at IS NOT NULL AND outcome_price IS NOT NULL "
    "AND benchmark_return IS NOT NULL AND excess_return IS NOT NULL)"
)


def _skip_reasons(*, unverdicted: bool) -> tuple[str, ...]:
    return _SKIP_REASONS_BEFORE + ((_UNVERDICTED,) if unverdicted else ())


def _membership(*, unverdicted: bool) -> tuple[str, ...]:
    return _MEMBERSHIP_BEFORE + ((_UNVERDICTED,) if unverdicted else ())


def _scope_check(*, unverdicted: bool) -> str:
    return (
        "(skip_reason IS NOT NULL AND "
        + _in_list("skip_reason", _membership(unverdicted=unverdicted))
        + " AND scope IS NULL) "
        "OR ((skip_reason IS NULL OR skip_reason = 'price_unavailable') "
        "AND scope IS NOT NULL AND "
        + _in_list("scope", _OUTCOME_SCOPES)
        + ")"
    )


def _checks(table: str, *, unverdicted: bool) -> list[tuple[str, str]]:
    """The eight CHECKs as ``(name, sql)``, in 0014's order -- the order
    ``models._label_outcome_checks`` declares them for both tables."""
    reasons = _skip_reasons(unverdicted=unverdicted)
    return [
        (f"ck_{table}_window", _in_list('"window"', _WINDOWS)),
        (f"ck_{table}_skip_reason", f"skip_reason IS NULL OR {_in_list('skip_reason', reasons)}"),
        (f"ck_{table}_shape", _LABEL_OUTCOME_SHAPE),
        (f"ck_{table}_scope", _scope_check(unverdicted=unverdicted)),
        (f"ck_{table}_reference_price", _price_check("reference_price")),
        (f"ck_{table}_outcome_price", _price_check("outcome_price")),
        (f"ck_{table}_benchmark_return", _money_shape("benchmark_return", nullable=True)),
        (f"ck_{table}_excess_return", _money_shape("excess_return", nullable=True)),
    ]


def _table(name: str, *, unverdicted: bool) -> sa.Table:
    """``label_outcome`` as 0014 created it, or its archive as 0018 did --
    with ``unverdicted``, as this revision leaves them."""
    columns: list[sa.Column[Any]] = [
        sa.Column("label_id", sa.Integer(), nullable=False),
        sa.Column("window", sa.String(length=4), nullable=False),
        sa.Column("reference_at", sa.DateTime(), nullable=True),
        sa.Column("reference_price", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("outcome_at", sa.DateTime(), nullable=True),
        sa.Column("outcome_price", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("benchmark_return", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("excess_return", sa.String(length=_MONEY_TEXT_WIDTH), nullable=True),
        sa.Column("correct", sa.Boolean(), nullable=True),
        sa.Column("skip_reason", sa.String(length=32), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=True),
        sa.Column("audit_run_id", sa.Integer(), nullable=False),
    ]
    key = ["label_id", "window"]
    if name == "label_outcome_voided":
        columns.append(sa.Column("voided_at", sa.DateTime(), nullable=False))
        key = ["label_id", "window", "audit_run_id"]
    return sa.Table(
        name,
        sa.MetaData(),
        *columns,
        *(sa.CheckConstraint(sql, name=ck) for ck, sql in _checks(name, unverdicted=unverdicted)),
        sa.ForeignKeyConstraint(["label_id"], ["sentiment_label.id"]),
        sa.ForeignKeyConstraint(["audit_run_id"], ["audit_run.id"]),
        sa.PrimaryKeyConstraint(*key),
    )


def _restate_checks(
    batch_op: BatchOperations, table: str, *, before: bool, after: bool
) -> None:
    """Drop every CHECK ``before`` had and create every one ``after`` has, in
    order -- 0017's ``_restate_checks``, for the same reason."""
    for name, _sql in _checks(table, unverdicted=before):
        batch_op.drop_constraint(name, type_="check")
    for name, sql in _checks(table, unverdicted=after):
        batch_op.create_check_constraint(name, sql)


def _rebuild(table: str, *, before: bool, after: bool) -> None:
    with op.batch_alter_table(table, copy_from=_table(table, unverdicted=before)) as batch_op:
        _restate_checks(batch_op, table, before=before, after=after)


#: How many offending rows a refusal names; the count states the rest.
_ORPHANS_NAMED = 3


def _refuse_orphans(direction: str) -> None:
    """Refuse, before any DDL, while either table holds a row its foreign
    keys do not admit.

    The rebuild's ``INSERT ... SELECT`` is checked against ``sentiment_label``
    and ``audit_run``, so one orphan fails it half-way -- and the failure
    strands ``_alembic_tmp_<table>``, so every retry then fails on the
    leftover rather than on the cause. The live database cannot hold an
    orphan (both keys enforced since 0014), but one written with foreign keys
    off, or restored from a damaged copy, can; deleting it to make the
    migration fit is not this migration's call (0008's rule).
    """
    bind = op.get_bind()
    for table in _TABLES:
        found = bind.execute(sa.text(f"PRAGMA foreign_key_check({table})")).all()
        if not found:
            continue
        # One result row per broken key: (table, rowid, parent, fkid).
        parents: dict[int, list[str]] = {}
        for _table, rowid, parent, _fkid in found:
            parents.setdefault(int(rowid), []).append(str(parent))
        named = []
        for rowid in sorted(parents)[:_ORPHANS_NAMED]:
            label_id, window, run_id = bind.execute(
                sa.text(f'SELECT label_id, "window", audit_run_id FROM {table} WHERE rowid = :r'),
                {"r": rowid},
            ).one()
            missing = ", ".join(sorted(set(parents[rowid])))
            named.append(
                f"label_id={label_id} window={window!r} audit_run_id={run_id} (missing: {missing})"
            )
        raise RuntimeError(
            f"refusing to {direction} 0019: {table} holds {len(parents)} row(s) whose foreign "
            f"key names no row, first {'; '.join(named)}. The rebuild copies every row "
            "under both foreign keys, so it would fail half-way and leave its temporary "
            "table behind. Deleting or re-pointing audit records to make a migration fit "
            "is not this migration's call; repair them, then retry."
        )


def upgrade() -> None:
    _refuse_orphans("upgrade")
    for table in _TABLES:
        _rebuild(table, before=False, after=True)


def downgrade() -> None:
    # Refuse before touching anything (0008's rule): each such row is an
    # audit record, and nothing below may delete or re-label one.
    _refuse_orphans("downgrade")
    bind = op.get_bind()
    for table in _TABLES:
        count = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE skip_reason = :reason"),
            {"reason": _UNVERDICTED},
        ).scalar_one()
        if count:
            raise RuntimeError(
                f"refusing to downgrade 0019: {table} holds {count} row(s) with skip_reason "
                f"'{_UNVERDICTED}', the record of a label no verdict could be had for. "
                "Deleting or re-labelling audit records to make a downgrade fit is not "
                "this migration's call."
            )
    for table in _TABLES:
        _rebuild(table, before=True, after=False)
