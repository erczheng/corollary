"""The two market-data websockets: connect, subscribe, translate, publish.

``AlpacaQuoteStream`` is the only thing in the tree that opens a market-data
socket, and it is the producer for one of rule 9's two halt conditions. So
these tests cover the protocol (auth, subscribe, acknowledge, error), the
translation (vendor message to :class:`Quote` to frame), and the restraint
(what it must *not* record).

Nothing here binds a port and nothing here sleeps: ``tests/sockets_support.py``
holds the doubles, and the delay is injected.
"""

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Any

import pytest

from corollary.api.fanout import Fanout, quote_sink
from corollary.api.schemas import WsQuoteFrame
import corollary.data.providers.alpaca as alpaca_provider
from corollary.data.providers.alpaca import (
    FATAL_STREAM_CODES,
    OPTION_STREAM_URL_TEMPLATE,
    QUOTES_CHANNEL,
    STOCK_STREAM_URL_TEMPLATE,
    AlpacaCredentials,
    AlpacaQuoteStream,
    StreamProtocolError,
    option_quote_stream,
    stock_quote_stream,
)
from corollary.data.providers.interface import Quote
from corollary.engine.stream import (
    Stream,
    contract_unit,
    plan_subscriptions,
    underlying_unit,
)
from corollary.sockets import JSON_CODEC, MSGPACK_CODEC, SocketClosed
from corollary.wire import ERROR_BODY_MAX
from tests.sockets_support import (
    T0,
    Clock,
    FakeConnect,
    FakeSocket,
    NoMoreSockets,
    SpyActivity,
    SpySleep,
)

pytestmark = pytest.mark.asyncio

KEY = "AKFAKEKEYFORTESTSONLY000"
SECRET = "STREAMnotarealsecretSTREAMnotareal000000"

ENV = {
    "ALPACA_OPTIONS_FEED": "indicative",
    "ALPACA_STOCK_FEED_HISTORICAL": "sip",
    "ALPACA_STOCK_FEED_REALTIME": "iex",
    "ALPACA_PAPER_API_KEY": KEY,
    "ALPACA_PAPER_SECRET_KEY": SECRET,
}

CONTRACT = "AAPL241220C00150000"
OTHER_CONTRACT = "NVDA241220C00500000"

CONNECTED = [{"T": "success", "msg": "connected"}]
AUTHENTICATED = [{"T": "success", "msg": "authenticated"}]


def option_plan(*symbols: str, cap: int = 200) -> Any:
    units = [contract_unit(f"pos-{index}", [symbol]) for index, symbol in enumerate(symbols)]
    return plan_subscriptions(
        units, at=T0, correlation_id="cid-1", cap=cap, stream=Stream.OPTION
    )


def equity_plan(*symbols: str, cap: int = 30) -> Any:
    return plan_subscriptions(
        [underlying_unit(symbol) for symbol in symbols],
        at=T0,
        correlation_id="cid-1",
        cap=cap,
        stream=Stream.EQUITY,
    )


def quote_frame(symbol: str = CONTRACT, *, bid: float = 1.24) -> dict[str, Any]:
    return {
        "T": "q",
        "S": symbol,
        "bx": "C",
        "bp": bid,
        "bs": 4,
        "ax": "C",
        "ap": 1.34,
        "as": 5,
        "c": "B",
        "t": "2026-09-14T13:30:01.123456789Z",
    }


def subscription(*symbols: str) -> dict[str, Any]:
    return {
        "T": "subscription",
        "trades": [],
        "quotes": list(symbols),
        "bars": [],
    }


def build(
    socket_frames: list[Any],
    *,
    plan: Any,
    codec: Any = MSGPACK_CODEC,
    stream: Stream = Stream.OPTION,
    activity: SpyActivity | None = None,
    on_quote: Any = None,
    hold_open: bool = False,
) -> tuple[AlpacaQuoteStream, FakeSocket, SpyActivity, list[Quote]]:
    recorder = activity or SpyActivity()
    received: list[Quote] = []
    sink = on_quote if on_quote is not None else received.append
    socket = FakeSocket(socket_frames, codec=codec, hold_open=hold_open)
    client = AlpacaQuoteStream(
        credentials=AlpacaCredentials(
            key_id=KEY, secret_key=SECRET, trading_base_url="https://paper", is_paper=True
        ),
        url="wss://stream.data.alpaca.markets/v1beta1/indicative",
        codec=codec,
        stream=stream,
        plan=plan,
        activity=recorder,
        on_quote=sink,
        connect=FakeConnect(socket),
        sleep=SpySleep(),
        now=Clock(),
    )
    return client, socket, recorder, received


async def settle(predicate: Any, *, what: str, turns: int = 200) -> None:
    """Hand the loop back until ``predicate`` holds. Never sleeps for real."""
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")

# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------


async def test_the_option_stream_authenticates_then_subscribes_from_the_plan() -> None:
    """Two frames out, in that order, and never a raw symbol list.

    The subscribe carries :attr:`SubscriptionPlan.subscribed` -- what the
    budget admitted -- rather than everything the caller wanted, which is the
    whole point of planning it first.
    """
    plan = option_plan(CONTRACT, OTHER_CONTRACT)
    client, socket, _, _ = build(
        [CONNECTED, AUTHENTICATED, subscription(CONTRACT, OTHER_CONTRACT)],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert socket.sent == [
        {"action": "auth", "key": KEY, "secret": SECRET},
        {"action": "subscribe", "quotes": [CONTRACT, OTHER_CONTRACT]},
    ]


async def test_the_socket_transmits_nothing_but_auth_and_subscribe() -> None:
    """The positive form of ``tests/test_hard_rules.py``'s write-verb guard.

    The guard treats ``.send()`` on the vendor surface as a write verb, and
    the transport's methods are ``send_text``/``send_bytes`` so it can stay
    about HTTP verbs. This is what replaces it: whatever the vendor says, the
    only two actions this client ever transmits are ``auth`` and
    ``subscribe``.

    The error frame is a 500 because it has to be one the client *survives*:
    500 is the vendor's own internal error, which is exactly what a reconnect
    is for, so the session carries on and the transmitted list is complete.
    This used to use 407, which is now in ``FATAL_STREAM_CODES`` -- it recurs
    identically on reconnect -- and a fatal code ends the session before the
    list is worth reading.
    """
    plan = option_plan(CONTRACT)
    client, socket, _, _ = build(
        [
            CONNECTED,
            AUTHENTICATED,
            subscription(CONTRACT),
            [quote_frame()],
            [{"T": "error", "code": 500, "msg": "internal error"}],
        ],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert {frame["action"] for frame in socket.sent} == {"auth", "subscribe"}


async def test_the_option_stream_speaks_msgpack_and_the_stock_stream_json() -> None:
    """msgpack is not configuration: the option stream has no other format."""
    option = option_quote_stream(
        plan=option_plan(CONTRACT),
        activity=SpyActivity(),
        on_quote=lambda quote: None,
        env=ENV,
    )
    stock = stock_quote_stream(
        plan=equity_plan("AAPL"),
        activity=SpyActivity(),
        on_quote=lambda quote: None,
        env=ENV,
    )
    assert option.codec is MSGPACK_CODEC
    assert stock.codec is JSON_CODEC


async def test_the_feed_in_each_url_comes_from_the_environment() -> None:
    """Feed names are configuration, read only in this module. Never literals."""
    env = dict(ENV, ALPACA_OPTIONS_FEED="opra", ALPACA_STOCK_FEED_REALTIME="sip")
    option = option_quote_stream(
        plan=option_plan(CONTRACT),
        activity=SpyActivity(),
        on_quote=lambda quote: None,
        env=env,
    )
    stock = stock_quote_stream(
        plan=equity_plan("AAPL"),
        activity=SpyActivity(),
        on_quote=lambda quote: None,
        env=env,
    )
    assert option.url == OPTION_STREAM_URL_TEMPLATE.format(feed="opra")
    assert stock.url == STOCK_STREAM_URL_TEMPLATE.format(feed="sip")
    assert "v1beta1" in option.url and "v2" in stock.url


async def test_the_default_feeds_are_the_basic_plans() -> None:
    option = option_quote_stream(
        plan=option_plan(CONTRACT),
        activity=SpyActivity(),
        on_quote=lambda quote: None,
        env=ENV,
    )
    stock = stock_quote_stream(
        plan=equity_plan("AAPL"),
        activity=SpyActivity(),
        on_quote=lambda quote: None,
        env=ENV,
    )
    assert option.url.endswith("/indicative")
    assert stock.url.endswith("/iex")


async def test_an_empty_plan_subscribes_to_nothing_rather_than_to_everything() -> None:
    """A star subscription is refused for option quotes, so an empty list is empty.

    An empty book is an ordinary state -- no positions, nothing visible. The
    failure to avoid is sending ``{"action": "subscribe", "quotes": []}``,
    which Alpaca answers with a 400, or worse inventing a ``"*"``, which it
    refuses for option quotes because there are hundreds of thousands of them.
    """
    plan = option_plan()
    client, socket, _, _ = build([CONNECTED, AUTHENTICATED], plan=plan)
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert socket.sent == [{"action": "auth", "key": KEY, "secret": SECRET}]


# --------------------------------------------------------------------------
# Translation, and the fan-out
# --------------------------------------------------------------------------


async def test_a_quote_is_translated_into_a_domain_quote_with_decimal_money() -> None:
    plan = option_plan(CONTRACT)
    client, _, _, received = build(
        [CONNECTED, AUTHENTICATED, subscription(CONTRACT), [quote_frame()]], plan=plan
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert len(received) == 1
    quote = received[0]
    assert quote.symbol == CONTRACT
    assert quote.bid == Decimal("1.24")
    assert quote.ask == Decimal("1.34")
    assert isinstance(quote.bid, Decimal)
    assert quote.bid_size == 4
    assert quote.at.isoformat() == "2026-09-14T13:30:01.123456+00:00"


async def test_a_zero_side_is_no_bid_rather_than_a_bid_of_nothing() -> None:
    plan = option_plan(CONTRACT)
    frame = quote_frame()
    frame["bp"] = 0
    client, _, _, received = build(
        [CONNECTED, AUTHENTICATED, subscription(CONTRACT), [frame]], plan=plan
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert received[0].bid is None
    assert received[0].mid is None


async def test_a_quote_reaches_every_fanout_client_as_one_frame() -> None:
    """Vendor message, domain object, frame, ``publish``. One hub, no second one."""
    fanout = Fanout()
    subscriber = fanout.subscribe()
    plan = option_plan(CONTRACT)
    client, _, _, _ = build(
        [CONNECTED, AUTHENTICATED, subscription(CONTRACT), [quote_frame()]],
        plan=plan,
        on_quote=quote_sink(fanout),  # the composition root's wiring
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    frame = await subscriber.next_frame()
    assert isinstance(frame, WsQuoteFrame)
    assert frame.quote.symbol == CONTRACT
    assert frame.quote.bid == Decimal("1.24")
    assert frame.quote.at.isoformat() == "2026-09-14T13:30:01.123456+00:00"


async def test_a_malformed_quote_does_not_take_the_feed_down() -> None:
    """One unreadable message is logged and skipped; the next one still arrives.

    A vendor field change must not cost every other symbol its mark.
    """
    plan = option_plan(CONTRACT)
    broken = quote_frame()
    del broken["t"]
    client, _, _, received = build(
        [
            CONNECTED,
            AUTHENTICATED,
            subscription(CONTRACT),
            [broken],
            [quote_frame(bid=1.30)],
        ],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert [quote.bid for quote in received] == [Decimal("1.30")]


# --------------------------------------------------------------------------
# Acknowledgement reconciliation, and the 405
# --------------------------------------------------------------------------


async def test_fewer_symbols_acknowledged_than_subscribed_counts_as_not_streamed() -> None:
    plan = option_plan(CONTRACT, OTHER_CONTRACT)
    client, _, _, _ = build(
        [CONNECTED, AUTHENTICATED, subscription(CONTRACT)], plan=plan
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert client.not_streamed == 1
    assert client.message == "1 symbol not streamed"
    ack = client.acknowledgement
    assert ack is not None
    assert ack.channel == QUOTES_CHANNEL
    assert ack.absent == (OTHER_CONTRACT,)


async def test_a_405_lowers_the_effective_cap_and_re_plans() -> None:
    """The server's refusal is authoritative: re-plan, do not re-send the same list."""
    plan = option_plan(CONTRACT, OTHER_CONTRACT, cap=2)
    client, socket, _, _ = build(
        [
            CONNECTED,
            AUTHENTICATED,
            [
                {
                    "T": "error",
                    "code": 405,
                    "msg": "symbol subscription request would put you over the limit",
                }
            ],
            subscription(CONTRACT),
        ],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert socket.sent == [
        {"action": "auth", "key": KEY, "secret": SECRET},
        {"action": "subscribe", "quotes": [CONTRACT, OTHER_CONTRACT]},
        {"action": "subscribe", "quotes": [CONTRACT]},
    ]
    assert client.plan.cap == 1
    assert client.not_streamed == 1


async def test_a_405_after_an_acknowledgement_takes_the_servers_own_figure() -> None:
    """It acknowledged three; that *is* the cap, and it is not a guess."""
    plan = option_plan(CONTRACT, OTHER_CONTRACT, "TSLA241220C00250000", cap=200)
    client, socket, _, _ = build(
        [
            CONNECTED,
            AUTHENTICATED,
            subscription(CONTRACT, OTHER_CONTRACT),
            [{"T": "error", "code": 405, "msg": "over the limit"}],
        ],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert client.plan.cap == 2


async def test_a_cap_correction_never_raises_the_cap() -> None:
    plan = option_plan(CONTRACT, cap=1)
    client, _, _, _ = build(
        [
            CONNECTED,
            AUTHENTICATED,
            [{"T": "error", "code": 405, "msg": "over the limit"}],
        ],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert client.plan.cap == 0
    assert client.not_streamed == 1


async def test_a_405_correction_is_logged_with_the_rule_and_the_inputs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = option_plan(CONTRACT, OTHER_CONTRACT, cap=2)
    client, _, _, _ = build(
        [
            CONNECTED,
            AUTHENTICATED,
            [{"T": "error", "code": 405, "msg": "over the limit"}],
        ],
        plan=plan,
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(SocketClosed):
            await client.run_session()
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "stream_cap_corrected"
    ]
    assert len(events) == 1
    assert events[0].code == 405  # type: ignore[attr-defined]
    assert events[0].cap == 1  # type: ignore[attr-defined]
    assert events[0].previous_cap == 2  # type: ignore[attr-defined]


async def test_an_authentication_failure_is_fatal_rather_than_retried() -> None:
    """A wrong key is not a transient condition, and a retry loop hides it."""
    plan = option_plan(CONTRACT)
    client, _, recorder, _ = build(
        [CONNECTED, [{"T": "error", "code": 402, "msg": "auth failed"}]], plan=plan
    )
    with pytest.raises(StreamProtocolError) as raised:
        await client.run_session()
    assert raised.value.code == 402
    # It is still a lost feed: rule 9's condition is recorded either way.
    assert len(recorder.closes) == 1


async def test_a_vendor_error_never_quotes_a_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 6, at the one boundary that interpolates vendor text.

    Redaction runs before truncation, in ``wire.vendor_detail``; what this
    pins is that the vendor's words reach the log *through* it.
    """
    plan = option_plan(CONTRACT)
    client, _, _, _ = build(
        [
            CONNECTED,
            [{"T": "error", "code": 402, "msg": f"auth failed for key {KEY} / {SECRET}"}],
        ],
        plan=plan,
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(StreamProtocolError) as raised:
            await client.run_session()

    assert KEY not in str(raised.value)
    assert SECRET not in str(raised.value)
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.__dict__)
        assert KEY not in rendered
        assert SECRET not in rendered


# --------------------------------------------------------------------------
# Closes, ours and theirs
# --------------------------------------------------------------------------


async def test_a_vendor_close_records_a_stream_close() -> None:
    plan = option_plan(CONTRACT)
    client, _, recorder, _ = build(
        [CONNECTED, AUTHENTICATED, SocketClosed("1006 abnormal closure")], plan=plan
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert len(recorder.closes) == 1
    assert "1006" in recorder.closes[0][1]


async def test_a_graceful_vendor_close_is_still_their_close() -> None:
    """A polite hangup is still a lost feed. 1000 is not an exemption."""
    plan = option_plan(CONTRACT)
    client, _, recorder, _ = build(
        [CONNECTED, AUTHENTICATED, SocketClosed("1000 going away", code=1000)],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert len(recorder.closes) == 1


async def test_our_own_close_records_nothing() -> None:
    """Shutdown is not a fault, and the flag is set before the socket is closed.

    That ordering *is* the distinction: ``aclose`` marks the intent first, so
    every close observed afterwards is ours by construction rather than by
    guessing at a close code.
    """
    plan = option_plan(CONTRACT)
    activity = SpyActivity()
    closing: list[Any] = []
    socket = FakeSocket(
        [CONNECTED, AUTHENTICATED, subscription(CONTRACT)], codec=MSGPACK_CODEC
    )
    client = AlpacaQuoteStream(
        credentials=AlpacaCredentials(
            key_id=KEY, secret_key=SECRET, trading_base_url="https://paper", is_paper=True
        ),
        url="wss://stream.data.alpaca.markets/v1beta1/indicative",
        codec=MSGPACK_CODEC,
        stream=Stream.OPTION,
        plan=plan,
        activity=activity,
        on_quote=lambda quote: None,
        connect=FakeConnect(socket),
        sleep=SpySleep(),
        now=Clock(),
    )
    socket.push(lambda: closing.append(client.begin_close()))
    await client.run_session()

    assert closing == [None]
    assert activity.closes == []
    assert socket.closed is True


async def test_run_reconnects_after_a_vendor_close_and_backs_off() -> None:
    plan = option_plan(CONTRACT)
    activity = SpyActivity()
    sleeper = SpySleep()
    first = FakeSocket([CONNECTED, AUTHENTICATED, SocketClosed("1006")], codec=MSGPACK_CODEC)
    second = FakeSocket([CONNECTED, AUTHENTICATED, SocketClosed("1006")], codec=MSGPACK_CODEC)
    connect = FakeConnect(first, second)
    client = AlpacaQuoteStream(
        credentials=AlpacaCredentials(
            key_id=KEY, secret_key=SECRET, trading_base_url="https://paper", is_paper=True
        ),
        url="wss://stream.data.alpaca.markets/v1beta1/indicative",
        codec=MSGPACK_CODEC,
        stream=Stream.OPTION,
        plan=plan,
        activity=activity,
        on_quote=lambda quote: None,
        connect=connect,
        sleep=sleeper,
        now=Clock(),
    )
    with pytest.raises(NoMoreSockets):
        await client.run()

    assert len(connect.urls) == 3
    assert sleeper.slept == [1.0, 2.0]
    assert len(activity.closes) == 2
    assert len(activity.opens) == 2


async def test_a_connection_that_cannot_be_opened_leaves_the_process_alive() -> None:
    """Rule: the app must keep serving when Alpaca is unreachable.

    A refused connection is a :class:`SocketClosed` from ``connect``, so it
    goes down the same backoff path as a dropped one -- no exception escapes
    into whatever supervises the socket.
    """
    plan = option_plan(CONTRACT)

    class RefusingConnect:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, url: str, headers: Any) -> Any:
            self.calls += 1
            if self.calls > 2:
                raise NoMoreSockets(url)
            raise SocketClosed("could not connect: [Errno 111] refused")

    sleeper = SpySleep()
    activity = SpyActivity()
    client = AlpacaQuoteStream(
        credentials=AlpacaCredentials(
            key_id=KEY, secret_key=SECRET, trading_base_url="https://paper", is_paper=True
        ),
        url="wss://stream.data.alpaca.markets/v1beta1/indicative",
        codec=MSGPACK_CODEC,
        stream=Stream.OPTION,
        plan=plan,
        activity=activity,
        on_quote=lambda quote: None,
        connect=RefusingConnect(),
        sleep=sleeper,
        now=Clock(),
    )
    with pytest.raises(NoMoreSockets):
        await client.run()
    assert len(activity.closes) == 2
    assert activity.opens == []


# --------------------------------------------------------------------------
# What the socket reports to the watchdog, and what it does not
# --------------------------------------------------------------------------


async def test_every_vendor_frame_records_a_message() -> None:
    plan = option_plan(CONTRACT)
    client, _, recorder, _ = build(
        [CONNECTED, AUTHENTICATED, subscription(CONTRACT), [quote_frame()]], plan=plan
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert len(recorder.messages) == 4
    assert recorder.messages == [T0, T0, T0, T0]


async def test_the_stream_open_is_recorded_only_once_authenticated() -> None:
    """An open socket that has not authenticated is not a feed.

    ``record_stream_open`` is what stops the watchdog complaining about a
    closed socket, so recording it on a connection that then fails auth would
    quiet the switch on a stream carrying nothing.
    """
    plan = option_plan(CONTRACT)
    client, _, recorder, _ = build(
        [CONNECTED, [{"T": "error", "code": 402, "msg": "auth failed"}]], plan=plan
    )
    with pytest.raises(StreamProtocolError):
        await client.run_session()
    assert recorder.opens == []


# --------------------------------------------------------------------------
# Rule 6 on the quote path, and what is contained around the sink
# --------------------------------------------------------------------------


async def test_an_unreadable_quote_is_logged_through_the_redactor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 6: the log *message* is scrubbed too, not only ``extra["detail"]``.

    This record carried the raw exception in its format args while scrubbing
    the same text into ``extra["detail"]`` -- and the scrubbed copy sitting on
    the same record made it read as intentional. No leak is constructible
    from a quote payload today, but the *bound* was genuinely gone: the
    exception interpolates the whole vendor message with ``{message!r}``, so
    an unbounded frame reached the log in full.

    ``VendorStream._detail`` states the invariant -- everything
    vendor-derived that reaches a log record or an exception message in a
    stream client goes through it -- so both halves of this record do.
    """
    plan = option_plan(CONTRACT)
    payload = {
        "T": "q",
        "bp": 1.24,
        "note": f"{KEY} {SECRET} " + "padding-" * 200,
    }
    client, _, _, _ = build([CONNECTED, AUTHENTICATED, [payload]], plan=plan)
    with caplog.at_level("WARNING"):
        with pytest.raises(SocketClosed):
            await client.run_session()

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "stream_quote_unreadable"
    ]
    assert len(records) == 1
    rendered = records[0].getMessage()
    assert KEY not in rendered
    assert SECRET not in rendered
    assert len(rendered) <= ERROR_BODY_MAX + 80, "the vendor message is unbounded"


async def test_a_sink_that_raises_costs_one_quote_and_not_the_socket(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sink is inside the containment, where the docstring always said.

    It was called *outside* the ``try``, so anything it raised propagated out
    of ``run()``: socket dead, **no** ``record_stream_closed``, no reconnect
    -- rule 9's condition lost rather than fired, which is the one direction
    that leaves the engine believing the feed is fine. Today's sinks cannot
    raise, but ``api/fanout.py``'s ``wire_action`` is a ``dict`` lookup mypy
    will not check for exhaustiveness, so a fifth ``PositionIntent`` member
    turns a fill into a ``KeyError`` here, and the composition root is where
    a sink grows a body.
    """
    delivered: list[Quote] = []

    def sink(quote: Quote) -> None:
        delivered.append(quote)
        if len(delivered) == 1:
            raise KeyError("wire_action has no branch for this PositionIntent")

    plan = option_plan(CONTRACT)
    client, _, recorder, _ = build(
        [
            CONNECTED,
            AUTHENTICATED,
            subscription(CONTRACT),
            [quote_frame()],
            [quote_frame(bid=1.30)],
        ],
        plan=plan,
        on_quote=sink,
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(SocketClosed):
            await client.run_session()

    assert len(delivered) == 2, "one bad publish cost every later symbol its mark"
    assert len(recorder.closes) == 1, "the close never reached the watchdog"
    failures = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "stream_sink_failed"
    ]
    assert len(failures) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_a_non_finite_price_costs_one_quote_and_not_the_socket(
    value: float,
) -> None:
    """``Decimal('NaN')`` is not a price, and comparing it raised.

    ``decode_msgpack`` converts a non-finite float64 to ``Decimal('NaN')`` or
    ``Decimal('Infinity')`` *successfully* -- neither exact nor comparable --
    and ``_price_or_none``'s ``parsed <= 0`` then raised
    ``InvalidOperation``, an ``ArithmeticError``, which is in neither
    ``_publish``'s catch nor anything above it. So one malformed price took
    the whole socket down through the path fix 6 closes.

    Refused twice over now. ``decode_msgpack`` refuses the *frame* before
    ``_price_or_none`` is ever reached -- see ``corollary.wire._decimalise``,
    which is where a non-finite ``Decimal`` stopped being constructible at
    all -- and the price guard stays as the inner one. Either way the cost is
    the message and never the socket, which is what this asserts.
    """
    plan = option_plan(CONTRACT)
    bad = quote_frame()
    bad["bp"] = value
    client, _, _, received = build(
        [
            CONNECTED,
            AUTHENTICATED,
            subscription(CONTRACT),
            [bad],
            [quote_frame(bid=1.30)],
        ],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert len(received) == 1
    assert received[0].bid == Decimal("1.30")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["bs", "as"])
async def test_a_non_finite_size_costs_the_frame_and_not_the_socket(
    field: str, value: float
) -> None:
    """The half that escaped: sizes, not prices.

    ``bs`` and ``as`` go through ``as_int``, and the two non-finite cases
    diverged there. ``int(Decimal('NaN'))`` raises ``ValueError``, which
    ``_publish`` catches, so the path *looked* covered;
    ``int(Decimal('Infinity'))`` raises ``OverflowError``, which it did not,
    and that escaped ``_publish``, ``_handle``, ``run_session`` and ``run``
    -- every one of which catches only ``SocketClosed``. One frame carrying
    ``{"bs": inf}`` and the option feed was gone permanently with no close
    recorded and no reconnect attempted.
    """
    bad = quote_frame()
    bad[field] = value
    client, _, recorder, received = build(
        [
            CONNECTED,
            AUTHENTICATED,
            subscription(CONTRACT),
            [bad],
            [quote_frame(bid=1.30)],
        ],
        plan=option_plan(CONTRACT),
    )
    # The socket ran to the end of its script and reported the close *itself*
    # -- which is the assertion. An escape returned out of `run_session`
    # having recorded nothing.
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert len(received) == 1, "a later symbol must still be marked"
    assert received[0].bid == Decimal("1.30")
    assert len(recorder.closes) == 1, "rule 9's close condition still recorded"


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
async def test_a_non_finite_frame_is_logged_as_undecodable(
    value: float, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 8 on a dropped frame: the rule, the inputs, the timestamp.

    The refusal is a whole-frame one, because that is where a non-finite can
    be refused before anything builds a ``Decimal`` out of it. A silently
    dropped frame is the failure mode this log line exists to rule out.
    """
    bad = quote_frame()
    bad["bs"] = value
    client, _, _, _ = build(
        [CONNECTED, AUTHENTICATED, subscription(CONTRACT), [bad]],
        plan=option_plan(CONTRACT),
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(SocketClosed):
            await client.run_session()

    dropped = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "stream_frame_undecodable"
    ]
    assert len(dropped) == 1
    assert "not a finite number" in getattr(dropped[0], "detail", "")


@pytest.mark.parametrize(
    "error",
    [
        OverflowError("cannot convert Infinity to integer"),
        InvalidOperation("comparison involving NaN"),
        ZeroDivisionError("a derived figure divided by nothing"),
    ],
)
async def test_an_arithmetic_error_in_the_translation_costs_one_quote(
    error: ArithmeticError, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth, because the decode boundary is one line of defence.

    Every exception here is an ``ArithmeticError``, and the family is what the
    catch names now: ``OverflowError`` was the escape, ``InvalidOperation``
    was the one before it, and the next derived figure somebody computes in
    ``_quote`` is the third. A bad field must cost one symbol its mark and
    never the whole feed.
    """
    real = alpaca_provider._quote

    def refusing(symbol: str, payload: Any) -> Any:
        if symbol == CONTRACT:
            raise error
        return real(symbol, payload)

    monkeypatch.setattr(alpaca_provider, "_quote", refusing)
    client, _, recorder, received = build(
        [
            CONNECTED,
            AUTHENTICATED,
            subscription(CONTRACT),
            [quote_frame()],
            [quote_frame(symbol=OTHER_CONTRACT)],
        ],
        plan=option_plan(CONTRACT, OTHER_CONTRACT),
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert [quote.symbol for quote in received] == [OTHER_CONTRACT]
    assert len(recorder.closes) == 1


# --------------------------------------------------------------------------
# The refusals that recur identically on reconnect
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message"),
    [
        pytest.param(406, "connection limit exceeded", id="406-connection-limit"),
        pytest.param(407, "slow client", id="407-slow-client"),
    ],
)
async def test_a_refusal_that_recurs_on_reconnect_is_fatal(
    code: int, message: str
) -> None:
    """``FATAL_STREAM_CODES``' own criterion, applied to the two it omitted.

    406 is the account's concurrent-connection limit and 407 is this client
    reading too slowly; both recur identically on reconnect, so a backoff
    loop turns a stated problem into a silent one. 406 in particular is what
    a second process on the same key produces, and looping on it forever
    looks exactly like a flaky network.
    """
    plan = option_plan(CONTRACT)
    client, _, recorder, _ = build(
        [CONNECTED, [{"T": "error", "code": code, "msg": message}]], plan=plan
    )
    with pytest.raises(StreamProtocolError) as raised:
        await client.run_session()
    assert raised.value.code == code
    assert code in FATAL_STREAM_CODES
    # Still a lost feed: rule 9's condition is recorded either way.
    assert len(recorder.closes) == 1


async def test_a_405_does_not_count_a_surplus_acknowledgement_as_room() -> None:
    """The server's figure, minus what we never asked for.

    ``_corrected_cap`` was fed ``len(acknowledged)``, which includes the
    *surplus* -- symbols the server acknowledged that are not in our plan --
    so the corrected cap could overstate our room by the surplus count. It
    still converges and can never widen, but ``surplus`` exists precisely to
    name this case, so it is subtracted.
    """
    plan = option_plan(CONTRACT, OTHER_CONTRACT, cap=200)
    client, _, _, _ = build(
        [
            CONNECTED,
            AUTHENTICATED,
            subscription(CONTRACT, "SPY241220C00500000"),
            [{"T": "error", "code": 405, "msg": "over the limit"}],
        ],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert client.plan.cap == 1


async def test_a_re_plan_gets_a_correlation_id_of_its_own() -> None:
    """Two contradictory ``not_streamed`` summaries cannot share one handle.

    ``correlation_ids`` defaults to ``None`` in both factories, so the
    fallback *is* the production path -- and it reused the original plan's id,
    leaving the pre-405 summary and the post-405 summary under one
    correlation id with nothing to order them by. The fallback keeps the
    original as a prefix, so the scan is still findable, and numbers the
    re-plans.
    """
    plan = option_plan(CONTRACT, OTHER_CONTRACT, cap=2)
    client, _, _, _ = build(
        [
            CONNECTED,
            AUTHENTICATED,
            [{"T": "error", "code": 405, "msg": "over the limit"}],
        ],
        plan=plan,
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert client.plan.correlation_id != "cid-1"
    assert client.plan.correlation_id.startswith("cid-1")


# --------------------------------------------------------------------------
# A plan revised mid-session
# --------------------------------------------------------------------------


async def test_a_revised_plan_with_no_connection_waits_for_the_next_handshake() -> None:
    """Mid-backoff there is nothing to send to, and nothing is lost.

    The plan in force becomes the new one, so the subscribe the next
    handshake sends is the revised list -- one subscribe, not a repair.
    """
    client, socket, _, _ = build(
        [CONNECTED, AUTHENTICATED, subscription("AAPL", "NVDA")],
        plan=equity_plan("AAPL"),
        codec=JSON_CODEC,
        stream=Stream.EQUITY,
    )
    revision = await client.apply_plan(equity_plan("AAPL", "NVDA"))

    assert revision.added == 1
    assert revision.removed == 0
    assert revision.changed is True
    assert revision.dispatched is False
    assert client.plan.subscribed == ("AAPL", "NVDA")

    with pytest.raises(SocketClosed):
        await client.run_session()

    assert socket.sent == [
        {"action": "auth", "key": KEY, "secret": SECRET},
        {"action": "subscribe", "quotes": ["AAPL", "NVDA"]},
    ]


async def test_a_revised_plan_that_changes_nothing_is_not_a_resubscribe() -> None:
    """The set is what matters, not the object. Every resubscribe is a gap in the marks.

    A viewport that scrolled and came back, or one whose whole tail was
    trimmed both times, produces a new plan with the same symbols. Sending
    that again would cost a mark on a held underlying to change nothing.
    """
    plan = equity_plan("AAPL")
    client, _, _, _ = build(
        [CONNECTED, AUTHENTICATED],
        plan=plan,
        codec=JSON_CODEC,
        stream=Stream.EQUITY,
    )
    revision = await client.apply_plan(equity_plan("AAPL"))

    assert revision.changed is False
    assert revision.dispatched is False
    # The plan in force is kept, so the correlation id on this socket's
    # records still names the decision that reached the vendor.
    assert client.plan is plan


async def test_a_revised_plan_for_the_other_socket_is_refused() -> None:
    """The same rule the constructor enforces, at the other door.

    A list of OCC symbols fits inside thirty equity slots, is admitted, and
    is never quoted -- ``not_streamed == 0`` with no banner to say so.
    """
    client, _, _, _ = build(
        [CONNECTED, AUTHENTICATED],
        plan=equity_plan("AAPL"),
        codec=JSON_CODEC,
        stream=Stream.EQUITY,
    )
    with pytest.raises(ValueError, match="equity stream"):
        await client.apply_plan(option_plan(CONTRACT))


async def _live(
    plan: Any, *, frames: list[Any] | None = None
) -> tuple[AlpacaQuoteStream, FakeSocket, Any]:
    """An equity stream on a connection that stays up. The caller ends it.

    The script is exhausted but the socket parks rather than hanging up,
    which is the only way to reach :meth:`AlpacaQuoteStream.apply_plan` with
    a live connection under it -- the *dispatched* half of that method, which
    nothing covered before these tests.
    """
    client, socket, _, _ = build(
        frames if frames is not None else [CONNECTED, AUTHENTICATED],
        plan=plan,
        codec=JSON_CODEC,
        stream=Stream.EQUITY,
        hold_open=True,
    )
    session = asyncio.ensure_future(client.run_session())
    return client, socket, session


async def _quiet(session: Any, socket: FakeSocket) -> None:
    """End a held-open session without leaving a pending task behind."""
    socket.release()
    with pytest.raises(SocketClosed):
        await session


async def _pump(turns: int = 5) -> None:
    """Hand the loop back, so a frame already taken is also handled."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def test_a_revised_plan_on_a_live_socket_sends_only_the_difference() -> None:
    """The dispatched half: an unsubscribe for what left, a subscribe for what came.

    In that order, under one lock, and never the whole list again -- the
    symbols already on this socket are position underlyings, and every
    resubscribe is a gap in their marks.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA"))
    try:
        await settle(
            lambda: {"action": "subscribe", "quotes": ["AAPL", "TSLA"]} in socket.sent,
            what="the opening subscribe",
        )
        sent = len(socket.sent)
        revision = await client.apply_plan(equity_plan("AAPL", "NVDA"))

        assert (revision.added, revision.removed, revision.dispatched) == (1, 1, True)
        assert socket.sent[sent:] == [
            {"action": "unsubscribe", "quotes": ["TSLA"]},
            {"action": "subscribe", "quotes": ["NVDA"]},
        ]
    finally:
        await _quiet(session, socket)


async def test_the_vendors_reply_to_the_unsubscribe_is_not_read_as_a_drop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The interim acknowledgement is the old set minus what left. It is not news.

    Alpaca answers *each* of the two frames with the connection's current
    set, so the reply to the unsubscribe lists neither ``TSLA`` -- just gone
    -- nor ``NVDA``, not yet asked for. Reconciled against the plan already
    in force, that reads as *"the server refused everything we added"*: a
    rule-8 WARNING carrying the viewport hint's symbols verbatim, which are
    browser strings admitted by a shape filter rather than a redactor, and a
    *"N symbols not streamed"* banner counting client-tier rows. Once per
    viewport scroll -- a rare anomaly path turned hot path, which is the one
    thing ``markets_visible_units``' docstring promised it would not become.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA"))
    try:
        socket.push(subscription("AAPL", "TSLA"))
        await settle(
            lambda: client.acknowledgement is not None,
            what="the opening subscribe to be acknowledged",
        )
        await client.apply_plan(equity_plan("AAPL", "NVDA"))
        assert client.acknowledgement is None

        with caplog.at_level("WARNING", logger="corollary.engine.stream"):
            reads = socket.reads
            socket.push(subscription("AAPL"))
            await settle(
                lambda: socket.reads > reads, what="the reply to the unsubscribe"
            )
            await _pump()

            # Nothing has been refused, so nothing is claimed about the plan
            # and nothing is logged about it.
            assert client.acknowledgement is None
            assert client.not_streamed == 0
            assert caplog.records == []

            # The reply to the *subscribe* is the one that settles it.
            socket.push(subscription("AAPL", "NVDA"))
            await settle(
                lambda: client.acknowledgement is not None,
                what="the reply to the subscribe",
            )
            acknowledged = client.acknowledgement
            assert acknowledged is not None
            assert acknowledged.absent == ()
            assert client.not_streamed == 0
            assert caplog.records == []
    finally:
        await _quiet(session, socket)


async def test_a_genuinely_unacknowledged_symbol_still_reports_after_a_revision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other side of the interim: suppression is one reply, not a mute button.

    A server that answers the *subscribe* without the symbol we added has
    refused it, and that is exactly the rule-8 record the previous test's
    interim reply must not be mistaken for.

    The handshake's own reply is taken first, so the only replies outstanding
    when the revision goes out are its two. That is not incidental: a third
    reply in flight is a different scenario with a different expected record
    count, and it has its own test below rather than riding in the margin of
    this one.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA"))
    try:
        socket.push(subscription("AAPL", "TSLA"))
        await settle(
            lambda: client.acknowledgement is not None,
            what="the opening subscribe to be acknowledged",
        )
        await client.apply_plan(equity_plan("AAPL", "NVDA"))
        with caplog.at_level("WARNING", logger="corollary.engine.stream"):
            # Two replies: the interim, then a second saying the same thing,
            # which is the server declining to add NVDA at all.
            socket.push(subscription("AAPL"), subscription("AAPL"))
            await settle(
                lambda: client.acknowledgement is not None,
                what="the reply to the subscribe",
            )
            acknowledged = client.acknowledgement
            assert acknowledged is not None
            assert acknowledged.absent == ("NVDA",)
            assert client.not_streamed == 1
            assert [record.__dict__["event"] for record in caplog.records] == [
                "stream_subscription_unacknowledged"
            ]
    finally:
        await _quiet(session, socket)


async def test_a_reply_already_in_flight_cannot_steal_the_revisions_suppression(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The handshake's own reply is outstanding when the tick lands. It is not news either.

    Alpaca cycles a data node, the socket reconnects, and ``_subscribe`` puts
    the whole list on the wire. The supervisor's five-second tick lands inside
    that round trip with a moved viewport hint, so **three** replies are owed:
    the handshake subscribe's, the unsubscribe's, and the subscribe's. Only
    the last is news about the plan in force.

    Suppressing *"the next subscription message"* is what breaks here -- the
    reply already in flight spends it, and the genuine interim then reconciles
    against the swapped plan and writes the viewport hint's symbols verbatim
    into ``absent`` (rule 6, a shape filter is not a redactor) while counting
    client-tier rows into the *"N symbols not streamed"* banner. Matching on
    the reply's **content** is what makes the three tellable apart.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA"))
    try:
        await settle(
            lambda: {"action": "subscribe", "quotes": ["AAPL", "TSLA"]} in socket.sent,
            what="the handshake's subscribe",
        )
        # Its reply has *not* arrived. This is the window.
        await client.apply_plan(equity_plan("AAPL", "NVDA"))
        assert client.acknowledgement is None

        with caplog.at_level("WARNING", logger="corollary.engine.stream"):
            reads = socket.reads
            socket.push(subscription("AAPL", "TSLA"), subscription("AAPL"))
            await settle(
                lambda: socket.reads > reads + 1,
                what="the handshake's reply and the unsubscribe's",
            )
            await _pump()

            # Neither says anything about the plan in force, so neither is
            # claimed and neither is logged.
            assert client.acknowledgement is None
            assert client.not_streamed == 0
            assert caplog.records == []

            socket.push(subscription("AAPL", "NVDA"))
            await settle(
                lambda: client.acknowledgement is not None,
                what="the reply to the subscribe",
            )
            acknowledged = client.acknowledgement
            assert acknowledged is not None
            assert acknowledged.absent == ()
            assert client.not_streamed == 0
            assert caplog.records == []
    finally:
        await _quiet(session, socket)


async def test_an_in_flight_reply_that_differs_records_out_of_step_and_not_a_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half of the in-flight window: the reply that matches nothing.

    Same shape as the test above -- reconnect, the handshake's subscribe on
    the wire, the supervisor's tick landing inside that round trip with a
    moved viewport hint -- except that the handshake's reply comes back
    ``{AAPL}``: the server refused TSLA, and this is our first knowledge of
    it. It matches no state we asked for, so nothing is spent and it is read
    as news.

    **What it must not be read as is a refusal of NVDA.** NVDA's subscribe
    went out microseconds earlier and has not been answered; reconciling this
    reply against the plan in force says *"the server refused NVDA"* twice
    over, with the viewport hint's symbols written verbatim into ``absent``
    (rule 6 -- ``_EQUITY_TICKER`` is a shape filter and a shape filter is not
    a redactor) and a client-tier row counted into the banner as though the
    engine had lost a mark.

    So the claim is **scoped to what can be known**: a symbol this revision
    added, whose subscribe is still unanswered, is not absent -- it is
    unanswered. Rule 8 is still served, and loudly: the divergence gets its
    own counts-only record, ``stream_subscription_out_of_step``, which says
    we are out of step with the server rather than that the server refused
    anything. When the last frame is answered the claim comes back, and NVDA
    -- genuinely never added -- is named then.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA"))
    try:
        await settle(
            lambda: {"action": "subscribe", "quotes": ["AAPL", "TSLA"]} in socket.sent,
            what="the handshake's subscribe",
        )
        # Its reply has *not* arrived. This is the window.
        await client.apply_plan(equity_plan("AAPL", "NVDA"))

        with caplog.at_level("WARNING", logger="corollary.engine.stream"):
            reads = socket.reads
            # Three frames are outstanding and this answers the first of
            # them, having refused TSLA at the handshake.
            socket.push(subscription("AAPL"))
            await settle(lambda: socket.reads > reads, what="the handshake's reply")
            await _pump()

            assert [record.__dict__["event"] for record in caplog.records] == [
                "stream_subscription_out_of_step"
            ]
            out_of_step = caplog.records[0].__dict__
            # Counts, never symbols: nothing here can carry a browser string.
            assert "absent" not in out_of_step
            assert "surplus" not in out_of_step
            assert "NVDA" not in str(out_of_step)
            assert out_of_step["unanswered_frames"] == 2
            assert out_of_step["unanswered_count"] == 1
            assert out_of_step["acknowledged_count"] == 1
            assert out_of_step["subscribed_count"] == 2

            acknowledged = client.acknowledgement
            assert acknowledged is not None
            assert acknowledged.absent == ()
            # Not yet acknowledged is not yet streaming, so the banner counts
            # it: over-reporting a gap is the safe direction for that figure.
            assert client.not_streamed == 1

            # The unsubscribe's reply, still inside the window: same again.
            reads = socket.reads
            socket.push(subscription("AAPL"))
            await settle(lambda: socket.reads > reads, what="the unsubscribe's reply")
            await _pump()
            assert [record.__dict__["event"] for record in caplog.records] == [
                "stream_subscription_out_of_step",
                "stream_subscription_out_of_step",
            ]

            # The subscribe's reply. Every frame is answered, the claim is
            # whole again, and NVDA was genuinely never added.
            socket.push(subscription("AAPL"))
            await settle(
                lambda: any(
                    record.__dict__["event"] == "stream_subscription_unacknowledged"
                    for record in caplog.records
                ),
                what="the reply to the subscribe",
            )
            acknowledged = client.acknowledgement
            assert acknowledged is not None
            assert acknowledged.absent == ("NVDA",)
            assert client.not_streamed == 1
    finally:
        await _quiet(session, socket)


async def test_a_405_resubscribe_racing_a_revision_claims_nothing_until_it_is_answered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other way into the window: the cap correction's whole-list subscribe.

    A revision is on the wire, the server answers one of its frames with a
    405, and :meth:`_correct_cap` re-plans and re-subscribes the whole list
    behind the same send lock. The reply still owed to the revision now
    predates a subscribe carrying *every* symbol in the corrected plan -- so
    reconciling it against that plan says the server refused whatever it does
    not list, which it has not been asked about yet.

    The handshake's own whole-list subscribe is the case this is **not**: it
    is the oldest frame on the connection, so every reply after it has seen
    the whole list and an omission is real. The difference is whether
    anything was outstanding when the list went out, which is what the frame
    count is for.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA"))
    try:
        socket.push(subscription("AAPL", "TSLA"))
        await settle(
            lambda: client.acknowledgement is not None,
            what="the opening subscribe to be acknowledged",
        )
        await client.apply_plan(equity_plan("AAPL", "NVDA"))

        with caplog.at_level("WARNING"):
            # Answering the unsubscribe with a 405: the subscribe's reply is
            # still owed, and the correction resubscribes the whole list.
            socket.push({"T": "error", "code": 405, "msg": "over the limit"})
            await settle(
                lambda: any(
                    record.__dict__.get("event") == "stream_cap_corrected"
                    for record in caplog.records
                ),
                what="the cap correction",
            )
            reads = socket.reads
            # The reply the revision was still owed. It predates the
            # correction's list and can refuse nothing in it.
            socket.push(subscription("AAPL"))
            await settle(lambda: socket.reads > reads, what="the stale reply")
            await _pump()

            events = [record.__dict__["event"] for record in caplog.records]
            assert "stream_subscription_unacknowledged" not in events
            assert events[-1] == "stream_subscription_out_of_step"
            assert "NVDA" not in str(caplog.records[-1].__dict__)
    finally:
        await _quiet(session, socket)


async def test_an_error_answering_the_unsubscribe_does_not_mute_the_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refusal answered with an ``error`` frame, and the real news behind it.

    Nothing makes the vendor answer each frame with a ``subscription``
    message; it may answer one with an ``error``. A suppression spent only by
    a ``subscription`` message would still be armed when the *subscribe* is
    answered, and would eat the record saying the server never added what we
    asked for -- rule 8's failure mode, which is silence. So an error frame
    drops the expectations: whatever comes next is read as news.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA"))
    try:
        socket.push(subscription("AAPL", "TSLA"))
        await settle(
            lambda: client.acknowledgement is not None,
            what="the opening subscribe to be acknowledged",
        )
        await client.apply_plan(equity_plan("AAPL", "NVDA"))

        with caplog.at_level("WARNING"):
            socket.push({"T": "error", "code": 500, "msg": "unsubscribe failed"})
            await settle(
                lambda: any(
                    record.__dict__.get("event") == "stream_vendor_error"
                    for record in caplog.records
                ),
                what="the vendor's error frame",
            )
            # TSLA was never dropped and NVDA was never added.
            socket.push(subscription("AAPL", "TSLA"))
            await settle(
                lambda: client.acknowledgement is not None,
                what="the reply to the subscribe",
            )
            acknowledged = client.acknowledgement
            assert acknowledged is not None
            assert acknowledged.absent == ("NVDA",)
            assert acknowledged.surplus == ("TSLA",)
            assert client.not_streamed == 1
            assert [record.__dict__["event"] for record in caplog.records] == [
                "stream_vendor_error",
                "stream_subscription_unacknowledged",
            ]
    finally:
        await _quiet(session, socket)


async def test_a_coalesced_reply_matching_no_interim_state_reports_what_is_unmarked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The vendor answers both frames once, its content matching neither.

    *"Alpaca answers each frame"* is an observation, not a guarantee the code
    may rest on: a reply count would be left permanently ahead by a coalesced
    answer and a bare flag would swallow it. The symbol missing here is
    ``MSFT`` -- an **engine**-tier symbol, a position underlying -- so muting
    this record answers *"is anything I hold unmarked?"* with no when the
    answer is yes.

    **The name is narrow on purpose.** It is the reply's *content* that makes
    this one news: ``{AAPL, NVDA}`` is neither interim state, so nothing is
    spent and nothing is muted. A coalesced reply whose content happens to
    equal the interim state is the one case that is still muted, and it has
    its own test below saying so.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA", "MSFT"))
    try:
        socket.push(subscription("AAPL", "TSLA", "MSFT"))
        await settle(
            lambda: client.acknowledgement is not None,
            what="the opening subscribe to be acknowledged",
        )
        await client.apply_plan(equity_plan("AAPL", "MSFT", "NVDA"))

        with caplog.at_level("WARNING", logger="corollary.engine.stream"):
            # One reply for the unsubscribe *and* the subscribe: TSLA gone as
            # asked, NVDA added as asked, and MSFT no longer streaming.
            socket.push(subscription("AAPL", "NVDA"))
            await settle(
                lambda: client.acknowledgement is not None,
                what="the coalesced reply",
            )
            acknowledged = client.acknowledgement
            assert acknowledged is not None
            assert acknowledged.absent == ("MSFT",)
            assert client.not_streamed == 1
            assert [record.__dict__["event"] for record in caplog.records] == [
                "stream_subscription_unacknowledged"
            ]
    finally:
        await _quiet(session, socket)


async def test_a_coalesced_reply_equal_to_the_interim_state_is_the_surviving_mute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one mute left, pinned so that it is a decided property and not an accident.

    ``{AAPL, TSLA, MSFT}`` is acknowledged, the revision to
    ``{AAPL, MSFT, NVDA}`` puts two frames on the wire -- interim
    ``{AAPL, MSFT}``, then ``{AAPL, MSFT, NVDA}`` -- and the server coalesces
    them into one answer of ``{AAPL, MSFT}``: TSLA dropped as asked, NVDA
    refused outright. That content *is* the interim state and a later frame is
    still owed, so it is read as the answer to the first frame and suppressed.

    **This is ambiguous in the frames themselves, not in the code.** *"Answered
    frame one only"* and *"coalesced, and refused everything we added"* are
    indistinguishable from content alone, and the alternative to suppressing
    is a false refusal record on every ordinary revision. What bounds the
    cost: a match on the interim state means every symbol that survived the
    revision **is** acknowledged, so the only thing this can hide is a symbol
    the revision itself *added* -- and it is hidden only until the next reply
    or revision, which is what makes it a mute and not a hole.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA", "MSFT"))
    try:
        socket.push(subscription("AAPL", "TSLA", "MSFT"))
        await settle(
            lambda: client.acknowledgement is not None,
            what="the opening subscribe to be acknowledged",
        )
        await client.apply_plan(equity_plan("AAPL", "MSFT", "NVDA"))
        assert client.acknowledgement is None

        with caplog.at_level("WARNING", logger="corollary.engine.stream"):
            reads = socket.reads
            socket.push(subscription("AAPL", "MSFT"))
            await settle(lambda: socket.reads > reads, what="the coalesced reply")
            await _pump()

            assert caplog.records == []
            assert client.acknowledgement is None
            assert client.not_streamed == 0
            assert client.message is None

            # And it heals on the next word from the server, which is what
            # keeps it a mute rather than a hole: NVDA is named then.
            socket.push(subscription("AAPL", "MSFT"))
            await settle(
                lambda: client.acknowledgement is not None,
                what="the next reply, which is news",
            )
            acknowledged = client.acknowledgement
            assert acknowledged is not None
            assert acknowledged.absent == ("NVDA",)
            assert [record.__dict__["event"] for record in caplog.records] == [
                "stream_subscription_unacknowledged"
            ]
    finally:
        await _quiet(session, socket)


async def test_two_revisions_inside_one_round_trip_claim_nothing_until_the_last(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Four frames out, four replies back, and only the fourth is news.

    Two viewport settles inside one round trip -- ``SESSION_CHECK_INTERVAL``
    is five seconds, so this needs a slow reply rather than a fast user, but
    it needs nothing else. One expectation is recorded per frame and they are
    spent in order, so the three stale replies claim nothing rather than
    reconciling against a plan they predate.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA"))
    try:
        socket.push(subscription("AAPL", "TSLA"))
        await settle(
            lambda: client.acknowledgement is not None,
            what="the opening subscribe to be acknowledged",
        )
        await client.apply_plan(equity_plan("AAPL", "NVDA"))
        await client.apply_plan(equity_plan("AAPL", "SPY"))

        with caplog.at_level("WARNING", logger="corollary.engine.stream"):
            reads = socket.reads
            socket.push(
                subscription("AAPL"),  # the first unsubscribe's
                subscription("AAPL", "NVDA"),  # the first subscribe's
                subscription("AAPL"),  # the second unsubscribe's
            )
            await settle(
                lambda: socket.reads > reads + 2, what="the three stale replies"
            )
            await _pump()

            assert client.acknowledgement is None
            assert client.not_streamed == 0
            assert caplog.records == []

            socket.push(subscription("AAPL", "SPY"))
            await settle(
                lambda: client.acknowledgement is not None,
                what="the reply to the second subscribe",
            )
            acknowledged = client.acknowledgement
            assert acknowledged is not None
            assert acknowledged.absent == ()
            assert client.not_streamed == 0
            assert caplog.records == []
    finally:
        await _quiet(session, socket)


async def test_a_revision_before_the_handshake_is_answered_is_not_dispatched() -> None:
    """The ``_socket`` set / ``_subscribed`` False window, on its own.

    A connection exists and auth has gone out, but the server has not
    answered it yet. A subscribe sent here is a 401, and 401 is fatal -- a
    browser scrolling would take the feed down for the session. So the plan
    is taken and nothing is sent: the next ``success`` authenticates and
    subscribes the plan in force, whole.
    """
    client, socket, session = await _live(equity_plan("AAPL"), frames=[CONNECTED])
    try:
        await settle(
            lambda: socket.sent == [{"action": "auth", "key": KEY, "secret": SECRET}],
            what="the auth frame",
        )
        revision = await client.apply_plan(equity_plan("AAPL", "NVDA"))

        assert revision.changed is True
        assert revision.dispatched is False
        # The auth frame, and nothing after it.
        assert socket.sent == [{"action": "auth", "key": KEY, "secret": SECRET}]

        socket.push(AUTHENTICATED[0])
        await settle(lambda: len(socket.sent) > 1, what="the handshake's subscribe")
        assert socket.sent[1] == {"action": "subscribe", "quotes": ["AAPL", "NVDA"]}
    finally:
        await _quiet(session, socket)


async def test_a_revision_may_not_widen_a_cap_the_server_lowered() -> None:
    """A 405 is the server's own figure, and a re-plan must not unlearn it.

    ``replan_at_cap`` refuses to raise a cap on the strength of a refusal;
    this is the same guard at the other door, because the supervisor builds
    every viewport re-plan from the *account's* budget and would otherwise
    put the symbols the correction dropped straight back on the wire.
    """
    client, socket, session = await _live(equity_plan("AAPL", "TSLA", "NVDA"))
    try:
        await settle(
            lambda: any(frame.get("action") == "subscribe" for frame in socket.sent),
            what="the opening subscribe",
        )
        socket.push([{"T": "error", "code": 405, "msg": "over the limit"}])
        await settle(lambda: client.plan.cap < 30, what="the cap correction")
        corrected = client.plan.cap
        sent = len(socket.sent)

        with pytest.raises(ValueError, match="cap"):
            await client.apply_plan(equity_plan("AAPL", "SPY", cap=30))

        assert client.plan.cap == corrected
        # Refused before anything reached the wire.
        assert socket.sent[sent:] == []

        # A revision *at* the corrected cap is ordinary and goes out.
        revision = await client.apply_plan(equity_plan("AAPL", "SPY", cap=corrected))
        assert revision.dispatched is True
    finally:
        await _quiet(session, socket)
