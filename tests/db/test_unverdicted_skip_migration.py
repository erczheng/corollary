"""Migration 0020: ``off_watch_unverdicted`` (owner decision Q24a).

The reason joins ``label_outcome``'s and ``label_outcome_voided``'s
``ck_*_skip_reason`` list and their membership list in ``ck_*_scope``: a
label no tradeability verdict could be had for is in no graded set, so it
carries no scope. SQLite cannot alter a CHECK, so both tables are rebuilt;
these tests prove every row and every other constraint survives, the DDL is
``create_all()``'s, and the downgrade refuses while the record exists. All
values are SYNTHETIC.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.db.models import Base
from corollary.db.session import create_db_engine, sqlite_url
from tests.data.news.test_audit_runner import _article, _label_on
from tests.db.test_migrations import _config
from tests.db.test_news_watchlist_tradeability import _normalised

T0 = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
_TABLES = ("label_outcome", "label_outcome_voided")
_VOIDED_AT = "'2026-11-20 20:00:00.000000'"


def _seed(url: str) -> tuple[int, int, int]:
    """A finished run and two real labels -- the rebuild's copy checks both FKs."""
    eng = create_db_engine(url)
    try:
        with Session(eng) as session:
            first = _label_on(session, _article(session, at=T0), ticker="XA", at=T0)
            second = _label_on(session, _article(session, at=T0), ticker="XB", at=T0)
            session.execute(
                text(
                    "INSERT INTO audit_run (started_at, finished_at, \"trigger\", status, detail) "
                    "VALUES (:s, :f, 'scheduled', 'succeeded', :d)"
                ),
                {
                    "s": T0.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "f": (T0 + timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "d": '{"discovery_cap_skipped_labels": 0, "discovery_cap_skipped_tickers": 0}',
                },
            )
            run_id = int(session.execute(text("SELECT max(id) FROM audit_run")).scalar_one())
            session.commit()
    finally:
        eng.dispose()
    return first, second, run_id


def _outcome(label_id: int, run_id: int, reason: str, scope: str | None = None) -> str:
    scope_sql = "NULL" if scope is None else f"'{scope}'"
    return (
        'INSERT INTO label_outcome (label_id, "window", skip_reason, scope, audit_run_id) '
        f"VALUES ({label_id}, '1h', '{reason}', {scope_sql}, {run_id})"
    )


def _archived(label_id: int, run_id: int, reason: str, scope: str | None = None) -> str:
    scope_sql = "NULL" if scope is None else f"'{scope}'"
    return (
        'INSERT INTO label_outcome_voided (label_id, "window", skip_reason, scope, '
        f"audit_run_id, voided_at) VALUES ({label_id}, '1d', '{reason}', {scope_sql}, "
        f"{run_id}, {_VOIDED_AT})"
    )


def _execute(url: str, sql: str) -> None:
    eng = create_db_engine(url)
    try:
        with eng.begin() as conn:
            conn.execute(text(sql))
    finally:
        eng.dispose()


def _refused(url: str, sql: str) -> str:
    eng = create_db_engine(url)
    try:
        with pytest.raises(IntegrityError) as caught:
            with eng.begin() as conn:
                conn.execute(text(sql))
    finally:
        eng.dispose()
    return str(caught.value.orig)


def _rows(url: str) -> dict[str, list[tuple[object, ...]]]:
    eng = create_db_engine(url)
    try:
        with eng.connect() as conn:
            return {
                table: [tuple(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY label_id"))]
                for table in _TABLES
            }
    finally:
        eng.dispose()


def _shape(url: str, table: str) -> dict[str, object]:
    eng = create_db_engine(url)
    try:
        inspector = inspect(eng)
        return {
            "pk": inspector.get_pk_constraint(table)["constrained_columns"],
            "fks": sorted(
                (tuple(fk["constrained_columns"]), fk["referred_table"])
                for fk in inspector.get_foreign_keys(table)
            ),
            "indexes": inspector.get_indexes(table),
            "checks": sorted(str(c["name"]) for c in inspector.get_check_constraints(table)),
        }
    finally:
        eng.dispose()


def _ddl(engine: Engine, name: str) -> tuple[str, list[str]]:
    with engine.connect() as conn:
        sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :n"), {"n": name}
        ).scalar_one()
    return _normalised(sql)


@pytest.fixture
def at_0019(tmp_path: Path) -> tuple[Config, str, tuple[int, int, int]]:
    url = sqlite_url(tmp_path / "unverdicted.db")
    cfg = _config(url)
    command.upgrade(cfg, "0019")
    return cfg, url, _seed(url)


def test_0020_revises_0019() -> None:
    script = ScriptDirectory.from_config(_config("sqlite://"))
    revision = script.get_revision("0020")
    assert revision is not None and revision.down_revision == "0019"


def test_upgrade_keeps_every_row_and_constraint_and_admits_the_reason(
    at_0019: tuple[Config, str, tuple[int, int, int]],
) -> None:
    cfg, url, (first, second, run_id) = at_0019
    _execute(url, _outcome(first, run_id, "off_watch_untradeable"))
    _execute(url, _archived(first, run_id, "discovery_cap"))
    assert "ck_label_outcome_skip_reason" in _refused(
        url, _outcome(second, run_id, "off_watch_unverdicted")
    )
    before = _rows(url)
    shapes = {table: _shape(url, table) for table in _TABLES}

    command.upgrade(cfg, "0020")

    assert _rows(url) == before
    assert {table: _shape(url, table) for table in _TABLES} == shapes
    _execute(url, _outcome(second, run_id, "off_watch_unverdicted"))
    _execute(url, _archived(second, run_id, "off_watch_unverdicted"))
    # A membership skip: no scope, on the live table and the archive alike.
    _execute(url, f"DELETE FROM label_outcome WHERE label_id = {second}")
    assert "ck_label_outcome_scope" in _refused(
        url, _outcome(second, run_id, "off_watch_unverdicted", scope="discovery")
    )
    _execute(url, f"DELETE FROM label_outcome_voided WHERE label_id = {second}")
    assert "ck_label_outcome_voided_scope" in _refused(
        url, _archived(second, run_id, "off_watch_unverdicted", scope="watch")
    )
    # The other constraints still bite: an unknown reason, a missing label.
    assert "ck_label_outcome_skip_reason" in _refused(url, _outcome(second, run_id, "no_reason"))
    assert "FOREIGN KEY" in _refused(url, _outcome(second + 1000, run_id, "discovery_cap"))


@pytest.mark.parametrize("table", _TABLES)
def test_create_all_and_upgrade_head_write_the_same_ddl(tmp_path: Path, table: str) -> None:
    command.upgrade(_config(sqlite_url(tmp_path / "migrated.db")), "head")
    migrated = create_db_engine(sqlite_url(tmp_path / "migrated.db"))
    modelled = create_db_engine(sqlite_url(tmp_path / "models.db"))
    try:
        Base.metadata.create_all(modelled)
        assert _ddl(migrated, table) == _ddl(modelled, table)
    finally:
        migrated.dispose()
        modelled.dispose()


def _orphan(path: Path, sql: str) -> None:
    """Write ``sql`` with foreign keys off -- a bare ``sqlite3`` connection's
    default -- so a row naming a missing label can exist, as it could in a
    database written by hand or restored from a damaged copy."""
    raw = sqlite3.connect(path)
    try:
        raw.execute(sql)
        raw.commit()
    finally:
        raw.close()


def _version_and_tables(url: str) -> tuple[str, set[str]]:
    eng = create_db_engine(url)
    try:
        with eng.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        return str(version), set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


@pytest.mark.parametrize("table", _TABLES)
def test_upgrade_refuses_an_orphan_row_before_touching_anything(
    tmp_path: Path, table: str
) -> None:
    """The rebuild's copy is checked against both foreign keys, and a failure
    there used to strand ``_alembic_tmp_<table>`` at 0019, failing every retry
    on the leftover. Now the upgrade refuses first, naming the table and the
    row, and the database is exactly as it was; repaired, it upgrades."""
    path = tmp_path / "orphan.db"
    url = sqlite_url(path)
    cfg = _config(url)
    command.upgrade(cfg, "0019")
    first, _second, run_id = _seed(url)
    _execute(url, _outcome(first, run_id, "discovery_cap"))  # a sound row beside it
    missing = first + 1000
    write = _outcome if table == "label_outcome" else _archived
    _orphan(path, write(missing, run_id, "discovery_cap"))
    before = _rows(url)
    _, tables_before = _version_and_tables(url)

    with pytest.raises(RuntimeError, match="refusing to upgrade 0020") as caught:
        command.upgrade(cfg, "0020")

    message = str(caught.value)
    assert f"{table} holds 1 row(s)" in message
    assert f"label_id={missing}" in message and "sentiment_label" in message
    version, tables = _version_and_tables(url)
    assert version == "0019"
    assert tables == tables_before
    assert not any(name.startswith("_alembic_tmp") for name in tables)
    assert _rows(url) == before

    _orphan(path, f"DELETE FROM {table} WHERE label_id = {missing}")
    command.upgrade(cfg, "0020")
    assert _version_and_tables(url)[0] == "0020"


def test_downgrade_refuses_an_orphan_row_before_touching_anything(
    at_0019: tuple[Config, str, tuple[int, int, int]], tmp_path: Path
) -> None:
    """The reverse rebuild copies the same way, so it refuses the same way."""
    cfg, url, (first, _second, run_id) = at_0019
    command.upgrade(cfg, "0020")
    _orphan(tmp_path / "unverdicted.db", _outcome(first, run_id + 1000, "discovery_cap"))

    with pytest.raises(RuntimeError, match="refusing to downgrade 0020") as caught:
        command.downgrade(cfg, "0019")

    assert "label_outcome holds 1 row(s)" in str(caught.value)
    assert "audit_run" in str(caught.value)
    version, tables = _version_and_tables(url)
    assert version == "0020"
    assert not any(name.startswith("_alembic_tmp") for name in tables)


def test_downgrade_refuses_while_an_unverdicted_row_exists_and_otherwise_reverses(
    at_0019: tuple[Config, str, tuple[int, int, int]],
) -> None:
    cfg, url, (first, second, run_id) = at_0019
    command.upgrade(cfg, "0020")
    _execute(url, _outcome(first, run_id, "discovery_cap"))
    _execute(url, _archived(second, run_id, "off_watch_unverdicted"))
    with pytest.raises(RuntimeError, match="refusing to downgrade 0020"):
        command.downgrade(cfg, "0019")
    assert len(_rows(url)["label_outcome_voided"]) == 1  # the record stands

    _execute(url, f"DELETE FROM label_outcome_voided WHERE label_id = {second}")
    before = _rows(url)
    command.downgrade(cfg, "0019")
    assert _rows(url) == before
    assert "ck_label_outcome_skip_reason" in _refused(
        url, _outcome(second, run_id, "off_watch_unverdicted")
    )
    command.upgrade(cfg, "0020")
    _execute(url, _outcome(second, run_id, "off_watch_unverdicted"))
