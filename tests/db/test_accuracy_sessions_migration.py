"""Migration 0017: ``sentiment_accuracy.sessions`` (R3, owner decision Q23b).

``sessions`` is nullable -- NULL is a row written before Q23b, which no
reader treats as eligible -- and ``ck_sentiment_accuracy_sessions`` refuses a
negative or fractional count. SQLite cannot add a table CHECK in place, so
the column and its CHECK arrive in one batch rebuild; these tests prove the
rebuild keeps every row and every constraint the table already had.
Autogenerate compares neither CHECKs nor their SQL, so
``test_sentiment_audit.py`` also runs the CHECK against both ``create_all()``
and the migration.

Every write here is raw SQL with named columns: at ``0016`` the ORM model
already carries ``sessions``, which that schema does not have.
"""

import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from corollary.db.models import Base
from corollary.db.session import create_db_engine, sqlite_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
T0 = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)

#: Every CHECK 0014 gave the table; the rebuild must keep each one.
_0014_CHECKS = {
    "ck_sentiment_accuracy_source",
    "ck_sentiment_accuracy_window",
    "ck_sentiment_accuracy_basis",
    "ck_sentiment_accuracy_scope",
    "ck_sentiment_accuracy_graded",
    "ck_sentiment_accuracy_correct",
    "ck_sentiment_accuracy_neutral",
}

#: One row per 0014 CHECK, each named for the CHECK it is built to break.
#: Not every row breaks only that one -- ``graded: -1`` also breaks
#: ``correct <= graded`` -- which is why 0017 fixes the CHECK order.
_BREAKS: list[tuple[dict[str, object], str]] = [
    ({"source": "llm"}, "ck_sentiment_accuracy_source"),
    ({"window": "4h"}, "ck_sentiment_accuracy_window"),
    ({"basis": "trailing_7d"}, "ck_sentiment_accuracy_basis"),
    ({"scope": "manual"}, "ck_sentiment_accuracy_scope"),
    ({"graded": -1, "correct": 0}, "ck_sentiment_accuracy_graded"),
    ({"correct": 32}, "ck_sentiment_accuracy_correct"),
    ({"neutral": -1}, "ck_sentiment_accuracy_neutral"),
]

_ROW: Mapping[str, object] = {
    "source": "rules",
    "window": "1h",
    "basis": "trailing_28d",
    "scope": "all",
    "graded": 31,
    "correct": 17,
    "neutral": 4,
}


def _config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _audit_run(conn: Connection) -> int:
    conn.execute(
        text(
            "INSERT INTO audit_run (started_at, finished_at, \"trigger\", status, detail) "
            "VALUES (:s, :f, 'scheduled', 'succeeded', :d)"
        ),
        {
            # UtcDateTime's stored form: naive UTC, fixed-width ISO text.
            "s": T0.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "f": (T0 + timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M:%S.%f"),
            "d": '{"discovery_cap_skipped_labels": 0, "discovery_cap_skipped_tickers": 0}',
        },
    )
    run_id = conn.execute(text("SELECT max(id) FROM audit_run")).scalar_one()
    assert isinstance(run_id, int)
    return run_id


def _insert(conn: Connection, run_id: int, **overrides: object) -> None:
    fields = {"audit_run_id": run_id, **_ROW, **overrides}
    columns = ", ".join(f'"{c}"' for c in fields)
    params = ", ".join(f":{c}" for c in fields)
    conn.execute(text(f"INSERT INTO sentiment_accuracy ({columns}) VALUES ({params})"), fields)


def _rows(eng: Engine) -> list[dict[str, object]]:
    with eng.connect() as conn:
        result = conn.execute(
            text('SELECT * FROM sentiment_accuracy ORDER BY source, "window", basis, scope')
        )
        return [dict(r._mapping) for r in result]


def _shape(eng: Engine) -> dict[str, object]:
    """Everything about the table but its columns: the rebuild must keep it."""
    inspector = inspect(eng)
    return {
        "pk": inspector.get_pk_constraint("sentiment_accuracy")["constrained_columns"],
        "fks": [
            (fk["constrained_columns"], fk["referred_table"], fk["referred_columns"])
            for fk in inspector.get_foreign_keys("sentiment_accuracy")
        ],
        "indexes": inspector.get_indexes("sentiment_accuracy"),
        "unique": inspector.get_unique_constraints("sentiment_accuracy"),
    }


def _checks(eng: Engine) -> set[str]:
    return {
        str(c["name"]) for c in inspect(eng).get_check_constraints("sentiment_accuracy")
    }


def _ddl_check_order(eng: Engine) -> list[str]:
    """The CHECK names in the table's ``CREATE TABLE``, in order -- the order
    SQLite tries them in, and so which one a row breaking two is refused by."""
    with eng.connect() as conn:
        sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sentiment_accuracy'")
        ).scalar_one()
    return re.findall(r"CONSTRAINT (\w+) CHECK", str(sql))


def _columns(eng: Engine) -> dict[str, bool]:
    return {
        str(c["name"]): bool(c["nullable"]) for c in inspect(eng).get_columns("sentiment_accuracy")
    }


@pytest.fixture
def at_0016(db_path: Path) -> tuple[Config, str]:
    url = sqlite_url(db_path)
    cfg = _config(url)
    command.upgrade(cfg, "0016")
    return cfg, url


def test_0017_revises_0016() -> None:
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    revision = script.get_revision("0017")
    assert revision is not None and revision.down_revision == "0016"


def test_upgrade_keeps_every_row_and_leaves_its_sessions_null(
    at_0016: tuple[Config, str],
) -> None:
    cfg, url = at_0016
    eng = create_db_engine(url)
    assert "sessions" not in _columns(eng)
    with eng.begin() as conn:
        run_id = _audit_run(conn)
        _insert(conn, run_id)
        _insert(conn, run_id, scope="watch", graded=0, correct=0, neutral=0)
    before = _rows(eng)
    eng.dispose()

    command.upgrade(cfg, "0017")
    eng = create_db_engine(url)
    assert _columns(eng)["sessions"] is True  # nullable
    after = _rows(eng)
    assert [{k: v for k, v in r.items() if k != "sessions"} for r in after] == before
    assert [r["sessions"] for r in after] == [None, None]  # no backfill: pre-Q23b rows
    eng.dispose()


def test_the_sessions_check_refuses_a_negative_or_fractional_count(
    at_0016: tuple[Config, str],
) -> None:
    cfg, url = at_0016
    command.upgrade(cfg, "0017")
    eng = create_db_engine(url)
    with eng.begin() as conn:
        run_id = _audit_run(conn)
    # 2.5: INTEGER affinity keeps it REAL without complaint; the CHECK does not.
    for sessions in (-1, 2.5):
        with pytest.raises(IntegrityError, match="ck_sentiment_accuracy_sessions"):
            with eng.begin() as conn:
                _insert(conn, run_id, sessions=sessions)
    accepted = [("1h", "trailing_28d", None), ("1d", "trailing_28d", 0), ("1h", "since_demotion", 5)]
    for window, basis, sessions in accepted:
        with eng.begin() as conn:
            _insert(conn, run_id, window=window, basis=basis, sessions=sessions)
    assert sorted((r["window"], r["basis"], r["sessions"]) for r in _rows(eng)) == sorted(
        accepted, key=lambda a: (a[0], a[1])
    )
    eng.dispose()


def test_the_rebuild_keeps_every_constraint_the_table_had(at_0016: tuple[Config, str]) -> None:
    cfg, url = at_0016
    eng = create_db_engine(url)
    shape, checks = _shape(eng), _checks(eng)
    assert checks == _0014_CHECKS
    eng.dispose()

    command.upgrade(cfg, "0017")
    eng = create_db_engine(url)
    assert _shape(eng) == shape
    assert _checks(eng) == _0014_CHECKS | {"ck_sentiment_accuracy_sessions"}
    with eng.begin() as conn:
        run_id = _audit_run(conn)
    for overrides, constraint in _BREAKS:
        with pytest.raises(IntegrityError, match=constraint):
            with eng.begin() as conn:
                _insert(conn, run_id, sessions=5, **overrides)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        with eng.begin() as conn:
            _insert(conn, run_id + 1000, sessions=5)
    with eng.begin() as conn:
        _insert(conn, run_id, sessions=5)
    with pytest.raises(IntegrityError, match="UNIQUE"):
        with eng.begin() as conn:
            _insert(conn, run_id, sessions=6)
    eng.dispose()


def test_the_rebuilt_checks_are_in_the_models_order(
    at_0016: tuple[Config, str], tmp_path: Path
) -> None:
    """Batch mode copies constraints out of a set; 0017 re-creates them so the
    migrated table tries its CHECKs in ``create_all()``'s order. Without that,
    ``graded = -1`` (which also breaks ``correct <= graded``) is refused under
    a name that varies from one process to the next."""
    cfg, url = at_0016
    command.upgrade(cfg, "0017")
    migrated = create_db_engine(url)
    built = create_db_engine(sqlite_url(tmp_path / "models.db"))
    Base.metadata.create_all(built)
    try:
        order = _ddl_check_order(migrated)
        assert order == _ddl_check_order(built)
        assert order[-1] == "ck_sentiment_accuracy_sessions"
        assert order.index("ck_sentiment_accuracy_graded") < order.index(
            "ck_sentiment_accuracy_correct"
        )
    finally:
        migrated.dispose()
        built.dispose()

    command.downgrade(cfg, "0016")
    eng = create_db_engine(url)
    assert _ddl_check_order(eng) == order[:-1]  # 0014's seven, in 0014's order
    eng.dispose()


def test_0017_downgrades_to_0016_and_back(at_0016: tuple[Config, str]) -> None:
    cfg, url = at_0016
    command.upgrade(cfg, "0017")
    eng = create_db_engine(url)
    with eng.begin() as conn:
        run_id = _audit_run(conn)
        _insert(conn, run_id, sessions=5)
        _insert(conn, run_id, scope="watch", sessions=None)
    eng.dispose()

    command.downgrade(cfg, "0016")
    eng = create_db_engine(url)
    assert "sessions" not in _columns(eng)
    assert len(_rows(eng)) == 2  # rows kept; only the count goes, with its column
    assert _checks(eng) == _0014_CHECKS
    with pytest.raises(OperationalError, match="sessions"):
        with eng.begin() as conn:
            _insert(conn, run_id, scope="discovery", sessions=5)
    eng.dispose()

    command.upgrade(cfg, "0017")
    eng = create_db_engine(url)
    assert [r["sessions"] for r in _rows(eng)] == [None, None]
    with pytest.raises(IntegrityError, match="ck_sentiment_accuracy_sessions"):
        with eng.begin() as conn:
            _insert(conn, run_id, scope="discovery", sessions=-1)
    eng.dispose()
