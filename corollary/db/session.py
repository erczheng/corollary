"""Engine and session construction.

SQLite, WAL, one writer *process* — which is not the same thing as one
writer. The design spec chose a single process so this stays simple, and
there is no connection pool tuning here; but that process writes from several
places at once (the API's handlers, the settings audit, the watchdog), so
contention for the single write lock is real and ``PRAGMA busy_timeout``
below is what decides whether a collision waits or raises.

All three pragmas are set **explicitly on every connect** rather than
assumed, because all three are per connection. Journal mode is a property of
the database file, so a database created by some other tool would otherwise
be in rollback-journal mode and nothing in the app would say so.
``PRAGMA foreign_keys`` defaults *off* in SQLite, which is the kind of
default that only shows up as a dangling row three phases later. And
``busy_timeout`` otherwise takes the sqlite3 driver's implicit value, which
is a tuning decision made by somebody else — see the constant for why it is
made here instead, and why it is bounded rather than generous.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from corollary.db.types import guard_activity_id_sql, guard_money_sql

__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_DATABASE_PATH",
    "SQLITE_BUSY_TIMEOUT_MS",
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

#: How long a second writer waits for the write lock before giving up and
#: raising ``OperationalError: database is locked``. Milliseconds.
#:
#: A hardcoded module constant, and deliberately not configuration: the feed
#: names in ``data/providers/alpaca.py`` are env vars because they vary with
#: the data plan, and nothing about a deployment makes this vary. It is named
#: rather than inlined for a different reason -- a tuning value buried in a
#: pragma string is one nobody finds when it is wrong. Change it here, in a
#: commit, against the argument below.
#:
#: **Why a value is set at all.** SQLite permits exactly one writer; a second
#: one waits. Absent this pragma the sqlite3 driver's implicit ~5s applies,
#: and a writer past it *fails*. Writers in this one process include
#: ``POST /api/engine/halt``, ``POST /api/engine/resume``, the settings audit
#: write, ``engine_state()``'s create-on-read commit, and the watchdog. The
#: watchdog's write is most likely to collide with a human hitting Halt or
#: Resume -- which is precisely when a fault is in progress. On Windows an AV
#: or backup handle on the file raises the same class of error, as does a full
#: disk. A halt whose write is refused is now retried until it lands, so rule
#: 9 survives one; but the retry is the seatbelt, and setting this is not
#: driving into the wall.
#:
#: **Why 5s, which is what the driver already did.** On a connection opened
#: with the default connect args this changes no behaviour, and that is the
#: point: the value was nobody's decision before, and an inherited default is
#: not a choice anyone can defend or revisit. It is now named, exported, and
#: asserted against a floor read from the driver at runtime --
#: ``test_the_wait_is_never_shorter_than_the_drivers_own_default`` goes red
#: both if this is lowered and if the pragma statement below is deleted, which
#: the two equality tests beside it do not.
#:
#: **It overrides the URL, and silently.** The pragma runs *after*
#: ``sqlite3.connect``, so it replaces whatever the connection already
#: carried, in either direction -- including a ``?timeout=`` on
#: ``COROLLARY_DATABASE_URL``, which the pysqlite dialect parses and hands to
#: the driver. An operator who sets ``...corollary.db?timeout=30`` gets 30s on
#: connect and then 5s from here; the URL value survives only the instant
#: between the two. That path is reachable, since ``.env.example`` exposes the
#: URL as an operator knob, so if the URL is ever meant to win, this pragma
#: has to read it rather than precede it.
#:
#: **Deliberately not lower.** A shorter wait makes ``database is locked``
#: *more* likely, and that is the exact error this whole line of work came
#: from -- one refused write used to leave the dead-man's switch disarmed for
#: the life of the process. Trading a rare hard failure for a more frequent
#: one is the wrong direction, whatever the retry now catches.
#:
#: **Deliberately not higher, yet.** ``EngineRuntime.check_watchdog()`` is
#: synchronous and runs on the API's asyncio event loop, so every millisecond
#: blocked *there* is a millisecond the API answers nothing. That is a
#: narrower path than it first looks: the route handlers are sync ``def``, so
#: FastAPI runs them in AnyIO's threadpool, and a halt, a resume or a settings
#: commit blocking for seconds parks a worker thread rather than the loop. The
#: watchdog's own DB work is the exception, and it is on the loop. Raising
#: this past the point where a collision is survivable would trade a rare
#: failure for a visible hang there. The honest fix is to stop doing blocking
#: SQLite work on the loop; once that lands, this can go up. Until then 5s is
#: the ceiling worth paying, and the cases that genuinely hold the file -- an
#: AV or backup handle, a full disk -- outlast any timeout worth waiting on a
#: loop anyway. Those are the retry's job, not this one's.
SQLITE_BUSY_TIMEOUT_MS = 5_000

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
    """A new Engine with the SQLite pragmas and the column guards attached.

    Every Engine in this codebase is built here — ``get_engine``, the Alembic
    env, and the test fixtures all route through it — which is what makes
    attaching the guards here equivalent to attaching them globally, without
    an import-time side effect on ``sqlalchemy.Engine`` itself.
    """
    engine = create_engine(url or database_url())
    _attach_sqlite_pragmas(engine)
    _attach_column_guards(engine)
    return engine


def _attach_column_guards(engine: Engine) -> None:
    """Refuse to execute a statement that orders or aggregates a guarded column.

    Two columns types answer a plausible wrong number rather than erroring, and
    both do it in ``ORDER BY`` and in an aggregate — the two forms a column's
    comparator never sees, because neither is an operator call.

    * ``Money`` is TEXT on SQLite, so ``MAX(value)`` over the five seeded
      ceilings (7, 20, 8, 25, 40) answers 8.
    * ``ActivityId`` is the broker's composite id, and non-trade rows carry a
      **zeroed** timestamp half, so ``MAX(activity_id)`` answers with the
      day's last *fill* and a cursor built on it steps over that day's
      expiries and assignments without trace. Decision 13; no carve-out.

    See ``corollary.db.types``.
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
        guard_activity_id_sql(clauseelement)


def _attach_sqlite_pragmas(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            # busy_timeout leads because it is the budget the two statements
            # after it spend. ``journal_mode=WAL`` is the one pragma here
            # that can return SQLITE_BUSY -- converting a rollback-journal
            # file needs brief exclusive access -- so set third it would run
            # that conversion on whatever wait the driver happened to hand
            # us rather than on the value above. Equal today; not equal the
            # moment the constant moves, and the statement most likely to
            # block would then have the smallest budget on the connection.
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS:d}")
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
