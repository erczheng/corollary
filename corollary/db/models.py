"""The Phase-2 configuration, engine-state and ledger tables.

Nine of the ten tables the design spec lists. The tenth — ``notification`` —
belongs to step 8 and is deliberately absent.

Three config tables rather than one key/value table, because the risk limits
need an exact decimal and the other two do not; collapsing them would push
every limit through a text column and lose the type at the boundary. One
``audit_log`` spanning all three, per PRD §8.7: "one log rather than three"
— on a bad day the question is simply whether *anything* changed first.

**The ledger half — ``fill``, ``realized_trade``, ``mleg_group`` and
``mleg_leg`` — exists because Alpaca computes no realized P&L at all.** There
is no P&L field on any activity, no ``position_intent`` on a fill, and no leg
grouping on a position. Every figure the Activity page shows is therefore
Corollary's arithmetic over the broker's raw record, and these four tables are
where that record and that arithmetic are kept. ``fill`` exists in particular
because ``page_size`` on the activities endpoint maxes at 100 and re-fetching
all history per request is untenable.

**Money on these tables is signed, and that changes what SQL may do with it.**
``Money`` is TEXT on SQLite (see ``corollary.db.types``), so SQL compares it
lexicographically and every ordering, aggregate and arithmetic operation
raises ``MoneyComparisonError`` instead of answering. On ``risk_limit`` that
cost is small — five rows, read whole. On ``realized_trade`` it has a
second-order consequence worth knowing before step 7: **lifetime realized
P&L, average win, average loss and win rate are all Python folds over loaded
rows**, because ``SUM``, ``AVG``, ``MIN`` and ``ORDER BY`` are unavailable to
every one of them. That bounds how many rows the Activity page can afford to
load, and the bound is real rather than an optimisation to defer.
"""

from decimal import Decimal
from datetime import datetime
from typing import Any, Mapping, NamedTuple

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    validates,
)

from corollary.db.types import Money, UtcDateTime

__all__ = [
    "ACCOUNT_MODES",
    "AuditLog",
    "Base",
    "CLOSE_KINDS",
    "DataFeed",
    "EngineState",
    "ENGINE_STATE_ID",
    "FILL_SIDES",
    "Fill",
    "LimitRange",
    "MlegGroup",
    "MlegLeg",
    "NotificationRoute",
    "POSITION_INTENTS",
    "RealizedTrade",
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

#: The two books, spelled exactly as the frontend's ``AccountMode`` union in
#: ``web/src/lib/types.ts`` — ``type AccountMode = 'paper' | 'cash'``. Same
#: standard ``AUDIT_CATEGORIES`` holds itself to: a row renders without
#: translation, so there is no mapping table to get wrong.
ACCOUNT_MODES = ("paper", "cash")

#: **Three values, not two.** The probe against the real paper account
#: observed ``buy`` ×7, ``sell_short`` ×4 and ``sell`` ×1.
#:
#: ``sell_short`` opens a short (STO) and ``sell`` closes a long (STC), so
#: ``side`` does separate those two. It does *not* separate ``buy``: BTO and
#: BTC are both ``buy``. Two of the four actions are indistinguishable from
#: ``side`` alone, which is the entire reason ``position_intent`` is a
#: separate column rather than something derived from this one.
FILL_SIDES = ("buy", "sell", "sell_short")

#: Alpaca's own spelling, so a response field stores without translation.
#: This lives on the **order**, not the fill — which is why every ledger row
#: needs a fill→order join, and why ``fill.position_intent`` is nullable.
POSITION_INTENTS = (
    "buy_to_open",
    "buy_to_close",
    "sell_to_open",
    "sell_to_close",
)

#: The four ways a lot stops existing. ``fill`` is a closing trade; the other
#: three are option events, which arrive as their own activity types
#: (``OPEXP``, ``OPEXC``, ``OPASN``) and are not fills at all. Phase 1's
#: ledger had four statuses and no concept of expiry, which left the most
#: common way an option position ends with no render path — and it is a
#: realized loss.
CLOSE_KINDS = ("fill", "expiry", "exercise", "assignment")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _in_list_or_null(column: str, values: tuple[str, ...]) -> str:
    """``_in_list``, spelled so a NULL is admitted on purpose rather than by luck.

    SQLite already satisfies a CHECK that evaluates to NULL, so bare
    ``_in_list`` would admit NULL anyway. Writing the branch out means the
    next reader does not have to know that, and means removing the
    nullability is a visible edit rather than an invisible one.
    """
    return f"{column} IS NULL OR {_in_list(column, values)}"


#: Same width as ``corollary.db.types._MONEY_TEXT_WIDTH``. Repeated as a
#: literal rather than imported for the same reason the migration repeats it:
#: a CHECK constraint is part of the schema, and the schema should not change
#: shape because a private constant moved.
_MONEY_TEXT_WIDTH = 40


def _money_shape(column: str, *, nullable: bool = False) -> str:
    """A CHECK that a **signed** money column holds a plain decimal string.

    This is a *shape* constraint, not a policy one, and the distinction is
    the whole reason it is not ``ck_risk_limit_value``. That constraint
    rejects anything not starting with a digit, because a negative or zero
    risk ceiling silently stops the bot trading. Copied onto ``pnl`` it would
    reject **every losing trade**, which is the normal case. So this one
    admits a leading ``-``, admits zero (an ``OPEXP`` closes at zero; a
    scratch trade nets zero), and puts no bound on magnitude — a P&L has no
    defensible ceiling and inventing one would reject a real broker row.

    What it does refuse is text that is not a number, because
    ``Money.process_result_value`` calls ``Decimal(value)`` on the way out and
    ``Decimal('NaN')`` *succeeds*. What a stored NaN then does is worth
    stating exactly, because the obvious guess is wrong and the truth is a
    stronger reason to keep this CHECK:

    * ``Decimal('NaN') > 0`` and ``< 0`` **raise** ``InvalidOperation`` --
      they do *not* answer False the way ``float('nan')`` does. So
      ``sorted()`` over the loaded rows raises too, and "biggest loser"
      becomes a 500 on the Activity page rather than a wrong ordering.
    * ``Decimal('NaN') == 0`` is **False**, silently -- equality is the one
      comparison that does not raise.
    * ``sum()`` over rows containing one **folds the whole total to NaN**,
      silently. Lifetime P&L, average win and average loss all go with it.
    * ``Decimal('Infinity')`` is worse, because arithmetic on it propagates
      silently rather than raising at all.

    So the failure is a mix of loud and silent, and the silent half is the
    dangerous one. Nothing on the read path catches either value --
    ``Decimal(value)`` accepts both, and ``wire.as_decimal('NaN')`` returns
    a NaN quite happily -- so this CHECK is the only guard, and it sits at
    the write boundary on purpose: the cheapest place to refuse a value is
    before it is stored, not on every fold that later reads it.

    Rejected: ``''``, ``'-'``, ``'+7'``, ``'--7'``, ``' 7'``, ``'7.'``,
    ``'7.5.5'``, ``'1E+2'``, ``'NaN'``, ``'Infinity'``, ``'-Infinity'``.
    Accepted: ``'0'``, ``'0.00'``, ``'7'``, ``'-7'``, ``'-0.07'``,
    ``'123.45'``.

    ``'1E+2'`` is refused rather than parsed because ``Money`` binds with
    ``format(value, 'f')`` and therefore never writes an exponent; a value in
    that form arrived some other way, and one canonical spelling per number is
    what a text column owes its own constraints.
    """
    # The magnitude, with any sign stripped, so every test below reads as a
    # statement about digits rather than about signs.
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


def _positive_count(column: str) -> str:
    """A CHECK that a count column holds a whole number above zero.

    ``typeof()`` is not belt-and-braces here. SQLite applies INTEGER
    *affinity*, which converts only when the conversion is lossless, so
    ``INSERT ... VALUES (1.5)`` into an INTEGER column stores a REAL and
    reports no error. Options trade in whole contracts and a leg ratio in
    simplest form is a whole number by definition — a fractional one is not a
    spread, it is a parse error.

    ``> 0`` because these columns store the **normalised, unsigned**
    convention. A non-trade activity arrives with a signed ``qty`` and no
    ``side``; ingestion turns the sign into a side and keeps the magnitude. A
    negative value here means that normalisation did not happen, which is how
    a matcher books an exercise backwards.
    """
    return f"typeof({column}) = 'integer' AND {column} > 0"


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
    * ``Decimal('NaN')`` — checked before the ordering comparisons, because
      ``Decimal`` *raises* ``InvalidOperation`` on ``NaN < 1`` rather than
      answering. Unguarded that is an exception in the risk manager, not a
      passed check — but an exception mid-approval is its own failure, so it
      is refused here rather than thrown later.
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


# --------------------------------------------------------------------- #
# The ledger — the broker's raw record, and Corollary's arithmetic over it
# --------------------------------------------------------------------- #


class Fill(Base):
    """One account activity, normalised. The raw record everything else folds.

    This table exists for a mundane reason and a load-bearing one. The mundane
    one: ``page_size`` on ``/v2/account/activities`` maxes at 100, so
    re-fetching all history to answer one request is untenable. The
    load-bearing one: Alpaca computes **no realized P&L at all**, so every
    figure the Activity page shows is arithmetic over these rows, and
    arithmetic needs its inputs kept.

    "Fill" is the table's name rather than its full contents, and the gap is
    deliberate. A ``FILL`` activity is one kind of row here; an ``OPEXP``,
    ``OPEXC`` or ``OPASN`` is another, and those are not fills — they are how
    the *most common* way an option position ends arrives. Ingestion
    discriminates on ``activity_type`` and writes both branches here, because
    the matcher wants one sequence of lot movements, not two.

    **Two things are normalised on the way in, and neither is a normalisation
    the broker does for you:**

    * ``qty`` is stored **unsigned**, with the direction in ``side``. A
      ``FILL`` arrives that way already; a non-trade activity arrives with a
      *signed* ``qty`` and no ``side`` at all (``-2`` when contracts leave a
      long, ``+2`` when a short is assigned away). Ingestion turns the sign
      into a side and keeps the magnitude, so ``ck_fill_qty`` can insist on a
      positive count — a negative value here means that step did not happen,
      which is how a matcher books an exercise backwards.
    * ``price`` is the money for the lot movement, which on an option event is
      **not on the event row**. ``OPEXC`` and ``OPASN`` carry
      ``net_amount: "0"``; the strike arrives on the paired ``OPTRD`` or is
      parsed out of the OCC symbol. A row written straight from ``net_amount``
      books every exercise as a total loss.

    ``order_id``, ``group_id`` and ``position_intent`` are all nullable, and
    each absence means something specific rather than "missing data" — see the
    column comments.
    """

    __tablename__ = "fill"
    __table_args__ = (
        # Idempotent ingestion is this constraint and nothing else. Ingestion
        # runs on startup and on an interval, re-pulling an overlapping window
        # every time; without the UNIQUE the second pass doubles every fill
        # and the matcher books every position twice.
        UniqueConstraint("activity_id", name="uq_fill_activity_id"),
        CheckConstraint(_in_list("account", ACCOUNT_MODES), name="ck_fill_account"),
        CheckConstraint(_in_list("side", FILL_SIDES), name="ck_fill_side"),
        CheckConstraint(
            _in_list_or_null("position_intent", POSITION_INTENTS),
            name="ck_fill_position_intent",
        ),
        CheckConstraint(_positive_count("qty"), name="ck_fill_qty"),
        CheckConstraint(_money_shape("price"), name="ck_fill_price"),
        # Ingestion resumes from the newest row it already holds, per book.
        Index("ix_fill_account_at", "account", "at"),
        # The matcher's open-lot queue is per contract symbol.
        Index("ix_fill_symbol", "symbol"),
        # The fill->order join for `position_intent`, and fee attribution.
        Index("ix_fill_order_id", "order_id"),
        # Fee attribution for the option events, which have no `order_id`.
        Index("ix_fill_group_id", "group_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(8), nullable=False)
    #: The activity id, **composite**: a 17-digit timestamp, ``::``, then a
    #: UUID — ``20260910131125598::68cda3e9-…``, 55 characters. Not a bare
    #: UUID, and a column sized for one would truncate it.
    #:
    #: It is the **idempotency key** — ``uq_fill_activity_id`` above rests on
    #: it — and the **resume cursor Alpaca itself takes**, since ``page_token``
    #: is the last row's id. It is **not** a chronological **sort** key.
    #: ``ORDER BY activity_id`` does not sequence fills: only the 17-digit
    #: stamp half orders, stamps repeat, and the UUID half then breaks the tie
    #: arbitrarily. On this account's real data the first pair it inverts is
    #: the two legs of one vertical — wrong lot order into the FIFO matcher,
    #: wrong ``open_price`` out of it. Sequence by :attr:`at`, per contract
    #: symbol, which is what ``ix_fill_account_at`` and ``ix_fill_symbol``
    #: exist for. The measurement and the full reasoning live on
    #: :attr:`~corollary.engine.execution.interface._ActivityBase.stamp`.
    activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The **leg's** order id on an ``mleg`` order, not the parent's — each leg
    #: is a full order object with its own id, and the parent id appears
    #: nowhere on the fill. Reaching the parent is a two-hop join through
    #: ``GET /v2/orders?nested=true``. NULL because a non-trade activity has no
    #: ``order_id`` at all.
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: *"ID used to link activities who share a sibling relationship"* — the
    #: only linkage a non-trade activity gets, and therefore the only way to
    #: attribute a fee to an expiry, an assignment or an exercise. It is also
    #: the field that would pair the two rows of an option event if Alpaca
    #: really does reuse one ``id`` across both.
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: OCC for a contract, the bare ticker for the paired ``OPTRD`` on the
    #: underlying. Wide enough for an adjusted root's numeric suffix.
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    #: NULL means **not known**, which is a state the broker really produces:
    #: intent lives on the order, so the join can fail, and a non-trade
    #: activity has no order to join to. Guessing would defeat the column —
    #: ``buy`` being both BTO and BTC is the precise ambiguity it resolves.
    position_intent: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Contracts, whole and unsigned. See the class docstring.
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class RealizedTrade(Base):
    """One closed round trip, as the FIFO matcher emits it.

    A row per matched slice rather than per position: a closing fill that
    consumes two opening lots at different prices realises two trades, because
    each had its own basis and averaging them loses the only thing the row is
    for.

    ``pnl`` is **signed**, and that is what changes what SQL may do with it.
    Being ``Money`` it is TEXT on SQLite, so ``ORDER BY pnl`` is
    lexicographic: ``'-410' < '-7' < '120'`` puts the biggest loss first only
    by coincidence of the digits, and with −7 and −41 the order inverts. So
    the type raises ``MoneyComparisonError`` rather than answering, and
    **"biggest loser" is a Python sort over loaded rows.** The consequence
    worth knowing before the Activity page is built: lifetime realized P&L,
    average win, average loss and win rate are *all* Python folds, because
    ``SUM``, ``AVG``, ``MIN`` and ``ORDER BY`` are unavailable to every one of
    them. That bounds how many rows the page can afford to load, and the bound
    is real rather than an optimisation to defer.

    The money CHECKs here are ``_money_shape``, not ``ck_risk_limit_value``,
    and the difference is the whole point: a risk ceiling may not be negative
    or zero, whereas a P&L is negative on every losing trade and zero on a
    scratch. What ``_money_shape`` still refuses is text that is not a number,
    because ``Decimal('NaN')`` *succeeds* on the way out. A stored NaN then
    **raises** ``InvalidOperation`` on any ordering comparison — so the
    "biggest loser" sort fails rather than mis-sorting — while ``sum()``
    folds lifetime P&L to NaN *silently*. See ``_money_shape`` for why the
    silent half is the one that matters.
    """

    __tablename__ = "realized_trade"
    __table_args__ = (
        CheckConstraint(
            _in_list("account", ACCOUNT_MODES), name="ck_realized_trade_account"
        ),
        CheckConstraint(
            _in_list("close_kind", CLOSE_KINDS), name="ck_realized_trade_close_kind"
        ),
        CheckConstraint(_positive_count("qty"), name="ck_realized_trade_qty"),
        CheckConstraint(
            _money_shape("open_price"), name="ck_realized_trade_open_price"
        ),
        CheckConstraint(
            _money_shape("close_price"), name="ck_realized_trade_close_price"
        ),
        CheckConstraint(_money_shape("pnl"), name="ck_realized_trade_pnl"),
        CheckConstraint(
            _money_shape("pnl_pct", nullable=True), name="ck_realized_trade_pnl_pct"
        ),
        # The Activity page reads one book, newest close first. That ordering
        # is on `closed_at`, a timestamp, which orders fine — it is only the
        # money columns SQL may not sort.
        Index("ix_realized_trade_account_closed_at", "account", "closed_at"),
        Index("ix_realized_trade_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The opening lot's fill time, from ``fill`` — not from the position,
    #: which carries no open date at all.
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: Contracts in this slice, whole and positive.
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    open_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: NOT NULL, including on an expiry: an ``OPEXP`` closes at **zero**, which
    #: is a price rather than an absence. Zero is a full loss on a long and the
    #: full credit kept on a short, and the matcher has to state which.
    close_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Signed. ``(close − open) × qty × multiplier`` on a long, inverted on a
    #: short, with ``multiplier`` read **per contract** from the contracts
    #: endpoint — never assumed to be 100, because an adjusted contract's
    #: deliverable is not 100 shares and a wrong multiplier here is a wrong
    #: max-loss figure in the risk manager later.
    pnl: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Denominated on cost basis — ``open_price × qty × multiplier`` — so a
    #: short's basis is the credit received, matching the frontend's negative
    #: ``openUnitValue``. NULL where that basis is zero: a percentage of
    #: nothing is not zero percent, and reporting 0% would read as a flat
    #: trade.
    pnl_pct: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    #: How the lot stopped existing. Three of the four are not fills at all;
    #: Phase 1's ledger had no concept of expiry, which left the most common
    #: ending with no render path — and it is a realized loss.
    close_kind: Mapped[str] = mapped_column(String(16), nullable=False)


class MlegGroup(Base):
    """One historical ``mleg`` order, standing as evidence that legs belong together.

    Decision 5: group multi-leg **only where an mleg order proves it**. Alpaca
    returns a position per OCC symbol with no leg grouping, no order linkage
    and no open date, so a four-leg iron condor is four rows; this table is the
    record that says which four. Nothing is inferred — a heuristic on
    same-underlying/same-expiry/offsetting-sides is wrong on iron condors,
    wrong on ratio spreads, and wrong on any two unrelated positions that
    happen to rhyme.

    The stakes are rule 4's rather than cosmetic: a short leg rendered alone
    reports as an *undefined-risk* naked short, so a defined-risk credit spread
    would state the wrong risk class and be sized against the wrong number.

    A group here **proposes** a logical position; it does not assert one still
    exists. Liveness is a question about today's positions — every leg still
    held, on the expected side, in quantities consistent with the ratios — and
    is answered by the grouper, not stored. A partially-closed spread is not
    live, and its survivors fall back to ungrouped rows, because that spread
    genuinely no longer exists.
    """

    __tablename__ = "mleg_group"
    __table_args__ = (
        # One parent mleg order proposes exactly one group. Which also makes
        # re-ingestion idempotent for groups, and supplies the index the
        # grouper's lookup-by-order_id needs.
        UniqueConstraint("order_id", name="uq_mleg_group_order_id"),
        CheckConstraint(
            _in_list("account", ACCOUNT_MODES), name="ck_mleg_group_account"
        ),
        CheckConstraint(
            _money_shape("net_price", nullable=True), name="ck_mleg_group_net_price"
        ),
        Index("ix_mleg_group_account_opened_at", "account", "opened_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(8), nullable=False)
    #: The **parent** order id, reached by the two-hop join — a fill carries
    #: its leg's id, and the parent appears nowhere on it. A one-hop
    #: implementation writes no groups at all, and does so silently.
    order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: Signed, and the sign *is* the direction: debit long, credit short.
    #: ``_money_shape`` admits the leading minus for exactly this reason. NULL
    #: means the direction could not be established, and NULL is what must then
    #: be reported — a group labelled debit or credit off a substituted number
    #: is the invented-value failure in the one place where the invented value
    #: picks the risk class.
    net_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    #: Delete-orphan rather than a nullable parent: a leg with no group is
    #: grouping evidence pointing at nothing, which is worse than no evidence.
    #: The foreign key carries ``ON DELETE CASCADE`` as well, so raw SQL cannot
    #: leave an orphan either.
    legs: Mapped[list["MlegLeg"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
    )


class MlegLeg(Base):
    """One leg of an ``mleg`` order, as the order itself reported it.

    ``(group_id, symbol)`` is the primary key, which makes a duplicate symbol
    within one group a loud error rather than a quiet merge. That is right
    because leg ratios arrive in simplest form — Alpaca requires the GCD across
    a group's ``ratio_qty`` values to be 1 — so two legs on one symbol would
    already have been combined into one. A second row for the same symbol is a
    parse error.

    ``position_intent`` is **NOT NULL here and nullable on** ``Fill``, and the
    asymmetry is deliberate: a leg is read straight off the order object, where
    the field is always present, whereas a fill's intent comes from a join to
    that order and the join can fail.

    There is no ``account`` column. A leg inherits its book from its group,
    because a denormalised copy is a second answer to "which book is this in",
    and two answers is how a cash leg ends up under a paper group.
    """

    __tablename__ = "mleg_leg"
    __table_args__ = (
        CheckConstraint(_in_list("side", FILL_SIDES), name="ck_mleg_leg_side"),
        CheckConstraint(
            _in_list("position_intent", POSITION_INTENTS),
            name="ck_mleg_leg_position_intent",
        ),
        CheckConstraint(_positive_count("ratio"), name="ck_mleg_leg_ratio"),
    )

    group_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("mleg_group.id", ondelete="CASCADE"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: In simplest form, so a whole number by definition. A fractional ratio is
    #: not a spread, it is a parse error — and SQLite's INTEGER *affinity* would
    #: store ``1.5`` as a REAL without complaint, which is why
    #: ``_positive_count`` asserts the storage class rather than trusting it.
    ratio: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    position_intent: Mapped[str] = mapped_column(String(16), nullable=False)

    group: Mapped["MlegGroup"] = relationship(back_populates="legs")
