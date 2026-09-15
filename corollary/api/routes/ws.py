"""``/api/ws``: the browser's live feed. Quotes and ``trade_updates``, nothing else.

What this module is
-------------------

The browser half of the transport, and only that half. It accepts a
connection, registers it with the process-wide fan-out
(:class:`corollary.api.fanout.Fanout`), writes frames to the socket as they
are published, reads the one message a client may send, and deregisters on the
way out. It opens no vendor connection and imports no vendor library: CLAUDE.md
permits ``import alpaca`` in exactly two files, ``data/providers/alpaca.py``
and ``engine/execution/alpaca.py``, and neither of them is a route. The vendor
sockets are a separate dispatch and they publish into the same fan-out.

**Nothing here records watchdog activity, deliberately.** Rule 9's switch
halts the engine when the *Alpaca* connection is lost, and a browser tab
opening or closing says nothing about whether Alpaca is connected. Wiring
``record_message`` or a stream open to this endpoint would arm that switch to
the wrong signal -- a halt fired by someone closing a laptop lid, or worse, a
dead feed masked by a healthy browser -- which is more dangerous than leaving
it unarmed. Those call sites belong with the vendor sockets.

What is **not** on this socket
------------------------------

Engine halt state and notifications. They are polled at 15s. Two reasons,
both from the frame contract decided 2026-09-13:

* Rule 9 halts *because* the socket closed, so a halt announcement cannot
  arrive on the thing that just died.
* A broken **client** socket has to stay distinguishable from a **halted
  engine**. Collapse them and a human presses Resume on an engine nobody
  halted, which rule 9 exists to stop.

``tests/api/test_ws.py`` pins both, including a source-level check on this
file, because the contract is an *absence* and an absence is what a
plausible-looking diff adds to.

Failures, where there is no status code to carry one
---------------------------------------------------

Every HTTP failure in this app comes back as ``ApiErrorResponse``. A socket
has no status code, so the envelope becomes a frame:
:class:`~corollary.api.schemas.WsErrorFrame` carries the same
:class:`~corollary.api.schemas.ApiErrorBody`, under the same ``error`` key,
with codes drawn from the same vocabulary -- ``invalid_request`` is the 422's
code and means the same thing here. One error shape for the whole API, so
``api.ts`` has one parser.

Two decisions that follow from it:

* **A refused message does not close the connection.** Closing would make a
  rejected subscription indistinguishable from a dead socket, which is the
  confusion the frame contract exists to prevent. The client is told and the
  feed continues.
* **A refusal is scrubbed by hand.** ``api/app.py``'s exception handlers never
  run for a websocket, so the rule 6 scrubbing they apply to every error body
  is applied here explicitly, through the same
  :func:`corollary.wire.vendor_detail`. **Redaction runs before truncation**,
  which is a rule and not an implementation detail: the redactor substitutes
  *literals*, so a value cut before it gets there matches nothing and keeps
  its prefix. Nothing in this module cuts a client value; ``_scrub`` bounds
  the assembled message and is the only thing that bounds anything. And
  nothing client-derived goes into a log record's ``inputs``, which is spread
  in unscrubbed -- counts and server-side constants only, typed so.

Rule 8 applies to every refusal: the rule, the inputs and the timestamp go to
the log, and the reason goes to the client. A subscription that is silently
ignored is the bug this project has already been bitten by at the other end of
the same pipe.
"""

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Final, Literal, TypeAlias

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from corollary.api.fanout import MAX_SUBSCRIBED_SYMBOLS, Fanout, Subscription
from corollary.api.schemas import (
    ApiErrorBody,
    WsErrorFrame,
    WsServerFrame,
    WsSubscribeRequest,
)
from corollary.wire import ERROR_BODY_MAX, vendor_detail

__all__ = ["WsErrorCode", "router", "stream"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["stream"])

#: A plausible symbol: an OCC contract or a ticker, upper case, dots allowed
#: for a class share (``BRK.B``). Shape only -- this refuses a client value
#: that cannot be a symbol at all, and does not pretend to know which symbols
#: exist. **Not upper-cased on the client's behalf**: silently accepting
#: ``aapl`` here would make it work on this one surface and nowhere else.
_SYMBOL: Final = re.compile(r"^[A-Z][A-Z0-9.]{0,31}$")

#: How many entries of a refused client *list* are quoted back. A count, not
#: a width: the length of each value is bounded by ``_scrub``, after
#: redaction, and never before it. See :func:`_echo`.
_ECHO_ITEMS: Final[int] = 5

#: What stands in for an ``inputs`` value that is not a number. See
#: :func:`_numbers_only`.
_NON_NUMERIC: Final[str] = "<non-numeric input omitted>"

#: Sent when the fan-out is missing, which means the app was constructed in a
#: way no shipped path constructs it. 1011 is the WebSocket code for an
#: internal error; the frame says so first, because a bare close code tells a
#: client nothing about whether to retry.
_INTERNAL_ERROR_CLOSE: Final[int] = 1011

WsErrorCode: TypeAlias = Literal[
    "invalid_request", "subscription_refused", "stream_unavailable"
]
"""Every ``code`` this socket may state. Three, drawn from the HTTP vocabulary.

:class:`~corollary.api.schemas.ApiErrorBody` types ``code`` as a free ``str``,
because one error shape serves the whole API and the API's vocabulary is
larger than this endpoint's. That freedom is a hole here: an error frame
carrying ``code="engine_halted"`` and a halt in its ``message`` would satisfy
the frame contract's *shape* while breaking the thing the contract exists for
-- the three kinds are pinned, and a halt riding an ``error`` frame is not a
fourth kind. So the transport's codes are pinned separately, as a type mypy
checks at the call site and a test checks as a set. Widening this alias is a
deliberate change with a test, the way extending the indicator whitelist is.
"""


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


@router.websocket("/ws")
async def stream(websocket: WebSocket) -> None:
    """Accept a browser, feed it frames, and clean up whatever happens.

    A client disconnecting -- cleanly or abruptly, mid-frame or mid-burst --
    is an ordinary event. It does not raise out of this function, does not log
    at ``ERROR``, and costs no other client a frame.
    """
    await websocket.accept()

    fanout = getattr(websocket.app.state, "fanout", None)
    if not isinstance(fanout, Fanout):
        await _state_its_failure(websocket)
        return

    subscription = fanout.subscribe()
    try:
        await _pump(websocket, subscription)
    finally:
        fanout.unsubscribe(subscription)


async def _state_its_failure(websocket: WebSocket) -> None:
    """No fan-out: say so on the socket rather than raising into the server.

    The constraint this serves is the one that outranks the feature:
    ``corollary.api:app`` must never stop being importable or stop answering
    ``GET /api/health``. An endpoint that raised here would take a page that
    needs no broker offline with it.
    """
    logger.error(
        "a client connected but no fan-out is attached to the app",
        extra={
            "event": "ws_fanout_missing",
            "rule": (
                "the socket states its failure rather than raising; the "
                "polled pages must keep answering"
            ),
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await _write(
        websocket,
        _error(
            "stream_unavailable",
            "The live feed is not running. The rest of the terminal is "
            "unaffected; its figures are polled.",
        ),
    )
    await websocket.close(code=_INTERNAL_ERROR_CLOSE)


async def _pump(websocket: WebSocket, subscription: Subscription) -> None:
    """Read in this coroutine, write in a child task, until the client goes.

    The two directions are independent -- the writer waits on the fan-out and
    the reader waits on the socket -- so one of them has to be concurrent.
    Which one is *not* a free choice, and getting it wrong is what the first
    version of this function did:

    **Nothing here may await an ``asyncio`` combinator.** The app runs inside
    an ``anyio`` cancel scope owned by the server (and by ``TestClient``), and
    anyio's asyncio backend recognises its own cancellation by the *message*
    on the ``CancelledError``. ``asyncio.wait`` and ``asyncio.gather`` raise a
    **fresh** ``CancelledError`` when they are cancelled, the message is lost,
    and the scope then cannot absorb the cancellation it issued -- so a
    perfectly ordinary disconnect surfaced as a cancelled server task.
    Awaiting the read directly keeps the original exception object intact all
    the way to the scope that raised it.

    So: the read is awaited here, the write is a child task, and the child is
    cancelled in a ``finally`` that reaps it without letting the reap's own
    cancellation escape.
    """
    writer = asyncio.create_task(
        _write_loop(websocket, subscription), name=f"{subscription.client_id}-write"
    )
    try:
        await _read(websocket, subscription)
    except Exception as error:
        # ``CancelledError`` is a ``BaseException`` and is deliberately not
        # caught: it belongs to the cancel scope that issued it.
        _log_end(subscription, error)
    finally:
        writer.cancel()
        # Reaping is courtesy, not correctness -- and while this coroutine is
        # itself being cancelled the await below raises immediately. Suppress
        # *that* exception only; the one already in flight resumes propagating
        # when this block ends.
        #
        # ``CancelledError`` and nothing else. This read ``suppress(
        # asyncio.CancelledError, Exception)``, which was broader than the
        # sentence above it and would have silently eaten a genuine bug raised
        # out of ``_log_end`` inside the writer -- the one place in this
        # module where a logging mistake would otherwise be invisible.
        # ``_write_loop`` already contains every ``Exception`` a client can
        # cause, so anything reaching here is ours.
        with contextlib.suppress(asyncio.CancelledError):
            await writer


def _log_end(subscription: Subscription, error: BaseException) -> None:
    """Record how a connection ended, at a level that matches what happened.

    A disconnect is ``DEBUG`` and a socket that went away under us is
    ``INFO``. Neither is an error: a browser tab closing is the most ordinary
    event this endpoint has, and logging it at ``ERROR`` would train whoever
    reads these logs to ignore the level that matters.
    """
    ordinary = (WebSocketDisconnect, ConnectionError, RuntimeError, OSError)
    if isinstance(error, WebSocketDisconnect):
        logger.debug(
            "%s disconnected",
            subscription.client_id,
            extra={"event": "ws_client_disconnected", "client": subscription.client_id},
        )
        return
    if isinstance(error, ordinary):
        logger.info(
            "%s went away: %s",
            subscription.client_id,
            type(error).__name__,
            extra={
                "event": "ws_client_gone",
                "client": subscription.client_id,
                "reason": type(error).__name__,
            },
        )
        return
    logger.error(
        "%s ended on an unexpected %s",
        subscription.client_id,
        type(error).__name__,
        exc_info=error,
        extra={
            "event": "ws_connection_failed",
            "client": subscription.client_id,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


async def _write_loop(websocket: WebSocket, subscription: Subscription) -> None:
    """Write frames until cancelled, or until the socket stops accepting them.

    A send that fails means the client is gone, which is ordinary: it is
    recorded at the level the reason deserves and the loop ends. The reader is
    the coroutine that ends the *connection*, and a client whose socket
    refuses a write has already stopped reading from it.
    """
    try:
        while True:
            frame = await subscription.next_frame()
            await _write(websocket, frame)
    except Exception as error:
        _log_end(subscription, error)


async def _write(websocket: WebSocket, frame: WsServerFrame) -> None:
    """One frame, serialized exactly as the HTTP routes serialize a response.

    ``model_dump_json`` honours ``ApiModel``'s ``serialize_by_alias``, so the
    wire is camelCase, and ``JsonMoney``'s ``when_used="json"`` serializer,
    so money is a JSON number produced at the last possible moment and read
    back by nothing.
    """
    await websocket.send_text(frame.model_dump_json())


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


async def _read(websocket: WebSocket, subscription: Subscription) -> None:
    """Consume client messages until the client goes away.

    Reads the raw ASGI message rather than ``receive_text``, which raises a
    bare ``KeyError`` on a binary frame -- a client bug deserves a stated
    reason, not a traceback in our log.
    """
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            logger.debug(
                "%s disconnected",
                subscription.client_id,
                extra={
                    "event": "ws_client_disconnected",
                    "client": subscription.client_id,
                    "code": message.get("code"),
                },
            )
            return

        text = message.get("text")
        if text is None:
            _refuse(
                websocket,
                subscription,
                code="invalid_request",
                message=(
                    "This socket reads JSON text frames. A binary frame has no "
                    "reader here and was discarded."
                ),
                rule="a client message is JSON text or it is refused",
                inputs={"bytes_received": len(message.get("bytes") or b"")},
            )
            continue

        _handle(websocket, subscription, text)


def _handle(websocket: WebSocket, subscription: Subscription, text: str) -> None:
    """Validate one client message and apply it, or say why not."""
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as error:
        _refuse(
            websocket,
            subscription,
            code="invalid_request",
            message=f"The message did not parse as JSON: {error.msg}.",
            rule="a client message is JSON text or it is refused",
            inputs={"length": len(text)},
        )
        return

    if not isinstance(payload, dict):
        _refuse(
            websocket,
            subscription,
            code="invalid_request",
            message=(
                "A client message is a JSON object with a `type`. "
                f"Received {type(payload).__name__}."
            ),
            rule="a client message is a tagged object or it is refused",
            inputs={"length": len(text)},
        )
        return

    kind = payload.get("type")
    if kind != "subscribe":
        _refuse(
            websocket,
            subscription,
            code="invalid_request",
            message=(
                f"Unknown frame type {_echo(kind)}. This socket accepts "
                "`subscribe`."
            ),
            rule="an unrecognised client frame type is refused whole",
            # The tag itself is client-derived, so it goes in ``message``,
            # which is scrubbed, and not in ``inputs``, which is not. The log
            # record carries that message, so nothing is lost by the move.
            inputs={"length": len(text)},
        )
        return

    try:
        request = WsSubscribeRequest.model_validate(payload)
    except ValidationError as error:
        _refuse(
            websocket,
            subscription,
            code="invalid_request",
            message=f"The subscribe message did not validate: {_fields(error)}.",
            rule="a client message validates against its schema or it is refused",
            inputs={"length": len(text)},
        )
        return

    _apply(websocket, subscription, request)


def _apply(
    websocket: WebSocket, subscription: Subscription, request: WsSubscribeRequest
) -> None:
    """Apply a validated ``subscribe`` -- whole, or not at all.

    The filter decides what this **connection** is sent. It does not decide
    what Corollary streams from the vendor: that is ``engine/stream.py``'s two
    budgets in priority order, where a client-supplied list is admitted at the
    lowest priority there is. Step 15 wires that half.
    """
    if request.symbols is None:
        subscription.filter_to(None)
        logger.info(
            "%s subscribed to every published symbol",
            subscription.client_id,
            extra={
                "event": "ws_subscribed",
                "client": subscription.client_id,
                "symbols": None,
            },
        )
        return

    asked = request.symbols
    if len(asked) > MAX_SUBSCRIBED_SYMBOLS:
        _refuse(
            websocket,
            subscription,
            code="subscription_refused",
            message=(
                f"{len(asked)} symbols is more than one connection may filter "
                f"on; the bound is {MAX_SUBSCRIBED_SYMBOLS}. The previous "
                "filter still stands."
            ),
            rule="a client-supplied list is bounded server-side",
            inputs={"asked": len(asked), "bound": MAX_SUBSCRIBED_SYMBOLS},
        )
        return

    malformed = tuple(symbol for symbol in asked if not _SYMBOL.match(symbol))
    if malformed:
        _refuse(
            websocket,
            subscription,
            code="subscription_refused",
            message=(
                f"{len(malformed)} of {len(asked)} entries are not symbols: "
                f"{_echo_all(malformed)}. The whole message is refused and the "
                "previous filter still stands -- a half-applied filter watches "
                "a list nobody asked for."
            ),
            rule="a subscription is applied whole or refused whole",
            inputs={"asked": len(asked), "malformed": len(malformed)},
        )
        return

    subscription.filter_to(asked)
    logger.info(
        "%s subscribed to %d symbols",
        subscription.client_id,
        len(subscription.symbols or ()),
        extra={
            "event": "ws_subscribed",
            "client": subscription.client_id,
            "symbols": len(subscription.symbols or ()),
        },
    )


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def _refuse(
    websocket: WebSocket,
    subscription: Subscription,
    *,
    code: WsErrorCode,
    message: str,
    rule: str,
    inputs: Mapping[str, int],
) -> None:
    """Log the refusal and tell the client, on one connection only.

    Rule 8's three parts are the ``extra``: the rule, the inputs and the
    timestamp. The reason reaches the client too, because a subscription that
    is silently ignored looks exactly like a feed that has nothing to say.

    **``inputs`` carries counts and server-side constants only**, which is
    why it is typed ``Mapping[str, int]`` rather than ``Mapping[str, Any]``.
    Only ``message`` is scrubbed -- ``inputs`` is spread into the record as it
    arrives -- so a client-derived value routed through it would reach the log
    unredacted, which is how a correctly shaped account number and a
    twenty-character key id both got there. The rule chosen over scrubbing
    both halves is the one a future call site cannot get wrong: nothing
    client-derived goes in ``inputs`` at all, mypy refuses a string here, and
    :func:`_numbers_only` is the backstop for the diff that casts past it.
    A client value that is worth quoting goes in ``message``, which is
    scrubbed, and the log record carries that message.

    The error frame goes through the connection's own queue rather than
    straight to the socket: the writer task owns the socket, and two
    coroutines sending on one websocket can interleave a frame.
    """
    detail = _scrub(websocket, message)
    logger.warning(
        "refused a client message from %s: %s",
        subscription.client_id,
        detail,
        extra={
            "event": "ws_refusal",
            "rule": rule,
            "code": code,
            "client": subscription.client_id,
            "at": datetime.now(timezone.utc).isoformat(),
            **_numbers_only(inputs),
        },
    )
    subscription.offer(_error(code, detail))


def _error(code: WsErrorCode, message: str) -> WsErrorFrame:
    """The **one** place this module builds an error frame.

    One constructor so that :data:`WsErrorCode` actually binds. Typing
    ``_refuse``'s ``code`` parameter was not enough on its own:
    :func:`_state_its_failure` built its frame inline, and
    ``ApiErrorBody.code`` is a free ``str``, so mypy would have accepted
    ``code="engine_halted"`` there -- a halt announcement on the socket, which
    is the one thing the frame contract exists to forbid. ``test_ws.py``
    pins that this stays the only ``WsErrorFrame(...)`` in the file, because a
    second one would reopen the hole without touching this line.
    """
    return WsErrorFrame(error=ApiErrorBody(code=code, message=message))


def _numbers_only(inputs: Mapping[str, int]) -> dict[str, object]:
    """Rule 6's backstop on a log record's ``inputs``: numbers, or a placeholder.

    :func:`_refuse` types ``inputs`` as ``Mapping[str, int]`` and that is the
    primary enforcement, because ``uv run mypy corollary`` has to be clean.
    This is the same rule at runtime, for the diff that arrives with a cast or
    an ``Any`` on it -- the log is the durable surface and it is not worth
    trusting a type alone with a credential.

    A non-numeric value is **replaced rather than dropped**: rule 8 wants a
    reader to see that a field was there. ``bool`` passes, being an ``int``;
    a ``float`` does not, and money must never be here in any case.
    """
    return {
        key: (value if isinstance(value, int) else _NON_NUMERIC)
        for key, value in inputs.items()
    }


def _scrub(websocket: WebSocket, text: str) -> str:
    """Bound and de-identify text before it becomes a frame.

    The same helper, and the same reasoning, as ``api/app.py``'s ``_scrub``:
    every credential in the environment by literal substitution, the account
    number by shape, then a length bound. Applied here by hand because the
    exception handlers that would otherwise do it never run for a websocket.

    **This is the only place a client value is bounded**, and the ordering is
    the reason: ``vendor_detail`` redacts and *then* truncates, and no caller
    may cut a value before handing it over. ``limit`` is passed rather than
    left to default so that the bound reads at the call site next to the text
    it applies to -- a per-value cut upstream is what a reader would
    otherwise reach for.
    """
    provider = getattr(websocket.app.state, "secret_values", None)
    secrets: Sequence[str] = provider() if callable(provider) else ()
    return vendor_detail(text, secrets=secrets, limit=ERROR_BODY_MAX)


def _fields(error: ValidationError) -> str:
    """Pydantic's field locations and messages -- never the offending input.

    ``errors()`` carries the ``input`` alongside each entry, and echoing it is
    how a mistyped message that happened to contain a credential ends up
    quoted back. ``api/app.py``'s 422 handler drops it for the same reason.
    """
    return (
        "; ".join(
            f"{'.'.join(str(part) for part in entry['loc'])}: {entry['msg']}"
            for entry in error.errors()
        )
        or "the message did not validate"
    )


def _echo(value: object) -> str:
    """One client-supplied value, quoted back **whole**. Bounding happens later.

    Deliberately does not truncate, and this function existing at all is what
    keeps that decision in one place. It used to cut the value to 24
    characters, which put the two operations in the wrong order:
    ``vendor_detail`` redacts by **literal substitution**, so a 40-character
    Alpaca secret already cut to 24 matched no literal and its prefix reached
    both the frame and the log record. ``wire.py`` forbids that ordering in
    its own words -- *"the other order can cut an identifier in half and keep
    the half, which is worth no less to whoever reads the log"* -- and says
    that is why ``limit`` is a parameter there rather than a second function.

    The cut bought nothing anyway: :func:`_scrub` bounds the whole assembled
    message at :data:`~corollary.wire.ERROR_BODY_MAX`, so an unbounded echo
    was never what this prevented. It only defeated the redactor. The path
    with no ``_echo`` on it -- :func:`_fields`, where a client-supplied extra
    field name arrives through pydantic's ``loc`` -- redacts correctly for
    exactly this reason.
    """
    return f"`{value}`"


def _echo_all(values: Sequence[str]) -> str:
    """The first few of a client-supplied list, with a count of the rest.

    Bounded in the number of entries, never in the width of one: the width
    bound is ``_scrub``'s, applied to the whole message after redaction.
    """
    shown = ", ".join(_echo(value) for value in values[:_ECHO_ITEMS])
    remaining = len(values) - _ECHO_ITEMS
    return shown if remaining <= 0 else f"{shown} and {remaining} more"
