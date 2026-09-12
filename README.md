# Corollary

A single-user equity options trading terminal. See `PRD.md` for product scope,
`CLAUDE.md` for engineering rules, and `DESIGN.md` for the token system.

## Backend

```bash
uv sync
uv run python -m pytest
uv run alembic upgrade head
uv run python -m corollary.engine
uv run python -m uvicorn corollary.api:app --reload
```

The `python -m` form is deliberate: `uv run pytest` and `uv run uvicorn` are
both blocked by Windows Application Control on this machine (`os error 4551`),
because each resolves to a generated console script. See `CLAUDE.md`.

## Frontend

```bash
cd web
npm install
npm run dev
npm run typecheck
```
