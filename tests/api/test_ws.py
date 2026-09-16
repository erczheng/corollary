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
from corollary.engine.runtime import MAX_MARKETS_VISIBLE_SYMBOLS, EngineRuntime
from corollary.engine.stream import SubscriptionPriority

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


@pytest.mark.risk
def test_nothing_under_the_api_package_arms_the_dead_mans_switch() -> None:
    """Rule 9's producers are the **vendor** sockets, and only those.

    Part 1 deliberately wired no watchdog recorder to this endpoint, and step
    8d part 2 -- which armed the switch for real, from
    ``data/providers/alpaca.py`` and ``engine/execution/alpaca.py`` -- is
    exactly when that restraint could have been lost by being helpful. A
    browser tab opening or closing says nothing about whether Alpaca is
    connected: wiring ``record_message`` here would arm the switch to the
    wrong signal, and the two failures are a halt fired by a closed laptop
    lid and a dead feed masked by a healthy browser.

    Scoped to the whole ``corollary/api`` package rather than to ``ws.py``,
    for the reason ``tests/test_hard_rules.py`` gives: a guard named after
    its subject covers the one module somebody thought of, and the next
    route is covered by nobody.

    Read off the parse tree -- ``ws.py``'s docstring has to be able to *say*
    ``record_message``, and a substring test would fail on the documentation
    that records the rule.
    """
    import ast
    from pathlib import Path

    import corollary.api as api_package

    recorders = {
        "record_message",
        "record_poll",
        "record_heartbeat",
        "record_stream_open",
        "record_stream_closed",
        "record_opening_snapshot",
    }
    package = Path(api_package.__file__).parent
    sources = sorted(package.rglob("*.py"))
    assert sources, f"no sources found under {package}"
    offenders = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in recorders:
                    offenders.append(f"{path.name}:{node.lineno} {node.func.attr}()")
            elif isinstance(node, ast.FunctionDef) and node.name in recorders:
                offenders.append(f"{path.name}:{node.lineno} def {node.name}()")
    assert offenders == [], (
        f"a browser-side signal reaches rule 9's dead-man's switch: {offenders}"
    )


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
    # Both accepted tags are named, from the one dispatch table, so the
    # refusal cannot claim a vocabulary the parser does not have.
    assert "subscribe" in refusal["error"]["message"]
    assert "markets_visible" in refusal["error"]["message"]


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
# The Markets viewport hint -- step 15, decision 18
# --------------------------------------------------------------------------
#
# The hint is the only thing a client may say about what Corollary streams
# *from Alpaca*, and it can only ever occupy the lowest tier. These tests are
# the transport half: what shape arrives, what is refused, and that a refusal
# leaves the previous hint standing. That the tier can never outrank a
# position is the engine's half and is pinned in ``tests/engine/test_runtime``.
#
# **There is no acknowledgement frame, deliberately** -- the server frame
# contract has three kinds and a fourth would be the thing the contract
# exists to forbid. So these tests synchronise on a *refusal*, which is
# ordered behind the message before it in the one reader loop.


def engine_runtime(app: FastAPI) -> EngineRuntime:
    runtime = app.state.engine_runtime
    assert isinstance(runtime, EngineRuntime)
    return runtime


def test_a_viewport_hint_reaches_the_runtime_at_the_lowest_tier(
    ws_app: FastAPI,
) -> None:
    with TestClient(ws_app) as client:
        with client.websocket_connect(WS) as socket:
            socket.send_json({"type": "markets_visible", "symbols": ["NVDA", "TSLA"]})
            # A refused message behind it, as the synchronisation point: one
            # reader loop, in order, so this frame arriving proves the hint
            # above it was handled.
            socket.send_json({"type": "markets_visible", "symbols": ["not a ticker"]})
            refusal = socket.receive_json()

            # Read while the connection is open: the hint belongs to it and
            # is dropped when it goes.
            runtime = engine_runtime(ws_app)
            held = runtime.markets_visible
            plans = runtime.plan_stream_subscriptions(
                option_units=[], equity_units=[]
            )

    assert refusal["error"]["code"] == "subscription_refused"
    # Applied whole, refused whole: the good hint stands and the bad one
    # changed nothing.
    assert held == ("NVDA", "TSLA")
    assert plans.equity.subscribed == ("NVDA", "TSLA")
    assert {unit.priority for unit in plans.equity.admitted} == {
        SubscriptionPriority.MARKETS_VISIBLE
    }
    # And it is an equity producer: nothing of it reaches the option socket.
    assert plans.option.subscribed == ()


def test_a_viewport_hint_is_not_a_delivery_filter(ws_app: FastAPI) -> None:
    """Two different questions on one socket, and naming one is not naming the other.

    ``subscribe`` says what this **connection** is sent. ``markets_visible``
    says which Markets rows are on screen, which is an input to the lowest
    subscription tier. A hint that quietly re-filtered the connection would
    make scrolling change what a position row receives.
    """
    with TestClient(ws_app) as client:
        with client.websocket_connect(WS) as socket:
            socket.send_json({"type": "subscribe", "symbols": ["MSFT"]})
            socket.send_json({"type": "markets_visible", "symbols": ["AAPL"]})
            socket.send_json({"type": "markets_visible", "symbols": ["bad"]})
            socket.receive_json()
            # Deliverable only under the *old* filter, and published first:
            # a hint that widened the filter delivers AAPL rather than MSFT.
            publish_quote(client, AAPL_QUOTE)
            publish_quote(client, MSFT_QUOTE)
            frame = socket.receive_json()
            assert engine_runtime(ws_app).markets_visible == ("AAPL",)

    assert frame["quote"]["symbol"] == "MSFT"


def test_an_oversized_viewport_hint_is_refused_and_says_why(
    ws_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A value that arrived from the client is bounded before it is allocated."""
    caplog.set_level(logging.WARNING)
    asked = [f"VIS{index}" for index in range(MAX_MARKETS_VISIBLE_SYMBOLS + 1)]

    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": "markets_visible", "symbols": asked})
        refusal = socket.receive_json()
        # The refusal does not close the socket.
        publish_quote(ws_client, MSFT_QUOTE)
        after = socket.receive_json()

    assert refusal["type"] == "error"
    assert refusal["error"]["code"] == "subscription_refused"
    assert str(MAX_MARKETS_VISIBLE_SYMBOLS) in refusal["error"]["message"]
    assert after["quote"]["symbol"] == "MSFT"

    records = refusals(caplog)
    assert len(records) == 1
    assert records[0].__dict__["rule"]
    assert records[0].__dict__["at"]
    assert records[0].__dict__["asked"] == len(asked)
    assert records[0].__dict__["bound"] == MAX_MARKETS_VISIBLE_SYMBOLS


def test_an_occ_symbol_on_the_viewport_hint_is_refused(
    ws_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """A contract on the viewport hint is a caller bug, not a chain subscription.

    Step 11 refuses a unit whose symbols straddle the two budgets for the
    same reason. Re-pointing this tier at option contracts is Phase 4 work
    (U7) with its own producer; a browser cannot ask for it.
    """
    caplog.set_level(logging.WARNING)

    with TestClient(ws_app) as client:
        with client.websocket_connect(WS) as socket:
            socket.send_json(
                {
                    "type": "markets_visible",
                    "symbols": ["AAPL", "AAPL241220C00150000"],
                }
            )
            refusal = socket.receive_json()
            # Whole or not at all: the equity ticker in the same message is
            # not applied either.
            assert engine_runtime(ws_app).markets_visible == ()

    assert refusal["error"]["code"] == "subscription_refused"
    assert refusals(caplog)[0].__dict__["rule"]
    assert refusals(caplog)[0].__dict__["at"]


def test_a_hint_the_engine_refuses_is_stated_to_the_client(
    ws_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """The gap between the two validators, which the client was never told about.

    ``_SYMBOL`` admits 32 characters because ``subscribe`` must also admit an
    OCC contract; the engine's ``_EQUITY_TICKER`` admits 16, because a
    viewport row is a ticker. Seventeen characters passes here and is refused
    there, and the engine's answer used to be indistinguishable from *"the
    set did not differ"* -- so this frame was never sent, the transport
    logged the hint at INFO as applied, and only the engine's own record
    said otherwise. A subscription that is silently ignored looks exactly
    like a feed that has nothing to say.
    """
    caplog.set_level(logging.INFO)
    gap_band = "ABCDEFGHIJKLMNOPQ"
    assert len(gap_band) == 17

    with TestClient(ws_app) as client:
        with client.websocket_connect(WS) as socket:
            socket.send_json({"type": "markets_visible", "symbols": ["NVDA"]})
            socket.send_json(
                {"type": "markets_visible", "symbols": ["NVDA", gap_band]}
            )
            # A message the *transport* refuses, behind it, as the
            # synchronisation point: one reader loop, in order. Without it
            # this test would block forever on the frame the bug never
            # sends, and a hang is a worse failure than an assertion.
            socket.send_json({"type": "markets_visible", "symbols": ["not a ticker"]})
            refusal = socket.receive_json()
            # Refused whole, and the hint the engine did accept still stands.
            assert engine_runtime(ws_app).markets_visible == ("NVDA",)

    assert refusal["type"] == "error"
    assert refusal["error"]["code"] == "subscription_refused"
    # The engine's rule, not the transport's: this frame is the answer to
    # the message before the synchronisation point.
    assert "equity ticker" in refusal["error"]["message"]
    # Two rule 8 records: the engine's refusal and the transport's.
    assert len(refusals(caplog)) == 2
    applied = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "ws_markets_visible"
    ]
    assert [record.__dict__["status"] for record in applied] == ["applied"]


def test_a_refused_hint_does_not_echo_the_clients_string(
    ws_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 6 on the frame the engine's refusal produces.

    The reason reaches the client, and the reason is a server-side constant.
    The client's own string is not quoted back into the log record's fields
    -- the shape filter admits the paper account-number pattern that
    ``vendor_detail`` exists to redact.
    """
    caplog.set_level(logging.INFO)
    looks_like_a_key = "PA3XYZ12AB9ZQQQQQQQQ"

    with TestClient(ws_app) as client:
        with client.websocket_connect(WS) as socket:
            socket.send_json(
                {"type": "markets_visible", "symbols": [looks_like_a_key]}
            )
            socket.send_json({"type": "markets_visible", "symbols": ["not a ticker"]})
            refusal = socket.receive_json()

    assert refusal["error"]["code"] == "subscription_refused"
    assert "equity ticker" in refusal["error"]["message"]
    written = [
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    ]
    assert not any(looks_like_a_key in line for line in written)


def test_a_viewport_hint_needs_its_symbols_spelled_out(
    ws_client: TestClient,
) -> None:
    """No default, and no extra fields. An omitted list is a silent choice."""
    with ws_client.websocket_connect(WS) as socket:
        socket.send_json({"type": "markets_visible"})
        missing = socket.receive_json()
        socket.send_json(
            {"type": "markets_visible", "symbols": ["AAPL"], "priority": 1}
        )
        extra = socket.receive_json()

    assert missing["error"]["code"] == "invalid_request"
    assert "symbols" in missing["error"]["message"]
    # There is no priority argument, because there is no other priority this
    # could ever take.
    assert extra["error"]["code"] == "invalid_request"
    assert "priority" in extra["error"]["message"]


def test_an_empty_viewport_hint_gives_the_slots_back(ws_app: FastAPI) -> None:
    """Scrolling away, or leaving the Markets page, is a hint of nothing."""
    with TestClient(ws_app) as client:
        with client.websocket_connect(WS) as socket:
            socket.send_json({"type": "markets_visible", "symbols": ["NVDA"]})
            socket.send_json({"type": "markets_visible", "symbols": []})
            socket.send_json({"type": "markets_visible", "symbols": ["bad"]})
            socket.receive_json()
            assert engine_runtime(ws_app).markets_visible == ()


def test_a_hint_is_dropped_with_the_connection_that_sent_it(
    ws_app: FastAPI,
) -> None:
    """A closed tab is nobody looking at anything.

    A hint outliving its client would hold the lowest tier's slots on rows
    that are on no screen -- and it would never be corrected, because the
    client only sends on a *change*.
    """
    with TestClient(ws_app) as client:
        with client.websocket_connect(WS) as socket:
            socket.send_json({"type": "markets_visible", "symbols": ["NVDA"]})
            socket.send_json({"type": "markets_visible", "symbols": ["bad"]})
            socket.receive_json()
            assert engine_runtime(ws_app).markets_visible == ("NVDA",)

        assert engine_runtime(ws_app).markets_visible == ()


def test_a_second_tabs_hint_survives_the_first_tab_closing(
    ws_app: FastAPI,
) -> None:
    """The hint belongs to whoever spoke last, and only that one drops it.

    Two tabs take turns rather than merging, which is harmless at this tier
    and nowhere else: it is last in the priority order, so it can only spend
    slots nothing above it wanted, and every Markets row is polled anyway.
    What would *not* be harmless is a closing tab silently clearing a hint
    the surviving one had already replaced.
    """
    with TestClient(ws_app) as client:
        with client.websocket_connect(WS) as second:
            with client.websocket_connect(WS) as first:
                # Each hint is synchronised on its own connection's refusal,
                # so the two are ordered against each other rather than
                # racing two independent reader loops.
                first.send_json({"type": "markets_visible", "symbols": ["NVDA"]})
                first.send_json({"type": "markets_visible", "symbols": ["bad"]})
                first.receive_json()

                second.send_json({"type": "markets_visible", "symbols": ["TSLA"]})
                second.send_json({"type": "markets_visible", "symbols": ["bad"]})
                second.receive_json()
                assert engine_runtime(ws_app).markets_visible == ("TSLA",)

            # The first tab closed, and its hint was already superseded.
            assert engine_runtime(ws_app).markets_visible == ("TSLA",)


def test_a_viewport_hint_with_no_engine_states_its_failure(
    ws_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """An app with no runtime answers on the socket rather than raising.

    The same standing the missing fan-out has, and for the same reason: the
    polled pages need no engine, and ``GET /api/health`` must keep answering.
    """
    caplog.set_level(logging.WARNING)

    with TestClient(ws_app) as client:
        ws_app.state.engine_runtime = None
        with client.websocket_connect(WS) as socket:
            socket.send_json({"type": "markets_visible", "symbols": ["NVDA"]})
            refusal = socket.receive_json()
        assert client.get("/api/health").json() == {"status": "ok"}

    assert refusal["error"]["code"] == "stream_unavailable"


def test_a_refused_viewport_hint_never_quotes_a_credential(
    ws_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 6 on the second client message, not only on the first.

    The hint echoes the entries it refused, exactly as ``subscribe`` does,
    so it goes through the same ``_scrub`` and inherits the same ordering --
    redaction before truncation. Forty characters, the real shape of an
    Alpaca secret, because that is the length a truncate-first order would
    leak the prefix of.
    """
    caplog.set_level(logging.WARNING)
    secret = "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcd"
    app = create_app(
        registry=ws_app.state.registry,
        db_engine=ws_app.state.db_engine,
        secrets=(secret,),
    )

    with TestClient(app) as client:
        with client.websocket_connect(WS) as socket:
            socket.send_json({"type": "markets_visible", "symbols": [secret]})
            refusal = socket.receive_json()

    assert refusal["error"]["code"] == "subscription_refused"
    assert "<redacted>" in refusal["error"]["message"]
    assert secret[:16] not in refusal["error"]["message"]

    record = refusals(caplog)[0]
    assert secret[:16] not in record.getMessage()
    assert leaks(record, secret[:16]) == []


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
