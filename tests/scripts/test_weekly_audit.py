"""``scripts/weekly_audit.py``'s pure parts and its flow, with no real server.

What is pinned: the slot the wait is for; the deadline (the latest of now,
the slot and today's close); that a trading day before its close launches
nothing, leaving port 8000 to the owner's ``:app``; whether the status
satisfies the wait, and when it shows the slot passed with no run (exit 4);
the already-running branch; a missing ``.env`` failing fast without being
read; the server's output going to ``DEVNULL`` (rule 6 -- its tracebacks are
unscrubbed); the script's own log appending; and that the log carries
exception *types*, never messages or bodies.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "weekly_audit.py"
UTC = timezone.utc

#: Saturday 21 Nov 2026 (EST): 09:45 ET is 14:45 UTC, the slot 15:00 UTC.
SAT_0945 = datetime(2026, 11, 21, 14, 45, tzinfo=UTC)
SLOT = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)
NEXT_SLOT = SLOT + timedelta(days=7)
LAST_WEEK = "2026-11-14T15:00:00Z"


@pytest.fixture()
def wa(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("weekly_audit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "weekly_audit", module)
    spec.loader.exec_module(module)
    yield module


# ------------------------------------------------------------------- the slot


@pytest.mark.parametrize(
    ("now", "slot"),
    [
        # The 09:45 launch waits for today's 10:00, not last week's.
        (SAT_0945, SLOT),
        (SLOT, SLOT),
        (SLOT + timedelta(hours=3), SLOT),
        # A launch missed and run late waits for the Saturday just gone.
        (datetime(2026, 11, 22, 12, 0, tzinfo=UTC), SLOT),
        (datetime(2026, 11, 27, 16, 0, tzinfo=UTC), SLOT),
        # Friday belongs to the week before's Saturday.
        (datetime(2026, 11, 20, 23, 0, tzinfo=UTC), SLOT - timedelta(days=7)),
        # Saturday 00:30 UTC is still Friday evening in New York.
        (datetime(2026, 11, 21, 0, 30, tzinfo=UTC), SLOT - timedelta(days=7)),
        # July is EDT: 10:00 ET is 14:00 UTC.
        (datetime(2026, 7, 4, 13, 45, tzinfo=UTC), datetime(2026, 7, 4, 14, 0, tzinfo=UTC)),
    ],
)
def test_the_slot_is_this_weeks_saturday_1000_eastern(
    wa: ModuleType, now: datetime, slot: datetime
) -> None:
    assert wa.audit_slot_for(now) == slot


def test_the_slot_refuses_a_naive_instant(wa: ModuleType) -> None:
    with pytest.raises(ValueError):
        wa.audit_slot_for(datetime(2026, 11, 21, 9, 45))


@pytest.mark.parametrize(
    ("now", "deadline"),
    [
        # The 09:45 launch: counted from the 10:00 slot, not from 09:45.
        (SAT_0945, SLOT + timedelta(hours=2)),
        # A launch before 08:00 ET would otherwise give up before the slot.
        (datetime(2026, 11, 21, 12, 0, tzinfo=UTC), SLOT + timedelta(hours=2)),
        # Later the same Saturday: from now.
        (SLOT + timedelta(hours=1), SLOT + timedelta(hours=3)),
        # In a Monday session, the catch-up defers to the 16:00 ET close.
        (
            datetime(2026, 11, 23, 16, 0, tzinfo=UTC),
            datetime(2026, 11, 23, 21, 0, tzinfo=UTC) + timedelta(hours=2),
        ),
        # The half-day after Thanksgiving closes at 13:00 ET -- the calendar's.
        (
            datetime(2026, 11, 27, 16, 0, tzinfo=UTC),
            datetime(2026, 11, 27, 18, 0, tzinfo=UTC) + timedelta(hours=2),
        ),
        # After a close, two hours from now.
        (
            datetime(2026, 11, 23, 22, 0, tzinfo=UTC),
            datetime(2026, 11, 23, 22, 0, tzinfo=UTC) + timedelta(hours=2),
        ),
    ],
    ids=["saturday_0945", "saturday_early", "saturday_late", "in_session", "half_day",
         "after_close"],
)
def test_the_deadline_counts_from_the_latest_of_now_the_slot_and_the_close(
    wa: ModuleType, now: datetime, deadline: datetime
) -> None:
    assert wa.wait_deadline(now) == deadline


@pytest.mark.parametrize(
    ("now", "close"),
    [
        # Monday 08:00 ET: before the open is still before the close.
        (datetime(2026, 11, 23, 13, 0, tzinfo=UTC), datetime(2026, 11, 23, 21, 0, tzinfo=UTC)),
        # A moment before the half-day's 13:00 ET close.
        (
            datetime(2026, 11, 27, 17, 59, 59, tzinfo=UTC),
            datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
        ),
        # At the close the session is over.
        (datetime(2026, 11, 27, 18, 0, tzinfo=UTC), None),
        (datetime(2026, 11, 23, 22, 0, tzinfo=UTC), None),
        # Weekend, and Thanksgiving (a market holiday on a Thursday).
        (SAT_0945, None),
        (datetime(2026, 11, 26, 15, 0, tzinfo=UTC), None),
    ],
    ids=["monday_0800", "half_day_before_close", "half_day_at_close", "monday_after_close",
         "saturday", "thanksgiving"],
)
def test_a_session_still_ahead_today_is_read_from_the_calendar(
    wa: ModuleType, now: datetime, close: datetime | None
) -> None:
    assert wa.session_close_ahead(now) == close


# ------------------------------------------------------------- the status


def _body(next_run_at: str | None = None, **run: Any) -> dict[str, Any]:
    base = {
        "id": 7,
        "trigger": "scheduled",
        "status": "succeeded",
        "startedAt": "2026-11-21T15:00:00.000001Z",
        "finishedAt": "2026-11-21T15:04:00Z",
    }
    base.update(run)
    return {"sources": [], "lastRun": base, "accuracyRun": None, "nextRunAt": next_run_at}


def test_last_run_parses_the_route_shape(wa: ModuleType) -> None:
    run = wa.parse_last_run(_body())
    assert run == wa.LastRun(
        id=7,
        trigger="scheduled",
        status="succeeded",
        started_at=datetime(2026, 11, 21, 15, 0, 0, 1, tzinfo=UTC),
        finished_at=datetime(2026, 11, 21, 15, 4, tzinfo=UTC),
    )
    assert wa.parse_last_run({"sources": [], "lastRun": None}) is None
    assert wa.parse_last_run(_body(status="running", finishedAt=None)).finished_at is None


def test_the_status_parses_next_run_at(wa: ModuleType) -> None:
    status = wa.parse_status(_body(next_run_at="2026-11-28T15:00:00Z"))
    assert status.next_run_at == NEXT_SLOT
    assert status.last_run == wa.parse_last_run(_body())
    assert wa.parse_status({"sources": [], "lastRun": None, "nextRunAt": None}) == wa.AuditStatus(
        last_run=None, next_run_at=None
    )


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"lastRun": "SECRET-ish text"},
        _body(id="7"),
        _body(startedAt="SECRET-ish text"),
        _body(startedAt="2026-11-21T15:00:00"),  # naive
        _body(next_run_at="SECRET-ish text"),
        _body(next_run_at="2026-11-28T15:00:00"),  # naive
    ],
)
def test_an_unrecognised_shape_raises_without_echoing_the_value(
    wa: ModuleType, body: object
) -> None:
    with pytest.raises(ValueError) as caught:
        wa.parse_status(body)
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize(
    ("run", "state"),
    [
        (None, "WAITING"),
        # Last week's finished run is not this week's.
        (dict(startedAt="2026-11-21T14:59:59.999999Z"), "WAITING"),
        (dict(status="running", finishedAt=None), "WAITING"),
        (dict(startedAt="2026-11-21T15:00:00Z"), "SUCCEEDED"),
        (dict(status="failed"), "FAILED"),
        # A manual run since the slot graded the same labels.
        (dict(trigger="manual"), "SUCCEEDED"),
    ],
    ids=["no_run", "before_the_slot", "running", "at_the_slot", "failed", "manual"],
)
def test_whether_last_run_satisfies_the_wait(
    wa: ModuleType, run: dict[str, Any] | None, state: str
) -> None:
    parsed = None if run is None else wa.parse_last_run(_body(**run))
    assert wa.run_state(parsed, SLOT) is wa.RunState[state]


@pytest.mark.parametrize(
    ("body", "skipped"),
    [
        # The scheduler moved past the slot and nothing ran for it.
        (_body("2026-11-28T15:00:00Z", startedAt=LAST_WEEK), True),
        ({"sources": [], "lastRun": None, "nextRunAt": "2026-11-28T15:00:00Z"}, True),
        # Still due: the slot itself is next.
        (_body("2026-11-21T15:00:00Z", startedAt=LAST_WEEK), False),
        # A catch-up in progress: the loop has not asked for a next run yet.
        (_body(None, startedAt=LAST_WEEK), False),
        # Moved past the slot *because* the slot's run happened.
        (_body("2026-11-28T15:00:00Z"), False),
        (_body("2026-11-28T15:00:00Z", status="running", finishedAt=None), False),
    ],
    ids=["moved_past_no_run", "moved_past_never_ran", "slot_next", "catch_up_pending",
         "ran", "running"],
)
def test_whether_the_status_shows_the_slot_passed_with_no_run(
    wa: ModuleType, body: dict[str, Any], skipped: bool
) -> None:
    assert wa.slot_passed_without_run(wa.parse_status(body), SLOT) is skipped


# ------------------------------------------------------------------ the flow


@dataclass
class FakeServer:
    exit_code: int | None = None
    stops: int = 0

    def exited(self) -> int | None:
        return self.exit_code

    def stop(self) -> str:
        self.stops += 1
        return "graceful"


@dataclass
class World:
    """Fakes for every effect. ``health_after_launch`` and ``statuses`` script answers."""

    now: datetime = SAT_0945
    up_before_launch: bool = False
    health_after_launch: bool = True
    env_file: bool = True
    statuses: list[object] = field(default_factory=list)
    server: FakeServer = field(default_factory=FakeServer)
    launches: int = 0
    reads: int = 0

    def health(self) -> bool:
        return self.up_before_launch if self.launches == 0 else self.health_after_launch

    def read_status(self) -> object:
        self.reads += 1
        if not self.statuses:
            return None
        answer = self.statuses[min(self.reads, len(self.statuses)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def env_file_present(self) -> bool:
        return self.env_file

    def launch(self) -> FakeServer:
        self.launches += 1
        return self.server

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def effects(self, wa: ModuleType) -> Any:
        return wa.Effects(
            health=self.health,
            read_status=self.read_status,
            env_file_present=self.env_file_present,
            launch=self.launch,
            clock=self.clock,
            sleep=self.sleep,
        )


def _status(wa: ModuleType, next_run_at: str | None = None, **run: Any) -> Any:
    return wa.parse_status(_body(next_run_at, **run))


def _no_run(wa: ModuleType, next_run_at: str | None = None) -> Any:
    return wa.AuditStatus(
        last_run=None, next_run_at=None if next_run_at is None else datetime.fromisoformat(
            next_run_at
        )
    )


def _events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.fields["event"] for r in caplog.records if hasattr(r, "fields")]


def test_a_corollary_already_running_starts_nothing_and_exits_0(
    wa: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekly_audit")
    world = World(up_before_launch=True)
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.OK
    assert world.launches == 0 and world.reads == 0
    assert _events(caplog) == ["already_running"]


@pytest.mark.parametrize(
    "now",
    [
        # A missed Saturday launch run by Task Scheduler at Monday 08:00 ET.
        datetime(2026, 11, 23, 13, 0, tzinfo=UTC),
        # In session.
        datetime(2026, 11, 23, 16, 0, tzinfo=UTC),
        # A moment before the half-day's 13:00 ET close.
        datetime(2026, 11, 27, 17, 59, 59, tzinfo=UTC),
    ],
    ids=["monday_0800", "in_session", "half_day_before_close"],
)
def test_on_a_trading_day_before_its_close_it_defers_to_the_app_and_launches_nothing(
    wa: ModuleType, caplog: pytest.LogCaptureFixture, now: datetime
) -> None:
    """Launched, ``headless_app`` would hold 127.0.0.1:8000 into the session, and
    the owner's ``:app`` at 09:25 would fail to bind or the browser would reach a
    no-sockets app. ``:app`` carries the same catch-up, so it adds nothing."""
    caplog.set_level(logging.INFO, logger="weekly_audit")
    world = World(now=now)
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.OK
    assert world.launches == 0 and world.reads == 0
    assert _events(caplog) == ["deferred_to_app"]


@pytest.mark.parametrize(
    "now",
    [
        SAT_0945,
        # Thanksgiving: a weekday with no session.
        datetime(2026, 11, 26, 15, 0, tzinfo=UTC),
        # The half-day at its close exactly: the session is over.
        datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
        datetime(2026, 11, 23, 22, 0, tzinfo=UTC),
    ],
    ids=["saturday", "thanksgiving", "half_day_at_close", "monday_after_close"],
)
def test_off_session_it_launches(wa: ModuleType, now: datetime) -> None:
    slot = wa.audit_slot_for(now)
    done = wa.AuditStatus(
        last_run=wa.LastRun(id=1, trigger="scheduled", status="succeeded",
                            started_at=slot, finished_at=slot + timedelta(minutes=4)),
        next_run_at=None,
    )
    world = World(now=now, statuses=[done])
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.OK
    assert world.launches == 1 and world.server.stops == 1


def test_a_missing_env_file_fails_fast_without_launching(
    wa: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekly_audit")
    world = World(env_file=False)
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.SERVER_FAILED
    assert world.launches == 0 and world.now == SAT_0945
    assert _events(caplog) == ["env_file_missing"]


def test_it_waits_for_the_slots_run_then_stops_the_server(
    wa: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekly_audit")
    last_week = _status(wa, "2026-11-21T15:00:00Z", startedAt=LAST_WEEK)
    running = _status(wa, "2026-11-28T15:00:00Z", status="running", finishedAt=None)
    done = _status(wa, "2026-11-28T15:00:00Z")
    world = World(statuses=[last_week, running, done])
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.OK
    assert world.reads == 3 and world.server.stops == 1
    assert _events(caplog) == ["launching", "healthy", "run_finished", "stopped"]


def test_a_failed_run_exits_non_zero(wa: ModuleType) -> None:
    world = World(statuses=[_status(wa, status="failed")])
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.RUN_FAILED
    assert world.server.stops == 1


def test_no_run_by_the_deadline_times_out_and_still_stops_the_server(wa: ModuleType) -> None:
    world = World(statuses=[_no_run(wa)])
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.TIMED_OUT
    assert world.now >= SLOT + wa.RUN_DEADLINE
    assert world.server.stops == 1


def test_a_slot_passed_with_no_run_exits_skipped_without_waiting_out_the_deadline(
    wa: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    """A skip writes no ``audit_run`` row: without this the script waited two
    hours and then reported a timeout for what was a skip."""
    caplog.set_level(logging.INFO, logger="weekly_audit")
    due = _status(wa, "2026-11-21T15:00:00Z", startedAt=LAST_WEEK)
    moved_on = _status(wa, "2026-11-28T15:00:00Z", startedAt=LAST_WEEK)
    world = World(statuses=[due, moved_on, moved_on])
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.SKIPPED
    assert int(wa.Exit.SKIPPED) == 4
    assert world.reads == 3 and world.server.stops == 1
    assert world.now < SLOT + wa.RUN_DEADLINE
    assert _events(caplog) == ["launching", "healthy", "run_skipped", "stopped"]
    skipped = [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "run_skipped"]
    assert skipped[0].levelno == logging.ERROR
    assert skipped[0].fields["next_run_at"] == NEXT_SLOT.isoformat()  # type: ignore[attr-defined]


def test_one_skip_shaped_read_is_confirmed_before_exiting(wa: ModuleType) -> None:
    """``lastRun`` is read before ``nextRunAt`` in one request, so a run that
    opened and finished between the two looks like a skip once. A second read,
    made after ``nextRunAt`` had moved, sees the run."""
    looks_skipped = _status(wa, "2026-11-28T15:00:00Z", startedAt=LAST_WEEK)
    done = _status(wa, "2026-11-28T15:00:00Z")
    world = World(statuses=[looks_skipped, done])
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.OK
    assert world.reads == 2


def test_a_server_that_dies_before_health_fails_fast(wa: ModuleType) -> None:
    world = World(health_after_launch=False, server=FakeServer(exit_code=1))
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.SERVER_FAILED
    assert world.reads == 0 and world.server.stops == 1


def test_a_server_that_never_answers_health_is_bounded(wa: ModuleType) -> None:
    world = World(health_after_launch=False)
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.SERVER_FAILED
    assert world.now >= SAT_0945 + wa.HEALTH_TIMEOUT
    assert world.now < SAT_0945 + wa.HEALTH_TIMEOUT + timedelta(minutes=1)


def test_a_failed_status_read_logs_its_type_and_never_its_message(
    wa: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekly_audit")
    world = World(statuses=[RuntimeError("SECRET body from somewhere"), _status(wa)])
    assert wa.run_weekly_audit(world.effects(wa)) is wa.Exit.OK
    formatter = wa._JsonLines()
    lines = [formatter.format(r) for r in caplog.records]
    assert any('"error_type": "RuntimeError"' in line for line in lines)
    assert not any("SECRET" in line for line in lines)


# ------------------------------------------------------------ the real effects


def test_the_launch_command_reads_no_env_file_itself(wa: ModuleType) -> None:
    """The keys are the launcher's to load (rule 6): ``--env-file`` is handed to
    uvicorn, and nothing in the script opens or parses it. ``exists()`` is a
    stat, not a read."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"--env-file", str(ENV_FILE)' in source
    assert "dotenv" not in source and "os.environ" not in source and "getenv" not in source
    assert "ENV_FILE.open" not in source and "ENV_FILE.read" not in source
    assert "env_file_present=ENV_FILE.exists" in source
    assert '"corollary.api:headless_app"' in source
    assert "sys.executable" in source


def test_the_servers_output_is_never_written_to_disk(
    wa: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 6. The server's stderr carries tracebacks ``logging.lastResort``
    renders unscrubbed -- chained causes included, and an httpx transport
    error can name a URL with ``api_key=`` in it. ``corollary.wire`` has no
    pattern scrubber that removes a short query-string secret, so the output
    goes to ``DEVNULL`` rather than to a file."""
    seen: dict[str, Any] = {}

    class FakePopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            seen.update(kwargs, command=command)

    monkeypatch.setattr(wa.subprocess, "Popen", FakePopen)
    server = wa.UvicornServer.launch()
    assert server.process is not None
    assert seen["stdout"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL
    assert seen["stdin"] is subprocess.DEVNULL
    source = SCRIPT.read_text(encoding="utf-8")
    assert "SERVER_LOG_FILE" not in source and "weekly-audit-server.log" not in source


def test_the_scripts_own_log_appends(
    wa: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_file = tmp_path / "weekly-audit.log"
    log_file.write_text('{"event": "last_week"}\n', encoding="utf-8")
    monkeypatch.setattr(wa, "LOG_DIR", tmp_path)
    monkeypatch.setattr(wa, "LOG_FILE", log_file)
    wa.configure_logging()
    try:
        wa._log("this_week", "a line", at=SAT_0945)
    finally:
        for handler in wa.logger.handlers:
            handler.close()
        wa.logger.handlers[:] = []
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"event": "last_week"}
    assert json.loads(lines[-1])["event"] == "this_week"
