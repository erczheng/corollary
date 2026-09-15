"""HTTP routes, one module per surface.

Phase 2 step 7 splits into sub-steps, and this package fills in with them.
Present now: ``account``, ``activity``, ``engine``, ``markets``, ``positions``,
``settings`` and ``ws`` -- each with its own module and its own
``include_router`` line in ``api/app.py``.

``ws`` is the odd one: a websocket rather than a set of HTTP verbs, carrying
quotes and ``trade_updates`` and deliberately *not* engine state, which is
polled. Its vendor-facing half -- the sockets that feed it, and rule 9's
watchdog producers -- is step 8d part 2 and lives outside this package,
because ``import alpaca`` is permitted in exactly two files and neither is a
route.

Each router carries its own ``prefix``, so mounting is one line and the path
lives next to the handlers rather than in the wiring.

Routers are re-exported under explicit names rather than as bare ``router``,
because ``from .engine import router`` in six modules is six things called the
same thing and one import someone gets wrong.
"""

from corollary.api.routes.account import router as account_router
from corollary.api.routes.activity import router as activity_router
from corollary.api.routes.engine import router as engine_router
from corollary.api.routes.markets import router as markets_router
from corollary.api.routes.positions import router as positions_router
from corollary.api.routes.settings import router as settings_router
from corollary.api.routes.ws import router as ws_router

__all__ = [
    "account_router",
    "activity_router",
    "engine_router",
    "markets_router",
    "positions_router",
    "settings_router",
    "ws_router",
]
