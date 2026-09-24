"""The owner's actions, told to the bell and Discord.

The owner, 2026-09-24: *"put it on the bell and discord, any action i do
should be put into the discord."* Five routes change state, and each emits one
notification after its commit:

========================================  ============================
event                                     emitted by
========================================  ============================
``operator_halt``                         ``POST /api/engine/halt``
``operator_resume``                       ``POST /api/engine/resume``
``risk_limits_changed``                   ``PUT /api/settings/limits``
``data_feeds_changed``                    ``PUT /api/settings/feeds``
``notification_routes_changed``           ``PUT /api/settings/routes``
========================================  ============================

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
from enum import StrEnum
from typing import Final, NamedTuple, Protocol
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Request

from corollary.engine.runtime import OperatorNotice

__all__ = [
    "AuditChange",
    "OperatorEvent",
    "deliver_notice",
    "halt_notice",
    "notify_after_response",
    "resume_notice",
    "settings_notice",
]

logger = logging.getLogger(__name__)

EASTERN: Final = ZoneInfo("America/New_York")


class OperatorEvent(StrEnum):
    """The five events an owner's action raises. The web codes against these."""

    OPERATOR_HALT = "operator_halt"
    OPERATOR_RESUME = "operator_resume"
    RISK_LIMITS_CHANGED = "risk_limits_changed"
    DATA_FEEDS_CHANGED = "data_feeds_changed"
    NOTIFICATION_ROUTES_CHANGED = "notification_routes_changed"


#: A halt is worth a glance; everything else is a record.
_SEVERITY: Final[dict[OperatorEvent, str]] = {
    OperatorEvent.OPERATOR_HALT: "warning",
    OperatorEvent.OPERATOR_RESUME: "info",
    OperatorEvent.RISK_LIMITS_CHANGED: "info",
    OperatorEvent.DATA_FEEDS_CHANGED: "info",
    OperatorEvent.NOTIFICATION_ROUTES_CHANGED: "info",
}

_SETTINGS_TITLE: Final[dict[OperatorEvent, str]] = {
    OperatorEvent.RISK_LIMITS_CHANGED: "Risk limits changed",
    OperatorEvent.DATA_FEEDS_CHANGED: "Data feeds changed",
    OperatorEvent.NOTIFICATION_ROUTES_CHANGED: "Notification routing changed",
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
    """``operator_resume``: which halt ended, or that there was none."""
    if was_halted:
        began = _eastern(previous_at) if previous_at else "an unrecorded time"
        body = (
            f"Ended the halt that began {began}.\n"
            f"Halt reason: {previous_reason or 'none recorded'}"
        )
    else:
        body = "The engine was not halted; the resume changed nothing."
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
