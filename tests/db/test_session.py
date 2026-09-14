"""Session and engine construction.

The spec chose one writer process specifically so WAL stays simple. WAL is
still set explicitly rather than assumed: journal mode is a property of the
database file, so a database created by some other tool would otherwise be
in rollback-journal mode and nothing would say so.

``busy_timeout`` is pinned three ways here, and the three are not
interchangeable: two assert that the pragma and the constant agree (so they
cannot drift), and one asserts the constant clears a floor read from the
sqlite3 driver itself (so the *value* is pinned, not merely the agreement).
Only the third catches a deleted pragma statement or a shortened wait, and
only the third is tagged ``risk``.
"""

import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from corollary.db.models import Base, RiskLimit
from corollary.db.session import (
    DATABASE_URL_ENV,
    SQLITE_BUSY_TIMEOUT_MS,
    create_db_engine,
    database_url,
    session_scope,
    sqlite_url,
)

#: Passed to the probe connection and probe URL below, in seconds, to ask the
#: sqlite3 driver for a *shorter* wait than it would pick on its own. The test
#: asserts that it really is shorter rather than assuming it, so a driver whose
#: default ever dropped this low would fail loudly instead of going vacuous.
PROBE_TIMEOUT_S = 1.0


def _busy_timeout_ms(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA busy_timeout").fetchone()[0])


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


def test_busy_timeout_is_set(engine: Engine) -> None:
    """The pragma and the constant agree.

    Asserted against the constant rather than a repeated literal so the two
    cannot drift apart. That is *all* this asserts: because the constant is
    set equal to the driver's own default, it would still pass with the
    pragma statement deleted. The floor test below is the one that catches
    that.
    """
    with engine.connect() as conn:
        timeout = conn.execute(text("PRAGMA busy_timeout")).scalar_one()
    assert timeout == SQLITE_BUSY_TIMEOUT_MS


def test_busy_timeout_survives_a_fresh_connection(db_path: Path) -> None:
    """Per connection, like the other two pragmas — so set on every connect."""
    first = create_db_engine(sqlite_url(db_path))
    Base.metadata.create_all(first)
    first.dispose()

    second = create_db_engine(sqlite_url(db_path))
    with second.connect() as conn:
        timeout = conn.execute(text("PRAGMA busy_timeout")).scalar_one()
    assert timeout == SQLITE_BUSY_TIMEOUT_MS
    second.dispose()


@pytest.mark.risk
def test_the_wait_is_never_shorter_than_the_drivers_own_default(
    tmp_path: Path,
) -> None:
    """The configured wait never undercuts the one we would have inherited.

    This is the argument the constant's docstring uses to reject a lower
    value, asserted rather than merely written down: a shorter wait makes
    ``database is locked`` *more* likely, and that error is what leaves the
    dead-man's switch disarmed.

    Tagged on the ground stated at the top of
    ``tests/engine/test_runtime.py``. Not because a busy timeout is on rule
    9's path -- it is not -- but because a refused write is how rule 9's halt
    goes missing, and this setting decides whether a collision waits or is
    refused. Its failure direction is a switch that does not fire, which
    costs money rather than clarity.

    The two equality tests above cannot catch either way this goes wrong.
    ``SQLITE_BUSY_TIMEOUT_MS`` is deliberately *equal* to the sqlite3
    driver's implicit default, so ``pragma == constant`` still holds with the
    pragma statement deleted, and holds just as well at 2,000 -- the value
    the constant's own docstring rejects. They pin agreement between two
    numbers; this pins the number.

    Neither half hardcodes 5,000. The floor is read from the driver at
    runtime, because the claim being made is *not shorter than what we would
    have inherited*, and a literal would stop tracking that if Python's
    default ever moved.

    The probe engine asks, through its URL, for a shorter wait than the
    driver picks on its own -- the pysqlite dialect parses ``?timeout=`` and
    passes it to ``sqlite3.connect`` -- which is what makes the pragma's
    effect observable at all. Delete the pragma statement and this connection
    reports the 1s the URL asked for; shorten the constant and it reports
    that instead. Both are under the floor.
    """
    bare = sqlite3.connect(":memory:")
    try:
        driver_default_ms = _busy_timeout_ms(bare)
    finally:
        bare.close()

    assert SQLITE_BUSY_TIMEOUT_MS >= driver_default_ms

    asked = sqlite3.connect(":memory:", timeout=PROBE_TIMEOUT_S)
    try:
        assert _busy_timeout_ms(asked) < driver_default_ms
    finally:
        asked.close()

    probe_url = f"{sqlite_url(tmp_path / 'probe.db')}?timeout={PROBE_TIMEOUT_S:g}"
    probe = create_db_engine(probe_url)
    try:
        with probe.connect() as conn:
            in_force = int(conn.execute(text("PRAGMA busy_timeout")).scalar_one())
    finally:
        probe.dispose()

    assert in_force >= driver_default_ms


def test_a_writer_that_does_not_wait_fails_at_once(
    db_path: Path, engine: Engine
) -> None:
    """The control for the test below: without a wait, the second writer raises.

    This is the failure mode ``busy_timeout`` exists to remove. SQLite permits
    one writer; a second one either waits or gets
    ``OperationalError: database is locked``. Nothing here is timed — the
    impatient connection is given a zero timeout, so its refusal is immediate
    by definition rather than by luck of scheduling.
    """
    holder = sqlite3.connect(str(db_path), isolation_level=None)
    impatient = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")

        impatient.execute("PRAGMA busy_timeout=0")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            impatient.execute("BEGIN IMMEDIATE")
    finally:
        impatient.close()
        holder.close()


def test_two_writers_serialise_rather_than_one_raising(
    db_path: Path, engine: Engine
) -> None:
    """A second writer waits for the first and then lands.

    Deliberately not a timing assertion. The only sleep is the one that makes
    the writer *likely* to be blocked when the lock is released, and it is not
    load-bearing: if the writer has not reached the lock yet, the write simply
    succeeds uncontended and the test still passes. The single way to fail is
    for this thread to stall for the whole configured timeout between waking
    and committing — two orders of magnitude beyond the sleep, and a real
    boundary rather than a notional one: raise the sleep past
    ``SQLITE_BUSY_TIMEOUT_MS`` (6s against the configured 5s) and the writer
    does raise. So ~5s of main-thread stall is the failure line, not any
    smaller scheduling hiccup.
    """
    holder = sqlite3.connect(str(db_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")

    outcome: list[str] = []
    started = threading.Event()

    def write() -> None:
        started.set()
        try:
            with session_scope(engine) as sess:
                sess.add(RiskLimit(key="waiter", value=Decimal("7")))
        except Exception as exc:  # pragma: no cover - only on a real failure
            outcome.append(repr(exc))
        else:
            outcome.append("committed")

    writer = threading.Thread(target=write, name="second-writer")
    writer.start()
    try:
        assert started.wait(timeout=30)
        time.sleep(0.05)
        holder.execute("COMMIT")
    finally:
        holder.close()
        writer.join(timeout=30)

    assert not writer.is_alive()
    assert outcome == ["committed"]

    with Session(engine) as verify:
        assert verify.get(RiskLimit, "waiter") is not None
