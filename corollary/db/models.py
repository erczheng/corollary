"""The Phase-2 configuration and engine-state tables.

Five of the ten tables the design spec lists. The other five — ``fill``,
``realized_trade``, ``mleg_group``, ``mleg_leg`` and ``notification`` — belong
to later steps and are deliberately absent.

Three config tables rather than one key/value table, because the risk limits
need an exact decimal and the other two do not; collapsing them would push
every limit through a text column and lose the type at the boundary. One
``audit_log`` spanning all three, per PRD §8.7: "one log rather than three"
— on a bad day the question is simply whether *anything* changed first.
"""

from decimal import Decimal
from datetime import datetime
from typing import Any, Mapping, NamedTuple

from sqlalchemy import Boolean, CheckConstraint, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from corollary.db.types import Money, UtcDateTime

__all__ = [
    "AuditLog",
    "Base",
    "DataFeed",
    "EngineState",
    "ENGINE_STATE_ID",
    "LimitRange",
    "NotificationRoute",
    "RiskLimit",
    "RISK_LIMIT_ABSOLUTE_MAX",
    "RISK_LIMIT_RANGES",
    "validate_risk_limit",
]

#: The only row ``engine_state`` may ever hold. Enforced by a CHECK
#: constraint below rather than by callers passing the right number.
ENGINE_STATE_ID = 1

#: PRD §8.7's three audited categories, matching the frontend's
#: ``AuditCategory`` union so a row renders without translation.
AUDIT_CATEGORIES = ("risk", "feed", "notification")

#: PRD §10's two channels. ``Notifier`` gains more later; adding one is a
#: migration, which is the point — a typo must not create a third silently.
NOTIFICATION_CHANNELS = ("bell", "discord")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    """Declarative base. ``Base.metadata`` is Alembic's target."""


# --------------------------------------------------------------------- #
# What a risk ceiling is allowed to be
# --------------------------------------------------------------------- #


class LimitRange(NamedTuple):
    """The inclusive range one ceiling may be set to.

    ``whole`` marks a count rather than a percentage: 8.5 concurrent
    positions is not a setting, it is a typo.
    """

    low: Decimal
    high: Decimal
    whole: bool


#: Per-limit ranges. These mirror ``web/src/lib/settings.ts``
#: ``validateRiskLimit`` and the ``min``/``max`` on each entry of
#: ``RISK_LIMITS``, and the mirroring is checked by
#: ``test_the_ranges_match_the_settings_page``.
#:
#: The server is authoritative — CLAUDE.md rule 4 is explicit that a value
#: arriving from the client is never trusted — but it must not silently
#: *disagree* with the control the human types into either. A field that
#: accepts a number the server then rejects is a bug report whichever way
#: round it happens.
RISK_LIMIT_RANGES: Mapping[str, LimitRange] = {
    "max_risk_per_trade_pct": LimitRange(Decimal("1"), Decimal("25"), False),
    "max_daily_loss_pct": LimitRange(Decimal("1"), Decimal("50"), False),
    "max_concurrent_positions": LimitRange(Decimal("1"), Decimal("20"), True),
    "max_exposure_per_underlying": LimitRange(Decimal("5"), Decimal("100"), False),
    "max_net_directional_pct": LimitRange(Decimal("5"), Decimal("100"), False),
}

#: Exclusive upper bound for a key that has no entry above.
#:
#: Unknown keys are tolerated — ``seed.risk_limit_value`` answers ``None`` for
#: one and says so — but "no configured range" is not "no rules". Every limit
#: this app has is a percentage or a count, so three integer digits is a
#: generous structural bound rather than a guess at a policy. It is the same
#: bound the CHECK constraint enforces, expressed the only way SQL can
#: express it on a text column; a future limit measured in dollars means
#: changing both together.
RISK_LIMIT_ABSOLUTE_MAX = Decimal("1000")

#: The same rules again, as SQL, because rule 4 says the engine enforces
#: limits and Python is not the only way into this file.
#:
#: It reads oddly because it has to. ``value`` is TEXT (see
#: ``corollary.db.types.Money``), so ``value > 0`` would be a lexicographic
#: comparison and ``CAST(value AS REAL)`` would put an IEEE double in the one
#: place this schema exists to keep floats out of. What is left is the shape
#: of the text: it must start with a digit (no ``-7``, no ``+7``, no
#: ``Infinity``, no ``NaN``), contain nothing but digits and at most one
#: point, contain at least one non-zero digit (no ``0``, no ``0.00``), not end
#: on the point, and carry at most three digits before it (no ``999999``).
#: Those are exactly the five values the audit round-tripped, and each one is
#: a distinct way to disable the risk manager.
_RISK_LIMIT_VALUE_CHECK = (
    "typeof(value) = 'text' "
    "AND length(value) BETWEEN 1 AND 24 "
    "AND value GLOB '[0-9]*' "
    "AND NOT value GLOB '*[^0-9.]*' "
    "AND value GLOB '*[1-9]*' "
    "AND NOT value GLOB '*.' "
    "AND length(value) - length(replace(value, '.', '')) <= 1 "
    "AND (CASE WHEN instr(value, '.') = 0 "
    "THEN length(value) ELSE instr(value, '.') - 1 END) <= 3"
)


def validate_risk_limit(key: str | None, value: Decimal) -> None:
    """Raise ``ValueError`` unless ``value`` is a usable ceiling for ``key``.

    Each rejected value below round-tripped without complaint before this
    existed, and each one disables the risk manager in its own way:

    * ``Decimal('Infinity')`` — every ceiling check passes, so there is no
      limit at all.
    * ``Decimal('NaN')`` — every comparison against it is false, same result.
      (It is checked before the ordering comparisons because ``Decimal``
      *raises* ``InvalidOperation`` on ``NaN < 1`` rather than answering.)
    * ``Decimal('-7')`` and ``Decimal('0')`` — every trade is over the
      ceiling, so the bot silently never trades.
    * ``Decimal('999999')`` — a 999999% ceiling is not a ceiling.

    ``key`` may be ``None`` while an object is half-built; the universal rules
    still apply and the per-limit range is checked as soon as both are known.

    A non-``Decimal`` is *not* this function's business: the column type owns
    the money rule and ``Money.process_bind_param`` raises on anything else.
    Two owners for one rule is how they end up disagreeing.
    """
    if not isinstance(value, Decimal):
        return

    if not value.is_finite():
        raise ValueError(
            f"risk limit {key or '(unnamed)'} must be a finite number, got {value} — "
            "an infinite or undefined ceiling is no ceiling at all"
        )
    if value <= 0:
        raise ValueError(
            f"risk limit {key or '(unnamed)'} must be greater than zero, got {value} — "
            "a zero or negative ceiling rejects every trade"
        )

    limit_range = RISK_LIMIT_RANGES.get(key or "")
    if limit_range is None:
        if value >= RISK_LIMIT_ABSOLUTE_MAX:
            raise ValueError(
                f"risk limit {key or '(unnamed)'} must be below "
                f"{RISK_LIMIT_ABSOLUTE_MAX}, got {value}"
            )
        return

    if limit_range.whole and value != value.to_integral_value():
        raise ValueError(
            f"risk limit {key} counts positions and must be a whole number, "
            f"got {value}"
        )
    if not (limit_range.low <= value <= limit_range.high):
        raise ValueError(
            f"risk limit {key} must be between {limit_range.low} and "
            f"{limit_range.high}, got {value}"
        )


class RiskLimit(Base):
    """One risk ceiling, as edited in Settings and enforced by the engine.

    ``value`` is exact. It is the single column in this file where CLAUDE.md's
    money rule bites — see ``corollary.db.types.Money`` for why a plain
    ``Numeric`` would not hold on SQLite, and why comparing this column in SQL
    raises rather than answering.

    A key with no row means **no ceiling is configured**, not zero and not a
    default. Nothing in this package invents one; ``seed.risk_limit_value``
    returns ``None`` and says so.

    What the value may *be* is enforced twice, because rule 4 says the engine
    enforces limits and a caller can only reach one of the two layers:
    ``validate_risk_limit`` on assignment, and ``ck_risk_limit_value`` for
    anything arriving as raw SQL.
    """

    __tablename__ = "risk_limit"
    __table_args__ = (
        CheckConstraint(_RISK_LIMIT_VALUE_CHECK, name="ck_risk_limit_value"),
    )

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Decimal] = mapped_column(Money, nullable=False)

    @validates("key", "value")
    def _validate(self, field: str, incoming: Any) -> Any:
        """Validate on both fields, so neither assignment order slips through.

        ``RiskLimit(key=..., value=...)`` sets attributes in keyword order,
        and a caller building the object field by field may set either first.
        Checking only ``value`` would mean ``limit.value = Decimal('99')``
        before ``limit.key = 'max_risk_per_trade_pct'`` validated against no
        range at all.
        """
        if field == "value":
            validate_risk_limit(self.key, incoming)
        elif self.value is not None:
            validate_risk_limit(incoming, self.value)
        return incoming


class DataFeed(Base):
    """One of the three ``ALPACA_*_FEED`` settings.

    ``key`` is the environment variable name itself — ``ALPACA_OPTIONS_FEED``
    and friends — so the stored row names exactly what it configures. The
    values are still read only inside ``data/providers/alpaca.py``; this table
    is what Settings edits, not a second place the provider is chosen.
    """

    __tablename__ = "data_feed"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(32), nullable=False)


class NotificationRoute(Base):
    """Whether one event reaches one channel.

    A row per (event, channel) pair rather than a row per event with a boolean
    column per channel: adding SMS later is then a data change, not a schema
    change, and the audit log's ``field`` is already ``event.channel``.
    """

    __tablename__ = "notification_route"
    __table_args__ = (
        CheckConstraint(
            _in_list("channel", NOTIFICATION_CHANNELS),
            name="ck_notification_route_channel",
        ),
    )

    event: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AuditLog(Base):
    """Every configuration change, newest first, each row carrying what it replaced.

    ``previous_value`` and ``new_value`` are text for every category. The
    audit log records *what a human changed*, and rendering "5" → "7" needs no
    arithmetic; typing it as a decimal would also mean a feed change had no
    home in the same log.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            _in_list("category", AUDIT_CATEGORIES), name="ck_audit_log_category"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The stored key — ``max_risk_per_trade_pct``, ``ALPACA_OPTIONS_FEED``,
    #: or ``engine_error.discord`` — never the label it was worded with on the
    #: day. The frontend resolves it to a label at render time.
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_value: Mapped[str] = mapped_column(String(128), nullable=False)
    new_value: Mapped[str] = mapped_column(String(128), nullable=False)


class EngineState(Base):
    """The engine's halt state and its start marker. Exactly one row, ever.

    The singleton is a CHECK constraint, so a second row is a database error
    rather than a subtle one: two rows would mean two answers to "is the
    engine halted", and rule 9 says recovery from a halt requires an explicit
    human resume — a second row is a way to resume by accident.

    ``halted`` defaults to True. Cold start comes up halted until the opening
    snapshot succeeds; coming up running and discovering the connection is
    down afterwards is the wrong order.

    ``t0`` is the first-ever-start marker the equity curve is drawn against.
    It is nullable because it has not been written yet, and once written it is
    never rewritten.
    """

    __tablename__ = "engine_state"
    __table_args__ = (
        CheckConstraint(f"id = {ENGINE_STATE_ID}", name="ck_engine_state_singleton"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, default=ENGINE_STATE_ID
    )
    halted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    halted_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    halted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    t0: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
