"""Void one sentiment audit run (R4): archive its grades and restore what it moved.

    uv run --env-file .env python scripts/void_audit_run.py --run-id N --reason "..."

``--env-file .env`` is how the Discord webhook reaches this process: nothing
under ``corollary/`` reads ``.env`` (CLAUDE.md). Without it the void still
happens, the Discord delivery is recorded ``dropped``, and the exit status says
so.

What it does, on the database the app uses (``COROLLARY_DATABASE_URL``, or
``corollary.db`` in the working directory, whose absolute path is printed
first): :func:`corollary.data.news.audit_void.void_audit_run`, one short
transaction that holds SQLite's write lock from its first read to its commit
(``audit_void.take_write_lock``), so it may run with the app up. A run that
opens meanwhile waits for it, and a void that arrives during a run's open
waits and is then refused. Then, **only after that commit**, one
``sentiment_audit_voided`` notification (``info``, no account), routed at emit
time from ``notification_route`` by the same
``EngineRuntime.notify_operator_action`` and delivered by the same three sinks
the app's lifespan builds: the log line, the bell's ``DbNotifier`` and the
``DiscordNotifier``. The app builds its stack inline in the lifespan, so there
is no builder to import. The sinks and the routing gate are the app's own
classes, not copies, and the Discord sink is built and started under the same
guards as the lifespan's: Discord unavailable means the bell only, logged.

**The run must be the latest non-voided terminal run**, and nothing may be
running. A failed run is voided like a succeeded one, whether or not it
graded or moved anything, so voids go newest first: a failed run after N is
voided before N. A refused void writes nothing, emits nothing, logs the rule
(JSON on stderr) and exits 1.

**Nothing after the commit can undo the void, and nothing is retried here.**
The notification goes out first and the summary is printed after it. Printing
replaces what the console cannot encode: a piped Windows console is cp1252,
and a reason holding a character outside it must not cost the notification.
Any failure from the commit on (a delivery, the sink, the console) is
logged, says the run IS voided, and exits 3. It never exits 1, which would
claim nothing was written. Re-running would be refused as ``already_voided``
anyway.

Exit status:

* **0**: voided, and every routed channel took the notification.
* **1**: refused. Nothing was written and nothing was sent.
* **2**: a bad argument, or a database that is not an existing SQLite file.
  Nothing was opened.
* **3**: voided, but something after the commit did not complete: a routed
  channel did not take the notification, or notifying or printing raised.
  The void stands. Do not re-run it.
* **4**: the void failed unexpectedly and was rolled back. Nothing was
  written and nothing was sent.

After a void, **the next scheduler start runs the slot's audit by itself**
when the voided run was that Saturday slot's only run: the start-up
catch-up no longer counts it (``scheduler._audit_runs_since_blocking``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import httpx

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from corollary.data.news.audit_void import (  # noqa: E402
    VOID_EVENT,
    VOID_SEVERITY,
    VoidRefused,
    VoidResult,
    void_audit_run,
    void_notice_text,
)
from corollary.db.models import NotificationDelivery  # noqa: E402
from corollary.db.session import create_db_engine, database_url, session_factory  # noqa: E402
from corollary.engine.notify import (  # noqa: E402
    DISCORD_MAX_RETRY_AFTER_SECONDS,
    DISCORD_RETRY_DELAY_SECONDS,
    DISCORD_TIMEOUT_SECONDS,
    DbNotifier,
    DiscordNotifier,
    FanoutNotifier,
)
from corollary.engine.runtime import (  # noqa: E402
    DISCORD_WEBHOOK_ENV,
    EngineRuntime,
    LoggingNotifier,
    Notification,
    OperatorNotice,
)

#: The exit statuses. The module docstring says what each one promises.
EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_BAD_INPUT: Final = 2
EXIT_NOT_DELIVERED: Final = 3
EXIT_VOID_FAILED: Final = 4

#: Under ``corollary.``, so :func:`_json_logs_to_stderr` writes its lines.
_LOGGER: Final = logging.getLogger("corollary.scripts.void_audit_run")

#: How long to wait for Discord before recording what did not go out. The
#: app's shutdown grace is 5 s because shutting down is the point there; here
#: the delivery is the point, so the wait covers one attempt, the longest
#: ``Retry-After`` honoured, and the retry.
DELIVERY_WAIT_SECONDS: Final = (
    2 * DISCORD_TIMEOUT_SECONDS + DISCORD_RETRY_DELAY_SECONDS + DISCORD_MAX_RETRY_AFTER_SECONDS
)

def _log_fields(record: logging.LogRecord) -> dict[str, object]:
    """The structured fields a void's log lines carry, each read **by literal
    name**: ``audit_void``'s refusal and success lines, this script's
    not-delivered and post-commit lines, the Discord sink's not-built,
    not-started and misconfigured lines, and the ``event`` /
    ``correlation_id`` / ``notification_event`` / ``channels`` the sinks set.
    Not ``vars(record)``: the rule 1 scan (``tests/test_hard_rules.py``)
    refuses ``vars``, ``__dict__`` and a computed ``getattr`` in every
    ``scripts/*.py``.

    **A field not listed here does not appear in the JSON line at all.** Only
    the message text (``record.getMessage()``) carries it, and only if the
    message was written to include it. Add a name here when a line this
    script can emit starts carrying a field worth reading. The bell's row is
    the notification's own record either way."""
    fields: dict[str, object] = {
        "event": getattr(record, "event", None),
        "rule": getattr(record, "rule", None),
        "detail": getattr(record, "detail", None),
        "inputs": getattr(record, "inputs", None),
        "facts": getattr(record, "facts", None),
        "at": getattr(record, "at", None),
        "audit_run_id": getattr(record, "audit_run_id", None),
        "reason": getattr(record, "reason", None),
        "archived_outcomes": getattr(record, "archived_outcomes", None),
        "restorations": getattr(record, "restorations", None),
        "notification_id": getattr(record, "notification_id", None),
        "notification_event": getattr(record, "notification_event", None),
        "channels": getattr(record, "channels", None),
        "problems": getattr(record, "problems", None),
        "error_type": getattr(record, "error_type", None),
        "variable": getattr(record, "variable", None),
        "correlation_id": getattr(record, "correlation_id", None),
    }
    return {key: value for key, value in fields.items() if value is not None}


class _JsonLines(logging.Formatter):
    """One JSON object per line: the message and the void's structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        fields = _log_fields(record)
        return json.dumps(
            {
                "logged_at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                **fields,
            },
            default=str,
        )


@contextmanager
def _json_logs_to_stderr() -> Iterator[None]:
    """``corollary.*`` at INFO as JSON lines on stderr, for this run only."""
    target = logging.getLogger("corollary")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonLines())
    level = target.level
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    try:
        yield
    finally:
        target.removeHandler(handler)
        target.setLevel(level)


def _sqlite_file(url: str) -> Path | None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _notice(result: VoidResult) -> OperatorNotice:
    title, body = void_notice_text(result)
    return OperatorNotice(
        event=VOID_EVENT,
        severity=VOID_SEVERITY,
        title=title,
        body=body,
        at=result.voided_at,
        correlation_id=result.correlation_id,
        account=None,
    )


async def notify(
    sessions: Callable[[], Session],
    result: VoidResult,
    *,
    environ: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[Notification, list[str]]:
    """Emit the void's one notification through the app's stack; what did not land.

    Returns the notification and one line per routed channel without a
    ``delivered`` row. The void has committed and nothing here can undo it.

    The Discord sink is built and started under the guards
    ``corollary.api.app``'s lifespan uses, for the same reason: Discord is
    optional and the record is not. On this machine a stale ``SSL_CERT_FILE``
    makes ``start()`` raise (``FileNotFoundError``). Construction that raises
    falls back to a disabled sink with no URL, and a ``start()`` that raises
    leaves the sink unstarted. Either way it records ``dropped`` for the
    notification at emit time. ``DbNotifier`` is a separate sink in the
    fan-out (``FanoutNotifier`` runs every sink whatever another does), so the
    bell's row is written regardless. The caller sees the drop in the
    returned problems and exits 3.
    """
    try:
        discord = DiscordNotifier(
            webhook_url=environ.get(DISCORD_WEBHOOK_ENV),
            session_factory=sessions,
            transport=transport,
            shutdown_grace_seconds=DELIVERY_WAIT_SECONDS,
        )
    except Exception as exc:
        # Class name only: the message may quote the URL (rule 6).
        _LOGGER.error(
            "the Discord sink could not be built; the void's notification is "
            "recorded as dropped there",
            extra={
                "event": "notification_discord_not_built",
                "variable": DISCORD_WEBHOOK_ENV,
                "error_type": type(exc).__name__,
            },
        )
        discord = DiscordNotifier(
            webhook_url=None,
            session_factory=sessions,
            disabled_reason=(
                f"the Discord sink could not be built ({type(exc).__name__}); "
                f"check {DISCORD_WEBHOOK_ENV}"
            ),
        )
    try:
        discord.start()
    except Exception as exc:
        # Stated, never fatal: the unstarted sink records a ``dropped`` row,
        # which is the trace this failure leaves.
        _LOGGER.error(
            "the Discord delivery task did not start; the void's notification "
            "is recorded as dropped there",
            extra={
                "event": "notification_discord_not_started",
                "error_type": type(exc).__name__,
            },
        )
    runtime = EngineRuntime(
        session_factory=sessions,
        notifier=FanoutNotifier([LoggingNotifier(), DbNotifier(session_factory=sessions), discord]),
        env=environ,
    )
    try:
        notification = runtime.notify_operator_action(_notice(result))
    finally:
        await discord.aclose()
    with sessions() as session:
        outcomes: dict[str, list[str]] = {}
        for row in session.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.notification_id == notification.id)
            .order_by(NotificationDelivery.id)
        ):
            outcomes.setdefault(row.channel, []).append(f"{row.status}: {row.detail}")
    problems = [
        f"{channel}: {'; '.join(outcomes.get(channel, ['no delivery row was written']))}"
        for channel in notification.channels
        if not any(line.startswith("delivered") for line in outcomes.get(channel, []))
    ]
    return notification, problems


def _say(text: str, *, err: bool = False) -> None:
    """``print``, with what the console cannot encode replaced rather than
    raised. A piped Windows console is cp1252 with strict errors, so a reason
    holding a character outside it (an arrow, say) would otherwise raise
    ``UnicodeEncodeError`` after the void has committed (finding M2). The
    database and the notification keep the exact text. Only the console copy
    is lossy."""
    stream = sys.stderr if err else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:  # an encoding name the codec registry does not know
        safe = text.encode("ascii", errors="replace").decode("ascii")
    print(safe, file=stream)


def _print_result(result: VoidResult) -> None:
    _say(f"voided audit run {result.run_id} at {result.voided_at.isoformat()}")
    _say(f"reason: {result.reason}")
    _say(f"archived {result.archived} label outcome(s) to label_outcome_voided")
    if not result.restorations:
        _say(f"restored: none (run {result.run_id}, {result.run_status}, moved no source)")
    for r in result.restorations:
        origin = "never audited" if r.audit_run_id is None else f"run {r.audit_run_id}"
        _say(f"restored {r.source}: {r.from_status} -> {r.to_status} ({origin})")


def _after_commit(
    result: VoidResult,
    sessions: Callable[[], Session],
    environ: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    """Everything after the void's commit: notify **first**, then print.
    :data:`EXIT_OK` or :data:`EXIT_NOT_DELIVERED`. The caller turns anything
    this raises into :data:`EXIT_NOT_DELIVERED` too."""
    notification, problems = asyncio.run(
        notify(sessions, result, environ=environ, transport=transport)
    )
    _print_result(result)
    _say(
        f"notified {VOID_EVENT} ({notification.id}) to: "
        f"{', '.join(notification.channels) or 'no channel (every route is off)'}"
    )
    if not problems:
        return EXIT_OK
    _LOGGER.error(
        "the void stands, but a routed channel did not take its notification",
        extra={
            "event": "sentiment_audit_void_not_delivered",
            "audit_run_id": result.run_id,
            "notification_id": notification.id,
            "problems": problems,
            "correlation_id": result.correlation_id,
        },
    )
    for line in problems:
        _say(f"not delivered -- {line}", err=True)
    _say(
        f"audit run {result.run_id} IS voided; only the notification is incomplete. "
        "Do not re-run the void.",
        err=True,
    )
    return EXIT_NOT_DELIVERED


def _voided_but(result: VoidResult, exc: BaseException) -> None:
    """The post-commit failure: logged, and the one sentence that matters
    printed. Neither may raise: this is the last thing the script says."""
    try:
        _LOGGER.error(
            "the void stands, but what follows its commit failed",
            extra={
                "event": "sentiment_audit_void_after_commit_failed",
                "audit_run_id": result.run_id,
                # Class name only: an exception's text may quote a URL (rule 6).
                "error_type": type(exc).__name__,
                "correlation_id": result.correlation_id,
            },
        )
    except Exception:  # pragma: no cover - a logging handler that itself raises
        pass
    try:
        _say(
            f"audit run {result.run_id} IS voided; what follows the commit failed "
            f"({type(exc).__name__}), so the notification may be incomplete -- check "
            "the bell. Do not re-run the void. It would be refused as already_voided.",
            err=True,
        )
    except Exception:  # pragma: no cover - a console that cannot be written at all
        pass


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] = _utc_now,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Void one run. ``clock``, ``environ`` and ``transport`` are for tests."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        epilog=(
            "Opens COROLLARY_DATABASE_URL, or corollary.db in the working directory, "
            "and prints its absolute path first. Launch with `uv run --env-file .env` "
            f"so {DISCORD_WEBHOOK_ENV} is set. Exit: 0 voided and notified, 1 refused, "
            "2 bad input, 3 voided but the notification is incomplete or something after "
            "the commit failed (do not re-run), 4 void failed and rolled back."
        ),
    )
    parser.add_argument("--run-id", required=True, type=int, help="the audit_run id to void")
    parser.add_argument(
        "--reason",
        required=True,
        help="why the run is voided; stored on the run and quoted in the notification",
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ

    url = database_url()
    path = _sqlite_file(url)
    if path is None:
        _say(
            f"{make_url(url).render_as_string(hide_password=True)} is not a SQLite "
            "database file; this script works on the app's SQLite file only.",
            err=True,
        )
        return EXIT_BAD_INPUT
    if not path.exists():
        _say(
            f"database file {path} does not exist; run from the repo root or set "
            "COROLLARY_DATABASE_URL. Nothing was created.",
            err=True,
        )
        return EXIT_BAD_INPUT
    _say(f"database: {path}")

    engine = create_db_engine(url)
    sessions = session_factory(engine)
    try:
        with _json_logs_to_stderr():
            try:
                result = void_audit_run(
                    sessions,
                    run_id=args.run_id,
                    reason=args.reason,
                    # Read by the void after it takes the write lock.
                    clock=clock,
                    correlation_id=uuid.uuid4().hex,
                )
            except VoidRefused as refused:
                _say(
                    f"refused ({refused.refusal.rule.value}): {refused.refusal.message}. "
                    "Nothing was written and nothing was sent.",
                    err=True,
                )
                return EXIT_REFUSED
            except Exception as exc:
                _say(
                    f"the void failed ({type(exc).__name__}: {exc}) and was rolled back; "
                    "nothing was written and nothing was sent.",
                    err=True,
                )
                return EXIT_VOID_FAILED
            # Committed. From here on nothing may exit 1 or 4, which say
            # "nothing was written": every outcome is 0 or 3 (finding M2).
            try:
                return _after_commit(result, sessions, env, transport)
            except (Exception, KeyboardInterrupt) as exc:
                _voided_but(result, exc)
                return EXIT_NOT_DELIVERED
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
