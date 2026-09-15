"""The fan-out on its own: one publish, every client, and what a slow one costs.

These are the properties the websocket tests cannot see from the outside --
eviction under backlog, isolation between subscribers, and which frame kinds a
filter is allowed to touch. No socket is opened here.
"""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from corollary.api.fanout import Fanout, Subscription
from corollary.api.schemas import (
    ApiErrorBody,
    WsErrorFrame,
    WsQuote,
    WsQuoteFrame,
    WsServerFrame,
    WsTradeUpdate,
    WsTradeUpdateFrame,
)

AT = datetime(2026, 9, 14, 14, 30, tzinfo=timezone.utc)


def quote_frame(symbol: str, bid: str = "1.00") -> WsQuoteFrame:
    return WsQuoteFrame(
        quote=WsQuote(
            symbol=symbol,
            bid=Decimal(bid),
            ask=Decimal(bid) + Decimal("0.02"),
            bid_size=1,
            ask_size=1,
            at=AT,
        )
    )


def update_frame(symbol: str) -> WsTradeUpdateFrame:
    return WsTradeUpdateFrame(
        update=WsTradeUpdate(
            event="fill",
            at=AT,
            order_id="order-1",
            symbol=symbol,
            status="filled",
            action="BTO",
            quantity=1,
            filled_quantity=1,
            fill_price=Decimal("3.40"),
            fill_quantity=1,
            filled_avg_price=Decimal("3.40"),
            position_quantity=1,
        )
    )


def error_frame(message: str = "no") -> WsErrorFrame:
    return WsErrorFrame(error=ApiErrorBody(code="invalid_request", message=message))


def drain(subscription: Subscription) -> list[WsServerFrame]:
    """Every queued frame, in order, without waiting for another."""
    frames: list[WsServerFrame] = []
    while subscription.pending:
        frames.append(asyncio.run(subscription.next_frame()))
    return frames


def symbols_of(frames: list[WsServerFrame]) -> list[str]:
    return [
        frame.quote.symbol
        for frame in frames
        if isinstance(frame, WsQuoteFrame)
    ]


# --------------------------------------------------------------------------
# One publish, every subscriber
# --------------------------------------------------------------------------


def test_one_publish_reaches_every_subscriber_with_the_same_object() -> None:
    fanout = Fanout()
    first, second = fanout.subscribe(), fanout.subscribe()
    frame = quote_frame("AAPL")

    fanout.publish(frame)

    # Identity, not equality: the frame is built once, so there is no second
    # copy of a price to disagree with the first.
    assert drain(first) == [frame]
    assert drain(second)[0] is frame


def test_frames_keep_their_publish_order() -> None:
    fanout = Fanout()
    subscription = fanout.subscribe()

    for symbol in ("AAPL", "MSFT", "NVDA"):
        fanout.publish(quote_frame(symbol))

    assert symbols_of(drain(subscription)) == ["AAPL", "MSFT", "NVDA"]


def test_unsubscribing_stops_delivery_and_is_idempotent() -> None:
    fanout = Fanout()
    subscription = fanout.subscribe()

    fanout.unsubscribe(subscription)
    fanout.unsubscribe(subscription)
    fanout.publish(quote_frame("AAPL"))

    assert fanout.subscriber_count == 0
    assert subscription.pending == 0


def test_a_subscriber_that_raises_does_not_cost_the_others_a_frame(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One broken client is one broken client, not an outage."""
    caplog.set_level(logging.DEBUG)
    fanout = Fanout()
    broken = fanout.subscribe()
    healthy = fanout.subscribe()

    def boom(frame: WsServerFrame) -> None:
        raise RuntimeError("this client is beyond help")

    broken.offer = boom  # type: ignore[method-assign]

    fanout.publish(quote_frame("AAPL"))

    assert symbols_of(drain(healthy)) == ["AAPL"]
    events = [record.__dict__.get("event") for record in caplog.records]
    assert "ws_publish_failed" in events


# --------------------------------------------------------------------------
# Backlog
# --------------------------------------------------------------------------


def test_a_full_queue_evicts_the_oldest_frame_and_records_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    fanout = Fanout(queue_size=2)
    subscription = fanout.subscribe()

    for symbol in ("AAPL", "MSFT", "NVDA"):
        fanout.publish(quote_frame(symbol))

    # The newest two survive. A stale quote superseded by a newer one is worth
    # nothing; the *newest* price is the one the screen needs.
    assert symbols_of(drain(subscription)) == ["MSFT", "NVDA"]
    assert subscription.dropped == 1

    records = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "ws_frame_dropped"
    ]
    assert len(records) == 1
    # Rule 8: the rule, the inputs, the timestamp -- and the kind, so a
    # dropped ``trade_update`` is visible in the log rather than pooled in
    # with the quotes.
    assert records[0].__dict__["rule"]
    assert records[0].__dict__["at"]
    assert records[0].__dict__["dropped_kind"] == "quote"
    assert records[0].__dict__["queue_size"] == 2


def kinds_of(frames: list[WsServerFrame]) -> list[str]:
    return [frame.type for frame in frames]


def test_a_backlog_evicts_a_quote_rather_than_a_fill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The money finding: eviction used to be kind-blind.

    A connection at its frame bound during an opening burst holds a
    ``trade_update`` for a fill as its oldest queued frame; one more quote
    used to evict it. There is no replay, and a ``trade_updates`` event is
    what *triggers* the REST refetch -- so the trigger was what was lost and
    the position row sat pre-fill until the 15s poll.

    A quote is superseded by the next quote for the same symbol. A fill is
    superseded by nothing.
    """
    caplog.set_level(logging.WARNING)
    fanout = Fanout(queue_size=2)
    subscription = fanout.subscribe()

    fanout.publish(update_frame("AAPL241220C00150000"))
    fanout.publish(quote_frame("AAPL"))
    fanout.publish(quote_frame("MSFT"))

    frames = drain(subscription)
    assert kinds_of(frames) == ["trade_update", "quote"]
    assert symbols_of(frames) == ["MSFT"]
    assert subscription.dropped == 1

    records = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "ws_frame_dropped"
    ]
    assert len(records) == 1
    assert records[0].__dict__["dropped_kind"] == "quote"


def test_a_backlog_evicts_a_quote_rather_than_a_refusal() -> None:
    """The same mechanism used to eat a refusal.

    A client whose ``subscribe`` was refused and whose refusal was then
    evicted believes it is watching a list it is not watching -- which is the
    confusion the whole all-or-nothing subscription rule exists to prevent.
    """
    fanout = Fanout(queue_size=2)
    subscription = fanout.subscribe()

    subscription.offer(error_frame())
    fanout.publish(quote_frame("AAPL"))
    fanout.publish(quote_frame("MSFT"))

    frames = drain(subscription)
    assert kinds_of(frames) == ["error", "quote"]
    assert symbols_of(frames) == ["MSFT"]


def test_evicting_a_mid_queue_quote_leaves_the_rest_in_order() -> None:
    """The oldest *quote* can sit behind a frame that may not be evicted.

    Removing from the middle is where a rebuild can silently reorder, and
    order is the one thing a feed of prices cannot get wrong.
    """
    fanout = Fanout(queue_size=3)
    subscription = fanout.subscribe()

    fanout.publish(update_frame("AAPL241220C00150000"))
    fanout.publish(quote_frame("AAPL"))
    fanout.publish(quote_frame("MSFT"))
    fanout.publish(quote_frame("NVDA"))

    frames = drain(subscription)
    assert kinds_of(frames) == ["trade_update", "quote", "quote"]
    assert symbols_of(frames) == ["MSFT", "NVDA"]


def test_with_no_quote_queued_the_oldest_frame_is_evicted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fallback. Something has to go, and it is still never the newest.

    A queue holding only fills is a client that has stopped reading during a
    burst of executions. Preserved from the kind-blind version: the oldest
    goes, the newest stays, and the record says which kind was lost.
    """
    caplog.set_level(logging.WARNING)
    fanout = Fanout(queue_size=2)
    subscription = fanout.subscribe()

    for symbol in ("AAPL", "MSFT", "NVDA"):
        fanout.publish(update_frame(symbol))

    frames = drain(subscription)
    assert [
        frame.update.symbol
        for frame in frames
        if isinstance(frame, WsTradeUpdateFrame)
    ] == ["MSFT", "NVDA"]

    records = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "ws_frame_dropped"
    ]
    assert records[0].__dict__["dropped_kind"] == "trade_update"


# --------------------------------------------------------------------------
# What a filter may touch
# --------------------------------------------------------------------------


def test_a_filter_gates_quotes_only() -> None:
    fanout = Fanout()
    subscription = fanout.subscribe()
    subscription.filter_to(["AAPL"])

    fanout.publish(quote_frame("MSFT"))
    fanout.publish(update_frame("MSFT"))
    fanout.publish(quote_frame("AAPL"))

    frames = drain(subscription)
    assert [type(frame).__name__ for frame in frames] == [
        "WsTradeUpdateFrame",
        "WsQuoteFrame",
    ]


def test_an_error_frame_is_never_filtered_out() -> None:
    """Through ``publish``, because ``offer`` never consults ``wants``.

    Written against ``offer`` first, which exercised no filter at all: the
    test passed and would have kept passing if ``wants`` had gated error
    frames. An error frame is not broadcast in the shipped path -- it belongs
    to one connection -- and ``publish`` is used here to put it through the
    filter, which is the thing under test.
    """
    fanout = Fanout()
    subscription = fanout.subscribe()
    subscription.filter_to([])

    fanout.publish(error_frame())

    assert subscription.pending == 1
    assert isinstance(drain(subscription)[0], WsErrorFrame)


def test_a_filter_is_replaced_whole_and_resets_to_everything() -> None:
    fanout = Fanout()
    subscription = fanout.subscribe()

    subscription.filter_to(["AAPL"])
    assert subscription.symbols == frozenset({"AAPL"})

    subscription.filter_to(None)
    assert subscription.symbols is None

    fanout.publish(quote_frame("MSFT"))
    assert symbols_of(drain(subscription)) == ["MSFT"]
