"""audit_run.voided_at/void_reason, label_outcome_voided, the void's route and record (R4)

Owner decision R4 (``docs/superpowers/specs/2026-11-09-phase-3-steps-5-7-remainder.md``):
a sentiment audit run can be voided by ``scripts/void_audit_run.py``. This
revision adds what that needs, and nothing else:

* ``audit_run`` + ``voided_at`` (DATETIME, ``UtcDateTime``) and
  ``void_reason`` (TEXT), with ``ck_audit_run_voided``: both NULL, or both
  set with a non-blank reason on a finished run.

  **Not a batch rebuild, which the spec named, because a rebuild cannot run
  on the live database.** ``label_outcome``, ``sentiment_accuracy`` and
  ``sentiment_source_status`` reference ``audit_run``, migrations run with
  ``PRAGMA foreign_keys=ON`` (``env.py`` uses ``create_db_engine``), and the
  rebuild's ``DROP TABLE audit_run`` fails with ``FOREIGN KEY constraint
  failed`` the moment any of them holds a row -- ``PRAGMA
  defer_foreign_keys`` does not help, since the dropped rows' violations
  outlive the rename. The pragma cannot be switched off inside the
  migration's transaction either. A failed rebuild strands
  ``_alembic_tmp_audit_run`` (Alembic's SQLite impl is not transactional
  DDL). So the CHECK is attached to ``void_reason`` **as a column
  constraint**, which ``ALTER TABLE ADD COLUMN`` accepts and tests against
  every existing row; SQLite enforces a column CHECK that names another
  column exactly as it does a table CHECK. ``models.AuditRun`` declares it at
  column level too, so ``create_all()`` and this revision store the same DDL.
  ``tests/db/test_audit_void_migration.py`` upgrades a database whose
  ``audit_run`` is referenced, so a later switch to a rebuild fails a test.

* ``label_outcome_voided``: ``label_outcome``'s twelve columns and eight
  CHECKs, plus ``voided_at`` NOT NULL, keyed ``(label_id, window,
  audit_run_id)``, both FKs with no ``ON DELETE`` action (decision 21).

* ``audit_log``'s ``ck_audit_log_sentiment_row`` admits ``new_value =
  'unaudited'``: the restore row that voiding writes when the voided run had
  moved a source out of ``unaudited``. ``previous_value <> new_value`` stays,
  so the only new shape is ``active|demoted -> unaudited``. A batch rebuild
  -- ``audit_log`` is referenced by nothing -- restating 0016's table, with
  all three CHECKs re-created in the model's order (0017's
  ``_restate_checks``: batch mode otherwise emits them in set order).

* ``notification_route`` + ``sentiment_audit_voided``, bell on and Discord
  on, filled only where absent (``seed.py`` may have written them first).

**Downgrade** refuses, before touching anything, while any run is voided,
any outcome is archived, or any restore row exists: each is the record of a
void, and deleting records to make a downgrade fit is not this migration's
call (0016's rule). With none, it is the exact reverse and keeps every row.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from alembic.operations import BatchOperations

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
# tests/db/test_audit_void_migration.py compares the DDL this writes with
# ``create_all()``'s, so drift fails a test.
_MONEY_TEXT_WIDTH = 40
_WINDOWS: tuple[str, ...] = ("1h", "1d")
_SKIP_REASONS: tuple[str, ...] = (
    "off_watch_untradeable",
    "discovery_cap",
    "price_unavailable",
    "duplicate_copy",
)
_MEMBERSHIP_SKIP_REASONS: tuple[str, ...] = (
    "off_watch_untradeable",
    "discovery_cap",
    "duplicate_copy",
)
_OUTCOME_SCOPES: tuple[str, ...] = ("watch", "discovery")
_AUDIT_CATEGORIES: tuple[str, ...] = (
    "risk",
    "feed",
    "notification",
    "watchlist",
    "sentiment",
    "account",
)

_AUDIT_RUN_VOIDED = (
    "(voided_at IS NULL AND void_reason IS NULL) "
    "OR (voided_at IS NOT NULL AND void_reason IS NOT NULL "
    "AND trim(void_reason) <> '' AND status <> 'running')"
)

#: 0014's ``ck_audit_log_sentiment_row`` as 0016 carried it, and as this
#: revision leaves it: ``new_value`` gains ``unaudited``.
_SENTIMENT_ROW_BEFORE = (
    "category <> 'sentiment' OR ("
    "field IN ('rules', 'massive') "
    "AND previous_value IN ('unaudited', 'active', 'demoted') "
    "AND new_value IN ('active', 'demoted') "
    "AND previous_value <> new_value)"
)
_SENTIMENT_ROW_AFTER = (
    "category <> 'sentiment' OR ("
    "field IN ('rules', 'massive') "
    "AND previous_value IN ('unaudited', 'active', 'demoted') "
    "AND new_value IN ('unaudited', 'active', 'demoted') "
    "AND previous_value <> new_value)"
)

_ACTIVE_GLOB = "active ••••" + "[A-Za-z0-9]" * 4


def _cash_session_value(column: str) -> str:
    return f"({column} = 'inactive' OR {column} GLOB '{_ACTIVE_GLOB}')"


_AUDIT_LOG_ACCOUNT_ROW = (
    "category <> 'account' OR ("
    "field = 'cash_session'"
    + " AND "
    + _cash_session_value("previous_value")
    + " AND "
    + _cash_session_value("new_value")
    + " AND NOT (previous_value = 'inactive' AND new_value = 'inactive')"
    + ")"
)

_ROUTES: tuple[tuple[str, str, bool], ...] = (
    ("sentiment_audit_voided", "bell", True),
    ("sentiment_audit_voided", "discord", True),
)

_notification_route = sa.table(
    "notification_route",
    sa.column("event", sa.String(length=64)),
    sa.column("channel", sa.String(length=16)),
    sa.column("enabled", sa.Boolean()),
)


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

_LABEL_OUTCOME_SCOPE = (
    "(skip_reason IS NOT NULL AND "
    + _in_list("skip_reason", _MEMBERSHIP_SKIP_REASONS)
    + " AND scope IS NULL) "
    "OR ((skip_reason IS NULL OR skip_reason = 'price_unavailable') "
    "AND scope IS NOT NULL AND "
    + _in_list("scope", _OUTCOME_SCOPES)
    + ")"
)

_PREFIX = "label_outcome_voided"


def _archive_checks() -> list[sa.CheckConstraint]:
    """``label_outcome``'s eight CHECKs in 0014's order, named for the archive."""
    return [
        sa.CheckConstraint(_in_list('"window"', _WINDOWS), name=f"ck_{_PREFIX}_window"),
        sa.CheckConstraint(
            f"skip_reason IS NULL OR {_in_list('skip_reason', _SKIP_REASONS)}",
            name=f"ck_{_PREFIX}_skip_reason",
        ),
        sa.CheckConstraint(_LABEL_OUTCOME_SHAPE, name=f"ck_{_PREFIX}_shape"),
        sa.CheckConstraint(_LABEL_OUTCOME_SCOPE, name=f"ck_{_PREFIX}_scope"),
        sa.CheckConstraint(
            _price_check("reference_price"), name=f"ck_{_PREFIX}_reference_price"
        ),
        sa.CheckConstraint(_price_check("outcome_price"), name=f"ck_{_PREFIX}_outcome_price"),
        sa.CheckConstraint(
            _money_shape("benchmark_return", nullable=True),
            name=f"ck_{_PREFIX}_benchmark_return",
        ),
        sa.CheckConstraint(
            _money_shape("excess_return", nullable=True),
            name=f"ck_{_PREFIX}_excess_return",
        ),
    ]


def _audit_log_checks(sentiment_row: str) -> list[tuple[str, str]]:
    """``audit_log``'s three CHECKs as ``(name, sql)``, in the model's order."""
    return [
        ("ck_audit_log_category", _in_list("category", _AUDIT_CATEGORIES)),
        ("ck_audit_log_sentiment_row", sentiment_row),
        ("ck_audit_log_account_row", _AUDIT_LOG_ACCOUNT_ROW),
    ]


def _audit_log_table(sentiment_row: str) -> sa.Table:
    """``audit_log`` as 0016 left it (``_SENTIMENT_ROW_BEFORE``) or as this
    revision leaves it (``_SENTIMENT_ROW_AFTER``)."""
    return sa.Table(
        "audit_log",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("previous_value", sa.String(length=128), nullable=False),
        sa.Column("new_value", sa.String(length=128), nullable=False),
        *(sa.CheckConstraint(sql, name=name) for name, sql in _audit_log_checks(sentiment_row)),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_audit_log_at", "at"),
    )


def _restate_audit_log_checks(batch_op: BatchOperations, *, before: str, after: str) -> None:
    """Drop every CHECK ``before`` had and create every one ``after`` has, in
    order -- 0017's ``_restate_checks``, for the same reason."""
    for name, _sql in _audit_log_checks(before):
        batch_op.drop_constraint(name, type_="check")
    for name, sql in _audit_log_checks(after):
        batch_op.create_check_constraint(name, sql)


def upgrade() -> None:
    # 1. audit_run: two columns, the CHECK on the second -- see the docstring
    # for why this is ALTER TABLE and not a rebuild.
    op.execute("ALTER TABLE audit_run ADD COLUMN voided_at DATETIME")
    op.execute(
        "ALTER TABLE audit_run ADD COLUMN void_reason TEXT "
        f"CONSTRAINT ck_audit_run_voided CHECK ({_AUDIT_RUN_VOIDED})"
    )

    # 2. The archive.
    op.create_table(
        "label_outcome_voided",
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
        sa.Column("voided_at", sa.DateTime(), nullable=False),
        *_archive_checks(),
        sa.ForeignKeyConstraint(["label_id"], ["sentiment_label.id"]),
        sa.ForeignKeyConstraint(["audit_run_id"], ["audit_run.id"]),
        sa.PrimaryKeyConstraint("label_id", "window", "audit_run_id"),
    )

    # 3. The restore row's shape.
    with op.batch_alter_table(
        "audit_log", copy_from=_audit_log_table(_SENTIMENT_ROW_BEFORE)
    ) as batch_op:
        _restate_audit_log_checks(
            batch_op, before=_SENTIMENT_ROW_BEFORE, after=_SENTIMENT_ROW_AFTER
        )

    # 4. The void's routes, where absent.
    bind = op.get_bind()
    existing = {
        (event, channel)
        for event, channel in bind.execute(
            sa.select(_notification_route.c.event, _notification_route.c.channel)
        )
    }
    missing = [
        {"event": event, "channel": channel, "enabled": enabled}
        for event, channel, enabled in _ROUTES
        if (event, channel) not in existing
    ]
    if missing:
        op.bulk_insert(_notification_route, missing)


def downgrade() -> None:
    # Refuse before touching anything (0008's rule): each of these is the
    # record of a void, and nothing below may delete one.
    bind = op.get_bind()
    blocking = {
        "voided audit_run row(s)": "SELECT COUNT(*) FROM audit_run WHERE voided_at IS NOT NULL "
        "OR void_reason IS NOT NULL",
        "archived label_outcome_voided row(s)": "SELECT COUNT(*) FROM label_outcome_voided",
        "restore audit_log row(s) (sentiment, new_value 'unaudited')": (
            "SELECT COUNT(*) FROM audit_log "
            "WHERE category = 'sentiment' AND new_value = 'unaudited'"
        ),
    }
    for what, sql in blocking.items():
        count = bind.execute(sa.text(sql)).scalar_one()
        if count:
            raise RuntimeError(
                f"refusing to downgrade 0018: the database holds {count} {what}, the "
                "record of a voided sentiment audit run. Deleting audit records to "
                "make a downgrade fit is not this migration's call."
            )

    events = sorted({event for event, _channel, _enabled in _ROUTES})
    op.execute(_notification_route.delete().where(_notification_route.c.event.in_(events)))

    with op.batch_alter_table(
        "audit_log", copy_from=_audit_log_table(_SENTIMENT_ROW_AFTER)
    ) as batch_op:
        _restate_audit_log_checks(
            batch_op, before=_SENTIMENT_ROW_AFTER, after=_SENTIMENT_ROW_BEFORE
        )

    op.drop_table("label_outcome_voided")

    # The CHECK lives on void_reason, so dropping that column drops it, and
    # voided_at is then referenced by nothing. SQLite >= 3.35.
    op.execute("ALTER TABLE audit_run DROP COLUMN void_reason")
    op.execute("ALTER TABLE audit_run DROP COLUMN voided_at")
