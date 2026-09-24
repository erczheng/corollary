"""Alembic, wired for the first time.

The load-bearing test is ``test_upgrade_head_matches_the_models``: it
autogenerates a diff between the migrated database and ``Base.metadata`` and
requires it to be empty. Without it, a hand-written migration and the models
drift and nothing notices until a query fails at runtime.

The seed values are duplicated between the migration and ``seed.py`` on
purpose — a migration is a frozen record of what the schema was on the day,
so it must not import defaults that can change underneath it. The duplication
is safe only because ``test_migration_seed_matches_the_seed_module`` fails
the moment the two disagree.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect
from sqlalchemy.orm import Session

from corollary.db.models import Base, DataFeed, EngineState, NotificationRoute, RiskLimit
from corollary.db.seed import (
    DATA_FEED_DEFAULTS,
    NOTIFICATION_ROUTE_DEFAULTS,
    RISK_LIMIT_DEFAULTS,
)
from corollary.db.session import create_db_engine, sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

EXPECTED_TABLES = {
    # 0001 — config, audit, engine state
    "risk_limit",
    "data_feed",
    "notification_route",
    "audit_log",
    "engine_state",
    # 0002 — the ledger
    "fill",
    "realized_trade",
    "mleg_group",
    "mleg_leg",
    # 0004 — the refusals, kept so a gap in P&L can state its cause
    "ledger_rejection",
    # 0005 -- the bell's rows, and every delivery attempt behind them
    "notification",
    "notification_delivery",
}


def _config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def migrated(db_path: Path) -> Engine:
    """A database built by ``alembic upgrade head``, not by create_all()."""
    url = sqlite_url(db_path)
    command.upgrade(_config(url), "head")
    return create_db_engine(url)


def test_alembic_ini_exists() -> None:
    assert ALEMBIC_INI.is_file()


def test_upgrade_head_creates_every_table_the_models_declare(
    migrated: Engine,
) -> None:
    tables = set(inspect(migrated).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_0005_downgrades_to_0004_and_back(db_path: Path) -> None:
    """The notification tables come and go alone; nothing earlier is touched."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert "notification" not in tables
    assert "notification_delivery" not in tables
    assert EXPECTED_TABLES - {"notification", "notification_delivery"} <= tables

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert EXPECTED_TABLES <= tables


def test_there_is_exactly_one_head(db_path: Path) -> None:
    """Two revisions revising 0001 breaks ``alembic upgrade head`` outright.

    Steps 5 and 6 both want new tables and both run against ``0002``, in
    parallel. Either one adding its own revision on top of ``0001`` gives
    Alembic two heads and an ambiguous target — which is why the ledger
    schema is one revision written ahead of both, and why this test exists
    rather than the convention being left to memory.

    ``0006`` is the head now: the routing for the owner's own actions.
    ``0005`` before it added ``notification`` and ``notification_delivery``,
    the tables rule 9's halt alert lands in, and ``0004`` ``ledger_rejection``.
    """
    script = ScriptDirectory.from_config(_config(sqlite_url(db_path)))
    assert script.get_heads() == ["0006"]


_OPERATOR_EVENTS = {
    "operator_halt",
    "operator_resume",
    "risk_limits_changed",
    "data_feeds_changed",
    "notification_routes_changed",
}


def _route_rows(url: str) -> dict[tuple[str, str], bool]:
    eng = create_db_engine(url)
    try:
        with Session(eng) as sess:
            return {
                (row.event, row.channel): row.enabled
                for row in sess.query(NotificationRoute).all()
            }
    finally:
        eng.dispose()


def test_0006_seeds_the_operator_routes_and_downgrades_to_0005(
    db_path: Path,
) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    routes = _route_rows(url)
    assert routes[("operator_halt", "bell")] is True
    assert routes[("operator_resume", "discord")] is True
    assert routes[("risk_limits_changed", "bell")] is False
    assert routes[("notification_routes_changed", "discord")] is True

    command.downgrade(cfg, "0005")
    routes = _route_rows(url)
    assert not {event for event, _channel in routes} & _OPERATOR_EVENTS
    assert len(routes) == 16  # 0001's table, untouched

    command.upgrade(cfg, "head")
    assert len(_route_rows(url)) == 26


def test_0006_leaves_rows_the_seed_already_wrote(db_path: Path) -> None:
    """``seed.py`` runs on every startup; an edited row must survive 0006."""
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "0005")
    eng = create_db_engine(url)
    with Session(eng) as sess:
        sess.add(NotificationRoute(event="operator_halt", channel="bell", enabled=False))
        sess.commit()
    eng.dispose()

    command.upgrade(cfg, "head")
    routes = _route_rows(url)
    assert routes[("operator_halt", "bell")] is False
    assert routes[("operator_halt", "discord")] is True


def test_upgrade_head_matches_the_models(migrated: Engine) -> None:
    def _skip_alembic_version(
        obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
    ) -> bool:
        return not (type_ == "table" and name == "alembic_version")

    with migrated.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={"compare_type": True, "include_object": _skip_alembic_version},
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_migration_seed_matches_the_seed_module(migrated: Engine) -> None:
    with Session(migrated) as sess:
        limits = {row.key: row.value for row in sess.query(RiskLimit).all()}
        feeds = {row.key: row.value for row in sess.query(DataFeed).all()}
        routes = {
            (row.event, row.channel): row.enabled
            for row in sess.query(NotificationRoute).all()
        }
        state = sess.query(EngineState).one()

    assert limits == dict(RISK_LIMIT_DEFAULTS)
    assert all(type(v) is Decimal for v in limits.values())
    assert feeds == dict(DATA_FEED_DEFAULTS)
    assert routes == {(e, c): enabled for e, c, enabled in NOTIFICATION_ROUTE_DEFAULTS}
    assert state.id == 1
    assert state.halted is True


def test_downgrade_removes_everything(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert tables & EXPECTED_TABLES == set()
