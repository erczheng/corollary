"""One published frame, every connected client. The single place a quote fans out.

**Why this lives under ``api/`` rather than in the engine.** It carries wire
frames -- :data:`~corollary.api.schemas.WsServerFrame` -- and its only reader
is ``api/routes/ws.py``, so it is browser-transport state. Putting it in
``engine/`` would mean either the engine importing ``api.schemas``, which is
backwards, or inventing a second set of domain types for the transport to
translate from, which is a second shape for the same event and a second place
for it to drift. The vendor half (a separate dispatch, because CLAUDE.md
permits ``import alpaca`` in exactly two files and neither of them is a route)
translates a vendor message into a domain object and then into a frame, and
publishes it here.

**Why the instance lives on ``app.state`` rather than at module level.** The
hard requirement is that there is *one* place a quote reaches every client, so
two clients cannot disagree about a price -- the invariant CLAUDE.md states
for the frontend's ``underlyings`` map. One app means one fan-out, and the
design spec's decision 1 says one process, so ``app.state`` satisfies that
exactly. A module-level singleton would satisfy it *too* well: two tests'
apps would share one hub and a leaked subscriber from one test would be
delivered frames in another.

**Publishing is loop-affine and non-blocking.** :meth:`Fanout.publish` must be
called from the event-loop thread -- it hands frames to
:class:`asyncio.Queue` objects, and ``put_nowait`` from a foreign thread
neither wakes the waiting consumer nor is safe. It never awaits: a slow client
must not be able to stall the vendor socket that is feeding it, which is the
failure that would turn one stuck browser tab into a stale quote map for
everything.

**A backlog drops the oldest quote, and says so.** The queue is bounded, and
when it is full the oldest **quote** is evicted -- not simply the oldest
frame. The justification for dropping anything only covers quotes: a stale
quote is superseded by the next quote for the same symbol, so the one the
screen needs is the current one. A ``trade_update`` is superseded by nothing,
and neither is an error frame.

That asymmetry used to be *noticed here and not implemented*: :meth:`
Subscription.wants` was carefully kind-aware while eviction was kind-blind, so
a connection at its frame bound during an opening burst could lose a fill to
one more AAPL quote. There is no replay, and a ``trade_updates`` event is what
*triggers* the REST refetch of a position -- so the trigger was what was lost
and the row sat pre-fill until the 15s poll. The same mechanism ate a
:class:`~corollary.api.schemas.WsErrorFrame`, leaving a client believing it
was watching a list it was not. If the queue holds no quote at all, the oldest
frame goes: something has to, and it is still never the newest.

Rule 8 applies to a dropped frame as much as to a rejected order, so the
eviction logs the rule, the inputs, the timestamp -- and the *kind*, so a
dropped ``trade_update`` is visible in the log rather than pooled in with the
quotes it is much more consequential than.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from itertools import count
from typing import Final

from corollary.api.schemas import (
    OrderSide,
    WsQuote,
    WsQuoteFrame,
    WsServerFrame,
    WsTradeUpdate,
    WsTradeUpdateFrame,
)
from corollary.data.providers.interface import Quote
from corollary.engine.execution.interface import PositionIntent, TradeUpdate

__all__ = [
    "DEFAULT_FRAME_QUEUE_SIZE",
    "MAX_SUBSCRIBED_SYMBOLS",
    "Fanout",
    "Subscription",
    "quote_frame",
    "quote_sink",
    "trade_update_frame",
    "trade_update_sink",
    "wire_action",
]

logger = logging.getLogger(__name__)

#: Frames one connection may fall behind by before the oldest is evicted.
#:
#: Sized against the two stream budgets rather than picked: 30 equity symbols
#: plus 200 option quotes is 230 distinct symbols, so this is a little over
#: one full sweep of everything Corollary can stream. A client that cannot
#: keep up with a whole sweep is not behind, it is gone.
DEFAULT_FRAME_QUEUE_SIZE: Final[int] = 256

#: The most symbols one connection may name in a ``subscribe`` message.
#:
#: Rule 4: the value arrived from the client, so the ceiling is server-side.
#: The number is deliberately **above** the 230 symbols the two vendor
#: budgets can carry between them -- this bound exists to refuse an unbounded
#: list, not to be the binding constraint. Were it lower than the vendor caps
#: it could hide a vendor drop behind a transport refusal, and *"N symbols not
#: streamed"* would then be answering a different question than it claims.
MAX_SUBSCRIBED_SYMBOLS: Final[int] = 256


# --------------------------------------------------------------------------
# Domain object to frame -- the vendor half's last step, and the arrow's
# direction
# --------------------------------------------------------------------------
#
# The two vendor sockets translate a message into a domain object -- a
# `Quote` from `data/providers/interface.py`, a `TradeUpdate` from
# `engine/execution/interface.py` -- and hand it to a sink. The sink is built
# here, so the *frame* is assembled under `api/` and no module in `engine/`
# or `data/` imports `api.schemas`. That is the layering part 1 established,
# stated as code: this file already depends on the wire shapes because it
# carries them, and adding a dependency on the two domain shapes points
# inward. The reverse -- a provider importing a Pydantic wire model -- would
# put the browser's contract in the engine's import graph, and a second set
# of domain types for the transport to translate from would be a second
# place for the same event to drift.


def quote_frame(quote: Quote) -> WsQuoteFrame:
    """A domain :class:`Quote` as the frame the browser reads.

    Field for field, with no derivation: ``WsQuote`` deliberately carries no
    ``mid``, because a crossed quote has none and the judgement lives on
    ``Quote.mid`` where the vendor half can read it.
    """
    return WsQuoteFrame(
        quote=WsQuote(
            symbol=quote.symbol,
            bid=quote.bid,
            ask=quote.ask,
            bid_size=quote.bid_size,
            ask_size=quote.ask_size,
            at=quote.at,
        )
    )


#: The engine's four-way action as the wire's spelling of it. A mapping
#: rather than string surgery, so an intent Alpaca adds arrives as a missing
#: key -- loudly -- instead of as a plausible wrong action on a fill.
_WIRE_ACTION: Final[dict[PositionIntent, OrderSide]] = {
    PositionIntent.BUY_TO_OPEN: "BTO",
    PositionIntent.BUY_TO_CLOSE: "BTC",
    PositionIntent.SELL_TO_OPEN: "STO",
    PositionIntent.SELL_TO_CLOSE: "STC",
}


def wire_action(intent: PositionIntent | None) -> OrderSide | None:
    """``PositionIntent`` as the four-value literal the browser reads.

    ``None`` stays ``None``: an ``mleg`` parent has no action, and the wire
    field is nullable for exactly that reason.
    """
    if intent is None:
        return None
    return _WIRE_ACTION[intent]


def trade_update_frame(update: TradeUpdate) -> WsTradeUpdateFrame:
    """A domain :class:`TradeUpdate` as the frame the browser reads."""
    return WsTradeUpdateFrame(
        update=WsTradeUpdate(
            event=update.event,
            at=update.at,
            order_id=update.order_id,
            symbol=update.symbol,
            status=update.status,
            action=wire_action(update.action),
            quantity=update.quantity,
            filled_quantity=update.filled_quantity,
            fill_price=update.fill_price,
            fill_quantity=update.fill_quantity,
            filled_avg_price=update.filled_avg_price,
            position_quantity=update.position_quantity,
        )
    )


def quote_sink(fanout: "Fanout") -> Callable[[Quote], None]:
    """The callable a market-data socket is handed. Synchronous, by contract.

    :meth:`Fanout.publish` never awaits, so a slow browser tab cannot stall a
    vendor socket -- which is why the socket takes a plain callable rather
    than a coroutine. Keeping the sink synchronous is what keeps that
    guarantee checkable from the socket's side: there is nothing to await, so
    there is nothing that can block.
    """

    def publish(quote: Quote) -> None:
        fanout.publish(quote_frame(quote))

    return publish


def trade_update_sink(fanout: "Fanout") -> Callable[[TradeUpdate], None]:
    """The callable the ``trade_updates`` socket is handed."""

    def publish(update: TradeUpdate) -> None:
        fanout.publish(trade_update_frame(update))

    return publish


def frame_kind(frame: WsServerFrame) -> str:
    """The frame's ``type`` tag, for a log record."""
    return frame.type


class Subscription:
    """One connected client's queue, its filter, and what it has missed.

    Held by the endpoint for the life of the connection and handed back to
    :meth:`Fanout.unsubscribe` in a ``finally``. Nothing here is awaited
    except :meth:`next_frame`.
    """

    def __init__(self, *, client_id: str, queue_size: int) -> None:
        if queue_size < 1:
            raise ValueError(
                f"a subscription needs room for at least one frame; got {queue_size}"
            )
        #: Identifies the connection in a log record. Not a credential and not
        #: derived from one -- rule 6 -- and not a client-supplied value
        #: either, so nothing a browser sends can name another connection.
        self.client_id = client_id
        self.queue_size = queue_size
        #: Frames evicted because this client fell behind. **Server-side
        #: only**: it reaches the eviction log record and the unsubscribe
        #: record, and no frame carries it to the browser.
        #:
        #: The comment here used to read *"so the transport can state it"*,
        #: and nothing stated it -- no frame kind, no field. Corrected rather
        #: than implemented, deliberately: telling the client costs either a
        #: fourth frame kind, which the frame contract pins at three and
        #: tests guard, or an ``error`` frame per eviction, which is
        #: self-amplifying -- the queue is full, and ``wants`` may never
        #: filter an error, so announcing the drop evicts another frame. The
        #: honest client-side signal is per-symbol staleness (``LiveStatus``
        #: is global today, so a stale price can sit under a "Live" badge),
        #: and that is a frontend change with a designed state, not a field
        #: smuggled onto the socket by a fix-up.
        self.dropped = 0
        self._queue: asyncio.Queue[WsServerFrame] = asyncio.Queue(maxsize=queue_size)
        self._symbols: frozenset[str] | None = None

    @property
    def symbols(self) -> frozenset[str] | None:
        """The quote filter, or ``None`` for every symbol published."""
        return self._symbols

    @property
    def pending(self) -> int:
        """Frames queued and not yet written to the socket."""
        return self._queue.qsize()

    def filter_to(self, symbols: Iterable[str] | None) -> None:
        """Replace the quote filter whole. ``None`` means every symbol.

        Replaced rather than merged: a client that has scrolled away from a
        symbol has no way to say so with a merge, and the accumulated union
        would quietly become "everything you have ever looked at".
        """
        self._symbols = None if symbols is None else frozenset(symbols)

    def wants(self, frame: WsServerFrame) -> bool:
        """Whether this connection should receive ``frame``.

        **The filter gates quotes and nothing else.** A ``trade_update`` is
        the broker reporting on money this account has committed, and an error
        is this connection being told what it did wrong; neither is a thing a
        viewport hint may suppress. A filter that could hide a fill is how an
        order executes and the screen never hears about it.
        """
        if self._symbols is None:
            return True
        if isinstance(frame, WsQuoteFrame):
            return frame.quote.symbol in self._symbols
        return True

    def offer(self, frame: WsServerFrame) -> None:
        """Queue a frame, evicting the oldest quote if this client is behind.

        Never blocks and never awaits, so one slow client costs the publisher
        nothing. Called by :meth:`Fanout.publish` for a broadcast and directly
        by the endpoint for an error frame, which belongs to one connection and
        is never broadcast.
        """
        try:
            self._queue.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass

        evicted = self._evict()
        self.dropped += 1
        logger.warning(
            "dropped a %s frame for %s: the client is %d frames behind",
            frame_kind(evicted) if evicted is not None else "queued",
            self.client_id,
            self.queue_size,
            extra={
                "event": "ws_frame_dropped",
                "rule": (
                    "a client that falls a full queue behind loses its oldest "
                    "quote, because a quote is superseded by the next quote "
                    "for the same symbol and a fill or a refusal is superseded "
                    "by nothing; with no quote queued the oldest frame goes, "
                    "and blocking the publisher instead would stale every "
                    "other client"
                ),
                "client": self.client_id,
                "dropped_kind": (
                    frame_kind(evicted) if evicted is not None else None
                ),
                "kept_kind": frame_kind(frame),
                "queue_size": self.queue_size,
                "dropped_total": self.dropped,
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        self._queue.put_nowait(frame)

    def _evict(self) -> WsServerFrame | None:
        """Remove and return the oldest quote, or the oldest frame if none is.

        Written the long way on purpose. ``asyncio.Queue`` can only be read
        from the front, and the frame that has to go may not be the front one,
        so the queue is drained into a list, one frame is removed from it, and
        the rest go back **in the same order**. Reaching into the queue's
        private deque would be shorter and would couple this module to a
        standard-library internal that carries money.

        O(n) in the queue depth, on the eviction path only: a client that is
        not behind never reaches this, and one that is has at most
        :data:`DEFAULT_FRAME_QUEUE_SIZE` frames. Nothing here awaits, so no
        consumer can observe the queue mid-rebuild -- a waiting
        :meth:`next_frame` cannot resume until this returns.
        """
        queued: list[WsServerFrame] = []
        while True:
            try:
                queued.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not queued:  # pragma: no cover - the consumer raced us
            return None

        # The first quote is the oldest quote. Falling back to index 0 keeps
        # the property the kind-blind version had and this must not lose: the
        # oldest goes, never the newest.
        index = next(
            (
                position
                for position, queued_frame in enumerate(queued)
                if isinstance(queued_frame, WsQuoteFrame)
            ),
            0,
        )
        evicted = queued.pop(index)
        for kept in queued:
            self._queue.put_nowait(kept)
        return evicted

    async def next_frame(self) -> WsServerFrame:
        """The next frame to write, waiting for one if the queue is empty."""
        return await self._queue.get()


class Fanout:
    """Every connected browser, and the one call that reaches all of them."""

    def __init__(self, *, queue_size: int = DEFAULT_FRAME_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscriptions: dict[str, Subscription] = {}
        self._ids = count(1)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def subscribe(self) -> Subscription:
        """Register a connection. The caller must hand the result back.

        No replay: a frame published before a client connected is not
        delivered to it. The socket is a live feed, and the authoritative
        state of a position or an order comes from the polled REST routes --
        a socket that replayed history would be a second source for figures
        that already have one.
        """
        subscription = Subscription(
            client_id=f"ws-{next(self._ids)}", queue_size=self._queue_size
        )
        self._subscriptions[subscription.client_id] = subscription
        logger.info(
            "%s subscribed; %d connected",
            subscription.client_id,
            len(self._subscriptions),
            extra={
                "event": "ws_client_subscribed",
                "client": subscription.client_id,
                "clients": len(self._subscriptions),
            },
        )
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Deregister a connection. Idempotent, because it runs in a ``finally``.

        A disconnect is ordinary: it logs at ``INFO``, never at ``ERROR``, and
        takes nothing from any other client.
        """
        if self._subscriptions.pop(subscription.client_id, None) is None:
            return
        logger.info(
            "%s unsubscribed; %d connected",
            subscription.client_id,
            len(self._subscriptions),
            extra={
                "event": "ws_client_unsubscribed",
                "client": subscription.client_id,
                "clients": len(self._subscriptions),
                "dropped_total": subscription.dropped,
            },
        )

    def publish(self, frame: WsServerFrame) -> None:
        """Hand one frame to every subscriber that wants it.

        The frame is built **once** and the same object reaches everyone, so
        there is no second copy of a price to disagree with the first.

        Iterates a snapshot, so a client disconnecting during a broadcast
        cannot mutate the mapping underneath it -- and one subscriber raising
        is contained rather than cutting the broadcast short, because a broken
        client is a broken client and not an outage. The filter check is
        *inside* that containment: deciding whether a client wants a frame
        reads the frame, and a reader that can raise must not be able to take
        the publisher down with it.
        """
        for subscription in tuple(self._subscriptions.values()):
            try:
                if subscription.wants(frame):
                    subscription.offer(frame)
            except Exception:
                logger.exception(
                    "could not queue a %s frame for %s",
                    frame_kind(frame),
                    subscription.client_id,
                    extra={
                        "event": "ws_publish_failed",
                        "rule": (
                            "one unreachable client does not cost the others "
                            "their frame"
                        ),
                        "client": subscription.client_id,
                        "frame_kind": frame_kind(frame),
                        "at": datetime.now(timezone.utc).isoformat(),
                    },
                )
