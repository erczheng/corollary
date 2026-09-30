"""``corollary/sockets.py``: the lifecycle, the shutdown orderings, the conversion.

The three Alpaca clients share one transport, and what they share is the part
of rule 9 that must never differ between them: which closes are *ours*, which
are the vendor's, and whether a socket can be left running when the engine
believes it is shut. Those are properties of this module and not of Alpaca, so
they are tested here against a concrete :class:`VendorStream` that speaks no
vendor protocol at all -- ``tests/data/providers/test_alpaca_stream.py`` and
``tests/engine/execution/test_trade_update_stream.py`` cover the protocols.

Every test carries ``@pytest.mark.risk``: a shutdown that leaves a socket
reading frames keeps ``record_message`` arriving on an engine that thinks its
feeds are closed, which suppresses rule 9's staleness condition, and a
handshake failure that escapes the reconnect loop takes the feed away for good
with no close recorded.

Nothing here binds a port and nothing here waits.
"""

import asyncio
import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
import websockets

from corollary.sockets import (
    JSON_CODEC,
    MAX_RECONNECT_DELAY_SECONDS,
    REPEATED_WARNING_INTERVAL,
    RepeatedWarning,
    SocketClosed,
    VendorSocket,
    VendorStream,
    connect_websocket,
    reconnect_delay,
)
from tests.sockets_support import Clock, FakeConnect, FakeSocket, SpyActivity

pytestmark = [pytest.mark.asyncio, pytest.mark.risk]


class ProbeStream(VendorStream):
    """The smallest concrete ``VendorStream``: a lifecycle and no protocol.

    Four abstract methods, answered as plainly as possible, so that a failure
    in these tests is a failure in the shared transport rather than in
    anybody's handshake.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.handled: list[Mapping[str, Any]] = []

    def _handshake_headers(self) -> dict[str, str]:
        return {"X-Probe": "yes"}

    def _auth_message(self) -> Mapping[str, Any]:
        return {"action": "auth"}

    def _messages(self, frame: str | bytes) -> list[Mapping[str, Any]]:
        decoded = self._codec.decode(frame)
        if isinstance(decoded, Mapping):
            return [decoded]
        return [item for item in decoded if isinstance(item, Mapping)]

    async def _handle(self, socket: VendorSocket, message: Mapping[str, Any]) -> None:
        self.handled.append(message)


def probe(
    *sockets: FakeSocket, sleep: Any = None, activity: SpyActivity | None = None
) -> tuple[ProbeStream, SpyActivity]:
    recorder = activity or SpyActivity()
    stream = ProbeStream(
        # Required, because rule 9 tracks liveness per socket and an
        # anonymous one would share a clock with the other two.
        name="probe",
        url="wss://example.invalid/probe",
        codec=JSON_CODEC,
        activity=recorder,
        connect=FakeConnect(*sockets),
        sleep=sleep,
        now=Clock(),
    )
    return stream, recorder


class HangingSocket:
    """A healthy, quiet socket: :meth:`recv` never returns on its own.

    This is the shape of every real market-data socket outside of a session --
    a connection with nothing to say -- and it is the shape that makes
    ``begin_close()`` on its own insufficient to end a read loop.
    """

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.closed = False
        self._gate = asyncio.Event()

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def recv(self) -> str | bytes:
        await self._gate.wait()
        raise SocketClosed("1006 (connection closed abnormally)")

    async def close(self) -> None:
        self.closed = True
        self._gate.set()


# --------------------------------------------------------------------------
# A shutdown that lands inside the connect window
# --------------------------------------------------------------------------


async def test_a_shutdown_inside_the_connect_window_closes_the_new_socket() -> None:
    """``aclose()`` during the handshake used to leave a socket nobody held.

    ``aclose()`` reads ``self._socket``, which is ``None`` while a connect is
    in flight, so it closed nothing and only set the intent flag.
    ``run_session`` then carried on: assign the socket, send auth, read frames
    forever. The result was a vendor connection with no reference to it
    anywhere in the process, calling ``record_message`` on every frame -- so
    an engine that believed its sockets were shut had rule 9's *staleness*
    condition suppressed by a socket it could not close -- while holding a
    vendor connection slot the next process may be refused for.

    And because ``_closing`` was set, the eventual close was attributed to us
    and recorded nowhere: a vendor close read as a shutdown, which is the
    worse direction of the two.
    """
    socket = FakeSocket([{"T": "never read"}], codec=JSON_CODEC)
    stream, recorder = probe(socket)

    class ShutdownDuringConnect(FakeConnect):
        async def __call__(self, url: str, headers: Any) -> VendorSocket:
            # The lifespan shutdown, landing in the TLS handshake.
            await stream.aclose()
            return await super().__call__(url, headers)

    stream._connect = ShutdownDuringConnect(socket)  # type: ignore[assignment]

    await stream.run_session()

    assert socket.closed is True, "the socket was left open and unreachable"
    assert socket.sent == [], "auth was sent on a socket we had already closed"
    assert stream.handled == []
    assert recorder.messages == []
    assert recorder.opens == []
    assert recorder.closes == []


async def test_a_shutdown_inside_the_connect_window_ends_the_reconnect_loop() -> None:
    """And the loop does not go round again: one socket opened, one closed."""
    first = FakeSocket([{"T": "never read"}], codec=JSON_CODEC)
    second = FakeSocket([{"T": "never read either"}], codec=JSON_CODEC)
    stream, recorder = probe(first, second)

    class ShutdownDuringConnect(FakeConnect):
        async def __call__(self, url: str, headers: Any) -> VendorSocket:
            await stream.aclose()
            return await super().__call__(url, headers)

    connect = ShutdownDuringConnect(first, second)
    stream._connect = connect  # type: ignore[assignment]

    await stream.run()

    assert len(connect.urls) == 1
    assert first.closed is True
    assert second.closed is False
    assert recorder.closes == []


# --------------------------------------------------------------------------
# A shutdown that lands in the backoff
# --------------------------------------------------------------------------


async def test_a_shutdown_during_the_backoff_is_not_waited_out() -> None:
    """A thirty-second delay must not become a thirty-second shutdown.

    ``aclose()`` sets the intent flag and closes the socket, but the reconnect
    loop was inside ``self._sleep(reconnect_delay(attempt))`` with no way to
    hear about it -- so a lifespan shutdown in late backoff blocked for up to
    :data:`MAX_RECONNECT_DELAY_SECONDS`. The wait is now abandoned as soon as
    a close is asked for, and ``run`` returns.
    """
    sleeping = asyncio.Event()

    async def slow_sleep(seconds: float) -> None:
        sleeping.set()
        await asyncio.sleep(MAX_RECONNECT_DELAY_SECONDS)

    dropped = FakeSocket([], codec=JSON_CODEC)
    later = FakeSocket([{"T": "never reached"}], codec=JSON_CODEC)
    stream, recorder = probe(dropped, later, sleep=slow_sleep)

    task = asyncio.get_running_loop().create_task(stream.run())
    await asyncio.wait_for(sleeping.wait(), timeout=1.0)
    await stream.aclose()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(recorder.closes) == 1, "the vendor's close is still theirs"
    assert later.closed is False, "the backoff was slept through and reconnected"


async def test_aclose_ends_a_read_loop_on_a_quiet_socket() -> None:
    """``run`` returns on ``aclose()``, which is what a shutdown must call.

    ``begin_close()`` alone cannot end this: it closes nothing, so a loop
    blocked in ``recv()`` on a healthy-but-silent socket stays blocked until
    the vendor says something. The docstrings say so now, and this is the
    call that does work -- the composition step cancels the task as well,
    because trusting either is how a shutdown hangs.
    """
    socket = HangingSocket()
    stream, recorder = probe()
    stream._connect = _handing_out(socket)  # type: ignore[assignment]

    task = asyncio.get_running_loop().create_task(stream.run())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await stream.aclose()
    await asyncio.wait_for(task, timeout=1.0)

    assert socket.closed is True
    assert recorder.closes == [], "our own shutdown is not a fault"


def _handing_out(socket: Any) -> Any:
    async def connect(url: str, headers: Any) -> Any:
        return socket

    return connect


# --------------------------------------------------------------------------
# The handshake failure classes, and the conversion contract
# --------------------------------------------------------------------------


async def test_the_handshake_failures_are_not_os_errors() -> None:
    """The vendor fact this conversion rests on, pinned.

    ``except OSError`` covers a refusal, DNS and TLS. It does **not** cover
    ``websockets``' own handshake failures, none of which subclass it on the
    locked version -- which is why the conversion catches
    ``WebSocketException`` as well. If a future ``websockets`` re-parents
    these, this fails and the comment above the conversion stops being true.
    """
    for name in ("InvalidHandshake", "InvalidMessage", "InvalidURI", "InvalidStatus"):
        failure = getattr(websockets, name)
        assert not issubclass(failure, OSError), name
        assert issubclass(failure, websockets.WebSocketException), name


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(OSError("[Errno 111] connection refused"), id="refused"),
        pytest.param(TimeoutError("handshake timed out"), id="timeout"),
        pytest.param(
            websockets.InvalidHandshake("no upgrade"), id="invalid-handshake"
        ),
        pytest.param(
            websockets.InvalidMessage("did not receive a valid HTTP response"),
            id="invalid-message",
        ),
        pytest.param(
            websockets.InvalidURI("wss://example.invalid", "bad uri"), id="invalid-uri"
        ),
        pytest.param(
            websockets.InvalidStatus(SimpleNamespace(status_code=403)),  # type: ignore[arg-type]
            id="invalid-status",
        ),
    ],
)
async def test_every_handshake_failure_becomes_a_socket_closed(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``connect_websocket``'s stated contract, over the whole taxonomy.

    ``InvalidMessage`` is the one that mattered: it is what ``websockets``
    raises when the server accepts TCP and then hangs up or answers with
    something that is not HTTP -- a captive portal, a corporate proxy, and the
    transport-level shape of a vendor refusing a surplus concurrent
    connection. Uncaught, it escaped ``run_session`` *and* ``run`` (which
    catches only :class:`SocketClosed`), so no close was recorded, the
    reconnect loop ended permanently, and the socket never came back even
    when the network did.
    """

    async def refuse(url: str, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr("corollary.sockets.open_websocket", refuse)

    with pytest.raises(SocketClosed):
        await connect_websocket("wss://example.invalid/probe", {})


async def test_a_handshake_failure_records_a_close_and_keeps_reconnecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path: the real conversion, the recorded close, the backoff.

    Driven through ``connect_websocket`` rather than through a hand-made
    :class:`SocketClosed`, because a test that injects the conversion's
    *output* proves the reconnect loop and says nothing about whether the
    conversion happens.
    """
    attempts = 0

    async def refuse(url: str, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts > 2:
            raise _StopProbing(url)
        raise websockets.InvalidMessage("did not receive a valid HTTP response")

    monkeypatch.setattr("corollary.sockets.open_websocket", refuse)

    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    stream, recorder = probe(sleep=record)
    stream._connect = connect_websocket  # type: ignore[assignment]

    with pytest.raises(_StopProbing):
        await stream.run()

    assert len(recorder.closes) == 2
    assert recorder.opens == []
    assert delays == [reconnect_delay(1), reconnect_delay(2)]


class _StopProbing(RuntimeError):
    """Ends a reconnect-loop test without a timeout. Never raised in production."""


# --------------------------------------------------------------------------
# RepeatedWarning: loud once, then counted -- never silent
# --------------------------------------------------------------------------
#
# 2026-09-29: 632 identical "unreadable quote" warnings in ~25 live minutes.
# A log that repeats one line six hundred times is a log nobody reads, which
# is its own way of being silent. These pin the replacement: the first
# occurrence of each rule is a full WARNING with its sample, the repeats are
# counted, and the count is always reported -- on the next occurrence past
# the interval, or on ``flush()`` when the socket's session ends.


def _throttle_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", "") == "probe_unreadable"]


def _throttle(clock: Clock) -> RepeatedWarning:
    return RepeatedWarning(
        logging.getLogger("tests.throttle"),
        event="probe_unreadable",
        summary="unreadable probe on the test stream",
        now=clock,
    )


async def test_the_first_occurrence_is_a_full_warning_with_its_sample(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    throttle = _throttle(clock)
    with caplog.at_level("WARNING"):
        throttle.warn("ProviderError", "unreadable: %s", "sample-1", extra={"symbol": "X"})
    [record] = _throttle_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "unreadable: sample-1"
    assert record.rule == "ProviderError"
    assert record.occurrence == "first"
    assert record.symbol == "X"


async def test_repeats_inside_the_interval_are_counted_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    throttle = _throttle(clock)
    with caplog.at_level("WARNING"):
        for index in range(100):
            clock.advance(0.5)
            throttle.warn("ProviderError", "unreadable: %s", f"s{index}", extra={})
    assert len(_throttle_records(caplog)) == 1


async def test_the_next_repeat_past_the_interval_reports_the_count_and_latest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    throttle = _throttle(clock)
    with caplog.at_level("WARNING"):
        throttle.warn("ProviderError", "unreadable: %s", "first", extra={})
        for index in range(98):
            clock.advance(0.5)
            throttle.warn("ProviderError", "unreadable: %s", f"s{index}", extra={})
        clock.advance(REPEATED_WARNING_INTERVAL.total_seconds())
        throttle.warn("ProviderError", "unreadable: %s", "latest", extra={"symbol": "Y"})
    first, summary = _throttle_records(caplog)
    assert summary.levelno == logging.WARNING
    assert summary.occurrence == "aggregate"
    assert summary.suppressed == 99  # 98 inside the window, plus this one
    assert summary.rule == "ProviderError"
    assert summary.symbol == "Y"
    assert "99 more" in summary.getMessage()
    assert "unreadable: latest" in summary.getMessage()


async def test_flush_reports_a_pending_count_and_is_quiet_with_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    throttle = _throttle(clock)
    with caplog.at_level("WARNING"):
        throttle.flush()
        assert _throttle_records(caplog) == []
        throttle.warn("ProviderError", "unreadable: %s", "a", extra={})
        throttle.flush()  # the first was already loud; nothing pending
        assert len(_throttle_records(caplog)) == 1
        # A flush closes the window (a session ended), so the next occurrence
        # is a new episode and loud again; the one after it is counted.
        throttle.warn("ProviderError", "unreadable: %s", "b", extra={})
        throttle.warn("ProviderError", "unreadable: %s", "c", extra={})
        throttle.warn("ProviderError", "unreadable: %s", "d", extra={})
        throttle.flush()
        throttle.flush()  # a count is reported exactly once
    records = _throttle_records(caplog)
    assert [r.occurrence for r in records] == ["first", "first", "aggregate"]
    assert records[2].suppressed == 2
    assert "unreadable: d" in records[2].getMessage()


async def test_a_different_rule_is_loud_on_its_own_first_occurrence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A new kind of failure must not hide behind an old one's count."""
    clock = Clock()
    throttle = _throttle(clock)
    with caplog.at_level("WARNING"):
        throttle.warn("ProviderError", "unreadable: %s", "a", extra={})
        throttle.warn("ProviderError", "unreadable: %s", "b", extra={})
        throttle.warn("TypeError", "unreadable: %s", "c", extra={})
    records = _throttle_records(caplog)
    assert [(r.rule, r.occurrence) for r in records] == [
        ("ProviderError", "first"),
        ("TypeError", "first"),
    ]


async def test_after_a_quiet_interval_the_next_occurrence_is_loud_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    throttle = _throttle(clock)
    with caplog.at_level("WARNING"):
        throttle.warn("ProviderError", "unreadable: %s", "a", extra={})
        clock.advance(REPEATED_WARNING_INTERVAL.total_seconds() + 1)
        throttle.warn("ProviderError", "unreadable: %s", "b", extra={})
    records = _throttle_records(caplog)
    assert [r.occurrence for r in records] == ["first", "first"]
    assert records[1].getMessage() == "unreadable: b"


async def test_report_due_reports_a_pending_count_once_its_window_has_passed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A burst's tail is reported by time, not by the fault recurring.

    Before ``report_due`` the only reporters were the next occurrence past
    the interval and ``flush()``: a thirty-second burst at 10:00 was counted
    and then said nothing until the fault next happened or the session ended,
    which could be hours.
    """
    clock = Clock()
    throttle = _throttle(clock)
    with caplog.at_level("WARNING"):
        throttle.report_due()  # nothing open: nothing to say
        throttle.warn("ProviderError", "unreadable: %s", "a", extra={})
        throttle.warn("ProviderError", "unreadable: %s", "b", extra={})
        throttle.warn("ProviderError", "unreadable: %s", "c", extra={"symbol": "Z"})
        clock.advance(REPEATED_WARNING_INTERVAL.total_seconds() - 1)
        throttle.report_due()  # the window is still open
        assert len(_throttle_records(caplog)) == 1
        clock.advance(1)
        throttle.report_due()
        throttle.report_due()  # a count is reported exactly once
        throttle.flush()  # and flush does not report it a second time
    records = _throttle_records(caplog)
    assert [r.occurrence for r in records] == ["first", "aggregate"]
    assert records[1].suppressed == 2
    assert records[1].symbol == "Z"
    assert "unreadable: c" in records[1].getMessage()


async def test_report_due_leaves_a_quiet_window_to_expire_and_the_next_is_loud(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    throttle = _throttle(clock)
    with caplog.at_level("WARNING"):
        throttle.warn("ProviderError", "unreadable: %s", "a", extra={})
        clock.advance(REPEATED_WARNING_INTERVAL.total_seconds())
        throttle.report_due()  # nothing pending: nothing to report
        throttle.warn("ProviderError", "unreadable: %s", "b", extra={})
    records = _throttle_records(caplog)
    assert [r.occurrence for r in records] == ["first", "first"]
