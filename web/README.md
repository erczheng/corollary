# corollary-web

React + Vite + TypeScript frontend for Corollary. See the repo root
`README.md`, `CLAUDE.md`, and `DESIGN.md` for context — this file only
covers commands local to `web/`.

```bash
npm install
npm run dev         # http://127.0.0.1:5173
npm run typecheck   # tsc -b --noEmit — must pass before commit
npm run build       # production build
npm run test        # vitest run
```

`/design` renders every token and primitive from `DESIGN.md` — check it
before any real page depends on the token system.
