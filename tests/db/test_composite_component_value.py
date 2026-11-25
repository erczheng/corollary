"""``composite_component_value`` (step 8, U3): migration and model.

Autogenerate does not compare CHECK constraints, so every CHECK case below
runs against both builds -- ``alembic upgrade head`` and ``create_all()`` --
and a CHECK one side has and the other lacks fails here. Column parity is
``tests/db/test_migrations.py::test_upgrade_head_matches_the_models``. The
recheck window is not a CHECK; it is ``tests/data/macro/test_store.py``.
"""

import re
from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import Script, ScriptDirectory
from sqlalchemy import CheckConstraint, Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.data.macro.components import ComponentKey
from corollary.db.models import (
    COMPOSITE_COMPONENTS,
    COMPOSITE_UNIVERSE_COMPONENTS,
    Base,
    CompositeComponentValue,
)
from corollary.db.session import create_db_engine, sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
AT = datetime(2026, 11, 30, 22, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 30)
SEED_DAY = date(2026, 9, 30)
TABLE = "composite_component_value"
MIGRATION_SUFFIX = "_composite_component_value.py"


def _config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(params=["models", "migration"])
def any_engine(request: pytest.FixtureRequest, db_path: Path) -> Iterator[Engine]:
    url = sqlite_url(db_path)
    if request.param == "migration":
        command.upgrade(_config(url), "head")
        eng = create_db_engine(url)
    else:
        eng = create_db_engine(url)
        Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _value(**overrides: Any) -> CompositeComponentValue:
    fields: dict[str, Any] = {
        "date": DAY,
        "component": "momentum",
        "raw_value": Decimal("0.0123456789"),
        "source": "alpaca_daily:sip",
        "members": None,
        "universe_as_of": None,
        "computed_at": AT,
    }
    fields.update(overrides)
    return CompositeComponentValue(**fields)


def _add(eng: Engine, *rows: object) -> None:
    with Session(eng) as sess:
        sess.add_all(rows)
        sess.commit()


def _this_migration() -> Script:
    scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    found = [s for s in scripts.walk_revisions() if Path(s.path).name.endswith(MIGRATION_SUFFIX)]
    assert len(found) == 1, f"expected one *{MIGRATION_SUFFIX} migration, found {found}"
    return found[0]


def test_the_migration_revises_the_revision_numbered_before_it() -> None:
    script = _this_migration()
    number = Path(script.path).name.removesuffix(MIGRATION_SUFFIX)
    assert script.revision == number
    assert script.down_revision == f"{int(number) - 1:04d}"


def _check_sql(name: str) -> str:
    table: Any = CompositeComponentValue.__table__
    (found,) = [
        c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == name
    ]
    return str(found.sqltext)


def test_the_component_check_is_c2s_key_set() -> None:
    # Parsed from the model's own CHECK, not re-typed: db cannot import the
    # enum, so this is what ties the literal to it.
    sql = _check_sql("ck_composite_component_value_component")
    match = re.fullmatch(r"component IN \((.*)\)", sql)
    assert match is not None, sql
    listed = re.findall(r"'([a-z_]+)'", match.group(1))
    assert len(listed) == len(set(listed))
    assert set(listed) == {k.value for k in ComponentKey}
    assert set(COMPOSITE_COMPONENTS) == {k.value for k in ComponentKey}


def test_the_migrations_component_check_is_c2s_key_set(db_path: Path) -> None:
    # The migration carries its own literal copy of the key list. Behaviour
    # tests would not notice an extra key added there alone, so read the
    # CHECK the migrated table actually has and tie it to the enum too.
    url = sqlite_url(db_path)
    command.upgrade(_config(url), "head")
    eng = create_db_engine(url)
    try:
        with eng.connect() as conn:
            ddl = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
                {"name": TABLE},
            ).scalar_one()
    finally:
        eng.dispose()
    match = re.search(
        r"ck_composite_component_value_component\s+CHECK\s*\(\s*component\s+IN\s*\(([^)]*)\)",
        ddl,
    )
    assert match is not None, ddl
    listed = re.findall(r"'([a-z_]+)'", match.group(1))
    assert len(listed) == len(set(listed))
    assert set(listed) == {k.value for k in ComponentKey}


def test_the_universe_check_names_strength_and_breadth() -> None:
    sql = _check_sql("ck_composite_component_value_universe_as_of")
    assert set(re.findall(r"'([a-z_]+)'", sql)) == {"strength", "breadth"}
    assert set(COMPOSITE_UNIVERSE_COMPONENTS) == {
        ComponentKey.STRENGTH.value,
        ComponentKey.BREADTH.value,
    }


@pytest.mark.parametrize("component", sorted(k.value for k in ComponentKey))
def test_each_key_is_admitted(any_engine: Engine, component: str) -> None:
    universe = SEED_DAY if component in ("strength", "breadth") else None
    _add(any_engine, _value(component=component, universe_as_of=universe, members=0))
    with Session(any_engine) as sess:
        assert sess.scalars(select(CompositeComponentValue.component)).one() == component


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"component": "fear"}, id="unknown-component"),
        pytest.param({"component": "Momentum"}, id="wrong-case-component"),
        pytest.param({"source": ""}, id="empty-source"),
        pytest.param({"source": "   "}, id="blank-source"),
        pytest.param({"members": -1}, id="negative-members"),
        pytest.param({"universe_as_of": SEED_DAY}, id="universe-on-momentum"),
        pytest.param(
            {"component": "put_call", "universe_as_of": SEED_DAY}, id="universe-on-put-call"
        ),
        pytest.param({"component": "strength"}, id="no-universe-on-strength"),
        pytest.param({"component": "breadth"}, id="no-universe-on-breadth"),
    ],
)
def test_the_checks_refuse(any_engine: Engine, overrides: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        _add(any_engine, _value(**overrides))


@pytest.mark.parametrize("raw", ["", "abc", "1e5", "1.", "--1", "1.2.3"])
def test_raw_value_refuses_what_is_not_a_plain_decimal_text(any_engine: Engine, raw: str) -> None:
    with any_engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                f"INSERT INTO {TABLE} (date, component, raw_value, source, computed_at) "
                "VALUES ('2026-11-30', 'momentum', :raw, 'x', '2026-11-30 22:00:00')"
            ),
            {"raw": raw},
        )


@pytest.mark.parametrize("raw", ["-0.0123456789", "1234.5678901234", "0.0000000000"])
def test_raw_value_round_trips_exactly_as_text(any_engine: Engine, raw: str) -> None:
    _add(any_engine, _value(raw_value=Decimal(raw)))
    with Session(any_engine) as sess:
        stored = sess.scalars(select(CompositeComponentValue.raw_value)).one()
    assert type(stored) is Decimal and format(stored, "f") == raw
    with any_engine.connect() as conn:
        typ, text_value = conn.execute(text(f"SELECT typeof(raw_value), raw_value FROM {TABLE}")).one()
    assert (typ, text_value) == ("text", raw)


def test_one_row_per_date_and_component(any_engine: Engine) -> None:
    _add(any_engine, _value())
    with pytest.raises(IntegrityError):
        _add(any_engine, _value(raw_value=Decimal("0.5")))
    _add(any_engine, _value(component="volatility", source="fred:VIXCLS"))


def test_the_migration_upgrades_and_downgrades(db_path: Path) -> None:
    url = sqlite_url(db_path)
    cfg = _config(url)
    script = _this_migration()
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    assert TABLE in set(inspect(eng).get_table_names())
    eng.dispose()

    down = script.down_revision
    assert isinstance(down, str)
    command.downgrade(cfg, down)
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    # The predecessor's newest tables survive the downgrade.
    assert {"econ_release_value", "econ_release_fetch"} <= tables
    with eng.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    eng.dispose()
    assert TABLE not in tables
    assert version == down

    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    assert TABLE in set(inspect(eng).get_table_names())
    eng.dispose()
