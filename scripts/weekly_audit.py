"""The unattended Saturday sentiment self-audit: start Corollary headless, wait, stop.

Windows Task Scheduler runs this from the repo root, Saturday 09:45 local
(the machine is on Eastern time)::

    uv run python scripts/weekly_audit.py

It never places, cancels or reads an order, never POSTs anything, and never
halts or resumes the engine. What it does, in order:

1. ``GET /api/health`` on 127.0.0.1:8000. If anything answers, another
   Corollary is already running: its own scheduler runs the audit, and a
   second process would contend for the database. Logged, exit 0.
2. **A trading day before its close: launch nothing** (``deferred_to_app``,
   exit 0). Task Scheduler runs a missed launch late -- Monday 08:00, say --
   and ``headless_app`` would then hold port 8000 into the session: the
   owner's ``uvicorn corollary.api:app`` fails to bind, or the browser
   silently reaches an app with no sockets. ``:app`` and ``dev_app`` carry the
   same start-up catch-up, so the launch would add nothing. "Trading day" and
   "close" are the market calendar's (:func:`session_close_ahead`), never a
   hardcoded 16:00. Weekends and market holidays launch.
3. ``.env`` must exist -- ``exists()``, a stat; **this script never reads it**.
   Missing, nothing could authenticate: logged, exit 3, at once.
4. Launch ``python -m uvicorn corollary.api:headless_app`` -- no vendor
   sockets, Discord on -- with ``--env-file <repo>/.env``: the launcher loads
   the keys (rule 6). ``sys.executable -m uvicorn``, because the uvicorn
   console shim is blocked on this machine.
5. Wait, bounded, for health.
6. Poll ``GET /api/settings/sentiment`` until ``lastRun`` is a run that
   started at or after this week's Saturday 10:00 ET slot and has finished.
   The run is the scheduled job's, or its start-up catch-up's -- never a
   manual POST from here. The deadline is two hours, counted from the
   **latest** of now, the slot, and today's session close: a launch before
   08:00 ET would otherwise give up before the 10:00 slot.
7. **Stop early when the slot passed with no run** (exit 4): ``nextRunAt``
   has moved past the slot and ``lastRun`` started before it, seen on two
   consecutive reads. A skip writes no ``audit_run`` row, so without this the
   wait ran out its deadline and reported a timeout. The route reads
   ``lastRun`` before ``nextRunAt``, so one read can show a run that opened
   and finished between the two as a skip; the second read, made after
   ``nextRunAt`` had moved, cannot. **The skip's reason is not available
   here**: no route exposes ``sentiment_audit``'s ``JobStatus.last_skip_reason``
   (``/api/calendar`` serves it for the calendar jobs only). Only a start-up
   catch-up skip is logged at WARNING (``sentiment_audit_catch_up``); a skip of
   the 10:00 slot itself is logged at INFO and prints nowhere, and a run that
   failed before writing a row also reads here as a skip. So the reason is not
   recoverable from this script; exposing a skip code on the API is a follow-up.
8. Stop the server -- a graceful signal first, so the lifespan's ``aclose()``
   runs, then a kill after a grace period -- and log which one it took.

Logs go to ``logs/weekly-audit.log``, appended (one JSON object per line,
UTC). They carry what this script decided and the ``audit_run`` fields it read
-- never a response body, never an exception message, so nothing
vendor-shaped or secret can reach them.

**The server's own output goes to** ``DEVNULL`` **and is never written to
disk** (rule 6: "no keys ... in log output"). Its stderr carries tracebacks
that ``logging.lastResort`` renders raw, chained ``__cause__`` included, which
bypass :func:`corollary.wire.vendor_detail`; an httpx transport error can name
a URL such as FRED's with ``api_key=`` in its query string. Scrubbing was
rejected because ``corollary.wire`` has no pattern that removes a short
query-string secret (``api_key=SECRET123``) or a ~20-character Finnhub token
without knowing the value -- and this script never knows the values.

**Known limits, accepted:**

* If Task Scheduler terminates this script, the server child can be orphaned
  and keep port 8000. The task's own time limit is set above this script's
  longest wait (health bound + :data:`RUN_DEADLINE` past the slot or close +
  :data:`STOP_GRACE` twice) so that it does not.
* The already-running check looks at port 8000 only. A Corollary on another
  port is not seen, and both would run against the one database.
* A ``failed`` run, or a ``running`` row younger than the runner's
  abandonment bound, counts as the slot's run -- here (``failed`` exits 1)
  and in the engine's catch-up, which therefore does not rerun it.
* Past the market calendar's published range a weekday has no session, so
  it is treated as a holiday and launches -- as the scheduler's own calendar
  reads it.

Exit codes, for Task Scheduler's *last run result*: see :class:`Exit`.
"""

from __future__ import annotations

import enum
import json
import logging
import signal
import subprocess
import sys
import time as wall
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import httpx

from corollary.calendars import NYSE_TZ, nyse_session_close
from corollary.engine.scheduler import SENTIMENT_AUDIT_AT, SENTIMENT_AUDIT_DAYS

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "weekly-audit.log"
ENV_FILE = REPO_ROOT / ".env"

HOST = "127.0.0.1"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}"
HEALTH_PATH = "/api/health"
SENTIMENT_PATH = "/api/settings/sentiment"

#: How long the server may take to answer health after launch.
HEALTH_TIMEOUT = timedelta(minutes=3)
HEALTH_POLL = timedelta(seconds=2)
#: How long to wait for the audit run to finish, from the latest of the
#: start, the slot, and today's session close.
RUN_DEADLINE = timedelta(hours=2)
RUN_POLL = timedelta(seconds=30)
#: How long the graceful stop may take before the kill. The lifespan drains
#: the context sessions and the Discord sink on the way out.
STOP_GRACE = timedelta(seconds=60)
#: Per-request timeout on the loopback calls.
REQUEST_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger("weekly_audit")


class Exit(enum.IntEnum):
    """The process exit codes. Non-zero is what Task Scheduler reports as failure."""

    #: The run finished ``succeeded``, another Corollary was already up, or
    #: today is a trading day still ahead of its close (``deferred_to_app``).
    OK = 0
    #: The run finished ``failed`` (the app recorded why in ``audit_run``).
    RUN_FAILED = 1
    #: No finished run since the slot before the deadline.
    TIMED_OUT = 2
    #: The server did not come up, died while we waited, or ``.env`` is missing.
    SERVER_FAILED = 3
    #: The slot passed with no ``audit_run`` row: the scheduler skipped it, or
    #: the run failed before writing a row. The reason is not recoverable here.
    SKIPPED = 4


# --------------------------------------------------------------- pure parts


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"expected an aware datetime, got naive {moment!r}")
    return moment.astimezone(timezone.utc)


def audit_slot_for(now: datetime) -> datetime:
    """This week's Saturday 10:00 ET slot, in UTC -- the one the wait is for.

    The newest audit **day** on or before ``now``'s ET date, at
    :data:`SENTIMENT_AUDIT_AT` -- so on the Saturday itself it is today's
    slot even before 10:00 (the 09:45 launch waits for the 10:00 run), and on
    a later day it is the Saturday just gone (a missed launch, run late,
    waits for the catch-up's run). The day rule and the time are the
    scheduler's own constants, never re-typed here.
    """
    today = _require_utc(now).astimezone(NYSE_TZ).date()
    for back in range(7):
        day = today - timedelta(days=back)
        if SENTIMENT_AUDIT_DAYS(day):
            return datetime.combine(day, SENTIMENT_AUDIT_AT, tzinfo=NYSE_TZ).astimezone(
                timezone.utc
            )
    raise AssertionError("a weekly rule always has a day within seven")  # pragma: no cover


def session_close_ahead(now: datetime) -> datetime | None:
    """Today's session close, if today is a trading day and ``now`` is before it.

    ``None`` on a weekend, a market holiday, a date past the calendar's
    published range, and at or after the close -- the calendar's, so the
    half-day after Thanksgiving is 13:00 ET. Before the open counts as before
    the close: a Monday 08:00 launch would hold the port into the session.
    """
    moment = _require_utc(now)
    closes = nyse_session_close(moment.astimezone(NYSE_TZ).date())
    if closes is None or moment >= closes:
        return None
    return closes


def wait_deadline(now: datetime) -> datetime:
    """When the wait for the run gives up: :data:`RUN_DEADLINE` after the
    **latest** of ``now``, the slot (:func:`audit_slot_for`, which on a
    Saturday morning is still ahead), and today's session close (the
    calendar's). Launched on time, on a Saturday, that is the slot + 2 h.
    """
    moment = _require_utc(now)
    start = max(moment, audit_slot_for(moment))
    closes = nyse_session_close(moment.astimezone(NYSE_TZ).date())
    if closes is not None:
        start = max(start, closes)
    return start + RUN_DEADLINE


@dataclass(frozen=True, slots=True)
class LastRun:
    """``lastRun`` from ``GET /api/settings/sentiment``, parsed. Instants UTC."""

    id: int
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuditStatus:
    """The two fields of ``GET /api/settings/sentiment`` the wait reads."""

    last_run: LastRun | None
    #: The scheduled job's next slot; ``None`` while its start-up catch-up is
    #: still in progress (the loop has not asked for one yet), or with no
    #: scheduler.
    next_run_at: datetime | None


class RunState(enum.Enum):
    #: No run since the slot yet, or one still ``running``: keep waiting.
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _parse_instant(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} is not a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # ``fromisoformat``'s own message quotes the value; this names the field.
        raise ValueError(f"{name} is not an ISO 8601 instant") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} carries no UTC offset")
    return parsed.astimezone(timezone.utc)


def parse_last_run(body: object) -> LastRun | None:
    """``lastRun`` out of the status body, or ``None`` when no run exists.

    Raises :class:`ValueError` on a shape it does not recognise. The message
    names the field, never the value.
    """
    if not isinstance(body, Mapping):
        raise ValueError("the sentiment status body is not an object")
    raw = body.get("lastRun")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("lastRun is not an object")
    run_id, trigger, status = raw.get("id"), raw.get("trigger"), raw.get("status")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        raise ValueError("lastRun.id is not an integer")
    if not isinstance(trigger, str) or not isinstance(status, str):
        raise ValueError("lastRun.trigger or lastRun.status is not a string")
    finished = raw.get("finishedAt")
    return LastRun(
        id=run_id,
        trigger=trigger,
        status=status,
        started_at=_parse_instant(raw.get("startedAt"), "lastRun.startedAt"),
        finished_at=None if finished is None else _parse_instant(finished, "lastRun.finishedAt"),
    )


def parse_status(body: object) -> AuditStatus:
    """``lastRun`` and ``nextRunAt`` out of the status body. Raises like
    :func:`parse_last_run`, naming the field and never the value."""
    last_run = parse_last_run(body)
    assert isinstance(body, Mapping)  # parse_last_run refused anything else
    raw_next = body.get("nextRunAt")
    return AuditStatus(
        last_run=last_run,
        next_run_at=None if raw_next is None else _parse_instant(raw_next, "nextRunAt"),
    )


def run_state(last_run: LastRun | None, slot: datetime) -> RunState:
    """Whether ``last_run`` is a finished run for ``slot``.

    It must have started **at or after** the slot -- last week's run is not
    this week's -- and be finished: ``succeeded`` or ``failed`` with a
    ``finishedAt``. Any trigger counts; a manual run since the slot graded
    the same labels.
    """
    if last_run is None or last_run.started_at < _require_utc(slot):
        return RunState.WAITING
    if last_run.finished_at is None:
        return RunState.WAITING
    if last_run.status == "succeeded":
        return RunState.SUCCEEDED
    if last_run.status == "failed":
        return RunState.FAILED
    return RunState.WAITING


def slot_passed_without_run(status: AuditStatus, slot: datetime) -> bool:
    """The scheduler has moved past ``slot`` and no run started at or after it.

    ``nextRunAt`` strictly after the slot means the slot's turn -- the
    scheduled run, or the start-up catch-up that precedes the loop -- is
    over; ``lastRun`` older than the slot (or absent) means it wrote nothing.
    ``nextRunAt`` ``None`` is a catch-up still in progress, never a skip.
    One read is not enough to act on: see the module docstring, step 7.
    """
    bound = _require_utc(slot)
    if status.next_run_at is None or status.next_run_at <= bound:
        return False
    return status.last_run is None or status.last_run.started_at < bound


# ---------------------------------------------------------- the side effects


class Server(Protocol):
    """The launched server, as the flow sees it."""

    def exited(self) -> int | None:
        """The exit code once the process has ended, else ``None``."""
        ...

    def stop(self) -> str:
        """Stop it; return how (``"graceful"``, ``"killed"``, ``"already_exited"``)."""
        ...


@dataclass(frozen=True, slots=True)
class Effects:
    """Everything :func:`run_weekly_audit` does to the world, injectable for a test."""

    #: ``GET /api/health`` answered 200.
    health: Callable[[], bool]
    #: ``GET /api/settings/sentiment``'s ``lastRun`` and ``nextRunAt``.
    #: Raises on any failure.
    read_status: Callable[[], AuditStatus]
    #: Whether ``.env`` exists. A stat, never a read.
    env_file_present: Callable[[], bool]
    launch: Callable[[], Server]
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]


def run_weekly_audit(effects: Effects) -> Exit:
    """The whole flow. See the module docstring for the steps."""
    now = _require_utc(effects.clock())
    if effects.health():
        _log(
            "already_running",
            "another Corollary is answering on the port; its own scheduler runs "
            "the audit, so this run starts nothing",
            url=BASE_URL + HEALTH_PATH,
            at=now,
        )
        return Exit.OK

    closes = session_close_ahead(now)
    if closes is not None:
        _log(
            "deferred_to_app",
            "today is a trading day and its session has not closed; launching would "
            "hold the port into the session, and the owner's app runs the same "
            "start-up catch-up, so this run starts nothing",
            session_close=closes,
            at=now,
        )
        return Exit.OK

    if not effects.env_file_present():
        _log("env_file_missing", "the .env file the server is launched with does not exist",
             path=str(ENV_FILE), at=now, level=logging.ERROR)
        return Exit.SERVER_FAILED

    slot = audit_slot_for(now)
    deadline = wait_deadline(now)
    _log("launching", "launching headless_app", slot=slot, deadline=deadline, at=now)
    server = effects.launch()
    try:
        return _wait(effects, server, slot, deadline)
    finally:
        how = server.stop()
        _log("stopped", f"server stopped ({how})", how=how, at=effects.clock())


def _wait(effects: Effects, server: Server, slot: datetime, deadline: datetime) -> Exit:
    health_by = _require_utc(effects.clock()) + HEALTH_TIMEOUT
    while not effects.health():
        code = server.exited()
        if code is not None:
            _log("server_exited", "the server exited before answering health",
                 exit_code=code, at=effects.clock(), level=logging.ERROR)
            return Exit.SERVER_FAILED
        if _require_utc(effects.clock()) >= health_by:
            _log("health_timeout", "the server did not answer health in time",
                 bound_seconds=int(HEALTH_TIMEOUT.total_seconds()), at=effects.clock(),
                 level=logging.ERROR)
            return Exit.SERVER_FAILED
        effects.sleep(HEALTH_POLL.total_seconds())
    _log("healthy", "the server answers health", at=effects.clock())

    last_seen: AuditStatus | None = None
    skip_seen = False
    while True:
        now = _require_utc(effects.clock())
        code = server.exited()
        if code is not None:
            _log("server_exited", "the server exited while waiting for the run",
                 exit_code=code, at=now, level=logging.ERROR)
            return Exit.SERVER_FAILED
        try:
            status = effects.read_status()
        except Exception as exc:  # stated, then retried until the deadline
            skip_seen = False  # a skip is confirmed on consecutive reads only
            _log("status_read_failed", "the sentiment status could not be read",
                 error_type=type(exc).__name__, at=now, level=logging.WARNING)
        else:
            last_seen = status
            last_run = status.last_run
            state = run_state(last_run, slot)
            if state is not RunState.WAITING:
                assert last_run is not None
                _log("run_finished", f"the audit run finished {state.value}",
                     **_run_fields(last_run), slot=slot, at=now,
                     level=logging.INFO if state is RunState.SUCCEEDED else logging.ERROR)
                return Exit.OK if state is RunState.SUCCEEDED else Exit.RUN_FAILED
            if slot_passed_without_run(status, slot):
                if skip_seen:
                    _log("run_skipped",
                         "the scheduler moved past the slot and no audit run started for "
                         "it: the run was skipped, or failed before writing a row. The API does "
                         "not expose the reason, so it is not recoverable from here",
                         slot=slot, at=now, level=logging.ERROR, **_status_fields(status))
                    return Exit.SKIPPED
                skip_seen = True
            else:
                skip_seen = False
        if now >= deadline:
            # The newest status the route showed, whatever its week or state --
            # "still running" and "last week's" read differently the morning after.
            seen = {} if last_seen is None else _status_fields(last_seen)
            _log("run_timeout", "no finished audit run since the slot before the deadline",
                 slot=slot, deadline=deadline, at=now, level=logging.ERROR, **seen)
            return Exit.TIMED_OUT
        effects.sleep(RUN_POLL.total_seconds())


def _run_fields(run: LastRun) -> dict[str, object]:
    return {
        "run_id": run.id,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _status_fields(status: AuditStatus) -> dict[str, object]:
    runs = {} if status.last_run is None else _run_fields(status.last_run)
    return {**runs, "next_run_at": status.next_run_at}


# ------------------------------------------------------------ real effects


def _get(client: httpx.Client, path: str) -> httpx.Response:
    return client.get(BASE_URL + path, timeout=REQUEST_TIMEOUT_SECONDS)


def http_health(client: httpx.Client) -> bool:
    try:
        return _get(client, HEALTH_PATH).status_code == 200
    except httpx.HTTPError:
        return False


def http_status(client: httpx.Client) -> AuditStatus:
    response = _get(client, SENTIMENT_PATH)
    if response.status_code != 200:
        # The status code only: the body is not logged, whatever it says.
        raise RuntimeError(f"GET {SENTIMENT_PATH} answered {response.status_code}")
    return parse_status(response.json())


@dataclass(slots=True)
class UvicornServer:
    """``python -m uvicorn corollary.api:headless_app`` as a child process."""

    process: subprocess.Popen[bytes]

    @classmethod
    def launch(cls) -> UvicornServer:
        command = [
            sys.executable, "-m", "uvicorn", "corollary.api:headless_app",
            "--host", HOST, "--port", str(PORT),
            "--env-file", str(ENV_FILE),
            "--no-access-log",
        ]
        # A process group of its own on Windows, so CTRL_BREAK_EVENT reaches
        # it (uvicorn handles SIGBREAK there) and not this script.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            # Rule 6: never to disk. Its tracebacks are unscrubbed -- see the
            # module docstring.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        return cls(process=process)

    def exited(self) -> int | None:
        return self.process.poll()

    def stop(self) -> str:
        if self.process.poll() is not None:
            return "already_exited"
        graceful = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        try:
            self.process.send_signal(graceful)
        except OSError as exc:
            _log("graceful_signal_failed", "the graceful signal could not be sent",
                 error_type=type(exc).__name__, at=utc_now(), level=logging.WARNING)
        else:
            try:
                self.process.wait(timeout=STOP_GRACE.total_seconds())
                return "graceful"
            except subprocess.TimeoutExpired:
                pass
        self.process.kill()
        self.process.wait(timeout=STOP_GRACE.total_seconds())
        return "killed"


# ----------------------------------------------------------------- logging


class _JsonLines(logging.Formatter):
    """One JSON object per line. Every field is the script's own -- see :func:`_log`."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        return json.dumps(
            {
                "logged_at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                **fields,
            },
            default=str,
        )


def _log(event: str, message: str, *, level: int = logging.INFO, **fields: object) -> None:
    """One structured line. ``fields`` are this script's own values -- instants,
    ids, codes, exception *type names* -- never a response body or an
    exception message."""
    clean = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in fields.items()
    }
    logger.log(level, message, extra={"fields": {"event": event, **clean}})


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = _JsonLines()
    # Appended: one file is the history of every Saturday.
    to_file = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    to_file.setFormatter(formatter)
    to_stderr = logging.StreamHandler(sys.stderr)
    to_stderr.setFormatter(formatter)
    logger.handlers[:] = [to_file, to_stderr]
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main() -> int:
    configure_logging()
    with httpx.Client() as client:
        effects = Effects(
            health=lambda: http_health(client),
            read_status=lambda: http_status(client),
            env_file_present=ENV_FILE.exists,
            launch=UvicornServer.launch,
            clock=utc_now,
            sleep=wall.sleep,
        )
        try:
            outcome = run_weekly_audit(effects)
        except Exception as exc:
            _log("crashed", "the weekly audit runner failed", error_type=type(exc).__name__,
                 at=utc_now(), level=logging.ERROR)
            return int(Exit.SERVER_FAILED)
    _log("exit", f"exiting {outcome.name}", exit_code=int(outcome), at=utc_now())
    return int(outcome)


if __name__ == "__main__":
    sys.exit(main())
