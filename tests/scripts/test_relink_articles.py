"""``scripts/relink_articles.py`` (Phase 3 R2), on a temp database: no request, no key.

The database is a file under ``tmp_path`` built from the ORM metadata, found
the way the app finds its own -- ``COROLLARY_DATABASE_URL``. It holds one
SYNTHETIC Motley Fool pair stored with the matcher as it was before
``SYNDICATION_SKEWS``, so the Finnhub copy is the unlinked duplicate R2 exists
for. What is pinned: ``--dry-run`` writes nothing and takes no write lock on
the live file, the live run links and a second links nothing, the live run
refuses while the API port answers, ``--since`` is UTC midnight, the output
names the absolute database path, the skip count and what the bound covered,
and a database file that does not exist is refused rather than created empty.

No test opens a socket: the port check is injected (``api_is_up``).
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy.orm import Session

import corollary.data.news.ingest as ingest
from corollary.data.news.article import NewsArticle, NewsFeed
from corollary.db.models import Base
from corollary.db.session import DATABASE_URL_ENV, create_db_engine, sqlite_url

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "relink_articles.py"
FOOL_AT = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)


def _load(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the script by path; its ``sys.path`` edit is undone at teardown."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("relink_articles", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "relink_articles", module)
    spec.loader.exec_module(module)
    return module


def _fool(vendor: str, vendor_id: str, **overrides: Any) -> NewsArticle:
    fields: dict[str, Any] = {
        "vendor": vendor,
        "vendor_id": vendor_id,
        "feed": NewsFeed.MASSIVE_NEWS if vendor == "massive" else NewsFeed.FINNHUB_COMPANY,
        "url": f"https://example.com/story/{vendor}-{vendor_id}",
        "headline": "Is Micron a Buy After Its Earnings?",
        "summary": "A summary",
        "publisher": "The Motley Fool" if vendor == "massive" else "Yahoo",
        "published_at": FOOL_AT if vendor == "massive" else FOOL_AT + timedelta(minutes=20),
        "tickers": ("NVDA",),
    }
    fields.update(overrides)
    return NewsArticle(**fields)


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "corollary.db"
    engine = create_db_engine(sqlite_url(path))
    Base.metadata.create_all(engine)
    with Session(engine) as session, monkeypatch.context() as patched:
        patched.setattr(ingest, "SYNDICATION_SKEWS", ())
        ingest.store_articles(session, [_fool("massive", "m-1")], assets=None, now=NOW)
        ingest.store_articles(session, [_fool("finnhub", "f-1")], assets=None, now=NOW)
        session.commit()
    engine.dispose()
    monkeypatch.setenv(DATABASE_URL_ENV, sqlite_url(path))
    assert _links(path) == {"m-1": None, "f-1": None}
    return path


def _links(path: Path) -> dict[str, int | None]:
    """``vendor_id -> canonical_id``, read with the stdlib driver, not the ORM."""
    with sqlite3.connect(path) as conn:
        return dict(conn.execute("SELECT vendor_id, canonical_id FROM news_article"))


def _dump(path: Path) -> list[tuple[Any, ...]]:
    """Every column of every article and ticker row."""
    with sqlite3.connect(path) as conn:
        articles = conn.execute("SELECT * FROM news_article ORDER BY id").fetchall()
        tags = conn.execute("SELECT * FROM news_article_ticker ORDER BY article_id, ticker").fetchall()
    return [*articles, *tags]


def _ids(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as conn:
        return dict(conn.execute("SELECT vendor_id, id FROM news_article"))


def _app_down() -> bool:
    return False


def _app_up() -> bool:
    return True


def _never_asked() -> bool:
    raise AssertionError("the dry run must not need the port check")


def _store_member_of_the_finnhub_copy(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An Alpaca copy linked by URL to the unlinked Finnhub row, at ingest.

    The Finnhub row then matches the Massive row but is already canonical for
    another row, so the pass skips it rather than make a chain.
    """
    engine = create_db_engine(sqlite_url(path))
    with Session(engine) as session, monkeypatch.context() as patched:
        patched.setattr(ingest, "SYNDICATION_SKEWS", ())
        ingest.store_articles(
            session,
            [
                _fool(
                    "alpaca",
                    "a-1",
                    feed=NewsFeed.ALPACA_NEWS,
                    url="https://example.com/story/finnhub-f-1",
                    headline="Micron, the earnings, and what comes next",
                    publisher="Benzinga",
                    published_at=FOOL_AT + timedelta(minutes=25),
                )
            ],
            assets=None,
            now=NOW,
        )
        session.commit()
    engine.dispose()
    assert _links(path)["a-1"] == _ids(path)["f-1"]


def test_help_says_since_is_utc_midnight(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch)
    with pytest.raises(SystemExit) as exited:
        script.main(["--help"])
    assert exited.value.code == 0
    assert "UTC midnight" in " ".join(capsys.readouterr().out.split())


def test_since_parses_as_utc_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load(monkeypatch)
    assert script.parse_since("2026-11-02") == datetime(2026, 11, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["2026-11-2", "20261102", "2026-11-02T00:00", "yesterday"])
def test_a_since_that_is_not_yyyy_mm_dd_is_refused(
    monkeypatch: pytest.MonkeyPatch, database: Path, value: str
) -> None:
    script = _load(monkeypatch)
    with pytest.raises(SystemExit) as exited:
        script.main(["--since", value], api_is_up=_app_down)
    assert exited.value.code == 2


def test_dry_run_reports_the_link_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch)
    before = _dump(database)

    assert script.main(["--since", "2026-09-24", "--dry-run"]) == 0

    assert _dump(database) == before
    out = capsys.readouterr().out
    ids = _ids(database)
    assert "dry run" in out
    assert "1 link" in out
    assert "nothing written" in out
    line = next(text for text in out.splitlines() if f"article {ids['f-1']} " in text)
    assert f"canonical {ids['m-1']} " in line
    assert "Yahoo" in line and "The Motley Fool" in line
    assert (FOOL_AT + timedelta(minutes=20)).isoformat() in line
    assert FOOL_AT.isoformat() in line
    assert "Is Micron" not in out  # headlines are vendor text and never printed


def test_the_live_run_links_and_a_second_links_nothing(
    monkeypatch: pytest.MonkeyPatch, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch)
    ids = _ids(database)

    assert script.main(["--since", "2026-09-24"], api_is_up=_app_down) == 0
    assert "relinked 1 article" in capsys.readouterr().out
    assert _links(database) == {"m-1": None, "f-1": ids["m-1"]}

    after_first = _dump(database)
    assert script.main(["--since", "2026-09-24"], api_is_up=_app_down) == 0
    assert "relinked 0 article" in capsys.readouterr().out
    assert _dump(database) == after_first


def test_since_after_the_pair_links_nothing(
    monkeypatch: pytest.MonkeyPatch, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch)
    assert script.main(["--since", "2026-09-25"], api_is_up=_app_down) == 0
    assert "relinked 0 article" in capsys.readouterr().out
    assert _links(database) == {"m-1": None, "f-1": None}


def test_a_database_file_that_does_not_exist_is_refused_not_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "elsewhere" / "corollary.db"
    monkeypatch.setenv(DATABASE_URL_ENV, sqlite_url(missing))
    script = _load(monkeypatch)

    assert script.main(["--since", "2026-09-24"], api_is_up=_app_down) == 2

    assert not missing.exists()
    assert "does not exist" in capsys.readouterr().err


# ------------------------------------------------------- audit fixes (U2 round 2)

#: Run 2 graded stories published from 2026-10-30T07:07Z. A bound on or after
#: that day can leave a pair the re-grade reads unlinked, and Finnhub's watch
#: tier fetches the previous UTC day, so the bound must be earlier still.
RUN_2_FIRST_DAY = date(2026, 10, 30)


def test_the_documented_since_is_before_every_row_the_re_grade_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load(monkeypatch)
    doc = script.__doc__ or ""
    examples = re.findall(r"--since (\d{4}-\d{2}-\d{2})", doc)
    assert examples, "the docstring gives an example --since"
    for text in examples:
        assert date.fromisoformat(text) < RUN_2_FIRST_DAY - timedelta(days=1), text
    assert "idempotent" in " ".join(doc.split())


def test_help_says_the_dry_run_works_on_a_copy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch)
    with pytest.raises(SystemExit):
        script.main(["--help"])
    assert "private copy" in " ".join(capsys.readouterr().out.split())


def test_the_dry_run_takes_no_write_lock_on_the_live_file(
    monkeypatch: pytest.MonkeyPatch, database: Path
) -> None:
    """Rolling back on the live file would hold its write lock from the first
    flushed link to the rollback. Probed at the point the pass has made its
    link: a second connection must still get the write lock at once."""
    script = _load(monkeypatch)
    real = script.relink_stored
    probed: list[bool] = []

    def relink_then_probe(session: Session, *, since: datetime) -> Any:
        result = real(session, since=since)
        assert len(result.links) == 1  # the link is flushed by now
        probe = sqlite3.connect(database, timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")  # "database is locked" if the pass holds it
            probe.execute("ROLLBACK")
        finally:
            probe.close()
        probed.append(True)
        return result

    monkeypatch.setattr(script, "relink_stored", relink_then_probe)

    assert script.main(["--since", "2026-09-24", "--dry-run"], api_is_up=_never_asked) == 0
    assert probed == [True]
    assert _links(database) == {"m-1": None, "f-1": None}


def test_the_dry_run_runs_beside_the_app_and_never_asks_about_it(
    monkeypatch: pytest.MonkeyPatch, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch)
    before = _dump(database)

    assert script.main(["--since", "2026-09-24", "--dry-run"], api_is_up=_never_asked) == 0

    assert _dump(database) == before
    out = capsys.readouterr().out
    assert "1 link" in out and "private copy" in out


def test_the_live_run_refuses_while_the_api_port_answers(
    monkeypatch: pytest.MonkeyPatch, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch)

    def must_not_run(session: Session, *, since: datetime) -> Any:
        raise AssertionError("no pass may run while the app is up")

    monkeypatch.setattr(script, "relink_stored", must_not_run)
    before = _dump(database)

    assert script.main(["--since", "2026-09-24"], api_is_up=_app_up) == 3

    assert _dump(database) == before
    err = " ".join(capsys.readouterr().err.split())
    assert "127.0.0.1:8000" in err
    assert "stop the app" in err.lower()
    assert "scripts/weekday_session.py" in err
    assert "nothing was written" in err.lower()


@pytest.mark.parametrize("flags", [["--dry-run"], []], ids=["dry-run", "live"])
def test_the_first_line_is_the_absolute_path_of_the_database_opened(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    """From a relative URL, as the default ``corollary.db`` is: several worktrees
    hold their own, and the path is what tells a run from the live one."""
    monkeypatch.chdir(database.parent)
    monkeypatch.setenv(DATABASE_URL_ENV, sqlite_url(database.name))
    script = _load(monkeypatch)

    assert script.main(["--since", "2026-09-24", *flags], api_is_up=_app_down) == 0

    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"database: {database.resolve()}"


@pytest.mark.parametrize("flags", [["--dry-run"], []], ids=["dry-run", "live"])
def test_the_output_counts_skipped_rows(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    """A skip is logged at WARNING, and the count must not depend on logging."""
    _store_member_of_the_finnhub_copy(database, monkeypatch)
    script = _load(monkeypatch)

    assert script.main(["--since", "2026-09-24", *flags], api_is_up=_app_down) == 0

    out = capsys.readouterr().out
    assert re.search(r"\b0 link|relinked 0 article", out)
    assert "1 skipped" in out


def test_the_output_says_what_the_bound_covered(
    monkeypatch: pytest.MonkeyPatch, database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch)

    assert script.main(["--since", "2026-09-24", "--dry-run"], api_is_up=_app_down) == 0
    out = capsys.readouterr().out
    assert f"earliest published_at {(FOOL_AT + timedelta(minutes=20)).isoformat()}" in out
    assert "0 unlinked row(s) published before --since" in out

    # A bound after the pair: the candidate it left out is counted, not hidden.
    assert script.main(["--since", "2026-09-25", "--dry-run"], api_is_up=_app_down) == 0
    out = capsys.readouterr().out
    assert "earliest published_at none" in out
    assert "1 unlinked row(s) published before --since" in out
