# The commit approval queue

This directory is the handoff between `corollary-orchestrator`, which decides
what should be committed, and `corollary-committer`, which decides whether that
decision is **still true** and then commits it.

The split exists because a subagent cannot idle in the background waiting for a
signal. A file on disk is the signal.

## Contract

The orchestrator writes one JSON file per approved unit of work:

```
.claude/commit-queue/<ISO-timestamp>-<slug>.json
```

```json
{
  "subject": "feat(engine): the FIFO realized-P&L matcher",
  "body": "Alpaca returns no realized P&L on any endpoint, so avg win and\navg loss have no source without this. Matches closing fills against\nopening fills FIFO per contract symbol, Decimal throughout.\n\nExpiry closes remaining lots at zero -- a full loss on a long, the\nfull credit kept on a short.",
  "files": [
    "corollary/engine/ledger.py",
    "tests/test_ledger.py"
  ],
  "branch": "phase2-real-data",
  "verified": {
    "uv run pytest": "142 passed",
    "uv run pytest -m risk": "18 passed",
    "uv run mypy corollary": "Success: no issues found in 31 source files"
  },
  "approved_by": "corollary-orchestrator",
  "approved_at": "2026-09-10T15:42:00Z"
}
```

`verified` holds the **actual output** `corollary-test-runner` reported — not
the orchestrator's expectation of it. The committer re-runs these checks and
refuses the commit if its own output disagrees, because an approval written
twenty minutes ago describes a tree that has since changed.

## What the committer does

Filename order, which is timestamp order. Per entry:

1. Every path in `files` exists and has a pending change. A listed file with
   nothing pending means the approval is stale — reject.
2. Stage **only** the listed paths. Never `git add -A`, never `git add .`. An
   unrelated file swept into a commit is how the engine's history stops being
   a debugging tool.
3. Reject on anything credential-shaped in the staged diff, and on any staged
   path that is `.env*`, `*.pem` or `*.key`. `.env.example` with bare names and
   no values is fine and expected.
4. Re-run the `verified` checks. Backend paths pull in `pytest -m risk` and
   `mypy`; frontend paths pull in `typecheck` and Vitest.
5. Commit, then delete the queue file.

On failure the entry is **not** deleted. It is renamed `<name>.rejected.json`
with a sibling `<name>.rejected.txt` holding the exact failing output. A
rejected entry is evidence; deleting it destroys the only record of why.

## What the committer never does

Never pushes. Never `reset --hard`, `checkout -- <path>`, `clean`, `rebase`, or
`commit --amend`. Its entire write surface is `git add` of named paths and
`git commit`. Committing is local and reversible; everything on that list is
not, and none of it is the committer's call.

## Running it in the background

```
/loop 10m Drain the commit queue with the corollary-committer agent.
```

An empty queue is the common case and a successful result. The committer
reports "queue empty" and stops.

## This directory is not committed

`.gitignore` excludes everything here except this README. Queue entries are
scaffolding for a commit, not part of the repository — and a rejected entry can
quote failing test output verbatim, which has no business in history.
