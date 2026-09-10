"""Fixtures for the persistence tests.

Every fixture uses a **file-backed** SQLite database under ``tmp_path``
rather than ``:memory:``. That is not incidental: ``PRAGMA journal_mode=WAL``
is a no-op on an in-memory database (it answers ``memory``), so the one test
that proves WAL is on would pass vacuously against a memory URL.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.db.models import Base
from corollary.db.session import create_db_engine, sqlite_url


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "corollary.db"


@pytest.fixture
def engine(db_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(sqlite_url(db_path))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as sess:
        yield sess
