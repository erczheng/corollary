"""``/api/ws``: the browser transport, its frames, and what is *not* on it.

Three things in here are the point, and the rest is lifecycle.

**One source, one price.** Two connected clients receive the *same bytes* for
one published quote, because there is one fan-out and the frame is built once.
CLAUDE.md spends a paragraph on the frontend's ``underlyings`` map for the same
reason -- one symbol has one price, and a second copy is how the Markets table
and an Activity row end up disagreeing about AAPL.

**A disconnect is ordinary.** It must not raise out of the endpoint, must not
log at ``ERROR``, and must not cost the other client a frame. So the
disconnect tests assert on the *other* client and on ``caplog``, not on the
one that left.

**Engine state and notifications are not on this socket**, and that is pinned
here rather than written in a comment. Rule 9 halts *because* the socket
closed, so a halt notification cannot arrive on the thing that just died; and
a broken client socket has to stay distinguishable from a halted engine, or a
human presses Resume on an engine that was never halted. Both facts are
invisible in the code -- they are an *absence* -- which is exactly the kind of
thing a later dispatch adds "just one more frame type" to.

**Every test that receives publishes something that must arrive**, and asserts
on which frame came *first*. ``WebSocketTestSession.receive_json`` has no
timeout, so a test whose expected frame is never sent does not fail -- it
blocks until the suite is killed, and a hang says nothing about which rule
broke. Ordering the publishes so that a violated rule delivers the *wrong*
frame turns every one of these into a fast assertion. Both filter tests were
written the other way first and hung under a deliberately broken filter.

Frames are published through a **test-only HTTP route** rather than by calling
``fanout.publish`` from the test thread. That is not ceremony either:
``TestClient`` runs the app in an anyio portal on another thread, and
``asyncio.Queue.put_nowait`` from a foreign thread neither wakes the waiting
consumer nor is safe to call. The route publishes from inside the event loop,
which is where the vendor half will publish from too.
"""

import logging
from collections.abc import Iterator
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from corollary.api.app import create_app
from corollary.api.deps import ServiceRegistry
from corollary.api.fanout import MAX_SUBSCRIBED_SYMBOLS, Fanout
from corollary.api.schemas import (
    WsQuote,
    WsQuoteFrame,
    WsServerFrame,
    WsTradeUpdate,
    WsTradeUpdateFrame,
)

WS = "/api/ws"

AAPL_QUOTE: dict[str, Any] = {
    "symbol": "AAPL",
    "bid": "189.40",
    "ask": "189.48",
    "bidSize": 3,
    "askSize": 7,
    "at": "2026-09-14T14:30:00Z",
}

MSFT_QUOTE: dict[str, Any] = {**AAPL_QUOTE, "symbol": "MSFT"}

FILL_UPDATE: dict[str, Any] = {
    "event": "fill",
    "at": "2026-09-14T14:30:01Z",
    "orderId": "5f0c1a2b-0000-4000-8000-000000000001",
    "symbol": "AAPL241220C00150000",
    "status": "filled",
    "action": "BTO",
    "quantity": 2,
    "filledQuantity": 2,
    "fillPrice": "3.40",
    "fillQuantity": 2,
    "filledAvgPrice": "3.40",
    "positionQuantity": 2,
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def ws_app(registry: ServiceRegistry, db_engine: Engine) -> FastAPI:
    """The real app, plus two publishing routes no shipped build has.

    They stand in for the vendor half, which is a separate dispatch: a browser
    socket opening says nothing about whether Alpaca is connected, so nothing
    in this step may open a vendor connection.
    """
    app = create_app(registry=registry, db_engine=db_engine)

    @app.post("/test/publish/quote")
    async def _publish_quote(quote: WsQuote) -> dict[str, int]:
        fanout: Fanout = app.state.fanout
        fanout.publish(WsQuoteFrame(quote=quote))
        return {"subscribers": fanout.subscriber_count}

    @app.post("/test/publish/trade-update")
    async def _publish_update(update: WsTradeUpdate) -> dict[str, int]:
        fanout: Fanout = app.state.fanout
        fanout.publish(WsTradeUpdateFrame(update=update))
        return {"subscribers": fanout.subscriber_count}

    return app


@pytest.fixture
def ws_client(ws_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(ws_app) as client:
        yield client


def publish_quote(client: TestClient, quote: dict[str, Any]) -> int:
    response = client.post("/test/publish/quote", json=quote)
    assert response.status_code == 200, response.text
    return int(response.json()["subscribers"])


def publish_update(client: TestClient, update: dict[str, Any]) -> int:
    response = client.post("/test/publish/trade-update", json=update)
    assert response.status_code == 200, response.text
    return int(response.json()["subscribers"])


def corollary_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """``ERROR``-or-worse records from our own loggers, as messages."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR and record.name.startswith("corollary")
    ]


def refusals(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The rule 8 records a refusal writes, in order."""
    return [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "ws_refusal"
    ]


def leaks(record: logging.LogRecord, fragment: str) -> list[str]:
    """Which fields of a log record carry ``fragment``, if any.

    Every field, not just ``message``: a structured record's ``extra`` is the
    half that gets shipped to a log store and searched, so rule 6 applies to
    it at least as strongly.
    """
    return sorted(
        key for key, value in record.__dict__.items() if fragment in str(value)
    )


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_a_client_connects_and_receives_a_quote(ws_client: TestClient) -> None:
    with ws_client.websocket_connect(WS) as socket:
        assert publish_quote(ws_client, AAPL_QUOTE) == 1
        frame = socket.receive_json()

    assert frame["type"] == "quote"
    assert frame["quote"]["symbol"] == "AAPL"
    # camelCase on the wire, and money as a JSON *number* -- the one
    # sanctioned float, produced at the serializer and nowhere else.
    assert frame["quote"]["bidSize"] == 3
    assert frame["quote"]["bid"] == 189.40
    assert isinstance(frame["quote"]["bid"], float)


def test_a_clean_disconnect_deregisters_the_connection(ws_client: TestClient) -> None:
    with ws_client.websocket_connect(WS):
        assert publish_quote(ws_client, AAPL_QUOTE) == 1

    # The ``finally`` in the endpoint ran: nobody is subscribed, so a later
    # publish reaches nothing rather than piling up in a queue nobody reads.
    assert publish_quote(ws_client, AAPL_QUOTE) == 0


def test_a_disconnect_with_frames_unread_is_ordinary(
    ws_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The abrupt case: the client stops reading and goes away mid-stream."""
    caplog.set_level(logging.DEBUG)
    with ws_client.websocket_connect(WS):
        for _ in range(5):
            publish_quote(ws_client, AAPL_QUOTE)
        # No ``receive_json`` at all: five frames are queued and abandoned.

    assert publish_quote(ws_client, AAPL_QUOTE) == 0
    assert corollary_errors(caplog) == []


def test_one_client_leaving_does_not_cost_the_other_a_frame(
    ws_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with ws_client.websocket_connect(WS) as stays:
        with ws_client.websocket_connect(WS):
            assert publish_quote(ws_client, AAPL_QUOTE) == 2
        # One client is gone and did not read its frame. The other is still
        # subscribed and the next publish reaches it.
        assert publish_quote(ws_client, MSFT_QUOTE) == 1
        first = stays.receive_json()
        second = stays.receive_json()

    assert [first["quote"]["symbol"], second["quote"]["symbol"]] == ["AAPL", "MSFT"]
    assert corollary_errors(caplog) == []


# --------------------------------------------------------------------------
# One source, one price
# --------------------------------------------------------------------------


def test_two_clients_receive_the_same_quote_from_one_source(
    ws_client: TestClient,
) -> None:
    with ws_client.websocket_connect(WS) as first:
        with ws_client.websocket_connect(WS) as second:
            assert publish_quote(ws_client, AAPL_QUOTE) == 2
            one = first.receive_json()
            two = second.receive_json()

    assert one == two
    assert one["quote"]["bid"] == 189.40


def test_the_app_holds_exactly_one_fanout(ws_app: FastAPI) -> None:
    """The single-source invariant, as a property of the app rather than a habit.

    Connections come and go; the fan-out does not. A per-connection fan-out
    would pass every test above and still let two clients disagree, because
    each would be reading a different source.
    """
    with TestClient(ws_app) as client:
        fanout = ws_app.state.fanout
        assert isinstance(fanout, Fanout)
        with client.websocket_connect(WS), client.websocket_connect(WS):
            assert ws_app.state.fanout is fanout
            assert fanout.subscriber_count == 2


# --------------------------------------------------------------------------
# The frame contract
# --------------------------------------------------------------------------


def test_a_client_can_tell_a_quote_from_a_trade_update(ws_client: TestClient) -> None:
    with ws_client.websocket_connect(WS) as socket:
        publish_quote(ws_client, AAPL_QUOTE)
        publish_update(ws_client, FILL_UPDATE)
        frames = [socket.receive_json(), socket.receive_json()]

    kinds = [frame["type"] for frame in frames]
    assert kinds == ["quote", "trade_update"]
    # Discriminated, not guessed: the payload lives under a key named after
    # the kind, so reading the wrong one is a ``KeyError`` rather than a
    # plausible object with missing fields.
    assert set(frames[0]) == {"type", "quote"}
    assert set(frames[1]) == {"type", "update"}
    assert frames[1]["update"]["event"] == "fill"
    assert frames[1]["update"]["action"] == "BTO"
    assert frames[1]["update"]["fillPrice"] == 3.40


def test_the_socket_carries_quotes_trade_updates_and_errors_only() -> None:
    """Rule 9's half of the frame contract, pinned.

    Engine state and notifications are **polled at 15s**. A halt announced on
    the socket cannot arrive when the socket is what died, and a client that
    cannot tell a broken connection from a halted engine invites a human to
    press Resume on an engine nobody halted.
    """
    members = get_args(get_args(WsServerFrame)[0])
    kinds = {get_args(member.model_fields["type"].annotation)[0] for member in members}

    assert kinds == {"quote", "trade_update", "error"}


def test_no_frame_carries_engine_state_or_a_notification() -> None:
    members = get_args(get_args(WsServerFrame)[0])
    fields = {
        field
        for member in members
        for field in member.model_fields
        if field != "type"
    }
    nested = {
        name
        for member in members
        for field, info in member.model_fields.items()
        if field != "type"
        for name in getattr(info.annotation, "model_fields", {})
    }

    assert fields == {"quote", "update", "error"}
    forbidden = {"halted", "halted_reason", "halted_at", "notification", "severity"}
    assert nested & forbidden == set()


def test_the_ws_module_does_not_reach_for_engine_state() -> None:
    """A source-level pin, because the contract is an absence.

    The same standard ``api/routes/engine.py`` holds ``_clear_halt`` to: a
    rule a reviewer has to *notice* is a rule that gets lost in a
    plausible-looking diff.

    **Read from the parse tree, not from the text.** The first version of
    this test asserted ``f"import {forbidden}" not in source``, which catches
    only the single-line form -- and ``ws.py`` already imports its schemas in
    the parenthesized multi-name form, so adding ``NotificationResponse`` to
    that block would have defeated a guard whose docstring claimed otherwise.
    A guard that reads stronger than it is gets trusted. Identifiers come
    from the tree too, so the prose in this module's own docstring (which has
    to be able to say *why* notifications are absent) is not a false
    positive.
    """
    import ast
    from pathlib import Path

    import corollary.api.routes.ws as ws_module

    tree = ast.parse(Path(ws_module.__file__).read_text(encoding="utf-8"))

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
            names.update(alias.asname for alias in node.names if alias.asname)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
            names.update(alias.asname for alias in node.names if alias.asname)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)

    forbidden = ("EngineState", "engine_state", "Notification", "notification", "halt")
    for name in sorted(names):
        assert not any(bad in name for bad in forbidden), name


# --------------------------------------------------------------------------
# Client messages, and rule 8 on a refusal
# --------------------------------------------------------------------------


def test_a_subscribed_client_receives_only_its_symbols(ws_client: TestClient) -> None:
    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": "subscribe", "symbols": ["AAPL"]})
        # A MSFT quote is published *first*; the AAPL frame arriving first is
        # what proves the MSFT one was never sent rather than merely late.
        publish_quote(ws_client, MSFT_QUOTE)
        publish_quote(ws_client, AAPL_QUOTE)
        frame = socket.receive_json()

    assert frame["quote"]["symbol"] == "AAPL"


def test_a_filter_never_suppresses_a_trade_update(ws_client: TestClient) -> None:
    """A fill is about money, not about what is on screen.

    A viewport hint that could hide an execution is how an order fills and the
    screen never hears about it.
    """
    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": "subscribe", "symbols": ["NVDA"]})
        publish_quote(ws_client, AAPL_QUOTE)
        publish_update(ws_client, FILL_UPDATE)
        # A frame that passes the filter, published last, so a filter that
        # wrongly gated the update fails *here* rather than blocking on a
        # receive that never resolves.
        publish_quote(ws_client, {**AAPL_QUOTE, "symbol": "NVDA"})
        frame = socket.receive_json()

    assert frame["type"] == "trade_update"


def test_subscribing_to_null_symbols_means_every_symbol(
    ws_client: TestClient,
) -> None:
    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": "subscribe", "symbols": ["AAPL"]})
        socket.send_json({"type": "subscribe", "symbols": None})
        publish_quote(ws_client, MSFT_QUOTE)
        # Deliverable under the *old* filter, and published second, so a reset
        # that did not take arrives as AAPL rather than as a hang.
        publish_quote(ws_client, AAPL_QUOTE)
        frame = socket.receive_json()

    assert frame["quote"]["symbol"] == "MSFT"


def test_too_many_symbols_is_refused_and_says_why(
    ws_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    asked = [f"SYM{index}" for index in range(MAX_SUBSCRIBED_SYMBOLS + 1)]

    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": "subscribe", "symbols": asked})
        refusal = socket.receive_json()
        # The refusal does not close the socket, and does not change the
        # filter either: all-or-nothing, like a subscription unit.
        publish_quote(ws_client, MSFT_QUOTE)
        after = socket.receive_json()

    assert refusal["type"] == "error"
    assert refusal["error"]["code"] == "subscription_refused"
    assert str(MAX_SUBSCRIBED_SYMBOLS) in refusal["error"]["message"]
    assert after["quote"]["symbol"] == "MSFT"

    # Rule 8: the rule, the inputs and the timestamp.
    records = [
        record for record in caplog.records if record.__dict__.get("event") == "ws_refusal"
    ]
    assert len(records) == 1
    assert records[0].__dict__["rule"]
    assert records[0].__dict__["at"]
    assert records[0].__dict__["asked"] == len(asked)


def test_a_malformed_symbol_is_refused_whole(ws_client: TestClient) -> None:
    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": "subscribe", "symbols": ["AAPL", "aapl; drop"]})
        refusal = socket.receive_json()
        publish_quote(ws_client, MSFT_QUOTE)
        after = socket.receive_json()

    assert refusal["error"]["code"] == "subscription_refused"
    # The good symbol in the same message is not applied either: a
    # half-applied filter is the silent truncation ``stream.py`` refuses.
    assert after["quote"]["symbol"] == "MSFT"


def test_an_unknown_frame_type_is_refused_and_names_what_is_accepted(
    ws_client: TestClient,
) -> None:
    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": "viewport", "symbols": ["AAPL"]})
        refusal = socket.receive_json()

    assert refusal["type"] == "error"
    assert refusal["error"]["code"] == "invalid_request"
    assert "subscribe" in refusal["error"]["message"]


def test_malformed_json_is_refused_and_the_socket_survives(
    ws_client: TestClient,
) -> None:
    with ws_client.websocket_connect(WS) as socket:
        socket.send_text("{not json")
        refusal = socket.receive_json()
        publish_quote(ws_client, AAPL_QUOTE)
        after = socket.receive_json()

    assert refusal["error"]["code"] == "invalid_request"
    assert after["type"] == "quote"


def test_an_unexpected_field_is_refused(ws_client: TestClient) -> None:
    """``extra="forbid"`` on the request model, reaching the client as a reason."""
    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": "subscribe", "symbols": ["AAPL"], "cap": 900})
        refusal = socket.receive_json()

    assert refusal["error"]["code"] == "invalid_request"
    assert "cap" in refusal["error"]["message"]


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param("sk-live-deadbeef", id="sixteen-characters"),
        pytest.param("aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcd", id="forty-characters"),
    ],
)
def test_a_refusal_never_quotes_a_credential(
    ws_app: FastAPI, caplog: pytest.LogCaptureFixture, secret: str
) -> None:
    """Rule 6: a client message is echoed back scrubbed, then bounded.

    The socket has no exception handler, so the scrubbing ``api/app.py``
    applies to every HTTP error body has to be applied here by hand.

    **Both lengths, because the order of the two operations is the bug.**
    ``vendor_detail`` redacts by literal substitution, so a value that was cut
    before it reached the redactor matches nothing and keeps its prefix. The
    sixteen-character case fits under any per-value bound and passes either
    way; the forty-character one is the real shape of an Alpaca secret and
    only passes when redaction runs first. This test shipped with the short
    case alone, which is why the wrong order survived review.
    """
    # Lower case on purpose: a real key is mixed-case alphanumerics and the
    # symbol shape check refuses it, which is the path that echoes it back.
    # What reaches the echo is whatever a client pastes by mistake, and that
    # is what this proves is scrubbed.
    caplog.set_level(logging.WARNING)
    app = create_app(
        registry=ws_app.state.registry,
        db_engine=ws_app.state.db_engine,
        secrets=(secret,),
    )
    with TestClient(app) as client:
        with client.websocket_connect(WS) as socket:
            socket.send_json({"type": "subscribe", "symbols": [secret]})
            refusal = socket.receive_json()

    assert refusal["error"]["code"] == "subscription_refused"
    assert "<redacted>" in refusal["error"]["message"]
    # Not merely "the whole value is absent": no leading fragment of it
    # survives either, which is what a truncate-then-redact order leaves
    # behind and what an absent-whole-value assertion cannot see.
    assert secret[:16] not in refusal["error"]["message"]

    # Rule 8's record sits beside the frame and is the durable surface of the
    # two, so it is held to the same standard -- message *and* every extra.
    record = refusals(caplog)[0]
    assert secret[:16] not in record.getMessage()
    assert leaks(record, secret[:16]) == []


def test_the_health_route_still_answers(ws_client: TestClient) -> None:
    """The constraint that outranks everything else in this file."""
    assert ws_client.get("/api/health").json() == {"status": "ok"}


def test_a_refusals_log_record_carries_no_client_value(
    ws_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 6 on the ``extra``, which is the half no scrubber used to reach.

    ``_refuse`` scrubs ``message`` and spreads ``inputs`` into the record
    untouched, so a client value routed through ``inputs`` bypassed the
    scrubber entirely -- and the log record, not the frame, is the surface
    that persists. The value here is a correctly shaped paper account number,
    which ``wire.py``'s ``_ACCOUNT_NUMBER`` matches by shape: the frame
    redacted it and the record beside it did not.

    The enforced rule now: ``inputs`` carries counts and server-side
    constants only. Nothing client-derived goes in it, so there is nothing
    there to scrub.
    """
    caplog.set_level(logging.WARNING)
    account = "PA3Q8ZV71LKD"

    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": account, "symbols": None})
        refusal = socket.receive_json()

    assert refusal["error"]["code"] == "invalid_request"
    assert account not in refusal["error"]["message"]
    assert "<redacted>" in refusal["error"]["message"]

    record = refusals(caplog)[0]
    assert leaks(record, account) == []


def test_refusal_inputs_take_numbers_only() -> None:
    """The runtime backstop behind the rule chosen for ``inputs``.

    ``_refuse``'s signature types ``inputs`` as ``Mapping[str, int]``, so
    mypy refuses a client-derived string at the call site and that is the
    primary enforcement. This is the same rule at runtime, for the diff that
    arrives with a cast or an ``Any`` on it: a non-numeric value is
    *replaced* rather than dropped, because rule 8 wants to see that a field
    was there.
    """
    from corollary.api.routes.ws import _NON_NUMERIC, _numbers_only

    assert _numbers_only({"asked": 3, "bound": 256}) == {"asked": 3, "bound": 256}
    leaked: Any = {"kind": "PA3Q8ZV71LKD"}
    assert _numbers_only(leaked) == {"kind": _NON_NUMERIC}


# --------------------------------------------------------------------------
# The error vocabulary, and the fan-out that was not there
# --------------------------------------------------------------------------


def test_the_socket_states_only_its_own_error_codes() -> None:
    """``ApiErrorBody.code`` is a free ``str``; the transport's codes are not.

    Without this, a diff could publish ``code="engine_halted"`` with a halt
    announcement in the message and pass every frame-contract test in this
    file -- the union check reads the three *kinds*, and a halt riding an
    ``error`` frame is the one thing that contract exists to forbid. The
    codes are a ``Literal`` in ``ws.py`` so mypy refuses a new one at the
    call site, and this is the deliberate-change half: widening the vocabulary
    fails here.
    """
    import ast
    from pathlib import Path

    import corollary.api.routes.ws as ws_module
    from corollary.api.routes.ws import WsErrorCode

    assert set(get_args(WsErrorCode)) == {
        "invalid_request",
        "subscription_refused",
        "stream_unavailable",
    }

    # The type only binds where the frame is built, so the frame is built in
    # exactly one place. ``_state_its_failure`` constructed its own inline at
    # first, and ``ApiErrorBody.code`` is a free ``str``: mypy accepted
    # ``code="engine_halted"`` there and this test passed anyway.
    tree = ast.parse(Path(ws_module.__file__).read_text(encoding="utf-8"))
    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"WsErrorFrame", "ApiErrorBody"}
    ]
    assert len(built) == 2, "every error frame goes through ws._error"


def test_a_missing_fanout_states_its_failure_and_health_still_answers(
    ws_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """The branch that exists for a scenario no shipped path reaches.

    An app constructed without a fan-out is a programming error, and the
    constraint that outranks the feature is that it must not take
    ``GET /api/health`` down with it: the polled pages need no broker and no
    socket. So the endpoint states the condition on the socket and closes
    1011, rather than raising into the server.
    """
    caplog.set_level(logging.ERROR)

    with TestClient(ws_app) as client:
        del ws_app.state.fanout
        with client.websocket_connect(WS) as socket:
            frame = socket.receive_json()
        assert client.get("/api/health").json() == {"status": "ok"}

    assert frame["type"] == "error"
    assert frame["error"]["code"] == "stream_unavailable"
    assert "polled" in frame["error"]["message"]

    records = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "ws_fanout_missing"
    ]
    assert len(records) == 1
    assert records[0].__dict__["rule"]
    assert records[0].__dict__["at"]
