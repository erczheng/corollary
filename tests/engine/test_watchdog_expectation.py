"""Rule 9's staleness conditions, gated on whether a feed is expected at all.

Per-socket staleness halts a socket that has gone ninety seconds without a
message. That is right while the socket is held open in a market session and
**wrong** the rest of the time: the option socket is silent from 16:00 to
09:30 because there is nothing to say, and an engine that halts itself every
evening is an engine whose halts nobody reads.

So the watchdog is told when a feed is expected. The composition root --
``corollary/engine/sockets.py``, which owns the sockets' lifetime -- is the
only caller, because it is the only thing that knows which sockets it is
holding open.

The line between what is gated and what is not is the point of this file:

* **Staleness is gated.** Silence is evidence only against an expectation.
* **A close is not.** A close is a fact whenever it arrives, and a socket that
  drops at 15:59:50 must still halt the engine even though the tick that
  observes it lands after the bell.
* **The heartbeat is not.** The risk manager is expected to be alive whenever
  the process is, session or no session.
"""

from datetime import datetime, timedelta, timezone

import pytest

from corollary.engine.runtime import HaltRule, Watchdog

pytestmark = pytest.mark.risk

T0 = datetime(2026, 9, 14, 13, 30, 0, tzinfo=timezone.utc)
TIMEOUT = 90.0

OPTION = "option_quotes"
EQUITY = "equity_quotes"
TRADE = "trade_updates"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def watchdog(*, heartbeat_armed: bool = False) -> Watchdog:
    return Watchdog(
        started_at=T0, timeout_seconds=TIMEOUT, heartbeat_armed=heartbeat_armed
    )


def test_a_socket_the_engine_expects_still_halts_when_it_goes_silent() -> None:
    """The condition this whole module exists for. Gating must not disarm it."""
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)
    dog.record_message(at(1), socket=OPTION)

    decision = dog.evaluate(at(91))

    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE
    assert decision.inputs["socket"] == OPTION


def test_the_boundary_is_ninety_seconds_after_the_last_message() -> None:
    """Permits at eighty-nine, halts at ninety, measured from the message."""
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)
    dog.record_message(at(1), socket=OPTION)

    assert dog.evaluate(at(90)) is None
    assert dog.evaluate(at(91)) is not None


def test_a_socket_nobody_expects_is_never_the_socket_a_halt_names() -> None:
    """The per-socket gate, in isolation from the engine-wide one.

    Stopping one socket's expectation stops that socket being judged and
    leaves every other condition exactly where it was -- which is why the
    halt that fires here is the engine-wide one, naming no socket. Both gates
    are the supervisor's to close, and it closes them together; see
    ``test_the_engine_wide_condition_is_gated_on_the_same_expectation``.
    """
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)
    dog.record_message(at(1), socket=OPTION)
    dog.stop_expecting_feed(socket=OPTION)

    decision = dog.evaluate(at(10_000))

    assert dog.feed_expected(OPTION) is False
    assert decision is not None
    assert decision.inputs["socket"] is None


def test_the_engine_wide_condition_is_gated_on_the_same_expectation() -> None:
    """Per-socket gating alone would still halt overnight.

    Every quote refreshes the engine-wide clock as well as its socket's, so a
    process that gated only the named sockets went stale engine-wide ninety
    seconds after the last quote of the day -- the same nightly halt, under a
    different rule and with ``socket: None`` in the record.
    """
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)
    dog.expect_feed(at(0))
    dog.record_message(at(1), socket=OPTION)

    dog.stop_expecting_feed(socket=OPTION)
    dog.stop_expecting_feed()

    assert dog.evaluate(at(10_000)) is None
    assert dog.feed_expected() is False


def test_the_engine_wide_condition_still_fires_while_a_feed_is_expected() -> None:
    dog = watchdog()
    dog.expect_feed(at(0))
    dog.record_poll(at(1))

    decision = dog.evaluate(at(91))

    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE
    assert decision.inputs["socket"] is None


def test_an_expectation_begins_a_fresh_ninety_seconds() -> None:
    """The morning-after case, which per-socket gating alone gets wrong.

    Yesterday's last quote is seventeen hours old when the socket is opened
    again. Measured from that message the socket is stale the instant the
    session opens, so the engine halts at the bell -- before the new
    connection has had any chance to say anything.
    """
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)
    dog.expect_feed(at(0))
    dog.record_message(at(1), socket=OPTION)
    dog.stop_expecting_feed(socket=OPTION)
    dog.stop_expecting_feed()

    morning = 60_000.0
    dog.expect_feed(at(morning), socket=OPTION)
    dog.expect_feed(at(morning))

    assert dog.evaluate(at(morning + 1)) is None
    assert dog.evaluate(at(morning + 89)) is None
    decision = dog.evaluate(at(morning + 90))
    assert decision is not None
    assert decision.inputs["socket"] == OPTION


def test_expecting_a_feed_twice_does_not_move_the_floor() -> None:
    """A caller that re-expects on a schedule must not disarm the condition.

    The floor is an expectation's *start*. Refreshed on every tick of a
    five-second loop it would be a staleness condition that can never reach
    ninety seconds -- rule 9 off, with nothing on screen to say so. So a
    repeated expectation is a no-op rather than a new floor.
    """
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)
    dog.record_message(at(1), socket=OPTION)
    for tick in range(20):
        dog.expect_feed(at(tick * 5), socket=OPTION)

    assert dog.evaluate(at(91)) is not None


def test_one_socket_expectation_does_not_speak_for_another() -> None:
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)
    dog.expect_feed(at(0), socket=EQUITY)
    dog.record_message(at(1), socket=OPTION)
    dog.record_message(at(1), socket=EQUITY)

    dog.stop_expecting_feed(socket=EQUITY)

    decision = dog.evaluate(at(91))

    assert decision is not None
    assert decision.inputs["socket"] == OPTION


def test_a_close_is_reported_even_when_no_feed_is_expected() -> None:
    """The asymmetry. A drop at 15:59:50 is still a lost connection.

    Our *own* shutdown is not: ``VendorStream.begin_close`` sets the intent
    flag first and a close we asked for records nothing at all, so the only
    close that can reach here out of session is one the vendor or the network
    caused.
    """
    dog = watchdog()
    dog.stop_expecting_feed(socket=OPTION)
    dog.stop_expecting_feed()
    dog.record_stream_closed(at(0), socket=OPTION, detail="1006")

    decision = dog.evaluate(at(1))

    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    assert decision.inputs["socket"] == OPTION


def test_the_heartbeat_condition_is_not_gated_by_a_feed() -> None:
    """The risk manager is expected alive whenever the process is."""
    dog = watchdog(heartbeat_armed=True)
    dog.stop_expecting_feed(socket=OPTION)
    dog.stop_expecting_feed()

    decision = dog.evaluate(at(91))

    assert decision is not None
    assert decision.rule is HaltRule.HEARTBEAT_STALE


def test_a_feed_is_expected_until_something_says_otherwise() -> None:
    """The default is the strict reading: silence halts.

    A runtime built with no composition root around it -- which is every
    caller before this step -- keeps the behaviour it had. The gate opens the
    switch only where somebody has explicitly said a socket is not held open.
    """
    dog = watchdog()
    assert dog.feed_expected() is True
    assert dog.feed_expected(OPTION) is True
    dog.record_message(at(1), socket=OPTION)

    assert dog.evaluate(at(91)) is not None


# --------------------------------------------------------------------------
# A socket that dies before its first frame
# --------------------------------------------------------------------------


def test_a_socket_that_never_spoke_at_all_is_judged_from_its_expectation() -> None:
    """The expectation is the clock when there is no message to be the clock.

    A socket can fail *before* its first frame: the connection is accepted and
    the auth frame raises. ``VendorStream.run_session`` catches only
    ``SocketClosed``, so that escapes to ``SocketSupervisor._guard``, which
    logs it and deliberately leaves the expectation standing -- "so in session
    the watchdog halts the engine on it within ninety seconds".

    It did not. The socket had no liveness entry, because only a message, an
    open or a close creates one, and a socket with no entry was skipped before
    the expectation was ever consulted. The feed was never coming back and
    rule 9 reported healthy for the rest of the session.
    """
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)

    assert dog.evaluate(at(89)) is None
    decision = dog.evaluate(at(90))

    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE
    assert decision.inputs["socket"] == OPTION
    # The record says *nothing ever arrived*, rather than inventing a moment
    # the feed was last alive.
    assert decision.inputs["last_activity_at"] is None
    assert decision.inputs["last_activity_source"] == "none"


def test_expecting_a_socket_that_never_spoke_twice_does_not_move_its_floor() -> None:
    """The supervisor re-asserts every five seconds; the floor must not move.

    Same guard as ``test_expecting_a_feed_twice_does_not_move_the_floor``, on
    the path where the expectation *is* the clock -- which is where a refreshed
    floor would be a condition that can never reach ninety seconds.
    """
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)
    for tick in range(20):
        dog.expect_feed(at(tick * 5), socket=OPTION)

    assert dog.evaluate(at(90)) is not None


def test_a_socket_nobody_has_spoken_about_is_not_judged_from_nothing() -> None:
    """A feed with no clock *and* no stated expectation invents no halt.

    The default expectation is ``expected`` with no ``since`` -- deliberately
    not a floor, so a runtime with no composition root around it behaves
    exactly as it did before the gate. Measuring a socket nobody has mentioned
    from the process start would halt the engine for feeds this phase does not
    even open.
    """
    dog = watchdog()
    dog.record_message(at(1), socket=EQUITY)
    dog.stop_expecting_feed(socket=EQUITY)
    dog.stop_expecting_feed()

    assert dog.feed_expected(OPTION) is True
    assert dog.evaluate(at(10_000)) is None


def test_a_socket_that_never_spoke_and_is_not_expected_is_not_judged() -> None:
    """Out of session the gate still shuts, on this path as on the other."""
    dog = watchdog()
    dog.expect_feed(at(0), socket=OPTION)
    dog.stop_expecting_feed(socket=OPTION)
    dog.stop_expecting_feed()

    assert dog.evaluate(at(10_000)) is None


# --------------------------------------------------------------------------
# The order socket's handshake
# --------------------------------------------------------------------------


def test_a_socket_that_never_confirms_its_handshake_halts() -> None:
    """``trade_updates`` is exempt from silence, not from rule 9.

    A day with no fills is silent by definition, so a fill cannot be the
    detector -- but that leaves a socket which *connects and never completes
    the handshake* reporting nothing at all: no message, no close, no
    staleness. Fills then reach the account and never reach this process, with
    the switch reading healthy all session.

    So the handshake is what is expected, and unlike an absent fill an absent
    handshake is never legitimate.
    """
    dog = watchdog()
    dog.expect_handshake(at(0), socket=TRADE)

    assert dog.evaluate(at(89)) is None
    decision = dog.evaluate(at(90))

    assert decision is not None
    assert decision.rule is HaltRule.STREAM_UNCONFIRMED
    assert decision.inputs["socket"] == TRADE
    assert decision.inputs["expected_since"] == at(0).isoformat()


def test_a_confirmed_handshake_ends_the_condition() -> None:
    """Confirmed once is confirmed. The ordinary case must never halt."""
    dog = watchdog()
    dog.expect_handshake(at(0), socket=TRADE)
    dog.stop_expecting_handshake(socket=TRADE)

    assert dog.handshake_expected(TRADE) is False
    assert dog.evaluate(at(10_000)) is None


def test_expecting_a_handshake_twice_does_not_move_the_floor() -> None:
    """The supervisor re-asserts this on a five-second tick, like the others."""
    dog = watchdog()
    dog.expect_handshake(at(0), socket=TRADE)
    for tick in range(20):
        dog.expect_handshake(at(tick * 5), socket=TRADE)

    assert dog.evaluate(at(90)) is not None


def test_a_handshake_nobody_asked_about_is_not_judged() -> None:
    """The default is *not expected*, the opposite of the feed default.

    A feed defaults to expected because silence is measurable against the last
    message. A handshake has nothing to measure at all until somebody says
    when the socket started being held open, so an unstated one is not a
    condition -- it is an absence of one.
    """
    dog = watchdog()

    assert dog.handshake_expected(TRADE) is False
    assert dog.evaluate(at(10_000)) is None


def test_a_close_outranks_an_unconfirmed_handshake() -> None:
    """Both are true of a socket that dropped mid-handshake; the close is why."""
    dog = watchdog()
    dog.expect_handshake(at(0), socket=TRADE)
    dog.record_stream_closed(at(10), socket=TRADE, detail="1006")

    decision = dog.evaluate(at(100))

    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    assert decision.inputs["socket"] == TRADE
