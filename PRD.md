# Corollary — Product Requirements

**Version:** 0.1
**Owner:** Eric
**Status:** Draft — pre-implementation
**Last updated:** 2026-08-06

---

## 1. What this is

Corollary is a single-user equity options trading terminal. It runs locally, opens every morning before the bell, and stays open during the trading day. It does four jobs:

1. Shows what happened overnight and what's scheduled today.
2. Surfaces trade candidates generated from testable rules.
3. Executes and manages those trades — automatically or on click.
4. Records everything so the strategies can be measured and improved.

It is built for one person. There is no multi-tenancy, no sharing, no user accounts.

### Non-goals

- Not a product. No other users, ever.
- Not a general-purpose broker UI. Alpaca's own dashboard remains the fallback for anything Corollary doesn't cover.
- Not a fund-transfer tool. Deposits and withdrawals link out to Alpaca.
- Not a stock trading app. Equity options are the instrument; equities appear only as underlyings and context.

### The one-sentence test

If a feature does not help decide a trade, place a trade, manage a trade, or measure a trade, it does not belong in v1.

---

## 2. Operating model

**Runtime.** A persistent Python service (the engine) runs while the laptop is on. The web UI is a thin client over REST + WebSocket. The engine does not depend on a browser being open. Target deployment is local (`127.0.0.1`); a Raspberry Pi is a later possibility and nothing in the design should preclude it.

**Modes.** Two independent switches, always visible in the header:

| Switch | Values | Meaning |
|---|---|---|
| Account | Paper / Cash | Which Alpaca account keys are in use |
| Execution | Manual / Auto | Whether the engine may place orders unattended |

Paper is the default on every cold start. Switching to Cash requires an explicit confirm dialog that names the account and its balance.

**Controls.** Two distinct actions, never merged:

- **Halt** — stop opening new positions. Existing positions keep their managed exits.
- **Flatten** — close every open position now, then halt.

**Dead-man's switch.** If the engine loses its Alpaca connection, or the risk manager stops heartbeating for 90 seconds, the engine halts itself and fires a critical notification. Recovery requires an explicit resume.

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────┐
│  React + Vite frontend  (browser, localhost)             │
└──────────────┬──────────────────────┬───────────────────┘
               │ REST                 │ WebSocket
┌──────────────▼──────────────────────▼───────────────────┐
│  FastAPI                                                 │
│  ├── read endpoints (portfolio, positions, news, chains) │
│  ├── command endpoints (halt, flatten, execute, promote)  │
│  └── WS fan-out (quotes, fills, alerts)                  │
└──────────────┬───────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────┐
│  Engine (long-running process)                            │
│  ├── Scheduler        pre-market build, refresh, EOD roll │
│  ├── Scanner          deterministic candidate generation  │
│  ├── LLM layer        enrichment + origination (gated)    │
│  ├── Strategy runtime rule evaluation                     │
│  ├── Risk manager     the only path to an order           │
│  └── Execution        BrokerInterface → AlpacaBroker      │
└──────────────┬───────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────┐
│  Data layer                                               │
│  ├── SQLite      state, trades, strategies, recs, alerts  │
│  ├── Parquet     historical bars for backtesting          │
│  └── Providers   MarketDataProvider (Alpaca), news, macro │
└───────────────────────────────────────────────────────────┘

  Backtest worker — separate process, SimBroker only,
  no live credentials in its environment, queue concurrency 1
```

**Non-negotiable boundaries:**

- Every order passes through the risk manager. There is no other code path to `submit_order`.
- The backtest worker cannot instantiate `AlpacaBroker` — its environment has no live keys.
- MCP servers are never in the order path or the data pipeline. They exist for the Research chat tab and for Claude Code during development.

---

## 4. Risk management

These are account-level ceilings. Strategies set their own limits **within** them; a strategy can be more conservative, never less.

| Limit | Value | Behavior on breach |
|---|---|---|
| Max risk per trade | 7% of equity | Order rejected |
| Max daily loss | 20% of starting-day equity | Auto-halt, critical alert |
| Max concurrent positions | 8 | New entries rejected |
| Max exposure per underlying | 25% of equity | Order rejected |
| Max net directional exposure | 40% of equity | Order rejected |

All five are editable in Settings. Every change writes to an audit log with timestamp and previous value — when a bad month happens, you need to know whether a limit moved first.

**Order rejections are logged with the triggering rule**, surfaced in Activity, and never silently swallowed.

**Sizing.** Risk per trade is defined as maximum loss at expiry for defined-risk structures, and as premium paid for long options. For undefined-risk structures the engine computes a stress loss at ±2σ of the underlying's 20-day realized vol and sizes against that.

---

## 5. Strategies

### 5.1 Representation

A strategy is a **versioned declarative document** (YAML) validated against a JSON Schema. It may call named indicator functions from a whitelist. It may not contain executable code.

```yaml
name: spx_mean_reversion
version: 4
universe:
  symbols: [SPY, QQQ, IWM]
  min_avg_volume: 5_000_000
entry:
  all:
    - rsi(14, "1d") < 30
    - close < sma(20, "1d")
    - vix() > 15
contract_selection:
  type: put_credit_spread
  short_delta: 0.30
  width: 5
  dte: {min: 7, max: 21}
  min_open_interest: 500
  max_spread_pct: 8
sizing:
  risk_per_trade_pct: 2.0
exits:
  profit_target_pct: 50
  stop_loss_pct: 200
  time_stop_dte: 2
guardrails:
  max_concurrent: 3
  skip_if_earnings_within_days: 3
```

Adding a new primitive to the whitelist is a deliberate code change by the developer. The LLM proposes rules; it never extends the vocabulary.

### 5.2 Lifecycle

```
draft → backtest → paper → active → retired
```

Only one strategy is `active` at a time in v1. The intent/arbiter layer is built from the start so multi-strategy is a config change later.

### 5.3 Promotion gate

**Backtest, walk-forward:**

- In-sample: Feb 2024 → T−6mo. Requires **≥200 closed trades**.
- Out-of-sample: T−6mo → today. Requires **≥50 closed trades**.

**Paper:** 50 closed trades.

**Auto-approval requires all three, measured on the paper period:**

- Win rate ≥ 65%
- Profit factor ≥ 1.4
- Max drawdown ≤ 25%

Anything that misses can still be promoted manually. Anything that passes can still be rejected manually. The gate automates the obvious cases; it does not remove the decision.

**Backtest honesty requirements:**

- Alpaca historical options data begins **February 2024** — a vendor limit, not a fact about the world. OPRA history reaches further back and other vendors sell it.
- **Coverage is bars only, in practice.** There is no historical options quotes endpoint at all; quotes are latest-only via snapshot and chain. Trades exist but reach back **7 days**. Anything older than a week is bars and nothing else, so a Feb 2024 → today download yields bars alone.
- This means the spread model cannot be validated against real prints beyond a rolling 7-day window. Every backtest fill price is an estimate, never a measurement, and the UI must say so.
- The backtester therefore uses an **explicit, configurable spread model**, displayed alongside every result. Default: fill at mid ± half the modeled spread, where the spread is estimated from contract moneyness, DTE, and underlying liquidity.
- Every backtest result displays: trade count, win rate, profit factor, max drawdown, average hold time, **and the spread assumption used**.
- Commission and fees are modeled, not ignored.
- No look-ahead: indicators may only reference bars that had closed at decision time.

### 5.4 Editor

Strategies are edited in the Research tab. Hovering a strategy shows its backtest and live statistics side by side, always labeled distinctly — backtested performance and realized performance are never displayed as the same number.

---

## 6. Recommendation engine

### 6.1 Pipeline

```
Scanner (deterministic)
   ↓ candidates
LLM enrichment
   ↓ annotated candidates + LLM-originated ideas
Classifier → setup taxonomy
   ↓
Risk manager
   ↓
Recommended Trades
```

**The scanner** produces candidates from the active strategy's rules plus a standing library of setup classes. It is reproducible: same inputs, same output.

**The LLM layer** may do three things:

1. **Annotate** — add qualitative context (catalysts, filings, news) to a scanner candidate.
2. **Veto** — suppress a candidate, with a logged reason.
3. **Originate** — propose a trade the scanner did not find.

### 6.2 Origination gate

An LLM-originated trade is classified against the setup taxonomy:

- **Maps to a known setup** → normal sizing; confidence = that setup's historical base rate.
- **No match** → capped at **⅓ normal size**, labeled `unvalidated` in the UI, and drawn from a separate daily budget of **max 2 trades / 5% of equity aggregate**.

All account-level risk limits apply identically. Origin never buys an exemption.

### 6.3 Confidence

Confidence is the **backtested hit rate for the setup class**, not a model's self-report. The LLM's qualitative read is a separate displayed field. Where no base rate exists, confidence displays as `—` rather than a number.

### 6.4 Attribution logging

Every recommendation records: scanner output, LLM action (annotate/veto/originate) with reason, final decision, and eventual outcome. LLM-originated trades are tracked in their own P&L bucket. After ~50 of them the standalone contribution is measurable, and a single flag disables origination.

### 6.5 Schedule

- **08:00 ET** — pre-market build.
- **Every 15 min during RTH** — refresh.
- **Manual** — refresh button, rate-limited to one call per 60s.

Recommendations expire at market close and are archived, not deleted.

---

## 7. Data sources

| Purpose | Source | Notes |
|---|---|---|
| Execution + account | **Alpaca** | Level 3 options approval; paper and live |
| Real-time equities + options | **Alpaca Basic** (free), upgrading at Phase 6 | Equities: IEX for live, SIP free for historical >15 min. Options `indicative` feed. 200 req/min. **Websocket capped at 30 symbols.** |
| Historical options | **Alpaca** | From Feb 2024; bars only beyond 7 days (see §5.3) |
| News | **Alpaca news feed** + **Finnhub** | Ticker-tagged |
| News sentiment | Provider scores → rules → LLM | See §9 |
| Economic + earnings calendar | **Finnhub** | Consensus, prior, actual |
| Central bank dates | **Official sources**, seeded annually | Fed/ECB/BoE/BoJ publish years ahead |
| Geopolitical events | **Manual entry** | No clean API; editorial control preferred |
| Analyst consensus | **Finnhub** `recommendation-trends` | Monthly granularity. Sector leaders = top holding of each SPDR sector ETF (XLK, XLV, XLF, XLY, XLC, XLI, XLP, XLE, XLU, XLRE, XLB). Alpha Vantage benched as documented fallback — not built in v1 |
| Social attention | **StockTwits public API** | Keyless, 200 req/hr per IP |
| Macro series | **FRED** | Free API key |
| Market sentiment composite | **Computed in-house** | See §8.3 |

**Provider abstraction is mandatory.** All market data flows through a `MarketDataProvider` interface. Swapping Alpaca for ThetaData or Polygon must be a config change, not a refactor.

**Plan upgrade trigger.** Basic carries Phases 1–5: the shell, most read-only views, and all backtesting, since everything older than 15 minutes is available on every feed. **Algo Trader Plus ($99/mo)** becomes necessary at Phase 6, when a modeled spread stops being good enough because a real fill is at stake. Subscribe then, not before.

**Stream budget.** Basic caps the websocket at **30 symbols**, and every option contract is its own symbol. The live stream is scoped to open positions plus recommended trades; Markets-page chains and scanner universe scans run on polled snapshots. This constraint disappears on upgrade, but the polled path should remain the default so the app degrades gracefully rather than depending on the subscription.

**Dropped from v1:** Reddit (registration gated, ToS-gray third-party access), X/Twitter (cost), LSEG StarMine (institutional-only licensing).

**Deferred, not dropped:** Databento (OPRA historical — the answer if the §5.3 spread model proves too coarse, since Alpaca supplies no historical quotes), Polygon.io (second `MarketDataProvider` implementation, if one is ever needed to prove the abstraction holds), Marketaux (news sentiment supplement).

---

## 8. Pages

### 8.1 Dashboard

The morning page. Answers "what is my state and what should I look at."

- **Header stats:** total balance, 24h volume, strategy win rate (live, validated trades only — excludes LLM-originated `unvalidated` trades, which live in Research).
- **Controls:** account toggle (Paper/Cash), execution toggle (Manual/Auto), strategy dropdown, Halt, Flatten.
- **Performance chart:** portfolio value over time. Ranges 1D / 1W / 1M / 3M / YTD / 1Y / All. Series begins at first run, seeded with the then-current Alpaca portfolio value; no pre-Corollary reconstruction. Ranges extending before t₀ render only the available window and label the start date. Optional benchmark overlay (SPY) in `accent`.
- **Recommended Trades:** scrollable, refresh button, confidence per row, `unvalidated` badge where applicable. "View all" → Research.
- **Recent Executions:** scrollable table, filter, export CSV. "View all" → Activity.

### 8.2 Activity

- **Header stats:** average win ($ and %), average loss ($ and %), lifetime P&L.
- **Open Positions:** symbol, last price, cost basis, current value, quantity, unrealized P&L, and a **Close** action per row. Close opens a confirm dialog showing the current bid/ask and estimated proceeds.
- **Recent Activity:** trades, deposits, withdrawals. Time, contract, action, P&L, price, quantity, status. Filterable, paginated.
- Rejected orders appear here with their rejection reason.

### 8.3 News

- **Live feed:** each item carries a ticker (or `MARKET`), a sentiment label, and a publisher. Filterable by sector, publisher, sentiment. Sortable by time. Historical lookback supported.
- **Market Sentiment** (replaces "Global Sentiment"): a 0–100 composite computed in-house from seven components, mirroring CNN's published methodology — momentum (SPX vs 125d MA), strength (52w highs/lows), breadth (advance/decline volume), put/call ratio, volatility (VIX vs 50d MA), safe-haven demand (20d equity minus Treasury return), and junk bond demand (FRED `BAMLH0A0HYM2`). Each z-scored on a trailing window, equally weighted. Component breakdown visible on hover.
- **Social Attention** (replaces "Trending Signals"): StockTwits ticker mention velocity against a 30-day baseline. Displays sample size. Sentiment is aggregated only over messages carrying a user label (roughly 30–50% of messages) and the labeled count is shown. Treated as context, never as a trade trigger.
- **Top rated by sector:** consensus buy/hold/sell distribution from Finnhub, rolled up by sector via each sector's largest constituents. Displays an as-of date; refreshes monthly.
- **Market Calendar:** earnings, economic releases, central bank decisions, dividends, and manually entered geopolitical events, with exact scheduled times. Forward-looking only.

### 8.4 Markets

- **Options chains:** symbol, strike, expiration, last, change, change %, bid, ask. Filter by underlying, volume, top gainers, top losers, highest IV, highest open interest. Paginated.
- **Stocks & ETFs:** symbol, name, price, change, change %, volume, market cap. Filter by most active, top gainers, top losers, new, market cap. Paginated.

### 8.5 Research

- **Chat:** Claude, scoped to this account and its data. Can propose strategies, request additional recommendations, and retrieve market context. Strategy proposals enter as `draft` and must clear §5.3.
- **Strategies:** scrollable list with an `ACTIVE` badge. Edit and delete inline. Hover reveals backtest and live statistics, separately labeled.
- **LLM origination panel:** sits with the strategy list. Shows LLM-originated trades as their own bucket — count, win rate, profit factor, standalone P&L, and the validated/unvalidated split. This is the number that answers "is the LLM layer earning its place," and it is deliberately kept off the Dashboard so the headline win rate stays clean.
- **Market Pulse:** VIX, the in-house sentiment composite, top sector for the session.
- **Recommended Trades (full):** symbol, contract, confidence, action. Actions: Execute, Queue, Dismiss. Export CSV.

### 8.6 Account

Replaces "Wallet." Alpaca is the source of truth; Corollary keeps no ledger.

- Cash, buying power, options buying power, settled vs unsettled.
- Deposits and withdrawals link out to Alpaca.

### 8.7 Settings

Replaces the user menu. No authentication while bound to `127.0.0.1`.

- API keys (stored in env, never in the repo; UI shows masked presence only).
- Risk limits (§4), with audit log.
- Notification routing (§10).
- Data source selection and provider status.
- Theme (light / dark).
- **Sentiment accuracy readout** (§9).

---

## 9. News sentiment — zero human review

Three tiers, in order:

1. **Provider-supplied** — Finnhub / Alpaca ship a score. Published as-is.
2. **Rules** — deterministic headline patterns for high-signal events: beat/miss vs estimates, guidance raised/cut, upgrade/downgrade, M&A, secondary offering, buyback, executive departure, FDA action.
3. **LLM** — batched (20 headlines per call), cached by article ID.

**Confidence gating.** Tier 3 publishes a direction only above a confidence threshold. Below it, the item displays as `Unclassified`. Silence beats a wrong label.

**Self-auditing.** A weekly job scores every published label against that ticker's realized forward return at 1 hour and 1 day, producing an accuracy figure per source and per tier. That figure lives in Settings. If accuracy falls below 52% — coin-flip territory — the system fires a notification and **automatically demotes news sentiment from a scanner input to display-only**.

No article is ever manually reviewed. The system grades itself and reports when it stops working.

---

## 10. Notifications

**Channels:** in-app bell (all events) and Discord webhook (routed by severity).

| Event | Bell | Discord |
|---|---|---|
| Order filled | ✓ | ✓ |
| Order rejected | ✓ | ✓ |
| Stop loss hit | ✓ | ✓ |
| Daily loss halt | ✓ | ✓ |
| Engine error / dead-man's switch | ✓ | ✓ |
| Price alert on a recommended trade | ✓ | ✓ |
| New recommendations ready | ✓ | — |
| Strategy promotion eligible | ✓ | — |

Discord messages use rich embeds — a fill renders as a formatted block with contract, price, quantity, and P&L.

Implemented behind a `Notifier` interface with pluggable channels, so adding SMS or push later is configuration rather than code.

---

## 11. Build order

**Phase 1 — Shell.** Every page renders, navigation works, design system applied, Ctrl+K command palette, light/dark. Mock data. *Done when: you can click through the whole app and it feels real.*

**Phase 2 — Read-only, real data.** Alpaca paper connected. Dashboard, Activity, Markets, Account show live values. No execution. *Done when: you'd open it in the morning and learn something true.*

**Phase 3 — Context.** News, calendar, sentiment composite, social attention, analyst consensus. Self-audit job running.

**Phase 4 — Strategy runtime.** Declarative schema, indicator whitelist, strategy editor, intent/arbiter layer with one strategy.

**Phase 5 — Backtesting.** Parquet bulk download, DuckDB queries, spread model, walk-forward harness, promotion gate.

**Phase 6 — Execution.** Risk manager, manual execution from Recommended Trades, then auto with all limits enforced. Paper only.

**Phase 7 — Live.** Cash trading enabled after a sustained paper period. Small size. Every limit tested deliberately before it matters.

Phases 1–3 are safe to build fast. Phases 6–7 are where mistakes cost money; they get tests before features.

---

## 12. Open questions

- Multi-strategy arbitration rules, deferred but not designed away.
- **Engine and API: one process or two.** §3 draws them as separate boxes. Two OS processes sharing one SQLite file requires WAL mode and single-writer discipline, or `database is locked` surfaces at the worst possible moment. The alternative is one process with the scheduler on the FastAPI event loop — same module boundary, no process boundary. Needs deciding before Phase 2.
- **Adjusted contracts.** After a split or special dividend, OCC issues a modified root with a numeric suffix and a deliverable that is no longer 100 shares. §4 sizing assumes a 100 multiplier throughout. Either filter them out of the scanner universe or handle the multiplier explicitly — an unhandled adjusted contract makes the risk manager compute max loss wrong, which is the one failure §4 exists to prevent.