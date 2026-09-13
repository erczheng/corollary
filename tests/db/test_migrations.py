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


def test_upgrade_head_creates_the_nine_tables(migrated: Engine) -> None:
    tables = set(inspect(migrated).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_upgrade_head_does_not_create_a_later_phase_table(migrated: Engine) -> None:
    """``notification`` is step 8's, and the last one still outstanding."""
    tables = set(inspect(migrated).get_table_names())
    assert "notification" not in tables


def test_there_is_exactly_one_head(db_path: Path) -> None:
    """Two revisions revising 0001 breaks ``alembic upgrade head`` outright.

    Steps 5 and 6 both want new tables and both run against ``0002``, in
    parallel. Either one adding its own revision on top of ``0001`` gives
    Alembic two heads and an ambiguous target — which is why the ledger
    schema is one revision written ahead of both, and why this test exists
    rather than the convention being left to memory.

    ``0003`` is the head now: it makes ``fill.price`` nullable, so that an
    exercise whose deliverable could not be verified has somewhere to put
    "no price" other than an estimate.
    """
    script = ScriptDirectory.from_config(_config(sqlite_url(db_path)))
    assert script.get_heads() == ["0003"]


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
