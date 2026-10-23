"""audit_log admits account; cash_session_confirmed / cash_session_ended routes

The engine-side confirmed Cash session (rule 5). The session itself is **in
memory only** -- ``corollary.engine.cash_session`` -- and this revision
creates no table for it and must never gain one: a restart has no session
whatever the database says. What lands here is the record of it:

* ``audit_log.category`` admits ``account``, and ``audit_log`` gains
  ``ck_audit_log_account_row``: an ``account`` row has ``field`` =
  ``cash_session`` and each value either ``inactive`` or ``active ••••1234``
  (four ASCII letters or digits after the bullets), and never ``inactive`` to
  ``inactive`` -- ending a session that did not exist writes nothing. The
  shape is what makes "no full account number in the audit log" structural
  rather than a convention (rule 6). Other categories are untouched by it.
  SQLite cannot alter a CHECK in place, so both arrive in one batch rebuild,
  the one 0008 and 0014 used, with ``copy_from`` restating the table as 0014
  left it; every existing row is copied across.
* ``notification_route`` gains ``cash_session_confirmed`` and
  ``cash_session_ended``, **bell on, Discord on** for both (an orchestrator
  decision: which book the engine may read is worth the bell, not only a
  record for Discord), insert-if-missing exactly as 0006/0008/0011/0014 -- a
  human's edit stands. ``notification_route.event`` carries no CHECK, so the
  routes are rows, not a constraint change.

**Downgrade refuses rather than deletes** if any ``account`` audit row exists,
before anything moves -- 0008's reasoning: the old CHECK cannot hold the row,
and destroying an audit record to make a downgrade fit is not a migration's
call. Otherwise it is the real reverse: the two route pairs deleted, the
account-row CHECK dropped and the category CHECK narrowed, in one rebuild.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
# tests/db/test_cash_session_migration.py runs the account-row CHECK against
# both this and ``create_all()``, so drift fails a test.
_AUDIT_CATEGORIES_BEFORE: tuple[str, ...] = (
    "risk",
    "feed",
    "notification",
    "watchlist",
    "sentiment",
)
_AUDIT_CATEGORIES_AFTER: tuple[str, ...] = _AUDIT_CATEGORIES_BEFORE + ("account",)

#: 0014's ``ck_audit_log_sentiment_row``, expanded, as the table carries it.
_AUDIT_LOG_SENTIMENT_ROW = (
    "category <> 'sentiment' OR ("
    "field IN ('rules', 'massive') "
    "AND previous_value IN ('unaudited', 'active', 'demoted') "
    "AND new_value IN ('active', 'demoted') "
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
    ("cash_session_confirmed", "bell", True),
    ("cash_session_confirmed", "discord", True),
    ("cash_session_ended", "bell", True),
    ("cash_session_ended", "discord", True),
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


def _audit_log_table(categories: tuple[str, ...], *, account_row: bool) -> sa.Table:
    """``audit_log`` as 0014 left it, with ``categories`` in its CHECK and,
    from this revision on, ``ck_audit_log_account_row``."""
    checks = [
        sa.CheckConstraint(
            _in_list("category", categories), name="ck_audit_log_category"
        ),
        sa.CheckConstraint(
            _AUDIT_LOG_SENTIMENT_ROW, name="ck_audit_log_sentiment_row"
        ),
    ]
    if account_row:
        checks.append(
            sa.CheckConstraint(_AUDIT_LOG_ACCOUNT_ROW, name="ck_audit_log_account_row")
        )
    return sa.Table(
        "audit_log",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("previous_value", sa.String(length=128), nullable=False),
        sa.Column("new_value", sa.String(length=128), nullable=False),
        *checks,
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_audit_log_at", "at"),
    )


def upgrade() -> None:
    with op.batch_alter_table(
        "audit_log",
        copy_from=_audit_log_table(_AUDIT_CATEGORIES_BEFORE, account_row=False),
    ) as batch_op:
        batch_op.drop_constraint("ck_audit_log_category", type_="check")
        batch_op.create_check_constraint(
            "ck_audit_log_category", _in_list("category", _AUDIT_CATEGORIES_AFTER)
        )
        batch_op.create_check_constraint(
            "ck_audit_log_account_row", _AUDIT_LOG_ACCOUNT_ROW
        )

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
    # Refuse before touching anything -- see 0008's downgrade. Alembic's SQLite
    # impl is not transactional-DDL, so a failure part-way leaves the work
    # before it done, and a rebuild failing on the old CHECK strands
    # ``_alembic_tmp_audit_log``.
    bind = op.get_bind()
    blocking = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM audit_log WHERE NOT "
            + _in_list("category", _AUDIT_CATEGORIES_BEFORE)
        )
    ).scalar_one()
    if blocking:
        raise RuntimeError(
            f"refusing to downgrade 0016: audit_log holds {blocking} row(s) in a "
            f"category 0015 does not admit (account). Deleting audit records "
            f"to make a downgrade fit is not this migration's call."
        )

    events = sorted({event for event, _channel, _enabled in _ROUTES})
    op.execute(
        _notification_route.delete().where(_notification_route.c.event.in_(events))
    )

    with op.batch_alter_table(
        "audit_log",
        copy_from=_audit_log_table(_AUDIT_CATEGORIES_AFTER, account_row=True),
    ) as batch_op:
        batch_op.drop_constraint("ck_audit_log_account_row", type_="check")
        batch_op.drop_constraint("ck_audit_log_category", type_="check")
        batch_op.create_check_constraint(
            "ck_audit_log_category", _in_list("category", _AUDIT_CATEGORIES_BEFORE)
        )
