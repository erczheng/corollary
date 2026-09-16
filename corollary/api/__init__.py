"""FastAPI app: read endpoints, command endpoints, WebSocket fan-out.

The application itself lives in :mod:`corollary.api.app`. This module exists
to keep ``corollary.api:app`` working, which is not a convenience -- it is the
string ``uv run uvicorn corollary.api:app`` is started with, and the target a
Vite dev proxy forwards ``/api`` to. Re-exporting rather than re-building
means there is exactly one app object; two would be two sets of routes, one of
which is never served and never noticed.

``dev_app`` is re-exported beside it: the same application with no vendor
sockets, which is what ``--reload`` should be pointed at. A reloader opens a
second set of sockets on every save, and Alpaca answers the surplus trading
connection with a 406 that halts the engine -- correctly, and fifteen times an
afternoon. See :mod:`corollary.api.app`.
"""

from corollary.api.app import app, dev_app

__all__ = ["app", "dev_app"]
