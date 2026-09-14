"""``EngineRuntime``: lifecycle, halt state, and the dead-man's switch.

CLAUDE.md rule 9 is what this module is for, and it is the one item in Phase 2
that cannot be retrofitted:

    If the engine loses its Alpaca connection, or the risk manager stops
    heartbeating for 90 seconds, the engine calls ``halt()`` on itself and
    fires a critical notification. Recovery requires an explicit human resume.

    **Never auto-resume on reconnect.** Reconnecting into an unverified
    position state is how a bot doubles a position it already holds.

So this module **sets** a halt and never ends one. ``POST /api/engine/resume``
is the only thing in this codebase that ends a halt, via the private helper in
``api/routes/engine.py``; ``tests/engine/test_runtime.py`` greps this file to
prove no second path grew here, the same structural standard
``test_no_order_path.py`` holds the order path to. A rule a reviewer has to
notice is a rule that gets lost in a plausible-looking diff.

**Halting stops nothing in Phase 2, because nothing trades.** The state, the
notification and the explicit-resume requirement are real and tested anyway.
By the time there is an order path, every caller that might clear a halt
already exists, and finding them is archaeology.

Rule 7 is not blurred here either: a halt stops new entries and leaves managed
exits running. There is no flatten in this module and there must not be one --
a flatten closes positions, which is a different consequence and stays a
different control.

Where this runs, and why it is not a service
--------------------------------------------

Design spec decision 1 is *one process*, so the runtime is constructed in the
FastAPI lifespan rather than in a daemon of its own. ``api/app.py`` builds it,
calls :meth:`EngineRuntime.start`, hands it to :meth:`EngineRuntime.supervise`,
and closes it on shutdown.

**The halt does not ride the websocket, and that is deliberate.** Engine state
and notifications are polled at 15s while the WS carries quotes and
``trade_updates`` only. Two reasons, both load-bearing. Rule 9 halts *because*
the socket closed, so a halt notification pushed over that socket is a
notification nobody receives at the one moment it matters. And a broken client
socket has to stay distinguishable from a halted engine: collapse the two and
a human presses Resume on an engine that was never halted, which is exactly
the control rule 9 exists to keep explicit and human.

The two conditions, and why neither can fire in the shipped app yet
------------------------------------------------------------------

:class:`Watchdog` implements both of rule 9's conditions. **Neither one can
fire in the app as shipped, for the same reason: nothing produces the events
they measure.** Saying so here is the point -- a reader who believes the
connection condition is live will not go looking for the missing ``record_*``
calls.

* **Connection loss** -- ninety seconds with no message and no successful poll,
  or a websocket close. **No producer yet.** ``record_message``,
  ``record_poll``, ``record_stream_open``, ``record_stream_closed`` and
  ``record_opening_snapshot`` have no caller outside ``tests/``, and there is
  no websocket client anywhere under ``corollary/``. Step 8d's
  ``api/routes/ws.py`` is what attaches the transport that calls them, and
  that is a call site rather than a new condition.
* **A stalled risk-manager heartbeat** -- ninety seconds without one. **No
  producer either, and additionally unarmed by default.** ``RiskManager`` is
  nine lines and has no body, so armed with no producer this condition would
  halt every engine ninety seconds after boot. It ships behind
  ``heartbeat_armed=False`` so that wiring the producer is one argument rather
  than a new condition written under time pressure on the day ``RiskManager``
  grows a body.

The difference between the two is only the default: the connection condition
is armed but unfed, the heartbeat condition is unarmed *and* unfed. Both are
tested, neither is silently missing, and until step 8d this module is a proven
switch with no wire attached to it.

The connection condition is also **unarmed until something first connects**. A
runtime that has never seen a message or a poll has no connection to have
lost, and a cold start is already halted by the ``engine_state`` default --
halting it again for a socket that was never opened would put a fault reason
on an ordinary boot.

What stops a halt being announced twice
---------------------------------------

The watchdog answers *"what is wrong right now"*, freshly, every time it is
asked; it keeps no record of what it has already reported. The gate that stops
a five-second supervisor re-notifying eighteen times a minute lives in
:meth:`EngineRuntime.check_watchdog`, and it reads **``engine_state``** --
the row ``POST /api/engine/resume`` clears, and the only source of truth about
whether this engine is halted. It reads ``halted_reason`` alongside ``halted``,
because a cold start is halted with no reason at all and a fault during one is
still news; :meth:`EngineRuntime._halt_is_recorded` states that in full.

The gate's second term is this process's memory of a halt that ``engine_state``
would **not take** -- a read-only filesystem, a full disk, a locked file. That
halt is real, it was announced, and no row carries it, so a gate reading only
the row re-halts and re-notifies on every tick: one ongoing fault, a critical
alert every five seconds. The memory is set *only* when the write failed, which
is what stops it outliving a resume; :meth:`EngineRuntime.check_watchdog` has
the four states it has to get right, in a table.

**That memory is a state, never a latch.** It means "our record is missing",
and a thing that means that must end when the record is no longer missing --
so every suppressed tick retries the write
(:meth:`EngineRuntime._retry_persist`), and the fault ending clears it
regardless. Without both it was the same defect one level down: a single
refused commit, and the switch was silent for the life of the process, because
nothing a database recovering or a human resuming could do would ever reach it.

That is deliberate, and it replaced an in-memory latch that was wrong in a way
worth recording. A latch inside the watchdog answers *"have I already
announced this?"* out of memory that a resume cannot reach, so an operator who
resumed while the fault was still present got an engine that was running,
un-halted, with the switch permanently silent: the socket latch cleared only
on a reopen and the staleness latch only on a message, neither of which can
arrive from the dead feed that caused the halt in the first place.

Reading the row instead produces one behaviour that is **not** a bug and must
not be suppressed: **a resume into an ongoing fault re-halts within one
watchdog tick, with a fresh notification.** That is a halt, not an
auto-resume, so rule 9 is honoured -- and it is the right answer, because the
alternative leaves a human believing a resume worked while the feed is still
delivering nothing.

One more convention, small and worth stating: in this file the structured-log
key ``rule`` always carries a :class:`HaltRule` value, never a sentence. Prose
goes under ``policy``. See :meth:`EngineRuntime.halt` for the one place that
matters and for the mismatch it leaves behind.

Cold start, and why the runtime does not clear the cold-start halt
------------------------------------------------------------------

The design spec words the cold start as *halted until the opening snapshot
succeeds*. What the runtime does is record the opening snapshot as liveness
and expose :attr:`EngineRuntime.opening_snapshot_ok`; it does **not** clear
the halt, because a second code path that ends a halt is precisely what rule 9
forbids, and one that ends a halt automatically on a successful
reconnect-and-snapshot is the exact wording of what it forbids. The halt ends
where every other halt ends: at an explicit human resume.

``t0`` is written on the first ever start and never rewritten -- the equity
curve's marker for where Corollary started running, which would otherwise walk
forward on every ``uvicorn --reload``. ``api/routes/engine.py`` owns that rule
in ``mark_started``, along with the create-if-missing rule in ``engine_state``,
and this module **calls them rather than restating them**: two places deciding
whether ``t0`` is rewritten is how a marker starts moving, and two places
deciding what a missing row means is how an absent state becomes evidence of a
healthy engine.

Both imports are **deferred into the methods that use them**, and that is not
a style choice. ``corollary/api/__init__.py`` imports ``app`` eagerly and
``app`` imports this module, so a module-level import here is a genuine
circular import that fails at collection. The structurally right fix is for
those two helpers to live somewhere neither layer owns -- they are persistence,
not routing -- which is a change to a file this step does not own. Until then
the import is local, and the alternative (a second copy of the ``t0`` rule in
``engine/``) is the one thing worse than a deferred import.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from corollary.db.models import NOTIFICATION_CHANNELS, NotificationRoute
from corollary.engine.stream import (
    STREAM_SYMBOL_CAP,
    SubscriptionPlan,
    SubscriptionUnit,
    plan_subscriptions,
)

__all__ = [
    "ALPACA_DATA_PLAN_ENV",
    "BASIC_PLAN",
    "DISCORD_WEBHOOK_ENV",
    "HALT_EVENT",
    "HALT_SEVERITY",
    "PAID_PLAN",
    "UNLIMITED_STREAM_SYMBOL_CAP",
    "WATCHDOG_INTERVAL_SECONDS",
    "WATCHDOG_TIMEOUT_SECONDS",
    "EngineRuntime",
    "HaltDecision",
    "HaltRule",
    "LoggingNotifier",
    "Notification",
    "Notifier",
    "Watchdog",
    "data_plan",
    "stream_symbol_cap_for_plan",
]

logger = logging.getLogger(__name__)

#: Rule 9's ninety seconds, written once. Every condition measures against
#: this and no call site carries the literal, so an operational change is one
#: value rather than a grep.
WATCHDOG_TIMEOUT_SECONDS: Final[float] = 90.0

#: How often the supervisor asks the watchdog whether anything is wrong. Far
#: below the timeout on purpose: the interval bounds how *late* a halt can be,
#: and five seconds of lateness on a ninety-second limit is noise.
WATCHDOG_INTERVAL_SECONDS: Final[float] = 5.0

#: The notification event a rule-9 halt is filed under. One of PRD section
#: 10's three critical events and one of the seeded ``notification_route``
#: rows, so the routing decision is already one a human has configured.
HALT_EVENT: Final = "engine_error"

#: The severity every rule-9 halt carries. Not a parameter: a dead-man's
#: switch firing is critical by definition, and a caller that could choose
#: would eventually choose wrong.
HALT_SEVERITY: Final = "critical"

#: Read for *presence* only -- rule 6, the value never reaches a log or a
#: response. A ``discord`` route with no webhook is a critical alert with
#: nowhere to go, which is worth a warning at the moment it happens.
DISCORD_WEBHOOK_ENV: Final = "DISCORD_WEBHOOK_URL"

#: The account's data plan, from the environment. Re-declared here rather than
#: imported from ``api/routes/settings.py`` so that a reader of ``engine/``
#: sees the value this module actually uses; ``test_runtime.py`` pins all
#: three against that module, which is what stops the spellings drifting.
ALPACA_DATA_PLAN_ENV: Final = "ALPACA_DATA_PLAN"
BASIC_PLAN: Final = "basic"
PAID_PLAN: Final = "algo_trader_plus"

#: Algo Trader Plus streams every symbol -- Alpaca documents no ceiling at all.
#: A budget still needs a number, so this is one comfortably above anything
#: reachable: the whole US equity universe is roughly 5,000 symbols and an
#: eight-position option book is dozens. It is not infinity; it is a real cap,
#: large enough that ``plan_subscriptions`` never drops a unit under it.
UNLIMITED_STREAM_SYMBOL_CAP: Final[int] = 10_000

#: ``engine_state.halted_reason`` is ``String(256)``. A reason is truncated
#: rather than refused: losing the tail of a sentence is survivable, and
#: failing to record *why* the engine halted is not.
_REASON_MAX: Final[int] = 256


# --------------------------------------------------------------------------
# The plan of record, and the budget it implies
# --------------------------------------------------------------------------


def data_plan(env: Mapping[str, str]) -> str:
    """Which Alpaca data plan this account is on, from the environment.

    An unrecognised value reads as :data:`BASIC_PLAN` with a warning rather
    than raising, matching ``api/routes/settings.py``. Basic is the
    restrictive direction: the failure mode of a typo is a stream capped
    lower than it needed to be, which is visible and harmless, rather than an
    engine subscribing to 200 symbols on a plan that allows thirty.
    """
    raw = (env.get(ALPACA_DATA_PLAN_ENV) or "").strip().lower()
    if not raw:
        return BASIC_PLAN
    if raw not in (BASIC_PLAN, PAID_PLAN):
        logger.warning(
            "%s=%r is not a plan this app knows; reading it as %s",
            ALPACA_DATA_PLAN_ENV,
            raw,
            BASIC_PLAN,
            extra={
                "event": "engine_unknown_plan",
                "policy": (
                    "an unknown plan reads as basic -- the restrictive "
                    "direction, so a typo cannot lift a cap nobody paid for"
                ),
                "accepted": [BASIC_PLAN, PAID_PLAN],
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return BASIC_PLAN
    return raw


def stream_symbol_cap_for_plan(plan: str) -> int:
    """The websocket symbol budget this plan allows.

    ``engine/stream.py`` is deliberately ignorant of the environment and takes
    the cap as an argument; this is the caller side it named. On Algo Trader
    Plus the thirty-symbol limit does not exist, and a stream still cutting at
    thirty while running full OPRA reports *"N symbols not streamed"* for a
    limit that was lifted -- a banner that is wrong is worse than no banner,
    because the next real one gets ignored.
    """
    return UNLIMITED_STREAM_SYMBOL_CAP if plan == PAID_PLAN else STREAM_SYMBOL_CAP


# --------------------------------------------------------------------------
# What a halt is
# --------------------------------------------------------------------------


class HaltRule(StrEnum):
    """Why the engine halted itself. One value per condition, never a message.

    A rule is a thing you can count, filter and alert on; a sentence is not --
    the same reasoning as ``ledger.RejectionRule``, ``grouping.DeclineRule``
    and ``stream.DropRule``.
    """

    #: The websocket closed. Immediate: there is nothing to wait for.
    STREAM_CLOSED = "stream_closed"
    #: Ninety seconds with no message and no successful poll. The connection
    #: may still look open; nothing has come through it.
    CONNECTION_STALE = "connection_stale"
    #: Ninety seconds without a risk-manager heartbeat. **Inert** -- nothing
    #: produces one until ``RiskManager`` grows a body. See the module
    #: docstring for why it ships implemented and unarmed rather than absent.
    HEARTBEAT_STALE = "heartbeat_stale"


@dataclass(frozen=True, slots=True)
class HaltDecision:
    """One halt, with everything rule 8 asks a rejection to record.

    The **rule** (:attr:`rule`), the **inputs** (:attr:`inputs`) and the
    **timestamp** (:attr:`at`), plus the sentence that goes in
    ``engine_state.halted_reason`` and in front of a human.
    """

    rule: HaltRule
    #: Persisted verbatim, bounded to 256 characters. Written for the person
    #: who finds the engine doing nothing and wants to know why in one line.
    reason: str
    #: Everything the condition measured, for the structured log. Not persisted
    #: -- the column is one sentence wide, and these are for the record that
    #: gets grepped rather than the one that gets rendered.
    inputs: Mapping[str, Any]
    at: datetime


@dataclass(frozen=True, slots=True)
class Notification:
    """A critical alert, addressed to the channels a human left switched on.

    Carried rather than sent: :attr:`channels` is the routing *decision*, read
    from ``notification_route``, and delivering to each channel is a
    :class:`Notifier`. The split is what lets the halt path stay synchronous
    and testable while a Discord webhook is an HTTP call that can hang.
    """

    event: str
    severity: str
    title: str
    body: str
    at: datetime
    correlation_id: str
    #: The enabled channels, in ``NOTIFICATION_CHANNELS`` order. Empty means a
    #: human switched every channel off for this event, which the PRD permits
    #: and which is logged rather than overridden.
    channels: tuple[str, ...]


class Notifier(Protocol):
    """Delivery. One method, so a Discord sender is a new class not an edit."""

    def emit(self, notification: Notification) -> None: ...


class LoggingNotifier:
    """The default sink: a structured log line per notification.

    The ``notification`` table does not exist yet -- it is the tenth table and
    lands with the notifications work -- and the Discord transport is not
    written either. That is a gap in *delivery*, not in the decision: the
    event, the severity and the resolved channels are all computed here and
    handed over whole, so wiring a real sink is a constructor argument rather
    than a change to the halt path.

    Logging rather than raising on an unconfigured channel is deliberate. A
    dead-man's switch whose alerting raises would turn one fault into two, and
    the halt itself is already persisted by the time this runs.
    """

    def emit(self, notification: Notification) -> None:
        logger.critical(
            "%s: %s",
            notification.title,
            notification.body,
            extra={
                "event": "engine_notification",
                "notification_event": notification.event,
                "severity": notification.severity,
                "channels": list(notification.channels),
                "title": notification.title,
                "body": notification.body,
                "at": notification.at.isoformat(),
                "correlation_id": notification.correlation_id,
            },
        )


def _utc(moment: datetime) -> datetime:
    """UTC, refusing a naive datetime rather than guessing what it meant.

    ``astimezone`` on a naive datetime silently reads it as *this machine's*
    local time, which on a halt record reconciled against a fill is a
    four-or-five-hour error with no symptom. The same refusal
    ``stream._utc``, ``db.types.UtcDateTime`` and ``wire.require_aware`` each
    make at their own boundary.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            "an engine runtime timestamp must be timezone-aware; a naive "
            f"datetime would be read as this machine's local time. Got {moment!r}"
        )
    return moment.astimezone(timezone.utc)


def _bounded(reason: str) -> str:
    """Fit a reason into ``engine_state.halted_reason``, truncating visibly."""
    if len(reason) <= _REASON_MAX:
        return reason
    return reason[: _REASON_MAX - 1] + "…"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_correlation_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# The watchdog
# --------------------------------------------------------------------------


class Watchdog:
    """Rule 9's two conditions, over an injected clock. Opens no socket.

    The caller records what happened -- a message, a successful poll, a
    heartbeat, a close, a reopen -- and asks :meth:`evaluate` whether any of
    that adds up to a halt. Nothing here reads a clock or touches a database,
    which is what makes ninety seconds testable in microseconds.

    **Nothing here latches.** :meth:`evaluate` reports the fault that is
    present at the moment it is asked, every time it is asked, for as long as
    the fault lasts. A fault ends when the thing that caused it ends: a
    message or a poll ends :attr:`HaltRule.CONNECTION_STALE`, a reopen ends
    :attr:`HaltRule.STREAM_CLOSED`, a heartbeat ends
    :attr:`HaltRule.HEARTBEAT_STALE`. A poll arriving while the socket is
    still shut ends nothing, because the socket is still shut.

    Not re-announcing an ongoing fault is
    :meth:`EngineRuntime.check_watchdog`'s job, and it does it by reading
    ``engine_state.halted`` rather than by remembering. See the module
    docstring: a latch in here is memory a human's resume cannot reach, and it
    silenced the switch for exactly the operator who most needed it. Keeping
    this class memoryless is what makes its view and the database's impossible
    to diverge.
    """

    __slots__ = (
        "_closed_at",
        "_closed_detail",
        "_heartbeat_armed",
        "_last_activity_at",
        "_last_activity_source",
        "_last_heartbeat_at",
        "_started_at",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        started_at: datetime,
        timeout_seconds: float = WATCHDOG_TIMEOUT_SECONDS,
        heartbeat_armed: bool = False,
    ) -> None:
        self._started_at = _utc(started_at)
        self._timeout_seconds = timeout_seconds
        #: False until ``RiskManager`` has a body that heartbeats. See the
        #: module docstring: armed with no producer, this halts every engine
        #: ninety seconds after boot.
        self._heartbeat_armed = heartbeat_armed
        self._last_activity_at: datetime | None = None
        self._last_activity_source = "none"
        self._last_heartbeat_at: datetime | None = None
        self._closed_at: datetime | None = None
        self._closed_detail = ""

    # -- what the caller records ------------------------------------------

    def record_message(self, at: datetime) -> None:
        """A quote or a ``trade_updates`` message arrived."""
        self._record_activity(at, "message")

    def record_poll(self, at: datetime) -> None:
        """A poll succeeded. Liveness too -- the spec says *message or poll*."""
        self._record_activity(at, "poll")

    def record_stream_open(self, at: datetime) -> None:
        """The socket is up again.

        Ends the *close* this class reports and **nothing else**. The halt
        in ``engine_state`` is untouched: rule 9's whole point is that the
        socket coming back is not evidence that the position state is
        verified, and this class could not end a halt if it wanted to -- it
        has no database and no opinion about one.
        """
        moment = _utc(at)
        self._closed_at = None
        self._closed_detail = ""
        self._record_activity(moment, "stream_open")

    def record_stream_closed(self, at: datetime, *, detail: str = "") -> None:
        """The websocket closed, for the stated reason if the transport gave one."""
        self._closed_at = _utc(at)
        self._closed_detail = detail or "no reason given"

    def record_heartbeat(self, at: datetime) -> None:
        """The risk manager is alive.

        **Nothing calls this yet.** ``RiskManager`` has no body, so the
        condition it feeds is unarmed by default; see the module docstring.
        """
        self._last_heartbeat_at = _utc(at)

    def _record_activity(self, at: datetime, source: str) -> None:
        moment = _utc(at)
        if self._last_activity_at is None or moment > self._last_activity_at:
            self._last_activity_at = moment
            self._last_activity_source = source

    # -- the question -----------------------------------------------------

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def heartbeat_armed(self) -> bool:
        return self._heartbeat_armed

    @property
    def last_activity_at(self) -> datetime | None:
        return self._last_activity_at

    def evaluate(self, now: datetime) -> HaltDecision | None:
        """Has anything gone wrong? ``None`` when the answer is no.

        Conditions are checked in the order they become knowable: a close is a
        fact the moment it arrives, staleness takes ninety seconds to become
        one. The boundary is ``>=``, so eighty-nine seconds is healthy and
        ninety is not.

        Asked twice during one ongoing fault it answers twice, with equal
        decisions. That is not a bug to fix in here: the caller is what
        decides whether an answer is *news*, and it decides that against
        ``engine_state.halted``.
        """
        moment = _utc(now)
        if self._closed_at is not None:
            return self._decide(
                HaltRule.STREAM_CLOSED,
                _bounded(
                    f"The market data stream closed ({self._closed_detail}). The "
                    "engine halted itself; the socket may reconnect but the halt "
                    "does not clear without an explicit resume."
                ),
                {
                    "closed_at": self._closed_at.isoformat(),
                    "detail": self._closed_detail,
                    "elapsed_seconds": (moment - self._closed_at).total_seconds(),
                    "timeout_seconds": self._timeout_seconds,
                },
                moment,
            )

        if self._last_activity_at is not None:
            elapsed = (moment - self._last_activity_at).total_seconds()
            if elapsed >= self._timeout_seconds:
                return self._decide(
                    HaltRule.CONNECTION_STALE,
                    _bounded(
                        f"No market data message and no successful poll for "
                        f"{elapsed:.0f}s, against a {self._timeout_seconds:.0f}s "
                        "limit. The engine halted itself; recovery requires an "
                        "explicit resume."
                    ),
                    {
                        "elapsed_seconds": elapsed,
                        "timeout_seconds": self._timeout_seconds,
                        "last_activity_at": self._last_activity_at.isoformat(),
                        "last_activity_source": self._last_activity_source,
                    },
                    moment,
                )

        if self._heartbeat_armed:
            since = self._last_heartbeat_at or self._started_at
            elapsed = (moment - since).total_seconds()
            if elapsed >= self._timeout_seconds:
                return self._decide(
                    HaltRule.HEARTBEAT_STALE,
                    _bounded(
                        f"The risk manager has not heartbeat for {elapsed:.0f}s, "
                        f"against a {self._timeout_seconds:.0f}s limit. The engine "
                        "halted itself; recovery requires an explicit resume."
                    ),
                    {
                        "elapsed_seconds": elapsed,
                        "timeout_seconds": self._timeout_seconds,
                        "last_heartbeat_at": (
                            self._last_heartbeat_at.isoformat()
                            if self._last_heartbeat_at is not None
                            else None
                        ),
                        "started_at": self._started_at.isoformat(),
                    },
                    moment,
                )

        return None

    def _decide(
        self,
        rule: HaltRule,
        reason: str,
        inputs: Mapping[str, Any],
        at: datetime,
    ) -> HaltDecision:
        """Build the decision. Remembers nothing -- see the class docstring."""
        return HaltDecision(rule=rule, reason=reason, inputs=inputs, at=at)


# --------------------------------------------------------------------------
# The runtime
# --------------------------------------------------------------------------


class EngineRuntime:
    """The engine's lifecycle and its halt state. One per process.

    Constructed in the FastAPI lifespan (design spec decision 1: one process).
    Everything it depends on is injected, so a test drives ninety seconds of
    silence in microseconds and no test needs a socket or a credential:

    * ``session_factory`` -- opens a :class:`~sqlalchemy.orm.Session`.
    * ``now`` -- the clock. Every timestamp this class writes comes from it.
    * ``notifier`` -- delivery, defaulting to :class:`LoggingNotifier`.
    * ``env`` -- the process environment, defaulting to ``os.environ``.
      Nothing under ``corollary/`` reads ``.env``; the environment is the
      engine's input and the launcher is what loads the file.
    * ``correlation_ids`` -- one id per decision, so a halt's persistence, its
      log line and its notification read as the one event they are.

    **Nothing on this class ends a halt.** It is the only rule the type
    enforces structurally, and ``test_runtime.py`` greps the file to keep it
    that way.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        now: Callable[[], datetime] = _utc_now,
        notifier: Notifier | None = None,
        env: Mapping[str, str] | None = None,
        correlation_ids: Callable[[], str] = _new_correlation_id,
        timeout_seconds: float = WATCHDOG_TIMEOUT_SECONDS,
        heartbeat_armed: bool = False,
        watchdog_interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._now = now
        self._notifier: Notifier = LoggingNotifier() if notifier is None else notifier
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._correlation_ids = correlation_ids
        self._interval = watchdog_interval_seconds
        self._watchdog = Watchdog(
            started_at=_utc(now()),
            timeout_seconds=timeout_seconds,
            heartbeat_armed=heartbeat_armed,
        )
        self._task: asyncio.Task[None] | None = None
        self._opening_snapshot_ok = False
        #: The halt this process announced that the record does **not**
        #: carry: ``_persist`` refused, or the row could not be read to check.
        #: Consulted by :meth:`check_watchdog`'s gate in every branch, which
        #: is what keeps a readable-but-unwritable database from re-notifying
        #: on every tick. It is not the halt state and must never be used as
        #: one -- ``engine_state.halted`` is, and a resume writes that row.
        #:
        #: **Set only when the write failed, and that is the whole trick.** A
        #: successful read saying "no explained halt is in force" cannot on
        #: its own separate *a human resumed* from *our write never landed*:
        #: during a live fault both read as an un-halted row. ``_persist``'s
        #: return value is the only thing that can, so a halt the row took
        #: clears this immediately and is policed by the row alone, where a
        #: resume is visible. Per-process, and dies with the process.
        #:
        #: **It is bounded, and the bounds are the point.** This flag says
        #: "our record is missing", which is a *state* and not an event, so it
        #: has to end when the state does. Three things end it: the row coming
        #: to carry an explained halt (:meth:`_halt_is_recorded`), the retry in
        #: :meth:`_retry_persist` landing once the database is writable again,
        #: and the fault itself ending (:meth:`check_watchdog`'s healthy
        #: branch). Without the last two it was a one-way latch on a single
        #: refused commit: :meth:`halt` cannot clear it, because the gate that
        #: consults it is what stops ``halt`` running again, and a resume
        #: clears the row -- not halted, no stated reason -- which is not an
        #: explained halt either. One ``database is locked`` then suppressed
        #: every announcement for the life of the process -- rule 9 off, with
        #: no sign of it anywhere.
        self._announced: HaltRule | None = None
        #: The announcing halt's correlation id, and the moment it was
        #: announced. Carried for exactly as long as :attr:`_announced` is --
        #: set, cleared and forgotten in the same places, on adjacent lines,
        #: so the three can never come to describe different halts.
        #:
        #: Read in **one** place, :meth:`_retry_persist`'s log line, and never
        #: by the gate -- the same standing the two fields below have. Without
        #: them a halt written half an hour late is a record with no key
        #: joining it to the alert it belongs to: three artifacts, two
        #: timestamps, one event, and a post-incident reader reconciling the
        #: gap out of prose. CLAUDE.md asks for one correlation id per
        #: decision, and every other record on this file's halt path has one.
        self._announced_correlation_id: str | None = None
        self._announced_at: datetime | None = None
        #: The rule whose halt this process wrote into ``engine_state``, for
        #: as long as a read still shows *that* halt in force -- cleared the
        #: moment a read says otherwise, because then the row is somebody
        #: else's or nobody's. Read in one place: :meth:`_log_ongoing`, to
        #: tell an ongoing fault the record already describes from one it does
        #: not. Never a halt state either.
        #:
        #: Set by :meth:`halt`, and **only on a write that landed**: a halt
        #: the database refused is not in the record, so there is nothing for
        #: a later fault to agree with.
        self._recorded: HaltRule | None = None
        #: The exact ``halted_reason`` text written beside :attr:`_recorded`,
        #: which is how that field is checked against the row rather than
        #: merely remembered. ``halted_reason`` is prose, not a
        #: :class:`HaltRule`, so the row cannot say which rule explains it;
        #: what it can say is whether it still carries the sentence this
        #: process wrote. Already truncated through :func:`_bounded`, so it
        #: compares equal to what a read brings back.
        self._recorded_reason: str | None = None

    # -- state a caller can read ------------------------------------------

    @property
    def watchdog(self) -> Watchdog:
        return self._watchdog

    @property
    def opening_snapshot_ok(self) -> bool:
        """Has the opening snapshot succeeded since this process started?

        Readiness, **not** permission. The frontend's ``lastTickAt === null``
        skeleton is what this answers; whether the engine may act is
        ``engine_state.halted``, and only an explicit resume changes that.
        """
        return self._opening_snapshot_ok

    @property
    def stream_symbol_cap(self) -> int:
        """The websocket budget, derived from the plan of record.

        ``engine/stream.py`` names this as the caller's responsibility. This
        is that caller.
        """
        return stream_symbol_cap_for_plan(data_plan(self._env))

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Write ``t0`` if it has never been written, and say what state we are in.

        Does **not** touch ``halted``, in either direction. A cold start is
        halted because ``engine_state.halted`` defaults to true; a restart
        after an explicit resume stays running, and the watchdog will halt it
        within ninety seconds if the connection is not really there. A restart
        is not a resume, and it is not a halt either -- inventing a third halt
        condition here would be a fault reason on an ordinary reload.

        A database failure is logged and swallowed, the same call the lifespan
        makes for the same reason: an unmigrated database must degrade the
        routes that need a table, not stop the process booting.
        """
        # Deferred: see the module docstring. `corollary.api` imports `app`,
        # which imports this module, so importing the helper at module level
        # is a circular import rather than a preference.
        from corollary.api.routes.engine import mark_started

        at = _utc(self._now())
        try:
            with self._session_factory() as session:
                state = mark_started(session, at=at)
                # Named ``is_halted`` rather than ``halted`` on purpose.
                # A local called ``halted`` sits one careless edit away from
                # the exact literal that
                # ``test_the_runtime_has_no_code_that_clears_a_halt`` greps
                # this file for, and a guard that cries wolf is a guard
                # somebody eventually deletes.
                is_halted = state.halted
                reason = state.halted_reason
        except SQLAlchemyError:
            logger.exception(
                "could not read or write engine_state on start; run "
                "`uv run alembic upgrade head`",
                extra={
                    "event": "engine_start_degraded",
                    "policy": (
                        "a missing migration degrades the engine state, it does "
                        "not stop the process"
                    ),
                    "at": at.isoformat(),
                },
            )
            return
        logger.info(
            "engine runtime started (halted=%s)",
            is_halted,
            extra={
                "event": "engine_runtime_started",
                "policy": (
                    "a cold start comes up halted and a restart is not a resume "
                    "(CLAUDE.md rule 9)"
                ),
                "halted": is_halted,
                "halted_reason": reason,
                "stream_symbol_cap": self.stream_symbol_cap,
                "watchdog_timeout_seconds": self._watchdog.timeout_seconds,
                "heartbeat_armed": self._watchdog.heartbeat_armed,
                "at": at.isoformat(),
            },
        )

    def supervise(self) -> None:
        """Run the watchdog on its own asyncio task. Idempotent.

        Separate from :meth:`start` because starting needs no event loop and
        this does. The loop never dies of one bad tick: a watchdog that stops
        checking is indistinguishable from a healthy engine, which is the
        failure this whole module exists to make impossible.
        """
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    self.check_watchdog()
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "the watchdog raised; it keeps running",
                        extra={"event": "engine_watchdog_error"},
                    )
        except asyncio.CancelledError:
            raise

    async def aclose(self) -> None:
        """Stop supervising. Safe on a runtime that never supervised."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        logger.info(
            "engine runtime stopped",
            extra={
                "event": "engine_runtime_stopped",
                "at": _utc(self._now()).isoformat(),
            },
        )

    # -- what the sockets and the poll loop report -------------------------

    def record_message(self, at: datetime | None = None) -> None:
        """A quote or a ``trade_updates`` message arrived."""
        self._watchdog.record_message(self._moment(at))

    def record_poll(self, at: datetime | None = None) -> None:
        """A poll succeeded."""
        self._watchdog.record_poll(self._moment(at))

    def record_heartbeat(self, at: datetime | None = None) -> None:
        """The risk manager is alive. **No producer yet** -- see the module docstring."""
        self._watchdog.record_heartbeat(self._moment(at))

    def record_stream_open(self, at: datetime | None = None) -> None:
        """The socket came back. **This does not resume anything.**

        Rule 9, verbatim: never auto-resume on reconnect. Reconnecting into an
        unverified position state is how a bot doubles a position it already
        holds. The watchdog stops complaining about the socket; the halt in
        ``engine_state`` stays exactly where it was.
        """
        self._watchdog.record_stream_open(self._moment(at))

    def record_stream_closed(
        self, at: datetime | None = None, *, detail: str = ""
    ) -> None:
        """The socket closed. The next watchdog check halts on it."""
        self._watchdog.record_stream_closed(self._moment(at), detail=detail)

    def record_opening_snapshot(self, at: datetime | None = None) -> None:
        """The opening snapshot succeeded.

        Counts as liveness and sets :attr:`opening_snapshot_ok`. It does
        **not** clear the halt -- see the module docstring on why the
        cold-start halt still ends at an explicit resume like every other one.
        """
        moment = self._moment(at)
        self._watchdog.record_poll(moment)
        first = not self._opening_snapshot_ok
        self._opening_snapshot_ok = True
        if first:
            logger.info(
                "opening snapshot succeeded",
                extra={
                    "event": "engine_opening_snapshot",
                    "policy": (
                        "the snapshot is readiness, not permission; the halt "
                        "ends only at POST /api/engine/resume"
                    ),
                    "at": moment.isoformat(),
                },
            )

    def _moment(self, at: datetime | None) -> datetime:
        return _utc(self._now() if at is None else at)

    # -- the budget --------------------------------------------------------

    def plan_stream_subscriptions(
        self,
        units: Iterable[SubscriptionUnit],
        *,
        correlation_id: str | None = None,
    ) -> SubscriptionPlan:
        """Fit the desired symbols into this account's budget.

        The clock and the correlation id come from here rather than from
        ``stream.py``, which reads neither by design: identical inputs have to
        give an identical subscription list, and an id minted inside that
        module would tie the plan to nothing upstream of it.
        """
        return plan_subscriptions(
            units,
            at=_utc(self._now()),
            correlation_id=correlation_id or self._correlation_ids(),
            cap=self.stream_symbol_cap,
        )

    # -- the switch --------------------------------------------------------

    def check_watchdog(self) -> HaltDecision | None:
        """Ask the watchdog, and halt if it answers. ``None`` when all is well.

        Called by the supervisor task every :data:`WATCHDOG_INTERVAL_SECONDS`,
        and callable directly by a test or a poll loop that has just noticed
        something.

        Returns a decision **only when this call halted the engine**. An
        ongoing fault on an engine already halted *with a stated reason*
        returns ``None``: it is not news, and the alert channel would
        otherwise carry the same line every five seconds until a human read it
        once. A halt with no stated reason is a cold start rather than a
        recorded fault, and a fault during one is announced --
        :meth:`_halt_is_recorded` is where that distinction lives.

        The gate has **two terms**, because neither alone is right in all
        five states it has to cover:

        =================================  =============================  ==========
        State                              What the gate has              Outcome
        =================================  =============================  ==========
        Cold start, first fault            row halted, no reason;         announces
                                           nothing announced
        Persisted halt, ongoing fault      row halted with a reason       suppressed
        Readable but unwritable database   row does not carry the halt;   suppressed
                                           this process announced one     (and the
                                           the row refused                write is
                                                                          retried)
        Observed resume, fault present     row carries no explained       announces
                                           halt, and the halt announced
                                           *was* recorded -- so a human
                                           cleared it
        New episode, halt never recorded   row carries no explained       announces
                                           halt, and the announcement
                                           ended with the episode it
                                           belonged to
        =================================  =============================  ==========

        **The last two rows read identically to the gate and are not the same
        thing**, which is why both are listed rather than one standing in for
        the other. ``_announced is None`` means *the halt we announced was
        recorded, so the empty row is a human's doing* in the fourth, and *the
        episode that announcement belonged to ended* in the fifth -- where
        nobody cleared anything, the write was refused, the feed came back and
        then died again. Both announce, which is what a returning fault needs
        either way, but only one of them involved a human. A reader who takes
        the fourth row as the only reading of an empty :attr:`_announced` goes
        looking for a resume that never happened, and
        ``test_a_fault_that_ends_clears_an_announcement_the_row_never_took``
        is what pins the fifth.

        **A third history reaches the fourth row's shape, and it is not a
        resume either.** If the write is refused and a human then halts
        deliberately -- ``POST /api/engine/halt`` succeeding where ours did
        not -- :meth:`_halt_is_recorded` sees an explained halt in force and
        clears :attr:`_announced` against *their* record rather than ours.
        Our halt is then permanently unrecorded, because :meth:`_retry_persist`
        does nothing once :attr:`_announced` is empty. When that human resumes
        into a feed that is still dead, the gate announces, correctly -- but
        no ``engine_halt_persisted_late`` was ever written and the row's reason
        was never ours, so the only surviving record of that first fault is the
        ``engine_halt_ongoing`` line. Do not read the fourth row as promising
        that our halt reached the row; read it as the gate having nothing left
        in memory, which three different histories can produce.

        Row first: ``engine_state`` is the only source of truth about whether
        this engine is halted, and it is the row ``POST /api/engine/resume``
        clears. Memory second, and only for a halt that never reached that row
        -- see :attr:`_announced`, which is set only when the write failed for
        exactly this reason. A database that goes unreadable *after* a halt it
        did record costs one further alert and then goes quiet: the process
        can no longer see the record and has just failed to write one, and one
        alert in that state is the right price.

        **Third row, second half: the write is retried, every suppressed tick
        the row does not carry our halt.** Memory suppressing an announcement
        is a stopgap for a missing record, not a substitute for one, and it
        has to be able to end. :meth:`_retry_persist` is how it ends when the
        database comes back -- the halt finally lands, the row polices it from
        then on, and a resume is visible to this gate again. Until it lands
        nothing changes and nothing is announced, so the storm case is
        untouched. The healthy branch above is how it ends when the *fault*
        goes instead: an announcement belongs to one fault episode, and the
        episode being over is the end of it.

        The consequence is worth stating twice because it looks like a bug and
        is not: a human who resumes while the fault is still present is
        re-halted on the next tick, with a fresh notification. That is the
        switch working, and suppressing it would leave the operator believing
        the resume took.
        """
        decision = self._watchdog.evaluate(self._now())
        if decision is None:
            # The fault ended, which ends the episode any announcement
            # belonged to. Clearing here is what bounds `_announced` to one
            # episode: without it, a single refused write silences not only
            # the rest of *this* fault -- which is correct -- but every fault
            # afterwards for the life of the process, which is the switch
            # disarmed. `_recorded` is deliberately untouched: it describes
            # the row, not an episode, and `_halt_is_recorded` maintains it.
            self._announced = None
            self._announced_correlation_id = None
            self._announced_at = None
            return None
        recorded = self._halt_is_recorded()
        if recorded or self._announced is not None:
            if not recorded and self._retry_persist(decision):
                recorded = True
            self._log_ongoing(decision, recorded=recorded)
            return None
        self.halt(decision)
        return decision

    def _log_ongoing(self, decision: HaltDecision, *, recorded: bool) -> None:
        """One line for a fault that is real but not news. The level says why.

        DEBUG while the record already describes *this rule*: an engine halted
        for a silent feed, with the feed still silent, is the expected steady
        state, and repeating that every five seconds at a visible level is how
        a log stops being read.

        **WARNING when it does not**, which is rule 8's point. A human halt
        reading ``adjusting limits`` masks a feed that died underneath it. No
        entry can be opened either way, and the next tick after a resume halts
        for the real fault and notifies, so nobody can resume into a dead feed
        unnoticed -- but for the duration of that manual halt ``engine_state``
        states a reason that is no longer the whole truth, and at DEBUG a
        post-incident reader asking *"when did the feed die?"* has nothing at
        all. The same line covers a halt no row took: an announcement with no
        record is a gap in the record by definition.

        The comparison is against :attr:`_recorded`, which
        :meth:`_halt_is_recorded` holds only while the row still carries the
        halt *this* process wrote -- matched by its text, never by parsing
        prose into a rule. So an unknown rule reads as masked, which is the
        right way round: a record this process cannot vouch for is a record
        that may not describe the fault.
        """
        masked = self._recorded is not decision.rule
        logger.log(
            logging.WARNING if masked else logging.DEBUG,
            (
                "the watchdog reports %s, and the halt in force does not "
                "describe it"
                if masked
                else "the watchdog still reports %s and a halt is already "
                "recorded"
            ),
            decision.rule.value,
            extra={
                "event": "engine_halt_ongoing",
                "rule": decision.rule.value,
                "reason": decision.reason,
                # What the record says the halt is, as far as this process can
                # tell: the rule it wrote and the row still carries, or None
                # for a halt somebody else explained or nobody recorded.
                "recorded_rule": (
                    self._recorded.value if self._recorded is not None else None
                ),
                "halt_is_recorded": recorded,
                "masked": masked,
                "policy": (
                    "an ongoing fault is not announced again while a halt is "
                    "in force; a record that does not describe the fault is "
                    "logged at WARNING, because the record is what rule 8 is "
                    "about"
                ),
                "at": decision.at.isoformat(),
            },
        )

    def _halt_is_recorded(self) -> bool:
        """Is a halt **with a stated reason** already in force? One source of truth.

        Not a cache and not a latch: read on every tick that has something to
        report, because that row is what ``POST /api/engine/resume`` writes,
        and a resume this process never hears about is exactly the case that
        used to disarm the switch for good.

        **``halted`` alone is the wrong question, and getting it wrong is
        silent.** A cold start is halted by the ``engine_state`` default with
        no reason and no timestamp -- rule 5's Paper-and-halted boot, not a
        fault -- so a gate reading ``halted`` by itself swallows the *first*
        fault after every boot, never records why, and never alerts. The
        question is whether a halt that somebody already explained is in
        force: this switch writes ``halted_reason`` when it fires, ``POST
        /api/engine/halt`` writes it when a human halts, and
        ``POST /api/engine/resume`` clears it. An explained halt is one nobody
        needs told about twice; an unexplained one is a boot, and a fault
        during it is news.

        **This answers about the row and nothing else**, including when the
        row cannot be read at all: an unreadable database records nothing this
        process can see, so the answer is ``False``. It does not follow that
        the fault is then announced -- :meth:`check_watchdog`'s gate consults
        :attr:`_announced` in every branch, and a halt that failed to persist
        left its mark there. That keeps the old, worse answer exactly where it
        is the right one: with no readable row there is nothing to diverge
        from, the halt could not have been persisted either, and repeating a
        critical alert into Discord every five seconds for a fault the human
        already has is its own failure.

        It also maintains the two memory fields, because this is the only
        place a read happens and they are only meaningful against one. A row
        carrying an explained halt makes :attr:`_announced` redundant -- the
        row is what a resume will change, so clear it, or one failed write
        would suppress every announcement for the life of the process. A row
        carrying none makes :attr:`_recorded` false: whatever this process
        wrote is gone, or never landed.

        **How a text reason is matched to a rule: it is not.** ``halted_reason``
        is prose -- this switch writes a sentence, a human writes whatever they
        like -- so nothing here parses it into a :class:`HaltRule`, and nothing
        should. What is compared is the row's reason against
        :attr:`_recorded_reason`, the exact text this process wrote when its
        own halt landed. Equal means the row is still the halt this process
        recorded, and :attr:`_recorded` names its rule. Anything else -- a
        human's halt, a newer reason written over ours by ``POST
        /api/engine/halt``, a row somebody restored -- clears
        :attr:`_recorded`, and :meth:`_log_ongoing` then reports the rule as
        unknown rather than inventing one from the prose.
        """
        # Deferred for the same reason `start` defers `mark_started`.
        from corollary.api.routes.engine import engine_state

        try:
            with self._session_factory() as session:
                state = engine_state(session)
                recorded_reason = state.halted_reason
                in_force = bool(state.halted) and recorded_reason is not None
        except Exception:
            logger.exception(
                "could not read engine_state to see whether the engine is "
                "already halted; the gate falls back to this process's own "
                "record",
                extra={
                    "event": "engine_halt_state_unreadable",
                    "policy": (
                        "an unreadable halt state records nothing; the gate "
                        "falls back to what this process announced and could "
                        "not record, which dies with the process"
                    ),
                    "announced": (
                        self._announced.value if self._announced is not None else None
                    ),
                },
            )
            return False
        if in_force:
            self._announced = None
            self._announced_correlation_id = None
            self._announced_at = None
            if recorded_reason != self._recorded_reason:
                self._recorded = None
                self._recorded_reason = None
        else:
            self._recorded = None
            self._recorded_reason = None
        return in_force

    def halt(self, decision: HaltDecision) -> None:
        """Persist the halt, record it, and raise the alarm.

        Rule 7: this stops new entries. Nothing is closed -- a flatten is a
        different consequence and stays a different control. Rule 8: the rule,
        the inputs and the timestamp all go into the record.

        The three steps are deliberately independent. A database that cannot
        take the write must not swallow the alert about the fault it is part
        of, so the notification is emitted whether or not the persistence
        succeeded, and the log line says which happened.
        """
        correlation_id = self._correlation_ids()
        channels = self._channels_for(HALT_EVENT)
        persisted = self._persist(decision)

        logger.warning(
            "engine halted (%s): %s",
            decision.rule.value,
            decision.reason,
            extra={
                "event": "engine_halted",
                # ``rule`` carries the enum, here and everywhere in this file:
                # a value you can count, filter and alert on. Prose lives
                # under ``policy``.
                #
                # **Mismatch worth someone's attention.**
                # ``api/routes/engine.py`` emits this same ``engine_halted``
                # event with a *sentence* in ``rule``, so a query filtering on
                # ``event=engine_halted`` and grouping by ``rule`` gets enums
                # for automatic halts and prose for manual ones. That file is
                # not this step's to edit; whoever owns it should move its
                # sentence under ``policy`` too.
                "rule": decision.rule.value,
                "reason": decision.reason,
                "inputs": dict(decision.inputs),
                "persisted": persisted,
                "channels": list(channels),
                "at": decision.at.isoformat(),
                "correlation_id": correlation_id,
            },
        )
        self._emit(decision, channels, correlation_id)
        # Consulted by `check_watchdog`'s gate, and never as the halt state:
        # it says the record is missing, not that the engine is running.
        #
        # This is the only place it is *set*, and the only place that can be.
        # Clearing it is somebody else's job in every case -- the row taking a
        # halt, `_retry_persist` landing this one, or the fault ending -- for
        # the plain reason that the gate stops `halt` being reached again
        # while it is set, so a line here could never run to undo it.
        #
        # **Set only when the write failed**, which is what makes the fallback
        # safe to consult. Armed after a *successful* halt it would outlive the
        # halt itself: `POST /api/engine/resume` clears the row, the next read
        # correctly reports no halt in force, and this flag would then suppress
        # the re-halt -- leaving the operator believing the resume took while
        # the fault ran on. That is the blind spot this whole gate exists to
        # close, so it must not be re-opened by the gate's own fallback. When
        # the write succeeded the row is the record, and memory is redundant.
        self._announced = None if persisted else decision.rule
        # Its provenance, for the record `_retry_persist` may have to write on
        # its behalf later: that record describes *this* halt, from a
        # timestamp that can be half an hour away, so it carries this id.
        self._announced_correlation_id = None if persisted else correlation_id
        self._announced_at = None if persisted else decision.at
        # What the record now says, for `_log_ongoing` to compare the next
        # tick's fault against: the rule, and the exact sentence written
        # beside it. Both cleared when the write was refused -- there is no
        # record then, and an ongoing fault with no record of it is a gap in
        # the record whatever rule caused it.
        self._recorded = decision.rule if persisted else None
        self._recorded_reason = _bounded(decision.reason) if persisted else None

    def _retry_persist(self, decision: HaltDecision) -> bool:
        """Write a halt this process announced and the database refused. Again.

        Returns ``True`` when the row now carries it, which is also the tick
        on which :attr:`_announced` is handed back to the record.

        Called from the suppressed branch of :meth:`check_watchdog`, and only
        when the row does **not** carry our halt -- so the state on entry is
        always the same one: this process announced a halt, ``_persist``
        refused it, and the fault is still here. Two things follow from that,
        and both matter.

        **The record is missing, and a missing record is what gets written.**
        Rule 8 says a halt records the rule, the inputs and the timestamp;
        right now ``engine_state`` says nothing of the sort, and nothing else
        in this class will ever fix that -- :meth:`halt` cannot run again,
        because the gate that sent us here is what stops it. Left alone the
        row stays permanently wrong about a halt that really was announced,
        and ``GET /api/engine/state`` reports an engine that is running.

        **It is also what re-arms the switch.** :attr:`_announced` has no
        other way back: :meth:`halt` cannot clear it, and
        :meth:`_halt_is_recorded` clears it only on a row carrying an
        explained halt, which is exactly what the refused write prevented. So
        a single ``database is locked`` used to suppress every announcement
        for the life of the process -- including the next genuinely new fault,
        after the database recovered and after a human resumed. Once this
        write lands the row is the record again, a resume is visible to the
        gate the ordinary way, and the switch works.

        **A retry that fails changes nothing and announces nothing.** That is
        the storm case and it stays exactly as it was: still suppressed, still
        one alert for the episode. The cost is that ``_persist`` logs its
        failure once per tick rather than once per halt, which is a log line
        and not an alert -- the thing that must not repeat is the
        notification, and it does not.

        This is deliberately not an announcement either way. A row carrying no
        explained halt cannot distinguish *a human resumed* from *our write
        never landed* -- that ambiguity is the whole reason
        :attr:`_announced` exists -- so on this tick the honest reading is the
        one we have evidence for: our record is missing. The operator already
        has the alert for this fault. The *next* resume is a real observation
        and announces the ordinary way.
        """
        if self._announced is None:
            return False
        if not self._persist(decision):
            return False
        # Read out before they are cleared: the record below is the
        # announcement's, not this tick's, and its id is the only thing
        # joining the two.
        announced_correlation_id = self._announced_correlation_id
        announced_at = self._announced_at
        # Exactly what a successful `halt` sets, and for the same reasons:
        # the row is the record now, so memory of an unrecorded announcement
        # is wrong to keep, and `_log_ongoing` has a rule and a sentence to
        # compare the next tick's fault against.
        self._announced = None
        self._announced_correlation_id = None
        self._announced_at = None
        self._recorded = decision.rule
        self._recorded_reason = _bounded(decision.reason)
        logger.warning(
            "the halt for %s is now recorded; the database had refused it",
            decision.rule.value,
            extra={
                "event": "engine_halt_persisted_late",
                "rule": decision.rule.value,
                "reason": decision.reason,
                "at": decision.at.isoformat(),
                # The announcing halt's id, so this record, its `engine_halted`
                # line and its critical notification join up -- and the moment
                # it was announced, so the gap to `at` above is arithmetic
                # rather than prose.
                #
                # Not necessarily the same *rule*, and deliberately so. The
                # watchdog latches nothing and checks its conditions in order,
                # so a silent feed can be announced and the socket can then
                # formally close before the write lands; the row and this line
                # take the rule evaluated now, while the id and `announced_at`
                # come from the announcement. That is the right way round: the
                # announcing id is the only alert the operator actually
                # received for the episode, since everything after it was
                # suppressed, so joining to it beats joining to nothing. It
                # does mean `at` minus `announced_at` can measure the earlier
                # rule's outage rather than this one's. `at` and
                # the row's `halted_at` both stay the moment the write landed:
                # they are written from one decision alongside a reason
                # measured at that moment, and a timestamp that disagrees with
                # the sentence beside it is worse than one that is late.
                "correlation_id": announced_correlation_id,
                "announced_at": (
                    announced_at.isoformat() if announced_at is not None else None
                ),
                "policy": (
                    "a halt this process announced but could not write is "
                    "retried on every suppressed tick; until it lands the "
                    "record does not describe the halt, and the switch "
                    "cannot see a resume"
                ),
            },
        )
        return True

    def _persist(self, decision: HaltDecision) -> bool:
        """Write the halt to ``engine_state``. False when the database refused.

        Sets ``halted`` true and nothing else touches it. There is no branch
        in this method that could set it the other way, which is the point.

        **Catches every exception, not only the database's**, and the breadth
        is the requirement rather than laziness. :meth:`halt` promises that
        persisting, logging and notifying are independent; anything escaping
        this method breaks that promise at the worst possible moment, by
        swallowing the alert about the fault it is part of. The deferred
        import sits inside the ``try`` for exactly that reason -- an
        ``ImportError`` here is no more entitled to silence the switch than a
        locked SQLite file is. ``KeyboardInterrupt`` and ``SystemExit`` derive
        from ``BaseException`` and still propagate, which is right: those are
        the process being told to stop, not the halt path failing.
        """
        try:
            # Deferred for the same reason `start` defers `mark_started`.
            from corollary.api.routes.engine import engine_state

            with self._session_factory() as session:
                state = engine_state(session)
                state.halted = True
                state.halted_reason = _bounded(decision.reason)
                state.halted_at = decision.at
                session.commit()
            return True
        except Exception:
            logger.exception(
                "could not persist the halt; the notification is sent anyway",
                extra={
                    "event": "engine_halt_not_persisted",
                    "policy": (
                        "persisting, logging and notifying are independent; "
                        "nothing failing here may swallow the alert"
                    ),
                    "rule": decision.rule.value,
                    "at": decision.at.isoformat(),
                },
            )
            return False

    def _channels_for(self, event: str) -> tuple[str, ...]:
        """Which channels this event is routed to, in ``NOTIFICATION_CHANNELS`` order.

        Read from ``notification_route``, which a human configures in Settings.
        **A database failure falls back to every channel**, not to none: the
        failure mode of an extra alert is an annoyed reader, and the failure
        mode of a missing one is a halted engine nobody hears about.

        Catches every exception for the same reason :meth:`_persist` does: it
        runs inside :meth:`halt`, and anything that escapes it takes the
        notification with it.
        """
        try:
            with self._session_factory() as session:
                enabled = set(
                    session.scalars(
                        select(NotificationRoute.channel).where(
                            NotificationRoute.event == event,
                            NotificationRoute.enabled.is_(True),
                        )
                    )
                )
        except Exception:
            logger.exception(
                "could not read notification routing; alerting every channel",
                extra={
                    "event": "engine_notification_routing_unavailable",
                    "policy": (
                        "an unreadable route falls back to every channel -- a "
                        "missing critical alert is the worse failure"
                    ),
                    "notification_event": event,
                },
            )
            return tuple(NOTIFICATION_CHANNELS)
        return tuple(channel for channel in NOTIFICATION_CHANNELS if channel in enabled)

    def _emit(
        self, decision: HaltDecision, channels: tuple[str, ...], correlation_id: str
    ) -> None:
        notification = Notification(
            event=HALT_EVENT,
            severity=HALT_SEVERITY,
            title="Engine halted",
            body=decision.reason,
            at=decision.at,
            correlation_id=correlation_id,
            channels=channels,
        )
        if not channels:
            logger.warning(
                "the engine halted and every notification channel is switched off",
                extra={
                    "event": "engine_halt_unrouted",
                    "policy": (
                        "silencing a critical event is permitted and is logged, "
                        "so a halt cannot be both silent and unrecorded"
                    ),
                    "notification_event": HALT_EVENT,
                    "at": decision.at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
        elif "discord" in channels and not (
            self._env.get(DISCORD_WEBHOOK_ENV) or ""
        ).strip():
            logger.warning(
                "a critical halt is routed to Discord and %s is not set",
                DISCORD_WEBHOOK_ENV,
                extra={
                    "event": "engine_halt_discord_unconfigured",
                    "policy": (
                        "presence only -- rule 6, the webhook value never "
                        "reaches a log or a response"
                    ),
                    "notification_event": HALT_EVENT,
                    "at": decision.at.isoformat(),
                    "correlation_id": correlation_id,
                },
            )
        self._notifier.emit(notification)
