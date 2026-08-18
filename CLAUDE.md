# CLAUDE.md

Project instructions for Claude Code working on Corollary.

## What this is

Corollary is a single-user equity options trading terminal. Python engine, React frontend, Alpaca for execution and data. It places real orders with real money. Read `PRD.md` before making architectural decisions.

**This codebase can lose money if it is wrong.** Correctness beats velocity everywhere in the engine. In the UI, ship fast.

---

## Stack

| Layer | Choice |
|---|---|
| Backend | Python ≥3.12, FastAPI, uvicorn — `.python-version` pins **3.14**, because uv's managed 3.12 build fails to launch on this machine (missing runtime DLL, not a policy block) |
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
uv run alembic upgrade head      # migrations — NOT YET WIRED, no alembic.ini exists yet
uv run python -m corollary.engine    # start engine
uv run uvicorn corollary.api:app --reload   # start API
uv run mypy corollary            # type check, must be clean
```

Frontend commands run from `web/`:

```bash
npm run dev                      # 127.0.0.1:5173
npm run typecheck                # tsc, must pass before commit
npm run build                    # also the only way to inspect generated CSS — see below
npm run test                     # Vitest
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

**This is the target layout, not a description of the current tree.** As of
Phase 1, `corollary/` is ~130 lines of package scaffolding: every module below
exists as a stub or not at all, and `RiskManager` is nine lines. Do not go
looking for `data/providers/alpaca.py` or `db/models.py` — write them when the
phase calls for them. The frontend half of this tree is real.

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
- Multi-leg orders require Level 3. Current account level: 3.

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
- Every interactive element needs a visible keyboard focus state: 2px ring in `primary`, 2px offset in `surface`, so the ring reads against both the control and the page. **`:focus-visible` is not sufficient on its own** — per spec a text field matches it whenever it is focused, *including on click*, so inputs and selects ring on mouse click while buttons correctly stay quiet. The ring is gated on `html[data-modality]`, set by `useFocusModality`. Keep the ring on the keyboard path; never delete it outright.
- Every destructive action (Flatten, Close, Delete strategy) gets a confirm dialog that states the consequence in concrete terms, not "Are you sure?"
- Loading and empty states are designed, not afterthoughts. An empty Recommended Trades list at 3pm means something different than at 8am; say which.
- `accent` is capped at two roles per screen, ranked in `DESIGN.md`. It only works while it stays rare.

### Tailwind v4 traps — all four of these shipped silent bugs before being caught

Adding a token to `@theme` is not self-verifying. **After adding or renaming
one, run `npm run build` and grep `dist/assets/*.css` for the utility you
expect.** Every item below produced working-looking code that was wrong:

- **Don't add named `--spacing-{xs,sm,md,lg,xl}` keys.** `max-w-*`, `w-*`, and
  friends resolve names from the same `--spacing-*` namespace, so
  `--spacing-md: 24px` silently redefines `max-w-md` from 28rem to 24px
  app-wide. It broke a modal into a 24px sliver. DESIGN.md's 4/8/12/24/48/80px
  scale is already Tailwind's numeric scale (`p-1`/`2`/`3`/`6`/`12`/`20`) —
  use that. Only genuinely new names (`gutter`, `margin-desktop`) get entries.
- **`duration-*` reads `--transition-duration-*`, not `--duration-*`** —
  inconsistent with `ease-*`, which really is `--ease-*`. Getting this wrong
  emits no utility classes at all, no error.
- **Unreferenced `@theme` variables are tree-shaken out of the build.** A token
  nothing uses yet vanishes, so "I added it" and "it exists" are different
  claims. Exercise new tokens on `/design`.
- **A custom `className` on an icon component replaces its default sizing**
  rather than merging, so the SVG renders at browser-default size. Pass `h-*
  w-*` explicitly when overriding.

### Where frontend logic lives — check here before writing a new helper

- `web/src/lib/format.ts` — all money, percent, sign, and timestamp
  formatting, plus the confidence-tier mapping. Never inline this.
  **Date-only values (option expiries) format in UTC, not ET**: a bare
  `YYYY-MM-DD` parses as UTC midnight, and ET is behind UTC, so ET rendering
  shows the *previous day* — a Nov 21 expiry displays as Nov 20.
- `web/src/lib/mockData.ts` — Phase 1 fixtures. Deterministic (seeded PRNG,
  never `Math.random()`) so screenshots and tests don't flake, and typed to
  match the eventual API shape so Phase 2 is a data-source swap, not a
  component rewrite. Cover every state a component can render, including the
  ugly ones — a tier or empty state absent from the fixtures is one nobody
  can see.
- `web/src/components/ExecutionsTable.tsx` — the activity/executions table,
  its status filter, and its CSV row mapper. The Dashboard's "Recent
  Executions" and Activity's "Recent Activity" are the same feed at
  different depths and render from this one component so the cell logic
  cannot drift apart. The two column sets live in `COLUMN_LAYOUTS` and are
  written out in full rather than assembled from conditions — they differ
  in order as well as in membership (`summary` puts Qty before Price and
  ends on PnL; `full` leads with P&L and ends on Status), and inline
  ternaries made it far too easy to change one while meaning to change the
  other. Don't copy the markup into a third page — add a layout.
- `web/src/lib/orders.ts` — every rule about an order that could be wrong
  about money, as pure functions with no React in them: which order types a
  position can take, which side an action resolves to, which side of the
  spread it crosses, the estimate, the OCO stop-price threshold, and the
  payoff curve. Test it without rendering anything. **Two that are easy to
  get backwards:** selling hits the bid and buying lifts the ask (reversing
  it overstates proceeds by the spread on every estimate), and a short's
  `openUnitValue` is *negative* because a credit is a liability (reversing
  it inverts every credit spread's payoff curve).
- `web/src/lib/markets.ts` — the Markets page's filtering, ranking and
  sorting, as pure functions with no React in them. The named screens
  (`CHAIN_RANK_SORT`, `STOCK_RANK_SORT`) and the clickable column headers
  drive **one** sort between them, so the dropdown can never claim a
  ranking the table is not in; `chainRankFor`/`stockRankFor` map back and
  return null for "Custom". **Three that are easy to get wrong:** a fund's
  `marketCap` is `null`, not 0, and it sorts *last in both directions*
  rather than being coerced — coerced to zero it would sort SPY to the
  *top* of an ascending list and state, in a column of dollars, that a
  fund is worth nothing. `losers` is its own ascending sort, not
  `gainers` reversed. And "trending" is relative volume
  (`volume / avgVolume`), a different question from "most active" — raw
  volume finds the same mega caps every session because NVDA trades 200M
  shares on a quiet day. Nothing here sorts in place: every view reads the
  same array, so an in-place `.sort()` would leave the last screen's
  ordering behind in the data itself.
- `web/src/components/ChainOrderTicket.tsx` — opening a position from a
  chain row. Deliberately *not* `OrderTicket`, which acts on something you
  already hold and derives its side from the position's direction; here
  the side is the user's choice, so `orders.ts` grows a parallel
  `OpenDraft`/`estimateOpen`/`openRisk` set over a bare `Quote` rather
  than a `Position`. Buying to open lifts the ask, selling hits the bid.
  **A short reports `{ kind: 'undefined' }` risk and the ticket says so
  in words** — a naked short has no maximum loss to quote, the engine
  sizes it against a ±2σ stress loss (rule 4), and a confident wrong
  number under the word "risk" is worse than an honest absence.
- `web/src/components/PositionRow.tsx`, `OrderTicket.tsx`,
  `PositionChart.tsx` — the Open Positions row, its expanded ticket, and
  its value/payoff charts. `Activity.tsx` composes them and holds no order
  logic of its own.
- `web/src/components/UnderlyingChart.tsx` — a stock's price with a range
  control, expanded from a row of the Markets stock table. It carries no
  strike and no contract: it lived in the option ticket first and that was
  the wrong home, since a chart is something you *browse* and the place you
  browse stocks is the stock table. What the ticket needed from it was one
  sentence — `contractMoneyness` in `markets.ts`, stated inline there.
  **A call is in the money above its strike and a put below it**; inverted,
  a ticket calls a put worthless at the moment it is worth the most, and at
  the strike exactly is *out* of the money because intrinsic value is zero.
  The chart's readout reports the move **over the window on screen**, not
  over the day — a range control that redraws the axis and leaves a daily
  figure beside it is reporting on a chart nobody is looking at.
  `UNDERLYINGS` carries 400 calendar days for this reason: at a quarter,
  3M / YTD / 1Y / All all returned the same points, and at a year 1Y and
  All still did. Changing `QUOTE_SESSIONS` moves no quoted number —
  `buildUnderlying` walks backwards from today's price, so `previousClose`
  comes from the first draw whatever the count.
- `web/src/pages/Design.tsx` — the `/design` route, which proves the token
  system. New tokens get exercised here.

**Two feeds, not one: `store.tick()` streams and `store.pollMarkets()`
polls.** Activity streams at 400ms, scoped to the symbols behind open
positions — that is the 30-symbol websocket cap on the Basic plan. Markets
polls every 2s across *every* quoted symbol, which is what snapshot
requests allow under a 200/min budget. They write to the same
`underlyings` map, because one symbol has one price and a second map is how
the Markets table and an Activity row end up disagreeing about AAPL.
`MARKET_QUOTES` is that map — a superset of `UNDERLYINGS`, covering every
name on the Markets page. Two things the poll gets right that are easy to
get wrong: **volatility scales with √t, not t** (scaling linearly made a 2s
poll five times hotter per unit time than a 400ms tick, and swung a deep ITM
call from +15% to −0.7% between prints), and the poll carries its own,
calmer per-second figure than the stream — the stream's 0.3%/s is a
legibility choice for a handful of position rows, and on a 180-row screener
it is just noise. **The chain is re-priced from its underlying, never walked
contract by contract**: independent random steps invert the ladder within
seconds, printing a 225 call above the 220 beside it.

**The Activity page is live, and `store.tick()` is the mock broker.** It
stands in for the Alpaca WebSocket: it re-marks positions, fills working
orders whose price has been reached, and triggers attached exits. It
decides what the *market* did — never what is *allowed*, which stays with
`RiskManager.approve()` in Phase 2. It moves the contract *and* its
underlying — streaming one while the other sits frozen is only half live.
Its price stream is seeded like every other fixture, so a session replays
identically. Only the active account ticks, and `lastTickAt === null` is
what drives the loading skeletons — loading is a real condition, not a
timer. Past ~15s without a price the status pill reads `stale`; a badge
saying "Live" over a frozen timestamp is worse than no badge.

**The Markets chain is priced flat and displayed with a smirk.** The IV
column in `OPTION_CHAIN` is a *display* surface — prices are struck at the
underlying's base vol, unskewed. That is deliberate: with time value
carrying a skew factor, the slope of the put ladder just below the money
works out to `-a` for skew slope `a`, so any positive skew prints a lower
strike above the one beside it, which is an arbitrage rather than a
fixture. Pricing flat bounds the ladder's slope below 1, which is exactly
what keeps calls cheapening and puts richening all the way up. A contract's
day change is derived from its underlying's move scaled by delta, and the
dispersion *scales* that move rather than adding to it — added, it flipped
the sign wherever delta was small, and far OTM calls rallied on a down day.
`mockData.test.ts` pins both.

**Dates that are dates, not instants.** `Position.expiry` and `MARKET_TODAY`
are both `YYYY-MM-DD` and both parse as **UTC** midnight — `daysToExpiry`
parses both sides that way deliberately, because mixing in a local-time
`new Date()` puts them an offset apart and produces an off-by-one on any
afternoon in New York. Same trap `formatExpiry` documents. DTE is measured
from `MARKET_TODAY` rather than the real clock so fixtures don't rot: tied
to the wall clock, the near-expiry position silently becomes an expired one
next week and that state stops being reachable.

**`Position.last` is the contract's price, not the underlying's.** It sits
within `[bid, ask]`, and `Position.underlying` carries the underlying
separately — read by the payoff curve and nothing else. The row used to
carry three price columns covering two different instruments with nothing
in the names to say so; `orders.test.ts` asserts the invariant now, along
with `value === last × quantity × 100` and P&L running the right way for
the direction.

**Account-scoped state is keyed by `AccountMode`, not flattened.**
`ACCOUNT_SNAPSHOTS[mode]` carries the balance history, volume, positions,
*and* activity feed for one account, and the store holds
`openPositions`/`activity` as `Record<AccountMode, …>`. Read them as
`s.openPositions[s.accountMode]`. Rendering paper's positions while Cash is
live misreports real money exactly the way rendering paper's balance would,
and `flatten()` reaching into the other book would close positions the
loaded credentials can't even see.

**Semantic colors are not interchangeable with each other, either.** Beyond
the `bearish` vs `error` split: confidence uses `primary`/`caution`/`neutral`,
*never* `bullish`/`bearish`. A high-confidence bearish trade is ordinary here,
and a green `71%` beside a put debit spread reads as direction rather than
conviction. And a rejected order is `error` (a rule outcome), while a losing
position is `bearish`.

**`neutral` (#717879) is the same value as `outline` — 4.27:1, below the text
floor.** It is not a text color. Use the `*-container` / `on-*-container` pair,
or `on-surface-variant`.

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
- **The default branch is `master`, not `main`.** Tooling that assumes `main`
  will fail with "unknown revision."

---

## Working style

Claude must prefix every response with: "I am here Lord Eric Almighty🧎" 
---

## When uncertain

Ask rather than assume, particularly about: risk limit semantics, order types, exit logic, and anything touching live money. A wrong guess in the UI is a bug report; a wrong guess in the engine is a loss.