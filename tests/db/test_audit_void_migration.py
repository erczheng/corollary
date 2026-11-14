"""Migration 0018 (R4): the void's columns, its archive, its record and its route.

The upgrade runs on a database whose ``audit_run`` is **referenced** -- a
``label_outcome``, a ``sentiment_accuracy`` and a ``sentiment_source_status``
row name run 1 -- because that is the live database's shape, and it is the
shape a batch rebuild of ``audit_run`` cannot survive under ``PRAGMA
foreign_keys=ON`` (see 0018's docstring). The rows are written with a bare
``sqlite3`` connection, foreign keys off, since the ORM models are already
0018's. All values are SYNTHETIC.
"""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corollary.db.models import Base, NotificationRoute
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from tests.db.test_migrations import _config, _route_rows
from tests.db.test_news_watchlist_tradeability import _normalised

_SEED_SQL = (
    # Label 7 exists: 0020 rebuilds label_outcome, and the rebuild's copy is
    # checked against sentiment_label like any insert (the live database can
    # hold no dangling outcome, since 0014's foreign keys are enforced).
    "INSERT INTO news_article (id, vendor, vendor_id, feed, canonical_id, url, url_key, "
    "headline, headline_key, summary, publisher, published_at, ingested_at) VALUES "
    "(1, 'alpaca', 'synthetic-1', 'alpaca_news', NULL, 'https://example.com/synthetic/1', "
    "'https://example.com/synthetic/1', 'SYNTHETIC headline 1', 'synthetic headline 1', NULL, "
    "'Synthetic', '2026-11-07 14:00:00.000000', '2026-11-07 14:00:00.000000')",
    "INSERT INTO sentiment_label (id, article_id, ticker, source, tier, direction, reasoning, "
    "rule_id, labeled_at) VALUES (7, 1, 'AAPL', 'rules', 'rules', 'bullish', 'synthetic', "
    "'synthetic_rule', '2026-11-07 14:00:00.000000')",
    "INSERT INTO audit_run (id, started_at, finished_at, \"trigger\", status, detail) VALUES "
    "(1, '2026-11-07 15:00:00.000000', '2026-11-07 15:05:00.000000', 'scheduled', 'succeeded', NULL)",
    "INSERT INTO label_outcome (label_id, \"window\", skip_reason, scope, audit_run_id) "
    "VALUES (7, '1h', 'off_watch_untradeable', NULL, 1)",
    "INSERT INTO sentiment_accuracy VALUES (1, 'massive', '1h', 'trailing_28d', 'all', 30, 10, 0, 5)",
    "UPDATE sentiment_source_status SET status = 'demoted', "
    "changed_at = '2026-11-07 15:00:00.000000', reason = 'synthetic', audit_run_id = 1 "
    "WHERE source = 'massive'",
    "INSERT INTO audit_log (at, category, field, previous_value, new_value) VALUES "
    "('2026-11-07 15:05:00.000000', 'sentiment', 'massive', 'unaudited', 'demoted')",
)

_ROWS_SQL = {
    "audit_run": "SELECT id, status, started_at, finished_at FROM audit_run ORDER BY id",
    "label_outcome": 'SELECT label_id, "window", audit_run_id FROM label_outcome',
    "sentiment_accuracy": "SELECT audit_run_id, source, graded FROM sentiment_accuracy",
    "sentiment_source_status": (
        "SELECT source, status, audit_run_id FROM sentiment_source_status ORDER BY source"
    ),
    "audit_log": "SELECT field, previous_value, new_value FROM audit_log WHERE category = 'sentiment'",
}


def _raw(path: Path, *statements: str) -> None:
    conn = sqlite3.connect(path)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _foreign_key_check(path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()


def _rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    conn = sqlite3.connect(path)
    try:
        return {name: conn.execute(sql).fetchall() for name, sql in _ROWS_SQL.items()}
    finally:
        conn.close()


@pytest.fixture
def at_0017(tmp_path: Path) -> Path:
    path = tmp_path / "void.db"
    command.upgrade(_config(sqlite_url(path)), "0017")
    _raw(path, *_SEED_SQL)
    return path


def test_upgrade_and_downgrade_keep_every_row_of_a_referenced_audit_run(at_0017: Path) -> None:
    url = sqlite_url(at_0017)
    before = _rows(at_0017)
    # Nothing dangles at 0017 (label 7 and run 1 both exist), and nothing
    # may dangle after either direction.
    dangling = _foreign_key_check(at_0017)
    assert dangling == []
    command.upgrade(_config(url), "0018")
    assert _rows(at_0017) == before
    conn = sqlite3.connect(at_0017)
    try:
        assert conn.execute("SELECT voided_at, void_reason FROM audit_run").fetchall() == [
            (None, None)
        ]
    finally:
        conn.close()
    # Nothing references a missing run, and nothing new dangles.
    assert _foreign_key_check(at_0017) == dangling
    command.downgrade(_config(url), "0017")
    assert _rows(at_0017) == before
    assert _foreign_key_check(at_0017) == dangling
    conn = sqlite3.connect(at_0017)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(audit_run)")]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "voided_at" not in columns and "void_reason" not in columns
    assert "label_outcome_voided" not in tables


def _engine_at_head(path: Path) -> Engine:
    command.upgrade(_config(sqlite_url(path)), "head")
    return create_db_engine(sqlite_url(path))


def _refused(engine: Engine, sql: str) -> str:
    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as conn:
            conn.execute(text(sql))
    return str(caught.value.orig)


def test_the_void_columns_are_both_or_neither(at_0017: Path) -> None:
    engine = _engine_at_head(at_0017)
    try:
        for values in ("'2026-11-09 20:00:00.000000', NULL", "NULL, 'reason'",
                       "'2026-11-09 20:00:00.000000', '   '"):
            voided_at, reason = values.split(", ", 1)
            message = _refused(
                engine,
                f"UPDATE audit_run SET voided_at = {voided_at}, void_reason = {reason}",
            )
            assert "ck_audit_run_voided" in message
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE audit_run SET voided_at = '2026-11-09 20:00:00.000000', "
                    "void_reason = 'a bad grade'"
                )
            )
        # A running run cannot carry a void.
        message = _refused(
            engine,
            "INSERT INTO audit_run (started_at, \"trigger\", status, voided_at, void_reason) "
            "VALUES ('2026-11-09 21:00:00.000000', 'manual', 'running', "
            "'2026-11-09 21:00:00.000000', 'x')",
        )
        assert "ck_audit_run_voided" in message
    finally:
        engine.dispose()


def test_the_archive_is_keyed_by_label_window_and_run_and_holds_the_shape(
    at_0017: Path,
) -> None:
    engine = _engine_at_head(at_0017)
    insert = (
        "INSERT INTO label_outcome_voided (label_id, \"window\", skip_reason, scope, "
        "audit_run_id, voided_at) VALUES (7, '1h', 'discovery_cap', NULL, {run}, "
        "'2026-11-09 20:00:00.000000')"
    )
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text(insert.format(run=1)))
            conn.execute(text(insert.format(run=2)))  # same label and window, another run
        with pytest.raises(IntegrityError, match="UNIQUE"):
            with engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text(insert.format(run=1)))
        message = _refused(
            engine,
            "INSERT INTO label_outcome_voided (label_id, \"window\", reference_price, "
            "skip_reason, scope, audit_run_id, voided_at) VALUES (8, '1h', '1.00', "
            "'discovery_cap', NULL, 1, '2026-11-09 20:00:00.000000')",
        )
        assert "ck_label_outcome_voided_shape" in message
        message = _refused(
            engine,
            "INSERT INTO label_outcome_voided (label_id, \"window\", skip_reason, scope, "
            "audit_run_id) VALUES (9, '1h', 'discovery_cap', NULL, 1)",
        )
        assert "NOT NULL" in message and "voided_at" in message
    finally:
        engine.dispose()


def test_the_restore_row_shape_is_admitted_and_nothing_wider(at_0017: Path) -> None:
    engine = _engine_at_head(at_0017)
    row = (
        "INSERT INTO audit_log (at, category, field, previous_value, new_value) VALUES "
        "('2026-11-09 20:00:00.000000', 'sentiment', 'massive', '{prev}', '{new}')"
    )
    try:
        with engine.begin() as conn:
            conn.execute(text(row.format(prev="demoted", new="unaudited")))
            conn.execute(text(row.format(prev="active", new="unaudited")))
        assert "ck_audit_log_sentiment_row" in _refused(
            engine, row.format(prev="unaudited", new="unaudited")
        )
        assert "ck_audit_log_sentiment_row" in _refused(
            engine, row.format(prev="demoted", new="retired")
        )
    finally:
        engine.dispose()


def test_the_route_rows_are_seeded_bell_on_discord_on(at_0017: Path) -> None:
    url = sqlite_url(at_0017)
    command.upgrade(_config(url), "head")
    routes = _route_rows(url)
    assert routes[("sentiment_audit_voided", "bell")] is True
    assert routes[("sentiment_audit_voided", "discord")] is True
    command.downgrade(_config(url), "0017")
    assert not {e for e, _c in _route_rows(url)} & {"sentiment_audit_voided"}


def test_the_seed_module_writes_the_route_rows_bell_on_discord_on(tmp_path: Path) -> None:
    engine = create_db_engine(sqlite_url(tmp_path / "seeded.db"))
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            seed(session)
            session.commit()
        routes = _route_rows(sqlite_url(tmp_path / "seeded.db"))
    finally:
        engine.dispose()
    assert routes[("sentiment_audit_voided", "bell")] is True
    assert routes[("sentiment_audit_voided", "discord")] is True


def test_0018_leaves_a_route_the_seed_already_wrote(at_0017: Path) -> None:
    url = sqlite_url(at_0017)
    engine = create_db_engine(url)
    try:
        with Session(engine) as session:
            session.add(
                NotificationRoute(event="sentiment_audit_voided", channel="discord", enabled=False)
            )
            session.commit()
    finally:
        engine.dispose()
    command.upgrade(_config(url), "0018")
    routes = _route_rows(url)
    assert routes[("sentiment_audit_voided", "discord")] is False
    assert routes[("sentiment_audit_voided", "bell")] is True


def test_downgrade_refuses_while_a_void_is_recorded(at_0017: Path) -> None:
    url = sqlite_url(at_0017)
    command.upgrade(_config(url), "head")
    _raw(
        at_0017,
        "UPDATE audit_run SET voided_at = '2026-11-09 20:00:00.000000', void_reason = 'x'",
    )
    with pytest.raises(RuntimeError, match="refusing to downgrade 0018"):
        command.downgrade(_config(url), "0017")
    conn = sqlite3.connect(at_0017)
    try:
        assert conn.execute("SELECT void_reason FROM audit_run").fetchall() == [("x",)]
    finally:
        conn.close()


def _ddl(engine: Engine, name: str) -> tuple[str, list[str]]:
    with engine.connect() as conn:
        sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :n"), {"n": name}
        ).scalar_one()
    return _normalised(sql)


@pytest.mark.parametrize("table", ["audit_run", "label_outcome_voided", "audit_log"])
def test_create_all_and_upgrade_head_write_the_same_ddl(tmp_path: Path, table: str) -> None:
    migrated = _engine_at_head(tmp_path / "migrated.db")
    modelled = create_db_engine(sqlite_url(tmp_path / "models.db"))
    try:
        Base.metadata.create_all(modelled)
        assert _ddl(migrated, table) == _ddl(modelled, table)
    finally:
        migrated.dispose()
        modelled.dispose()
