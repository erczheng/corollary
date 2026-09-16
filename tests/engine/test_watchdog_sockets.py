"""Rule 9 per socket: three feeds, three liveness clocks, one halt.

``tests/engine/test_runtime.py`` proves the switch with a single unnamed
source, which is what it had. One ``Watchdog`` served all three sockets, so
``record_message`` from the stock stream refreshed the last-activity clock for
**all** of them and ``CONNECTION_STALE`` asked *"is any socket alive"*.
Measured on the shipped code:

    A silent 400s, B chatty            -> evaluate: None
    A closes, B authenticates 0.2s on  -> reconnected: True

Both answers are wrong in the same direction, and the socket it is worst on is
``trade_updates``: it can die -- an escaped ``OverflowError``, an undocumented
close code falling through to a warning -- and the engine never halts while
quotes keep flowing. The book stops receiving fills with rule 9 believing the
feed healthy, which is *"reconnecting into an unverified position state"* with
no reconnect even attempted.

So the tracking splits and the decision does not: the ``Watchdog`` holds
last-activity and an unreported close **per socket** and halts if *any* of
them goes ninety seconds silent. Rule 9's halt is engine-wide, so there is
still exactly one halt path.

Every test here is ``risk``-marked, on the line ``test_runtime.py`` states:
breaking one means the engine traded on through a feed it had lost.
"""

from datetime import timedelta

import pytest

from corollary.engine.runtime import (
    WATCHDOG_TIMEOUT_SECONDS,
    HaltRule,
    Watchdog,
)
from tests.engine.test_runtime import T0

pytestmark = pytest.mark.risk

#: The three real sockets, by the names their clients report themselves
#: under. Written out rather than imported so that renaming one of them
#: fails a test instead of quietly renaming what the halt reason says.
QUOTES = "option_quotes"
STOCKS = "equity_quotes"
TRADES = "trade_updates"


def seconds(count: float) -> timedelta:
    return timedelta(seconds=count)


# --------------------------------------------------------------------------
# One socket's evidence of life is not another's
# --------------------------------------------------------------------------


def test_a_chatty_socket_does_not_refresh_a_silent_one() -> None:
    """The measured defect, as a test. This is the whole of fix B.

    The option stream quotes all morning; the order socket said nothing after
    the first message. Under one shared clock the watchdog answered ``None``
    at four hundred seconds, which is the engine trading on with no idea
    whether its fills are arriving.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=TRADES)
    for at in range(10, 401, 10):
        watchdog.record_message(T0 + seconds(at), socket=QUOTES)

    decision = watchdog.evaluate(T0 + seconds(400))
    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE
    assert decision.inputs["socket"] == TRADES
    assert decision.inputs["elapsed_seconds"] == 400.0


def test_the_halt_reason_names_the_socket_that_went_silent() -> None:
    """A human reading the record has to know *which* feed they lost.

    Losing quotes and losing fills have very different consequences, and
    ``halted_reason`` is one sentence wide -- so the sentence has to carry the
    socket. Rule 8's standard: the rule, the inputs, the timestamp.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=TRADES)
    watchdog.record_message(T0 + seconds(120), socket=QUOTES)

    decision = watchdog.evaluate(T0 + seconds(120))
    assert decision is not None
    assert TRADES in decision.reason
    assert QUOTES not in decision.reason


def test_a_poll_does_not_refresh_a_socket_either() -> None:
    """A successful REST poll is weaker evidence than another socket's frames.

    It says the data host answers, which is not a claim about this socket at
    all. It still counts for the engine-wide condition below -- the spec says
    *message or poll* -- and it no longer rescues a feed that is gone.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=TRADES)
    for at in range(10, 401, 10):
        watchdog.record_poll(T0 + seconds(at))

    decision = watchdog.evaluate(T0 + seconds(400))
    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE
    assert decision.inputs["socket"] == TRADES


def test_two_speaking_sockets_are_not_a_halt_at_the_boundary() -> None:
    """The permit, at the boundary, for every tracked socket.

    A false halt stops the book trading, so the per-socket condition has to
    hold at eighty-nine seconds exactly as the engine-wide one does.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=QUOTES)
    watchdog.record_message(T0, socket=TRADES)

    assert watchdog.evaluate(T0 + seconds(WATCHDOG_TIMEOUT_SECONDS - 1)) is None
    assert watchdog.evaluate(T0 + seconds(WATCHDOG_TIMEOUT_SECONDS)) is not None


def test_the_stalest_socket_is_the_one_reported() -> None:
    """Two dead feeds, one halt, and it names the one that died first.

    Deterministic on purpose: the decision is a record somebody reads, and a
    reason that varies by dict ordering is a record two people disagree about.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=TRADES)
    watchdog.record_message(T0 + seconds(30), socket=QUOTES)
    watchdog.record_message(T0 + seconds(60), socket=STOCKS)

    decision = watchdog.evaluate(T0 + seconds(200))
    assert decision is not None
    assert decision.inputs["socket"] == TRADES


def test_a_socket_that_comes_back_ends_its_own_staleness() -> None:
    """Nothing latches here, per socket as well as overall."""
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=TRADES)
    watchdog.record_message(T0 + seconds(400), socket=QUOTES)
    assert watchdog.evaluate(T0 + seconds(400)) is not None

    watchdog.record_message(T0 + seconds(401), socket=TRADES)
    assert watchdog.evaluate(T0 + seconds(401)) is None


def test_the_engine_wide_condition_still_answers_for_an_unnamed_source() -> None:
    """A poll-only engine still halts, and its reason names no socket.

    The REST half of rule 9 predates any socket and is not per-socket: the
    opening snapshot and the poll loop are the only evidence of life a process
    has before a stream is wired, so *"nothing has come through at all"* stays
    a condition in its own right.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_poll(T0)

    assert watchdog.evaluate(T0 + seconds(89)) is None
    decision = watchdog.evaluate(T0 + seconds(90))
    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE
    assert decision.inputs.get("socket") is None


# --------------------------------------------------------------------------
# A close belongs to the socket that closed
# --------------------------------------------------------------------------


def test_a_reopen_on_one_socket_does_not_mark_anothers_close_reconnected() -> None:
    """``_close_reopened`` attributed one socket's reopen to another's close.

    The alert said *"has since reconnected"* about a socket that was still
    down. Same root cause as the shared liveness clock, and worse to read: it
    tells the operator the feed is back when it is not.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=QUOTES)
    watchdog.record_message(T0, socket=TRADES)
    watchdog.record_stream_closed(T0 + seconds(1), socket=TRADES, detail="1006")
    watchdog.record_stream_open(T0 + seconds(1.2), socket=QUOTES)

    decision = watchdog.evaluate(T0 + seconds(5))
    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    assert decision.inputs["socket"] == TRADES
    assert decision.inputs["reconnected"] is False
    assert TRADES in decision.reason
    assert "has since reconnected" not in decision.reason


def test_a_sockets_own_reopen_still_ends_its_close_on_the_observation() -> None:
    """The close-observation invariant, held per socket.

    No close is ever forgotten without having been reported at least once:
    the reopen marks it recovered, the first ``evaluate`` reports it, and that
    observation -- not the reopen -- is what clears it.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=TRADES)
    watchdog.record_stream_closed(T0 + seconds(1), socket=TRADES, detail="1006")
    watchdog.record_stream_open(T0 + seconds(2), socket=TRADES)

    decision = watchdog.evaluate(T0 + seconds(5))
    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    assert decision.inputs["reconnected"] is True
    assert "has since reconnected" in decision.reason
    # Observed once, and one close is one halt.
    assert watchdog.evaluate(T0 + seconds(10)) is None


def test_two_closed_sockets_are_both_reported_before_either_is_forgotten() -> None:
    """One decision per tick, and neither close is dropped.

    A shared close slot meant the second drop *replaced* the first, so a
    double outage was reported once and attributed to whichever socket
    happened to be last.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=QUOTES)
    watchdog.record_message(T0, socket=TRADES)
    watchdog.record_stream_closed(T0 + seconds(1), socket=TRADES, detail="1006 fills")
    watchdog.record_stream_closed(T0 + seconds(2), socket=QUOTES, detail="1006 quotes")
    watchdog.record_stream_open(T0 + seconds(3), socket=TRADES)
    watchdog.record_stream_open(T0 + seconds(3), socket=QUOTES)

    first = watchdog.evaluate(T0 + seconds(5))
    assert first is not None
    assert first.inputs["socket"] == TRADES
    second = watchdog.evaluate(T0 + seconds(6))
    assert second is not None
    assert second.inputs["socket"] == QUOTES
    assert watchdog.evaluate(T0 + seconds(7)) is None


def test_a_socket_still_shut_keeps_being_reported() -> None:
    """No latch, and no forgetting either. The fault is present until it is not."""
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0, socket=TRADES)
    watchdog.record_stream_closed(T0 + seconds(1), socket=TRADES, detail="1006")

    for at in (5, 10, 300):
        decision = watchdog.evaluate(T0 + seconds(at))
        assert decision is not None, at
        assert decision.rule is HaltRule.STREAM_CLOSED
        assert decision.inputs["socket"] == TRADES


def test_an_unnamed_close_is_still_reported_and_names_no_socket() -> None:
    """The unnamed source keeps exactly the behaviour it had.

    Every real socket names itself -- ``VendorStream`` requires it -- so this
    covers a hand-recorded close and the tests that predate the split.
    """
    watchdog = Watchdog(started_at=T0)
    watchdog.record_message(T0)
    watchdog.record_stream_closed(T0 + seconds(1), detail="1006")

    decision = watchdog.evaluate(T0 + seconds(2))
    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    assert decision.inputs["socket"] is None
    assert "market data stream closed" in decision.reason
