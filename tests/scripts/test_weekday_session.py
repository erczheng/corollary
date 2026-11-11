"""``scripts/weekday_session.py``'s decisions and its flow, with no real server.

What is pinned: which days launch (the market calendar's sessions, holidays
and half-days included, Good Friday among them); the run window ending at the
**later** of the calendar's close plus 30 minutes and 18:05 ET -- owner
decision Q24d, so R8's ``econ_first_prints`` release-day retries, which run to
18:00 ET whatever the close, actually run -- on a normal day, on a half-day
and on both sides of each DST change; a start after the close launching only
with at least an hour of runway before that stop -- 17:05 ET exactly launches,
a second later does not, on a half-day too -- and one at or after the stop
starting nothing; the docstring naming the 08:30 trigger, the runway and the
offset-free ``StartBoundary``; the already-running branch; a missing ``.env``
failing fast without being read;
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
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "weekday_session.py"
UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")

#: Monday 23 Nov 2026 (EST). The 08:30 ET launch is 13:30 UTC; the calendar's
#: close is 16:00 ET = 21:00 UTC. Close + 30 is 21:30 UTC, and 18:05 ET --
#: 23:05 UTC -- is later, so the window ends there.
MON_0830 = datetime(2026, 11, 23, 13, 30, tzinfo=UTC)
MON_CLOSE = datetime(2026, 11, 23, 21, 0, tzinfo=UTC)
MON_END = datetime(2026, 11, 23, 23, 5, tzinfo=UTC)
#: The half-day after Thanksgiving: the calendar closes it at 13:00 ET =
#: 18:00 UTC. Close + 30 would be 18:30 UTC; the window still ends 18:05 ET.
HALF_DAY_CLOSE = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
HALF_DAY_END = datetime(2026, 11, 27, 23, 5, tzinfo=UTC)


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


def test_the_stop_constants(ws: ModuleType) -> None:
    assert ws.POST_CLOSE == timedelta(minutes=30)
    assert ws.EARLIEST_STOP_ET == time(18, 5)
    assert ws.MIN_RUNWAY == timedelta(minutes=60)


def test_the_1805_stop_covers_r8s_last_possible_retry(ws: ModuleType) -> None:
    """R8's release-day retries end at ``RELEASE_DAY_UNTIL`` (18:00 ET), the last
    possible retry for any release time. If that cutoff ever moves later, the
    runner would stop before the last attempt could run, silently; this fails
    instead."""
    from corollary.data.econ_first_prints import RELEASE_DAY_UNTIL

    day = date(2026, 11, 23)
    last_attempt = datetime.combine(day, RELEASE_DAY_UNTIL)
    assert datetime.combine(day, ws.EARLIEST_STOP_ET) >= last_attempt + timedelta(minutes=5)


@pytest.mark.parametrize(
    ("now", "close", "end"),
    [
        # The 08:30 ET launch, before the open: the window is the whole day.
        (MON_0830, MON_CLOSE, MON_END),
        # A logon in the middle of the session.
        (datetime(2026, 11, 23, 18, 0, tzinfo=UTC), MON_CLOSE, MON_END),
        # At the close exactly: R8's retries still have until 18:00 ET.
        (MON_CLOSE, MON_CLOSE, MON_END),
        # Inside what used to be the post-close half hour.
        (MON_CLOSE + timedelta(minutes=15), MON_CLOSE, MON_END),
        # 17:00 ET, past close + 30: still launches, and runs to 18:05.
        (datetime(2026, 11, 23, 22, 0, tzinfo=UTC), MON_CLOSE, MON_END),
        # The last launch: exactly the hour's runway before the stop.
        (MON_END - timedelta(minutes=60), MON_CLOSE, MON_END),
        # The half-day closes at 13:00 ET -- the calendar's -- and still stops
        # at 18:05 ET, not 13:30.
        (datetime(2026, 11, 27, 13, 30, tzinfo=UTC), HALF_DAY_CLOSE, HALF_DAY_END),
        # The half-day, at its close: launches.
        (HALF_DAY_CLOSE, HALF_DAY_CLOSE, HALF_DAY_END),
        # July is EDT: the close is 20:00 UTC and 18:05 ET is 22:05 UTC.
        (datetime(2026, 7, 6, 12, 30, tzinfo=UTC), datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
         datetime(2026, 7, 6, 22, 5, tzinfo=UTC)),
    ],
    ids=["monday_0830", "mid_session", "at_close", "inside_post_close", "start_1700",
         "last_launch", "half_day", "half_day_at_close", "edt"],
)
def test_a_trading_day_before_its_stop_runs_until_1805_et(
    ws: ModuleType, now: datetime, close: datetime, end: datetime
) -> None:
    decision = ws.decide(now)
    assert decision.kind is ws.Decision.RUN
    assert decision.session_close == close
    assert decision.window_end == end
    assert decision.window_end.astimezone(NEW_YORK).time() == time(18, 5)
    # 18:05 ET is the later of the two on every real session.
    assert decision.window_end > close + timedelta(minutes=30)


@pytest.mark.parametrize(
    ("day", "stop"),
    [
        # DST starts Sunday 8 Mar 2026. The Friday before is EST: 23:05 UTC.
        (date(2026, 3, 6), datetime(2026, 3, 6, 23, 5, tzinfo=UTC)),
        # The Monday after is EDT: 22:05 UTC -- the same wall time, an hour
        # earlier in UTC.
        (date(2026, 3, 9), datetime(2026, 3, 9, 22, 5, tzinfo=UTC)),
        # DST ends Sunday 1 Nov 2026. The Friday before is EDT: 22:05 UTC.
        (date(2026, 10, 30), datetime(2026, 10, 30, 22, 5, tzinfo=UTC)),
        # The Monday after is EST: 23:05 UTC.
        (date(2026, 11, 2), datetime(2026, 11, 2, 23, 5, tzinfo=UTC)),
    ],
    ids=["friday_before_spring_forward", "monday_after_spring_forward",
         "friday_before_fall_back", "monday_after_fall_back"],
)
def test_the_1805_stop_is_the_right_utc_instant_across_a_dst_change(
    ws: ModuleType, day: date, stop: datetime
) -> None:
    # 08:30 New York time on that day, whichever offset it carries.
    launch = datetime.combine(day, time(8, 30), tzinfo=NEW_YORK).astimezone(UTC)
    decision = ws.decide(launch)
    assert decision.kind is ws.Decision.RUN
    assert decision.window_end == stop
    # The plan's instants are UTC, not New York wall times.
    assert decision.window_end.utcoffset() == timedelta(0)


def _et(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    """A New York wall time on ``day``, as the UTC instant the runner is given."""
    return datetime.combine(day, time(hour, minute, second), tzinfo=NEW_YORK).astimezone(UTC)


MONDAY = date(2026, 11, 23)
HALF_DAY = date(2026, 11, 27)


@pytest.mark.parametrize(
    ("now", "kind", "close", "end"),
    [
        # 60m01s before 18:05 ET: launches.
        (_et(MONDAY, 17, 4, 59), "RUN", MON_CLOSE, MON_END),
        # Exactly the runway: launches -- it is a minimum, not a margin past one.
        (_et(MONDAY, 17, 5), "RUN", MON_CLOSE, MON_END),
        # 59m59s: too late.
        (_et(MONDAY, 17, 5, 1), "TOO_LATE", MON_CLOSE, MON_END),
        # The half-day closes at 13:00 ET but its window still ends 18:05 ET,
        # so the boundary is the same wall time.
        (_et(HALF_DAY, 17, 4, 59), "RUN", HALF_DAY_CLOSE, HALF_DAY_END),
        (_et(HALF_DAY, 17, 5, 1), "TOO_LATE", HALF_DAY_CLOSE, HALF_DAY_END),
        # 18:04: a minute of window, which would boot a server only to stop it
        # once health answered, cancelling its start-up catch-ups.
        (_et(MONDAY, 18, 4), "TOO_LATE", MON_CLOSE, MON_END),
        (_et(HALF_DAY, 18, 4), "TOO_LATE", HALF_DAY_CLOSE, HALF_DAY_END),
    ],
    ids=["1704_59", "1705_00", "1705_01", "half_day_1704_59", "half_day_1705_01",
         "start_1804", "half_day_1804"],
)
def test_a_launch_needs_an_hours_runway_before_its_stop(
    ws: ModuleType, now: datetime, kind: str, close: datetime, end: datetime
) -> None:
    """The server's start-up catch-ups -- the Saturday audit's, when that was
    missed -- must be able to finish before this runner stops it. Later boots
    are left to ``weekly_audit.py``, which carries its own deadline."""
    decision = ws.decide(now)
    assert decision.kind is ws.Decision[kind]
    # Both carried either way: the launch needs them, and so does the log line
    # naming the stop that was too near.
    assert decision.session_close == close
    assert decision.window_end == end
    assert (end - now >= ws.MIN_RUNWAY) is (kind == "RUN")


def test_a_close_plus_30_later_than_1805_is_the_stop(
    ws: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No NYSE session closes that late, but the stop is the *later* of the
    two and both sides of that are pinned: here close + 30 wins."""
    late_close = datetime(2026, 11, 23, 23, 0, tzinfo=UTC)  # 18:00 ET
    monkeypatch.setattr(ws, "nyse_session_close", lambda _: late_close)
    decision = ws.decide(MON_0830)
    assert decision.kind is ws.Decision.RUN
    assert decision.window_end == late_close + timedelta(minutes=30)


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
        # Good Friday, 3 Apr 2026: NYSE is shut though economic releases may
        # land. Still nothing launches; R8's catch-up takes them next session.
        (datetime(2026, 4, 3, 12, 30, tzinfo=UTC), "NO_SESSION"),
        # At the stop exactly there is nothing left to run.
        (MON_END, "AFTER_STOP"),
        # 18:06 ET.
        (MON_END + timedelta(minutes=1), "AFTER_STOP"),
        # The half-day, after its 18:05 ET stop.
        (HALF_DAY_END, "AFTER_STOP"),
        # Tuesday 03:00 UTC is still Monday 22:00 in New York: Monday's
        # session, stopped -- not Tuesday's, still ahead.
        (datetime(2026, 11, 24, 3, 0, tzinfo=UTC), "AFTER_STOP"),
    ],
    ids=["saturday", "sunday", "thanksgiving", "july_3_observed", "good_friday",
         "at_stop", "start_1806", "half_day_after_stop", "utc_tomorrow_et_tonight"],
)
def test_no_session_or_after_the_stop_starts_nothing(
    ws: ModuleType, now: datetime, kind: str
) -> None:
    decision = ws.decide(now)
    assert decision.kind is ws.Decision[kind]
    if decision.kind is ws.Decision.NO_SESSION:
        assert decision.session_close is None and decision.window_end is None
    else:
        # The stop it missed is carried for the log line.
        assert decision.window_end is not None and decision.window_end <= now


def test_a_weekday_past_the_calendars_published_range_is_named(
    ws: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the calendar's range every day reads as no session, so the runner
    would stop collecting with every log line saying "holiday". It says so."""

    def ended(_: datetime) -> datetime:
        raise ValueError("past the published schedule")

    monkeypatch.setattr(ws, "nyse_session_close", lambda _: None)
    monkeypatch.setattr(ws, "nyse_next_open", ended)
    assert ws.decide(MON_0830).kind is ws.Decision.CALENDAR_EXHAUSTED


def test_the_decision_refuses_a_naive_instant(ws: ModuleType) -> None:
    with pytest.raises(ValueError):
        ws.decide(datetime(2026, 11, 23, 9, 0))


# --------------------------------------------------------------- health watch


def test_one_failed_health_check_after_a_long_gap_is_not_a_loss(ws: ModuleType) -> None:
    """A laptop that slept with the lid shut wakes with the clock hours on.
    The streak counts from the first *failed* check, so the gap is not read
    as minutes of silence."""
    watch = ws.HealthWatch()
    assert watch.observe(True, MON_0830) is False
    assert watch.observe(False, MON_0830 + timedelta(hours=3)) is False


def test_failures_spanning_the_bound_are_a_loss_and_a_success_resets(ws: ModuleType) -> None:
    bound = ws.HEALTH_LOSS_AFTER
    watch = ws.HealthWatch()
    first = MON_0830 + timedelta(minutes=1)
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
    assert watch.observe(False, MON_0830) is False
    assert watch.observe(False, MON_0830 + timedelta(hours=1)) is False
    assert watch.observe(False, MON_0830 + timedelta(hours=1, seconds=1)) is True
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

    now: datetime = MON_0830
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
        (datetime(2026, 4, 3, 12, 30, tzinfo=UTC), "not_a_trading_day"),
        (MON_END, "after_stop"),
        (MON_END + timedelta(minutes=1), "after_stop"),
        (HALF_DAY_END + timedelta(minutes=1), "after_stop"),
        (_et(MONDAY, 17, 5, 1), "too_late"),
        (_et(MONDAY, 18, 4), "too_late"),
        (_et(HALF_DAY, 17, 5, 1), "too_late"),
    ],
    ids=["saturday", "thanksgiving", "good_friday", "at_stop", "start_1806",
         "half_day_1806", "start_1705_01", "start_1804", "half_day_1705_01"],
)
def test_off_session_it_exits_0_and_touches_nothing(
    ws: ModuleType, caplog: pytest.LogCaptureFixture, now: datetime, event: str
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    world = World(now=now)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.launches == 0 and world.health_calls == 0 and world.trace == []
    assert _events(caplog) == [event]


def test_too_late_logs_the_stop_it_was_too_near_and_the_runway(
    ws: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    world = World(now=_et(MONDAY, 18, 4))
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    record = _record(caplog, "too_late")
    assert record.levelno == logging.INFO
    fields = record.fields  # type: ignore[attr-defined]
    assert fields["window_end"] == MON_END.isoformat()
    assert fields["session_close"] == MON_CLOSE.isoformat()
    assert fields["trading_day"] == "2026-11-23"
    assert fields["min_runway_seconds"] == 3600


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
    assert world.launches == 0 and world.trace == [] and world.now == MON_0830
    assert _events(caplog) == ["env_file_missing"]


def test_it_runs_until_1805_et_then_stops_and_releases(
    ws: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="weekday_session")
    # Off the poll grid, so a final sleep that overshot the window would show.
    world = World(now=MON_0830 + timedelta(seconds=7))
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


def test_the_half_day_window_ends_at_1805_et_not_its_close_plus_30(ws: ModuleType) -> None:
    """R8's release-day retries run to 18:00 ET whatever the market did; a
    13:00 ET close must not end them at 13:30."""
    world = World(now=datetime(2026, 11, 27, 13, 30, tzinfo=UTC))
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.now == HALF_DAY_END
    assert world.now != HALF_DAY_CLOSE + timedelta(minutes=30)


def test_a_start_at_1700_et_launches_and_runs_to_1805(
    ws: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    """Past close + 30, but 65 minutes before the stop: more than the runway."""
    caplog.set_level(logging.INFO, logger="weekday_session")
    world = World(now=datetime(2026, 11, 23, 22, 0, tzinfo=UTC))
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.launches == 1
    assert world.trace == ["hold", "launch", "stop", "release"]
    assert world.now == MON_END
    assert _events(caplog)[:3] == ["keep_awake_set", "launching", "healthy"]


def test_the_docstring_names_the_0830_trigger_and_the_1805_stop(ws: ModuleType) -> None:
    """Owner decision Q24d moved the trigger from 09:00 to 08:30 so the 08:40
    first attempts run on time. The registration notes are the docstring, so
    a stale time there is a stale task on the next re-registration."""
    doc = ws.__doc__
    assert doc is not None
    assert "08:30" in doc and "18:05" in doc
    assert "09:00" not in doc


def test_the_docstring_states_the_runway_the_overlap_and_the_offset(ws: ModuleType) -> None:
    """Three things a re-registration or a late logon depends on: the hour's
    runway (a start after 17:05 ET launches nothing), the overlap with
    ``weekly_audit.py`` after the close, and the trigger's ``StartBoundary``
    carrying no UTC offset -- with one, it fires an hour late all summer."""
    doc = ws.__doc__
    assert doc is not None
    assert "MIN_RUNWAY" in doc and "17:05" in doc and "too_late" in doc
    assert "weekly_audit" in doc and "already_running" in doc
    assert "StartBoundary" in doc and "Synchronize across time zones" in doc
    assert "Set-ScheduledTask" in doc


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
    dies = MON_0830 + timedelta(hours=2)
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
    silent_from = MON_0830 + timedelta(hours=1)
    world = World(healthy_at=lambda at: at < silent_from)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.SERVER_DIED
    assert world.launches == 1
    assert world.trace == ["hold", "launch", "stop", "release"]
    # Not before the bound, and within a poll of it.
    assert world.now >= silent_from + ws.HEALTH_LOSS_AFTER
    assert world.now <= silent_from + ws.HEALTH_LOSS_AFTER + ws.SESSION_POLL
    assert _record(caplog, "health_lost").levelno == logging.ERROR


def test_a_brief_health_blip_does_not_end_the_session(ws: ModuleType) -> None:
    blip = MON_0830 + timedelta(hours=1)
    world = World(healthy_at=lambda at: not (blip <= at < blip + timedelta(minutes=1)))
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.OK
    assert world.now == MON_END


def test_a_server_that_dies_before_health_exits_3_and_still_releases(ws: ModuleType) -> None:
    world = World(healthy_at=lambda _: False, dies_at=MON_0830)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.SERVER_FAILED
    assert world.launches == 1
    assert world.trace == ["hold", "launch", "stop", "release"]


def test_a_server_that_never_answers_health_is_bounded(ws: ModuleType) -> None:
    world = World(healthy_at=lambda _: False)
    assert ws.run_weekday_session(world.effects(ws)) is ws.Exit.SERVER_FAILED
    assert MON_0830 + ws.HEALTH_TIMEOUT <= world.now
    assert world.now < MON_0830 + ws.HEALTH_TIMEOUT + timedelta(minutes=1)
    assert world.trace == ["hold", "launch", "stop", "release"]


def test_a_launch_that_raises_still_releases_the_keep_awake(ws: ModuleType) -> None:
    world = World(launch_raises=OSError("SECRET path"))
    with pytest.raises(OSError):
        ws.run_weekday_session(world.effects(ws))
    assert world.trace == ["hold", "launch", "release"]


def test_an_interrupt_mid_session_stops_the_server_and_releases(ws: ModuleType) -> None:
    world = World(sleep_raises_at=MON_0830 + timedelta(hours=1))
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
        ws._log("today", "a line", at=MON_0830)
    finally:
        for handler in ws.logger.handlers:
            handler.close()
        ws.logger.handlers[:] = []
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"event": "yesterday"}
    last = json.loads(lines[-1])
    assert last["event"] == "today" and last["at"] == MON_0830.isoformat()
    assert set(last) == {"logged_at", "level", "message", "event", "at"}


@pytest.mark.skipif(sys.platform != "win32", reason="kernel32 exists on Windows only")
def test_the_real_execution_state_call_is_typed_dword_to_dword(ws: ModuleType) -> None:
    """Without ``argtypes``, ctypes passes a C int and ES_CONTINUOUS (the top
    bit) overflows it with ArgumentError. Inspected only, never called."""
    import ctypes

    function = ws.execution_state_call()
    assert function is not None
    assert function.argtypes == [ctypes.c_uint32]
    assert function.restype is ctypes.c_uint32
