"""notification_route: route the owner's own actions to the bell and Discord

The owner, 2026-09-24: *"put it on the bell and discord, any action i do
should be put into the discord."* Five new events, emitted by the API routes
that perform the actions (``corollary/api/operator.py``). This revision only
seeds their routing, exactly as ``0001`` seeded PRD section 10's table:

=============================  ====  =======
event                          bell  discord
=============================  ====  =======
operator_halt                  on    on
operator_resume                on    on
risk_limits_changed            off   on
data_feeds_changed             off   on
notification_routes_changed    off   on
=============================  ====  =======

No schema change: ``notification_route.event`` and ``notification.event``
carry no CHECK by design, and ``notification.severity`` already admits
``warning`` and ``info``.

**Insert-if-missing, not a bare bulk insert.** ``seed.py`` carries the same
rows and runs on every startup, so a database that was served before this
revision was applied may already hold them -- and a plain insert would fail
on the primary key. An existing row is left exactly as it is, the same rule
``seed.py`` follows: re-seeding must never undo a change a human made.

The downgrade deletes the ten rows, which discards any edit made to them since.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
_ROUTES: tuple[tuple[str, str, bool], ...] = (
    ("operator_halt", "bell", True),
    ("operator_halt", "discord", True),
    ("operator_resume", "bell", True),
    ("operator_resume", "discord", True),
    ("risk_limits_changed", "bell", False),
    ("risk_limits_changed", "discord", True),
    ("data_feeds_changed", "bell", False),
    ("data_feeds_changed", "discord", True),
    ("notification_routes_changed", "bell", False),
    ("notification_routes_changed", "discord", True),
)

_notification_route = sa.table(
    "notification_route",
    sa.column("event", sa.String(length=64)),
    sa.column("channel", sa.String(length=16)),
    sa.column("enabled", sa.Boolean()),
)


def upgrade() -> None:
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
    events = sorted({event for event, _channel, _enabled in _ROUTES})
    op.execute(
        _notification_route.delete().where(_notification_route.c.event.in_(events))
    )
