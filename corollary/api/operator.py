"""The owner's actions, told to the bell and Discord.

The owner, 2026-09-24: *"put it on the bell and discord, any action i do
should be put into the discord."* Ten routes change state, and each emits one
notification after its commit:

========================================  ====================================
event                                     emitted by
========================================  ====================================
``operator_halt``                         ``POST /api/engine/halt``
``operator_resume``                       ``POST /api/engine/resume``
``risk_limits_changed``                   ``PUT /api/settings/limits``
``data_feeds_changed``                    ``PUT /api/settings/feeds``
``notification_routes_changed``           ``PUT /api/settings/routes``
``watchlist_changed``                     ``POST``/``DELETE /api/news/watch/{t}``
``sentiment_audit_requested``             ``POST /api/settings/sentiment/audit``
``calendar_changed``                      ``POST``/``PUT``/``DELETE
                                          /api/calendar/manual``
``cash_session_confirmed``                ``POST /api/account/cash-session``
``cash_session_ended``                    ``DELETE /api/account/cash-session``
========================================  ====================================

``cash_session_confirmed`` and ``cash_session_ended`` are rule 5's Cash
session (``corollary.engine.cash_session``): ``info``, **bell on and Discord
on** -- which book the engine may read is worth the bell. Built from the
``audit_log`` row the route wrote and the server's own read of the account,
never the request body; the account appears masked (``••••1234``) only.
An end that ended nothing writes no row and says nothing.

``watchlist_changed`` is Phase 3 decision 21's, on decision 20's terms for the
config events: ``info``, built from the one ``audit_log`` row the route wrote.
``sentiment_audit_requested`` is step 6's manual audit trigger, on the same
terms (decision 20: an operator action notifies once it has an endpoint). It
writes no ``audit_log`` row -- the ``audit_run`` row is its record -- so its
words are the request instant and nothing else.

``calendar_changed`` is a manual geopolitical calendar entry added, edited or
removed (Phase 3 decision 9). Decision 20 is *"every operator action
notifies"*, and Q8's reading of the owner's words is that *"every other
state-changing request goes to Discord, with the bell off by default"*; the
only exclusions are bell reads. So it notifies on the info terms -- but
decision 9 keeps it **out of** the audit log, so it is built from the stored
row (old and new values read back from ``calendar_event``), not from an audit
row. Still stored values, never the request body.

Reading or dismissing a bell entry is not in the table and must never be: it
would page Discord for reading Discord, and loop.

**One path.** This module builds the words and hands an
:class:`~corollary.engine.runtime.OperatorNotice` to
``EngineRuntime.notify_operator_action``, which routes it with the same
``_channels_for`` and delivers it through the same fan-out as a rule-9 halt.
There is no notifier and no routing gate here (decision 14).

**The action always wins.** The notice is scheduled as a response background
task, so it runs after the response has been sent -- a locked database or a
slow sink cannot delay the route, and Discord only enqueues regardless.
:func:`deliver_notice` catches everything, and a missing runtime is a log
line, never an error. Callers schedule it only *after* ``session.commit()``
succeeds, so an action that rolled back is never announced.

**The settings words come from the audit values.** ``settings._audit`` returns
an :class:`AuditChange` carrying exactly what it wrote to ``audit_log``, and
the notification is built from those -- never from the request body -- so the
audit log and Discord cannot disagree about what changed (rule 4: stored
values, not client input). Nothing here reads the environment, so no key,
secret or webhook URL can reach a message (rule 6).
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, NamedTuple, Protocol
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Request

from corollary.engine.runtime import OperatorNotice

__all__ = [
    "AuditChange",
    "NoticeSink",
    "OperatorEvent",
    "deliver_notice",
    "halt_notice",
    "notify_after_response",
    "calendar_notice",
    "cash_session_confirmed_notice",
    "cash_session_ended_notice",
    "resume_notice",
    "sentiment_audit_notice",
    "settings_notice",
]

logger = logging.getLogger(__name__)

EASTERN: Final = ZoneInfo("America/New_York")


class OperatorEvent(StrEnum):
    """The events an owner's action raises. The web codes against these."""

    OPERATOR_HALT = "operator_halt"
    OPERATOR_RESUME = "operator_resume"
    RISK_LIMITS_CHANGED = "risk_limits_changed"
    DATA_FEEDS_CHANGED = "data_feeds_changed"
    NOTIFICATION_ROUTES_CHANGED = "notification_routes_changed"
    #: A manual news watch added or removed (Phase 3 decision 21).
    WATCHLIST_CHANGED = "watchlist_changed"
    #: A sentiment self-audit run started by hand (Phase 3 step 6).
    SENTIMENT_AUDIT_REQUESTED = "sentiment_audit_requested"
    #: A manual calendar entry added, edited or removed (decisions 9 and 20).
    CALENDAR_CHANGED = "calendar_changed"
    #: Rule 5's Cash session confirmed, or ended, by the owner.
    CASH_SESSION_CONFIRMED = "cash_session_confirmed"
    CASH_SESSION_ENDED = "cash_session_ended"


#: A halt is worth a glance; everything else is a record.
_SEVERITY: Final[dict[OperatorEvent, str]] = {
    OperatorEvent.OPERATOR_HALT: "warning",
    OperatorEvent.OPERATOR_RESUME: "info",
    OperatorEvent.RISK_LIMITS_CHANGED: "info",
    OperatorEvent.DATA_FEEDS_CHANGED: "info",
    OperatorEvent.NOTIFICATION_ROUTES_CHANGED: "info",
    OperatorEvent.WATCHLIST_CHANGED: "info",
    OperatorEvent.SENTIMENT_AUDIT_REQUESTED: "info",
    OperatorEvent.CALENDAR_CHANGED: "info",
    OperatorEvent.CASH_SESSION_CONFIRMED: "info",
    OperatorEvent.CASH_SESSION_ENDED: "info",
}

_SETTINGS_TITLE: Final[dict[OperatorEvent, str]] = {
    OperatorEvent.RISK_LIMITS_CHANGED: "Risk limits changed",
    OperatorEvent.DATA_FEEDS_CHANGED: "Data feeds changed",
    OperatorEvent.NOTIFICATION_ROUTES_CHANGED: "Notification routing changed",
    OperatorEvent.WATCHLIST_CHANGED: "News watchlist changed",
}


class AuditChange(NamedTuple):
    """One ``audit_log`` row, as ``settings._audit`` wrote it."""

    category: str
    field: str
    previous: str
    new: str


class NoticeSink(Protocol):
    """The one runtime method this module calls. ``EngineRuntime`` has it."""

    def notify_operator_action(self, notice: OperatorNotice) -> object: ...


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------


def _eastern(moment: datetime) -> str:
    """``2026-09-24 10:31:05 EDT`` -- stored UTC, displayed in New York."""
    return moment.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")


def change_line(change: AuditChange) -> str:
    """``max_daily_loss_pct: 20 → 15``, or ``engine_error / discord: on → off``.

    A routing cell's audit field is ``event.channel``; it reads as
    ``event / channel`` in a message, and says nothing the audit row does not.
    """
    field = change.field
    if change.category == "notification":
        event, _, channel = field.rpartition(".")
        if event:
            field = f"{event} / {channel}"
    return f"{field}: {change.previous} → {change.new}"


def halt_notice(
    *,
    reason: str,
    was_halted: bool,
    previous_reason: str | None,
    previous_at: datetime | None,
    at: datetime,
    correlation_id: str,
) -> OperatorNotice:
    """``operator_halt``: the stored reason, and the reason it replaced, if any.

    "Replaced" means the route's already-halted path: an engine halted *with a
    stated reason*. A halt on top of the cold-start halt, which has no reason,
    replaced nothing and says nothing extra.
    """
    body = reason
    if was_halted and previous_reason is not None:
        since = f" (halted since {_eastern(previous_at)})" if previous_at else ""
        body += f"\n\nThis replaced the earlier halt reason: {previous_reason}{since}."
    return OperatorNotice(
        event=OperatorEvent.OPERATOR_HALT.value,
        severity=_SEVERITY[OperatorEvent.OPERATOR_HALT],
        title="Engine halted by operator",
        body=body,
        at=at,
        correlation_id=correlation_id,
    )


def resume_notice(
    *,
    was_halted: bool,
    previous_reason: str | None,
    previous_at: datetime | None,
    at: datetime,
    correlation_id: str,
) -> OperatorNotice:
    """``operator_resume``: which halt ended, or that there was none.

    Four halted cases, written out rather than glued from fragments. The
    neither-recorded case is the cold-start halt: ``engine_state`` is created
    ``halted=True`` with no time and no reason (``db.seed``,
    ``engine.state``). An empty reason counts as no reason.
    """
    has_reason = bool(previous_reason)
    if not was_halted:
        body = "The engine was not halted; the resume changed nothing."
    elif previous_at is not None and has_reason:
        body = (
            f"Ended the halt that began {_eastern(previous_at)}.\n"
            f"Halt reason: {previous_reason}"
        )
    elif previous_at is None and not has_reason:
        body = "Ended the cold-start halt (no time or reason is recorded for it)."
    elif previous_at is None:
        body = (
            "Ended the halt (its start time is not recorded).\n"
            f"Halt reason: {previous_reason}"
        )
    else:
        body = (
            f"Ended the halt that began {_eastern(previous_at)}.\n"
            "No halt reason is recorded."
        )
    return OperatorNotice(
        event=OperatorEvent.OPERATOR_RESUME.value,
        severity=_SEVERITY[OperatorEvent.OPERATOR_RESUME],
        title="Engine resumed by operator",
        body=body,
        at=at,
        correlation_id=correlation_id,
    )


def settings_notice(
    event: OperatorEvent,
    changes: Sequence[AuditChange],
    *,
    at: datetime,
    correlation_id: str,
) -> OperatorNotice | None:
    """One notice listing every change a request made, or ``None`` for none.

    A request that wrote no audit row changed nothing, and says nothing.
    """
    if not changes:
        return None
    return OperatorNotice(
        event=event.value,
        severity=_SEVERITY[event],
        title=_SETTINGS_TITLE[event],
        body="\n".join(change_line(change) for change in changes),
        at=at,
        correlation_id=correlation_id,
    )


def sentiment_audit_notice(*, at: datetime, correlation_id: str) -> OperatorNotice:
    """``sentiment_audit_requested``: a manual run started, and where its result lands."""
    event = OperatorEvent.SENTIMENT_AUDIT_REQUESTED
    return OperatorNotice(
        event=event.value,
        severity=_SEVERITY[event],
        title="Sentiment audit started by operator",
        body=(
            f"A manual sentiment audit run started at {_eastern(at)}. It grades "
            "every matured label and may demote or re-promote a source; its "
            "result is on Settings."
        ),
        at=at,
        correlation_id=correlation_id,
    )


def calendar_notice(
    action: str, lines: Sequence[str], *, at: datetime, correlation_id: str
) -> OperatorNotice | None:
    """``calendar_changed``: one notice per request, ``None`` if it changed nothing.

    ``lines`` are the route's renderings of the stored row (the calendar
    route builds them from ``calendar_event`` as read back, never from the
    request body). ``action`` is ``added``, ``edited`` or ``removed``.
    """
    if not lines:
        return None
    return OperatorNotice(
        event=OperatorEvent.CALENDAR_CHANGED.value,
        severity=_SEVERITY[OperatorEvent.CALENDAR_CHANGED],
        title=f"Calendar entry {action}",
        body="\n".join(lines),
        at=at,
        correlation_id=correlation_id,
    )


def cash_session_confirmed_notice(
    *,
    account_number_masked: str,
    balance: Decimal,
    replaced: bool,
    at: datetime,
    correlation_id: str,
) -> OperatorNotice:
    """``cash_session_confirmed``: which account, its balance as read, and when.

    ``account_number_masked`` and ``balance`` are the server's read of the
    live account, never the request body. ``replaced`` says a session was
    already active and this confirmation restarted it.
    """
    event = OperatorEvent.CASH_SESSION_CONFIRMED
    body = (
        f"Cash session confirmed for live account {account_number_masked} at "
        f"{_eastern(at)}. Equity as the server read it: {balance} USD."
    )
    if replaced:
        body += "\nThis replaced a Cash session that was already active."
    body += (
        "\nThe session ends on an explicit end or on any restart; it is "
        "never restored."
    )
    return OperatorNotice(
        event=event.value,
        severity=_SEVERITY[event],
        title="Cash session confirmed",
        body=body,
        at=at,
        correlation_id=correlation_id,
    )


def cash_session_ended_notice(
    *,
    account_number_masked: str,
    confirmed_at: datetime,
    at: datetime,
    correlation_id: str,
) -> OperatorNotice:
    """``cash_session_ended``: which session ended, and when it had begun."""
    event = OperatorEvent.CASH_SESSION_ENDED
    return OperatorNotice(
        event=event.value,
        severity=_SEVERITY[event],
        title="Cash session ended",
        body=(
            f"Ended the Cash session for live account {account_number_masked} "
            f"at {_eastern(at)}; it was confirmed {_eastern(confirmed_at)}. "
            "Nothing reads Cash until it is confirmed again."
        ),
        at=at,
        correlation_id=correlation_id,
    )


# --------------------------------------------------------------------------
# Delivery -- after the response, and never into it
# --------------------------------------------------------------------------


def notify_after_response(
    request: Request, background: BackgroundTasks, notice: OperatorNotice | None
) -> None:
    """Schedule ``notice`` to be emitted once the response has been sent.

    Call only after the action's ``session.commit()`` has succeeded.
    """
    if notice is None:
        return
    runtime: NoticeSink | None = getattr(request.app.state, "engine_runtime", None)
    background.add_task(deliver_notice, runtime, notice)


def deliver_notice(runtime: NoticeSink | None, notice: OperatorNotice) -> None:
    """Hand ``notice`` to the runtime. Never raises."""
    if runtime is None:
        logger.warning(
            "no engine runtime is running; the action stands and no "
            "notification was emitted",
            extra={
                "event": "operator_notice_not_emitted",
                "policy": (
                    "a notification never fails an action; an app with no "
                    "runtime says so rather than raising"
                ),
                "notification_event": notice.event,
                "title": notice.title,
                "at": notice.at.isoformat(),
                "correlation_id": notice.correlation_id,
            },
        )
        return
    try:
        runtime.notify_operator_action(notice)
    except Exception as exc:
        # Class name only: an exception from a delivery path is not ours to
        # vouch for, and the Discord sink's would carry its URL (rule 6).
        logger.error(
            "notifying an operator action failed; the action stands",
            extra={
                "event": "operator_notice_failed",
                "policy": (
                    "a notification never delays, blocks or fails the action "
                    "it describes"
                ),
                "error_type": type(exc).__name__,
                "notification_event": notice.event,
                "at": notice.at.isoformat(),
                "correlation_id": notice.correlation_id,
            },
        )
