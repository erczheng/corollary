"""FastAPI app: read endpoints, command endpoints, WebSocket fan-out.

Phase 1 exposes a health check only; real routes land in Phase 2 onward.
"""

from fastapi import FastAPI

app = FastAPI(title="Corollary")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
