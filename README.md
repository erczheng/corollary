# Corollary

A single-user equity options trading terminal. See `PRD.md` for product scope,
`CLAUDE.md` for engineering rules, and `DESIGN.md` for the token system.

## Backend

```bash
uv sync
uv run pytest
uv run alembic upgrade head
uv run python -m corollary.engine
uv run uvicorn corollary.api:app --reload
```

## Frontend

```bash
cd web
npm install
npm run dev
npm run typecheck
```
