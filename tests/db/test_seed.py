"""Shipped defaults for the three config tables and the engine singleton.

The seed exists so Phase 6's risk manager reads a table that already has the
CLAUDE.md ceilings in it. It is deliberately *not* a fallback: seeding writes
a row, and a key with no row reads back as ``None``. The frontend's
``riskLimitFor`` makes the same promise — it returns ``number | null``, and
null means "no ceiling configured", never a substituted number. Two order
tickets once invented different fallbacks (``?? 7`` and ``?? 0``) for exactly
this lookup, so the null path is tested here as well as the seeded one.
"""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from corollary.db.models import DataFeed, EngineState, NotificationRoute, RiskLimit
from corollary.db.seed import (
    DATA_FEED_DEFAULTS,
    NOTIFICATION_ROUTE_DEFAULTS,
    RISK_LIMIT_DEFAULTS,
    risk_limit_value,
    seed,
)

# --------------------------------------------------------------------- #
# The ceilings themselves
# --------------------------------------------------------------------- #


def test_defaults_are_the_claude_md_ceilings() -> None:
    assert RISK_LIMIT_DEFAULTS == {
        "max_risk_per_trade_pct": Decimal("7"),
        "max_daily_loss_pct": Decimal("20"),
        "max_concurrent_positions": Decimal("8"),
        "max_exposure_per_underlying": Decimal("25"),
        "max_net_directional_pct": Decimal("40"),
    }


def test_seed_writes_every_risk_limit_as_a_decimal(session: Session) -> None:
    seed(session)
    session.commit()
    session.expunge_all()

    for key, expected in RISK_LIMIT_DEFAULTS.items():
        row = session.get(RiskLimit, key)
        assert row is not None, key
        assert type(row.value) is Decimal
        assert row.value == expected


def test_seed_writes_the_three_feed_env_var_keys(session: Session) -> None:
    """CLAUDE.md names exactly three feed variables; the table mirrors them."""
    seed(session)
    session.commit()

    assert set(DATA_FEED_DEFAULTS) == {
        "ALPACA_OPTIONS_FEED",
        "ALPACA_STOCK_FEED_HISTORICAL",
        "ALPACA_STOCK_FEED_REALTIME",
    }
    assert DATA_FEED_DEFAULTS["ALPACA_STOCK_FEED_HISTORICAL"] == "sip"
    for key, expected in DATA_FEED_DEFAULTS.items():
        row = session.get(DataFeed, key)
        assert row is not None, key
        assert row.value == expected


def test_seed_writes_a_row_per_event_and_channel(session: Session) -> None:
    seed(session)
    session.commit()

    assert session.query(NotificationRoute).count() == len(NOTIFICATION_ROUTE_DEFAULTS)
    # PRD §10: these two are bell-only in the shipped default.
    for event in ("recommendations_ready", "strategy_promotion"):
        discord = session.get(NotificationRoute, (event, "discord"))
        assert discord is not None
        assert discord.enabled is False
        bell = session.get(NotificationRoute, (event, "bell"))
        assert bell is not None
        assert bell.enabled is True


def test_seed_writes_the_engine_state_singleton_halted(session: Session) -> None:
    """Cold start comes up halted until the opening snapshot succeeds."""
    seed(session)
    session.commit()

    state = session.query(EngineState).one()
    assert state.id == 1
    assert state.halted is True
    assert state.t0 is None


# --------------------------------------------------------------------- #
# Idempotence
# --------------------------------------------------------------------- #


def test_seed_reports_what_it_inserted(session: Session) -> None:
    inserted = seed(session)
    session.commit()
    expected = (
        len(RISK_LIMIT_DEFAULTS)
        + len(DATA_FEED_DEFAULTS)
        + len(NOTIFICATION_ROUTE_DEFAULTS)
        + 1  # engine_state
    )
    assert inserted == expected


def test_seed_is_idempotent(session: Session) -> None:
    seed(session)
    session.commit()
    before = _row_counts(session)

    assert seed(session) == 0
    session.commit()

    assert _row_counts(session) == before


def test_seed_never_overwrites_an_edited_limit(session: Session) -> None:
    """Settings is server-backed; re-seeding must not undo a human's change."""
    seed(session)
    session.commit()

    edited = session.get(RiskLimit, "max_risk_per_trade_pct")
    assert edited is not None
    edited.value = Decimal("3")
    session.commit()

    seed(session)
    session.commit()
    session.expunge_all()

    after = session.get(RiskLimit, "max_risk_per_trade_pct")
    assert after is not None
    assert after.value == Decimal("3")


def test_seed_fills_only_the_gaps(session: Session) -> None:
    session.add(RiskLimit(key="max_daily_loss_pct", value=Decimal("11")))
    session.commit()

    inserted = seed(session)
    session.commit()

    expected = (
        len(RISK_LIMIT_DEFAULTS)
        - 1
        + len(DATA_FEED_DEFAULTS)
        + len(NOTIFICATION_ROUTE_DEFAULTS)
        + 1
    )
    assert inserted == expected
    kept = session.get(RiskLimit, "max_daily_loss_pct")
    assert kept is not None
    assert kept.value == Decimal("11")


# --------------------------------------------------------------------- #
# A missing limit is None, never a number
# --------------------------------------------------------------------- #


def test_risk_limit_value_returns_none_when_no_row_exists(session: Session) -> None:
    assert risk_limit_value(session, "max_risk_per_trade_pct") is None


def test_risk_limit_value_returns_a_decimal_once_seeded(session: Session) -> None:
    seed(session)
    session.commit()
    value = risk_limit_value(session, "max_risk_per_trade_pct")
    assert type(value) is Decimal
    assert value == Decimal("7")


def test_risk_limit_value_returns_none_for_an_unknown_key(session: Session) -> None:
    seed(session)
    session.commit()
    assert risk_limit_value(session, "max_vibes_pct") is None


def _row_counts(session: Session) -> tuple[int, int, int, int]:
    return (
        session.query(RiskLimit).count(),
        session.query(DataFeed).count(),
        session.query(NotificationRoute).count(),
        session.query(EngineState).count(),
    )


@pytest.mark.parametrize(
    "event",
    [
        "order_filled",
        "order_rejected",
        "stop_loss_hit",
        "daily_loss_halt",
        "engine_error",
        "price_alert",
        "recommendations_ready",
        "strategy_promotion",
    ],
)
def test_every_prd_event_is_routed(session: Session, event: str) -> None:
    seed(session)
    session.commit()
    for channel in ("bell", "discord"):
        assert session.get(NotificationRoute, (event, channel)) is not None
