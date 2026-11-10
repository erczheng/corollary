"""sentiment_accuracy.sessions: the five-session floor's stored count (R3, Q23b)

Owner decision Q23b: a source is eligible to transition only when its graded
directional labels span at least five distinct regular sessions in each
window, beside decision 13's 30 / 100 label floors
(``docs/superpowers/specs/2026-11-09-phase-3-steps-5-7-remainder.md``, R3).
``sentiment_accuracy`` gains the count that floor reads, so the figure a
transition acted on is the figure stored:

* ``sessions`` INTEGER, **nullable**. NULL means the row was written before
  Q23b and carries no session count -- every row that exists when this
  revision runs. There is **no backfill**: the sessions a past run counted
  would have to be recomputed from today's ``label_outcome`` rows, which is a
  different figure from the one that run acted on, and storing it beside
  that run's counts would misstate what the run saw. No reader may treat
  NULL as eligible.
* ``ck_sentiment_accuracy_sessions``: ``sessions IS NULL OR (typeof(sessions)
  = 'integer' AND sessions >= 0)``. The ``typeof`` guard is the one every
  other count on this table carries (0014's ``_non_negative_count``):
  INTEGER affinity stores ``2.5`` as REAL without complaint.

SQLite cannot add a table CHECK in place, so the column and its CHECK arrive
in one batch rebuild -- the one 0008, 0014 and 0016 used -- with
``copy_from`` restating the table exactly as 0014 created it (no later
revision touched it): its seven CHECKs, its foreign key to ``audit_run`` and
its five-column primary key. ``sentiment_accuracy`` has no secondary index.
Every existing row is copied across. The CHECKs are re-created in 0014's
order inside the rebuild, because batch mode otherwise emits them in an
arbitrary one (``_restate_checks``).

**Downgrade** is the real reverse, in one rebuild: the CHECK and the column
are dropped and every row is kept. The session counts go with their column;
nothing else can hold them, and a row without its count is exactly the shape
0016 stores. Re-upgrading leaves them NULL, as for any pre-Q23b row.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-09

"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from alembic.operations import BatchOperations

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
# tests/db/test_accuracy_sessions_migration.py checks the rebuilt table keeps
# every constraint 0014 gave it, and tests/db/test_sentiment_audit.py runs the
# new CHECK against both this and ``create_all()``, so drift fails a test.
_WINDOWS: tuple[str, ...] = ("1h", "1d")
_BASES: tuple[str, ...] = ("trailing_28d", "since_demotion")
_SCOPES: tuple[str, ...] = ("all", "watch", "discovery")
_SOURCES: tuple[str, ...] = ("rules", "massive")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _non_negative_count(column: str) -> str:
    return f"typeof({column}) = 'integer' AND {column} >= 0"


_SESSIONS_CHECK = f"sessions IS NULL OR ({_non_negative_count('sessions')})"


def _checks(*, sessions: bool) -> list[tuple[str, str]]:
    """Every CHECK on the table as ``(name, sql)``, in 0014's order -- the
    order ``models.SentimentAccuracy`` declares them -- then this revision's."""
    checks = [
        ("ck_sentiment_accuracy_source", _in_list("source", _SOURCES)),
        ("ck_sentiment_accuracy_window", _in_list('"window"', _WINDOWS)),
        ("ck_sentiment_accuracy_basis", _in_list("basis", _BASES)),
        ("ck_sentiment_accuracy_scope", _in_list("scope", _SCOPES)),
        ("ck_sentiment_accuracy_graded", _non_negative_count("graded")),
        (
            "ck_sentiment_accuracy_correct",
            f"{_non_negative_count('correct')} AND correct <= graded",
        ),
        ("ck_sentiment_accuracy_neutral", _non_negative_count("neutral")),
    ]
    if sessions:
        checks.append(("ck_sentiment_accuracy_sessions", _SESSIONS_CHECK))
    return checks


def _sentiment_accuracy_table(*, sessions: bool) -> sa.Table:
    """``sentiment_accuracy`` as 0014 created it and, with ``sessions``, as
    this revision leaves it."""
    columns: list[sa.Column[Any]] = [
        sa.Column("audit_run_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("window", sa.String(length=4), nullable=False),
        sa.Column("basis", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("graded", sa.Integer(), nullable=False),
        sa.Column("correct", sa.Integer(), nullable=False),
        sa.Column("neutral", sa.Integer(), nullable=False),
    ]
    if sessions:
        columns.append(sa.Column("sessions", sa.Integer(), nullable=True))
    return sa.Table(
        "sentiment_accuracy",
        sa.MetaData(),
        *columns,
        *(sa.CheckConstraint(sql, name=name) for name, sql in _checks(sessions=sessions)),
        sa.ForeignKeyConstraint(["audit_run_id"], ["audit_run.id"]),
        sa.PrimaryKeyConstraint("audit_run_id", "source", "window", "basis", "scope"),
    )


def _restate_checks(batch_op: BatchOperations, *, before: bool, after: bool) -> None:
    """Drop every CHECK ``before`` had and create every one ``after`` has, in order.

    Not cosmetic. Batch mode copies a table's constraints by iterating
    ``Table.constraints``, which is a **set**, so without this their order in
    the rebuilt ``CREATE TABLE`` is arbitrary and can differ from one process
    to the next. SQLite reports the first CHECK a row fails in that order, so
    a row breaking two (``graded = -1`` also breaks ``correct <= graded``)
    would name a different constraint from run to run, and from a
    ``create_all()`` database. Re-creating them here fixes the order to the
    model's: batch keeps named constraints in insertion order.
    """
    for name, _sql in _checks(sessions=before):
        batch_op.drop_constraint(name, type_="check")
    for name, sql in _checks(sessions=after):
        batch_op.create_check_constraint(name, sql)


def upgrade() -> None:
    with op.batch_alter_table(
        "sentiment_accuracy", copy_from=_sentiment_accuracy_table(sessions=False)
    ) as batch_op:
        batch_op.add_column(sa.Column("sessions", sa.Integer(), nullable=True))
        _restate_checks(batch_op, before=False, after=True)


def downgrade() -> None:
    with op.batch_alter_table(
        "sentiment_accuracy", copy_from=_sentiment_accuracy_table(sessions=True)
    ) as batch_op:
        _restate_checks(batch_op, before=True, after=False)
        batch_op.drop_column("sessions")
