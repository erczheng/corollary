"""The five Phase-2 config/state tables.

Two of these tests matter more than the rest:

* ``test_risk_limit_value_round_trips_as_decimal`` — CLAUDE.md's money rule
  says SQLAlchemy columns are ``Numeric``, never ``Float``, "or the rule
  leaks at the database boundary". On SQLite that boundary is exactly where
  it leaks: SQLAlchemy's own ``Numeric`` warns that the dialect "does *not*
  support Decimal objects natively, and SQLAlchemy must convert from
  floating point". So the assertion here is on the **type**, not only the
  value — a float that happens to compare equal is still the bug.
* ``test_engine_state_rejects_a_second_row`` — the singleton is enforced by
  the database, not by callers remembering to pass ``id=1``.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Numeric, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from corollary.db.models import (
    AuditLog,
    DataFeed,
    EngineState,
    NotificationRoute,
    RiskLimit,
)
from corollary.db.types import Money, UtcDateTime

# --------------------------------------------------------------------- #
# risk_limit — the money boundary
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["7", "20", "0.1", "12.345678", "0.05"])
def test_risk_limit_value_round_trips_as_decimal(session: Session, raw: str) -> None:
    session.add(RiskLimit(key="probe", value=Decimal(raw)))
    session.commit()
    session.expunge_all()

    stored = session.get(RiskLimit, "probe")
    assert stored is not None
    assert type(stored.value) is Decimal
    assert stored.value == Decimal(raw)
    # str() rather than == alone: Decimal("0.1") == Decimal("0.1000") is True,
    # so equality would not notice a float detour that reformatted the value.
    assert str(stored.value) == raw


def test_risk_limit_value_is_never_a_float(session: Session) -> None:
    session.add(RiskLimit(key="probe", value=Decimal("0.1")))
    session.commit()
    session.expunge_all()

    stored = session.get(RiskLimit, "probe")
    assert stored is not None
    assert not isinstance(stored.value, float)


def test_money_column_refuses_a_float_outright() -> None:
    """A float must not be able to enter the column at all."""
    with pytest.raises(TypeError):
        Money().process_bind_param(7.5, sqlite_dialect())  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [7.5, 7, "7"])
def test_a_float_cannot_reach_the_database_through_the_orm(
    session: Session, bad: object
) -> None:
    """The ORM path is the one production uses, so it is tested separately.

    An ``int`` and a ``str`` are refused alongside the float. Both are exact,
    but accepting either means the column quietly has three input types and
    the next one added is the one that rounds.
    """
    session.add(RiskLimit(key="probe", value=bad))  # type: ignore[arg-type]
    with pytest.raises(StatementError) as caught:
        session.commit()
    assert isinstance(caught.value.orig, TypeError)


def test_money_column_stores_text_on_sqlite(session: Session) -> None:
    """SQLite has no exact decimal storage class.

    A NUMERIC-affinity column converts '7.5' to an IEEE double on the way in,
    which is the leak the money rule exists to prevent. ``typeof()`` reports
    the storage class actually used for the value in the file, so this fails
    if the column ever becomes REAL — the assertion the money rule needs.
    """
    session.add(RiskLimit(key="probe", value=Decimal("7.5")))
    session.commit()

    storage_class = session.execute(
        text("SELECT typeof(value) FROM risk_limit WHERE key = 'probe'")
    ).scalar_one()
    assert storage_class == "text"

    assert str(Money().compile(sqlite_dialect())) == "VARCHAR(40)"


def test_money_stores_one_spelling_per_value(session: Session) -> None:
    """A text column owes its CHECK constraints one canonical form.

    ``str(Decimal('1E+2'))`` is ``'1E+2'``, so the same number would land in
    the column two ways depending on how the Decimal was built — and the
    exponent form is not a number as far as ``ck_risk_limit_value`` is
    concerned. ``format(value, 'f')`` settles it.
    """
    session.add(RiskLimit(key="probe", value=Decimal("1E+2")))
    session.commit()

    stored_text = session.execute(
        text("SELECT value FROM risk_limit WHERE key = 'probe'")
    ).scalar_one()
    assert stored_text == "100"


def test_the_non_sqlite_branch_is_aspirational_not_covered() -> None:
    """Finding 4: the ``Numeric(20, 8)`` branch has never run for real.

    Nothing in this project opens a non-SQLite connection — the design spec
    chose SQLite with one writer — so this pins what the branch *would*
    compile to and the one way it is known to diverge, rather than implying
    it is supported. ``Decimal('7')`` comes back as ``Decimal('7.00000000')``
    there: equal, but a different string, and
    ``test_risk_limit_value_round_trips_as_decimal`` above asserts on
    ``str()``.

    A real port needs a server in the test matrix, a decision on that
    quantize, and a decision on whether ``Money``'s comparator should keep
    refusing ``<`` on a dialect that has genuine NUMERIC and does not need it.
    """
    pg = postgresql.dialect()
    impl = Money().load_dialect_impl(pg)
    assert isinstance(impl, Numeric)
    assert (impl.precision, impl.scale) == (20, 8)

    # The divergence, stated rather than discovered later.
    quantized = Decimal("7").quantize(Decimal("1.00000000"))
    assert quantized == Decimal("7")
    assert str(quantized) == "7.00000000" != str(Decimal("7"))

    # And the bind path hands the Decimal through untouched off SQLite, so
    # nothing rounds on the way in — only on the way back out of the server.
    assert Money().process_bind_param(Decimal("7"), pg) == Decimal("7")


def test_risk_limit_key_is_unique(session: Session) -> None:
    session.add(RiskLimit(key="dup", value=Decimal("1")))
    session.commit()
    session.add(RiskLimit(key="dup", value=Decimal("2")))
    with pytest.raises(IntegrityError):
        session.commit()


# --------------------------------------------------------------------- #
# data_feed
# --------------------------------------------------------------------- #


def test_data_feed_round_trips(session: Session) -> None:
    session.add(DataFeed(key="ALPACA_OPTIONS_FEED", value="indicative"))
    session.commit()
    session.expunge_all()

    stored = session.get(DataFeed, "ALPACA_OPTIONS_FEED")
    assert stored is not None
    assert stored.value == "indicative"


# --------------------------------------------------------------------- #
# notification_route
# --------------------------------------------------------------------- #


def test_notification_route_is_keyed_by_event_and_channel(session: Session) -> None:
    session.add(NotificationRoute(event="order_filled", channel="bell", enabled=True))
    session.add(
        NotificationRoute(event="order_filled", channel="discord", enabled=False)
    )
    session.commit()

    assert session.get(NotificationRoute, ("order_filled", "bell")) is not None
    assert session.get(NotificationRoute, ("order_filled", "discord")) is not None


def test_notification_route_rejects_a_duplicate_pair(session: Session) -> None:
    session.add(NotificationRoute(event="order_filled", channel="bell", enabled=True))
    session.commit()
    session.add(NotificationRoute(event="order_filled", channel="bell", enabled=False))
    with pytest.raises(IntegrityError):
        session.commit()


def test_notification_route_rejects_an_unknown_channel(session: Session) -> None:
    session.add(
        NotificationRoute(event="order_filled", channel="carrier_pigeon", enabled=True)
    )
    with pytest.raises(IntegrityError):
        session.commit()


# --------------------------------------------------------------------- #
# audit_log
# --------------------------------------------------------------------- #


def test_audit_log_records_previous_and_new_values(session: Session) -> None:
    at = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    session.add(
        AuditLog(
            at=at,
            category="risk",
            field="max_risk_per_trade_pct",
            previous_value="5",
            new_value="7",
        )
    )
    session.commit()
    session.expunge_all()

    entry = session.query(AuditLog).one()
    assert entry.id is not None
    assert entry.category == "risk"
    assert entry.field == "max_risk_per_trade_pct"
    assert entry.previous_value == "5"
    assert entry.new_value == "7"
    assert entry.at == at


def test_audit_log_spans_all_three_config_categories(session: Session) -> None:
    """PRD §8.7: one log rather than three."""
    at = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    for category, field in (
        ("risk", "max_daily_loss_pct"),
        ("feed", "ALPACA_STOCK_FEED_HISTORICAL"),
        ("notification", "engine_error.discord"),
    ):
        session.add(
            AuditLog(
                at=at,
                category=category,
                field=field,
                previous_value="before",
                new_value="after",
            )
        )
    session.commit()
    assert session.query(AuditLog).count() == 3


def test_audit_log_rejects_an_unknown_category(session: Session) -> None:
    session.add(
        AuditLog(
            at=datetime.now(timezone.utc),
            category="whatever",
            field="x",
            previous_value="a",
            new_value="b",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


# --------------------------------------------------------------------- #
# Timestamps — stored UTC (CLAUDE.md Conventions)
# --------------------------------------------------------------------- #


def test_timestamps_come_back_utc_aware(session: Session) -> None:
    session.add(
        AuditLog(
            at=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
            category="risk",
            field="max_daily_loss_pct",
            previous_value="15",
            new_value="20",
        )
    )
    session.commit()
    session.expunge_all()

    entry = session.query(AuditLog).one()
    assert entry.at.tzinfo is not None
    assert entry.at.utcoffset() == timedelta(0)


def test_eastern_timestamps_are_normalised_to_utc_on_the_way_in(
    session: Session,
) -> None:
    """Market data is Eastern; storage is UTC. The conversion is not optional."""
    eastern = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    session.add(
        AuditLog(
            at=eastern,
            category="risk",
            field="max_daily_loss_pct",
            previous_value="15",
            new_value="20",
        )
    )
    session.commit()
    session.expunge_all()

    entry = session.query(AuditLog).one()
    assert entry.at == eastern
    assert entry.at.utcoffset() == timedelta(0)
    assert entry.at.hour == 14  # 10:00 EDT is 14:00 UTC


def test_naive_datetimes_are_refused() -> None:
    """A naive datetime has no defensible interpretation at the boundary."""
    with pytest.raises(ValueError):
        UtcDateTime().process_bind_param(datetime(2026, 7, 15, 14, 0), sqlite_dialect())


# --------------------------------------------------------------------- #
# engine_state — the singleton
# --------------------------------------------------------------------- #


def test_engine_state_defaults_to_id_one_and_halted(session: Session) -> None:
    """Cold start comes up halted (spec) and never has to be told its own id."""
    session.add(EngineState())
    session.commit()
    session.expunge_all()

    state = session.query(EngineState).one()
    assert state.id == 1
    assert state.halted is True
    assert state.halted_reason is None
    assert state.halted_at is None
    assert state.t0 is None


def test_engine_state_rejects_a_second_row(session: Session) -> None:
    session.add(EngineState())
    session.commit()
    session.add(EngineState(id=2))
    with pytest.raises(IntegrityError):
        session.commit()


def test_engine_state_rejects_any_id_but_one(session: Session) -> None:
    """Enforced by the database, not by callers passing the right number."""
    session.add(EngineState(id=7))
    with pytest.raises(IntegrityError):
        session.commit()


def test_engine_state_records_a_halt(session: Session) -> None:
    halted_at = datetime(2026, 8, 7, 17, 58, tzinfo=timezone.utc)
    session.add(EngineState())
    session.commit()

    state = session.query(EngineState).one()
    state.halted = True
    state.halted_reason = "Alpaca stream silent for 94s"
    state.halted_at = halted_at
    state.t0 = datetime(2026, 8, 1, 13, 30, tzinfo=timezone.utc)
    session.commit()
    session.expunge_all()

    stored = session.query(EngineState).one()
    assert stored.halted_reason == "Alpaca stream silent for 94s"
    assert stored.halted_at == halted_at
    assert stored.t0 is not None
