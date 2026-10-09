"""notification_route: route ``spdr_seed_amended``, an adopted NPORT-P/A

Owner decision, 2026-09-30: *"Amendments -> adopt + notify."* When an
NPORT-P/A for a quarter already loaded passes the seed's validation, the
``spdr_holdings`` job stores the rebuilt snapshot and emits one
``spdr_seed_amended`` notification (``info``, ``account: null``). This
revision only seeds its routing, exactly as ``0006`` and ``0008`` did:

=====================  ====  =======
event                  bell  discord
=====================  ====  =======
spdr_seed_amended      off   on
=====================  ====  =======

**The defaults are a parent-session assumption**: decision 20's routing for
an ``info`` event (``watchlist_changed``, ``risk_limits_changed``, ...) is
bell off, Discord on, and the owner's decision said to use it.

**No schema change, and no CHECK to widen.** The owner's decision assumed the
event was CHECK-constrained; it is not. ``notification.event`` and
``notification_route.event`` carry no CHECK by design (``0005``/``0006``),
and ``notification.severity`` already admits ``info`` -- so only the two
route rows need a migration.

**Insert-if-missing, not a bare bulk insert.** ``seed.py`` carries the same
rows and runs on every startup, so a database served before this revision was
applied may already hold them -- and a plain insert would fail on the primary
key. An existing row is left exactly as it is: re-seeding must never undo a
change a human made.

The downgrade deletes the two rows, which discards any edit made to them since.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copies, not imports, for the reason every earlier revision states: a
# migration is a frozen record of the schema on the day.
_ROUTES: tuple[tuple[str, str, bool], ...] = (
    ("spdr_seed_amended", "bell", False),
    ("spdr_seed_amended", "discord", True),
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
