"""Engine and session construction.

SQLite, WAL, one writer. The design spec chose a single process specifically
so this stays simple — there is no connection pool tuning here and no
retry-on-``database is locked`` loop, because with one writer there is nothing
to contend with.

WAL is set **explicitly on every connect** rather than assumed. Journal mode
is a property of the database file, so a database created by some other tool
would otherwise be in rollback-journal mode and nothing in the app would say
so. ``PRAGMA foreign_keys`` is likewise per connection and defaults *off* in
SQLite, which is the kind of default that only shows up as a dangling row
three phases later.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from corollary.db.types import guard_money_sql

__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_DATABASE_PATH",
    "create_db_engine",
    "database_url",
    "get_engine",
    "session_scope",
    "sqlite_url",
]

#: Override for the database location. Absent, the repo-root file below is
#: used. Not a secret — it holds no credentials — but it belongs in
#: ``.env.example`` alongside the rest of the configuration.
DATABASE_URL_ENV = "COROLLARY_DATABASE_URL"

DEFAULT_DATABASE_PATH = Path("corollary.db")

_engine: Engine | None = None


def sqlite_url(path: Path | str) -> str:
    """A SQLite URL for a filesystem path.

    ``as_posix()`` rather than ``str()``: on Windows a plain ``str(Path)``
    yields backslashes, which a URL treats as ordinary characters in some
    positions and as escapes in others.
    """
    return f"sqlite+pysqlite:///{Path(path).as_posix()}"


def database_url() -> str:
    """The configured database URL, or the repo-root default."""
    configured = os.environ.get(DATABASE_URL_ENV)
    if configured:
        return configured
    return sqlite_url(DEFAULT_DATABASE_PATH)


def create_db_engine(url: str | None = None) -> Engine:
    """A new Engine with the SQLite pragmas and the money guard attached.

    Every Engine in this codebase is built here — ``get_engine``, the Alembic
    env, and the test fixtures all route through it — which is what makes
    attaching the guard here equivalent to attaching it globally, without an
    import-time side effect on ``sqlalchemy.Engine`` itself.
    """
    engine = create_engine(url or database_url())
    _attach_sqlite_pragmas(engine)
    _attach_money_guard(engine)
    return engine


def _attach_money_guard(engine: Engine) -> None:
    """Refuse to execute a statement that orders or aggregates money.

    ``Money`` is TEXT on SQLite, so ``ORDER BY value`` and ``MAX(value)``
    answer lexicographically — 8 is the maximum of (7, 20, 8, 25, 40). The
    column's comparator catches every *operator*; neither of those two forms
    is one, so they are caught here instead. See ``corollary.db.types``.
    """

    @event.listens_for(engine, "before_execute")
    def _guard(
        conn: Any,
        clauseelement: Any,
        multiparams: Any,
        params: Any,
        execution_options: Any,
    ) -> None:
        guard_money_sql(clauseelement)


def _attach_sqlite_pragmas(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def get_engine() -> Engine:
    """The process-wide Engine, built on first use.

    One writer means one engine. Tests and Alembic build their own with
    ``create_db_engine`` rather than reaching for this.
    """
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """A transactional session: commit on success, roll back on any exception."""
    session = session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
