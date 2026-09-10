"""Session and engine construction.

The spec chose one writer process specifically so WAL stays simple. WAL is
still set explicitly rather than assumed: journal mode is a property of the
database file, so a database created by some other tool would otherwise be
in rollback-journal mode and nothing would say so.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from corollary.db.models import Base, RiskLimit
from corollary.db.session import (
    DATABASE_URL_ENV,
    create_db_engine,
    database_url,
    session_scope,
    sqlite_url,
)


def test_wal_is_actually_on(engine: Engine) -> None:
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
    assert mode == "wal"


def test_foreign_keys_are_enforced(engine: Engine) -> None:
    """SQLite defaults foreign key enforcement *off*, per connection."""
    with engine.connect() as conn:
        enabled = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
    assert enabled == 1


def test_wal_survives_a_fresh_connection(db_path: Path) -> None:
    """The pragma is per connection, so it is set on every connect, not once."""
    first = create_db_engine(sqlite_url(db_path))
    Base.metadata.create_all(first)
    first.dispose()

    second = create_db_engine(sqlite_url(db_path))
    with second.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
    second.dispose()


def test_sqlite_url_is_posix_even_on_windows(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "corollary.db")
    assert url.startswith("sqlite+pysqlite:///")
    assert "\\" not in url


def test_database_url_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "sqlite+pysqlite:///./elsewhere.db")
    assert database_url() == "sqlite+pysqlite:///./elsewhere.db"


def test_database_url_falls_back_to_the_repo_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    assert database_url().startswith("sqlite+pysqlite:///")
    assert database_url().endswith("corollary.db")


def test_session_scope_commits_on_success(engine: Engine) -> None:
    with session_scope(engine) as sess:
        sess.add(RiskLimit(key="probe", value=Decimal("7")))

    with Session(engine) as verify:
        stored = verify.get(RiskLimit, "probe")
        assert stored is not None
        assert stored.value == Decimal("7")


def test_session_scope_rolls_back_on_error(engine: Engine) -> None:
    with pytest.raises(RuntimeError):
        with session_scope(engine) as sess:
            sess.add(RiskLimit(key="probe", value=Decimal("7")))
            raise RuntimeError("boom")

    with Session(engine) as verify:
        assert verify.get(RiskLimit, "probe") is None
