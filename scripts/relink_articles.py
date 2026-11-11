"""Re-link stored news articles that ingest missed as duplicates (Phase 3 R2).

Runs :func:`corollary.data.news.ingest.relink_stored` once, in one
transaction, on the database the app uses: ``COROLLARY_DATABASE_URL`` when it
is set, otherwise ``corollary.db`` in the working directory, so run it from
the repo root, as the app is run. The first line of output is the absolute
path of the file opened. Several worktrees hold their own ``corollary.db``,
and without the path a run from the wrong directory would report its links
exactly as the live run does. It reads no API key, loads no ``.env`` and
makes no request.

    uv run python scripts/relink_articles.py --since 2026-10-01 --dry-run
    uv run python scripts/relink_articles.py --since 2026-10-01

What it does: decision 3's canonical matcher, ``SYNDICATION_SKEWS`` included,
over every stored article published at or after ``--since`` that has no
``canonical_id``. Each row is matched only against rows stored before it, so
the copy stored first stays canonical, as at ingest. It sets ``canonical_id``
and nothing else. It never deletes a row, never changes a row that is
already linked, and never rewrites a timestamp. A second live run links
nothing.

**Choosing --since: earlier than every stored row.** The pass is idempotent,
so a bound that is too early costs nothing but a longer pass, and a bound
that is too late misses pairs. A bound just before the window a re-grade
reads is too late. Run 2 graded stories published from 2026-10-30T07:07Z, and
Finnhub's watch tier on Nov 2 fetched Nov 1 UTC, so a Motley Fool pair
published Nov 1 can be stored with both copies unlinked: ``--since
2026-11-02`` leaves it out, and the re-grade then counts it twice, which is
the outcome R2 exists to prevent. The output says what the bound covered:
the earliest ``published_at`` among the rows the pass examined, and how many
unlinked rows published *before* the bound share a key with an earlier row
and so were left out. If that count is not zero, check the bound, and run
again with an earlier one if it cut into the data.

**--dry-run works on a private copy.** It copies the database with SQLite's
online backup API into a temporary file, opening the live file read-only to
do so, runs the identical pass on the copy, reports it, and deletes the copy.
So a dry run takes no write lock on the live file and is safe beside the
running app: "writes nothing" is true of the lock as well as the data. Running
the pass on the live file and rolling it back would not be: ``relink_stored``
flushes an UPDATE per link, so the pass would hold the write lock from its
first link to the rollback.

**The live run refuses while the app is up.** Before it opens the database
it checks whether anything accepts a TCP connection on 127.0.0.1:8000, the
API's port, which ``scripts/weekday_session.py`` holds through a trading day.
If something does, it names the port, exits 3 and writes nothing. Stop the
app first (``scripts/weekday_session.py``'s docstring says how), run, and
start it again. A run beside the app is unsafe at any duration, not only a
long one: ``store_articles`` needs serialised callers, and the app's matcher
reads the committed, pre-relink state, so an ingest during the pass can join
a third copy to a row the pass is linking and make a chain. The check is
best-effort. It cannot see an app on another port, nor one that starts after
the check, nor another process writing the file.

Sequencing (R5): run it live only after the merge has reached master and the
live app has restarted on it, so that no pair stored after the pass is missed
by the old matcher. Then stop the app, run, and start it again. On 30,600
synthetic rows with 600 pairs the pass took about 11-12 s on 2026-11-09; the
dry run reports how long the identical pass took on the copy.

Output: the database, the bound, the counts (links, skipped, examined, the
earliest examined ``published_at``, candidates before the bound), then one
line per link, giving both article ids, vendors, publishers and
``published_at``. Headlines are vendor text and are never printed. A row the
pass declines to link, because other rows already name it as their canonical
row, is counted as skipped and logged to stderr as a warning.

Exit status: 0 on success; 2 for a bad argument or a database that is not an
existing SQLite file; 3 when the live run refuses because the app is up.
"""

from __future__ import annotations

import argparse
import re
import socket
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.engine import make_url  # noqa: E402

from corollary.data.news.ingest import Relink, RelinkResult, relink_stored  # noqa: E402
from corollary.db.session import (  # noqa: E402
    create_db_engine,
    database_url,
    session_factory,
    sqlite_url,
)

#: Exactly ``YYYY-MM-DD``. ``date.fromisoformat`` alone also takes ``20261102``
#: and week dates, and a bound typed one way and read another is the wrong window.
_SINCE_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")

#: The API's address: where ``uvicorn corollary.api:app`` listens by default and
#: where ``scripts/weekday_session.py`` launches its server. Nothing here binds it.
API_HOST: Final = "127.0.0.1"
API_PORT: Final = 8000

#: A listening socket on loopback completes the handshake at once, so a longer
#: wait finds nothing more. Windows can take about two seconds to report a
#: refusal on a closed port, which is why a timeout also reads as "not up".
_PORT_CHECK_TIMEOUT_S: Final = 1.0

EXIT_APP_RUNNING: Final = 3


def parse_since(text: str) -> datetime:
    """``YYYY-MM-DD`` as that day's UTC midnight."""
    if not _SINCE_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date: {text!r} ({exc})") from exc
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def api_accepts_connections() -> bool:
    """Whether anything accepts a TCP connection on ``API_HOST:API_PORT``.

    Best-effort: it sees only that address, and only at the moment it asks.
    """
    try:
        with socket.create_connection((API_HOST, API_PORT), timeout=_PORT_CHECK_TIMEOUT_S):
            return True
    except OSError:
        return False


def _sqlite_file(url: str) -> Path | None:
    """The absolute path of the SQLite file ``url`` names, or ``None`` when it names none.

    ``None`` for another backend or an in-memory database: the dry run copies
    a file with SQLite's backup API, and the app's database is a file.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def _backup(source: Path, target: Path) -> None:
    """Copy ``source`` into ``target`` with SQLite's online backup API.

    ``source`` is opened read-only. The copy reads one consistent snapshot; in
    WAL mode, which the app's engine sets, a reader does not block the app's
    writer, and a read-only connection cannot write the live file at all.
    """
    reader = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


def _run_pass(url: str, since: datetime, *, commit: bool) -> tuple[RelinkResult, float]:
    """One :func:`relink_stored` pass on ``url``, committed or rolled back, and its duration."""
    engine = create_db_engine(url)
    try:
        started = time.monotonic()
        with session_factory(engine)() as session:
            result = relink_stored(session, since=since)
            if commit:
                session.commit()
            else:
                session.rollback()
        return result, time.monotonic() - started
    finally:
        engine.dispose()


def _describe(link: Relink) -> str:
    return (
        f"  article {link.article_id} ({link.vendor}, {link.publisher or '-'}, "
        f"{link.published_at.isoformat()}) -> canonical {link.canonical_id} "
        f"({link.canonical_vendor}, {link.canonical_publisher or '-'}, "
        f"{link.canonical_published_at.isoformat()})"
    )


def _report_coverage(result: RelinkResult) -> None:
    earliest = "none" if result.earliest_examined is None else result.earliest_examined.isoformat()
    print(
        f"examined {result.examined} unlinked row(s) sharing a key with an earlier row; "
        f"earliest published_at {earliest}"
    )
    line = (
        f"{result.before_since} unlinked row(s) published before --since share a key "
        "with an earlier row and were not examined"
    )
    if result.before_since:
        line += "; if any may be a duplicate the re-grade reads, run again with an earlier --since"
    print(line)


def main(
    argv: Sequence[str] | None = None,
    *,
    api_is_up: Callable[[], bool] = api_accepts_connections,
) -> int:
    """Run the pass. ``api_is_up`` is the live run's port check, injectable for tests."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        epilog=(
            "Opens COROLLARY_DATABASE_URL, or corollary.db in the working directory, "
            "and prints its absolute path first. Needs no API key. The live run "
            f"refuses while anything accepts a connection on {API_HOST}:{API_PORT}."
        ),
    )
    parser.add_argument(
        "--since",
        required=True,
        type=parse_since,
        metavar="YYYY-MM-DD",
        help=(
            "re-link unlinked articles published at or after this day's UTC "
            "midnight (not New York midnight). Use a day before every stored row: "
            "the pass is idempotent, so too early costs nothing and too late misses pairs"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "run the identical pass on a private copy of the database, made with "
            "SQLite's backup API, and report the links it would make; the live file "
            "is only read, takes no write lock, and is never written"
        ),
    )
    args = parser.parse_args(argv)
    since: datetime = args.since
    dry_run: bool = args.dry_run

    url = database_url()
    path = _sqlite_file(url)
    if path is None:
        print(
            f"{make_url(url).render_as_string(hide_password=True)} is not a SQLite "
            "database file; this script works on the app's SQLite file only.",
            file=sys.stderr,
        )
        return 2
    if not path.exists():
        # SQLite creates a missing file on connect, so a run from the wrong
        # directory would otherwise relink nothing in a new empty database.
        print(
            f"database file {path} does not exist; run from the repo root or set "
            "COROLLARY_DATABASE_URL. Nothing was created.",
            file=sys.stderr,
        )
        return 2

    print(f"database: {path}")
    print(f"since: {since.isoformat()} (UTC midnight)")

    if dry_run:
        print("dry run: on a private copy made with SQLite's backup API; the live file is only read")
        with tempfile.TemporaryDirectory(prefix="relink-dry-run-") as scratch:
            copy = Path(scratch) / "corollary.db"
            _backup(path, copy)
            result, elapsed = _run_pass(sqlite_url(copy), since, commit=False)
        print(
            f"dry run: {len(result.links)} link(s) would be made, {result.skipped} skipped; "
            f"nothing written ({elapsed:.1f}s)"
        )
    else:
        if api_is_up():
            print(
                f"refusing the live run: something accepts connections on {API_HOST}:{API_PORT}, "
                "the API's port, so the app looks up. Stop the app first -- "
                "scripts/weekday_session.py's docstring says how to stop it -- then run "
                "this again and start the app afterwards. A pass beside the app's ingest "
                "can make a chain at any duration. Nothing was written.",
                file=sys.stderr,
            )
            return EXIT_APP_RUNNING
        result, elapsed = _run_pass(sqlite_url(path), since, commit=True)
        print(f"relinked {len(result.links)} article(s), {result.skipped} skipped ({elapsed:.1f}s)")

    _report_coverage(result)
    if result.skipped:
        print(
            f"{result.skipped} matching row(s) skipped: other rows already name each as "
            "their canonical row, so linking it would make a chain (news_relink_skipped)"
        )
    for link in result.links:
        print(_describe(link))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
