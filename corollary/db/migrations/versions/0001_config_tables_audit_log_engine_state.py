"""Config tables, audit log, engine state

The first five of the design spec's ten tables. The other five — ``fill``,
``realized_trade``, ``mleg_group``, ``mleg_leg`` and ``notification`` — belong
to later steps and are deliberately not created here.

Two deliberate choices worth reading before editing this file:

* **``risk_limit.value`` is VARCHAR, not NUMERIC or REAL.** SQLite has no
  exact decimal storage class: a NUMERIC-affinity column converts ``'7.5'``
  to an IEEE double on the way in, which is exactly the leak CLAUDE.md's
  "``Numeric``, not ``Float``, or the rule leaks at the database boundary"
  exists to prevent. ``corollary.db.types.Money`` resolves to
  ``String(40)`` on SQLite and converts back to ``Decimal`` on read. The
  storage types are written out literally here rather than imported, because
  a migration is a frozen record of the schema on the day; if they ever drift
  from the models, ``tests/db/test_migrations.py`` fails.

  The cost of TEXT is that SQL orders it lexicographically, so ``MAX(value)``
  over (7, 20, 8, 25, 40) is 8. That is handled in ``corollary.db.types``,
  where the column refuses to be compared, ordered or aggregated in SQL at
  all — not here, because it is a property of the type rather than of the
  schema.
* **``ck_risk_limit_value`` is a text-shape constraint, and it has to be.**
  Rule 4 puts enforcement on the server, and the column being TEXT rules out
  both ``value > 0`` (lexicographic) and ``CAST(value AS REAL) > 0`` (a float
  in the one schema built to exclude them). See the comment on the
  constraint itself.
* **The seed rows are literals here, and duplicated in ``corollary/db/seed.py``.**
  Same reason: a migration that imported live defaults would silently rewrite
  its own history when a default changed.
  ``test_migration_seed_matches_the_seed_module`` fails the moment the two
  disagree.

Revision ID: 0001
Revises:
Create Date: 2026-09-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    risk_limit = op.create_table(
        "risk_limit",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=40), nullable=False),
        # Rule 4: the engine enforces limits, and Python is not the only way
        # into this file. `value` is TEXT, so `value > 0` would compare
        # lexicographically and `CAST(value AS REAL)` would put an IEEE double
        # in the one place this schema exists to keep floats out of. What is
        # left is the shape of the text: starts with a digit (no `-7`, no
        # `Infinity`, no `NaN`), digits and at most one point and nothing
        # else, at least one non-zero digit (no `0`, no `0.00`), no trailing
        # point, at most three digits before it (no `999999`). Those are
        # exactly the five values that used to round-trip, and each one
        # disables the risk manager in its own way.
        sa.CheckConstraint(
            "typeof(value) = 'text' "
            "AND length(value) BETWEEN 1 AND 24 "
            "AND value GLOB '[0-9]*' "
            "AND NOT value GLOB '*[^0-9.]*' "
            "AND value GLOB '*[1-9]*' "
            "AND NOT value GLOB '*.' "
            "AND length(value) - length(replace(value, '.', '')) <= 1 "
            "AND (CASE WHEN instr(value, '.') = 0 "
            "THEN length(value) ELSE instr(value, '.') - 1 END) <= 3",
            name="ck_risk_limit_value",
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    data_feed = op.create_table(
        "data_feed",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    notification_route = op.create_table(
        "notification_route",
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "channel IN ('bell', 'discord')", name="ck_notification_route_channel"
        ),
        sa.PrimaryKeyConstraint("event", "channel"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("previous_value", sa.String(length=128), nullable=False),
        sa.Column("new_value", sa.String(length=128), nullable=False),
        sa.CheckConstraint(
            "category IN ('risk', 'feed', 'notification')",
            name="ck_audit_log_category",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"], unique=False)

    engine_state = op.create_table(
        "engine_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("halted", sa.Boolean(), nullable=False),
        sa.Column("halted_reason", sa.String(length=256), nullable=True),
        sa.Column("halted_at", sa.DateTime(), nullable=True),
        sa.Column("t0", sa.DateTime(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_engine_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- seed -------------------------------------------------------- #
    # The CLAUDE.md rule-4 ceilings, as text because the column is text.
    op.bulk_insert(
        risk_limit,
        [
            {"key": "max_risk_per_trade_pct", "value": "7"},
            {"key": "max_daily_loss_pct", "value": "20"},
            {"key": "max_concurrent_positions", "value": "8"},
            {"key": "max_exposure_per_underlying", "value": "25"},
            {"key": "max_net_directional_pct", "value": "40"},
        ],
    )
    # Historical equity bars are SIP, never IEX: IEX is ~2.5% of US volume and
    # SIP is free for anything older than 15 minutes, so an IEX default would
    # silently reinterpret every min_avg_volume threshold by a factor of forty.
    op.bulk_insert(
        data_feed,
        [
            {"key": "ALPACA_OPTIONS_FEED", "value": "indicative"},
            {"key": "ALPACA_STOCK_FEED_HISTORICAL", "value": "sip"},
            {"key": "ALPACA_STOCK_FEED_REALTIME", "value": "iex"},
        ],
    )
    # PRD §10's shipped routing table. Defaults, not invariants.
    op.bulk_insert(
        notification_route,
        [
            {"event": "order_filled", "channel": "bell", "enabled": True},
            {"event": "order_filled", "channel": "discord", "enabled": True},
            {"event": "order_rejected", "channel": "bell", "enabled": True},
            {"event": "order_rejected", "channel": "discord", "enabled": True},
            {"event": "stop_loss_hit", "channel": "bell", "enabled": True},
            {"event": "stop_loss_hit", "channel": "discord", "enabled": True},
            {"event": "daily_loss_halt", "channel": "bell", "enabled": True},
            {"event": "daily_loss_halt", "channel": "discord", "enabled": True},
            {"event": "engine_error", "channel": "bell", "enabled": True},
            {"event": "engine_error", "channel": "discord", "enabled": True},
            {"event": "price_alert", "channel": "bell", "enabled": True},
            {"event": "price_alert", "channel": "discord", "enabled": True},
            {"event": "recommendations_ready", "channel": "bell", "enabled": True},
            {"event": "recommendations_ready", "channel": "discord", "enabled": False},
            {"event": "strategy_promotion", "channel": "bell", "enabled": True},
            {"event": "strategy_promotion", "channel": "discord", "enabled": False},
        ],
    )
    # Halted, and not resumable except by a human. Cold start comes up halted
    # until the opening snapshot succeeds; t0 is written on the first ever run.
    op.bulk_insert(
        engine_state,
        [
            {
                "id": 1,
                "halted": True,
                "halted_reason": None,
                "halted_at": None,
                "t0": None,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("engine_state")
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("notification_route")
    op.drop_table("data_feed")
    op.drop_table("risk_limit")
