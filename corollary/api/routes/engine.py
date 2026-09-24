"""Engine state: read it, halt it, and resume it -- explicitly, or not at all.

CLAUDE.md rule 9, in full, because every line of this module is downstream of
it:

    If the engine loses its Alpaca connection, or the risk manager stops
    heartbeating for 90 seconds, the engine calls ``halt()`` on itself and
    fires a critical notification. Recovery requires an explicit human resume.

    **Never auto-resume on reconnect.** Reconnecting into an unverified
    position state is how a bot doubles a position it already holds.

So ``POST /api/engine/resume`` is the **only** thing in this codebase that
clears a halt. Not a reconnect handler, not a startup path, not a retry. The
private helper that does it is named :func:`_clear_halt` and
``tests/api/test_engine_routes.py`` greps the package to prove it has exactly
one caller -- the same structural standard ``test_no_order_path.py`` holds the
order path to. A rule a reviewer has to notice is a rule that gets lost in a
plausible-looking diff.

**Halting stops nothing in Phase 2, because nothing trades.** The state, the
record and the explicit-resume requirement are real and tested regardless.
Rule 9 is the one item in this phase that cannot be retrofitted: by the time
there is an order path, every caller that might clear a halt already exists,
and finding them is archaeology.

**There is no flatten endpoint here, and there must not be one.** Rule 7:
``halt()`` stops new entries and leaves managed exits running; ``flatten()``
closes everything and then halts. They are different consequences and they
stay different controls. There is also no execution path this phase, so a
flatten would have nothing to close and everything to imply.

The watchdog that *calls* halt, the ``notification`` row it writes, and
``EngineRuntime`` are step 8. This module is the statement of state and the
two human controls, nothing more. The singleton's persistence rules --
create-if-missing and write-``t0``-once -- live in ``corollary/engine/state.py``,
below both this module and the runtime, so each imports them rather than
restating them.
"""

import logging
import uuid
from datetime import datetime, timezone
from collections.abc import Sequence
from enum import StrEnum
from typing import Final, NamedTuple

from fastapi import APIRouter, BackgroundTasks, Request

from corollary.api.deps import SessionDep
from corollary.api.operator import halt_notice, notify_after_response, resume_notice
from corollary.api.schemas import EngineStateResponse, HaltRequest
from corollary.db.models import EngineState
from corollary.engine.state import engine_state
from corollary.wire import operator_text

__all__ = ["HALTED_REASON_WIDTH", "OperatorRule", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/engine", tags=["engine"])

#: ``engine_state.halted_reason`` is ``String(256)``. The scrubbed reason is
#: bounded to it: redaction can lengthen text (a short secret becomes the ten
#: characters of ``<redacted>``), so a reason the schema admitted at full
#: width could otherwise come out wider than the column.
#: ``tests/api/test_halt_reason_redaction.py`` pins this to the column.
HALTED_REASON_WIDTH: Final = 256


class OperatorRule(StrEnum):
    """Why this module logged a state change. One value per case, never prose.

    ``rule`` on a log record is a thing you count, filter and alert on, as it
    is in ``engine/runtime.py`` -- where the *automatic* ``engine_halted``
    carries a ``HaltRule``. The sentence explaining the policy goes under
    ``policy``. Named to match the notification events they accompany.
    """

    #: A human halted the engine through ``POST /api/engine/halt``.
    OPERATOR_HALT = "operator_halt"
    #: The same, on an engine already halted with a stated reason: the newest
    #: reason is stored and the one it replaced is logged.
    OPERATOR_HALT_REPLACED = "operator_halt_replaced"
    #: A human ended a halt -- the only way one ends (rule 9).
    OPERATOR_RESUME = "operator_resume"


class _EndedHalt(NamedTuple):
    """What a resume found, for the record of it."""

    was_halted: bool
    previous_reason: str | None
    previous_at: datetime | None


def _response(state: EngineState) -> EngineStateResponse:
    return EngineStateResponse(
        halted=state.halted,
        halted_reason=state.halted_reason,
        halted_at=state.halted_at,
        t0=state.t0,
    )


def _clear_halt(
    state: EngineState, *, at: datetime, correlation_id: str
) -> _EndedHalt:
    """End a halt. **The only code in this package that does.**

    Private, and named so a grep over ``corollary/`` reads as an assertion.
    ``tests/api/test_engine_routes.py`` pins both this name and the literal
    assignment below to this one file.

    Returns what it found, so the resume route can say which halt ended. The
    notification that says so is a record, sent after the commit; nothing it
    does can end a halt.
    """
    ended = _EndedHalt(
        was_halted=state.halted,
        previous_reason=state.halted_reason,
        previous_at=state.halted_at,
    )
    state.halted = False
    state.halted_reason = None
    state.halted_at = None
    logger.warning(
        "engine resumed by an explicit request (previous reason: %s)",
        ended.previous_reason,
        extra={
            "event": "engine_resumed",
            "rule": OperatorRule.OPERATOR_RESUME.value,
            "policy": (
                "a halt ends only at POST /api/engine/resume -- never on "
                "reconnect, never on restart (CLAUDE.md rule 9)"
            ),
            "was_halted": ended.was_halted,
            "previous_reason": ended.previous_reason,
            "previous_halted_at": (
                ended.previous_at.isoformat() if ended.previous_at else None
            ),
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return ended


def _scrubbed_reason(request: Request, typed: str) -> str:
    """The halt reason as it may be kept: de-identified and column-bounded.

    Configured secrets come from ``app.state.secret_values`` -- the same
    per-request read of ``SECRET_ENV_VARS`` the error envelope uses.

    **An app without that state still halts.** Unlike the activity route,
    which refuses to serve vendor text it cannot redact, this route falls back
    to no configured secrets: failing a halt to protect a log line would trade
    the thing rule 9 exists for against the thing rule 6 exists for, and the
    credential *shapes* still apply either way. ``create_app`` always sets it.
    """
    provider = getattr(request.app.state, "secret_values", None)
    secrets: Sequence[str] = provider() if callable(provider) else ()
    return operator_text(typed, secrets=secrets, limit=HALTED_REASON_WIDTH)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/state", summary="Halt state and the t0 marker")
def read_state(session: SessionDep) -> EngineStateResponse:
    return _response(engine_state(session))


@router.post("/halt", summary="Stop new entries, with a stated reason")
def halt(
    body: HaltRequest,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
) -> EngineStateResponse:
    """Halt the engine and record why.

    Rule 7: this stops new entries. Existing positions keep their managed
    exits, and nothing is closed. Rule 8's standard -- the rule, the inputs
    and the timestamp -- applies to a state change as squarely as to a
    rejection, because the symptom of an unexplained halt is identical: a bot
    doing nothing when you expected it to trade.

    Halting an already-halted engine records the **newest** reason and logs
    the one it replaced, so the first cause survives in the record. Keeping
    the old reason instead would make the endpoint silently ignore an
    operator, which is worse than losing the earlier line from the response.

    The owner's action is then told to the bell and Discord
    (``operator_halt``), after the commit and after the response -- a
    notification never delays or fails a halt.

    **The reason is scrubbed once, here, before anything keeps it** (rule 6).
    It is owner-typed free text, and a key pasted into it by mistake would
    otherwise reach ``engine_state``, two log lines, the bell and Discord
    verbatim. :func:`_scrubbed_reason` runs first and ``body.reason`` is not
    read again, so every sink carries the same text and a sink added later
    cannot be the one that forgot to scrub.
    """
    at = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())
    reason = _scrubbed_reason(request, body.reason)
    state = engine_state(session)

    if state.halted and state.halted_reason is not None:
        logger.info(
            "engine was already halted; replacing the stated reason",
            extra={
                "event": "engine_halt_repeated",
                "rule": OperatorRule.OPERATOR_HALT_REPLACED.value,
                "policy": "the newest reason is served; the replaced one is logged",
                "previous_reason": state.halted_reason,
                "previous_halted_at": (
                    state.halted_at.isoformat() if state.halted_at else None
                ),
                "reason": reason,
                "at": at.isoformat(),
                "correlation_id": correlation_id,
            },
        )

    was_halted = state.halted
    previous_reason = state.halted_reason
    previous_at = state.halted_at
    state.halted = True
    state.halted_reason = reason
    state.halted_at = at
    session.commit()

    logger.warning(
        "engine halted: %s",
        reason,
        extra={
            "event": "engine_halted",
            "rule": OperatorRule.OPERATOR_HALT.value,
            "policy": (
                "halt stops new entries and is recorded with its reason and "
                "timestamp (CLAUDE.md rules 7 and 8)"
            ),
            "reason": reason,
            "previous_reason": previous_reason,
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    notify_after_response(
        request,
        background,
        halt_notice(
            # The stored value, as committed -- never the request's.
            reason=state.halted_reason or reason,
            was_halted=was_halted,
            previous_reason=previous_reason,
            previous_at=previous_at,
            at=at,
            correlation_id=correlation_id,
        ),
    )
    return _response(state)


@router.post("/resume", summary="End a halt -- the only way one ends")
def resume(
    session: SessionDep, request: Request, background: BackgroundTasks
) -> EngineStateResponse:
    """Clear the halt. Explicit, human, and reachable from nowhere else.

    Deliberately takes no body and no confirmation token. The confirmation
    that matters is the one the UI asks for before it calls this; adding a
    second here would only make the endpoint feel safe enough to call from
    code, which is exactly what rule 9 forbids.

    ``operator_resume`` is sent after the commit, naming the halt that ended:
    a record of a human's recovery step, never a mechanism for one.
    """
    at = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())
    state = engine_state(session)
    ended = _clear_halt(state, at=at, correlation_id=correlation_id)
    session.commit()
    notify_after_response(
        request,
        background,
        resume_notice(
            was_halted=ended.was_halted,
            previous_reason=ended.previous_reason,
            previous_at=ended.previous_at,
            at=at,
            correlation_id=correlation_id,
        ),
    )
    return _response(state)
