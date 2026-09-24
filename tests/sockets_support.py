"""Doubles for the three vendor websockets. No port is ever bound.

``corollary.sockets.VendorSocket`` is a four-method protocol precisely so that
these exist: a test queues the frames Alpaca would send, reads back what was
transmitted, and decides when the far end goes away. Shared between
``tests/data/providers/test_alpaca_stream.py``,
``tests/engine/execution/test_trade_update_stream.py`` and
``tests/engine/test_stream_watchdog.py``, because a second copy of a fake
socket is a second protocol to drift from the first.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from corollary.sockets import Codec, SocketClosed, VendorSocket

T0 = datetime(2026, 9, 14, 13, 30, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class RawFrame:
    """A frame :class:`FakeSocket` hands over exactly as given, not re-encoded.

    Every other scripted frame is encoded with the socket's own codec, which
    means a JSON socket's double could only ever emit *text* -- and Alpaca's
    paper trading host sends its JSON replies in *binary* websocket frames.
    No test could express that, so the decoder that dropped them shipped and
    halted the engine 90s after every start. This is how a test says "the
    vendor put these exact bytes (or this exact text) on the wire".
    """

    payload: str | bytes | bytearray | memoryview


class Clock:
    """A hand-wound UTC clock. Nothing in these tests reads a real one."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class SpyActivity:
    """A :class:`corollary.sockets.StreamActivityRecorder` that only remembers.

    Three lists, so a test can say *"the socket recorded a close"* without a
    database, and -- more to the point -- can say *"the socket recorded
    nothing"*, which is what the shutdown case has to prove.
    """

    def __init__(self) -> None:
        self.messages: list[datetime | None] = []
        self.opens: list[datetime | None] = []
        self.closes: list[tuple[datetime | None, str]] = []
        #: Every socket name this recorder was handed, in order, including
        #: the ones on messages. Rule 9's liveness is per socket, so *which
        #: socket said so* is part of what a client reports -- and a client
        #: that reported anonymously would share a clock with the other two.
        self.named: list[str] = []

    def record_message(self, at: datetime | None = None, *, socket: str) -> None:
        self.messages.append(at)
        self.named.append(socket)

    def record_stream_open(self, at: datetime | None = None, *, socket: str) -> None:
        self.opens.append(at)
        self.named.append(socket)

    def record_stream_closed(
        self, at: datetime | None = None, *, socket: str, detail: str = ""
    ) -> None:
        self.closes.append((at, detail))
        self.named.append(socket)


class FakeSocket:
    """One scripted connection.

    ``frames`` is what the vendor sends, in order, as Python objects: each is
    encoded with the socket's own codec on the way out of :meth:`recv`, so a
    msgpack stream is exercised as msgpack rather than as a convenient dict. A
    :class:`SocketClosed` in the list is raised instead of returned, which is
    how a test says *"the far end went away here"*. Running off the end of the
    list is also a close -- an abnormal one, the 1006 a dropped TCP connection
    produces.

    ``hold_open`` is the exception to that last sentence, and it is what a
    *mid-session* test needs: with it set, an exhausted script parks on
    :meth:`recv` until :meth:`push` supplies more, so the session stays up
    while the test revises the plan. Without it there is no way to reach
    ``apply_plan`` on a live connection at all -- the read loop has already
    ended by the time the test regains control -- and the dispatched half of
    that method goes untested. :meth:`release` ends such a session.
    """

    def __init__(
        self,
        frames: Sequence[Any],
        *,
        codec: Codec,
        on_exhausted: SocketClosed | None = None,
        hold_open: bool = False,
    ) -> None:
        self._frames = list(frames)
        self._codec = codec
        self._hold_open = hold_open
        self._more = asyncio.Event()
        self._on_exhausted = on_exhausted or SocketClosed(
            "1006 (connection closed abnormally [internal])"
        )
        #: Everything transmitted, decoded. A test asserts on the *whole* list,
        #: because "and nothing else was ever sent" is half of what it proves.
        self.sent: list[Any] = []
        self.closed = False
        #: Frames handed to the client. A ``hold_open`` test waits on this to
        #: know a pushed frame has been taken, rather than on a timer.
        self.reads = 0

    # -- the protocol ------------------------------------------------------

    async def send_text(self, text: str) -> None:
        self.sent.append(self._codec.decode(text))

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(self._codec.decode(payload))

    async def recv(self) -> str | bytes:
        while True:
            while self._frames:
                frame = self._frames.pop(0)
                if isinstance(frame, SocketClosed):
                    raise frame
                if callable(frame):
                    # A hook, so a test can close the client from inside its
                    # own read loop -- which is how "we closed it" is
                    # distinguished from "they closed it" without a second
                    # thread.
                    frame()
                    continue
                self.reads += 1
                if isinstance(frame, RawFrame):
                    return frame.payload  # type: ignore[return-value]  # bytearray/memoryview on purpose
                return self._codec.encode(frame)
            if not self._hold_open:
                raise self._on_exhausted
            self._more.clear()
            await self._more.wait()

    async def close(self) -> None:
        self.closed = True
        # A real connection's pending ``recv()`` raises when it is closed;
        # a parked one that did not would outlive the session.
        self._hold_open = False
        self._more.set()

    # -- driving a test ----------------------------------------------------

    def push(self, *frames: Any) -> None:
        self._frames.extend(frames)
        self._more.set()

    def release(self) -> None:
        """Stop holding the session open: the next exhausted read is a close."""
        self._hold_open = False
        self._more.set()


class FakeConnect:
    """A :data:`corollary.sockets.SocketConnect` handing out scripted sockets.

    Records every URL and every header set it was called with, so the feed
    name in a URL and the credentials on a handshake are assertable. When the
    queue runs out it raises :class:`NoMoreSockets`, which the clients do not
    catch -- that is how a reconnect-loop test terminates without a timeout.
    """

    def __init__(self, *sockets: FakeSocket) -> None:
        self._sockets = list(sockets)
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    async def __call__(self, url: str, headers: Any) -> VendorSocket:
        self.urls.append(url)
        self.headers.append(dict(headers))
        if not self._sockets:
            raise NoMoreSockets(url)
        return self._sockets.pop(0)


class NoMoreSockets(RuntimeError):
    """The scripted connections ran out. Never raised in production."""


class SpySleep:
    """Records the backoff delays instead of waiting them out."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
