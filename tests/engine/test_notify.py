"""Notification delivery: the bell's table, the Discord webhook, and the fan-out.

Phase 3 decision 14. Rule 9 says a dead-man's-switch halt *fires a critical
notification*; until this module the notification was a log line. What is
pinned here is the part that can hide a real failure:

* ``emit`` is synchronous and **returns without awaiting Discord** -- a
  webhook is an HTTP call that can hang, and the halt path must not hang with
  it;
* a failed delivery is **recorded** in ``notification_delivery``, never
  silent -- an alert that silently did not arrive is the rule 9 failure the
  routing confirm exists to prevent;
* the webhook URL, which embeds its token, reaches **no log line, no stored
  detail and no error text** (rule 6);
* routing is read **at emit time**, and one sink failing never stops another
  or the halt.

All marked ``risk``: they guard the dead-man's switch's only voice. No test
here opens a socket -- every Discord request goes to an ``httpx.MockTransport``.
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from corollary.db.models import (
    ENGINE_STATE_ID,
    Base,
    EngineState,
    NotificationDelivery,
    NotificationRecord,
    NotificationRoute,
)
from corollary.db.seed import seed
from corollary.db.session import create_db_engine, sqlite_url
from corollary.engine.notify import (
    DbNotifier,
    DiscordNotifier,
    FanoutNotifier,
    discord_payload,
)
from corollary.engine.runtime import (
    DISCORD_WEBHOOK_ENV,
    HALT_EVENT,
    EngineRuntime,
    HaltDecision,
    HaltRule,
    Notification,
)

pytestmark = pytest.mark.risk

#: A webhook URL whose token is distinctive enough that finding it anywhere is
#: unambiguous. Fake: nothing here resolves, and the transport is mocked.
TOKEN = "TOKEN-NEVER-LOG-7f3a9c1e2b4d"
WEBHOOK_URL = f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}"

AT = datetime(2026, 9, 24, 14, 30, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(sqlite_url(tmp_path / "notify.db"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
    yield engine
    engine.dispose()


def _notification(
    *,
    channels: tuple[str, ...] = ("bell", "discord"),
    account: str | None = None,
    title: str = "Engine halted",
    body: str = "the trading stream closed",
) -> Notification:
    return Notification(
        event=HALT_EVENT,
        severity="critical",
        title=title,
        body=body,
        at=AT,
        correlation_id="corr-1",
        channels=channels,
        account=account,
    )


def _deliveries(engine: Engine, channel: str | None = None) -> list[NotificationDelivery]:
    with Session(engine) as session:
        query = select(NotificationDelivery).order_by(NotificationDelivery.id)
        if channel is not None:
            query = query.where(NotificationDelivery.channel == channel)
        rows = list(session.scalars(query))
        session.expunge_all()
        return rows


def _records(engine: Engine) -> list[NotificationRecord]:
    with Session(engine) as session:
        rows = list(session.scalars(select(NotificationRecord)))
        session.expunge_all()
        return rows


class Recorder:
    """An ``httpx.MockTransport`` handler serving a script of responses."""

    def __init__(self, *responses: httpx.Response | BaseException) -> None:
        self.script = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.script.pop(0) if self.script else httpx.Response(204)
        if isinstance(item, BaseException):
            raise item
        return item

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


class Hung:
    """A webhook that accepts the connection and never answers."""

    def __init__(self) -> None:
        self.requests = 0
        self.release = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        await self.release.wait()
        return httpx.Response(204)


class Sleeps:
    """A sleep that records what it was asked for and returns at once."""

    def __init__(self) -> None:
        self.asked: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)


def _discord(
    engine: Engine,
    transport: httpx.AsyncBaseTransport,
    *,
    webhook_url: str | None = WEBHOOK_URL,
    **kwargs: Any,
) -> DiscordNotifier:
    return DiscordNotifier(
        webhook_url=webhook_url,
        session_factory=lambda: Session(engine),
        transport=transport,
        **kwargs,
    )


def _halt_decision() -> HaltDecision:
    return HaltDecision(
        rule=HaltRule.STREAM_CLOSED,
        reason="the trading stream closed (code 1006)",
        inputs={"socket": "trading", "code": 1006},
        at=AT,
    )


def _runtime(
    engine: Engine,
    notifier: Any,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> EngineRuntime:
    return EngineRuntime(
        session_factory=session_factory or (lambda: Session(engine)),
        now=lambda: AT,
        notifier=notifier,
        env={DISCORD_WEBHOOK_ENV: WEBHOOK_URL},
        correlation_ids=lambda: "halt-corr",
    )


def _set_route(engine: Engine, channel: str, enabled: bool) -> None:
    with Session(engine) as session:
        session.execute(
            update(NotificationRoute)
            .where(
                NotificationRoute.event == HALT_EVENT,
                NotificationRoute.channel == channel,
            )
            .values(enabled=enabled)
        )
        session.commit()


def _halted(engine: Engine) -> bool:
    with Session(engine) as session:
        state = session.get(EngineState, ENGINE_STATE_ID)
        assert state is not None
        return state.halted


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record, message, extras and traceback, as one string."""
    formatter = logging.Formatter()
    parts = []
    for record in caplog.records:
        parts.append(formatter.format(record))
        parts.append(repr(record.__dict__))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# emit is synchronous and never waits on Discord
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_returns_without_awaiting_discord(db_engine: Engine) -> None:
    hung = Hung()
    discord = _discord(db_engine, httpx.MockTransport(hung), timeout_seconds=30)
    discord.start()
    try:
        started = time.monotonic()
        discord.emit(_notification())
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        # Nothing has been sent yet: emit enqueued and returned. The delivery
        # task only runs once this coroutine yields.
        assert hung.requests == 0
        await asyncio.sleep(0.05)
        assert hung.requests == 1
    finally:
        hung.release.set()
        await discord.aclose()


@pytest.mark.asyncio
async def test_a_hung_webhook_does_not_delay_a_halt(db_engine: Engine) -> None:
    hung = Hung()
    discord = _discord(db_engine, httpx.MockTransport(hung), timeout_seconds=0.2)
    discord.start()
    runtime = _runtime(
        db_engine,
        FanoutNotifier(
            [DbNotifier(session_factory=lambda: Session(db_engine)), discord]
        ),
    )
    _set_route(db_engine, "bell", True)
    try:
        started = time.monotonic()
        runtime.halt(_halt_decision())
        assert time.monotonic() - started < 1.0
        assert _halted(db_engine)
        assert len(_records(db_engine)) == 1

        # The hang is then bounded by the timeout and recorded as a failure.
        await asyncio.sleep(0.6)
        failed = _deliveries(db_engine, "discord")
        assert [row.status for row in failed] == ["failed"]
        assert "timed out" in failed[0].detail
    finally:
        hung.release.set()
        await discord.aclose()


@pytest.mark.asyncio
async def test_emit_from_a_worker_thread_is_delivered(db_engine: Engine) -> None:
    """A sync route runs in a threadpool; its emit must still reach the loop."""
    recorder = Recorder(httpx.Response(204))
    discord = _discord(db_engine, recorder.transport())
    discord.start()
    DbNotifier(session_factory=lambda: Session(db_engine)).emit(_notification())
    try:
        thread = threading.Thread(target=discord.emit, args=(_notification(),))
        thread.start()
        thread.join(timeout=2)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if recorder.requests:
                break
        await discord.join()
        assert len(recorder.requests) == 1
    finally:
        await discord.aclose()


# --------------------------------------------------------------------------
# Every outcome is recorded; failures are never silent
# --------------------------------------------------------------------------


async def _deliver_one(
    engine: Engine,
    transport: httpx.AsyncBaseTransport,
    notification: Notification | None = None,
    **kwargs: Any,
) -> Notification:
    """Write the notification row, deliver it to Discord, wait for the outcome."""
    notification = notification or _notification()
    DbNotifier(session_factory=lambda: Session(engine)).emit(notification)
    discord = _discord(engine, transport, **kwargs)
    discord.start()
    try:
        discord.emit(notification)
        await discord.join()
    finally:
        await discord.aclose()
    return notification


@pytest.mark.asyncio
async def test_a_delivered_post_is_recorded(db_engine: Engine) -> None:
    recorder = Recorder(httpx.Response(204))
    sent = await _deliver_one(db_engine, recorder.transport())
    rows = _deliveries(db_engine, "discord")
    assert [(row.notification_id, row.status) for row in rows] == [(sent.id, "delivered")]
    assert rows[0].attempted_at.tzinfo is not None


@pytest.mark.asyncio
async def test_a_5xx_is_retried_once_and_both_failures_are_recorded(
    db_engine: Engine,
) -> None:
    recorder = Recorder(httpx.Response(502), httpx.Response(503))
    sleeps = Sleeps()
    await _deliver_one(db_engine, recorder.transport(), sleep=sleeps)
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["failed", "failed"]
    assert "502" in rows[0].detail and "retry" in rows[0].detail
    assert "503" in rows[1].detail
    assert len(recorder.requests) == 2
    assert len(sleeps.asked) == 1


@pytest.mark.asyncio
async def test_a_5xx_then_success_is_delivered(db_engine: Engine) -> None:
    recorder = Recorder(httpx.Response(500), httpx.Response(204))
    await _deliver_one(db_engine, recorder.transport(), sleep=Sleeps())
    assert [row.status for row in _deliveries(db_engine, "discord")] == [
        "failed",
        "delivered",
    ]


@pytest.mark.asyncio
async def test_a_4xx_is_not_retried_and_is_recorded(db_engine: Engine) -> None:
    recorder = Recorder(httpx.Response(404, json={"message": "Unknown Webhook"}))
    sleeps = Sleeps()
    await _deliver_one(db_engine, recorder.transport(), sleep=sleeps)
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["failed"]
    assert "404" in rows[0].detail
    assert len(recorder.requests) == 1
    assert sleeps.asked == []


@pytest.mark.asyncio
async def test_a_429_is_retried_once_honouring_retry_after(db_engine: Engine) -> None:
    recorder = Recorder(
        httpx.Response(
            429,
            json={"message": "You are being rate limited.", "retry_after": 2.5, "global": False},
        ),
        httpx.Response(204),
    )
    sleeps = Sleeps()
    await _deliver_one(db_engine, recorder.transport(), sleep=sleeps)
    assert sleeps.asked == [2.5]
    assert len(recorder.requests) == 2
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["failed", "delivered"]
    assert "429" in rows[0].detail and "2.5" in rows[0].detail


@pytest.mark.asyncio
async def test_a_second_429_is_not_retried_again(db_engine: Engine) -> None:
    limited = {"message": "rate limited", "retry_after": 0.1}
    recorder = Recorder(
        httpx.Response(429, json=limited), httpx.Response(429, json=limited)
    )
    sleeps = Sleeps()
    await _deliver_one(db_engine, recorder.transport(), sleep=sleeps)
    assert len(recorder.requests) == 2
    assert len(sleeps.asked) == 1
    assert [row.status for row in _deliveries(db_engine, "discord")] == [
        "failed",
        "failed",
    ]


@pytest.mark.asyncio
async def test_a_retry_after_past_the_cap_is_not_waited_out(db_engine: Engine) -> None:
    recorder = Recorder(httpx.Response(429, json={"retry_after": 3600}))
    sleeps = Sleeps()
    await _deliver_one(
        db_engine, recorder.transport(), sleep=sleeps, max_retry_after_seconds=60
    )
    assert sleeps.asked == []
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["failed"]
    assert "3600" in rows[0].detail


@pytest.mark.asyncio
async def test_a_transport_error_is_recorded(db_engine: Engine) -> None:
    recorder = Recorder(httpx.ConnectError("connection refused"))
    await _deliver_one(db_engine, recorder.transport(), sleep=Sleeps())
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["failed"]
    assert "ConnectError" in rows[0].detail


@pytest.mark.asyncio
async def test_no_webhook_configured_is_recorded_as_dropped(db_engine: Engine) -> None:
    recorder = Recorder()
    await _deliver_one(db_engine, recorder.transport(), webhook_url="  ")
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["dropped"]
    assert DISCORD_WEBHOOK_ENV in rows[0].detail
    assert recorder.requests == []


#: Dummy values. The first is one ``urllib.parse.urlsplit`` raises on.
MALFORMED_TOKEN = "dummy-token-not-real"
MALFORMED_WEBHOOK_URL = f"https://[::1/api/webhooks/1/{MALFORMED_TOKEN}"
PLAIN_HTTP_TOKEN = "dummy-http-token-not-real"
PLAIN_HTTP_WEBHOOK_URL = f"http://discord.com/api/webhooks/1/{PLAIN_HTTP_TOKEN}"


@pytest.mark.asyncio
async def test_a_malformed_webhook_is_an_unavailable_sink_not_a_crash(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = Recorder()
    with caplog.at_level(logging.DEBUG):
        await _deliver_one(
            db_engine, recorder.transport(), webhook_url=MALFORMED_WEBHOOK_URL
        )
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["dropped"]
    assert rows[0].detail == f"{DISCORD_WEBHOOK_ENV} is set but malformed"
    assert recorder.requests == []
    logged = _everything_logged(caplog)
    assert MALFORMED_TOKEN not in logged
    assert "[::1" not in logged
    misconfigured = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "notification_discord_misconfigured"
    ]
    assert len(misconfigured) == 1
    assert misconfigured[0].levelno == logging.ERROR
    assert DISCORD_WEBHOOK_ENV in misconfigured[0].getMessage()


@pytest.mark.asyncio
async def test_a_non_https_webhook_is_an_unavailable_sink(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Plain ``http`` would send the token in the clear; nothing posts."""
    recorder = Recorder()
    with caplog.at_level(logging.DEBUG):
        await _deliver_one(
            db_engine, recorder.transport(), webhook_url=PLAIN_HTTP_WEBHOOK_URL
        )
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["dropped"]
    assert rows[0].detail == f"{DISCORD_WEBHOOK_ENV} is set but is not an https URL"
    assert recorder.requests == []
    assert PLAIN_HTTP_TOKEN not in _everything_logged(caplog)


@pytest.mark.parametrize("url", ["https:///api/webhooks/1/x", "https://host:99999/x"])
@pytest.mark.asyncio
async def test_a_webhook_without_a_usable_host_or_port_is_malformed(
    db_engine: Engine, url: str
) -> None:
    recorder = Recorder()
    await _deliver_one(db_engine, recorder.transport(), webhook_url=url)
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["dropped"]
    assert rows[0].detail == f"{DISCORD_WEBHOOK_ENV} is set but malformed"
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_a_disabled_sink_records_why(db_engine: Engine) -> None:
    recorder = Recorder()
    await _deliver_one(
        db_engine, recorder.transport(), disabled_reason="dev_app sends no Discord"
    )
    rows = _deliveries(db_engine, "discord")
    assert [(row.status, row.detail) for row in rows] == [
        ("dropped", "dev_app sends no Discord")
    ]
    assert recorder.requests == []


def test_an_emit_before_start_is_recorded_as_dropped(db_engine: Engine) -> None:
    notification = _notification()
    DbNotifier(session_factory=lambda: Session(db_engine)).emit(notification)
    discord = _discord(db_engine, Recorder().transport())
    discord.emit(notification)
    rows = _deliveries(db_engine, "discord")
    assert [row.status for row in rows] == ["dropped"]


@pytest.mark.asyncio
async def test_shutdown_records_what_it_never_sent(db_engine: Engine) -> None:
    hung = Hung()
    notification = _notification()
    DbNotifier(session_factory=lambda: Session(db_engine)).emit(notification)
    discord = _discord(
        db_engine,
        httpx.MockTransport(hung),
        timeout_seconds=30,
        shutdown_grace_seconds=0.05,
    )
    discord.start()
    discord.emit(notification)
    discord.emit(notification)
    await asyncio.sleep(0.02)
    await discord.aclose()
    hung.release.set()
    rows = _deliveries(db_engine, "discord")
    # The first was mid-POST when shutdown cancelled it: the request may have
    # gone out, so it was *attempted* -- ``failed``, not ``dropped``. The
    # second never left the queue, so no attempt was made -- ``dropped``.
    assert [(row.status, row.detail) for row in rows] == [
        ("failed", "the process shut down during delivery"),
        ("dropped", "the process shut down before delivery"),
    ]


@pytest.mark.asyncio
async def test_a_client_that_cannot_be_built_drops_every_alert_at_once(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ``SSL_CERT_FILE`` makes ``httpx.AsyncClient()`` raise.

    ``start()`` then fails, and every later alert routed to Discord must be a
    ``dropped`` row *at emit time* -- not parked in a queue nothing reads,
    surfacing only at shutdown.
    """

    def no_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        raise FileNotFoundError("SSL_CERT_FILE points nowhere")

    notification = _notification()
    DbNotifier(session_factory=lambda: Session(db_engine)).emit(notification)
    discord = _discord(db_engine, Recorder().transport())
    monkeypatch.setattr(httpx, "AsyncClient", no_client)
    with pytest.raises(FileNotFoundError):
        discord.start()
    try:
        discord.emit(notification)
        rows = _deliveries(db_engine, "discord")
        assert [row.status for row in rows] == ["dropped"]
        assert "not running" in rows[0].detail
    finally:
        await discord.aclose()
    assert len(_deliveries(db_engine, "discord")) == 1


@pytest.mark.asyncio
async def test_a_dead_delivery_task_drops_every_alert_at_once(db_engine: Engine) -> None:
    """A ``_run`` task that has ended for any reason delivers nothing more."""
    recorder = Recorder()
    notification = _notification()
    DbNotifier(session_factory=lambda: Session(db_engine)).emit(notification)
    discord = _discord(db_engine, recorder.transport())
    discord.start()
    try:
        # Reaching in is the only way to stand for "the task died"; the class
        # offers no public way to kill it, which is the point.
        task = discord._task
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        discord.emit(notification)
        rows = _deliveries(db_engine, "discord")
        assert [row.status for row in rows] == ["dropped"]
        assert "not running" in rows[0].detail
    finally:
        await discord.aclose()
    assert recorder.requests == []
    assert len(_deliveries(db_engine, "discord")) == 1


# --------------------------------------------------------------------------
# Rule 6: the webhook URL is a secret
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        # httpx puts the request URL into its own exception text.
        (httpx.ConnectError(f"could not connect to {WEBHOOK_URL}"),),
        (httpx.ReadTimeout(f"timed out reading {WEBHOOK_URL}"),),
        # A body that echoes the URL back, on each branch that reads a body.
        (
            httpx.Response(400, json={"message": f"bad request for {WEBHOOK_URL}"}),
        ),
        (
            httpx.Response(500, text=f"upstream failed: {WEBHOOK_URL}"),
            httpx.Response(429, json={"message": WEBHOOK_URL, "retry_after": 0.1}),
        ),
    ],
    ids=["connect-error", "read-timeout", "400-echo", "500-then-429-echo"],
)
async def test_the_webhook_url_reaches_no_log_row_or_error(
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    responses: tuple[httpx.Response | BaseException, ...],
) -> None:
    recorder = Recorder(*responses)
    with caplog.at_level(logging.DEBUG):
        await _deliver_one(db_engine, recorder.transport(), sleep=Sleeps())
    rows = _deliveries(db_engine, "discord")
    assert rows, "the failure must be recorded"
    assert all(row.status == "failed" for row in rows)
    for row in rows:
        assert TOKEN not in row.detail
        assert "webhooks/" not in row.detail
    logged = _everything_logged(caplog)
    assert logged, "the failure must be logged"
    assert TOKEN not in logged


@pytest.mark.asyncio
async def test_the_webhook_url_is_absent_from_a_whole_halt(
    db_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The runtime's own lines, the fan-out's, the sinks' -- none carry it."""
    recorder = Recorder(httpx.Response(500, text=WEBHOOK_URL), httpx.Response(204))
    discord = _discord(db_engine, recorder.transport(), sleep=Sleeps())
    discord.start()
    runtime = _runtime(
        db_engine,
        FanoutNotifier(
            [DbNotifier(session_factory=lambda: Session(db_engine)), discord]
        ),
    )
    try:
        with caplog.at_level(logging.DEBUG):
            runtime.halt(_halt_decision())
            await discord.join()
    finally:
        await discord.aclose()
    assert TOKEN not in _everything_logged(caplog)
    for row in _deliveries(db_engine):
        assert TOKEN not in row.detail
    for record in _records(db_engine):
        assert TOKEN not in record.title + record.body


# --------------------------------------------------------------------------
# Routing, the halt alert, and sink isolation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_halt_alert_reaches_both_sinks(db_engine: Engine) -> None:
    recorder = Recorder(httpx.Response(204))
    discord = _discord(db_engine, recorder.transport())
    discord.start()
    runtime = _runtime(
        db_engine,
        FanoutNotifier(
            [DbNotifier(session_factory=lambda: Session(db_engine)), discord]
        ),
    )
    try:
        runtime.halt(_halt_decision())
        await discord.join()
    finally:
        await discord.aclose()

    [record] = _records(db_engine)
    assert record.event == HALT_EVENT
    assert record.severity == "critical"
    assert record.account is None
    assert record.title == "Engine halted"
    assert record.body == "the trading stream closed (code 1006)"
    assert record.correlation_id == "halt-corr"
    assert record.read_at is None and record.dismissed_at is None

    bell = _deliveries(db_engine, "bell")
    assert [(row.notification_id, row.status) for row in bell] == [
        (record.id, "delivered")
    ]
    discord_rows = _deliveries(db_engine, "discord")
    assert [(row.notification_id, row.status) for row in discord_rows] == [
        (record.id, "delivered")
    ]

    [request] = recorder.requests
    assert request.method == "POST"
    payload = json.loads(request.content)
    [embed] = payload["embeds"]
    assert embed["title"] == "Engine halted"
    assert embed["description"] == "the trading stream closed (code 1006)"
    assert "halt-corr" in json.dumps(embed)
    # Nobody gets pinged by a halt reason that happens to contain "@everyone".
    assert payload["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_routing_is_read_at_emit_time(db_engine: Engine) -> None:
    recorder = Recorder()
    discord = _discord(db_engine, recorder.transport())
    discord.start()
    runtime = _runtime(
        db_engine,
        FanoutNotifier(
            [DbNotifier(session_factory=lambda: Session(db_engine)), discord]
        ),
    )
    try:
        _set_route(db_engine, "discord", False)
        runtime.halt(_halt_decision())
        await discord.join()
        # Switched back on afterwards: the earlier halt is not re-sent, and
        # nothing about what was already delivered changes.
        _set_route(db_engine, "discord", True)
        await asyncio.sleep(0.05)
    finally:
        await discord.aclose()
    assert recorder.requests == []
    assert _deliveries(db_engine, "discord") == []
    assert [row.status for row in _deliveries(db_engine, "bell")] == ["delivered"]


def test_a_bell_route_switched_off_writes_no_bell_delivery(db_engine: Engine) -> None:
    DbNotifier(session_factory=lambda: Session(db_engine)).emit(
        _notification(channels=("discord",))
    )
    assert len(_records(db_engine)) == 1
    assert _deliveries(db_engine, "bell") == []


class BrokenDb:
    """A session factory whose database refuses every statement."""

    def __call__(self) -> Session:
        raise OperationalError("INSERT", {}, Exception("database is locked"))


class Raises:
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, notification: Notification) -> None:
        self.calls += 1
        raise RuntimeError("a sink with a bug")


@pytest.mark.asyncio
async def test_a_db_sink_failure_does_not_stop_discord_or_the_halt(
    db_engine: Engine,
) -> None:
    recorder = Recorder(httpx.Response(204))
    discord = _discord(db_engine, recorder.transport())
    discord.start()
    runtime = _runtime(
        db_engine,
        FanoutNotifier([DbNotifier(session_factory=BrokenDb()), discord]),
    )
    try:
        runtime.halt(_halt_decision())
        await discord.join()
    finally:
        await discord.aclose()
    assert _halted(db_engine)
    assert len(recorder.requests) == 1
    # The bell write failed, so there is no notification row -- and Discord's
    # outcome still landed. ``notification_id`` is a soft reference, not a
    # foreign key, precisely so this row can exist: the orphan is the record
    # that the bell write failed while Discord delivered.
    assert _records(db_engine) == []
    [row] = _deliveries(db_engine, "discord")
    assert row.status == "delivered"
    assert row.notification_id


def test_a_raising_sink_does_not_stop_the_next_one(db_engine: Engine) -> None:
    broken = Raises()
    FanoutNotifier(
        [broken, DbNotifier(session_factory=lambda: Session(db_engine))]
    ).emit(_notification())
    assert broken.calls == 1
    assert len(_records(db_engine)) == 1


def test_a_raising_notifier_does_not_escape_halt(db_engine: Engine) -> None:
    """Even a bare notifier, with no fan-out around it, cannot break ``halt``."""
    broken = Raises()
    runtime = _runtime(db_engine, broken)
    runtime.halt(_halt_decision())
    assert broken.calls == 1
    assert _halted(db_engine)


def test_the_embed_is_bounded_to_discords_limits() -> None:
    payload = discord_payload(_notification(title="t" * 400, body="b" * 5000))
    [embed] = payload["embeds"]
    assert len(embed["title"]) <= 256
    assert len(embed["description"]) <= 4096


def test_an_account_scoped_notification_names_its_book() -> None:
    payload = discord_payload(_notification(account="paper"))
    assert "paper" in json.dumps(payload["embeds"][0]["fields"])
