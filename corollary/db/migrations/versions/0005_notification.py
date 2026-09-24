"""notification, notification_delivery: rule 9's halt alert lands somewhere

Phase 3 decision 14. Rule 9 says a dead-man's-switch halt *fires a critical
notification*, and until this revision that notification was a log line: the
``notification`` table Phase 2's Database section described never landed, and
the bell rendered fixtures. Two tables.

``notification`` is one row per notification the engine raised, whichever
channels it reached -- the Discord sink's attempts need something to
reference, and "raised and sent nowhere" is itself worth keeping. ``account``
is NULL for engine and audit events, which belong to no book and show in both.
``id`` is a uuid4 hex generated when the notification is raised, so every sink
records against the same id without waiting on another.

``notification_delivery`` is one row per **attempt** on one channel --
``delivered``, ``failed``, or ``dropped`` when no attempt could be made --
because an alert that silently did not arrive is the rule 9 failure the
routing confirm exists to prevent. A retry is a second row. The bell's own
row is a delivery too: whether the bell shows a notification is decided by
the routing **at emit time** and recorded here, never re-read from
``notification_route``, so unchecking a route cannot erase what already
arrived. ``detail`` never carries the webhook URL (rule 6).

``notification_delivery.notification_id`` is an indexed **soft reference**,
not a foreign key. Foreign keys are enforced on every connection, so one here
would reject every Discord outcome for an alert whose ``notification`` row
failed to write -- most likely from lock contention during a halt, exactly
when the record matters. Without it the outcome always lands, and an orphan
delivery row is the evidence that the bell write failed.

``event`` carries no CHECK, following ``notification_route.event``: the seeded
routes and the engine's constants are the authority, and Phase 3 step 6 adds
two events.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.

_ACCOUNT_MODES = ("paper", "cash")
_NOTIFICATION_CHANNELS = ("bell", "discord")
_NOTIFICATION_SEVERITIES = ("critical", "warning", "info")
_NOTIFICATION_DELIVERY_STATUSES = ("delivered", "failed", "dropped")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.create_table(
        "notification",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("account", sa.String(length=8), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            _in_list("severity", _NOTIFICATION_SEVERITIES),
            name="ck_notification_severity",
        ),
        # NULL admitted on purpose: an engine event belongs to no book.
        sa.CheckConstraint(
            f"account IS NULL OR {_in_list('account', _ACCOUNT_MODES)}",
            name="ck_notification_account",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_account_at", "notification", ["account", "at"]
    )
    op.create_table(
        "notification_delivery",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("notification_id", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
        sa.Column("detail", sa.String(length=400), nullable=False),
        sa.CheckConstraint(
            _in_list("channel", _NOTIFICATION_CHANNELS),
            name="ck_notification_delivery_channel",
        ),
        sa.CheckConstraint(
            _in_list("status", _NOTIFICATION_DELIVERY_STATUSES),
            name="ck_notification_delivery_status",
        ),
        # No foreign key on notification_id, deliberately -- see the module
        # docstring. A Discord outcome must land even when its notification
        # row did not.
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_delivery_notification_id",
        "notification_delivery",
        ["notification_id"],
    )


def downgrade() -> None:
    """Drops both tables, and with them every notification and delivery record.

    That is the real cost of going back: the bell empties, and the record of
    which alerts reached Discord and which did not is gone. Nothing else
    reads either table, so nothing downstream breaks -- the engine's
    notifier degrades to logging its own write failures.
    """
    op.drop_index(
        "ix_notification_delivery_notification_id",
        table_name="notification_delivery",
    )
    op.drop_table("notification_delivery")
    op.drop_index("ix_notification_account_at", table_name="notification")
    op.drop_table("notification")
