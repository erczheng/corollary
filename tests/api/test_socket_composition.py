"""The composition root: who runs the three vendor sockets, and when.

``corollary/engine/sockets.py`` owns the sockets' lifetime and
``corollary/api/app.py`` composes it -- the fan-out's sinks, the Paper book,
and the one :class:`EngineRuntime` whose watchdog those sockets arm. Nothing
here binds a port: every connection is a scripted double over
``corollary.sockets.VendorSocket``, the four-method protocol that exists for
exactly this.

What these tests are really about is the two halves of rule 9 that only the
composition root can get wrong:

**Out of session nothing is held open, so nothing goes stale.** Per-socket
staleness halts a socket ninety seconds silent, and the option socket is
silent from the closing bell to the opening one. Held to the in-session
standard the engine halts itself every single evening, and a halt log full of
nightly false positives is a halt log nobody reads on the morning that
matters.

**In session a silent socket still halts.** Which is the condition, and
gating it out of session must not disarm it.

The order socket is the exception and it is stated rather than implied:
``trade_updates`` is silent on any day with no fills, which is most days, so
its staleness is never expected. Losing it is detected by its *close*, which
the transport reports either way.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from corollary.api.app import (
    app as shipped_app,
    build_socket_supervisor,
    create_app,
    dev_app,
    no_socket_supervisor,
)
from corollary.api.deps import AccountMode, ServiceRegistry
from corollary.api.fanout import Fanout, quote_sink, trade_update_sink
from corollary.engine.state import engine_state
from corollary.data.providers.alpaca import (
    ALPACA_OPTIONS_FEED_ENV,
    ALPACA_PAPER_KEY_ENV,
    ALPACA_PAPER_SECRET_ENV,
    ALPACA_STOCK_FEED_HISTORICAL_ENV,
    ALPACA_STOCK_FEED_REALTIME_ENV,
)
from corollary.engine.execution.interface import BrokerPosition
from corollary.engine.runtime import (
    WATCHDOG_TIMEOUT_SECONDS,
    EngineRuntime,
    HaltRule,
    MarketsVisibleStatus,
)
from corollary.engine.sockets import (
    EQUITY_SOCKET,
    OPTION_SOCKET,
    TRADE_SOCKET,
    SocketSupervisor,
)
from corollary.engine.stream import EQUITY_STREAM_SYMBOL_CAP
from corollary.sockets import Codec, SocketClosed, VendorSocket
from tests.api.conftest import RecordedBroker

# A session on a Monday, and an instant inside it: 10:00 ET.
SESSION_DAY = date(2026, 9, 14)
IN_SESSION = datetime(2026, 9, 14, 14, 0, 0, tzinfo=timezone.utc)
OPENS = datetime(2026, 9, 14, 13, 30, 0, tzinfo=timezone.utc)
CLOSES = datetime(2026, 9, 14, 20, 0, 0, tzinfo=timezone.utc)

#: Obviously fake, and paper. Rule 6: no key material in tests or fixtures.
#: Rule 5: nothing here builds a live credential.
#: The feed names are the Basic plan's, per CLAUDE.md. A test may name them
#: because it is standing in for the environment, which is the one other
#: place they are allowed to appear -- and they have no defaults on purpose,
#: since defaulting historical equity to IEX measures volume on a fortieth of
#: the market.
FAKE_ENV: Mapping[str, str] = {
    ALPACA_PAPER_KEY_ENV: "PKTESTTESTTESTTEST",
    ALPACA_PAPER_SECRET_ENV: "not-a-real-secret",
    ALPACA_OPTIONS_FEED_ENV: "indicative",
    ALPACA_STOCK_FEED_HISTORICAL_ENV: "sip",
    ALPACA_STOCK_FEED_REALTIME_ENV: "iex",
}

ALL_SOCKETS = (TRADE_SOCKET, OPTION_SOCKET, EQUITY_SOCKET)

#: What each socket says on the way up. Enough to authenticate, which is what
#: makes the client record a message and a stream open; after that each
#: scripted socket goes quiet, because a quiet socket is the state rule 9 is
#: about.
QUOTE_FRAMES: tuple[Any, ...] = (
    [{"T": "success", "msg": "connected"}],
    [{"T": "success", "msg": "authenticated"}],
)
TRADE_FRAMES: tuple[Any, ...] = (
    {"stream": "authorization", "data": {"status": "authorized"}},
    {"stream": "listening", "data": {"streams": ["trade_updates"]}},
)


class Clock:
    """A hand-wound UTC clock. Nothing in this file reads a real one."""

    def __init__(self, start: datetime = IN_SESSION) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class ScriptedSocket:
    """One connection that says its lines and then goes quiet.

    ``FakeSocket`` in ``tests/sockets_support.py`` treats the end of its
    script as a 1006, which is the right default for a client test and the
    wrong one here: a socket that hangs up is a *fault*, and most of what
    this file needs to say is about a socket that is healthy and has nothing
    to report.

    ``unblocks_on_close`` is the difference between the two shutdown cases.
    True is what a real ``websockets`` connection does -- closing it makes the
    pending ``recv()`` raise -- and False is the socket that has stopped
    answering entirely, which no ``aclose()`` can reach and only cancelling
    the task can end.
    """

    def __init__(
        self,
        frames: tuple[Any, ...],
        *,
        codec: Codec,
        unblocks_on_close: bool = True,
    ) -> None:
        self._frames = list(frames)
        self._codec = codec
        self._unblocks_on_close = unblocks_on_close
        self._released = asyncio.Event()
        self.sent: list[Any] = []
        self.closed = False
        self.reads = 0

    async def send_text(self, text: str) -> None:
        self.sent.append(self._codec.decode(text))

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(self._codec.decode(payload))

    async def recv(self) -> str | bytes:
        self.reads += 1
        if self._frames:
            return self._codec.encode(self._frames.pop(0))
        await self._released.wait()
        raise SocketClosed("the test released the read")

    async def close(self) -> None:
        self.closed = True
        if self._unblocks_on_close:
            self._released.set()

    def push(self, *frames: Any) -> None:
        self._frames.extend(frames)
        self._released.set()


class DroppingSocket:
    """A connection that drops the moment it is read, forever."""

    def __init__(self, *, detail: str = "1006 (connection closed abnormally)") -> None:
        self._detail = detail
        self.closed = False

    async def send_text(self, text: str) -> None:
        return None

    async def send_bytes(self, payload: bytes) -> None:
        return None

    async def recv(self) -> str | bytes:
        raise SocketClosed(self._detail)

    async def close(self) -> None:
        self.closed = True


class ErroringSocket:
    """A connection that raises something other than a close on its first read.

    ``VendorStream.run_session`` catches only :class:`SocketClosed`, so this
    escapes the reconnect loop entirely and lands in
    ``SocketSupervisor._guard`` -- which is the shape of a refusal that would
    recur identically on every reconnect, and the one case where a socket is
    gone for the rest of the session. Nothing is ever recorded against it: no
    message, no open, no close.
    """

    def __init__(self, *, detail: str = "the vendor sent something unreadable") -> None:
        self._detail = detail
        self.closed = False
        self.reads = 0

    async def send_text(self, text: str) -> None:
        return None

    async def send_bytes(self, payload: bytes) -> None:
        return None

    async def recv(self) -> str | bytes:
        self.reads += 1
        raise RuntimeError(self._detail)

    async def close(self) -> None:
        self.closed = True


class ScriptedConnect:
    """Hands out sockets **by URL**, not in call order.

    Three tasks start in one tick and their connects interleave however the
    loop feels like scheduling them, so a queue would make every assertion
    here depend on that order. The URL says which socket is asking: the two
    market-data streams share a host and differ in their version segment --
    ``/v1beta1/{feed}`` is options and ``/v2/{feed}`` is equities -- and the
    order stream is the one on the trading host.
    """

    def __init__(self, sockets: Mapping[str, Any], *, refuse: bool = False) -> None:
        self._sockets = dict(sockets)
        self._refuse = refuse
        self.urls: list[str] = []
        self.attempts: dict[str, int] = {}

    def _name_for(self, url: str) -> str:
        if "/v1beta1/" in url:
            return OPTION_SOCKET
        if "/v2/" in url:
            return EQUITY_SOCKET
        return TRADE_SOCKET

    async def __call__(self, url: str, headers: Any) -> VendorSocket:
        self.urls.append(url)
        name = self._name_for(url)
        self.attempts[name] = self.attempts.get(name, 0) + 1
        if self._refuse:
            raise SocketClosed("the test refused the connection")
        socket = self._sockets.get(name)
        if socket is None:
            raise SocketClosed(f"no scripted socket for {name}")
        return socket  # type: ignore[no-any-return]


async def pump(times: int = 50) -> None:
    """Give the socket tasks a chance to run. No wall-clock waiting."""
    for _ in range(times):
        await asyncio.sleep(0)


async def settle(predicate: Callable[[], bool], *, what: str) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"never settled: {what}")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def runtime(db_engine: Engine, clock: Clock) -> EngineRuntime:
    engine = EngineRuntime(
        session_factory=lambda: Session(db_engine),
        now=clock,
        env={},
    )
    engine.start()
    return engine


@pytest.fixture
def scripted() -> ScriptedConnect:
    return ScriptedConnect(
        {
            OPTION_SOCKET: ScriptedSocket(QUOTE_FRAMES, codec=_option_codec()),
            EQUITY_SOCKET: ScriptedSocket(QUOTE_FRAMES, codec=_stock_codec()),
            TRADE_SOCKET: ScriptedSocket(TRADE_FRAMES, codec=_stock_codec()),
        }
    )


def _option_codec() -> Codec:
    from corollary.sockets import MSGPACK_CODEC

    return MSGPACK_CODEC


def _stock_codec() -> Codec:
    from corollary.sockets import JSON_CODEC

    return JSON_CODEC


@pytest.fixture
def make_supervisor(
    runtime: EngineRuntime,
    paper_broker: RecordedBroker,
    clock: Clock,
    scripted: ScriptedConnect,
) -> Callable[..., SocketSupervisor]:
    """Build a supervisor over the scripted sockets.

    Every test closes the supervisor it built -- in a ``finally`` where it
    asserts afterwards, directly where the shutdown *is* the assertion. There
    is no teardown here on purpose: a fixture that quietly closed a leaked
    supervisor would hide the one failure mode this file is about.
    """

    def build(
        *,
        broker: Any = None,
        connect: Any = None,
        session_open: Any = None,
        session_close: Any = None,
        stream_sleep: Any = None,
    ) -> SocketSupervisor:
        supervisor = SocketSupervisor(
            runtime=runtime,
            broker=(lambda: broker or paper_broker),
            on_quote=quote_sink(Fanout()),
            on_update=trade_update_sink(Fanout()),
            env=FAKE_ENV,
            now=clock,
            connect=connect or scripted,
            stream_sleep=stream_sleep,
            session_open=session_open or (lambda day: OPENS if day == SESSION_DAY else None),
            session_close=session_close
            or (lambda day: CLOSES if day == SESSION_DAY else None),
        )
        return supervisor

    return build


class EmptyBook(RecordedBroker):
    """A configured account holding nothing. The shipped state of a new install."""

    async def positions(self) -> list[BrokerPosition]:
        self._record("positions")
        return []


# --------------------------------------------------------------------------
# In session
# --------------------------------------------------------------------------


@pytest.mark.risk
@pytest.mark.asyncio
async def test_in_session_all_three_sockets_are_held_and_the_watchdog_hears_them(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    scripted: ScriptedConnect,
) -> None:
    """Rule 9's producers, running. Before this step nothing started them."""
    supervisor = make_supervisor()
    try:
        await supervisor.tick()
        await settle(
            lambda: len(scripted.attempts) == 3, what="all three sockets connected"
        )
        await settle(
            lambda: runtime.watchdog.last_activity_at is not None,
            what="the watchdog heard a message",
        )

        assert set(supervisor.held) == set(ALL_SOCKETS)
        # The option socket is planned against the option budget and the
        # stock socket against the equity one -- the plan carries its stream,
        # so a plan on the wrong socket is refused rather than subscribed.
        plans = supervisor.plans
        assert plans is not None
        assert plans.option.subscribed
        assert plans.equity.subscribed
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_in_session_a_silent_quote_socket_still_halts(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
    db_engine: Engine,
) -> None:
    """The condition. Ninety seconds of silence on a held socket halts."""
    supervisor = make_supervisor()
    try:
        await supervisor.tick()
        await settle(
            lambda: runtime.watchdog.last_activity_at is not None,
            what="the watchdog heard a message",
        )
        # The tick that consults the order socket's handshake, which the loop
        # does every five seconds and which a test has to do for itself. Its
        # condition is the more specific one, so an unconfirmed order socket
        # would answer here instead of the silence this test is about.
        await supervisor.tick()

        clock.advance(91)
        decision = runtime.check_watchdog()

        assert decision is not None
        assert decision.rule is HaltRule.CONNECTION_STALE
        assert decision.inputs["socket"] in {OPTION_SOCKET, EQUITY_SOCKET}
        with Session(db_engine) as session:
            assert engine_state(session).halted is True
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_day_with_no_fills_does_not_halt_on_the_order_socket(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
) -> None:
    """``trade_updates`` is silent on most days, and silence is not a fault.

    An empty book subscribes no quotes either, so this is also the state a
    fresh paper account boots into: one socket open, nothing expected to
    tick, and no halt.
    """
    supervisor = make_supervisor(broker=EmptyBook(label="empty"))
    try:
        await supervisor.tick()
        await settle(
            lambda: runtime.watchdog.last_activity_at is not None,
            what="the order socket authorized",
        )
        assert supervisor.held == (TRADE_SOCKET,)
        # The tick that sees the acknowledgement and withdraws the handshake
        # expectation. What is exempt is *silence*; a socket that never
        # confirmed is a fault, and the two are told apart here.
        await supervisor.tick()

        clock.advance(10_000)

        assert runtime.check_watchdog() is None
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_reconnect_does_not_clear_a_halt(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
    db_engine: Engine,
) -> None:
    """Rule 9, verbatim: never auto-resume on reconnect.

    The socket drops, the engine halts, the socket comes back on its own --
    and the halt is exactly where it was. Reconnecting into an unverified
    position state is how a bot doubles a position it already holds.
    """
    healthy = ScriptedSocket(QUOTE_FRAMES, codec=_stock_codec())
    connect = ScriptedConnect(
        {
            OPTION_SOCKET: ScriptedSocket(QUOTE_FRAMES, codec=_option_codec()),
            EQUITY_SOCKET: healthy,
            TRADE_SOCKET: DroppingSocket(detail="1006"),
        }
    )
    supervisor = make_supervisor(
        connect=connect, stream_sleep=lambda seconds: asyncio.sleep(0)
    )
    try:
        await supervisor.tick()
        await settle(
            lambda: connect.attempts.get(TRADE_SOCKET, 0) >= 2,
            what="the order socket dropped and reconnected",
        )

        decision = runtime.check_watchdog()
        assert decision is not None
        assert decision.rule is HaltRule.STREAM_CLOSED
        assert decision.inputs["socket"] == TRADE_SOCKET

        # It keeps reconnecting, and the halt keeps standing.
        await settle(
            lambda: connect.attempts.get(TRADE_SOCKET, 0) >= 4,
            what="the order socket reconnected again",
        )
        clock.advance(5)
        runtime.check_watchdog()

        with Session(db_engine) as session:
            state = engine_state(session)
        assert state.halted is True
        assert state.halted_reason is not None
        assert TRADE_SOCKET in state.halted_reason
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_socket_that_dies_before_its_first_frame_still_halts(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
    db_engine: Engine,
) -> None:
    """``_guard``'s promise, which the watchdog was not keeping.

    ``_guard`` logs a socket that stopped and deliberately leaves its
    expectation standing -- *"so in session the watchdog halts the engine on it
    within ninety seconds"*. It did not. A liveness entry is created only by a
    message, an open or a close, and a socket that fails on its **first frame**
    has none of the three: the watchdog skipped it before the expectation was
    ever consulted, and the feed was never coming back.

    The equity socket keeps quoting throughout, so the halt this asserts is
    the one named socket that has nothing to say for itself rather than a
    process-wide silence.
    """
    doomed = ErroringSocket()
    connect = ScriptedConnect(
        {
            OPTION_SOCKET: doomed,
            EQUITY_SOCKET: ScriptedSocket(QUOTE_FRAMES, codec=_stock_codec()),
            TRADE_SOCKET: ScriptedSocket(TRADE_FRAMES, codec=_stock_codec()),
        }
    )
    supervisor = make_supervisor(connect=connect)
    try:
        await supervisor.tick()
        await settle(
            lambda: doomed.closed, what="the option socket escaped to the guard"
        )
        # Still expected, which is the whole point: the socket is held, the
        # subscription was made, and nothing withdrew anything.
        assert runtime.watchdog.feed_expected(OPTION_SOCKET) is True
        # A second tick, so the order socket's handshake is confirmed and
        # withdrawn -- otherwise its condition, which is the more specific
        # one, would be the answer instead.
        await supervisor.tick()

        clock.advance(91)
        # The equity socket is alive and quoting. Only the option socket is
        # silent, and it has never once spoken.
        runtime.record_message(clock.now, socket=EQUITY_SOCKET)
        decision = runtime.check_watchdog()

        assert decision is not None
        assert decision.rule is HaltRule.CONNECTION_STALE
        assert decision.inputs["socket"] == OPTION_SOCKET
        assert decision.inputs["last_activity_at"] is None
        with Session(db_engine) as session:
            state = engine_state(session)
        assert state.halted is True
        assert state.halted_reason is not None
        assert OPTION_SOCKET in state.halted_reason
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_an_order_socket_whose_handshake_is_never_answered_halts(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
    db_engine: Engine,
) -> None:
    """The gap the fills exemption left open, which is not a silent day.

    ``trade_updates`` is exempt from staleness because a day with no fills is
    silent by definition, and its *close* is what detects losing it. But a
    close covers a socket that **drops**, not one that **never comes up**: the
    connection is accepted, the auth frame goes out, and nothing answers. That
    records nothing, raises nothing and drops nothing -- no close, no
    staleness, no timer -- so fills reach the account and never reach this
    process, all session, with the switch reading healthy.

    An empty book subscribes no quotes, so this is the fresh paper account as
    well as the failure: one socket, held open, confirming nothing.
    """
    mute = ScriptedSocket((), codec=_stock_codec())
    supervisor = make_supervisor(
        broker=EmptyBook(label="empty"), connect=ScriptedConnect({TRADE_SOCKET: mute})
    )
    try:
        await supervisor.tick()
        await settle(lambda: mute.reads >= 1, what="the order socket read a frame")
        assert supervisor.held == (TRADE_SOCKET,)
        # We asked. Nothing answered, and nothing ever will.
        assert [str(frame.get("action")) for frame in mute.sent] == ["auth"]
        # A second tick: the handshake is still unconfirmed, and re-asserting
        # an expectation must not move the floor it is measured from.
        await supervisor.tick()

        clock.advance(91)
        decision = runtime.check_watchdog()

        assert decision is not None
        assert decision.rule is HaltRule.STREAM_UNCONFIRMED
        assert decision.inputs["socket"] == TRADE_SOCKET
        with Session(db_engine) as session:
            state = engine_state(session)
        assert state.halted is True
        assert state.halted_reason is not None
        assert TRADE_SOCKET in state.halted_reason
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_confirmed_order_socket_is_not_judged_for_its_silence(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
) -> None:
    """The other side of the boundary: a working socket with nothing to say.

    The handshake is confirmed, so the expectation is withdrawn and the day's
    silence proves nothing -- which is the exemption, unchanged. What the
    supervisor consults is the client's own ``listening`` flag, set only by an
    acknowledgement naming ``trade_updates``.
    """
    supervisor = make_supervisor(broker=EmptyBook(label="empty"))
    try:
        await supervisor.tick()
        await settle(
            lambda: runtime.watchdog.last_activity_at is not None,
            what="the order socket authorized",
        )
        assert runtime.watchdog.handshake_expected(TRADE_SOCKET) is True

        await supervisor.tick()

        assert runtime.watchdog.handshake_expected(TRADE_SOCKET) is False
        clock.advance(10_000)
        assert runtime.check_watchdog() is None
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_out_of_session_no_socket_owes_a_handshake(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
) -> None:
    """Nothing held open owes anything. Same gate, the other condition.

    Left armed at the bell this would be the nightly halt again, arriving
    under the one rule the session gate did not cover.
    """
    supervisor = make_supervisor()
    try:
        await supervisor.tick()
        await settle(
            lambda: runtime.watchdog.handshake_expected(TRADE_SOCKET), what="armed"
        )

        clock.now = CLOSES
        await supervisor.tick()

        assert supervisor.held == ()
        assert runtime.watchdog.handshake_expected(TRADE_SOCKET) is False
        clock.advance(60_000)
        assert runtime.check_watchdog() is None
    finally:
        await supervisor.aclose()


# --------------------------------------------------------------------------
# Out of session
# --------------------------------------------------------------------------


@pytest.mark.risk
@pytest.mark.asyncio
async def test_out_of_session_nothing_is_held_and_no_staleness_halt_fires(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
    scripted: ScriptedConnect,
    db_engine: Engine,
) -> None:
    """The nightly false positive this gate exists to prevent.

    A full session runs, the bell goes, and the engine sits there overnight
    with nothing held open. Without the gate the next watchdog tick halts on
    the last quote of the day and every morning starts from a halt nobody
    caused.
    """
    supervisor = make_supervisor()
    try:
        await supervisor.tick()
        await settle(
            lambda: len(scripted.attempts) == 3, what="all three sockets connected"
        )

        clock.now = CLOSES + timedelta(seconds=1)
        await supervisor.tick()

        assert supervisor.held == ()
        assert supervisor.plans is None
        for name in ALL_SOCKETS:
            assert runtime.watchdog.feed_expected(name) is False
        assert runtime.watchdog.feed_expected() is False

        clock.advance(17 * 60 * 60)
        assert runtime.check_watchdog() is None
        with Session(db_engine) as session:
            # Still the cold-start halt, and no *new* reason written over it.
            assert engine_state(session).halted_reason is None
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_a_weekend_holds_nothing_open(
    make_supervisor: Callable[..., SocketSupervisor],
    clock: Clock,
    scripted: ScriptedConnect,
) -> None:
    supervisor = make_supervisor()
    try:
        clock.now = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)  # a Saturday
        await supervisor.tick()
        await pump()

        assert supervisor.held == ()
        assert scripted.urls == []
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_the_session_gate_reads_the_calendar_and_honours_a_half_day(
    runtime: EngineRuntime,
    paper_broker: RecordedBroker,
    clock: Clock,
    scripted: ScriptedConnect,
) -> None:
    """No hardcoded 09:30--16:00. The Friday after Thanksgiving closes at 13:00.

    Built with the **real** calendar resolvers rather than the injected ones,
    because "we read a market calendar" is the claim and an injected close
    cannot make it.
    """
    supervisor = SocketSupervisor(
        runtime=runtime,
        broker=lambda: paper_broker,
        on_quote=quote_sink(Fanout()),
        on_update=trade_update_sink(Fanout()),
        env=FAKE_ENV,
        now=clock,
        connect=scripted,
    )
    half_day_noon = datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc)  # 12:00 ET
    half_day_afternoon = datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc)  # 14:00 ET
    holiday = datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc)

    assert supervisor.in_session(half_day_noon) is True
    assert supervisor.in_session(half_day_afternoon) is False
    assert supervisor.in_session(holiday) is False
    assert supervisor.in_session(IN_SESSION) is True


# --------------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_ends_a_read_blocked_in_recv(
    make_supervisor: Callable[..., SocketSupervisor],
    scripted: ScriptedConnect,
    runtime: EngineRuntime,
) -> None:
    """``begin_close()`` cannot stop a blocked read; the owner closes and cancels.

    All three scripted sockets go quiet after authenticating, which is what a
    market-data socket looks like most of the time, so all three are parked
    in ``recv()`` when the shutdown lands.
    """
    supervisor = make_supervisor()
    await supervisor.tick()
    await settle(lambda: len(scripted.attempts) == 3, what="three sockets connected")

    await asyncio.wait_for(supervisor.aclose(), timeout=2.0)

    assert supervisor.held == ()
    # Our own shutdown is not a fault: the intent flag is set before anything
    # touches the socket, so nothing records a close and no halt follows.
    assert runtime.check_watchdog() is None


@pytest.mark.asyncio
async def test_shutdown_ends_a_socket_parked_in_backoff(
    make_supervisor: Callable[..., SocketSupervisor],
) -> None:
    """A pending backoff is up to thirty seconds. Shutdown does not wait it out."""
    connect = ScriptedConnect({}, refuse=True)
    supervisor = make_supervisor(
        connect=connect, stream_sleep=lambda seconds: asyncio.sleep(30)
    )
    await supervisor.tick()
    await settle(
        lambda: sum(connect.attempts.values()) >= 1, what="a connection was refused"
    )

    await asyncio.wait_for(supervisor.aclose(), timeout=2.0)

    assert supervisor.held == ()


@pytest.mark.asyncio
async def test_shutdown_ends_a_socket_that_stopped_answering(
    make_supervisor: Callable[..., SocketSupervisor],
) -> None:
    """The case neither ``aclose()`` nor ``begin_close()`` can reach.

    A socket that ignores its own close leaves the read blocked forever.
    Cancelling the task is the only bound on the wait, which is why the owner
    cancels rather than trusting either method.
    """
    deaf = ScriptedSocket(TRADE_FRAMES, codec=_stock_codec(), unblocks_on_close=False)
    connect = ScriptedConnect(
        {
            TRADE_SOCKET: deaf,
            OPTION_SOCKET: ScriptedSocket(
                QUOTE_FRAMES, codec=_option_codec(), unblocks_on_close=False
            ),
            EQUITY_SOCKET: ScriptedSocket(
                QUOTE_FRAMES, codec=_stock_codec(), unblocks_on_close=False
            ),
        }
    )
    supervisor = make_supervisor(connect=connect)
    await supervisor.tick()
    await settle(lambda: deaf.reads >= 3, what="the socket parked in recv")

    await asyncio.wait_for(supervisor.aclose(), timeout=2.0)

    assert supervisor.held == ()


# --------------------------------------------------------------------------
# Failure
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_socket_that_cannot_connect_leaves_the_supervisor_running(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
) -> None:
    """A refused connection is retried by the socket, not repaired here.

    And the expectation stands while it retries: a socket that never comes
    back is ninety seconds of silence, which is a halt. Loud, which is the
    point.
    """
    connect = ScriptedConnect({}, refuse=True)
    supervisor = make_supervisor(
        connect=connect, stream_sleep=lambda seconds: asyncio.sleep(0)
    )
    try:
        await supervisor.tick()
        await settle(
            lambda: sum(connect.attempts.values()) >= 3, what="it kept retrying"
        )

        assert set(supervisor.held) == set(ALL_SOCKETS)
        assert runtime.watchdog.feed_expected(OPTION_SOCKET) is True
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_a_book_that_cannot_be_read_still_opens_the_order_socket(
    make_supervisor: Callable[..., SocketSupervisor],
    scripted: ScriptedConnect,
) -> None:
    """The quote sockets need a plan; the order socket does not.

    A broker failure at the open therefore costs quotes and not fills, and
    the next tick tries the book again rather than the process needing a
    restart.
    """
    broken = RecordedBroker(label="broken", fail_with=RuntimeError("no book today"))
    supervisor = make_supervisor(broker=broken)
    try:
        await supervisor.tick()
        await pump()

        assert supervisor.held == (TRADE_SOCKET,)
        assert supervisor.plans is None

        broken.fail_with = None
        await supervisor.tick()
        await settle(
            lambda: len(scripted.attempts) == 3, what="the quote sockets followed"
        )
        assert set(supervisor.held) == set(ALL_SOCKETS)
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_the_book_is_read_once_per_session_not_once_per_tick(
    make_supervisor: Callable[..., SocketSupervisor],
    paper_broker: RecordedBroker,
    clock: Clock,
) -> None:
    """Re-planning on a book change is deferred to the scheduler, on purpose.

    Nothing in this process places an order yet, so the book cannot change
    under it; a plan rebuilt every five seconds would spend the 200/min
    budget on a question whose answer cannot have changed.
    """
    supervisor = make_supervisor()
    try:
        await supervisor.tick()
        await pump()
        reads = paper_broker.calls.count("positions")
        assert reads == 1

        for _ in range(5):
            await supervisor.tick()
        await pump()
        assert paper_broker.calls.count("positions") == reads

        # A new session plans again.
        clock.now = CLOSES + timedelta(seconds=1)
        await supervisor.tick()
        clock.now = IN_SESSION
        await supervisor.tick()
        await pump()
        assert paper_broker.calls.count("positions") == reads + 1
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_process_booted_after_the_bell_withdraws_every_expectation(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
) -> None:
    """The one path into the session gate that used to skip the gate.

    ``_stop`` returned early when there was nothing to release -- which is
    every tick of a process started at 18:00. An *absent* expectation is not a
    withdrawn one: the watchdog reads a missing key as expected with no floor,
    so the engine-wide condition stayed armed all night on a process holding
    nothing at all.

    Latent only because nothing calls ``record_poll`` yet. The moment
    something does -- Phase 4's position poll -- it is the nightly
    ``socket: null`` halt, arriving by the one path that skips the code
    written to close it.
    """
    supervisor = make_supervisor()
    clock.now = CLOSES + timedelta(seconds=1)
    try:
        await supervisor.tick()

        assert supervisor.held == ()
        assert runtime.watchdog.feed_expected() is False
        assert runtime.watchdog.feed_expected(TRADE_SOCKET) is False
        assert runtime.watchdog.handshake_expected(TRADE_SOCKET) is False

        # The producer that makes it reachable, and the halt that used to
        # follow it ninety seconds later.
        runtime.record_poll(clock.now)
        clock.advance(91)

        assert runtime.check_watchdog() is None
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_a_book_that_cannot_be_read_is_stated_once_a_session(
    make_supervisor: Callable[..., SocketSupervisor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same argument ``_unbuildable`` makes, which this half was missing.

    The likeliest trigger is the documented mistake of launching the API
    without ``--env-file .env``: a standing condition, retried every five
    seconds, which is 720 identical warnings an hour burying the one record
    that is news. Said once a session, and the plan is still retried on every
    tick -- a credential added to a reloaded environment has to be picked up.
    """
    broken = RecordedBroker(label="broken", fail_with=RuntimeError("no book today"))
    supervisor = make_supervisor(broker=broken)
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(4):
                await supervisor.tick()
                await pump()

        failures = [
            record
            for record in caplog.records
            if getattr(record, "event", "") == "socket_plan_failed"
        ]
        assert len(failures) == 1
        # Retried all the same: the quote sockets are still shut, and the
        # order socket is up regardless.
        assert supervisor.plans is None
        assert supervisor.held == (TRADE_SOCKET,)
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_being_correctly_idle_is_said_once_rather_than_never(
    make_supervisor: Callable[..., SocketSupervisor],
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An idle engine and a broken one produced the same log: nothing.

    One record per idle stretch is the whole difference, and the flag is what
    stops it being 17,000 of them overnight.
    """
    supervisor = make_supervisor()
    clock.now = CLOSES + timedelta(seconds=1)
    try:
        with caplog.at_level(logging.INFO):
            for _ in range(5):
                await supervisor.tick()

        idle = [
            record
            for record in caplog.records
            if getattr(record, "event", "") == "sockets_idle"
        ]
        assert len(idle) == 1
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_viewport_hint_alone_opens_no_socket_and_arms_no_watchdog(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
    scripted: ScriptedConnect,
    db_engine: Engine,
) -> None:
    """Rule 9: a browser scrolling must not be able to halt the engine.

    A flat book is ordinary -- it is every session before the first trade.
    With nothing held, the equity plan's entire content is the client's
    viewport hint. If that were enough to launch the equity socket, the
    watchdog would then judge it for ninety seconds of silence on symbols
    nobody validated against a universe: a name that never quotes on IEX, a
    typo, or a ticker that does not exist gives ninety seconds of nothing and
    a rule-9 halt requiring an explicit human resume. So the launch and the
    expectation read only what the *engine* asked for.
    """
    supervisor = make_supervisor(broker=EmptyBook(label="empty"))
    runtime.set_markets_visible(["NVDA", "TSLA", "SPY"])
    try:
        await supervisor.tick()
        await settle(
            lambda: runtime.watchdog.last_activity_at is not None,
            what="the order socket authorized",
        )
        # A second tick withdraws the order socket's handshake expectation,
        # which is a different condition and not what this test is about.
        await supervisor.tick()

        # The hint is planned -- it is not discarded, and it will ride a
        # socket the book opens -- but it opens nothing by itself.
        plans = supervisor.plans
        assert plans is not None
        assert plans.equity.subscribed == ("NVDA", "TSLA", "SPY")
        assert plans.equity.engine_subscribed == ()
        assert supervisor.held == (TRADE_SOCKET,)
        assert EQUITY_SOCKET not in scripted.attempts
        assert runtime.watchdog.feed_expected(EQUITY_SOCKET) is False
        assert runtime.watchdog.feed_expected() is False

        # And ninety seconds of the silence that would have followed is not
        # a halt, because nothing armed the condition.
        clock.advance(WATCHDOG_TIMEOUT_SECONDS + 1)
        assert runtime.check_watchdog() is None
        with Session(db_engine) as session:
            assert engine_state(session).halted_reason is None
    finally:
        await supervisor.aclose()


# --------------------------------------------------------------------------
# The mid-session re-plan (step 15's trigger)
# --------------------------------------------------------------------------


def _quote_connect() -> tuple[ScriptedSocket, ScriptedSocket, ScriptedConnect]:
    """``(option socket, equity socket, connect)``, held so a test can read them.

    The ``scripted`` fixture builds the same three and keeps them to itself;
    these tests are about what goes *out* on the equity socket after the
    session's first subscribe, so they need the reference.
    """
    option = ScriptedSocket(QUOTE_FRAMES, codec=_option_codec())
    equity = ScriptedSocket(QUOTE_FRAMES, codec=_stock_codec())
    connect = ScriptedConnect(
        {
            OPTION_SOCKET: option,
            EQUITY_SOCKET: equity,
            TRADE_SOCKET: ScriptedSocket(TRADE_FRAMES, codec=_stock_codec()),
        }
    )
    return option, equity, connect


async def _subscribed(supervisor: SocketSupervisor, socket: ScriptedSocket) -> None:
    """Run the first tick and wait for the equity socket's opening subscribe."""
    await supervisor.tick()
    await settle(
        lambda: any(frame.get("action") == "subscribe" for frame in socket.sent),
        what="the equity socket sent its opening subscribe",
    )


@pytest.mark.asyncio
async def test_a_hint_applied_mid_session_resubscribes_the_equity_stream(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
) -> None:
    """The trigger itself: an *applied* hint reaches the socket this session.

    The plan used to be built once at the open, so a hint that arrived at
    10:05 did nothing until the next session. What goes on the wire is the
    **difference** -- every resubscribe is a gap in the marks, and re-sending
    the position underlyings would cost marks on held positions to add one
    Markets row.
    """
    option, equity, connect = _quote_connect()
    supervisor = make_supervisor(connect=connect)
    try:
        await _subscribed(supervisor, equity)
        before = supervisor.plans
        assert before is not None
        held = before.equity.subscribed
        assert "ZZAA" not in held
        equity_frames = len(equity.sent)
        option_frames = len(option.sent)

        assert runtime.set_markets_visible(["ZZAA"]).changed is True
        await supervisor.tick()
        await pump()

        after = supervisor.plans
        assert after is not None
        assert after.equity.subscribed == (*held, "ZZAA")
        # The difference, and nothing else. Not the whole list again.
        assert equity.sent[equity_frames:] == [
            {"action": "subscribe", "quotes": ["ZZAA"]}
        ]
        # An equity-only hint change does not touch the option stream at all:
        # not on the wire, and not even a new plan object to report.
        assert option.sent[option_frames:] == []
        assert after.option is before.option
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_replan_on_a_flat_book_still_opens_no_equity_socket(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    clock: Clock,
    db_engine: Engine,
) -> None:
    """Finding 1, at the one moment a re-plan makes newly reachable.

    A flat book is every session before the first trade. The launch gate was
    only ever asked once, at the open, before any hint could exist; a
    mid-session re-plan asks it again, with a plan whose entire content is the
    client's. If that could launch the equity socket, ``_settle_expectations``
    would arm rule 9's ninety-second condition on symbols nobody validated
    against a universe, and a browser scrolling would halt the engine.
    """
    _, equity, connect = _quote_connect()
    supervisor = make_supervisor(broker=EmptyBook(label="empty"), connect=connect)
    try:
        await supervisor.tick()
        await settle(
            lambda: runtime.watchdog.last_activity_at is not None,
            what="the order socket authorized",
        )
        assert runtime.set_markets_visible(["ZZAA", "ZZAB", "ZZAC"]).changed is True
        await supervisor.tick()
        await pump()
        # A third tick, because the launch gate is asked again on every tick
        # after the one that re-planned.
        await supervisor.tick()
        await pump()

        plans = supervisor.plans
        assert plans is not None
        assert plans.equity.subscribed == ("ZZAA", "ZZAB", "ZZAC")
        assert plans.equity.engine_subscribed == ()
        assert supervisor.held == (TRADE_SOCKET,)
        assert EQUITY_SOCKET not in connect.attempts
        assert equity.sent == []
        assert runtime.watchdog.feed_expected(EQUITY_SOCKET) is False
        assert runtime.watchdog.feed_expected() is False

        clock.advance(WATCHDOG_TIMEOUT_SECONDS + 1)
        assert runtime.check_watchdog() is None
        with Session(db_engine) as session:
            assert engine_state(session).halted_reason is None
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_replan_never_widens_what_the_watchdog_judges(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
) -> None:
    """The other half of finding 1: the hint rides the socket, it never arms it.

    With a book the equity socket is open and expected -- on the *book's*
    symbols. Folding a viewport hint into that plan must leave
    ``engine_subscribed`` exactly where it was, because that tuple is what
    both the launch gate and the watchdog's expectation are taken on.
    """
    _, equity, connect = _quote_connect()
    supervisor = make_supervisor(connect=connect)
    try:
        await _subscribed(supervisor, equity)
        before = supervisor.plans
        assert before is not None
        engine_owned = before.equity.engine_subscribed
        assert engine_owned

        runtime.set_markets_visible(["ZZAA", "ZZAB"])
        await supervisor.tick()
        await pump()

        after = supervisor.plans
        assert after is not None
        assert after.equity.engine_subscribed == engine_owned
        assert set(after.equity.subscribed) == {*engine_owned, "ZZAA", "ZZAB"}
    finally:
        await supervisor.aclose()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_a_cap_the_server_lowered_survives_a_viewport_scroll(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
) -> None:
    """A 405 is the server's figure, and a browser scrolling must not unlearn it.

    ``_replan_for_viewport`` builds its revision from
    ``plan_stream_subscriptions``, which plans at the **account's** budget.
    After a 405 has narrowed the equity socket to fifteen, one viewport
    settle would otherwise re-subscribe at thirty, be refused again, and
    ratchet by halving to seven -- each round a whole-list resubscribe, which
    is a gap in the mark of every position underlying, once per scroll. The
    re-plan is built at the cap *in force* instead, so the correction holds
    and the hint spends only what is left under it.
    """
    # The 405 is scripted rather than pushed: ``ScriptedSocket.push`` wakes a
    # reader already parked on the release event, which reads as a close.
    equity = ScriptedSocket(
        (*QUOTE_FRAMES, [{"T": "error", "code": 405, "msg": "over the limit"}]),
        codec=_stock_codec(),
    )
    connect = ScriptedConnect(
        {
            OPTION_SOCKET: ScriptedSocket(QUOTE_FRAMES, codec=_option_codec()),
            EQUITY_SOCKET: equity,
            TRADE_SOCKET: ScriptedSocket(TRADE_FRAMES, codec=_stock_codec()),
        }
    )
    supervisor = make_supervisor(connect=connect)
    try:
        await _subscribed(supervisor, equity)
        planned = supervisor.plans
        assert planned is not None
        # What the *account* allows, which is what the supervisor planned at.
        assert planned.equity.cap == EQUITY_STREAM_SYMBOL_CAP
        engine_owned = planned.equity.engine_subscribed
        assert engine_owned

        # Nothing acknowledged yet, so the correction halves: the server
        # refused what we asked for and gave no figure of its own.
        corrected = EQUITY_STREAM_SYMBOL_CAP // 2
        assert len(engine_owned) < corrected
        await settle(
            lambda: sum(frame.get("action") == "subscribe" for frame in equity.sent)
            == 2,
            what="the re-subscribe at the corrected cap",
        )
        frames = len(equity.sent)

        # A viewport asking for more than the whole corrected budget.
        hint = tuple(f"ZZ{index:02d}" for index in range(EQUITY_STREAM_SYMBOL_CAP))
        assert runtime.set_markets_visible(list(hint)).changed is True
        await supervisor.tick()
        await pump()

        after = supervisor.plans
        assert after is not None
        # The correction survived the scroll, in the supervisor's own record
        # of what is in force as well as on the wire.
        assert after.equity.cap == corrected
        assert len(after.equity.subscribed) == corrected
        # The book keeps its slots; the hint spends what is left and the
        # tail of it is dropped, loudly, as a client-tier drop.
        assert after.equity.engine_subscribed == engine_owned
        assert after.client_not_streamed == len(hint) - (
            corrected - len(engine_owned)
        )
        # The difference only -- not the whole list again, and no second 405.
        added = [
            symbol
            for frame in equity.sent[frames:]
            for symbol in frame.get("quotes", ())
        ]
        assert all(frame.get("action") == "subscribe" for frame in equity.sent[frames:])
        assert set(added) == set(after.equity.subscribed) - set(
            planned.equity.subscribed
        )
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_an_unchanged_or_refused_hint_replans_nothing(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """*Applied*, never merely *not refused*. The gap in the marks is the cost.

    A hint identical to the one in force and a hint the engine refused whole
    are both reasons **not** to touch the socket, and the ``bool`` this used
    to return made the second look like the first.
    """
    _, equity, connect = _quote_connect()
    supervisor = make_supervisor(connect=connect)
    try:
        await _subscribed(supervisor, equity)
        runtime.set_markets_visible(["ZZAA"])
        await supervisor.tick()
        await pump()
        frames = len(equity.sent)

        with caplog.at_level(logging.INFO):
            # Identical: UNCHANGED.
            assert runtime.set_markets_visible(["ZZAA"]).changed is False
            await supervisor.tick()
            # Refused whole, and the previous hint stands.
            assert (
                runtime.set_markets_visible(["AAPL241220C00150000"]).status
                is MarketsVisibleStatus.REFUSED
            )
            await supervisor.tick()
            await pump()

        assert equity.sent[frames:] == []
        assert supervisor.plans is not None
        assert supervisor.plans.equity.subscribed[-1] == "ZZAA"
        replans = [
            record
            for record in caplog.records
            if getattr(record, "event", "") == "socket_replan"
        ]
        assert replans == []
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_a_replan_the_socket_cannot_carry_is_recorded_and_the_tick_survives(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A viewport scroll must not take the supervisor tick -- or the order socket -- down.

    ``apply_plan`` reaches the vendor socket's ``send``, and the realistic
    raise on a half-closed connection is ``SocketClosed``, not the
    ``ValueError`` the cap guard raises. Uncaught it escapes ``_open`` into
    ``tick``, which skips that tick's watchdog settle and the order socket's
    turn with it.

    It is recorded under its **own** event, because a refusal and a transport
    failure are different news, and the revision still becomes the plan in
    force: the client swapped its own before the send, so a supervisor that
    kept the old one would describe a plan no socket holds for the rest of
    the session.
    """
    _, equity, connect = _quote_connect()
    supervisor = make_supervisor(connect=connect)
    try:
        await _subscribed(supervisor, equity)

        async def _gone(_: Any) -> None:
            raise SocketClosed("sent 1011 (internal error); no close frame received")

        equity.send_text = _gone  # type: ignore[method-assign]
        equity.send_bytes = _gone  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO):
            assert runtime.set_markets_visible(["ZZAA"]).changed is True
            # The raise, if it escaped, would come out of here.
            await supervisor.tick()
            await pump()

        events = [getattr(record, "event", "") for record in caplog.records]
        assert events.count("socket_replan_undelivered") == 1
        assert events.count("socket_replan") == 1

        plans = supervisor.plans
        assert plans is not None
        # The client holds the revision, so this must too.
        assert plans.equity.subscribed[-1] == "ZZAA"

        undelivered = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "socket_replan_undelivered"
        )
        fields = undelivered.__dict__
        assert fields["error"] == "SocketClosed"
        assert fields["correlation_id"] == plans.equity.correlation_id
        # Counts, never keys: this symbol came from a browser, and neither is
        # the vendor's close text interpolated here.
        assert "ZZAA" not in repr(sorted(fields.items(), key=str))
        assert "1011" not in repr(sorted(fields.items(), key=str))

        replan = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "socket_replan"
        )
        assert replan.__dict__["dispatched"] is False
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_viewport_churn_is_one_info_record_and_never_a_dropped_symbol_warning(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Finding 2, re-checked at the volume a re-plan per settle produces.

    A full viewport is 64 rows against 30 equity slots, so most of it is
    trimmed on every plan -- by design, since every Markets row is polled
    regardless. That must stay **one INFO** record per plan, must never reach
    ``dropped_symbols``/``not_streamed`` (whose question is *"is anything I
    hold unmarked?"*), and must never put a browser's string into a log
    record's fields (finding 4).
    """
    _, equity, connect = _quote_connect()
    supervisor = make_supervisor(connect=connect)
    viewport = [f"ZZ{index:02d}" for index in range(64)]
    try:
        await _subscribed(supervisor, equity)
        with caplog.at_level(logging.INFO):
            assert runtime.set_markets_visible(viewport).changed is True
            await supervisor.tick()
            await pump()

        plans = supervisor.plans
        assert plans is not None
        assert plans.client_not_streamed > 0
        assert plans.not_streamed == 0
        assert plans.message is None

        events = [getattr(record, "event", "") for record in caplog.records]
        assert events.count("socket_replan") == 1
        assert events.count("stream_client_tier_trimmed") == 1
        assert events.count("stream_subscription_dropped") == 0
        assert events.count("stream_subscription_budget_exceeded") == 0

        replan = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "socket_replan"
        )
        # Counts, never keys. A shape filter is not a redactor, and these
        # strings came from a browser.
        assert "ZZ" not in repr(sorted(replan.__dict__.items(), key=str))
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_the_plan_records_survive_being_logged_at_info(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``message`` is a reserved ``LogRecord`` key, and both records used it.

    ``Logger.info`` returns before it builds a record when INFO is not
    enabled, so ``extra={"message": ...}`` was invisible for as long as
    nothing read these at INFO -- and fatal the moment something did:
    ``makeRecord`` raises ``KeyError``, the raise lands inside ``_plan``, the
    supervisor logs it as one bad tick and retries, and the quote sockets
    never open at all. The banner is carried as ``banner``.
    """
    _, equity, connect = _quote_connect()
    supervisor = make_supervisor(connect=connect)
    try:
        with caplog.at_level(logging.INFO):
            await _subscribed(supervisor, equity)
            runtime.set_markets_visible(["ZZAA"])
            await supervisor.tick()
            await pump()

        events = [getattr(record, "event", "") for record in caplog.records]
        assert events.count("socket_plan") == 1
        assert events.count("socket_replan") == 1
        assert supervisor.plans is not None
        plan_record = next(
            record
            for record in caplog.records
            if getattr(record, "event", "") == "socket_plan"
        )
        assert hasattr(plan_record, "banner")
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_a_closed_tab_gives_its_slots_back_in_the_same_session(
    make_supervisor: Callable[..., SocketSupervisor],
    runtime: EngineRuntime,
) -> None:
    """The hint is dropped with its client, and now that drop reaches the socket.

    The same trigger read the other way: what left the plan is unsubscribed,
    and what the book asked for is not re-sent.
    """
    _, equity, connect = _quote_connect()
    supervisor = make_supervisor(connect=connect)
    try:
        await _subscribed(supervisor, equity)
        before = supervisor.plans
        assert before is not None
        held = before.equity.subscribed
        runtime.set_markets_visible(["ZZAA", "ZZAB"], owner="tab-1")
        await supervisor.tick()
        await pump()
        frames = len(equity.sent)

        assert runtime.clear_markets_visible(owner="tab-1") is True
        await supervisor.tick()
        await pump()

        assert equity.sent[frames:] == [
            {"action": "unsubscribe", "quotes": ["ZZAA", "ZZAB"]}
        ]
        assert supervisor.plans is not None
        assert supervisor.plans.equity.subscribed == held
    finally:
        await supervisor.aclose()


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


def _app_with_streams(
    *,
    registry: ServiceRegistry,
    db_engine: Engine,
    clock: Clock,
    connect: Any,
    stream_sleep: Any = None,
) -> FastAPI:
    def factory(
        runtime: EngineRuntime, fanout: Fanout, services: ServiceRegistry
    ) -> SocketSupervisor:
        return SocketSupervisor(
            runtime=runtime,
            # Rule 5: the sockets read the Paper book, whatever else is
            # configured.
            broker=lambda: services.broker(AccountMode.PAPER),
            on_quote=quote_sink(fanout),
            on_update=trade_update_sink(fanout),
            env=FAKE_ENV,
            now=clock,
            connect=connect,
            stream_sleep=stream_sleep,
            interval_seconds=0.0,
            session_open=lambda day: OPENS if day == SESSION_DAY else None,
            session_close=lambda day: CLOSES if day == SESSION_DAY else None,
        )

    return create_app(registry=registry, db_engine=db_engine, streams=factory)


@pytest.mark.risk
@pytest.mark.asyncio
async def test_startup_runs_the_sockets_and_shutdown_terminates_them(
    registry: ServiceRegistry,
    db_engine: Engine,
    clock: Clock,
    scripted: ScriptedConnect,
) -> None:
    app = _app_with_streams(
        registry=registry, db_engine=db_engine, clock=clock, connect=scripted
    )
    async with app.router.lifespan_context(app):
        supervisor = app.state.socket_supervisor
        assert supervisor is not None
        await settle(
            lambda: len(scripted.attempts) == 3, what="all three sockets connected"
        )
        runtime = app.state.engine_runtime
        await settle(
            lambda: runtime.watchdog.last_activity_at is not None,
            what="the watchdog heard a message",
        )

    assert supervisor.held == ()


@pytest.mark.risk
@pytest.mark.asyncio
async def test_cold_start_is_halted_and_reads_the_paper_book(
    make_registry: Callable[..., ServiceRegistry],
    paper_broker: RecordedBroker,
    cash_broker: RecordedBroker,
    db_engine: Engine,
    clock: Clock,
    scripted: ScriptedConnect,
) -> None:
    """Rule 5 and rule 9's cold start, together, because they are one startup.

    A cash book is configured here and the sockets still read Paper. Nothing
    in the startup path resumes anything: the engine comes up halted and the
    only way out is ``POST /api/engine/resume``.
    """
    app = _app_with_streams(
        registry=make_registry(live_keys=True),
        db_engine=db_engine,
        clock=clock,
        connect=scripted,
    )
    async with app.router.lifespan_context(app):
        await settle(
            lambda: "positions" in paper_broker.calls, what="the paper book was read"
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://engine"
        ) as client:
            state = (await client.get("/api/engine/state")).json()

    assert state["halted"] is True
    assert cash_broker.calls == []


@pytest.mark.asyncio
async def test_a_socket_that_cannot_connect_leaves_health_answering(
    registry: ServiceRegistry,
    db_engine: Engine,
    clock: Clock,
) -> None:
    """``corollary.api:app`` must never stop answering ``GET /api/health``.

    A ``uvicorn --reload`` server runs against that exact string and the Vite
    proxy forwards ``/api`` to it, so a vendor socket that cannot connect has
    to leave every page that needs no broker working.
    """
    connect = ScriptedConnect({}, refuse=True)
    app = _app_with_streams(
        registry=registry,
        db_engine=db_engine,
        clock=clock,
        connect=connect,
        stream_sleep=lambda seconds: asyncio.sleep(0),
    )
    async with app.router.lifespan_context(app):
        await settle(
            lambda: sum(connect.attempts.values()) >= 2, what="the socket retried"
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://health"
        ) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_a_supervisor_that_cannot_be_built_leaves_the_app_serving(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """The composition root states its failures rather than dying of them."""

    def factory(
        runtime: EngineRuntime, fanout: Fanout, services: ServiceRegistry
    ) -> SocketSupervisor:
        raise RuntimeError("no sockets today")

    app = create_app(registry=registry, db_engine=db_engine, streams=factory)
    async with app.router.lifespan_context(app):
        assert app.state.socket_supervisor is None
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://health"
        ) as client:
            assert (await client.get("/api/health")).status_code == 200


@pytest.mark.asyncio
async def test_an_app_built_without_sockets_starts_nothing(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """What the route suites get: a lifespan that opens no vendor socket.

    Not a convenience. An ``app`` fixture that opens a websocket to Alpaca
    depending on whether the developer happened to export keys into their
    shell is not a fixture.
    """
    app = create_app(
        registry=registry, db_engine=db_engine, streams=no_socket_supervisor
    )
    async with app.router.lifespan_context(app):
        assert app.state.socket_supervisor is None


@pytest.mark.risk
def test_an_app_built_without_asking_opens_no_sockets(
    registry: ServiceRegistry, db_engine: Engine
) -> None:
    """The default is none, so the fifteen test apps are safe by construction.

    It was the shipped wiring, and the safety of every ``create_app(...)`` in
    the suite rested on three coincidences: the supervisor sleeps before its
    first tick, no test holds a lifespan open for five seconds, and no
    ``tests/conftest.py`` scrubs the environment. Break one -- a breakpoint
    under ``pytest --pdb``, a slow box -- and a test reads the real paper
    credentials and opens the trading stream. Alpaca allows one of those per
    account, so the suite would take the slot from the running engine, which
    then halts itself correctly because of a test.
    """
    app = create_app(registry=registry, db_engine=db_engine)

    assert app.state.socket_factory is no_socket_supervisor


@pytest.mark.risk
def test_the_shipped_app_opts_in_and_the_reload_app_does_not() -> None:
    """The opt-in is one line, so a test has to be able to see it.

    With the safe default, the line that can go missing is the *production*
    one -- an app serving every route with rule 9's producers quietly absent.
    ``dev_app`` is deliberately without them: a reloader opens a second set of
    sockets on every save and Alpaca answers the surplus trading connection
    with a 406, which halts the engine correctly and repeatedly, and a halt
    log full of self-inflicted entries is one nobody reads on the morning that
    matters.
    """
    assert shipped_app.state.socket_factory is build_socket_supervisor
    assert dev_app.state.socket_factory is no_socket_supervisor
    # Two apps, not one shared object: `dev_app` must not be able to hand a
    # route table or a fan-out to the one that holds the sockets.
    assert shipped_app is not dev_app
