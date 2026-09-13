"""FastAPI app: read endpoints, command endpoints, WebSocket fan-out.

The application itself lives in :mod:`corollary.api.app`. This module exists
to keep ``corollary.api:app`` working, which is not a convenience -- it is the
string ``uv run uvicorn corollary.api:app`` is started with, and the target a
Vite dev proxy forwards ``/api`` to. Re-exporting rather than re-building
means there is exactly one app object; two would be two sets of routes, one of
which is never served and never noticed.
"""

from corollary.api.app import app

__all__ = ["app"]
