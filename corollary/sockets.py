"""The vendor-neutral half of a websocket client: transport, codec, backoff.

Two Alpaca sockets carry market data (``data/providers/alpaca.py``) and one
carries the order lifecycle (``engine/execution/alpaca.py``). CLAUDE.md caps
the vendor surface at those two files, so everything the three clients share
that is *not* about Alpaca lives here instead of being written twice or
imported across the surface.

What is here:

* :class:`VendorSocket` -- the two directions and the close, as a protocol, so
  a test drives a list of frames and never opens a port.
* :class:`SocketClosed` -- one exception for *"the far end is gone"*, however
  the transport spelled it.
* :class:`Codec` -- how a frame becomes an object. Alpaca's option stream is
  msgpack only; the stock stream and the trading stream take JSON. That is a
  vendor fact per socket, not configuration, so it is a constructor argument
  and never an environment variable.
* :class:`StreamActivityRecorder` -- the *narrow* view of
  ``EngineRuntime`` that a socket is allowed to touch. Three recorders, no
  ``halt``, no ``resume``, no ``record_poll``.
* :func:`reconnect_delay` -- the backoff, pure.

**Why the activity recorder is a protocol and not the runtime itself.** A
``data/providers/`` module importing ``engine/runtime.py`` points the
dependency the wrong way and drags the database session factory, the halt
path and the notification writer into the market-data layer. A three-method
protocol inverts it: the socket says *"something arrived"* and knows nothing
about what that causes. ``EngineRuntime`` satisfies it structurally, which
``tests/data/providers/test_alpaca_stream.py`` proves by passing a real one.

**Why there is no null recorder.** Rule 9's switch is the reason these
clients exist at all, so a socket constructed without a recorder would be a
socket that silently un-arms the dead-man's switch. The argument is required.
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Final, Protocol, runtime_checkable

import msgpack
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as open_websocket

from corollary.wire import decode_json, decode_msgpack, vendor_detail

__all__ = [
    "JSON_CODEC",
    "MSGPACK_CODEC",
    "MAX_RECONNECT_DELAY_SECONDS",
    "Codec",
    "SocketClosed",
    "SocketConnect",
    "StreamActivityRecorder",
    "VendorSocket",
    "VendorStream",
    "connect_websocket",
    "reconnect_delay",
    "sleep_for",
    "utcnow",
]

logger = logging.getLogger(__name__)


class SocketClosed(RuntimeError):
    """The far end of a websocket is gone. Ours or theirs is the caller's to say.

    Carries the transport's own words in :attr:`detail` so a log record can
    quote them. The caller is responsible for putting that text through
    :func:`corollary.wire.vendor_detail` before it reaches a log or an
    exception message -- rule 6 -- because the text is vendor-derived and a
    reverse proxy in front of a vendor has been known to reflect request
    headers into a body.
    """

    def __init__(self, detail: str = "", *, code: int | None = None) -> None:
        super().__init__(detail or "socket closed")
        self.detail = detail
        #: The websocket close code where the transport reported one. ``1000``
        #: is a graceful close and still a lost feed -- see the clients on why
        #: a polite vendor hangup is rule 9's condition all the same.
        self.code = code


@runtime_checkable
class VendorSocket(Protocol):
    """One open websocket, in the two directions a client needs.

    **The send methods are named ``send_text`` and ``send_bytes``, not
    ``send``.** That is deliberate and it is not a dodge:
    ``tests/test_hard_rules.py`` treats ``.send()`` on the vendor surface as a
    write verb, because ``httpx``'s ``send`` takes the method as an argument
    and would carry a POST past the guard. A websocket subscribe changes
    nothing at the vendor -- it is how a read is scoped -- and the two names
    keep the guard's list about HTTP verbs, where it belongs. What the sockets
    transmit is pinned by behaviour instead, in each client's tests: the
    market-data client sends an auth frame and subscribe frames and nothing
    else, ever.
    """

    async def send_text(self, text: str) -> None: ...

    async def send_bytes(self, payload: bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


#: How a client opens a socket. Injected, so a test never binds a port.
SocketConnect = Callable[[str, Mapping[str, str]], Awaitable[VendorSocket]]


@runtime_checkable
class StreamActivityRecorder(Protocol):
    """The only part of the engine a vendor socket may touch.

    Three recorders and nothing else. A socket cannot halt the engine, cannot
    resume it, and cannot record a *poll* -- a poll is a REST call's evidence
    of life and a socket has no business claiming one. It reports what it saw;
    ``EngineRuntime.check_watchdog`` decides what that means.

    **Every record names its socket, and the name is required here on
    purpose.** Rule 9's liveness is per socket: one shared clock meant a
    chatty stock stream refreshed the clock the ``trade_updates`` socket was
    judged by, so the order feed could die with quotes still flowing and
    nothing halted. The name is also what the halt reason says, and losing
    quotes is not the same event as losing fills. :class:`VendorStream` takes
    it in its constructor and passes it at every call site, so a client cannot
    report anonymously by forgetting one.
    """

    def record_message(
        self, at: datetime | None = None, *, socket: str
    ) -> None: ...

    def record_stream_open(
        self, at: datetime | None = None, *, socket: str
    ) -> None: ...

    def record_stream_closed(
        self, at: datetime | None = None, *, socket: str, detail: str = ""
    ) -> None: ...


class Codec:
    """How one socket's frames are encoded, both ways.

    Encoding differs per socket and decoding does not: outgoing frames use
    whichever format that socket's handshake negotiated, while an incoming
    frame declares its own format by *being* text or bytes. Decoding on the
    frame type rather than on the configured codec is defensive on purpose --
    an error emitted before codec negotiation completes arrives as text on a
    socket that is otherwise msgpack, and a decoder that insisted on msgpack
    would turn the vendor's explanation into a parse error.
    """

    def __init__(self, name: str, *, binary: bool) -> None:
        self.name = name
        #: Whether outgoing frames are msgpack bytes rather than JSON text.
        self.binary = binary

    def __repr__(self) -> str:
        return f"Codec({self.name!r})"

    def encode(self, message: Any) -> str | bytes:
        """One outgoing message, in this codec's format.

        Typed ``Any`` rather than ``Mapping``: what this codec encodes and
        what it decodes are the same language, and Alpaca's data streams send
        *arrays* of messages. A signature that only admitted a mapping would
        make the test doubles unable to speak the protocol they are doubling.
        """
        if self.binary:
            packed = msgpack.packb(message, use_bin_type=True)
            if packed is None:  # pragma: no cover -- msgpack returns None never
                raise ValueError(f"could not encode {self.name} frame")
            return bytes(packed)
        return json.dumps(message, separators=(",", ":"), default=str)

    def decode(self, frame: str | bytes) -> Any:
        """One received frame as Python objects, with money as ``Decimal``.

        Both paths convert numbers exactly: ``decode_json`` parses ``4.15``
        from its digits, and ``decode_msgpack`` converts the double it is
        handed through its shortest repr. Neither leaves a ``float`` where
        ``corollary.wire.as_decimal`` would refuse it.
        """
        if isinstance(frame, (bytes, bytearray, memoryview)):
            return decode_msgpack(bytes(frame))
        return decode_json(frame)

    async def transmit(self, socket: VendorSocket, message: Mapping[str, Any]) -> None:
        """Encode ``message`` and put it on the wire, in this codec's format."""
        encoded = self.encode(message)
        if isinstance(encoded, bytes):
            await socket.send_bytes(encoded)
        else:
            await socket.send_text(encoded)


#: JSON text frames: Alpaca's stock data stream and the trading stream.
JSON_CODEC: Final = Codec("json", binary=False)

#: msgpack binary frames: Alpaca's **option** data stream, which documents no
#: other format -- *"unlike the stock and crypto stream, the option stream is
#: only available in msgpack format"*.
MSGPACK_CODEC: Final = Codec("msgpack", binary=True)


#: The longest a client waits between reconnect attempts.
#:
#: Thirty seconds, which is a third of rule 9's ninety-second window: a feed
#: that comes back inside one delay still leaves the watchdog two chances to
#: see a message before it halts. Longer would make the backoff itself the
#: cause of a halt.
#:
#: **That reasoning is about the *staleness* condition only, and reading it as
#: the whole story hid a defect for a while.** The other condition is the
#: close itself, and the close is not measured in seconds -- it is a fact the
#: moment it arrives. The short end of this schedule is what made it
#: invisible: ``reconnect_delay(1)`` is one second against a five-second
#: supervisor period, so a reopen that cleared the recorded close erased rule
#: 9's condition before anything evaluated it, every time a socket came back
#: quickly. ``Watchdog.record_stream_open`` is where that is now held instead
#: -- a close waits for the first ``evaluate`` and is cleared by it -- so no
#: value on this schedule can make a close unobservable.
MAX_RECONNECT_DELAY_SECONDS: Final[float] = 30.0


def reconnect_delay(attempt: int) -> float:
    """Seconds to wait before reconnect attempt ``attempt`` (1-based).

    Doubling from one second to :data:`MAX_RECONNECT_DELAY_SECONDS`, and
    **no jitter**. Jitter exists to de-synchronise many clients from one
    server; this is one desktop terminal holding at most three sockets, so it
    would buy nothing and cost determinism in the tests that prove the
    schedule.
    """
    if attempt < 1:
        raise ValueError(f"a reconnect attempt is 1-based; got {attempt}")
    return min(2.0 ** (attempt - 1), MAX_RECONNECT_DELAY_SECONDS)


def utcnow() -> datetime:
    """The default clock. Injected everywhere, so no test reads a real one."""
    return datetime.now(timezone.utc)


class VendorStream(ABC):
    """One vendor websocket's lifecycle: connect, read, attribute the close.

    The three Alpaca sockets differ in their protocols and not in their
    lifecycles, and the part of the lifecycle that must never differ is rule
    9's **close attribution**. So it is written once, here, and a subclass
    supplies four things: the handshake headers, the auth message, how a
    frame becomes a list of messages, and what to do with one.

    Two copies of this would be two chances for one socket to stop halting
    the engine while the other kept doing it -- and the symptom of the broken
    copy is silence, which is the failure mode rule 9 exists to end.

    **How a close we initiated is told from one the vendor caused.** Not by
    the close code: 1000 is a graceful hangup and a graceful hangup is still a
    lost feed, and a proxy in the middle can produce any code it likes. It is
    told by :meth:`begin_close`, which sets an intent flag *before* anything
    touches the socket. Every close observed with the flag set is ours by
    construction; every close observed with it clear is theirs, whatever it
    says. The flag is the only evidence that exists before the fact, and
    guessing after the fact is how a shutdown gets reported as a fault --
    which trains whoever reads the alerts to ignore the real one.

    **One ordering that construction does not cover**, handled explicitly in
    :meth:`run_session` rather than left to the flag: the flag can be set
    while a connect is still in flight, so it is set before the socket it
    would attribute exists. A session that finds the flag set immediately
    after its connect returns closes that socket and records nothing --
    there was no feed, and so no close to attribute either way.
    """

    def __init__(
        self,
        *,
        name: str,
        url: str,
        codec: Codec,
        activity: StreamActivityRecorder,
        connect: SocketConnect | None = None,
        sleep: Callable[[float], Any] | None = None,
        now: Callable[[], datetime] = utcnow,
        secrets: Sequence[str] = (),
    ) -> None:
        #: How this socket is known to rule 9's watchdog and to a halt
        #: reason: ``option_quotes``, ``equity_quotes``, ``trade_updates``.
        #: Required, because liveness is tracked per socket and a socket that
        #: reported anonymously would share a clock with the other two --
        #: which is exactly the defect that made a dead order feed invisible.
        self._name = name
        self._url = url
        self._codec = codec
        self._activity = activity
        self._connect: SocketConnect = connect or connect_websocket
        self._sleep = sleep or sleep_for
        self._now = now
        #: Both halves of the key pair, for :meth:`_detail`. Both are sent, and
        #: of the two it is the secret that authenticates.
        self._secrets = tuple(secrets)
        self._socket: VendorSocket | None = None
        #: Set **before** the socket is closed. See the class docstring.
        self._closing = False
        #: The same fact, awaitable, so a backoff can be abandoned. Set
        #: alongside the flag and never instead of it -- see :meth:`_backoff`.
        self._close_requested = asyncio.Event()
        #: One sender at a time. See :meth:`_transmit`.
        self._send_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """This socket's name in the watchdog and in a halt reason."""
        return self._name

    @property
    def url(self) -> str:
        return self._url

    @property
    def codec(self) -> Codec:
        return self._codec

    @property
    def closing(self) -> bool:
        return self._closing

    # -- running -----------------------------------------------------------

    async def run(self) -> None:
        """Hold the socket open, reconnecting when the vendor drops it.

        **Returns promptly only on :meth:`aclose`.** ``begin_close()`` sets
        the intent flag and closes nothing, so a loop blocked in ``recv()``
        on a healthy-but-quiet socket -- which is what a market-data socket
        looks like most of the time -- stays blocked until the vendor sends
        something or hangs up. That is not a defect in ``begin_close``, whose
        job is attribution, but it was advertised here as a way to stop the
        loop and it is not one. A shutdown calls ``aclose()``; a supervisor
        that wants a bound on the wait cancels the task as well, because a
        vendor that stops answering entirely can outlast either.

        A close asked for **during the backoff** no longer waits the delay
        out: the sleep is raced against the close, so a shutdown in late
        backoff costs nothing rather than up to
        :data:`MAX_RECONNECT_DELAY_SECONDS`.

        Exceptions other than :class:`SocketClosed` are **not** caught: a
        refusal that recurs identically on reconnect must surface rather than
        loop, and a bug in a subclass must not be retried forever.

        The backoff counter is not reset by a session that authenticated. A
        feed that connects and drops repeatedly is not a healthy feed, and the
        cap at :data:`MAX_RECONNECT_DELAY_SECONDS` bounds the wait at a third
        of rule 9's ninety seconds -- so a socket that is going to come back
        still has two watchdog ticks to prove it before the switch fires.

        **Reconnecting never clears a halt.** The socket records that it came
        back; ``engine_state.halted`` is a human's to clear. Reconnecting into
        an unverified position state is how a bot doubles a position it
        already holds.
        """
        attempt = 0
        while not self._closing:
            try:
                await self.run_session()
            except SocketClosed:
                if self._closing:
                    return
                attempt += 1
                await self._backoff(reconnect_delay(attempt))

    async def _backoff(self, seconds: float) -> None:
        """Wait ``seconds``, or until a close is asked for -- whichever first.

        The delay is still the injected :attr:`_sleep`, so a test asserts the
        schedule without waiting; what is added is the race against
        :attr:`_close_requested`. Without it, ``aclose()`` during a late
        backoff set the flag, closed the socket that was already gone, and
        then blocked its caller for the rest of the delay -- up to
        :data:`MAX_RECONNECT_DELAY_SECONDS` on a lifespan shutdown.

        The event is an *optimisation on top of the flag*, never a
        replacement for it: :meth:`begin_close` may be called from a signal
        handler or another thread, where ``Event.set`` does not reliably wake
        a waiter, so :meth:`run` re-reads ``self._closing`` after this returns
        and the worst case is the old behaviour rather than a missed
        shutdown.
        """
        sleeping = asyncio.ensure_future(_awaited(self._sleep(seconds)))
        closing = asyncio.ensure_future(self._close_requested.wait())
        done, pending = await asyncio.wait(
            {sleeping, closing}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if sleeping in done:
            # A sleep that raised is a bug in the injected delay, not a
            # silence to swallow -- and an unretrieved task exception is a
            # warning nobody reads.
            sleeping.result()

    async def run_session(self) -> None:
        """One connection, from connect to close.

        Returns normally when **we** closed it, and raises
        :class:`SocketClosed` when the vendor or the network did -- having
        recorded that close for the watchdog first. That asymmetry is rule 9:
        a shutdown is not a fault, and a fault is not a shutdown.
        """
        socket: VendorSocket | None = None
        try:
            socket = await self._connect(self._url, self._handshake_headers())
            if self._closing:
                # A shutdown landed inside the handshake. `aclose()` read
                # `self._socket` while it was still None, so it closed
                # nothing, and this frame holds the only reference to a live
                # vendor connection: carrying on would send auth and then
                # read frames forever on a socket the engine believes is
                # shut, calling `record_message` on every one -- rule 9's
                # staleness condition suppressed by a socket nobody can close
                # -- while holding a vendor connection slot the next process
                # may be refused for. The `finally` below closes it.
                #
                # This is the one ordering the class docstring's "every close
                # observed with the flag set is ours by construction" does
                # not cover, because the flag is set before the socket it
                # would attribute exists.
                return
            self._socket = socket
            await self._transmit(socket, self._auth_message())
            while True:
                frame = await socket.recv()
                # Recorded before the frame is decoded: the socket is alive
                # whether or not we can read what it said, and rule 9's
                # question is only whether anything is coming through.
                self._record_message(self._now())
                for message in self._messages(frame):
                    await self._handle(socket, message)
        except SocketClosed as closed:
            if self._closing:
                return
            self._record_stream_closed(self._now(), self._detail(closed.detail))
            raise
        finally:
            self._socket = None
            if socket is not None:
                with suppress(Exception):
                    await socket.close()

    async def _transmit(
        self, socket: VendorSocket, *messages: Mapping[str, Any]
    ) -> None:
        """Put frames on the wire. One sender at a time, and grouped stays grouped.

        Every frame a stream sends goes through here, and the lock is not
        decoration: a subscription may now be revised from a task that is not
        the one reading the socket (``SocketSupervisor`` hands the equity
        stream a new plan mid-session), so two coroutines can reach one
        connection. Interleaved there, an unsubscribe and a subscribe can land
        either side of a frame the read loop is sending -- and the state that
        leaves behind is a subscription set matching neither plan, which shows
        up as a position marking off a price nobody asked for.

        Several messages in one call are sent under **one** acquisition,
        because a revision is one decision: unsubscribe what left, subscribe
        what arrived, with nothing of anyone else's in between.
        """
        async with self._send_lock:
            for message in messages:
                await self._codec.transmit(socket, message)

    def begin_close(self) -> None:
        """Declare that any close from here on is **ours**. Synchronous.

        Separate from :meth:`aclose` because the *flag* is the distinction and
        the flag has to be settable from anywhere -- a signal handler, a
        lifespan shutdown, a supervisor with no loop of its own to await on.
        Setting it closes nothing; it makes the close that follows
        attributable.

        **It does not stop a read loop**, and nothing here should be read as
        implying otherwise: a loop blocked in ``recv()`` on a quiet socket is
        still blocked afterwards. :meth:`aclose` is what a shutdown calls.
        """
        self._closing = True
        self._close_requested.set()

    async def aclose(self) -> None:
        """Stop, and close the socket if one is open. Idempotent.

        Three states, and the middle one used to be silently wrong. With a
        socket open, this closes it and the read loop ends. With one still
        *connecting*, there is nothing here to close -- :meth:`run_session`
        checks the flag the moment its connect returns and closes it there.
        With the loop in backoff, :meth:`_backoff` abandons the wait.

        It does not wait for :meth:`run` to return, so a caller that needs
        that awaits the task afterwards.
        """
        self.begin_close()
        socket, self._socket = self._socket, None
        if socket is not None:
            with suppress(Exception):
                await socket.close()

    # -- what a subclass reports -------------------------------------------
    #
    # Three one-line wrappers rather than three call sites reaching for
    # `self._activity` directly, and the reason is the name: it has to be on
    # every record, and a subclass that forgot it at one site out of five
    # would silently put that socket back on the shared liveness clock. The
    # symptom of that is silence, which is the failure rule 9 exists to end,
    # so it is arranged to be unforgettable instead of documented.

    def _record_message(self, at: datetime) -> None:
        self._activity.record_message(at, socket=self._name)

    def _record_stream_open(self, at: datetime) -> None:
        self._activity.record_stream_open(at, socket=self._name)

    def _record_stream_closed(self, at: datetime, detail: str) -> None:
        self._activity.record_stream_closed(at, socket=self._name, detail=detail)

    # -- what a subclass supplies ------------------------------------------

    @abstractmethod
    def _handshake_headers(self) -> dict[str, str]: ...

    @abstractmethod
    def _auth_message(self) -> Mapping[str, Any]: ...

    @abstractmethod
    def _messages(self, frame: str | bytes) -> list[Mapping[str, Any]]: ...

    @abstractmethod
    async def _handle(self, socket: VendorSocket, message: Mapping[str, Any]) -> None:
        ...

    # -- rule 6 ------------------------------------------------------------

    def _detail(self, text: str) -> str:
        """Vendor text, fit to be logged. **Redacted first, then bounded.**

        The ordering is the rule, not an implementation detail: cutting before
        redacting keeps the first half of a credential, which is worth no less
        to whoever reads the log. Everything vendor-derived that reaches a log
        record or an exception message in a stream client goes through here.
        """
        return vendor_detail(text, secrets=self._secrets)


class _WebsocketsSocket:
    """:class:`VendorSocket` over the ``websockets`` library.

    The only thing in this module that knows what a real socket is. Every
    close the library reports -- graceful, abnormal, or a transport error
    mid-read -- becomes one :class:`SocketClosed`, because a client that had
    to branch on the transport's exception taxonomy would get it wrong on the
    day a new one appeared, and *"the feed is gone"* is the only distinction
    rule 9 draws.
    """

    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection

    async def send_text(self, text: str) -> None:
        try:
            await self._connection.send(text)
        except websockets.ConnectionClosed as closed:
            raise _closed(closed) from closed

    async def send_bytes(self, payload: bytes) -> None:
        try:
            await self._connection.send(payload)
        except websockets.ConnectionClosed as closed:
            raise _closed(closed) from closed

    async def recv(self) -> str | bytes:
        try:
            return await self._connection.recv()
        except websockets.ConnectionClosed as closed:
            raise _closed(closed) from closed

    async def close(self) -> None:
        await self._connection.close()


def _closed(closed: websockets.ConnectionClosed) -> SocketClosed:
    received = closed.rcvd
    code = received.code if received is not None else None
    return SocketClosed(str(closed), code=code)


async def connect_websocket(
    url: str, headers: Mapping[str, str]
) -> VendorSocket:
    """Open ``url`` and hand back a :class:`VendorSocket`.

    The default :data:`SocketConnect`. ``headers`` carries the credentials on
    the trading stream's handshake and the ``Content-Type`` that selects
    msgpack on the option stream, so it is a required argument rather than an
    optional one -- an omitted content type is a socket that silently speaks
    the wrong format.
    """
    try:
        connection = await open_websocket(url, additional_headers=dict(headers))
    except websockets.InvalidStatus as exc:
        # An HTTP status refusal, carrying the vendor's own code. Kept ahead
        # of the clause below -- it is an `InvalidHandshake` too -- so the
        # message says *refused* rather than *could not connect*.
        raise SocketClosed(f"handshake refused: {exc}") from exc
    except (OSError, websockets.WebSocketException) as exc:
        # `OSError` is a refused connection, a DNS failure, a TLS error and a
        # handshake timeout (`TimeoutError` subclasses `OSError`).
        #
        # `WebSocketException` is the library's own half of the taxonomy, and
        # it needs its own clause because **none of it subclasses OSError**:
        # `InvalidHandshake`, `InvalidMessage` and `InvalidURI` all went
        # straight through an `except OSError`. `InvalidMessage` is not
        # exotic -- it is what the library raises when a server accepts TCP
        # and then hangs up or answers with something that is not HTTP, which
        # covers a captive portal, a corporate proxy, and the transport-level
        # shape of a vendor refusing a surplus concurrent connection.
        # Uncaught it escaped `run_session` *and* `run` (which catches only
        # SocketClosed), so no close was recorded, the reconnect loop ended
        # permanently, and the feed never returned even once the network did.
        # `tests/test_sockets.py` pins the non-subclassing, so a `websockets`
        # upgrade that re-parents these fails a test instead of quietly
        # narrowing this catch.
        #
        # All of it is one thing to a caller: no feed. Raised as SocketClosed
        # so the reconnect loop has one exception to catch, and so
        # `corollary.api:app` keeps serving when Alpaca is unreachable.
        raise SocketClosed(f"could not connect: {exc}") from exc
    return _WebsocketsSocket(connection)


async def sleep_for(seconds: float) -> None:
    """The default delay. Injected everywhere, so no test waits one out."""
    await asyncio.sleep(seconds)


async def _awaited(awaitable: Any) -> None:
    """Await whatever the injected sleep returned, as a coroutine.

    ``VendorStream._sleep`` is typed ``Callable[[float], Any]`` because a test
    may hand it any awaitable; :meth:`VendorStream._backoff` needs one it can
    wrap in a task. One line, so the race lives in ``_backoff`` and the typing
    accommodation lives here.
    """
    await awaitable
