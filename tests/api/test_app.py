"""The app object, the health check, and the import path the dev server uses.

``uv run uvicorn corollary.api:app`` is how this project runs, and a Vite dev
proxy forwards ``/api`` to it. So ``corollary.api:app`` is not merely one
import among several -- it is the contract with the process manager, and
moving the app into ``corollary/api/app.py`` may not break it. These tests
pin both spellings.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_app_is_importable_from_the_package_root() -> None:
    """``corollary.api:app`` -- the string uvicorn is started with."""
    from corollary.api import app

    assert isinstance(app, FastAPI)


def test_the_package_root_app_is_the_one_in_app_py() -> None:
    """One app object, re-exported -- not a second one built alongside it."""
    from corollary.api import app as reexported
    from corollary.api.app import app as canonical

    assert reexported is canonical


def test_health_answers_unchanged(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_answers_without_a_database(clientless_app: FastAPI) -> None:
    """A missing database must not stop the health check answering.

    The dev server runs against a repo-root SQLite file that may not have been
    migrated yet. Coming up dead because ``alembic upgrade head`` has not been
    run would take the whole terminal offline for a condition that affects
    three routes.
    """
    with TestClient(clientless_app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
