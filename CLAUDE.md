# CLAUDE.md

Project instructions for Claude Code working on Corollary.

## What this is

Corollary is a single-user equity options trading terminal. Python engine, React frontend, Alpaca for execution and data. It places real orders with real money. Read `PRD.md` before making architectural decisions.

**This codebase can lose money if it is wrong.** Correctness beats velocity everywhere in the engine. In the UI, ship fast.

---

## Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, uvicorn |
| Package manager | `uv` (not pip, not poetry) |
| Frontend | React 18 + Vite + TypeScript |
| Styling | Tailwind v4, CSS-first config via `@theme`, no `tailwind.config.js` |
| Charts | Recharts |
| State/data | TanStack Query for server state, Zustand for UI state |
| Database | SQLite via SQLAlchemy |
| Historical data | Parquet on disk, queried with DuckDB |
| Migrations | Alembic |
| Tests | pytest, Vitest |

## Commands

```bash
uv sync                          # install
uv run pytest                    # backend tests
uv run pytest -m risk            # risk manager tests only — run before any engine change
uv run alembic upgrade head      # migrations
uv run python -m corollary.engine    # start engine
uv run uvicorn corollary.api:app --reload   # start API
npm run dev                      # frontend
npm run typecheck                # tsc, must pass before commit
```

---

## Hard rules

These are not style preferences. Violating them creates financial risk.

### 1. All orders go through the risk manager

There is exactly one code path to `submit_order`, and it runs inside `RiskManager.approve()`. Never call the broker directly from a strategy, the scanner, the LLM layer, or an API endpoint.

If a test needs to bypass the risk manager, that test is wrong.

### 2. Backtests cannot touch live credentials

`BrokerInterface` has two implementations: `AlpacaBroker` and `SimBroker`. The backtest worker runs as a separate process whose environment contains no live API keys. It must be structurally impossible for a backtest to place a real order — not merely discouraged.

On Windows this means launching the worker with an explicitly scrubbed environment, not an inherited one.

### 3. MCP servers are never in the order path or the data pipeline

MCP is a protocol for language models to call tools. It belongs in two places: the Research chat tab, and your own tooling while developing. The engine talks to Alpaca over REST and WebSocket. Do not route orders, quotes, or scanner inputs through an MCP server — it is slow, nondeterministic, and unauditable after the fact.

### 4. Risk limits are enforced server-side

The UI displays limits. The engine enforces them. Never trust a value that arrived from the client.

Current ceilings (editable in Settings, audit-logged on change):

```
max_risk_per_trade_pct       = 7
max_daily_loss_pct           = 20
max_concurrent_positions     = 8
max_exposure_per_underlying  = 25
max_net_directional_pct      = 40
```

**What "risk" means** — a percentage ceiling is uninterpretable without this, so do not infer it per call site:

- **Defined-risk structures** — maximum loss at expiry.
- **Long options** — premium paid.
- **Undefined-risk structures** — a stress loss computed at ±2σ of the underlying's 20-day realized volatility.

### 5. Paper is the default

Every cold start comes up in Paper. Switching to Cash requires an explicit confirmation naming the account and balance. Never default to live, never persist Cash across a restart without re-confirmation.

### 6. Secrets live in the environment

`.env` is gitignored. No keys in code, in tests, in fixtures, or in log output. The Settings UI shows masked presence only — it never renders a key.

This is enforced, not just instructed: `.claude/settings.json` denies `Read(./.env)`. That denial is expected, not a misconfiguration — work from `.env.example`, which carries the key names with no values. If a variable you need isn't there, add it to `.env.example` and say so rather than trying to read the real file.

### 7. Halt and Flatten are distinct

`halt()` stops new entries; existing positions keep their managed exits. `flatten()` closes everything, then halts. Never merge them into one control or one function.

### 8. Log every rejection

A rejected order records the rule that rejected it, the inputs, and the timestamp. Silent rejection is a bug — you will need this the first time the bot does nothing when you expected it to trade.

### 9. The dead-man's switch halts automatically, and never auto-resumes

If the engine loses its Alpaca connection, or the risk manager stops heartbeating for 90 seconds, the engine calls `halt()` on itself and fires a critical notification. Recovery requires an explicit human resume.

Never auto-resume on reconnect. Reconnecting into an unverified position state is how a bot doubles a position it already holds.

---

## Layout

```
corollary/
├── engine/
│   ├── scheduler.py         # pre-market build, 15m refresh, EOD roll
│   ├── scanner/             # deterministic candidate generation
│   ├── llm/                 # enrichment, origination, classification
│   ├── strategy/
│   │   ├── schema.py        # JSON Schema for strategy YAML
│   │   ├── indicators.py    # THE WHITELIST — see below
│   │   └── runtime.py       # rule evaluation
│   ├── risk/                # RiskManager — the only path to an order
│   └── execution/
│       ├── interface.py     # BrokerInterface
│       ├── alpaca.py        # AlpacaBroker
│       └── sim.py           # SimBroker
├── data/
│   ├── providers/           # MarketDataProvider + implementations
│   ├── news/                # 3-tier sentiment pipeline
│   └── macro/               # FRED, sentiment composite
├── backtest/                # separate worker, SimBroker only
│   └── spread.py            # the explicit spread model — see market data below
├── api/                     # FastAPI routes + WS
└── db/                      # models, migrations

web/
├── src/
│   ├── pages/               # Dashboard, Activity, News, Markets, Research, Account, Settings
│   ├── components/
│   ├── hooks/
│   └── lib/
```

---

## The indicator whitelist

`engine/strategy/indicators.py` defines every function a strategy YAML may call. Strategies are declarative documents validated against a JSON Schema; they contain no executable code.

**The LLM may propose rules. The LLM may never extend the whitelist.** Adding a primitive is a deliberate human code change with a test.

When validation encounters an unknown function name, reject the whole strategy. Do not partially evaluate.

---

## Working with market data

**Provider abstraction is mandatory.** Everything goes through `MarketDataProvider`. Swapping Alpaca for ThetaData or Polygon should be a config change, not a refactor.

Do not import `alpaca` anywhere except these two files: `data/providers/alpaca.py` for market data, and `engine/execution/alpaca.py` for order placement. Those two are the entire vendor surface area.

**Alpaca specifics worth knowing:**

- **Data plan is Basic (free).** Real-time options is the `indicative` feed — a 15-minute-delayed derivative of OPRA, not OPRA itself. Equities real-time is IEX only. 200 req/min, and websocket streams are capped at **30 symbols**. Requesting `feed=opra` or `feed=sip` for a timestamp inside the last 15 minutes returns an auth error, not empty data — if a call fails on feed access, check the plan before debugging the code.
- **Feed names are configuration, never literals.** Three env vars, read only inside
  `data/providers/alpaca.py`:

      ALPACA_OPTIONS_FEED=indicative
      ALPACA_STOCK_FEED_HISTORICAL=sip
      ALPACA_STOCK_FEED_REALTIME=iex

  Upgrading to Algo Trader Plus ($99/mo — full OPRA and SIP, unlimited symbols,
  10,000 req/min) sets all three to `opra`/`sip`/`sip` and changes nothing else.

- **Historical equity data uses SIP even on Basic — never default it to IEX.** IEX is
~2.5% of US equity volume; SIP is 100%. Any historical request whose `end` is more than 15 minutes old may use `feed=sip` for free. Only the latest/snapshot endpoints and the live stream are IEX-limited. This lands directly on the scanner: `min_avg_volume` in a strategy YAML compares
against whatever feed produced the bars. Computed from IEX, a 5,000,000 threshold is filtering on a fortieth of real volume and means nothing like what the strategy author wrote.
- **The 30-symbol stream cap is a design constraint, not a footnote.** Every option contract is its own symbol, so thirty goes fast. Scope the live stream to open positions plus recommended trades; the Markets page chains run on polled snapshots.
- Historical options data **starts February 2024** — an Alpaca limit, not a fact about the world. OPRA history exists further back and other vendors sell it. A backtest window starting before Feb 2024 is a bug *against this provider*; revisit if a second `MarketDataProvider` is ever added.
- **Historical options coverage is bars only, in practice.** There is no historical quotes endpoint at all — quotes are latest-only, via snapshot and chain. Trades exist but reach back **7 days**, so anything older than a week is bars and nothing else. The backtester must use the explicit spread model in `backtest/spread.py` and surface the assumption in every result. That model cannot be validated against real prints beyond the 7-day window, so treat every backtest fill price as an estimate, never a measurement.
- **Backtesting still does not need the paid plan.** Everything older than 15 minutes is available on every feed, so the Feb 2024 → yesterday bulk download to Parquet runs fine on Basic — it just returns bars, per the point above. Only live quotes are degraded by the free tier.
- Option symbols are OCC format: underlying + YYMMDD + C/P + 8-digit strike ×1000. `AAPL241220C00150000` is the AAPL $150 call expiring 20 Dec 2024.
- **Watch for adjusted contracts.** After a split or special dividend, OCC issues a modified root with a numeric suffix (`AAPL1`) and the deliverable is no longer 100 shares. Sizing and P&L math that assumes a 100 multiplier will be wrong on those, which means the risk manager computes max loss wrong — exactly the failure rule 4 exists to prevent. Filter them out of the scanner universe unless they are handled explicitly.
- Multi-leg orders require Level 3. Current account level: 

**Backtest data access:** bulk-download to Parquet once, query with DuckDB. **Never call the API inside a backtest loop** — it turns minutes into hours and burns the rate limit. Filter the contract universe on download: ±15% of spot, ≤60 DTE, minimum open interest.

**When unsure of an endpoint shape, read the docs rather than guessing.** Alpaca publishes an LLM-formatted index at `https://docs.alpaca.markets/us/llms.txt`. A hallucinated parameter name in the data layer surfaces as a runtime error; a hallucinated one in the order path surfaces as a rejected or malformed order.

---

## Frontend

**Design tokens live in `DESIGN.md` frontmatter.** Use them; do not invent colors. Light values are in `colors` + `semantic`, dark in `colorsDark` + `semanticDark`. The two halves are parallel — every key in one has a counterpart in the other, so a token resolver can treat them identically.

**Type.** Montserrat for display, headings, and body. Plus Jakarta Sans for labels, buttons, tags, table headers, and captions — its narrower proportions hold up at 12–14px where Montserrat gets loose. JetBrains Mono with `tabular-nums` for every price, strike, P&L, quantity, and percentage. That last one is non-negotiable in tables: proportional figures make columns of numbers wobble, which measurably slows down scanning a position list.

All three faces are self-hosted via `@fontsource`. No Google Fonts `<link>`, no CDN — the terminal binds to `127.0.0.1` and has to render correctly with no internet.

**Radii.** 0.5rem buttons, inputs, checkboxes. 1rem cards and primary containers. 1.5rem hero elements and modals. `full` for chips, tags, sentiment pills, and the trading-state pill — pills read as status, rectangles read as controls, and that distinction has to survive a dense table. Spacing on an 8px scale.

**Elevation is tonal, not shadowed** — layered surface tints and 1px outlines. Light theme may use one soft ambient shadow on hover, tinted with `primary` at very low opacity, never black. **Dark theme uses no shadows at all**: a primary-tinted shadow is invisible on `#13140d` and a light-tinted one reads as a glow, so lift comes from stepping up the surface-container scale instead.

Borders have three tokens and they are not interchangeable: `outline-warm` for cards, large flat divisions, and chart gridlines; `outline` for input borders and dense table rules; `outline-variant` for fine hairlines that need to recede completely.

### Two things that will go wrong if you skim

**The outline tokens are not text colors.** `outline #717879` measures 4.3:1 on `surface` and `outline-warm #b7b7a5` measures 1.9:1, against a stated 4.5:1 floor. Both read as plausible muted greys and neither one is. Placeholder text, captions, and secondary labels take `on-surface-variant #414849` (8.9:1). Disabled controls are the single exception, since WCAG exempts them.

**`bearish` and `error` are different colors and must stay different.** A loss renders in `bearish`; a rejected order, a failed connection, or a destructive confirm renders in `error`. A losing position is not a system failure and the interface must never imply that it is. The split holds in dark theme too — do not collapse them when deriving dark values.

**Other frontend rules:**

- No `localStorage` or `sessionStorage` in artifacts previewed in chat; in the real app they're fine.
- Tables are the primary interface. Get density, alignment, and number formatting right before anything decorative.
- P&L must encode sign textually as well as by color — an explicit `+` or `−` on every value. Color alone fails for colorblind users, screenshots, and grayscale.
- Every interactive element needs a visible keyboard focus state: 2px ring in `primary`, 2px offset in `surface`, so the ring reads against both the control and the page.
- Every destructive action (Flatten, Close, Delete strategy) gets a confirm dialog that states the consequence in concrete terms, not "Are you sure?"
- Loading and empty states are designed, not afterthoughts. An empty Recommended Trades list at 3pm means something different than at 8am; say which.
- `accent` is capped at two roles per screen, ranked in `DESIGN.md`. It only works while it stays rare.

---

## Testing expectations

Coverage requirements scale with blast radius:

| Area | Requirement |
|---|---|
| Risk manager | Every limit has a test that proves it rejects. Every limit has a test that proves it permits at the boundary. |
| Execution | Fill handling, partial fills, rejections, disconnects, reconnects. |
| Strategy schema | Unknown function names rejected. Malformed YAML rejected. Valid documents round-trip. |
| Backtest | No look-ahead — a test that fails if an indicator reads an unclosed bar. |
| Scanner | Deterministic: same inputs produce identical output. |
| UI | Typecheck passes. Component tests for tables and forms. |

Run `uv run pytest -m risk` before any change to the engine, no exceptions. The `risk` marker is registered in `pyproject.toml` under `--strict-markers`, so a typo fails loudly instead of quietly running zero tests.

---

## Conventions

- Type hints everywhere in Python. `mypy` clean.
- Money as `Decimal`, never `float`. Currency arithmetic in floats is a real bug source. SQLAlchemy columns are `Numeric`, not `Float`, or the rule leaks at the database boundary.
- All timestamps stored UTC, displayed in `America/New_York`. Market data is Eastern; never assume local. On Windows this requires the `tzdata` package — `zoneinfo` raises without it.
- Session boundaries come from a market calendar, never hardcoded 09:30–16:00. Half-days and holidays are real.
- Log structurally (JSON) with a correlation ID per decision, so a trade can be traced from scan → LLM → risk → order → fill.
- Prefer boring, explicit code in the engine. Cleverness there costs money later.
- Small commits. The engine's git history is a debugging tool.

---

## Working style

Address me as Eric at the start of each response.

---

## When uncertain

Ask rather than assume, particularly about: risk limit semantics, order types, exit logic, and anything touching live money. A wrong guess in the UI is a bug report; a wrong guess in the engine is a loss.