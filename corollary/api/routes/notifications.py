"""The bell: ``GET /api/notifications``, and the two explicit actions on one entry.

Phase 3 decision 14. Polled at 15s by the frontend -- the frame contract of
2026-09-13 keeps notifications off the socket, because rule 9 halts *because*
the socket died, and an alert carried by the thing that failed is not an alert.

**Account-scoped, per the bell's rules.** ``?account=`` resolves exactly as
every other account-scoped route (paper unless told otherwise, rule 5). A book
sees its own rows **plus** the rows with ``account IS NULL`` -- engine and
audit events, which belong to no book and show in both -- and never the other
book's. The same scope guards read and dismiss: an id from the other book is a
404 here, as though it did not exist, because from this book it does not.

**No broker dependency, deliberately unlike ``/api/activity``.** That route
depends on the broker for the 409 a cash request without live keys earns;
here that 409 would hide a halt alert behind a missing credential, which is
backwards -- the engine's own rows need no vendor to read.

**The routing gate stays at emit time.** What the bell shows is what the bell
*received*: a ``notification_delivery`` row with ``channel = 'bell'``, written
by ``engine/notify.py``'s ``DbNotifier`` when the notification was raised and
the bell was routed. This module never reads ``notification_route``, so
unchecking a route stops future alerts and erases none already received.

Read and dismiss each set their timestamp **once**: repeating either is a
no-op that answers 200, and the first time is the one kept.
"""

import logging
from datetime import datetime, timezone
from typing import Annotated, Final

from fastapi import APIRouter, Query
from pydantic import ValidationError
from sqlalchemy import ColumnElement, exists, or_, select
from sqlalchemy.orm import Session

from corollary.api.deps import AccountModeDep, ApiError, SessionDep
from corollary.api.schemas import AccountMode, NotificationItem
from corollary.db.models import NotificationDelivery, NotificationRecord

__all__ = ["MAX_NOTIFICATIONS", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

#: The most rows one GET returns. The bell is a panel, not a page (PRD
#: section 10); an unbounded list would grow with every halt forever.
MAX_NOTIFICATIONS: Final = 500
DEFAULT_NOTIFICATIONS: Final = 100


def _visible_in(mode: AccountMode) -> list[ColumnElement[bool]]:
    """The bell's filter for one book: its rows, the book-less ones, bell-received."""
    return [
        or_(
            NotificationRecord.account == mode.value,
            NotificationRecord.account.is_(None),
        ),
        exists().where(
            NotificationDelivery.notification_id == NotificationRecord.id,
            NotificationDelivery.channel == "bell",
            NotificationDelivery.status == "delivered",
        ),
    ]


def _item(row: NotificationRecord) -> NotificationItem:
    """One row as a bell item. Raises ``ValidationError`` for a row the wire
    contract cannot carry -- use :func:`_renderable` unless that is known not
    to happen.

    Validated from a mapping rather than keyword arguments because ``event``
    and ``severity`` are free strings in the table and ``Literal``s on the
    wire: the check is pydantic's, at runtime, and :func:`_renderable` is
    where its failure is handled.
    """
    return NotificationItem.model_validate(
        {
            "id": row.id,
            "time": row.at,
            "event": row.event,
            "severity": row.severity,
            "title": row.title,
            "detail": row.body,
            "account": row.account,
            "read": row.read_at is not None,
            "correlation_id": row.correlation_id,
        }
    )


def _renderable(row: NotificationRecord) -> NotificationItem | None:
    """The row as a bell item, or ``None`` -- logged at ERROR -- if it cannot be.

    ``notification.event`` has no CHECK by design, so the engine can write an
    event before ``NotificationEvent`` knows it (Phase 3 step 6 adds two).
    One such row must not take the whole bell down with a 500 -- halt alerts
    included -- so it is skipped, and the log says which row and why. Body
    text is left out of the line: the id finds it.
    """
    try:
        return _item(row)
    except ValidationError as exc:
        logger.error(
            "a notification row does not fit the bell's wire contract; skipped",
            extra={
                "event": "notification_unrenderable",
                "policy": (
                    "an unrenderable row is logged and skipped so every other "
                    "row, halt alerts included, is still served"
                ),
                "notification_id": row.id,
                "notification_event": row.event,
                "severity": row.severity,
                "account": row.account,
                "invalid_fields": sorted(
                    {".".join(str(part) for part in error["loc"]) for error in exc.errors()}
                ),
            },
        )
        return None


def _visible_row(session: Session, mode: AccountMode, ident: str) -> NotificationRecord:
    row = session.scalars(
        select(NotificationRecord).where(
            NotificationRecord.id == ident, *_visible_in(mode)
        )
    ).one_or_none()
    if row is None:
        raise ApiError(
            status_code=404,
            code="notification_not_found",
            message=(
                f"No notification {ident!r} in the {mode.value} bell. Engine "
                "events show in both books; the other book's do not."
            ),
        )
    return row


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("", summary="The bell -- this book's notifications and the engine's")
def read_notifications(
    mode: AccountModeDep,
    session: SessionDep,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_NOTIFICATIONS, description="Newest first; at most this many."),
    ] = DEFAULT_NOTIFICATIONS,
) -> list[NotificationItem]:
    rows = session.scalars(
        select(NotificationRecord)
        .where(*_visible_in(mode), NotificationRecord.dismissed_at.is_(None))
        .order_by(NotificationRecord.at.desc(), NotificationRecord.id.desc())
        .limit(limit)
    )
    # Per row, so one unrenderable row costs that row and nothing else. It
    # still counts against ``limit``: the query bounds the work, and a short
    # page beside an ERROR line is the honest answer.
    items = (_renderable(row) for row in rows)
    return [item for item in items if item is not None]


def _require_renderable(row: NotificationRecord) -> None:
    """Refuse to act on a row the bell cannot show -- before anything is written.

    The list skips such a row, so a read or dismiss aimed at one names the
    problem instead of recording a timestamp and then failing to answer.
    """
    if _renderable(row) is None:
        raise ApiError(
            status_code=500,
            code="notification_unrenderable",
            message=(
                f"Notification {row.id!r} does not fit the bell's wire contract "
                "(logged); it was left unchanged."
            ),
        )


@router.post("/{notification_id}/read", summary="Mark one notification read")
def mark_read(
    notification_id: str, mode: AccountModeDep, session: SessionDep
) -> NotificationItem:
    row = _visible_row(session, mode, notification_id)
    _require_renderable(row)
    if row.read_at is None:
        row.read_at = _now()
        session.commit()
    return _item(row)


@router.post("/{notification_id}/dismiss", summary="Dismiss one notification from the bell")
def dismiss(
    notification_id: str, mode: AccountModeDep, session: SessionDep
) -> NotificationItem:
    row = _visible_row(session, mode, notification_id)
    _require_renderable(row)
    if row.dismissed_at is None:
        row.dismissed_at = _now()
        session.commit()
    return _item(row)
