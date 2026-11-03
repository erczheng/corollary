"""The unattended weekday session: run Corollary headless through a trading day.

News labels are collected only while the backend runs, and the sentiment audit
needs a month of them. Windows Task Scheduler runs this from the repo root at
**09:00 local on weekdays** ("wake the computer to run this task") and again
**at logon** (the machine is on Eastern time)::

    uv run python scripts/weekday_session.py

It never places, cancels or reads an order, never sends anything but
``GET /api/health``, and never halts or resumes the engine: rule 9 never
auto-resumes, and an auto-start must not become one. What it does, in order:

1. **No session today, or past today's close: launch nothing** (exit 0).
   "Session" and "close" are the market calendar's
   (:func:`corollary.calendars.nyse_session_close`), never a hardcoded 16:00:
   weekends and market holidays have none, and the half-day after
   Thanksgiving closes at 13:00 ET. A weekday with no session because the
   calendar's published range has ended is logged at ERROR
   (``calendar_exhausted``) rather than read as a holiday, since from then on
   nothing would be collected and every line would say why not wrongly.
2. ``GET /api/health`` on 127.0.0.1:8000. If it answers 200, the owner's
   own app or another runner is up (``already_running``, exit 0). A non-200
   listener on 8000 is not counted: the launch then fails to bind and the run
   exits 3, which is safe.
3. ``.env`` must exist -- ``exists()``, a stat; **this script never reads
   it**. Missing, nothing could authenticate: logged, exit 3.
4. Hold the machine awake (below), then launch
   ``python -m uvicorn corollary.api:headless_app`` -- no vendor sockets, so a
   laptop that sleeps cannot trip rule 9's dead-man's switch; Discord on --
   with ``--env-file <repo>/.env``: the launcher loads the keys (rule 6).
   ``sys.executable -m uvicorn``, because the uvicorn console shim is blocked
   on this machine. Wait, bounded, for health (exit 3 if it never comes).
5. Run until **the calendar's close plus** :data:`POST_CLOSE`, so the
   post-close news cycle is captured. Every :data:`SESSION_POLL` it checks the
   process and health. A server that exits, or stops answering health for
   :data:`HEALTH_LOSS_AFTER`, ends the run with exit 5 -- **logged, never
   relaunched**: a crash that repeats would otherwise loop all day, and a
   restart is the owner's call.
6. Stop the server -- a graceful signal first, so the lifespan's ``aclose()``
   runs, then a kill after a grace period -- and release the keep-awake.

**Keep-awake.** On Windows, ``SetThreadExecutionState(ES_CONTINUOUS |
ES_SYSTEM_REQUIRED)`` from the launch to the stop, then ``ES_CONTINUOUS`` alone
to clear it -- in a ``finally``, so an exception clears it too. That is a
request scoped to this thread, not a power-plan change: the owner's global
settings are untouched, so the laptop still sleeps normally at night. It
holds off *idle* sleep only -- closing the lid or choosing Sleep still sleeps,
and the session resumes on wake, ending at once if the window has passed. If
this process is killed outright the ``finally`` never runs, but Windows drops
a thread's execution state when the thread ends, so a killed runner cannot
leave the machine awake. Off Windows it does nothing. Both are logged, with
``applied``.

**Stopping it, to run the full terminal.** While this runs it holds port
8000, so ``uvicorn corollary.api:app`` (the one with sockets) cannot bind.
Either:

* **kill the server it launched** -- the process listening on 8000; the
  ``healthy`` log line carries its ``pid``, or
  ``Get-NetTCPConnection -LocalPort 8000 -State Listen`` names it. The runner
  notices within a poll, logs ``server_exited``, exits 5 without relaunching
  and clears its keep-awake; or
* **end the task in Task Scheduler**, or kill this script's own process. The
  server child can outlive it (it is in its own process group, so the
  graceful stop can reach it); if 8000 still answers afterwards, kill that
  process too, as above.

The next 09:00 or logon trigger starts a fresh run as usual; a run that finds
the owner's app up exits ``already_running``.

**Task Scheduler settings this assumes:** "If the task is already running:
Do not start a new instance" (the 09:00 and at-logon triggers can coincide);
"Start the task only if the computer is on AC power" *cleared*, or a laptop
on battery collects nothing; "Stop if the computer switches to battery
power" *cleared* too (``New-ScheduledTaskSettingsSet`` defaults it on, and a
stop there can orphan the server on 8000); and a time limit above the longest run -- a
logon just after midnight runs until the close plus 30 minutes, so the
default of three days is fine and anything under a day is not.

**Two things the owner should know while it runs.** From 09:00 to the close
plus 30 minutes on a trading day, a browser on the Vite dev server reaches
*this* app, which has no live vendor stream and nothing feeding rule 9's
connection checks, and nothing on screen says so yet. No order path exists
today, so nothing is at risk; before ``BrokerExecution`` lands (Phase 6) the
terminal must show the socket mode and refuse orders without a live stream.
And uvicorn runs the whole lifespan *before* it binds the port, so an
``uvicorn corollary.api:app`` started while this holds 8000 briefly
bootstraps against the same database, starts its scheduler's catch-ups, and
then shuts down on the failed bind. It opens no vendor socket, because the
supervisor sleeps before its first tick, so there is no 406 and no halt; the
worst case is a duplicated catch-up call. Stop this runner first, as above.

Logs go to ``logs/weekday-session.log``, appended (one JSON object per line,
UTC), carrying only this script's own decisions, instants, codes and
exception *type names* -- never a response body, never an exception message.
**The server's own output goes to** ``DEVNULL`` **and is never written to
disk** (rule 6: "no keys ... in log output"), for the reason
``scripts/weekly_audit.py`` gives: its tracebacks render unscrubbed, and an
httpx transport error can name a URL with a key in its query string.

**Known limits, accepted:**

* The already-running check looks at port 8000 only. A Corollary on another
  port is not seen, and both would run against the one database.
* A run that found the owner's app up does not take over when that app is
  later closed: nothing is collected for the rest of that day unless a logon
  trigger fires again.
* This shares nothing with ``scripts/weekly_audit.py`` by import: that
  script's tests pin its own source text, and ``scripts/`` is not a package.
  The launch, health and logging pieces are duplicated deliberately.

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, cast

import httpx

from corollary.calendars import NYSE_TZ, nyse_next_open, nyse_session_close

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "weekday-session.log"
ENV_FILE = REPO_ROOT / ".env"

HOST = "127.0.0.1"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}"
HEALTH_PATH = "/api/health"

#: How long past the calendar's close the server keeps running, so the
#: post-close news cycle is captured.
POST_CLOSE = timedelta(minutes=30)
#: How long the server may take to answer health after launch.
HEALTH_TIMEOUT = timedelta(minutes=3)
HEALTH_POLL = timedelta(seconds=2)
#: How often the running session checks the process and health.
SESSION_POLL = timedelta(seconds=30)
#: How long health may go unanswered, counted from the first failed check of
#: a streak, before the run gives up on its server.
HEALTH_LOSS_AFTER = timedelta(minutes=5)
#: And how many consecutive failed checks that takes at least -- so one failed
#: check after a long clock jump (a laptop woken from sleep) is not a loss.
HEALTH_LOSS_MIN_FAILURES = 3
#: How long the graceful stop may take before the kill.
STOP_GRACE = timedelta(seconds=60)
#: Per-request timeout on the loopback health call.
REQUEST_TIMEOUT_SECONDS = 10.0

#: ``SetThreadExecutionState`` flags, from ``winbase.h``.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

logger = logging.getLogger("weekday_session")


class Exit(enum.IntEnum):
    """The process exit codes. Non-zero is what Task Scheduler reports as failure."""

    #: The window closed with the server still up, or there was nothing to do:
    #: no session today, past the close, or a Corollary already running.
    OK = 0
    #: ``.env`` is missing, or the server never came up.
    SERVER_FAILED = 3
    #: The server exited, or stopped answering health, mid-session. Not relaunched.
    SERVER_DIED = 5


# --------------------------------------------------------------- pure parts


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"expected an aware datetime, got naive {moment!r}")
    return moment.astimezone(timezone.utc)


class Decision(enum.Enum):
    #: A session today still ahead of its close: launch.
    RUN = "run"
    #: Weekend or market holiday.
    NO_SESSION = "no_session"
    #: Today's session has closed.
    AFTER_CLOSE = "after_close"
    #: No session today, and none in the calendar's look-ahead either: the
    #: published schedule has ended, not the market.
    CALENDAR_EXHAUSTED = "calendar_exhausted"


@dataclass(frozen=True, slots=True)
class Plan:
    """What :func:`decide` concluded. Instants UTC."""

    kind: Decision
    #: Today's New York date -- the session's, not UTC's.
    trading_day: date
    session_close: datetime | None = None
    #: When the server is stopped: ``session_close`` + :data:`POST_CLOSE`.
    window_end: datetime | None = None


def decide(now: datetime) -> Plan:
    """Whether to run today and until when, from the market calendar.

    "Today" is New York's date, so 22:00 ET Monday -- 03:00 UTC Tuesday -- is
    Monday, closed. At the close exactly the session is over: a launch inside
    the post-close half hour starts nothing.
    """
    moment = _require_utc(now)
    today = moment.astimezone(NYSE_TZ).date()
    closes = nyse_session_close(today)
    if closes is None:
        try:
            nyse_next_open(moment)
        except ValueError:
            return Plan(Decision.CALENDAR_EXHAUSTED, today)
        return Plan(Decision.NO_SESSION, today)
    if moment >= closes:
        return Plan(Decision.AFTER_CLOSE, today, session_close=closes)
    return Plan(Decision.RUN, today, session_close=closes, window_end=closes + POST_CLOSE)


class HealthWatch:
    """Whether the running server has stopped answering health.

    The streak is timed from its **first failed check**, not from the last
    success: a laptop that slept with its lid shut wakes with the clock hours
    on, and that gap is not minutes of silence. A loss is
    :data:`HEALTH_LOSS_AFTER` of failures spanning at least
    :data:`HEALTH_LOSS_MIN_FAILURES` checks; any success resets it.
    """

    def __init__(self) -> None:
        self._streak_started: datetime | None = None
        self._failures = 0

    @property
    def failures(self) -> int:
        return self._failures

    def observe(self, healthy: bool, at: datetime) -> bool:
        moment = _require_utc(at)
        if healthy:
            self._streak_started = None
            self._failures = 0
            return False
        if self._streak_started is None:
            self._streak_started = moment
        self._failures += 1
        return (
            self._failures >= HEALTH_LOSS_MIN_FAILURES
            and moment - self._streak_started >= HEALTH_LOSS_AFTER
        )


# ---------------------------------------------------------- the side effects


class Server(Protocol):
    """The launched server, as the flow sees it."""

    @property
    def pid(self) -> int: ...

    def exited(self) -> int | None:
        """The exit code once the process has ended, else ``None``."""
        ...

    def stop(self) -> str:
        """Stop it; return how (``"graceful"``, ``"killed"``, ``"already_exited"``)."""
        ...


@dataclass(frozen=True, slots=True)
class Effects:
    """Everything :func:`run_weekday_session` does to the world, injectable for a test."""

    #: ``GET /api/health`` answered 200.
    health: Callable[[], bool]
    #: Whether ``.env`` exists. A stat, never a read.
    env_file_present: Callable[[], bool]
    launch: Callable[[], Server]
    #: Hold off idle sleep; whether it was applied.
    hold_awake: Callable[[], bool]
    #: Clear that hold; whether it was applied.
    release_awake: Callable[[], bool]
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]


def run_weekday_session(effects: Effects) -> Exit:
    """The whole flow. See the module docstring for the steps."""
    now = _require_utc(effects.clock())
    plan = decide(now)
    if plan.kind is Decision.CALENDAR_EXHAUSTED:
        _log("calendar_exhausted",
             "the market calendar has no session today or in its look-ahead: its "
             "published range has ended, so nothing will launch until it is updated",
             trading_day=plan.trading_day.isoformat(), at=now, level=logging.ERROR)
        return Exit.OK
    if plan.kind is Decision.NO_SESSION:
        _log("not_a_trading_day", "the market calendar has no session today",
             trading_day=plan.trading_day.isoformat(), at=now)
        return Exit.OK
    if plan.kind is Decision.AFTER_CLOSE:
        _log("after_close", "today's session has already closed",
             trading_day=plan.trading_day.isoformat(), session_close=plan.session_close,
             at=now)
        return Exit.OK
    assert plan.session_close is not None and plan.window_end is not None

    if effects.health():
        _log("already_running",
             "a Corollary is already answering on the port (the owner's app or another "
             "runner), so this run starts nothing",
             url=BASE_URL + HEALTH_PATH, at=now)
        return Exit.OK

    if not effects.env_file_present():
        _log("env_file_missing", "the .env file the server is launched with does not exist",
             path=str(ENV_FILE), at=now, level=logging.ERROR)
        return Exit.SERVER_FAILED

    applied = effects.hold_awake()
    _log("keep_awake_set", "holding off idle sleep for the session", applied=applied,
         at=effects.clock())
    try:
        _log("launching", "launching headless_app", trading_day=plan.trading_day.isoformat(),
             session_close=plan.session_close, window_end=plan.window_end,
             at=effects.clock())
        server = effects.launch()
        try:
            return _run(effects, server, plan.window_end)
        finally:
            how = server.stop()
            _log("stopped", f"server stopped ({how})", how=how, at=effects.clock())
    finally:
        cleared = effects.release_awake()
        _log("keep_awake_cleared", "idle sleep allowed again", applied=cleared,
             at=effects.clock())


def _run(effects: Effects, server: Server, window_end: datetime) -> Exit:
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
    _log("healthy", "the server answers health", pid=server.pid, at=effects.clock())

    watch = HealthWatch()
    while True:
        now = _require_utc(effects.clock())
        if now >= window_end:
            _log("window_closed", "the session window has closed", window_end=window_end,
                 at=now)
            return Exit.OK
        code = server.exited()
        if code is not None:
            _log("server_exited",
                 "the server exited mid-session; not relaunching",
                 exit_code=code, at=now, level=logging.ERROR)
            return Exit.SERVER_DIED
        if watch.observe(effects.health(), now):
            _log("health_lost",
                 "the server stopped answering health; stopping it, not relaunching",
                 failed_checks=watch.failures,
                 bound_seconds=int(HEALTH_LOSS_AFTER.total_seconds()), at=now,
                 level=logging.ERROR)
            return Exit.SERVER_DIED
        effects.sleep(min(SESSION_POLL, window_end - now).total_seconds())


# ------------------------------------------------------------ real effects


def _get(client: httpx.Client, path: str) -> httpx.Response:
    return client.get(BASE_URL + path, timeout=REQUEST_TIMEOUT_SECONDS)


def http_health(client: httpx.Client) -> bool:
    try:
        return _get(client, HEALTH_PATH).status_code == 200
    except httpx.HTTPError:
        return False


def execution_state_call() -> Callable[[int], int] | None:
    """``kernel32.SetThreadExecutionState``, or ``None`` off Windows.

    Typed as ``DWORD -> DWORD`` so ``ES_CONTINUOUS`` (the top bit) is passed
    unsigned. It returns the previous state, or 0 on failure.
    """
    if sys.platform != "win32":
        return None
    import ctypes

    function = ctypes.windll.kernel32.SetThreadExecutionState
    function.argtypes = [ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    return cast(Callable[[int], int], function)


@dataclass(frozen=True, slots=True)
class KeepAwake:
    """The keep-awake hold. ``call`` is :func:`execution_state_call`'s answer.

    Thread-scoped: the hold and its release must come from the same thread,
    which in this single-threaded script they do.
    """

    call: Callable[[int], int] | None

    def hold(self) -> bool:
        return self._apply(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

    def release(self) -> bool:
        return self._apply(ES_CONTINUOUS)

    def _apply(self, flags: int) -> bool:
        if self.call is None:
            return False
        return self.call(flags) != 0


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

    @property
    def pid(self) -> int:
        return self.process.pid

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
    dates, codes, exception *type names* -- never a response body or an
    exception message."""
    clean = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in fields.items()
    }
    logger.log(level, message, extra={"fields": {"event": event, **clean}})


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = _JsonLines()
    # Appended: one file is the history of every weekday.
    to_file = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    to_file.setFormatter(formatter)
    to_stderr = logging.StreamHandler(sys.stderr)
    to_stderr.setFormatter(formatter)
    logger.handlers[:] = [to_file, to_stderr]
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main() -> int:
    configure_logging()
    awake = KeepAwake(call=execution_state_call())
    with httpx.Client() as client:
        effects = Effects(
            health=lambda: http_health(client),
            env_file_present=ENV_FILE.exists,
            launch=UvicornServer.launch,
            hold_awake=awake.hold,
            release_awake=awake.release,
            clock=utc_now,
            sleep=wall.sleep,
        )
        try:
            outcome = run_weekday_session(effects)
        except Exception as exc:
            _log("crashed", "the weekday session runner failed", error_type=type(exc).__name__,
                 at=utc_now(), level=logging.ERROR)
            return int(Exit.SERVER_FAILED)
    _log("exit", f"exiting {outcome.name}", exit_code=int(outcome), at=utc_now())
    return int(outcome)


if __name__ == "__main__":
    sys.exit(main())
