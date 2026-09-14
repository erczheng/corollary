"""The dead-man's switch: it halts, it records, and it never resumes itself.

CLAUDE.md rule 9 is the one item in Phase 2 that cannot be retrofitted, so
this file is written the way the risk suite is: every condition proves it
fires, **and** proves it stays quiet at the boundary below it.

Nothing here sleeps and nothing here opens a socket. The clock is injected and
the connection state is recorded by hand, because a test that waits ninety
seconds is a test that gets marked slow and then gets skipped.
"""

# --------------------------------------------------------------------------
# Why some of these carry ``@pytest.mark.risk`` and some do not
# --------------------------------------------------------------------------
#
# ``uv run python -m pytest -m risk`` is the gate CLAUDE.md makes mandatory
# before any engine change, so what it selects has to mean something. The
# marker means **this test protects a money-safety rule**. When
# ``RiskManager`` lands its limits will be the largest group; today rule 9's
# switch is the first, and it is this file.
#
# A test here is tagged when its failure would mean the engine did something
# unsafe with money at stake: it did not halt when rule 9 says halt, it
# halted when nothing was wrong (the boundary below the timeout -- a false
# halt stops the book trading, which is a cost of its own), it resumed
# itself, it lost the record of the halt, or the human never heard about
# one. That last clause is why the storm cases are tagged: a critical alert
# repeated every five seconds until somebody pulls the plug informs nobody.
#
# A test here is left bare when the engine's *behaviour* is the same either
# way and only its reporting differs:
#
#   * the three ``_log_ongoing`` level tests -- DEBUG or WARNING, the engine
#     is halted and stays halted in both, and the halt's own record is
#     pinned by a tagged test;
#   * ``test_a_halt_does_not_route_to_a_channel_that_is_switched_off`` --
#     the failure direction is one alert too many on a channel the human
#     muted, and *that* a halt notifies at all is tagged;
#   * the whole stream-budget section, plan parsing included -- dropping a
#     symbol silently is a real defect, but the cap is a plan limit rather
#     than a hard rule, and a section that has to be right is not the same
#     set as a section that has to be right *before touching the engine*.
#     ``tests/engine/test_stream.py`` is bare for the same reason;
#   * ``test_closing_a_runtime_that_never_supervised_is_fine`` -- lifecycle
#     hygiene; it asserts nothing about halting.
#
# Adding a test to this file: tag it if breaking it could cost money, leave
# it bare if breaking it could only cost clarity. A marker applied by vibe
# stops meaning anything.

import asyncio
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from corollary.db.models import (
    ENGINE_STATE_ID,
    NOTIFICATION_CHANNELS,
    Base,
    EngineState,
    NotificationRoute,
)
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine.runtime import (
    HALT_EVENT,
    UNLIMITED_STREAM_SYMBOL_CAP,
    WATCHDOG_INTERVAL_SECONDS,
    WATCHDOG_TIMEOUT_SECONDS,
    EngineRuntime,
    HaltDecision,
    HaltRule,
    Notification,
    Watchdog,
    data_plan,
    stream_symbol_cap_for_plan,
)
from corollary.engine.stream import STREAM_SYMBOL_CAP, contract_unit, underlying_unit

T0 = datetime(2026, 9, 13, 13, 30, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class Clock:
    """A hand-wound UTC clock. The only clock any test in this file reads."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class SpyNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def emit(self, notification: Notification) -> None:
        self.sent.append(notification)


class ReadOnlySession(Session):
    """Reads fine and refuses every write -- a read-only disk, or a full one.

    The state the gate's second term exists for, and the one that used to
    storm the alert channel. ``_persist`` reads the row, sets ``halted`` and
    commits; here the *commit* is what fails, so the row the next tick reads
    is byte-for-byte the row this one read, and nothing in the database can
    tell the process its halt ever happened.

    A locked SQLite file, a full disk and a read-only mount all arrive as
    ``OperationalError`` from the commit, so that is what this raises.
    """

    def commit(self) -> None:
        raise OperationalError(
            "UPDATE engine_state SET halted=?",
            None,
            Exception("attempt to write a readonly database"),
        )


class FlakyWrites:
    """A session factory whose commits fail until the database is mended.

    :class:`ReadOnlySession` models a database that never recovers. This one
    models the ordinary case on this stack: a write refused *once*. SQLite
    with WAL and no ``busy_timeout`` raises ``database is locked`` when two
    writers in this one process overlap past sqlite3's five-second default --
    and the writers include ``POST /api/engine/halt``, ``POST
    /api/engine/resume``, the settings audit and the watchdog itself, so the
    collision is likeliest precisely while a fault is in progress. A Windows
    backup or AV handle on the file, and a full disk, arrive the same way and
    clear the same way.

    Flipping :attr:`writable` is the database coming back, and what happens
    after it is what
    ``test_a_persist_that_failed_then_recovered_re_arms_the_switch`` is about.
    """

    def __init__(self, engine: Engine, *, writable: bool = False) -> None:
        self._engine = engine
        self.writable = writable

    def __call__(self) -> Session:
        if self.writable:
            return Session(self._engine)
        return ReadOnlySession(self._engine)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "runtime.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def unmigrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """A database file with no tables -- ``alembic upgrade head`` unrun."""
    engine = create_db_engine(sqlite_url(tmp_path / "unmigrated.db"))
    yield engine
    engine.dispose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def notifier() -> SpyNotifier:
    return SpyNotifier()


@pytest.fixture
def runtime(db_engine: Engine, clock: Clock, notifier: SpyNotifier) -> EngineRuntime:
    return EngineRuntime(
        session_factory=lambda: Session(db_engine),
        now=clock,
        notifier=notifier,
        env={},
        correlation_ids=lambda: "test-correlation-id",
    )


def read_state(engine: Engine) -> EngineState:
    with Session(engine) as session:
        state = session.get(EngineState, ENGINE_STATE_ID)
        assert state is not None
        session.expunge(state)
        return state


def resume_engine(engine: Engine) -> None:
    """End the halt the way a human does -- through the route that does it.

    Deliberately not a hand-written UPDATE. What these tests exercise is the
    runtime's view of a halt that *somebody else's* code path ended, so it has
    to be that code path: ``api/routes/engine.py:_clear_halt``, reached the
    only way anything reaches it.
    """
    from corollary.api.routes.engine import resume

    with Session(engine) as session:
        resume(session)


def halt_engine(engine: Engine, reason: str) -> None:
    """Halt the way a *human* does, through the route a human reaches.

    Same reasoning as :func:`resume_engine`: what these tests exercise is the
    runtime's view of a row somebody else wrote, so somebody else's code path
    has to write it. The reason is free prose -- that is the whole point of
    the cases below, because prose is what a manual halt puts in
    ``halted_reason`` and prose is not a :class:`HaltRule`.
    """
    from corollary.api.routes.engine import halt
    from corollary.api.schemas import HaltRequest

    with Session(engine) as session:
        halt(HaltRequest(reason=reason), session)


# --------------------------------------------------------------------------
# The watchdog, in isolation: the timeout and its boundary
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_ninety_seconds_is_a_named_constant() -> None:
    assert WATCHDOG_TIMEOUT_SECONDS == 90.0


@pytest.mark.risk
def test_a_watchdog_with_no_connection_yet_does_not_halt() -> None:
    """Unarmed until something connects. A cold start is already halted."""
    watchdog = Watchdog(started_at=T0)
    assert watchdog.evaluate(T0 + timedelta(seconds=600)) is None


@pytest.mark.risk
def test_eighty_nine_seconds_without_a_message_does_not_halt() -> None:
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0)
    assert watchdog.evaluate(T0 + timedelta(seconds=89)) is None


@pytest.mark.risk
def test_ninety_seconds_without_a_message_halts() -> None:
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0)
    decision = watchdog.evaluate(T0 + timedelta(seconds=90))
    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE
    assert decision.at == T0 + timedelta(seconds=90)


@pytest.mark.risk
def test_ninety_seconds_without_a_successful_poll_halts() -> None:
    """A poll is liveness too -- the spec says *message or successful poll*."""
    watchdog = Watchdog(started_at=T0)
    watchdog.record_poll(T0)
    assert watchdog.evaluate(T0 + timedelta(seconds=89)) is None
    decision = watchdog.evaluate(T0 + timedelta(seconds=90))
    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE


@pytest.mark.risk
def test_a_later_message_refreshes_the_liveness_clock() -> None:
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0)
    watchdog.record_message(T0 + timedelta(seconds=60))
    assert watchdog.evaluate(T0 + timedelta(seconds=100)) is None
    assert watchdog.evaluate(T0 + timedelta(seconds=150)) is not None


@pytest.mark.risk
def test_a_stream_close_halts_immediately() -> None:
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0)
    watchdog.record_stream_closed(T0 + timedelta(seconds=1), detail="1006 abnormal")
    decision = watchdog.evaluate(T0 + timedelta(seconds=1))
    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    assert "1006 abnormal" in decision.reason


@pytest.mark.risk
def test_the_watchdog_keeps_reporting_an_ongoing_fault() -> None:
    """No latch in here, and that is the fix rather than the bug.

    A latch answered "have I already announced this?" out of memory that a
    human's resume cannot reach, so a resume into an ongoing fault left the
    switch silent for good. Suppression belongs to the runtime, which reads
    ``engine_state.halted`` -- see the two re-halt tests below.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0)
    for seconds in (90, 91, 300):
        decision = watchdog.evaluate(T0 + timedelta(seconds=seconds))
        assert decision is not None, seconds
        assert decision.rule is HaltRule.CONNECTION_STALE


@pytest.mark.risk
def test_activity_ends_the_connection_fault() -> None:
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0)
    assert watchdog.evaluate(T0 + timedelta(seconds=90)) is not None
    watchdog.record_message(T0 + timedelta(seconds=100))
    assert watchdog.evaluate(T0 + timedelta(seconds=150)) is None
    assert watchdog.evaluate(T0 + timedelta(seconds=190)) is not None


@pytest.mark.risk
def test_a_poll_does_not_end_a_stream_close() -> None:
    """The socket is still shut. Only reopening it says otherwise."""
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0)
    watchdog.record_stream_closed(T0, detail="1006")
    assert watchdog.evaluate(T0) is not None

    watchdog.record_poll(T0 + timedelta(seconds=5))
    still_shut = watchdog.evaluate(T0 + timedelta(seconds=5))
    assert still_shut is not None
    assert still_shut.rule is HaltRule.STREAM_CLOSED

    watchdog.record_stream_open(T0 + timedelta(seconds=10))
    assert watchdog.evaluate(T0 + timedelta(seconds=10)) is None
    watchdog.record_stream_closed(T0 + timedelta(seconds=20), detail="1006 again")
    assert watchdog.evaluate(T0 + timedelta(seconds=20)) is not None


@pytest.mark.risk
def test_a_naive_datetime_is_refused() -> None:
    watchdog = Watchdog(started_at=T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        watchdog.record_message(datetime(2026, 9, 13, 13, 30, 0))


# --------------------------------------------------------------------------
# The second condition, which is inert because it has no producer
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_the_heartbeat_condition_is_inert_by_default() -> None:
    """No producer until ``RiskManager`` grows a body, so it is not armed.

    Armed by default it would halt every engine ninety seconds after boot for
    a heartbeat nothing in the codebase sends.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0 + timedelta(seconds=300))
    assert watchdog.evaluate(T0 + timedelta(seconds=300)) is None


@pytest.mark.risk
def test_the_heartbeat_condition_halts_when_it_is_armed() -> None:
    watchdog = Watchdog(started_at=T0, heartbeat_armed=True)
    watchdog.record_message(T0 + timedelta(seconds=89))
    decision = watchdog.evaluate(T0 + timedelta(seconds=90))
    assert decision is not None
    assert decision.rule is HaltRule.HEARTBEAT_STALE


@pytest.mark.risk
def test_an_armed_heartbeat_does_not_halt_at_eighty_nine_seconds() -> None:
    watchdog = Watchdog(started_at=T0, heartbeat_armed=True)
    watchdog.record_message(T0 + timedelta(seconds=89))
    assert watchdog.evaluate(T0 + timedelta(seconds=89)) is None


@pytest.mark.risk
def test_a_heartbeat_refreshes_its_own_clock() -> None:
    watchdog = Watchdog(started_at=T0, heartbeat_armed=True)
    watchdog.record_heartbeat(T0 + timedelta(seconds=60))
    watchdog.record_message(T0 + timedelta(seconds=140))
    assert watchdog.evaluate(T0 + timedelta(seconds=140)) is None
    assert watchdog.evaluate(T0 + timedelta(seconds=151)) is not None


# --------------------------------------------------------------------------
# Cold start, t0, and the halt that outlives a restart
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_cold_start_comes_up_halted(runtime: EngineRuntime, db_engine: Engine) -> None:
    runtime.start()
    assert read_state(db_engine).halted is True


@pytest.mark.risk
def test_t0_is_written_on_the_first_start(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock
) -> None:
    runtime.start()
    assert read_state(db_engine).t0 == clock.now


@pytest.mark.risk
def test_t0_is_not_overwritten_on_a_second_start(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock
) -> None:
    runtime.start()
    first = read_state(db_engine).t0
    clock.advance(3600)
    runtime.start()
    assert read_state(db_engine).t0 == first


@pytest.mark.risk
def test_starting_never_clears_a_halt(
    runtime: EngineRuntime, db_engine: Engine
) -> None:
    """A restart is not a resume -- ``uvicorn --reload`` must not un-halt."""
    runtime.start()
    runtime.record_message()
    runtime.record_stream_closed(detail="1006")
    runtime.check_watchdog()
    assert read_state(db_engine).halted is True

    runtime.start()
    state = read_state(db_engine)
    assert state.halted is True
    assert state.halted_reason is not None


@pytest.mark.risk
def test_the_opening_snapshot_does_not_clear_the_halt(
    runtime: EngineRuntime, db_engine: Engine
) -> None:
    """Rule 9: only ``POST /api/engine/resume`` ends a halt.

    The spec words the cold start as *halted until the opening snapshot
    succeeds*; the runtime records the snapshot as liveness and leaves the
    halt exactly where it found it, because a second code path that clears a
    halt is the thing rule 9 exists to prevent.
    """
    runtime.start()
    runtime.record_opening_snapshot()
    assert read_state(db_engine).halted is True
    assert runtime.opening_snapshot_ok is True


@pytest.mark.risk
def test_reconnecting_leaves_the_engine_halted(
    runtime: EngineRuntime, db_engine: Engine, notifier: SpyNotifier
) -> None:
    """**The test that cannot be retrofitted.**

    Reconnecting into an unverified position state is how a bot doubles a
    position it already holds, so the socket comes back and the halt does not.
    """
    runtime.start()
    runtime.record_message()
    runtime.record_stream_closed(detail="1006 abnormal closure")
    assert runtime.check_watchdog() is not None
    assert read_state(db_engine).halted is True

    runtime.record_stream_open()
    runtime.record_message()
    runtime.record_poll()
    runtime.record_opening_snapshot()

    state = read_state(db_engine)
    assert state.halted is True
    assert state.halted_reason is not None
    assert len(notifier.sent) == 1


@pytest.mark.risk
def test_the_first_fault_after_a_cold_start_is_still_announced(
    runtime: EngineRuntime, db_engine: Engine, notifier: SpyNotifier, clock: Clock
) -> None:
    """The gate asks whether a halt has a *stated reason*, not whether it is set.

    A cold start is halted by the ``engine_state`` default with no reason and
    no timestamp -- the boot state, not a fault. A gate reading ``halted``
    alone therefore swallows the **first** fault after every boot: nothing
    announced, nothing recorded, and a row still claiming the engine is
    halted for no stated cause. This test is what caught that; it is the
    reason the predicate is ``halted and halted_reason is not None``.
    """
    runtime.start()
    booted = read_state(db_engine)
    assert booted.halted is True
    assert booted.halted_reason is None

    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)
    decision = runtime.check_watchdog()

    assert decision is not None
    assert len(notifier.sent) == 1
    assert read_state(db_engine).halted_reason == decision.reason


@pytest.mark.risk
def test_resuming_while_the_stream_is_still_closed_re_halts(
    runtime: EngineRuntime, db_engine: Engine, notifier: SpyNotifier, clock: Clock
) -> None:
    """A resume into an ongoing fault is re-halted on the next tick.

    The fault outlived the resume: the socket never reopened. The switch has
    to say so again, because the alternative is an engine that is running,
    un-halted, with its dead-man's switch permanently silent -- the state this
    module's in-memory latch used to produce, and the state that in Phase 3 is
    an engine opening entries against a feed delivering nothing.

    This is a **halt**, not an auto-resume. Rule 9 forbids the engine ending a
    halt by itself; it requires the engine to start one.
    """
    runtime.start()
    runtime.record_message()
    runtime.record_stream_closed(detail="1006 abnormal closure")
    assert runtime.check_watchdog() is not None
    assert read_state(db_engine).halted is True
    assert len(notifier.sent) == 1

    resume_engine(db_engine)
    assert read_state(db_engine).halted is False

    clock.advance(WATCHDOG_INTERVAL_SECONDS)
    again = runtime.check_watchdog()
    assert again is not None
    assert again.rule is HaltRule.STREAM_CLOSED

    state = read_state(db_engine)
    assert state.halted is True
    assert state.halted_at == clock.now
    assert len(notifier.sent) == 2


@pytest.mark.risk
def test_resuming_while_the_feed_is_still_silent_re_halts(
    runtime: EngineRuntime, db_engine: Engine, notifier: SpyNotifier, clock: Clock
) -> None:
    """The same for staleness, whose latch was the worse of the two.

    ``STREAM_CLOSED`` at least cleared on a reopen. ``CONNECTION_STALE``
    cleared only on a message or a poll -- neither of which can arrive from
    the dead feed that caused the halt, so the blind spot was permanent.
    """
    runtime.start()
    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)
    assert runtime.check_watchdog() is not None
    assert len(notifier.sent) == 1

    resume_engine(db_engine)
    assert read_state(db_engine).halted is False

    clock.advance(600)
    again = runtime.check_watchdog()
    assert again is not None
    assert again.rule is HaltRule.CONNECTION_STALE
    assert read_state(db_engine).halted is True
    assert len(notifier.sent) == 2


@pytest.mark.risk
def test_an_ongoing_fault_is_not_announced_on_every_tick(
    runtime: EngineRuntime, db_engine: Engine, notifier: SpyNotifier, clock: Clock
) -> None:
    """The other half of the same gate: no alert storm while nobody resumes.

    Reading ``engine_state`` per tick has to be as quiet as the latch it
    replaced. One fault, one notification, until a human does something about
    it.
    """
    runtime.start()
    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)
    assert runtime.check_watchdog() is not None

    for _ in range(12):
        clock.advance(WATCHDOG_INTERVAL_SECONDS)
        assert runtime.check_watchdog() is None

    assert len(notifier.sent) == 1
    assert read_state(db_engine).halted is True


@pytest.mark.risk
def test_a_database_that_cannot_be_written_does_not_storm_the_alert_channel(
    db_engine: Engine, clock: Clock, notifier: SpyNotifier
) -> None:
    """Readable row, refused write, ongoing fault: **one** critical alert.

    The third row of the gate's table, and the one the other three depend on
    being right. When the row can be read but not written, ``_persist`` never
    sets ``halted``, so every tick reads back the same cold-start row --
    ``halted`` true with no reason -- and the row alone would announce the
    same fault forever. Measured before the second term landed: five ticks on
    one ongoing fault, five critical notifications. Today the sink is
    ``LoggingNotifier`` and that is five log lines; the moment a Discord
    transport lands it is a webhook every five seconds until somebody pulls
    the plug.

    ``_announced`` is what closes it, and this is the only test that exercises
    the branch that sets it. It is also the test that proves the *narrowing*
    of that flag -- set only when the write failed -- did not buy the resume
    cases at the price of this one.
    """
    runtime = EngineRuntime(
        session_factory=lambda: ReadOnlySession(db_engine),
        now=clock,
        notifier=notifier,
        env={},
        correlation_ids=lambda: "test-correlation-id",
    )
    runtime.start()
    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)

    first = runtime.check_watchdog()
    assert first is not None
    assert first.rule is HaltRule.CONNECTION_STALE

    for _ in range(5):
        clock.advance(WATCHDOG_INTERVAL_SECONDS)
        assert runtime.check_watchdog() is None

    assert len(notifier.sent) == 1
    assert notifier.sent[0].severity == "critical"

    # The write really was refused, which is what makes the run above the
    # storm case rather than the ordinary one: the row still carries the
    # cold-start halt with no reason, exactly as the gate read it on every
    # tick, so nothing in the database suppressed anything.
    state = read_state(db_engine)
    assert state.halted is True
    assert state.halted_reason is None


@pytest.mark.risk
def test_a_persist_that_failed_then_recovered_re_arms_the_switch(
    db_engine: Engine, clock: Clock, notifier: SpyNotifier
) -> None:
    """The join: refused write, mended database, human resume, fault still on.

    Neither of the two tests either side of this one reaches it. The storm
    test never lets the database recover, so ``_announced`` staying set is
    exactly right there; the resume tests never have a failed write in their
    history, so ``_announced`` is never set at all. Between them sat a state
    covered by nothing, and it disarmed rule 9 for the life of the process:

    * a single refused commit sets ``_announced``;
    * the gate then suppresses every later call to :meth:`EngineRuntime.halt`,
      so ``halt`` can never clear it;
    * ``_halt_is_recorded`` clears it only on a row carrying an *explained*
      halt -- which the refused write is precisely what prevented;
    * a resume writes ``halted=False, halted_reason=None``, which is not that.

    So the flag outlived the fault, the outage and the operator, and the next
    genuinely new fault was suppressed: not halted, not recorded, not
    announced. One ``database is locked`` on a busy SQLite file was enough,
    and there is no shortage of writers in this process to collide with.

    What closes it is that the suppressed tick *retries the write*. When the
    row does not carry our halt, the honest reading is not "somebody
    resumed" but "our record is missing", and the fix for a missing record is
    to write it. Once it lands the row is the record again -- rule 8 satisfied
    for a halt that really did happen -- and a resume is visible to the gate
    the ordinary way, which re-arms the switch.
    """
    sessions = FlakyWrites(db_engine)
    runtime = EngineRuntime(
        session_factory=sessions,
        now=clock,
        notifier=notifier,
        env={},
        correlation_ids=lambda: "test-correlation-id",
    )
    runtime.start()
    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)

    first = runtime.check_watchdog()
    assert first is not None
    assert first.rule is HaltRule.CONNECTION_STALE
    assert len(notifier.sent) == 1
    # The commit really was refused: the row still carries the cold-start
    # halt, with no reason, so nothing in the database knows this happened.
    assert read_state(db_engine).halted_reason is None

    # The database is mended -- the lock cleared, the backup finished, the
    # disk was emptied. The fault has not ended, so nothing announces; what
    # has to happen on this tick is the record catching up.
    sessions.writable = True
    clock.advance(WATCHDOG_INTERVAL_SECONDS)
    assert runtime.check_watchdog() is None
    assert len(notifier.sent) == 1
    caught_up = read_state(db_engine)
    assert caught_up.halted is True
    assert caught_up.halted_reason is not None
    assert caught_up.halted_at is not None

    # A human reads the alert, resumes, and the feed is still dead.
    resume_engine(db_engine)
    assert read_state(db_engine).halted is False

    clock.advance(WATCHDOG_INTERVAL_SECONDS)
    again = runtime.check_watchdog()
    assert again is not None
    assert again.rule is HaltRule.CONNECTION_STALE
    assert read_state(db_engine).halted is True
    # And the operator hears about it, rather than believing the resume took.
    assert len(notifier.sent) == 2


@pytest.mark.risk
def test_a_resume_the_gate_could_not_observe_ends_with_the_engine_halted(
    db_engine: Engine, clock: Clock, notifier: SpyNotifier
) -> None:
    """The same join in the other order: the resume lands *before* the retry.

    Above, a tick happens between the database recovering and the human
    resuming, so the retry lands first, the row polices the halt, and the
    resume is an observation the gate can act on -- re-halt, second alert.
    Here nobody ticks in between, so on the first tick after the resume the
    row carries no explained halt and ``_announced`` is still set. Those two
    facts together are ambiguous by construction: *a human resumed* and *our
    write never landed* read identically, which is the entire reason
    ``_announced`` exists.

    So the tick resolves it the only way it has evidence for -- the record is
    missing, write it -- and the engine ends the tick **halted, recorded, and
    quiet**. That is the conservative side of the ambiguity and it is the side
    rule 9 wants: the fault is still here, so running is not an option, and
    ``GET /api/engine/state`` now says halted rather than lying. The operator
    loses a notification they would have got in the other order; they do not
    lose the halt. A second resume is then a *real* observation and announces
    the ordinary way, which the last three lines check.

    Pinned because both halves are easy to talk yourself out of: announcing
    here would mean announcing on the tick a retry lands in the storm case
    too, and skipping the write would leave the latch exactly where this pass
    found it.
    """
    sessions = FlakyWrites(db_engine)
    runtime = EngineRuntime(
        session_factory=sessions,
        now=clock,
        notifier=notifier,
        env={},
        correlation_ids=lambda: "test-correlation-id",
    )
    runtime.start()
    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)
    assert runtime.check_watchdog() is not None
    assert len(notifier.sent) == 1

    sessions.writable = True
    resume_engine(db_engine)
    assert read_state(db_engine).halted is False

    clock.advance(WATCHDOG_INTERVAL_SECONDS)
    assert runtime.check_watchdog() is None
    recovered = read_state(db_engine)
    assert recovered.halted is True
    assert recovered.halted_reason is not None
    assert len(notifier.sent) == 1

    # From here the row is the record again, so the next resume is visible and
    # behaves like every other resume into a live fault.
    resume_engine(db_engine)
    clock.advance(WATCHDOG_INTERVAL_SECONDS)
    again = runtime.check_watchdog()
    assert again is not None
    assert read_state(db_engine).halted is True
    assert len(notifier.sent) == 2


@pytest.mark.risk
def test_a_fault_that_ends_clears_an_announcement_the_row_never_took(
    db_engine: Engine, clock: Clock, notifier: SpyNotifier
) -> None:
    """The other bound on the latch: an announcement belongs to one episode.

    The database never recovers here, so the retry above can never land and
    ``_announced`` can only be cleared by the fault itself ending. It must be,
    or the first refused write silences every future episode as well as this
    one -- the same defect, reached without ever mending the database.

    The distinction against
    ``test_a_database_that_cannot_be_written_does_not_storm_the_alert_channel``
    is the whole point and is worth stating: there the fault never ends, so
    five ticks are five looks at *one* episode and must produce one alert.
    Here the feed comes back and dies again, which is two episodes, and two
    alerts is the correct count. Suppressing the second would be a dead feed
    nobody is told about.
    """
    runtime = EngineRuntime(
        session_factory=lambda: ReadOnlySession(db_engine),
        now=clock,
        notifier=notifier,
        env={},
        correlation_ids=lambda: "test-correlation-id",
    )
    runtime.start()
    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)

    first = runtime.check_watchdog()
    assert first is not None
    assert len(notifier.sent) == 1

    # The feed comes back. Nothing to announce, and the episode the
    # announcement belonged to is over.
    runtime.record_message()
    assert runtime.check_watchdog() is None
    assert len(notifier.sent) == 1

    # It dies again. The database is still refusing writes, so memory is the
    # only thing that could suppress this -- and it must not.
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)
    second = runtime.check_watchdog()
    assert second is not None
    assert second.rule is HaltRule.CONNECTION_STALE
    assert len(notifier.sent) == 2
    assert notifier.sent[1].severity == "critical"


def test_the_record_of_a_late_halt_names_the_alert_it_belongs_to(
    db_engine: Engine,
    clock: Clock,
    notifier: SpyNotifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``engine_halt_persisted_late`` carries the announcing halt's id, not a new one.

    The retry writes a record for a halt that was announced half an hour
    earlier, so three artifacts describe one event at two different times: the
    critical notification and ``engine_halted`` at the moment of the fault,
    and this row plus ``engine_halt_persisted_late`` at the moment the
    database came back. CLAUDE.md's convention is one correlation id per
    decision, and the late record had none -- leaving a post-incident reader
    starting from ``engine_state`` with no key joining the row to the alert
    and a gap to reconcile out of prose.

    **``halted_at`` deliberately stays the moment the write landed**, and this
    test pins that rather than leaving it to be changed by whoever next reads
    the gap and assumes it is a bug. ``halted_at`` and ``halted_reason`` are
    written from one :class:`HaltDecision` and must describe one moment:
    backdating the timestamp alone would put ``13:31:30`` beside a sentence
    measuring the outage at ``1895s``, and backdating both would persist a
    sentence understating a half-hour outage by half an hour -- in the one
    field the operator reads, with no second artifact beside it. The
    announcement time is not lost: it is on ``engine_halted``, and the late
    record now carries both the id that joins them and ``announced_at``
    outright, so the gap is arithmetic rather than prose.

    Left un-tagged deliberately, by the line at the top of this file: the
    engine is halted, recorded and quiet either way, and what changes is only
    whether a human can trace it afterwards. ``_announced`` itself, which is
    the part that could cost money, is tagged three tests above.
    """
    ids = iter(["halt-announced", "halt-second", "halt-third"])
    sessions = FlakyWrites(db_engine)
    runtime = EngineRuntime(
        session_factory=sessions,
        now=clock,
        notifier=notifier,
        env={},
        correlation_ids=lambda: next(ids),
    )
    runtime.start()
    runtime.record_message()
    announced_at = clock.advance(WATCHDOG_TIMEOUT_SECONDS)

    with caplog.at_level("DEBUG", logger="corollary.engine.runtime"):
        assert runtime.check_watchdog() is not None
        # The outage runs for half an hour before the database takes a write.
        sessions.writable = True
        landed_at = clock.advance(1805)
        assert runtime.check_watchdog() is None

    announced = [
        r for r in caplog.records if getattr(r, "event", "") == "engine_halted"
    ]
    late = [
        r
        for r in caplog.records
        if getattr(r, "event", "") == "engine_halt_persisted_late"
    ]
    assert len(announced) == 1
    assert len(late) == 1

    # The join key: one decision, one id, on every artifact describing it.
    assert announced[0].correlation_id == "halt-announced"
    assert notifier.sent[0].correlation_id == "halt-announced"
    assert late[0].correlation_id == "halt-announced"

    # And both ends of the gap, stated rather than inferred.
    assert late[0].announced_at == announced_at.isoformat()
    assert late[0].at == landed_at.isoformat()

    # The row is written at the moment the write landed, with the reason
    # measured at that same moment. Changing either means changing both.
    state = read_state(db_engine)
    assert state.halted is True
    assert state.halted_at == landed_at
    assert state.halted_reason is not None
    assert "1895s" in state.halted_reason


def test_a_late_record_names_its_own_episode_rather_than_an_earlier_one(
    db_engine: Engine,
    clock: Clock,
    notifier: SpyNotifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two episodes, and the id travels with the announcement that is still open.

    The provenance of an announcement is cleared everywhere ``_announced`` is,
    which is what stops the second episode's late record being filed under the
    first episode's alert. Captured because an id remembered once at the first
    refused write would pass the test above and be wrong here -- and wrong in
    the direction that points an incident reader at the wrong alert, which is
    worse than pointing them at none.
    """
    ids = iter(["halt-first-episode", "halt-second-episode", "halt-third"])
    sessions = FlakyWrites(db_engine)
    runtime = EngineRuntime(
        session_factory=sessions,
        now=clock,
        notifier=notifier,
        env={},
        correlation_ids=lambda: next(ids),
    )
    runtime.start()
    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)
    assert runtime.check_watchdog() is not None

    # The feed comes back: the episode the first announcement belonged to is
    # over, and its id goes with it.
    runtime.record_message()
    assert runtime.check_watchdog() is None

    # It dies again. A second announcement, refused by the same database.
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)
    with caplog.at_level("DEBUG", logger="corollary.engine.runtime"):
        assert runtime.check_watchdog() is not None
        sessions.writable = True
        clock.advance(WATCHDOG_INTERVAL_SECONDS)
        assert runtime.check_watchdog() is None

    late = [
        r
        for r in caplog.records
        if getattr(r, "event", "") == "engine_halt_persisted_late"
    ]
    assert len(late) == 1
    assert late[0].correlation_id == "halt-second-episode"
    assert notifier.sent[1].correlation_id == "halt-second-episode"


def test_an_ongoing_fault_the_record_already_describes_logs_at_debug(
    runtime: EngineRuntime, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """The quiet half of ``_log_ongoing``: steady state stays at DEBUG.

    An engine halted for a silent feed, with the feed still silent, is the
    expected state and not news. At a visible level every five seconds it is
    how a log stops being read -- and a log nobody reads is rule 8 failing
    quietly rather than loudly.
    """
    runtime.start()
    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)
    assert runtime.check_watchdog() is not None

    with caplog.at_level("DEBUG", logger="corollary.engine.runtime"):
        clock.advance(WATCHDOG_INTERVAL_SECONDS)
        assert runtime.check_watchdog() is None

    ongoing = [
        r for r in caplog.records if getattr(r, "event", "") == "engine_halt_ongoing"
    ]
    assert len(ongoing) == 1
    assert ongoing[0].levelname == "DEBUG"
    assert ongoing[0].masked is False
    assert ongoing[0].recorded_rule == HaltRule.CONNECTION_STALE.value


def test_a_manual_halt_masking_a_dead_feed_logs_at_warning(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock,
    notifier: SpyNotifier, caplog: pytest.LogCaptureFixture,
) -> None:
    """The loud half: a halt in force whose reason is not this fault.

    A human halts to adjust limits; the feed dies underneath the halt. The
    row says ``adjusting limits``, which is true and is not the whole truth.
    Nothing can be opened either way, and the next tick after a resume halts
    for the real fault and notifies -- but for the duration of the manual
    halt the record does not describe the fault, and at DEBUG the reader
    asking *"when did the feed die?"* afterwards has nothing at all.
    """
    runtime.start()
    runtime.record_message()
    halt_engine(db_engine, "adjusting limits")
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)

    with caplog.at_level("DEBUG", logger="corollary.engine.runtime"):
        assert runtime.check_watchdog() is None

    ongoing = [
        r for r in caplog.records if getattr(r, "event", "") == "engine_halt_ongoing"
    ]
    assert len(ongoing) == 1
    assert ongoing[0].levelname == "WARNING"
    assert ongoing[0].masked is True
    assert ongoing[0].rule == HaltRule.CONNECTION_STALE.value
    # Nobody wrote a rule this process could compare against, so it says so
    # rather than guessing one from the prose.
    assert ongoing[0].recorded_rule is None
    # Still not news to the alert channel: the engine is halted either way,
    # and rule 9 keeps it that way until a human resumes.
    assert notifier.sent == []


def test_a_manual_halt_replacing_this_engines_reason_stops_reading_as_recorded(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The row moved out from under the halt this process wrote.

    This engine halts for a dead feed and records it. A human then halts
    again -- ``POST /api/engine/halt`` on an already-halted engine replaces
    the stated reason -- so the row now explains something else while the
    feed is still dead. The rule this process wrote is no longer what the
    record says, and the honest answer is WARNING: the record and the fault
    have come apart, which is exactly the state the level is for.

    This is why the match is made against the *text* written rather than
    against the fact that a halt happened. A latch remembering only "we
    halted for CONNECTION_STALE" would call this steady state and go quiet.
    """
    runtime.start()
    runtime.record_message()
    clock.advance(WATCHDOG_TIMEOUT_SECONDS)
    assert runtime.check_watchdog() is not None

    halt_engine(db_engine, "adjusting limits")

    with caplog.at_level("DEBUG", logger="corollary.engine.runtime"):
        clock.advance(WATCHDOG_INTERVAL_SECONDS)
        assert runtime.check_watchdog() is None

    ongoing = [
        r for r in caplog.records if getattr(r, "event", "") == "engine_halt_ongoing"
    ]
    assert len(ongoing) == 1
    assert ongoing[0].levelname == "WARNING"
    assert ongoing[0].masked is True
    assert ongoing[0].recorded_rule is None


@pytest.mark.risk
def test_the_runtime_has_no_code_that_clears_a_halt() -> None:
    """Structural, the way ``test_no_order_path`` is structural.

    ``api/routes/engine.py:_clear_halt`` is the only thing in this codebase
    that ends a halt. A grep is what keeps that true through a
    plausible-looking diff.

    ``tests/api/test_engine_routes.py`` already greps every file under
    ``corollary/`` for an assignment of ``False`` to ``halted``; this one
    covers what that regex cannot see. Every pattern below is a way to clear a halt without
    ever writing that literal, and one of them -- importing ``resume``, which
    is public, from a module this file already imports from -- is two lines
    away at all times.
    """
    source = (
        Path(__file__).resolve().parents[2] / "corollary" / "engine" / "runtime.py"
    ).read_text(encoding="utf-8")

    # The helper itself, and the plain assignment in either spacing. The local
    # in ``start`` is named ``is_halted`` precisely so these cannot
    # false-positive on it: a guard that cries wolf is a guard somebody
    # deletes.
    assert "_clear_halt" not in source
    assert "halted = False" not in source
    assert "halted=False" not in source

    # Every assignment to the attribute, with its right-hand side: the only
    # one allowed is ``True``. Catches ``= flag``, ``= not state.halted``,
    # ``= some_computed_thing``. Written as a capture rather than a negative
    # lookahead, because ``\.halted[ \t]*=[ \t]*(?!True\b)`` backtracks the
    # [ \t]* to zero width and matches the *space* before ``True`` -- the one
    # assignment this file exists to allow. Under ``assert not
    # re.search(...)`` that fires on correct code: the guard would be red
    # while the line was right and green only while it was missing. It
    # cries wolf rather than waving anything through, and a guard that
    # cries wolf is a guard somebody deletes -- which is how it would
    # end up waving everything through after all.
    assignments = re.findall(r"\.halted[ \t]*=[ \t]*(\S+)", source)
    assert assignments == ["True"], assignments

    # Writing the attribute without naming it, or without an ORM attribute at
    # all -- ``update()``/``values()`` and raw SQL both bypass every grep
    # above.
    assert "setattr(" not in source
    assert ".values(" not in source
    assert "update(EngineState)" not in source
    assert not re.search(r"(?i)update\s+engine_state", source)
    assert not re.search(r"\btext\(", source)

    # Calling the resume route. ``resume`` is public and lives in the module
    # this file already imports ``engine_state`` and ``mark_started`` from.
    assert not re.search(r"import[^\n]*\bresume\b", source)
    assert "resume(" not in source


# --------------------------------------------------------------------------
# What a halt records, and who hears about it
# --------------------------------------------------------------------------


@pytest.mark.risk
def test_a_halt_persists_the_reason_and_a_utc_timestamp(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock
) -> None:
    runtime.start()
    runtime.record_message()
    clock.advance(90)
    decision = runtime.check_watchdog()
    assert decision is not None

    state = read_state(db_engine)
    assert state.halted is True
    assert state.halted_at == clock.now
    assert state.halted_at is not None and state.halted_at.tzinfo is not None
    assert state.halted_reason == decision.reason
    assert state.halted_reason is not None and len(state.halted_reason) <= 256


@pytest.mark.risk
def test_a_halt_emits_one_critical_notification(
    runtime: EngineRuntime, notifier: SpyNotifier, clock: Clock
) -> None:
    runtime.start()
    runtime.record_poll()
    clock.advance(90)
    runtime.check_watchdog()

    assert len(notifier.sent) == 1
    sent = notifier.sent[0]
    assert sent.severity == "critical"
    assert sent.event == HALT_EVENT
    assert sent.correlation_id == "test-correlation-id"
    assert sent.at == clock.now
    assert "discord" in sent.channels
    assert "bell" in sent.channels


def test_a_halt_does_not_route_to_a_channel_that_is_switched_off(
    db_engine: Engine, clock: Clock, notifier: SpyNotifier
) -> None:
    with Session(db_engine) as session:
        route = session.get(NotificationRoute, (HALT_EVENT, "discord"))
        assert route is not None
        route.enabled = False
        session.commit()

    runtime = EngineRuntime(
        session_factory=lambda: Session(db_engine),
        now=clock,
        notifier=notifier,
        env={},
    )
    runtime.start()
    runtime.record_poll()
    clock.advance(90)
    runtime.check_watchdog()

    assert notifier.sent[0].channels == ("bell",)


@pytest.mark.risk
def test_a_halt_still_notifies_when_the_database_is_unavailable(
    unmigrated_engine: Engine, clock: Clock, notifier: SpyNotifier
) -> None:
    """The switch's alert must not be swallowed by the thing it alerts on."""
    runtime = EngineRuntime(
        session_factory=lambda: Session(unmigrated_engine),
        now=clock,
        notifier=notifier,
        env={},
    )
    runtime.start()
    runtime.record_poll()
    clock.advance(90)
    decision = runtime.check_watchdog()

    assert decision is not None
    assert len(notifier.sent) == 1
    assert notifier.sent[0].severity == "critical"


@pytest.mark.risk
def test_a_halt_notifies_even_when_the_persist_step_raises_something_else(
    clock: Clock, notifier: SpyNotifier
) -> None:
    """Not only ``SQLAlchemyError``. The deferred import can raise too.

    ``_persist`` imports ``api/routes/engine.py`` *inside* the halt, to break
    a genuine circular import. Catching only the database's exception left an
    ``ImportError`` propagating out of ``halt()`` before the log line and
    before the notification -- the alert about one fault swallowed by an
    unrelated second one, at the exact moment it mattered.
    """

    def broken_session() -> Session:
        raise ImportError("cannot import name 'engine_state'")

    runtime = EngineRuntime(
        session_factory=broken_session, now=clock, notifier=notifier, env={}
    )
    runtime.halt(
        HaltDecision(
            rule=HaltRule.STREAM_CLOSED,
            reason="the market data stream closed (1006).",
            inputs={"detail": "1006"},
            at=clock.now,
        )
    )

    assert len(notifier.sent) == 1
    assert notifier.sent[0].severity == "critical"
    # Routing was unreadable for the same reason; it falls back to every
    # channel rather than to none.
    assert set(notifier.sent[0].channels) == set(NOTIFICATION_CHANNELS)


@pytest.mark.risk
def test_the_rule_the_inputs_and_the_timestamp_are_logged(
    runtime: EngineRuntime, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 8's standard, applied to a halt rather than to a rejection."""
    runtime.start()
    runtime.record_message()
    clock.advance(90)
    with caplog.at_level("WARNING", logger="corollary.engine.runtime"):
        runtime.check_watchdog()

    records = [r for r in caplog.records if getattr(r, "event", "") == "engine_halted"]
    assert len(records) == 1
    record = records[0]
    assert record.rule == HaltRule.CONNECTION_STALE.value
    assert record.at == clock.now.isoformat()
    assert record.correlation_id == "test-correlation-id"
    assert record.inputs["elapsed_seconds"] == 90.0
    assert record.inputs["timeout_seconds"] == WATCHDOG_TIMEOUT_SECONDS


# --------------------------------------------------------------------------
# The stream budget: the cap is derived from the plan of record
# --------------------------------------------------------------------------


def test_the_basic_plan_caps_the_stream_at_thirty() -> None:
    assert stream_symbol_cap_for_plan("basic") == STREAM_SYMBOL_CAP == 30


def test_the_paid_plan_lifts_the_cap() -> None:
    assert stream_symbol_cap_for_plan("algo_trader_plus") == UNLIMITED_STREAM_SYMBOL_CAP
    assert UNLIMITED_STREAM_SYMBOL_CAP > STREAM_SYMBOL_CAP


def test_an_unknown_plan_reads_as_basic() -> None:
    """The restrictive direction: a typo must not unlock a feed nobody bought."""
    assert data_plan({"ALPACA_DATA_PLAN": "platinum"}) == "basic"
    assert data_plan({}) == "basic"
    assert data_plan({"ALPACA_DATA_PLAN": "  Algo_Trader_Plus "}) == "algo_trader_plus"


def test_the_plan_names_match_the_plan_of_record() -> None:
    """Pinned against ``api/routes/settings.py``, which owns the variable.

    The engine deliberately does not import the API layer, so this assertion
    is what stops the two spellings drifting apart.
    """
    from corollary.api.routes import settings as settings_route
    from corollary.engine import runtime as runtime_module

    assert runtime_module.BASIC_PLAN == settings_route.BASIC_PLAN
    assert runtime_module.PAID_PLAN == settings_route.PAID_PLAN
    assert runtime_module.ALPACA_DATA_PLAN_ENV == settings_route.ALPACA_DATA_PLAN_ENV


def test_the_plan_parsing_matches_the_plan_of_record() -> None:
    """The names are pinned above; this pins what the parser does with them.

    ``data_plan`` is a **re-implementation** of
    ``api/routes/settings.py:_plan``, not a copy of it: the engine
    deliberately does not import the API layer, and the two genuinely differ
    -- membership is tested against a mapping of labels there and a tuple of
    names here, the warning they log carries a different event name, and the
    prose around both is different. Only the *answers* have to agree, so
    answers are what this compares.

    **The plan of record is walked, not listed.** The drift that costs money
    is a third plan added to ``settings.py:_PLAN_LABELS`` and not to
    ``data_plan``'s ``(BASIC_PLAN, PAID_PLAN)``: Settings would report the
    new plan, the engine would read ``basic``, and the stream would cut at
    thirty symbols on an account that paid for more -- silently, because a
    symbol dropped for budget looks exactly like a symbol nobody subscribed.
    A fixed list of environments cannot see that. Iterating the mapping can,
    and fails here the moment it happens.
    """
    from corollary.api.routes import settings as settings_route

    cases: list[dict[str, str]] = [
        # Every plan Settings knows, spelled the way it knows it, plus the two
        # ways a hand-edited ``.env`` mangles one.
        *(
            {settings_route.ALPACA_DATA_PLAN_ENV: spelling}
            for plan in settings_route._PLAN_LABELS
            for spelling in (plan, plan.upper(), f"  {plan.title()}  ")
        ),
        {},
        {"ALPACA_DATA_PLAN": ""},
        {"ALPACA_DATA_PLAN": "   "},
        {"ALPACA_DATA_PLAN": "basic"},
        {"ALPACA_DATA_PLAN": "BASIC"},
        {"ALPACA_DATA_PLAN": "algo_trader_plus"},
        {"ALPACA_DATA_PLAN": "  Algo_Trader_Plus  "},
        {"ALPACA_DATA_PLAN": "platinum"},
        {"ALPACA_DATA_PLAN": "opra"},
        {"ALPACA_DATA_PLAN": "algo trader plus"},
        {"ALPACA_DATA_PLAN": "0"},
    ]
    for env in cases:
        assert data_plan(env) == settings_route._plan(env), env

    # Agreeing is not enough on its own: two parsers that both fell back to
    # basic would agree on every line of the loop above and be wrong
    # together. Each plan of record has to come back as *itself*.
    for plan in settings_route._PLAN_LABELS:
        assert data_plan({settings_route.ALPACA_DATA_PLAN_ENV: plan}) == plan


def test_the_runtime_plans_subscriptions_at_the_basic_cap(
    runtime: EngineRuntime,
) -> None:
    units = [contract_unit(f"p{i}", [f"SYM{i}"]) for i in range(31)]
    plan = runtime.plan_stream_subscriptions(units)
    assert plan.cap == 30
    assert len(plan.subscribed) == 30
    assert plan.not_streamed == 1


def test_an_upgraded_account_stops_dropping_symbols_at_thirty(
    db_engine: Engine, clock: Clock
) -> None:
    runtime = EngineRuntime(
        session_factory=lambda: Session(db_engine),
        now=clock,
        env={"ALPACA_DATA_PLAN": "algo_trader_plus"},
    )
    units = [contract_unit(f"p{i}", [f"SYM{i}"]) for i in range(31)]
    plan = runtime.plan_stream_subscriptions(units)
    assert plan.cap == UNLIMITED_STREAM_SYMBOL_CAP
    assert plan.not_streamed == 0


def test_a_subscription_plan_carries_the_runtime_clock_and_a_correlation_id(
    runtime: EngineRuntime, clock: Clock
) -> None:
    plan = runtime.plan_stream_subscriptions([underlying_unit("AAPL")])
    assert plan.at == clock.now
    assert plan.correlation_id == "test-correlation-id"


# --------------------------------------------------------------------------
# The asyncio supervisor, and the lifespan
# --------------------------------------------------------------------------


@pytest.mark.risk
@pytest.mark.asyncio
async def test_the_supervisor_halts_without_anyone_ticking_it(
    db_engine: Engine, clock: Clock, notifier: SpyNotifier
) -> None:
    """The watchdog runs itself once the runtime is supervising."""
    runtime = EngineRuntime(
        session_factory=lambda: Session(db_engine),
        now=clock,
        notifier=notifier,
        env={},
        watchdog_interval_seconds=0.01,
    )
    runtime.start()
    runtime.record_message()
    clock.advance(120)
    runtime.supervise()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if notifier.sent:
            break
    await runtime.aclose()

    assert len(notifier.sent) == 1
    assert read_state(db_engine).halted is True


@pytest.mark.asyncio
async def test_closing_a_runtime_that_never_supervised_is_fine(
    db_engine: Engine, clock: Clock
) -> None:
    runtime = EngineRuntime(
        session_factory=lambda: Session(db_engine), now=clock, env={}
    )
    runtime.start()
    await runtime.aclose()


@pytest.mark.risk
def test_the_lifespan_exposes_a_started_runtime(db_engine: Engine) -> None:
    """Decision 1 is *one process*, so the runtime lives in the lifespan."""
    from corollary.api.app import create_app
    from corollary.api.deps import ServiceRegistry

    class _NothingToClose:
        async def aclose(self) -> None:
            return None

    registry = ServiceRegistry(
        brokers={},
        provider=lambda: _NothingToClose(),
        missing_live_credentials=("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY"),
    )
    app = create_app(registry=registry, db_engine=db_engine)
    with TestClient(app) as client:
        assert isinstance(app.state.engine_runtime, EngineRuntime)
        body = client.get("/api/engine/state").json()
        assert body["halted"] is True
        assert body["t0"] is not None
