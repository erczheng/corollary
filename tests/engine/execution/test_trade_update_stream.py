"""``trade_updates``: the order lifecycle, as it arrives on the trading socket.

Not market data, which is why it lives in ``engine/execution/alpaca.py`` and
not with the two quote streams -- it is the broker telling us what happened to
an order we placed, on the trading host, with its own rate-limit bucket.

It is also the second producer for rule 9's stream-closed condition, so the
close attribution is tested here as well as on the market-data side: the
shared skeleton in ``corollary/sockets.py`` is what makes them the same
answer, and a test on only one of them would not notice if it stopped being.
"""

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest

import corollary.engine.execution.alpaca as alpaca_broker

from corollary.api.fanout import Fanout, trade_update_sink
from corollary.api.schemas import WsTradeUpdateFrame
from corollary.data.providers.alpaca import AlpacaCredentials
from corollary.engine.execution.alpaca import (
    TRADE_UPDATES_STREAM,
    AlpacaTradeUpdateStream,
    trade_updates_url,
)
from corollary.engine.execution.interface import PositionIntent, TradeUpdate
from corollary.sockets import JSON_CODEC, SocketClosed
from corollary.wire import ERROR_BODY_MAX
from tests.sockets_support import T0, Clock, FakeConnect, FakeSocket, SpyActivity, SpySleep

pytestmark = pytest.mark.asyncio

KEY = "AKFAKEKEYFORTESTSONLY000"
SECRET = "TRADEUPDATESnotarealsecretTRADEUPDATE000"

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "alpaca"

AUTHORIZED = {
    "stream": "authorization",
    "data": {"status": "authorized", "action": "authenticate"},
}
LISTENING = {"stream": "listening", "data": {"streams": ["trade_updates"]}}


def nested_order() -> dict[str, Any]:
    """A real recorded ``mleg`` parent, legs and all."""
    body = json.loads((FIXTURES / "orders_nested.json").read_text(encoding="utf-8"))
    order: dict[str, Any] = body["body"][0]
    return order


def fill_event(**overrides: Any) -> dict[str, Any]:
    order = nested_order()
    data: dict[str, Any] = {
        "event": "fill",
        "execution_id": "2f63ea93-423d-4169-b3f6-3fdafc10c418",
        "order": order,
        "position_qty": "1",
        "price": "2.01",
        "qty": "1",
        "timestamp": "2026-09-14T13:30:05.024916716Z",
    }
    data.update(overrides)
    return {"stream": "trade_updates", "data": data}


def build(
    frames: list[Any],
    *,
    activity: SpyActivity | None = None,
    on_update: Any = None,
) -> tuple[AlpacaTradeUpdateStream, FakeSocket, SpyActivity, list[TradeUpdate]]:
    recorder = activity or SpyActivity()
    received: list[TradeUpdate] = []
    sink = on_update if on_update is not None else received.append
    socket = FakeSocket(frames, codec=JSON_CODEC)
    client = AlpacaTradeUpdateStream(
        credentials=AlpacaCredentials(
            key_id=KEY,
            secret_key=SECRET,
            trading_base_url="https://paper-api.alpaca.markets",
            is_paper=True,
        ),
        activity=recorder,
        on_update=sink,
        connect=FakeConnect(socket),
        sleep=SpySleep(),
        now=Clock(),
    )
    return client, socket, recorder, received


# --------------------------------------------------------------------------
# The URL and the protocol
# --------------------------------------------------------------------------


async def test_the_url_is_the_trading_host_not_the_data_host() -> None:
    """Two hosts, two rate-limit buckets. This one is the trading host.

    Rule 5 lives in the credentials: a paper key pair carries the paper base
    URL, so the socket cannot be pointed at the live host by a socket-layer
    mistake -- it would take a different key pair.
    """
    paper = AlpacaCredentials(
        key_id=KEY,
        secret_key=SECRET,
        trading_base_url="https://paper-api.alpaca.markets",
        is_paper=True,
    )
    live = AlpacaCredentials(
        key_id=KEY,
        secret_key=SECRET,
        trading_base_url="https://api.alpaca.markets",
        is_paper=False,
    )
    assert trade_updates_url(paper) == "wss://paper-api.alpaca.markets/stream"
    assert trade_updates_url(live) == "wss://api.alpaca.markets/stream"
    assert "stream.data.alpaca.markets" not in trade_updates_url(paper)


async def test_it_authenticates_then_listens_to_trade_updates_only() -> None:
    client, socket, _, _ = build([AUTHORIZED, LISTENING])
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert socket.sent == [
        {"action": "auth", "key": KEY, "secret": SECRET},
        {"action": "listen", "data": {"streams": [TRADE_UPDATES_STREAM]}},
    ]
    assert TRADE_UPDATES_STREAM == "trade_updates"


async def test_authorization_is_what_records_the_stream_open() -> None:
    client, _, recorder, _ = build([AUTHORIZED, LISTENING])
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert len(recorder.opens) == 1


async def test_a_refused_authorization_is_fatal_and_records_the_close() -> None:
    """A wrong key does not come back on reconnect; it comes back on a fix."""
    client, _, recorder, _ = build(
        [{"stream": "authorization", "data": {"status": "unauthorized"}}]
    )
    with pytest.raises(Exception) as raised:
        await client.run_session()
    assert "unauthorized" in str(raised.value)
    assert len(recorder.closes) == 1
    assert recorder.opens == []


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------


async def test_a_fill_becomes_a_trade_update_with_decimal_money() -> None:
    client, _, _, received = build([AUTHORIZED, LISTENING, fill_event()])
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert len(received) == 1
    update = received[0]
    assert update.event == "fill"
    assert update.status == "filled"
    assert update.fill_price == Decimal("2.01")
    assert isinstance(update.fill_price, Decimal)
    assert update.fill_quantity == 1
    assert update.position_quantity == 1
    assert update.filled_quantity == 1
    assert update.at.isoformat() == "2026-09-14T13:30:05.024916+00:00"
    assert update.order.is_multi_leg is True


async def test_an_mleg_parent_carries_no_action_rather_than_a_guessed_one() -> None:
    """Guessing an intent is how a buy-to-close is booked as a new lot."""
    client, _, _, received = build([AUTHORIZED, LISTENING, fill_event()])
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert received[0].action is None
    assert received[0].symbol == ""


async def test_a_credit_keeps_its_sign() -> None:
    """A negative average fill price is a net credit. The sign is the fact."""
    client, _, _, received = build([AUTHORIZED, LISTENING, fill_event()])
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert received[0].filled_avg_price == Decimal("-2.01")
    assert received[0].order.is_credit is True


async def test_a_leg_resolves_its_action_from_the_position_intent() -> None:
    order = nested_order()
    leg = order["legs"][0]
    client, _, _, received = build(
        [AUTHORIZED, LISTENING, fill_event(order=leg, price="9.41")]
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert received[0].action is PositionIntent.SELL_TO_OPEN
    assert received[0].symbol == leg["symbol"]


async def test_a_non_fill_event_carries_no_fill_price() -> None:
    client, _, _, received = build(
        [
            AUTHORIZED,
            LISTENING,
            {
                "stream": "trade_updates",
                "data": {
                    "event": "new",
                    "order": nested_order(),
                    "timestamp": "2026-09-14T13:30:00Z",
                },
            },
        ]
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert received[0].event == "new"
    assert received[0].fill_price is None
    assert received[0].fill_quantity is None
    assert received[0].position_quantity is None


async def test_a_fractional_quantity_is_refused_rather_than_rounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rounding a quantity is a position size that is wrong with nothing to say so.

    This book trades contracts; the one asset class that fills fractionally is
    not traded here. So the event is logged and dropped rather than booked at
    a number nobody sent.
    """
    client, _, _, received = build(
        [AUTHORIZED, LISTENING, fill_event(qty="1.5"), fill_event()]
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(SocketClosed):
            await client.run_session()
    assert len(received) == 1
    assert received[0].fill_quantity == 1


async def test_a_malformed_event_does_not_take_the_socket_down() -> None:
    client, _, _, received = build(
        [
            AUTHORIZED,
            LISTENING,
            {"stream": "trade_updates", "data": {"event": "fill"}},
            fill_event(),
        ]
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert len(received) == 1


async def test_an_update_reaches_the_fanout_as_a_trade_update_frame() -> None:
    fanout = Fanout()
    subscriber = fanout.subscribe()
    client, _, _, _ = build(
        [AUTHORIZED, LISTENING, fill_event()], on_update=trade_update_sink(fanout)
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    frame = await subscriber.next_frame()
    assert isinstance(frame, WsTradeUpdateFrame)
    assert frame.update.event == "fill"
    assert frame.update.fill_price == Decimal("2.01")
    assert frame.update.position_quantity == 1


# --------------------------------------------------------------------------
# Rule 9, on this socket too
# --------------------------------------------------------------------------


async def test_a_vendor_close_records_a_stream_close() -> None:
    client, _, recorder, _ = build(
        [AUTHORIZED, LISTENING, SocketClosed("1006 abnormal closure")]
    )
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert len(recorder.closes) == 1
    assert "1006" in recorder.closes[0][1]


async def test_our_own_close_records_nothing() -> None:
    client, socket, recorder, _ = build([AUTHORIZED, LISTENING])
    socket.push(lambda: client.begin_close())
    await client.run_session()
    assert recorder.closes == []
    assert socket.closed is True


async def test_every_vendor_frame_records_a_message() -> None:
    client, _, recorder, _ = build([AUTHORIZED, LISTENING, fill_event()])
    with pytest.raises(SocketClosed):
        await client.run_session()
    assert recorder.messages == [T0, T0, T0]


async def test_a_credential_never_reaches_a_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _, _, _ = build(
        [
            AUTHORIZED,
            LISTENING,
            SocketClosed(f"1006 abnormal closure while authed as {KEY}:{SECRET}"),
        ]
    )
    with caplog.at_level("DEBUG"):
        with pytest.raises(SocketClosed):
            await client.run_session()
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.__dict__)
        assert KEY not in rendered
        assert SECRET not in rendered


# --------------------------------------------------------------------------
# Rule 6 on the event path, and what is contained around the sink
# --------------------------------------------------------------------------


async def test_an_unreadable_update_is_logged_through_the_redactor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 6: the message is scrubbed as well as ``extra["detail"]``.

    Same defect as the quote path's, on the socket that carries fills: the
    raw exception went into the format args while the scrubbed copy went into
    ``extra``. ``VendorStream._detail`` is the stated single door for
    vendor-derived text in a stream client, and both halves of this record
    now go through it.
    """
    client, _, _, _ = build(
        [AUTHORIZED, LISTENING, fill_event(qty=f"1.5 {KEY} {SECRET}")]
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(SocketClosed):
            await client.run_session()

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "trade_update_unreadable"
    ]
    assert len(records) == 1
    rendered = records[0].getMessage()
    assert KEY not in rendered
    assert SECRET not in rendered
    assert len(rendered) <= ERROR_BODY_MAX + 80


async def test_a_sink_that_raises_costs_one_update_and_not_the_socket(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fill the browser cannot be told about must not end the order feed.

    The sink was called outside the ``try``, so a ``KeyError`` from
    ``wire_action`` -- a ``dict`` lookup with no exhaustiveness checking --
    would have killed the ``trade_updates`` socket with no
    ``record_stream_closed`` and no reconnect: the engine blind to its own
    fills while believing the feed healthy.
    """
    delivered: list[TradeUpdate] = []

    def sink(update: TradeUpdate) -> None:
        delivered.append(update)
        if len(delivered) == 1:
            raise KeyError("wire_action has no branch for this PositionIntent")

    client, _, recorder, _ = build(
        [AUTHORIZED, LISTENING, fill_event(), fill_event()], on_update=sink
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(SocketClosed):
            await client.run_session()

    assert len(delivered) == 2
    assert len(recorder.closes) == 1
    failures = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "trade_update_sink_failed"
    ]
    assert len(failures) == 1


@pytest.mark.parametrize(
    "error",
    [
        OverflowError("cannot convert Infinity to integer"),
        InvalidOperation("comparison involving NaN"),
        ZeroDivisionError("an average fill price over no shares"),
    ],
)
async def test_an_arithmetic_error_in_the_translation_costs_one_update(
    error: ArithmeticError, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order socket's half of fix A's defence in depth.

    ``(BrokerError, KeyError, TypeError, ValueError)`` contained
    ``int(Decimal('NaN'))`` and not ``int(Decimal('Infinity'))`` -- one family,
    two members, and only one of them caught. An escape here is worse than on
    a quote socket: the book stops receiving fills while quotes keep flowing,
    which is rule 9's "unverified position state" with nothing recorded and no
    reconnect attempted. The family is what the catch names now.
    """
    calls: list[int] = []
    real = alpaca_broker._trade_update

    def refusing(payload: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise error
        return real(payload)

    monkeypatch.setattr(alpaca_broker, "_trade_update", refusing)
    client, _, recorder, received = build(
        [
            AUTHORIZED,
            LISTENING,
            fill_event(),
            fill_event(execution_id="the-one-after"),
        ]
    )
    with pytest.raises(SocketClosed):
        await client.run_session()

    assert len(calls) == 2, "the later event was still translated"
    assert len(received) == 1, "and reached the sink"
    assert len(recorder.closes) == 1
