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
``EngineRuntime`` are step 8. This module is the persistence and the
statement of state, nothing more.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy.orm import Session

from corollary.api.deps import SessionDep
from corollary.api.schemas import EngineStateResponse, HaltRequest
from corollary.db.models import ENGINE_STATE_ID, EngineState

__all__ = ["engine_state", "mark_started", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/engine", tags=["engine"])


# --------------------------------------------------------------------------
# The singleton
# --------------------------------------------------------------------------


def engine_state(session: Session) -> EngineState:
    """The one ``engine_state`` row, created **halted** if it is not there.

    Creating on read is a write in a GET, which is normally a smell. It is the
    right call here for one reason: the alternative is answering "is the
    engine halted?" with a 404 or a made-up default, and the only safe default
    is the one that also has to be persisted. **Absence of state is not
    evidence of a healthy engine.**

    The row is seeded on startup, so reaching this branch means somebody has
    been in the database. Coming up halted is what the design spec asks for on
    a cold start anyway: halted until the opening snapshot succeeds.
    """
    state = session.get(EngineState, ENGINE_STATE_ID)
    if state is None:
        state = EngineState(id=ENGINE_STATE_ID, halted=True)
        session.add(state)
        session.commit()
        logger.warning(
            "engine_state was missing and was recreated halted",
            extra={
                "event": "engine_state_recreated",
                "rule": "absence of state is not evidence of a healthy engine",
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
    return state


def mark_started(session: Session, *, at: datetime) -> EngineState:
    """Write ``t0`` if it has never been written. Never rewrite it.

    Decision 6: the Dashboard draws Alpaca's own equity curve and marks where
    Corollary started running, so the chart does not claim credit for manual
    trading that predates it. A ``t0`` that moved on every restart would walk
    the marker forward until it claimed credit for none of the trading it
    covers -- and ``uvicorn --reload`` restarts this process on every edit.

    Called from the lifespan. It does **not** touch ``halted``: a restart is
    not a resume.
    """
    state = engine_state(session)
    if state.t0 is None:
        state.t0 = at
        session.commit()
        logger.info(
            "corollary t0 recorded",
            extra={
                "event": "engine_t0_recorded",
                "rule": "t0 is written on the first ever start and never rewritten",
                "at": at.isoformat(),
            },
        )
    return state


def _response(state: EngineState) -> EngineStateResponse:
    return EngineStateResponse(
        halted=state.halted,
        halted_reason=state.halted_reason,
        halted_at=state.halted_at,
        t0=state.t0,
    )


def _clear_halt(state: EngineState, *, at: datetime, correlation_id: str) -> None:
    """End a halt. **The only code in this package that does.**

    Private, and named so a grep over ``corollary/`` reads as an assertion.
    ``tests/api/test_engine_routes.py`` pins both this name and the literal
    assignment below to this one file.
    """
    previous_reason = state.halted_reason
    previous_at = state.halted_at
    state.halted = False
    state.halted_reason = None
    state.halted_at = None
    logger.warning(
        "engine resumed by an explicit request (previous reason: %s)",
        previous_reason,
        extra={
            "event": "engine_resumed",
            "rule": (
                "a halt ends only at POST /api/engine/resume -- never on "
                "reconnect, never on restart (CLAUDE.md rule 9)"
            ),
            "previous_reason": previous_reason,
            "previous_halted_at": previous_at.isoformat() if previous_at else None,
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/state", summary="Halt state and the t0 marker")
def read_state(session: SessionDep) -> EngineStateResponse:
    return _response(engine_state(session))


@router.post("/halt", summary="Stop new entries, with a stated reason")
def halt(body: HaltRequest, session: SessionDep) -> EngineStateResponse:
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
    """
    at = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())
    state = engine_state(session)

    if state.halted and state.halted_reason is not None:
        logger.info(
            "engine was already halted; replacing the stated reason",
            extra={
                "event": "engine_halt_repeated",
                "rule": "the newest reason is served; the replaced one is logged",
                "previous_reason": state.halted_reason,
                "previous_halted_at": (
                    state.halted_at.isoformat() if state.halted_at else None
                ),
                "reason": body.reason,
                "at": at.isoformat(),
                "correlation_id": correlation_id,
            },
        )

    previous_reason = state.halted_reason
    state.halted = True
    state.halted_reason = body.reason
    state.halted_at = at
    session.commit()

    logger.warning(
        "engine halted: %s",
        body.reason,
        extra={
            "event": "engine_halted",
            "rule": (
                "halt stops new entries and is recorded with its reason and "
                "timestamp (CLAUDE.md rules 7 and 8)"
            ),
            "reason": body.reason,
            "previous_reason": previous_reason,
            "at": at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return _response(state)


@router.post("/resume", summary="End a halt -- the only way one ends")
def resume(session: SessionDep) -> EngineStateResponse:
    """Clear the halt. Explicit, human, and reachable from nowhere else.

    Deliberately takes no body and no confirmation token. The confirmation
    that matters is the one the UI asks for before it calls this; adding a
    second here would only make the endpoint feel safe enough to call from
    code, which is exactly what rule 9 forbids.
    """
    at = datetime.now(timezone.utc)
    state = engine_state(session)
    _clear_halt(state, at=at, correlation_id=str(uuid.uuid4()))
    session.commit()
    return _response(state)
