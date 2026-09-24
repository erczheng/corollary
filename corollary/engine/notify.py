"""Notification delivery: the bell's table, the Discord webhook, and the fan-out.

Phase 3 decision 14. Rule 9 says a dead-man's-switch halt *fires a critical
notification*; :mod:`corollary.engine.runtime` decides that one fires, which
channels a human left on (``_channels_for``, read **at emit time**), and hands
a :class:`~corollary.engine.runtime.Notification` to a
:class:`~corollary.engine.runtime.Notifier`. This module is the delivery half:

* :class:`DbNotifier` writes the ``notification`` row, and -- when the bell is
  routed -- the ``notification_delivery`` row that puts it in the bell.
* :class:`DiscordNotifier` posts a rich embed (PRD section 10) when the
  ``discord`` channel is routed.
* :class:`FanoutNotifier` holds the list and isolates each sink from the others.

Three properties, each pinned by ``tests/engine/test_notify.py`` under the
``risk`` marker:

**The halt is never delayed or prevented by delivery.** ``emit`` is
synchronous so the halt path stays testable, and a webhook is an HTTP call
that can hang -- so :meth:`DiscordNotifier.emit` only *enqueues*, and an
asyncio task delivers with a timeout. Nothing here raises into its caller:
every sink catches its own failures, and the fan-out catches whatever a sink
failed to. (The database sink's write *is* synchronous: it is one SQLite
insert, the same cost ``EngineRuntime._persist`` has already paid on the same
path, and the halt it describes is committed before it runs.)

**A failed delivery is recorded, never silent.** Every attempt is a
``notification_delivery`` row -- ``delivered``, ``failed``, or ``dropped``
when no attempt could be made at all -- because an alert that silently did not
arrive is the rule 9 failure the routing confirm exists to prevent.

**The webhook URL is a secret (rule 6).** It embeds its token. It is held by
:class:`DiscordNotifier` and nothing else, and it reaches no log line, no
stored ``detail`` and no route response. httpx puts the request URL into its
own exception text (and ``raise_for_status`` into its message), so this module
never logs ``str(exc)`` from a request: it records a status code or an
exception class name, and anything read from a response body goes through
:func:`corollary.wire.vendor_detail` with the URL and its token as secrets.

Discord is not a broker and not a market-data vendor; nothing here imports
``alpaca`` or reaches an order path.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from typing import Any, Final

import httpx
from sqlalchemy.orm import Session

from corollary.db.models import NotificationDelivery, NotificationRecord
from corollary.engine.runtime import DISCORD_WEBHOOK_ENV, Notification, Notifier
from corollary.wire import ERROR_BODY_MAX, url_secrets, vendor_detail

__all__ = [
    "BELL_CHANNEL",
    "DISCORD_CHANNEL",
    "DISCORD_MAX_RETRY_AFTER_SECONDS",
    "DISCORD_QUEUE_SIZE",
    "DISCORD_RETRY_DELAY_SECONDS",
    "DISCORD_SHUTDOWN_GRACE_SECONDS",
    "DISCORD_TIMEOUT_SECONDS",
    "DbNotifier",
    "DiscordNotifier",
    "FanoutNotifier",
    "discord_payload",
]

logger = logging.getLogger(__name__)

BELL_CHANNEL: Final = "bell"
DISCORD_CHANNEL: Final = "discord"

#: The whole budget for one POST, connect to last byte. A webhook that has not
#: answered in ten seconds is not going to, and the queue behind it waits.
DISCORD_TIMEOUT_SECONDS: Final[float] = 10.0

#: How long to wait before the one retry a 5xx earns. A 429 waits for what
#: Discord's ``retry_after`` says instead.
DISCORD_RETRY_DELAY_SECONDS: Final[float] = 2.0

#: The longest ``retry_after`` this sink will wait out. Past it the attempt is
#: recorded as failed rather than held: a queue parked for an hour behind one
#: rate limit would delay every alert behind it by the same hour.
DISCORD_MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0

#: Bounded so a flood cannot grow memory without limit. An alert that does
#: not fit is recorded ``dropped``, not lost.
DISCORD_QUEUE_SIZE: Final = 100

#: How long shutdown waits for the queue to drain before recording what is
#: left as ``dropped``.
DISCORD_SHUTDOWN_GRACE_SECONDS: Final[float] = 5.0

#: Discord's embed limits, from its API documentation. Exceeding either is a
#: 400 and the whole alert is lost, so both are enforced here.
_EMBED_TITLE_MAX: Final = 256
_EMBED_DESCRIPTION_MAX: Final = 4096
_EMBED_FIELD_VALUE_MAX: Final = 1024

#: Embed colour per severity, from ``DESIGN.md``'s light tokens: ``error`` for
#: critical, ``caution`` for warning, ``primary`` for info. A stop firing is a
#: warning, never an error, and the colours keep that split.
_SEVERITY_COLOUR: Final[dict[str, int]] = {
    "critical": 0xBA1A1A,
    "warning": 0x855146,
    "info": 0x355255,
}

#: Where ``notification.title`` is bounded -- the column's width.
_TITLE_MAX: Final = 256


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _log_fields(notification: Notification) -> dict[str, Any]:
    """The fields every line about one notification carries -- never a URL."""
    return {
        "notification_id": notification.id,
        "notification_event": notification.event,
        "severity": notification.severity,
        "account": notification.account,
        "correlation_id": notification.correlation_id,
    }


# --------------------------------------------------------------------------
# Delivery records
# --------------------------------------------------------------------------


def _record_delivery(
    session_factory: Callable[[], Session],
    *,
    notification: Notification,
    channel: str,
    status: str,
    detail: str,
    at: datetime,
) -> bool:
    """Write one ``notification_delivery`` row. Never raises; says if it landed.

    A write that fails is logged -- the one place a delivery outcome can still
    go unrecorded is a database that refuses every write, and then the log is
    the record. ``detail`` must already be scrubbed by the caller.
    """
    try:
        with session_factory() as session:
            session.add(
                NotificationDelivery(
                    notification_id=notification.id,
                    channel=channel,
                    status=status,
                    attempted_at=at,
                    detail=detail,
                )
            )
            session.commit()
    except Exception as exc:
        logger.error(
            "could not record a notification delivery outcome",
            extra={
                "event": "notification_delivery_unrecorded",
                "policy": (
                    "the log line is the record when the database refuses the "
                    "delivery row"
                ),
                "channel": channel,
                "status": status,
                "detail": detail,
                "error_type": type(exc).__name__,
                "at": at.isoformat(),
                **_log_fields(notification),
            },
        )
        return False
    return True


# --------------------------------------------------------------------------
# The database sink -- the bell
# --------------------------------------------------------------------------


class DbNotifier:
    """Writes the ``notification`` row; a bell delivery row when the bell is routed.

    The row is written for **every** notification, routed or not: the Discord
    sink's delivery rows name it, and "raised and sent nowhere" is a fact
    worth keeping. The Discord rows do not *depend* on it: ``notification_id``
    is a soft reference with no foreign key, so when this write fails --
    lock contention during a halt is the likely cause -- Discord's outcome
    still lands, and a delivery row whose notification is missing is the
    durable record that this write failed. Whether the *bell* shows it is the ``bell`` delivery row,
    written only when ``bell`` is in :attr:`Notification.channels` -- the
    routing decision made at emit time. The bell's route reads that row and
    never the current routing, so unchecking a route cannot retroactively
    erase a notification already received.

    Both rows commit together: a bell entry with no notification, or the
    reverse, cannot exist.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._now = now

    def emit(self, notification: Notification) -> None:
        at = self._now()
        try:
            with self._session_factory() as session:
                session.add(
                    NotificationRecord(
                        id=notification.id,
                        at=notification.at,
                        event=notification.event,
                        severity=notification.severity,
                        account=notification.account,
                        title=_truncate(notification.title, _TITLE_MAX),
                        body=notification.body,
                        correlation_id=notification.correlation_id,
                    )
                )
                if BELL_CHANNEL in notification.channels:
                    session.add(
                        NotificationDelivery(
                            notification_id=notification.id,
                            channel=BELL_CHANNEL,
                            status="delivered",
                            attempted_at=at,
                            detail="written to the bell",
                        )
                    )
                session.commit()
        except Exception as exc:
            # The halt this describes is already committed; the alert's other
            # sinks run regardless. What is lost is the bell entry, and the
            # log says so.
            logger.error(
                "could not write the notification row; the bell will not show it",
                extra={
                    "event": "notification_unrecorded",
                    "policy": (
                        "a failed bell write is logged and never raised -- the "
                        "halt is already persisted and Discord still runs"
                    ),
                    "error_type": type(exc).__name__,
                    "title": notification.title,
                    "body": notification.body,
                    "channels": list(notification.channels),
                    "at": at.isoformat(),
                    **_log_fields(notification),
                },
            )


# --------------------------------------------------------------------------
# The Discord sink
# --------------------------------------------------------------------------


def discord_payload(notification: Notification) -> dict[str, Any]:
    """PRD section 10's rich embed for one notification.

    ``allowed_mentions`` is empty on purpose: a halt reason is our prose but a
    future event's body may quote a vendor, and nothing in an alert should be
    able to ping ``@everyone``.
    """
    fields = [
        {"name": "Event", "value": notification.event, "inline": True},
        {"name": "Severity", "value": notification.severity, "inline": True},
        {
            "name": "Account",
            "value": notification.account or "engine (both books)",
            "inline": True,
        },
        {
            "name": "Correlation ID",
            "value": _truncate(f"`{notification.correlation_id}`", _EMBED_FIELD_VALUE_MAX),
            "inline": False,
        },
    ]
    embed: dict[str, Any] = {
        "title": _truncate(notification.title, _EMBED_TITLE_MAX),
        "description": _truncate(notification.body, _EMBED_DESCRIPTION_MAX),
        "color": _SEVERITY_COLOUR.get(notification.severity, _SEVERITY_COLOUR["info"]),
        "timestamp": notification.at.isoformat(),
        "fields": fields,
        "footer": {"text": "Corollary"},
    }
    return {
        "username": "Corollary",
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }


class _Attempt:
    """What one POST came to. Carries no URL and no raw exception text."""

    __slots__ = ("delivered", "detail", "retry_in")

    def __init__(
        self, *, delivered: bool, detail: str, retry_in: float | None = None
    ) -> None:
        self.delivered = delivered
        self.detail = detail
        #: Seconds to wait before the one retry, or ``None`` for no retry.
        self.retry_in = retry_in


class DiscordNotifier:
    """Posts a notification to a Discord webhook, off the caller's thread of control.

    :meth:`emit` is synchronous and returns at once. It enqueues onto an
    :class:`asyncio.Queue` owned by the loop :meth:`start` ran on, and a task
    on that loop delivers: one POST bounded by ``timeout_seconds``, **one**
    retry on a 5xx or a 429 (a 429 waits ``retry_after``, as Discord asks), and
    a recorded outcome for every attempt. A timeout, a transport error and any
    other 4xx are not retried -- the spec's retry is for the two answers that
    mean "try again", and a timed-out POST may already have posted.

    **Safe from any thread.** Called on the loop's own thread -- the watchdog's
    task, which is where rule 9's halts come from -- it enqueues directly;
    called from any other thread -- a sync route in FastAPI's threadpool -- it
    hands the enqueue to the loop with ``call_soon_threadsafe``. The queue is
    only ever touched on the loop.

    **Never raises from ``emit``.** Every path that cannot deliver records a
    ``dropped`` row saying why: no webhook configured, a sink built disabled,
    ``emit`` before :meth:`start` or after :meth:`aclose`, a :meth:`start`
    that raised, a delivery task that has died, a full queue. A delivery
    cancelled mid-POST by shutdown is ``failed``, not ``dropped`` -- the
    request may already have gone out.

    ``disabled_reason`` builds a sink that delivers nothing and records that
    reason for every notification routed to it -- how ``dev_app`` keeps an
    editing loop from posting to the owner's channel without an alert routed
    to Discord ever vanishing without a trace.
    """

    def __init__(
        self,
        *,
        webhook_url: str | None,
        session_factory: Callable[[], Session],
        now: Callable[[], datetime] = _utc_now,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DISCORD_TIMEOUT_SECONDS,
        retry_delay_seconds: float = DISCORD_RETRY_DELAY_SECONDS,
        max_retry_after_seconds: float = DISCORD_MAX_RETRY_AFTER_SECONDS,
        queue_size: int = DISCORD_QUEUE_SIZE,
        shutdown_grace_seconds: float = DISCORD_SHUTDOWN_GRACE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        disabled_reason: str | None = None,
    ) -> None:
        self._url = (webhook_url or "").strip()
        self._secrets = url_secrets(self._url)
        self._session_factory = session_factory
        self._now = now
        self._transport = transport
        self._timeout = timeout_seconds
        self._retry_delay = retry_delay_seconds
        self._max_retry_after = max_retry_after_seconds
        self._queue_size = queue_size
        self._shutdown_grace = shutdown_grace_seconds
        self._sleep = sleep
        self._disabled_reason = disabled_reason
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[Notification] | None = None
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._log_filter = _RedactWebhook(self._secrets)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start the delivery task on the running loop. Idempotent.

        Needs a running loop, so it is called from the lifespan rather than a
        constructor. A disabled or unconfigured sink starts nothing -- it has
        nothing to deliver, and records a ``dropped`` row per notification.

        **All or nothing.** Everything that can raise -- finding the loop,
        building the client (a stale ``SSL_CERT_FILE`` makes that raise) --
        runs before any of this sink's state is assigned. A ``start()`` that
        raises therefore leaves no queue for :meth:`emit` to feed, and every
        alert routed here is recorded ``dropped`` at emit time rather than
        parked where nothing will ever send it. :meth:`emit` also checks the
        task itself, which covers a task that started and later died.
        """
        if self._task is not None and not self._task.done():
            return
        if self._unavailable() is not None:
            return
        loop = asyncio.get_running_loop()
        # Before the client exists, so no request can be logged unfiltered.
        httpx_logger = logging.getLogger(_HTTPX_LOGGER)
        httpx_logger.addFilter(self._log_filter)
        try:
            client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self._timeout),
            )
        except BaseException:
            # No client, so no request to filter; the lifespan logs the failure.
            httpx_logger.removeFilter(self._log_filter)
            raise
        self._closing = False
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        self._client = client
        self._task = loop.create_task(self._run())

    async def join(self) -> None:
        """Wait until everything enqueued so far has an outcome. For tests."""
        if self._queue is not None:
            await self._queue.join()

    async def aclose(self) -> None:
        """Drain for a bounded grace, then stop; record what never went out."""
        self._closing = True
        queue, task = self._queue, self._task
        if queue is not None:
            try:
                await asyncio.wait_for(queue.join(), timeout=self._shutdown_grace)
            except TimeoutError:
                pass
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if queue is not None:
            while not queue.empty():
                self._drop(queue.get_nowait(), "the process shut down before delivery")
                queue.task_done()
        if self._client is not None:
            await self._client.aclose()
        # After the client is closed, so its last request is still filtered.
        logging.getLogger(_HTTPX_LOGGER).removeFilter(self._log_filter)
        self._task = None
        self._queue = None
        self._client = None
        self._loop = None

    # -- the synchronous half ----------------------------------------------

    def emit(self, notification: Notification) -> None:
        if DISCORD_CHANNEL not in notification.channels:
            return
        unavailable = self._unavailable()
        if unavailable is not None:
            self._drop(notification, unavailable)
            return
        loop, task = self._loop, self._task
        if (
            loop is None
            or loop.is_closed()
            or self._closing
            # Never started, a start() that raised, or a task that has ended
            # for any reason: an enqueue would reach nothing that sends it.
            or task is None
            or task.done()
        ):
            self._drop(notification, "the Discord delivery task is not running")
            return
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if running is loop:
                self._enqueue(notification)
            else:
                loop.call_soon_threadsafe(self._enqueue, notification)
        except Exception as exc:
            # ``call_soon_threadsafe`` on a loop closing underneath us.
            self._drop(
                notification,
                f"could not hand the alert to the delivery task ({type(exc).__name__})",
            )

    def _unavailable(self) -> str | None:
        """Why this sink can deliver nothing, or ``None`` when it can."""
        if self._disabled_reason is not None:
            return self._disabled_reason
        if not self._url:
            # Named by variable, never by value -- rule 6.
            return f"{DISCORD_WEBHOOK_ENV} is not set"
        return None

    def _enqueue(self, notification: Notification) -> None:
        """On the loop's thread only."""
        queue, task = self._queue, self._task
        # Re-checked here: a hand-off from another thread can arrive after the
        # task it was meant for has ended.
        if queue is None or self._closing or task is None or task.done():
            self._drop(notification, "the Discord delivery task is not running")
            return
        try:
            queue.put_nowait(notification)
        except asyncio.QueueFull:
            self._drop(
                notification,
                f"the Discord queue is full ({self._queue_size} undelivered alerts)",
            )

    def _drop(self, notification: Notification, reason: str) -> None:
        detail = self._scrub(reason)
        logger.error(
            "a notification routed to Discord was not sent: %s",
            detail,
            extra={
                "event": "notification_discord_dropped",
                "channel": DISCORD_CHANNEL,
                "status": "dropped",
                "detail": detail,
                **_log_fields(notification),
            },
        )
        _record_delivery(
            self._session_factory,
            notification=notification,
            channel=DISCORD_CHANNEL,
            status="dropped",
            detail=detail,
            at=self._now(),
        )

    # -- the asynchronous half ---------------------------------------------

    async def _run(self) -> None:
        queue = self._queue
        assert queue is not None
        while True:
            notification = await queue.get()
            try:
                await self._deliver(notification)
            except asyncio.CancelledError:
                # ``failed``, not ``dropped``: the POST may already have gone
                # out, and ``dropped`` means no attempt was made.
                self._record(
                    notification, "failed", "the process shut down during delivery"
                )
                queue.task_done()
                raise
            except Exception as exc:  # pragma: no cover - defensive
                # A bug in this class, not a delivery outcome. Class name
                # only: the text is not ours to vouch for.
                self._record(
                    notification,
                    "failed",
                    f"delivery raised {type(exc).__name__}",
                )
            queue.task_done()

    async def _deliver(self, notification: Notification) -> None:
        payload = discord_payload(notification)
        for attempt_number in (1, 2):
            attempt = await self._post(payload)
            if attempt.delivered:
                self._record(notification, "delivered", attempt.detail)
                return
            retry_in = attempt.retry_in if attempt_number == 1 else None
            detail = attempt.detail
            if retry_in is not None:
                detail = f"{detail}; retrying once in {retry_in:g}s"
            self._record(notification, "failed", detail)
            if retry_in is None:
                return
            await self._sleep(retry_in)

    async def _post(self, payload: dict[str, Any]) -> _Attempt:
        client = self._client
        assert client is not None
        try:
            response = await asyncio.wait_for(
                client.post(self._url, json=payload), timeout=self._timeout
            )
        except (TimeoutError, httpx.TimeoutException):
            return _Attempt(
                delivered=False, detail=f"timed out after {self._timeout:g}s"
            )
        except httpx.HTTPError as exc:
            # Class name only: httpx writes the request URL into these.
            return _Attempt(
                delivered=False, detail=f"transport error ({type(exc).__name__})"
            )
        status = response.status_code
        if 200 <= status < 300:
            return _Attempt(delivered=True, detail=f"HTTP {status}")
        message = self._vendor_message(response)
        suffix = f": {message}" if message else ""
        if status == 429:
            retry_after = _retry_after(response)
            if retry_after is None:
                retry_after = self._retry_delay
            detail = f"HTTP 429 rate limited, retry_after={retry_after:g}s{suffix}"
            if retry_after > self._max_retry_after:
                return _Attempt(
                    delivered=False,
                    detail=(
                        f"{detail}; longer than the {self._max_retry_after:g}s "
                        "this sink waits, not retried"
                    ),
                )
            return _Attempt(delivered=False, detail=detail, retry_in=retry_after)
        if status >= 500:
            return _Attempt(
                delivered=False,
                detail=f"HTTP {status}{suffix}",
                retry_in=self._retry_delay,
            )
        return _Attempt(delivered=False, detail=f"HTTP {status}{suffix}")

    def _vendor_message(self, response: httpx.Response) -> str:
        """Discord's ``message`` field, scrubbed, or nothing.

        Only the one field, never the body: Discord's error JSON is small and
        its ``message`` is what says *why* ("Unknown Webhook", "Invalid Form
        Body"). An HTML error page from a proxy has no such field and is
        ignored rather than stored.
        """
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return ""
        if not isinstance(body, dict):
            return ""
        message = body.get("message")
        if not isinstance(message, str):
            return ""
        return self._scrub(message)

    def _scrub(self, text: str) -> str:
        return vendor_detail(text, secrets=self._secrets, limit=ERROR_BODY_MAX)

    def _record(self, notification: Notification, status: str, detail: str) -> None:
        detail = self._scrub(detail)
        level = logging.INFO if status == "delivered" else logging.ERROR
        logger.log(
            level,
            "Discord delivery %s: %s",
            status,
            detail,
            extra={
                "event": f"notification_discord_{status}",
                "channel": DISCORD_CHANNEL,
                "status": status,
                "detail": detail,
                **_log_fields(notification),
            },
        )
        _record_delivery(
            self._session_factory,
            notification=notification,
            channel=DISCORD_CHANNEL,
            status=status,
            detail=detail,
            at=self._now(),
        )


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds Discord asked us to wait: the JSON ``retry_after``, else the header.

    A duration, not money, so a float is the right type here.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        body = None
    candidates: list[object] = []
    if isinstance(body, dict):
        candidates.append(body.get("retry_after"))
    candidates.append(response.headers.get("retry-after"))
    for candidate in candidates:
        if isinstance(candidate, bool) or not isinstance(candidate, (int, float, str)):
            continue
        try:
            seconds = float(candidate)
        except ValueError:
            continue
        if seconds != seconds or seconds == float("inf"):
            continue
        return max(seconds, 0.0)
    return None


#: The logger httpx writes every request line to. Named here because it is
#: the one log line in the process this module does not author and that
#: *does* carry the webhook URL -- see :class:`_RedactWebhook`.
_HTTPX_LOGGER: Final = "httpx"


class _RedactWebhook(logging.Filter):
    """Scrubs the webhook URL out of httpx's own request log line.

    httpx logs ``HTTP Request: POST <full URL> "HTTP/1.1 204 No Content"`` at
    INFO for every request, with the URL as a formatting argument -- so any
    handler at INFO or below, uvicorn's included, would write the token into
    the log. ``tests/engine/test_notify.py`` found this; it is not
    hypothetical. A filter on the ``httpx`` logger runs before any handler,
    wherever the record propagates to, so the record itself is rewritten:
    the message is rendered, scrubbed, and the arguments dropped.

    Installed by :meth:`DiscordNotifier.start` and removed by
    :meth:`DiscordNotifier.aclose`. Only records that actually contain a
    secret are touched; every other httpx line passes unchanged.
    """

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - a malformed record upstream
            return True
        if any(secret in rendered for secret in self._secrets):
            record.msg = vendor_detail(rendered, secrets=self._secrets)
            record.args = ()
        return True


# --------------------------------------------------------------------------
# The fan-out
# --------------------------------------------------------------------------


class FanoutNotifier:
    """Every sink, in order, each isolated from the others.

    One sink raising must not stop the next: a database that refuses the bell
    row is no reason for Discord to stay quiet about the halt that caused it.
    Every sink in this module already catches its own failures; this catches
    the one that does not, and logs it by class name only.
    """

    def __init__(self, sinks: Sequence[Notifier]) -> None:
        self._sinks: tuple[Notifier, ...] = tuple(sinks)

    @property
    def sinks(self) -> tuple[Notifier, ...]:
        return self._sinks

    def emit(self, notification: Notification) -> None:
        for sink in self._sinks:
            try:
                sink.emit(notification)
            except Exception as exc:
                logger.error(
                    "a notification sink raised; the others still run",
                    extra={
                        "event": "notification_sink_failed",
                        "sink": type(sink).__name__,
                        "error_type": type(exc).__name__,
                        **_log_fields(notification),
                    },
                )
