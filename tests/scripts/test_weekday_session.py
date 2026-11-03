"""``scripts/weekday_session.py``'s decisions and its flow, with no real server.

What is pinned: which days launch (the market calendar's sessions, holidays
and half-days included) and that a launch after the close starts nothing; the
run window ending at the calendar's close plus 30 minutes; the
already-running branch; a missing ``.env`` failing fast without being read;
the keep-awake flag set before the launch and cleared after the stop -- on a
clean finish, a server crash, a health loss and an exception alike; a server
that dies mid-session exiting 5 without a relaunch; a server that stops
answering health exiting 5; and that the source never reads ``.env``, never
POSTs, and GETs nothing but ``/api/health``.

No real server, no network and no real ``SetThreadExecutionState``: every
effect is injected, as ``test_weekly_audit.py`` does.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "weekday_session.py"
UTC = timezone.utc

#: Monday 23 Nov 2026 (EST). The 09:00 ET launch is 14:00 UTC; the calendar's
#: close is 16:00 ET = 21:00 UTC, so the window ends 21:30 UTC.
MON_0900 = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)
MON_CLOSE = datetime(2026, 11, 23, 21, 0, tzinfo=UTC)
MON_END = MON_CLOSE + timedelta(minutes=30)


@pytest.fixture()
def ws(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("weekday_session", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "weekday_session", module)
    spec.loader.exec_module(module)
    yield module


# --------------------------------------------------------------- the decision


@pytest.mark.parametrize(
    ("now", "close"),
    [
        # The 09:00 ET launch, before the open: the window is the whole day.
        (MON_0900, MON_CLOSE),
        # A logon in the middle of the session.
        (datetime(2026, 11, 23, 18, 0, tzinfo=UTC), MON_CLOSE),
        # The half-day after Thanksgiving closes at 13:00 ET -- the calendar's.
        (datetime(2026, 11, 27, 14, 0, tzinfo=UTC), datetime(2026, 11, 27, 18, 0, tzinfo=UTC)),
        # July is EDT: the close is 20:00 UTC, not 21:00.
        (datetime(2026, 7, 6, 13, 0, tzinfo=UTC), datetime(2026, 7, 6, 20, 0, tzinfo=UTC)),
        # A moment before the close still launches.
        (MON_CLOSE - timedelta(seconds=1), MON_CLOSE),
    ],
    ids=["monday_0900", "mid_session", "half_day", "edt", "just_before_close"],
)
def test_a_trading_day_before_its_close_runs_until_close_plus_30_minutes(
    ws: ModuleType, now: datetime, close: datetime
) -> None:
    decision = ws.decide(now)
    assert decision.kind is ws.Decision.RUN
    assert decision.session_close == close
    assert decision.window_end == close + timedelta(minutes=30)
    assert ws.POST_CLOSE == timedelta(minutes=30)


@pytest.mark.parametrize(
    ("now", "kind"),
    [
        # Saturday and Sunday.
        (datetime(2026, 11, 21, 14, 0, tzinfo=UTC), "NO_SESSION"),
        (datetime(2026, 11, 22, 14, 0, tzinfo=UTC), "NO_SESSION"),
        # Thanksgiving: a weekday market holiday.
        (datetime(2026, 11, 26, 14, 0, tzinfo=UTC), "NO_SESSION"),
        # Friday 3 July 2026, Independence Day observed.
        (datetime(2026, 7, 3, 13, 0, tzinfo=UTC), "NO_SESSION"),
        # At the close exactly the session is over.
        (MON_CLOSE, "AFTER_CLOSE"),
        # Inside close + 30 but after the close: nothing launches.
        (MON_CLOSE + timedelta(minutes=15), "AFTER_CLOSE"),
        # The half-day, after its 13:00 ET close.
        (datetime(2026, 11, 27, 18, 0, tzinfo=UTC), "AFTER_CLOSE"),
        # Tuesday 03:00 UTC is still Monday 22:00 in New York: Monday's
        # session, closed -- not Tuesday's, still ahead.
        (datetime(2026, 11, 24, 3, 0, tzinfo=UTC), "AFTER_CLOSE"),
    ],
    ids=["saturday", "sunday", "thanksgiving", "july_3_observed", "at_close",
         "inside_post_close", "half_day_after_close", "utc_tomorrow_et_tonight"],
)
def test_no_session_or_after_the_close_starts_nothing(
    ws: ModuleType, now: datetime, kind: str
) -> None:
    decision = ws.decide(now)
    assert decision.kind is ws.Decision[kind]
    assert decision.window_end is None


def test_a_weekday_past_the_calendars_published_range_is_named(
    ws: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the calendar's range every day reads as no session, so the runner
    would stop collecting with every log line saying "holiday". It says so."""

    def ended(_: datetime) -> datetime:
        raise ValueError("past the published schedule")

    monkeypatch.setattr(ws, "nyse_session_close", lambda _: None)
    monkeypatch.setattr(ws, "nyse_next_open", ended)
    assert ws.decide(MON_0900).kind is ws.Decision.CALENDAR_EXHAUSTED


def test_the_decision_refuses_a_naive_instant(ws: ModuleType) -> None:
    with pytest.raises(ValueError):
        ws.decide(datetime(2026, 11, 23, 9, 0))


# --------------------------------------------------------------- health watch


def test_one_failed_health_check_after_a_long_gap_is_not_a_loss(ws: ModuleType) -> None:
    """A laptop that slept with the lid shut wakes with the clock hours on.
    The streak counts from the first *failed* check, so the gap is not read
    as minutes of silence."""
    watch = ws.HealthWatch()
    assert watch.observe(True, MON_0900) is False
    assert watch.observe(False, MON_0900 + timedelta(hours=3)) is False


def test_failures_spanning_the_bound_are_a_loss_and_a_success_resets(ws: ModuleType) -> None:
    bound = ws.HEALTH_LOSS_AFTER
    watch = ws.HealthWatch()
    first = MON_0900 + timedelta(minutes=1)
    assert watch.observe(False, first) is False
    assert watch.observe(False, first + bound / 2) is False
    # Recovery resets the streak.
    assert watch.observe(True, first + bound - timedelta(seconds=1)) is False
    assert watch.observe(False, first + bound) is False
    assert watch.observe(False, first + bound * 1.5) is False
    # The bound, counted from the new streak's first failure, with enough checks.
    assert watch.observe(False, first + bound * 2) is True


def test_a_loss_needs_more_than_two_failed_checks(ws: ModuleType) -> None:
    watch = ws.HealthWatch()
    assert watch.observe(False, MON_0900) is False
    assert watch.observe(False, MON_0900 + timedelta(hours=1)) is False
    assert watch.observe(False, MON_0900 + timedelta(hours=1, seconds=1)) is True
    assert ws.HEALTH_LOSS_MIN_FAILURES == 3


# ------------------------------------------------------------------ the flow


@dataclass
class FakeServer:
    trace: list[str]
    clock: Callable[[], datetime]
    #: The process ends at this instant, with ``exit_code``.
    dies_at: datetime | None = None
    exit_code: int = 1
    stops: int = 0
    pid: int = 4242

    def exited(self) -> int | None:
        if self.dies_at is not None and self.clock() >= self.dies_at:
            return self.exit_code
        return None

    def stop(self) -> str:
        self.stops += 1
        self.trace.append("stop")
        return "already_exited" if self.exited() is not None else "graceful"


@dataclass
class World:
    """Fakes for every effect. ``trace`` records the order of the side effects."""

    now: datetime = MON_0900
    up_before_launch: bool = False
    #: Health once launched, as a function of the instant.
    healthy_at: Callable[[datetime], bool] = lambda _: True
    env_file: bool = True
    dies_at: datetime | None = None
    launch_raises: BaseException | None = None
    sleep_raises_at: datetime | None = None
    awake_applied: bool = True
    trace: list[str] = field(default_factory=list)
    launches: int = 0
    health_calls: int = 0
    server: FakeServer | None = None

    def health(self) -> bool:
        self.health_calls += 1
        if self.launches == 0:
            return self.up_before_launch
        return self.healthy_at(self.now)

    def env_file_present(self) -> bool:
        return self.env_file

    def launch(self) -> FakeServer:
        # A relaunch fails the test here rather than looping on a server
        # that keeps dying: the runner must never launch twice.
        assert self.launches == 0, "the runner relaunched its server"
        self.launches += 1
        self.trace.append("launch")
        if self.launch_raises is not None:
            raise self.launch_raises
        self.server = FakeServer(trace=self.trace, clock=self.clock, dies_at=self.dies_at)
        return self.server

    def hold_awake(self) -> bool:
        self.trace.append("hold")
        return self.awake_applied

    def release_awake(self) -> bool:
        self.trace.append("release")
        return self.awake_applied

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.now += timedelta(seconds=seconds)
        if self.sleep_raises_at is not None and self.now >= self.sleep_raises_at:
            raise KeyboardInterrupt

    def effects(self, ws: ModuleType) -> Any:
        return ws.Effects(
            health=self.health,
            env_file_present=self.env_file_present,
            launch=self.launch,
            hold_awake=self.hold_awake,
            release_awake=self.release_awake,
            clock=self.clock,
            sleep=self.sleep,
        )


def _events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.fields["event"] for r in caplog.records if hasattr(r, "fields")]


def _record(caplog: pytest.LogCaptureFixture, event: str) -> logging.LogRecord:
    found = [r for r in caplog.records if getattr(r, "fields", {}).get("event") == event]
    assert len(found) == 1, f"{event}: {_events(caplog)}"
    return found[0]


def test_the_exit_codes(ws: ModuleType) -> None:
    assert int(ws.Exit.OK) == 0
    assert int(ws.Exit.SERVER_FAILED) == 3
    assert int(ws.Exit.SERVER_DIED) == 5
    assert {int(code) for code in ws.Exit} == {0, 3, 5}


@pytest.mark.parametrize(
    ("now", "event"),
    [
        (datetime(2026, 11, 21, 14, 0, tzinfo=UTC), "not_a_trading_day"),
        (datetime(2026, 11, 26, 14, 0, tzinfo=UTC), "not_a_trading_day"),
        (MON_CLOSE, "after_close"),
        (datetime(2026, 11, 27, 18, 30, tzinfo=UTC), "after_close"),
    ],
    ids=["saturday", "thanksgiving", "at_close", "half_day_after_close"],
)
def test_off_session_it_exits_0_and_touches_nothing(
    ws: ModuleType, caplog: pytest.LogCaptureFixture, now: datetime, event: str
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    world = World(now=now)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.launches == 0 and world.health_calls == 0 and world.trace == []
    assert _events(caplog) == [event]


def test_an_exhausted_calendar_exits_0_but_logs_an_error(
    ws: ModuleType, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ended(_: datetime) -> datetime:
        raise ValueError("past the published schedule")

    monkeypatch.setattr(ws, "nyse_session_close", lambda _: None)
    monkeypatch.setattr(ws, "nyse_next_open", ended)
    caplog.set_level(logging.INFO, logger="weekday_session")
    world = World()
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.trace == []
    assert _events(caplog) == ["calendar_exhausted"]
    assert _record(caplog, "calendar_exhausted").levelno == logging.ERROR


def test_a_corollary_already_running_starts_nothing_and_exits_0(
    ws: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    world = World(up_before_launch=True)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.launches == 0 and world.trace == []
    assert _events(caplog) == ["already_running"]


def test_a_missing_env_file_exits_3_without_launching_or_holding_the_machine_awake(
    ws: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    world = World(env_file=False)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.SERVER_FAILED
    assert world.launches == 0 and world.trace == [] and world.now == MON_0900
    assert _events(caplog) == ["env_file_missing"]


def test_it_runs_until_close_plus_30_minutes_then_stops_and_releases(
    ws: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    # Off the poll grid, so a final sleep that overshot the window would show.
    world = World(now=MON_0900 + timedelta(seconds=7))
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.now == MON_END
    assert world.launches == 1
    assert world.trace == ["hold", "launch", "stop", "release"]
    assert _events(caplog) == [
        "keep_awake_set", "launching", "healthy", "window_closed", "stopped",
        "keep_awake_cleared",
    ]
    launching = _record(caplog, "launching").fields  # type: ignore[attr-defined]
    assert launching["window_end"] == MON_END.isoformat()
    assert launching["session_close"] == MON_CLOSE.isoformat()
    assert launching["trading_day"] == "2026-11-23"
    assert _record(caplog, "keep_awake_set").fields["applied"] is True  # type: ignore[attr-defined]


def test_the_half_day_window_ends_at_its_own_close_plus_30(ws: ModuleType) -> None:
    world = World(now=datetime(2026, 11, 27, 14, 0, tzinfo=UTC))
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.now == datetime(2026, 11, 27, 18, 30, tzinfo=UTC)


def test_off_windows_the_keep_awake_is_logged_as_not_applied(
    ws: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    world = World(awake_applied=False)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert _record(caplog, "keep_awake_set").fields["applied"] is False  # type: ignore[attr-defined]
    assert _record(caplog, "keep_awake_cleared").fields["applied"] is False  # type: ignore[attr-defined]


def test_a_server_that_dies_mid_session_exits_5_without_a_relaunch(
    ws: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    dies = MON_0900 + timedelta(hours=2)
    world = World(dies_at=dies)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.SERVER_DIED
    assert world.launches == 1
    assert world.trace == ["hold", "launch", "stop", "release"]
    assert dies <= world.now < dies + ws.SESSION_POLL + timedelta(seconds=1)
    died = _record(caplog, "server_exited")
    assert died.levelno == logging.ERROR
    assert died.fields["exit_code"] == 1  # type: ignore[attr-defined]
    assert _events(caplog)[-2:] == ["stopped", "keep_awake_cleared"]


def test_a_server_that_stops_answering_health_exits_5(
    ws: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    silent_from = MON_0900 + timedelta(hours=1)
    world = World(healthy_at=lambda at: at < silent_from)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.SERVER_DIED
    assert world.launches == 1
    assert world.trace == ["hold", "launch", "stop", "release"]
    # Not before the bound, and within a poll of it.
    assert world.now >= silent_from + ws.HEALTH_LOSS_AFTER
    assert world.now <= silent_from + ws.HEALTH_LOSS_AFTER + ws.SESSION_POLL
    assert _record(caplog, "health_lost").levelno == logging.ERROR


def test_a_brief_health_blip_does_not_end_the_session(ws: ModuleType) -> None:
    blip = MON_0900 + timedelta(hours=1)
    world = World(healthy_at=lambda at: not (blip <= at < blip + timedelta(minutes=1)))
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.now == MON_END


def test_a_server_that_dies_before_health_exits_3_and_still_releases(ws: ModuleType) -> None:
    world = World(healthy_at=lambda _: False, dies_at=MON_0900)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.SERVER_FAILED
    assert world.launches == 1
    assert world.trace == ["hold", "launch", "stop", "release"]


def test_a_server_that_never_answers_health_is_bounded(ws: ModuleType) -> None:
    world = World(healthy_at=lambda _: False)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.SERVER_FAILED
    assert MON_0900 + ws.HEALTH_TIMEOUT <= world.now
    assert world.now < MON_0900 + ws.HEALTH_TIMEOUT + timedelta(minutes=1)
    assert world.trace == ["hold", "launch", "stop", "release"]


def test_a_launch_that_raises_still_releases_the_keep_awake(ws: ModuleType) -> None:
    world = World(launch_raises=OSError("SECRET path"))
    with pytest.raises(OSError):
        ws.run_weekday_session(world.effects(ws))
    assert world.trace == ["hold", "launch", "release"]


def test_an_interrupt_mid_session_stops_the_server_and_releases(ws: ModuleType) -> None:
    world = World(sleep_raises_at=MON_0900 + timedelta(hours=1))
    with pytest.raises(KeyboardInterrupt):
        ws.run_weekday_session(world.effects(ws))
    assert world.trace == ["hold", "launch", "stop", "release"]


# ------------------------------------------------------------ keep-awake


def test_keep_awake_passes_the_documented_flags(ws: ModuleType) -> None:
    calls: list[int] = []

    def fake(flags: int) -> int:
        calls.append(flags)
        return 0x80000000  # the previous state: any non-zero is success

    awake = ws.KeepAwake(call=fake)
    assert awake.hold() is True
    assert awake.release() is True
    assert calls == [0x80000000 | 0x00000001, 0x80000000]
    assert ws.ES_CONTINUOUS == 0x80000000 and ws.ES_SYSTEM_REQUIRED == 0x00000001


def test_keep_awake_reports_a_failed_call(ws: ModuleType) -> None:
    assert ws.KeepAwake(call=lambda _: 0).hold() is False


def test_keep_awake_off_windows_does_nothing(
    ws: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws.sys, "platform", "linux")
    assert ws.execution_state_call() is None
    awake = ws.KeepAwake(call=None)
    assert awake.hold() is False and awake.release() is False


# ------------------------------------------------------------ the real effects


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_source_never_reads_the_env_file(ws: ModuleType) -> None:
    """The keys are the launcher's to load (rule 6): ``--env-file`` is handed to
    uvicorn, and nothing here opens or parses it. ``exists()`` is a stat."""
    source = _source()
    assert '"--env-file", str(ENV_FILE)' in source
    assert "dotenv" not in source and "os.environ" not in source and "getenv" not in source
    assert "ENV_FILE.open" not in source and "ENV_FILE.read" not in source
    assert "env_file_present=ENV_FILE.exists" in source
    assert '"corollary.api:headless_app"' in source
    assert "sys.executable" in source


def test_the_source_never_posts_and_gets_only_health(ws: ModuleType) -> None:
    """Rule 9: an auto-start must not become an auto-resume. The only request
    this script can make is ``GET /api/health``."""
    code = "\n".join(
        line for line in _source().splitlines() if not line.lstrip().startswith("#")
    )
    for verb in (".post(", ".put(", ".patch(", ".delete(", ".request(", ".stream(",
                 ".send(", '"POST"', "'POST'"):
        assert verb not in code, verb
    assert "/api/engine" not in code
    paths = set(re.findall(r'"(/api/[^"]*)"', code))
    assert paths == {"/api/health"}
    assert code.count(".get(") == 1  # the one in _get


def test_the_servers_output_is_never_written_to_disk(
    ws: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class FakePopen:
        pid = 1

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            seen.update(kwargs, command=command)

    monkeypatch.setattr(ws.subprocess, "Popen", FakePopen)
    server = ws.UvicornServer.launch()
    assert server.process is not None
    assert seen["stdout"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["command"][1:4] == ["-m", "uvicorn", "corollary.api:headless_app"]
    assert "--reload" not in seen["command"]
    if sys.platform == "win32":
        assert seen["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP


def test_the_scripts_own_log_appends_to_its_own_file(
    ws: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert ws.LOG_FILE.name == "weekday-session.log"
    log_file = tmp_path / "weekday-session.log"
    log_file.write_text('{"event": "yesterday"}\n', encoding="utf-8")
    monkeypatch.setattr(ws, "LOG_DIR", tmp_path)
    monkeypatch.setattr(ws, "LOG_FILE", log_file)
    ws.configure_logging()
    try:
        ws._log("today", "a line", at=MON_0900)
    finally:
        for handler in ws.logger.handlers:
            handler.close()
        ws.logger.handlers[:] = []
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"event": "yesterday"}
    last = json.loads(lines[-1])
    assert last["event"] == "today" and last["at"] == MON_0900.isoformat()
    assert set(last) == {"logged_at", "level", "message", "event", "at"}
