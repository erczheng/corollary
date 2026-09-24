"""Shipped defaults for the three config tables and the engine singleton.

Seeding exists so Phase 6's risk manager reads a table that already has the
CLAUDE.md ceilings in it, rather than a table that might be empty.

**A seed is not a fallback, and the difference is the whole point.** ``seed()``
inserts a row that is *absent* and never touches one that is present. Nothing
here — and nothing that reads these tables — substitutes a number for a
missing row. ``risk_limit_value`` returns ``Decimal | None`` and ``None``
means "no ceiling configured", exactly as the frontend's ``riskLimitFor``
returns ``number | null`` for the same reason: two order tickets once did this
lookup themselves with different invented fallbacks, ``?? 7`` in one and
``?? 0`` in the other, so one reported a ceiling nobody had set and the other
reported every trade as over-limit. A missing limit is a condition to report,
not a hole to plug.
"""

from decimal import Decimal
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from corollary.db.models import (
    ENGINE_STATE_ID,
    DataFeed,
    EngineState,
    NotificationRoute,
    RiskLimit,
)

__all__ = [
    "DATA_FEED_DEFAULTS",
    "NOTIFICATION_ROUTE_DEFAULTS",
    "RISK_LIMIT_DEFAULTS",
    "risk_limit_value",
    "risk_limits",
    "seed",
]

#: The ceilings from CLAUDE.md rule 4. ``Decimal`` from a string literal, not
#: from an int or a float — the column is exact and the constant should read
#: as exact too.
#:
#: What "risk" means is stated in CLAUDE.md and is not re-derived per call
#: site: defined-risk structures use maximum loss at expiry, long options use
#: premium paid, undefined-risk structures use a stress loss at ±2σ of the
#: underlying's 20-day realized volatility.
RISK_LIMIT_DEFAULTS: Mapping[str, Decimal] = {
    "max_risk_per_trade_pct": Decimal("7"),
    "max_daily_loss_pct": Decimal("20"),
    "max_concurrent_positions": Decimal("8"),
    "max_exposure_per_underlying": Decimal("25"),
    "max_net_directional_pct": Decimal("40"),
}

#: The three feed variables named in CLAUDE.md, keyed by the environment
#: variable each one mirrors, with the values a Basic plan can actually serve.
#:
#: Historical equity bars default to **sip**, never iex: IEX is ~2.5% of US
#: volume and SIP is free for anything with an ``end`` older than 15 minutes,
#: so an iex default would silently reinterpret every ``min_avg_volume``
#: threshold in every strategy YAML by a factor of forty.
#:
#: These are the seeded rows, not a runtime lookup. The provider still reads
#: the environment; this table is what Settings edits.
DATA_FEED_DEFAULTS: Mapping[str, str] = {
    "ALPACA_OPTIONS_FEED": "indicative",
    "ALPACA_STOCK_FEED_HISTORICAL": "sip",
    "ALPACA_STOCK_FEED_REALTIME": "iex",
}

#: PRD §10's shipped routing table, one row per (event, channel).
#:
#: Defaults, not invariants: Settings may turn any cell off, including a
#: critical one, behind a confirm that names what stops arriving.
NOTIFICATION_ROUTE_DEFAULTS: tuple[tuple[str, str, bool], ...] = (
    ("order_filled", "bell", True),
    ("order_filled", "discord", True),
    ("order_rejected", "bell", True),
    ("order_rejected", "discord", True),
    ("stop_loss_hit", "bell", True),
    ("stop_loss_hit", "discord", True),
    ("daily_loss_halt", "bell", True),
    ("daily_loss_halt", "discord", True),
    ("engine_error", "bell", True),
    ("engine_error", "discord", True),
    # The owner's own actions (migration 0006). A halt or resume is worth the
    # bell; a settings change is a record for Discord, and the Settings page
    # already shows it in the audit log.
    ("operator_halt", "bell", True),
    ("operator_halt", "discord", True),
    ("operator_resume", "bell", True),
    ("operator_resume", "discord", True),
    ("price_alert", "bell", True),
    ("price_alert", "discord", True),
    ("recommendations_ready", "bell", True),
    ("recommendations_ready", "discord", False),
    ("strategy_promotion", "bell", True),
    ("strategy_promotion", "discord", False),
    ("risk_limits_changed", "bell", False),
    ("risk_limits_changed", "discord", True),
    ("data_feeds_changed", "bell", False),
    ("data_feeds_changed", "discord", True),
    ("notification_routes_changed", "bell", False),
    ("notification_routes_changed", "discord", True),
)


def seed(session: Session) -> int:
    """Insert any missing default rows. Returns how many were inserted.

    Idempotent: run it on every startup. An existing row is left exactly as it
    is, because Settings is server-backed and re-seeding must never undo a
    change a human made. The caller commits.
    """
    inserted = 0

    existing_limits = set(session.scalars(select(RiskLimit.key)).all())
    for key, value in RISK_LIMIT_DEFAULTS.items():
        if key not in existing_limits:
            session.add(RiskLimit(key=key, value=value))
            inserted += 1

    existing_feeds = set(session.scalars(select(DataFeed.key)).all())
    for key, feed_value in DATA_FEED_DEFAULTS.items():
        if key not in existing_feeds:
            session.add(DataFeed(key=key, value=feed_value))
            inserted += 1

    existing_routes = set(
        session.execute(
            select(NotificationRoute.event, NotificationRoute.channel)
        ).all()
    )
    for event, channel, enabled in NOTIFICATION_ROUTE_DEFAULTS:
        if (event, channel) not in existing_routes:
            session.add(
                NotificationRoute(event=event, channel=channel, enabled=enabled)
            )
            inserted += 1

    if session.get(EngineState, ENGINE_STATE_ID) is None:
        # Halted, per the spec's cold start: the engine comes up halted until
        # the opening snapshot succeeds, and rule 9 requires an explicit human
        # resume out of any halt.
        session.add(EngineState(id=ENGINE_STATE_ID, halted=True))
        inserted += 1

    return inserted


def risk_limit_value(session: Session, key: str) -> Decimal | None:
    """The configured ceiling for ``key``, or ``None`` if there is no row.

    ``None`` means **no ceiling is configured**. It is never a number, and a
    caller must report it rather than substitute one — see the module
    docstring.
    """
    row = session.get(RiskLimit, key)
    if row is None:
        return None
    return row.value


def risk_limits(session: Session) -> dict[str, Decimal]:
    """Every configured ceiling, keyed by name.

    This is the **supported way to compare a computed risk against the
    limits**, and it is a bulk read rather than a filter for a reason:
    ``Money`` is TEXT on SQLite, so

        select(RiskLimit).where(RiskLimit.value < computed_risk)

    is a lexicographic comparison. Against the seeded ceilings it approves a
    35%-of-account trade over the 40% net-directional limit and reports the 7%
    per-trade limit as unbreached, because ``'7' > '10'`` is true as text.
    That query now raises ``MoneyComparisonError`` instead of answering; see
    ``corollary.db.types``. Read the rows here and compare as ``Decimal``.

    The table holds five rows. There is no version of this app where loading
    all of them is the expensive choice.

    A key that is absent is absent — this returns what exists and invents
    nothing, exactly as ``risk_limit_value`` returns ``None`` rather than a
    substituted number.
    """
    return {row.key: row.value for row in session.scalars(select(RiskLimit)).all()}
