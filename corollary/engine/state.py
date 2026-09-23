"""The ``engine_state`` singleton: the create-if-missing rule and the ``t0`` rule.

These two helpers are persistence, not routing, and they used to live in
``api/routes/engine.py``. ``EngineRuntime`` needs both, and ``corollary.api``
imports ``app`` eagerly, which imports the runtime -- so reaching them from the
runtime meant a deferred import inside every method that touched the row. Here
they sit below both layers: the route module and the runtime each import them
at module level, and there is still exactly **one** copy of each rule.

That last part is the point of the move rather than a tidy-up. Two places
deciding whether ``t0`` is rewritten is how the equity curve's marker starts
walking forward, and two places deciding what a missing row means is how an
absent state becomes evidence of a healthy engine.

**Nothing here clears a halt, and nothing may.** ``POST /api/engine/resume``
is the only thing in the codebase that does (rule 9), through the private
helper in ``api/routes/engine.py``, and ``tests/api/test_engine_routes.py`` greps
the package to hold that line. The only value this module ever writes to
``halted`` is ``True``.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from corollary.db.models import ENGINE_STATE_ID, EngineState

__all__ = ["engine_state", "mark_started"]

logger = logging.getLogger(__name__)


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

    Called from ``EngineRuntime.start``. It does **not** touch ``halted``: a
    restart is not a resume.
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
