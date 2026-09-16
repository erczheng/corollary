"""Rule 9, with a producer attached. The switch was proven; now it is armed.

``tests/engine/test_runtime.py`` proves the dead-man's switch by recording
activity **by hand**. Until step 8d part 2 nothing else called those
recorders, so the switch was a well-tested switch wired to nothing. These
tests close that gap: a real :class:`EngineRuntime` is handed to a real
``AlpacaQuoteStream`` as its activity recorder, and the halt is observed
through the database row a human would have to clear.

Every test here carries ``@pytest.mark.risk``, on the line
``test_runtime.py`` states: breaking one of these means the engine traded on
through a lost connection, or halted itself when nothing was wrong, or -- the
worst of the three -- resumed itself on reconnect.

Nothing here sleeps. The clock is injected and ninety seconds costs a
microsecond, which is the pattern ``test_runtime.py`` established and the
reason these tests are not marked slow and then skipped.
"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.data.providers.alpaca import AlpacaCredentials, AlpacaQuoteStream
from corollary.engine.execution.alpaca import AlpacaTradeUpdateStream
from corollary.db.models import Base
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine.runtime import (
    WATCHDOG_INTERVAL_SECONDS,
    EngineRuntime,
    HaltRule,
)
from corollary.engine.stream import (
    Stream,
    contract_unit,
    plan_subscriptions,
)
from corollary.sockets import (
    JSON_CODEC,
    MSGPACK_CODEC,
    SocketClosed,
    StreamActivityRecorder,
)
from tests.engine.test_runtime import Clock, SpyNotifier, read_state, resume_engine
from tests.sockets_support import T0, FakeConnect, FakeSocket, SpySleep

pytestmark = pytest.mark.risk

CONTRACT = "AAPL241220C00150000"
CONNECTED = [{"T": "success", "msg": "connected"}]
AUTHENTICATED = [{"T": "success", "msg": "authenticated"}]
SUBSCRIBED = [{"T": "subscription", "quotes": [CONTRACT], "trades": [], "bars": []}]
QUOTE = [
    {
        "T": "q",
        "S": CONTRACT,
        "bp": 1.24,
        "bs": 4,
        "ap": 1.34,
        "as": 5,
        "t": "2026-09-14T13:30:01Z",
    }
]

KEY = "AKFAKEKEYFORTESTSONLY000"
SECRET = "not-a-real-secret-and-not-forty-chars"


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "watchdog.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def notifier() -> SpyNotifier:
    return SpyNotifier()


@pytest.fixture
def runtime(
    db_engine: Engine, clock: Clock, notifier: SpyNotifier
) -> EngineRuntime:
    """A real runtime, on a real database, with a hand-wound clock.

    Resumed immediately, because a cold start is *already* halted -- rule 9's
    doing -- and a test that cannot tell a fresh halt from the cold-start one
    proves nothing about either.
    """
    resume_engine(db_engine)
    return EngineRuntime(
        session_factory=lambda: Session(db_engine),
        now=clock,
        notifier=notifier,
        env={},
        correlation_ids=lambda: "stream-watchdog-test",
    )


class AdvancingSleep:
    """The backoff as *elapsed time* rather than as a wait.

    ``SpySleep`` records a delay and returns instantly, which is right for
    asserting the schedule and wrong for asserting what a supervisor tick
    sees: the whole close-to-reopen window collapses to one instant and the
    close looks observable at a moment nothing would have observed it. This
    moves the clock by exactly the delay, so the reconnect lands where
    ``reconnect_delay`` puts it -- one second into a five-second period.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._clock.advance(seconds)


def client(
    frames: list[Any], *, runtime: EngineRuntime, clock: Clock
) -> tuple[AlpacaQuoteStream, FakeSocket]:
    stream, sockets = _client(
        [frames], runtime=runtime, clock=clock, sleep=SpySleep()
    )
    return stream, sockets[0]


def reconnecting_client(
    sessions: list[list[Any]], *, runtime: EngineRuntime, clock: Clock
) -> tuple[AlpacaQuoteStream, list[FakeSocket]]:
    """A client with one scripted socket per session, and a clock that moves.

    For the tests that drive :meth:`VendorStream.run` -- the reconnect loop
    itself -- rather than one session. The backoff advances the clock, so
    "between two ticks" is a claim about time and not about ordering.
    """
    return _client(sessions, runtime=runtime, clock=clock, sleep=AdvancingSleep(clock))


def _client(
    sessions: list[list[Any]],
    *,
    runtime: EngineRuntime,
    clock: Clock,
    sleep: Any,
) -> tuple[AlpacaQuoteStream, list[FakeSocket]]:
    sockets = [FakeSocket(frames, codec=MSGPACK_CODEC) for frames in sessions]
    stream = AlpacaQuoteStream(
        credentials=AlpacaCredentials(
            key_id=KEY,
            secret_key=SECRET,
            trading_base_url="https://paper",
            is_paper=True,
        ),
        url="wss://stream.data.alpaca.markets/v1beta1/indicative",
        codec=MSGPACK_CODEC,
        stream=Stream.OPTION,
        plan=plan_subscriptions(
            [contract_unit("pos-1", [CONTRACT])],
            at=T0,
            correlation_id="cid-1",
            cap=200,
            stream=Stream.OPTION,
        ),
        # The runtime *is* the recorder. No adapter, no spy: what this file
        # exists to prove is that the real one is reachable from the socket.
        activity=runtime,
        on_quote=lambda quote: None,
        connect=FakeConnect(*sockets),
        sleep=sleep,
        now=clock,
    )
    return stream, sockets


# --------------------------------------------------------------------------
# The wiring itself
# --------------------------------------------------------------------------


def test_the_runtime_is_a_socket_activity_recorder(runtime: EngineRuntime) -> None:
    """The protocol is satisfied structurally, and that is checked here.

    ``corollary/sockets.py`` declares a three-method protocol rather than
    importing the runtime, so that ``data/providers/`` does not depend on
    ``engine/``. Nothing enforces a structural match at runtime unless
    somebody asserts it, and a protocol nothing is checked against is a
    protocol that drifts.
    """
    assert isinstance(runtime, StreamActivityRecorder)


# --------------------------------------------------------------------------
# A stream close -- theirs, and ours
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_vendor_close_halts_the_engine(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock
) -> None:
    stream, _ = client(
        [CONNECTED, AUTHENTICATED, SUBSCRIBED, SocketClosed("1006 abnormal closure")],
        runtime=runtime,
        clock=clock,
    )
    with pytest.raises(SocketClosed):
        await stream.run_session()

    decision = runtime.check_watchdog()
    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    assert read_state(db_engine).halted is True


@pytest.mark.asyncio
async def test_our_own_close_does_not_halt_the_engine(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock
) -> None:
    """Shutdown is not a fault. The intent flag is what tells them apart.

    ``begin_close`` is called from inside the read loop here, which is the
    real shape of it: a lifespan shutdown sets the flag and the close arrives
    afterwards. Nothing about the close *itself* differs -- not the code, not
    the timing -- so the flag is the only thing that could distinguish them.
    """
    stream, socket = client(
        [CONNECTED, AUTHENTICATED, SUBSCRIBED], runtime=runtime, clock=clock
    )
    socket.push(lambda: stream.begin_close())
    await stream.run_session()

    assert runtime.check_watchdog() is None
    assert read_state(db_engine).halted is False


# --------------------------------------------------------------------------
# Ninety seconds of silence, and the boundary below it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ninety_seconds_after_the_last_streamed_quote_halts(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock
) -> None:
    """The stream's own messages are what the timeout is measured from."""
    stream, socket = client(
        [CONNECTED, AUTHENTICATED, SUBSCRIBED, QUOTE], runtime=runtime, clock=clock
    )
    socket.push(lambda: stream.begin_close())
    await stream.run_session()
    assert runtime.check_watchdog() is None

    clock.advance(91)
    decision = runtime.check_watchdog()
    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE
    assert read_state(db_engine).halted is True


@pytest.mark.asyncio
async def test_eighty_nine_seconds_after_a_streamed_quote_is_not_a_halt(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock
) -> None:
    """The permit at the boundary. A false halt stops the book trading."""
    stream, socket = client(
        [CONNECTED, AUTHENTICATED, SUBSCRIBED, QUOTE], runtime=runtime, clock=clock
    )
    socket.push(lambda: stream.begin_close())
    await stream.run_session()

    clock.advance(89)
    assert runtime.check_watchdog() is None
    assert read_state(db_engine).halted is False


# --------------------------------------------------------------------------
# The one that matters most
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reconnect_does_not_clear_a_halt(
    runtime: EngineRuntime,
    db_engine: Engine,
    clock: Clock,
    notifier: SpyNotifier,
) -> None:
    """Rule 9, verbatim: never auto-resume on reconnect.

    The feed dies, the socket comes back, quotes arrive again -- and the tick
    that follows halts the engine and keeps it halted, because reconnecting
    into an unverified position state is how a bot doubles a position it
    already holds.

    **There is no ``check_watchdog`` between the close and the reconnect, and
    that absence is the test.** This used to place one there, which supplied
    by hand the one step production does not take: the supervisor ticks every
    ``WATCHDOG_INTERVAL_SECONDS`` and the reconnect takes ``reconnect_delay(1)``
    -- one second -- so in the shipped app the reconnect has *already
    happened* by the time anything evaluates the close. A test that ticks in
    the gap proves the halt over a schedule nothing runs.
    """
    stream, sockets = reconnecting_client(
        [
            [CONNECTED, AUTHENTICATED, SUBSCRIBED, SocketClosed("1006")],
            [CONNECTED, AUTHENTICATED, SUBSCRIBED, QUOTE],
        ],
        runtime=runtime,
        clock=clock,
    )
    sockets[1].push(lambda: stream.begin_close())
    await stream.run()

    clock.advance(WATCHDOG_INTERVAL_SECONDS)
    decision = runtime.check_watchdog()
    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    halted_reason = read_state(db_engine).halted_reason
    assert halted_reason is not None

    # Several ticks on the socket that came back. The halt stands and the
    # fault is not re-announced: an ongoing halt with a stated reason is not
    # news, which is the storm `test_runtime.py` pins separately.
    for _ in range(3):
        clock.advance(WATCHDOG_INTERVAL_SECONDS)
        assert runtime.check_watchdog() is None

    state = read_state(db_engine)
    assert state.halted is True
    assert state.halted_reason == halted_reason
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_a_close_and_a_reconnect_between_two_ticks_still_halts(
    runtime: EngineRuntime,
    db_engine: Engine,
    clock: Clock,
    notifier: SpyNotifier,
) -> None:
    """The window the supervisor would otherwise never see into.

    A 1006 at 10:00:02 that reconnects at 10:00:03.2, between ticks at
    10:00:00 and 10:00:05. The close was recorded and the reopen used to
    *erase* it, so the tick at 10:00:05 found nothing wrong: no halt, no
    notification, no human resume -- for a connection demonstrably lost, with
    no replay on either socket, so every quote in the gap is simply gone.
    That is the common case rather than a rare one, which is what made it the
    finding that defeated the step: the switch this file exists to arm was
    firing only when a reconnect took longer than the supervisor period.

    Nothing here forces an evaluation. The socket is given no new power --
    ``StreamActivityRecorder`` still exposes three recorders and no way to
    drive the supervisor -- and the clock advances by exactly the backoff the
    reconnect really waits.
    """
    stream, sockets = reconnecting_client(
        [
            [CONNECTED, AUTHENTICATED, SUBSCRIBED, QUOTE, SocketClosed("1006 abnormal")],
            [CONNECTED, AUTHENTICATED, SUBSCRIBED, QUOTE],
        ],
        runtime=runtime,
        clock=clock,
    )
    sockets[1].push(lambda: stream.begin_close())

    # The tick before anything went wrong.
    assert runtime.check_watchdog() is None
    assert read_state(db_engine).halted is False

    await stream.run()

    # The close and the reopen both happened inside one supervisor period.
    assert clock.now == T0 + timedelta(seconds=1)
    assert clock.now < T0 + timedelta(seconds=WATCHDOG_INTERVAL_SECONDS)

    clock.advance(WATCHDOG_INTERVAL_SECONDS - 1)
    decision = runtime.check_watchdog()
    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    assert "1006 abnormal" in decision.reason
    # The sentence says the socket is back, because a critical alert over a
    # visibly healthy feed reads as a glitch otherwise.
    assert "reconnected" in decision.reason
    assert read_state(db_engine).halted is True
    assert len(notifier.sent) == 1

    # And exactly once: an ordinary reconnect is one close and one halt.
    for _ in range(3):
        clock.advance(WATCHDOG_INTERVAL_SECONDS)
        assert runtime.check_watchdog() is None
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_a_reconnect_after_a_repaired_halt_still_leaves_it_halted(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock
) -> None:
    """The repair path and the reconnect path do not interact.

    ``bea4190`` made the runtime repair an announced halt the row never took,
    bounded at twelve attempts. A reconnect must not be able to land between
    the announcement and the repair and leave the engine running -- so this
    drives the two in that order and reads the row.
    """
    first, _ = client(
        [CONNECTED, AUTHENTICATED, SUBSCRIBED, SocketClosed("1006")],
        runtime=runtime,
        clock=clock,
    )
    with pytest.raises(SocketClosed):
        await first.run_session()
    assert runtime.check_watchdog() is not None

    clock.advance(1)
    second, socket = client(
        [CONNECTED, AUTHENTICATED, SUBSCRIBED, QUOTE], runtime=runtime, clock=clock
    )
    socket.push(lambda: second.begin_close())
    await second.run_session()

    # Several healthy ticks, which is what the repair rides on.
    for _ in range(3):
        clock.advance(5)
        runtime.check_watchdog()

    assert read_state(db_engine).halted is True


# --------------------------------------------------------------------------
# Two sockets, two liveness clocks, one halt
# --------------------------------------------------------------------------
#
# Everything above drives one socket, which is what let the shared clock hide:
# with a single producer "is any socket alive" and "is every socket alive" give
# the same answer. These two attach a real `AlpacaTradeUpdateStream` as well,
# because the socket whose death used to be invisible is the order one.

AUTHORIZED = {
    "stream": "authorization",
    "data": {"status": "authorized", "action": "authenticate"},
}
LISTENING = {"stream": "listening", "data": {"streams": ["trade_updates"]}}


def order_client(
    frames: list[Any], *, runtime: EngineRuntime, clock: Clock
) -> tuple[AlpacaTradeUpdateStream, FakeSocket]:
    """The trading socket, on the same runtime as the quote socket."""
    socket = FakeSocket(frames, codec=JSON_CODEC)
    stream = AlpacaTradeUpdateStream(
        credentials=AlpacaCredentials(
            key_id=KEY,
            secret_key=SECRET,
            trading_base_url="https://paper-api.alpaca.markets",
            is_paper=True,
        ),
        activity=runtime,
        on_update=lambda update: None,
        connect=FakeConnect(socket),
        sleep=SpySleep(),
        now=clock,
    )
    return stream, socket


@pytest.mark.asyncio
async def test_a_silent_order_socket_halts_while_quotes_keep_flowing(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock, notifier: SpyNotifier
) -> None:
    """The measured defect, end to end, through the database row.

    ``trade_updates`` authorizes and then says nothing -- an escaped
    ``OverflowError``, an undocumented close code, a vendor that stops
    answering. The option stream quotes throughout. Under one shared liveness
    clock the watchdog answered ``None`` for as long as the quotes lasted, so
    the book stopped receiving fills with rule 9 believing the feed healthy:
    *"reconnecting into an unverified position state"* with no reconnect even
    attempted.
    """
    orders, order_socket = order_client(
        [AUTHORIZED, LISTENING], runtime=runtime, clock=clock
    )
    order_socket.push(lambda: orders.begin_close())
    await orders.run_session()
    # Our own close, so nothing is recorded: the socket is simply quiet from
    # here, which is the state that used to be invisible.
    assert runtime.check_watchdog() is None

    # Quotes keep arriving, well past the ninety seconds.
    for _ in range(4):
        clock.advance(30)
        quotes, quote_socket = client(
            [CONNECTED, AUTHENTICATED, SUBSCRIBED, QUOTE],
            runtime=runtime,
            clock=clock,
        )
        quote_socket.push(lambda: quotes.begin_close())
        await quotes.run_session()

    decision = runtime.check_watchdog()
    assert decision is not None
    assert decision.rule is HaltRule.CONNECTION_STALE
    assert decision.inputs["socket"] == "trade_updates"

    state = read_state(db_engine)
    assert state.halted is True
    # The halt names the feed that was lost. A reader of this row has to be
    # able to tell losing fills from losing quotes.
    assert state.halted_reason is not None
    assert "trade_updates" in state.halted_reason
    assert len(notifier.sent) == 1
    assert "trade_updates" in notifier.sent[0].body


@pytest.mark.asyncio
async def test_a_reopen_on_the_quote_socket_does_not_report_the_order_socket_back(
    runtime: EngineRuntime, db_engine: Engine, clock: Clock
) -> None:
    """``_close_reopened`` attributed one socket's reopen to another's close.

    The order socket drops; the quote socket authenticates a fraction of a
    second later. The alert said *"has since reconnected"* -- about a feed that
    was still down and would stay down.
    """
    orders, _ = order_client(
        [AUTHORIZED, LISTENING, SocketClosed("1006 abnormal closure")],
        runtime=runtime,
        clock=clock,
    )
    with pytest.raises(SocketClosed):
        await orders.run_session()

    clock.advance(0.2)
    quotes, quote_socket = client(
        [CONNECTED, AUTHENTICATED, SUBSCRIBED], runtime=runtime, clock=clock
    )
    quote_socket.push(lambda: quotes.begin_close())
    await quotes.run_session()

    decision = runtime.check_watchdog()
    assert decision is not None
    assert decision.rule is HaltRule.STREAM_CLOSED
    assert decision.inputs["socket"] == "trade_updates"
    assert decision.inputs["reconnected"] is False
    assert "has since reconnected" not in decision.reason
    assert read_state(db_engine).halted is True


@pytest.mark.asyncio
async def test_each_client_reports_under_its_own_name(
    runtime: EngineRuntime, clock: Clock
) -> None:
    """The names the halt reason will use, as the clients actually report them.

    Written out rather than derived, because renaming one of these silently
    renames what an operator reads in ``halted_reason`` and in a log query.
    """
    quotes, _ = client(
        [CONNECTED, AUTHENTICATED, SUBSCRIBED], runtime=runtime, clock=clock
    )
    orders, _ = order_client([AUTHORIZED, LISTENING], runtime=runtime, clock=clock)
    assert quotes.name == "option_quotes"
    assert orders.name == "trade_updates"
