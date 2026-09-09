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

**Modes.** Two independent switches, living with the rest of the Dashboard controls (§8.1):

| Switch | Values | Meaning |
|---|---|---|
| Account | Paper / Cash | Which Alpaca account keys are in use |
| Execution | Manual / Auto | Whether the engine may place orders unattended |

Paper is the default on every cold start. Switching to Cash requires an explicit confirm dialog that names the account and its balance.

These sat in the persistent app header through early Phase 1 and were moved to the Dashboard deliberately. That left no on-screen indication of the live account on Activity, News, Markets, Research, Account, or Settings. It was acceptable while nothing off the Dashboard rendered account-scoped money, and it stopped being acceptable when Activity started showing per-account positions and P&L.

Two things resolve it, and they do different jobs:

- **A read-only account badge in the header**, on every page — a pill reading Paper or Cash, not a switch. Cash takes `caution`, since real money in play is a reason to read carefully rather than a failure; Paper recedes into `neutral`. This answers "whose money am I looking at" in the same place everywhere.
- **The Paper/Cash switch itself appears on any page whose entire contents are account-scoped.** That is the Dashboard, Activity, and Account. On all three, the toggle sits on the page title line, because it governs everything below it — sending someone to another page to change the scope of the page they're reading is a detour, not a safeguard.

The safeguard was never the switch's location. Cash is entered through the confirm dialog above no matter where the toggle is rendered, it never survives a restart, and every cold start comes up in Paper. That confirm names **buying power first, then cash** — on a cash account the two differ by whatever is unsettled, and quoting cash alone in the dialog that precedes real trading would overstate what can actually be deployed. The difference is stated rather than left to be noticed. Pages that only *display* account context and don't scope their whole contents to it (News, Markets, Research, Settings) get the badge and no switch.

The rule is what decides this, not the list. Account was added to the first group when the page was built: every figure on it — cash, buying power, settled and unsettled, its transfer ledger — belongs to exactly one account, which makes it the most completely account-scoped page in the app. Settings stays in the second group: it displays the badge and scopes nothing to it, apart from the equity figure a risk-limit confirm quotes.

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

Scoped to the account whose keys are in use. Paper and Cash are separate books, and the page shows one of them at a time — never a merged view. The Paper/Cash toggle sits on the page title line, since every section below it is account-scoped; the header badge names the account too, as it does everywhere (§3).

- **Header stats:** average win ($ and %), average loss ($ and %), lifetime P&L — three cards in a row, the same treatment the Dashboard gives its header stats. Computed from the feed below rather than stored separately, so the header can't disagree with the rows under it. Realized trades only — deposits and withdrawals are money moved, not money made, and are excluded. An average over zero trades renders as an em dash, never as $0.00.
- **Open Positions:** symbol, **days to expiry**, last price (the *contract's*, not the underlying's), cost basis, current value, quantity, unrealized P&L.
  - The column reads `Expired`, `Today`, or a day count, and the fixtures carry one of each — a render path with no fixture behind it is a state nobody can see. An expired contract stays in the book until settlement clears it, and those are exactly the hours you want the row saying so.
  - DTE is flagged in `caution` — never `error`, since running out of time is a deadline rather than a system failure — once the position is inside its strategy's own `time_stop_dte` (§5.1), or inside a week if it's detached. Expiry is the most time-sensitive fact about an option and previously lived only inside the contract string, where nothing could count it down. Each row **expands in place** into a chart and an order ticket; one row at a time, nothing modal, so the rest of the book and the live account stay on screen while you size a trade.
  - **Chart:** *Value since entry* with cost basis as a reference line, so unrealized P&L is the gap; *Payoff at expiry*, computed from strikes and premium, with max profit, max loss and breakevens spelled out; or *Underlying* — the stock behind the option, with yesterday's close and every strike marked, and its price, daily change and previous close stated as text beneath.
  - The underlying's price and its day also appear on the expanded row without opening the chart. The Last column is the *contract's* mark, which is what you close at, so the stock's own move — the thing that actually moved the position — has nowhere else to appear.
  - Underlying quotes are keyed by symbol, not carried per position. Two positions can share an underlying, and a per-position copy would let one stock show two prices on one page.
  - **Ticket:** Close, Add, and Attach/Edit exit. Order types are derived from the position — Market/Limit/Stop/Stop-Limit on a single leg, **Limit only** on a multi-leg, which the ticket says rather than silently offering less. Time-in-force is Day or GTC, the only two an option order takes.
  - The ticket states the expiry in its confirm, and warns when a **GTC order is written on a contract that expires before "canceled" ever arrives** — good-til-canceled is the one phrase on the ticket that reads like a promise, and on a contract days out it is a short one.
  - Enter opens the confirm rather than placing the order: one keystroke should not be able to close a position. Escape collapses the row, and dismissing the ⋯ menu returns focus to the button that opened it.
  - The ticket always names the resolved side. **Adding to a short is a sell to open**, so the control is "Add to position" and the ticket reads `Sell to open (STO)` — a button reading Buy would name the opposite of the order it places.
  - **Close is the order button**, not a fast market sell: it opens the ticket with market preselected, one confirm short of submitting. Closing one position never halts the engine — the same distinction §2 draws between Halt and Flatten.
  - An attached exit is an OCO: a take-profit limit paired with a stop or stop-limit. Attaching one **detaches the position from its strategy**, because two exit regimes on one position double-close when a cancel races a fill. A position carries at most one closing order, and editing replaces it in place rather than cancelling and re-submitting.
  - The row states who is responsible for closing the position, and *where an attached exit is held* — at the broker, where it fires whether or not Corollary is running, or by Corollary, where it does not exist while the engine is down.
- **Working Orders:** orders that are placed and haven't filled — time placed, asset, side, type, price, quantity, and **Cancel** per row. A market order fills immediately and never appears here; a limit, stop or stop-limit works until it fills or is cancelled. Cancelling flips that order's `pending` row in the ledger to `canceled` rather than appending a second row: the order had one life and the feed shows it once.
  - **A multi-leg position can only be closed by working an order**, since Alpaca accepts nothing but limit orders on a spread. The book therefore cannot be emptied from this page — Flatten, on the Dashboard, is the only immediate exit for a spread.
  - Attached exits are deliberately **not** listed here. They live on their position, which is where they are edited and cancelled, and duplicating them into a second list would give one thing two homes that could disagree.
- **Recent Activity:** trades, deposits, withdrawals. **Time, Asset, Action, P&L, Price, Qty, Status.** Searchable by symbol or contract and filterable by status — the two combine rather than replacing each other — and paginated. "Everything I did in AAPL" is the question a fifty-row ledger raises and a status filter cannot answer. The Dashboard shows a recent window of the same feed in a narrower summary layout — Time, Asset/Action, Qty, Price, P&L, with the status on hover because a half-width panel has no room for the column. Both render from one component, so the cell logic can't diverge; only the column set does.
  - P&L percent stays off screen in both. It's in the CSV export, which is what you reconcile against the broker with.
- Rejected orders appear here with their rejection reason **as visible text**, not as a tooltip. This is the page of record for rejections (§4); a reason reachable only by hovering doesn't exist on a screenshot, on a touch device, or to a keyboard.
- **The page is live, not refreshed.** Prices stream while it is open and the header reports the time of the last one. **Contract *and* underlying both move** — a page that streams one while the other sits frozen is only half live, and the payoff chart's "now" marker would never budge.
  - The status pill goes **stale** after 15s without a price. A badge reading "Live" beside a timestamp that stopped moving is a status indicator lying about the one thing it is for. Note the Phase 2 split: going stale is a *display* concern, while going disconnected is an *engine* one — §2's dead-man's switch halts and requires an explicit human resume, and the two must never be collapsed into "it'll come back on its own". There is no Refresh button: the question a positions screen has to answer is "are these numbers current", and prices arriving on their own answer it continuously where a button answers it once. The stream is scoped to the open positions of the account whose keys are loaded — one symbol per position, well inside the 30-symbol cap §7 describes on the Basic plan.
  - Because prices move, **working orders fill and attached exits trigger**. Without that both features are decorative: an order rests forever and an exit never fires.
- **Loading and empty states are designed, not blank.** Loading is a real condition rather than a timer — until the first price arrives there is nothing current to show, which is the state Phase 2 is in while the opening snapshot is in flight. Skeletons cover it, because a stream that hasn't connected and an account with nothing in it mean very different things at 9:31am and a blank panel cannot tell them apart. Empty states say what would appear there and where to go instead — an empty Working Orders list points at the position rows where exits live, rather than implying you have no exits.
- **Detaching is reversible.** A position records the strategy that *opened* it, separately from the one currently managing it, so reattaching returns it to the exit rules it was opened under rather than to whichever strategy happens to be active now.

**Deferred, and deliberately:** rolling a position (Alpaca rejects a roll of a short spread as an uncovered multi-leg leg), trailing stops (absent from the options order-type matrix), and greeks or IV in the ticket.

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
  - **Through Phase 3 this is a scripted shell, and it says so on screen.** Replies come from a keyword table; anything unmatched gets a reply admitting there is no answer rather than improvising one. Every other Phase 1 surface is self-evidently a fixture — a table of invented numbers reads as invented — but a fluent sentence reads as a considered answer, which makes an unlabelled scripted chat the one place here that could genuinely mislead. The live model arrives with the LLM layer in Phase 4.
  - **Chat history** sits in the chat's top-right corner, beside the scripted marker. Conversations are titled by their *first user message* — the reply is the shell's words, not yours — and a conversation is either current or archived, never both: opening an archived one files whatever is on screen on the way out, so switching never discards a transcript. Starting a new chat on an untouched one is a no-op, because a history of blank rows is a history nobody opens. Nothing here edits or deletes a conversation; it is a record.
  - A proposal carries its intent as prose, **not** a YAML body. The declarative schema and the indicator whitelist are Phase 4, and printing rules no validator has checked would invite trusting them. Accepting one lands a `draft` and nothing further along — the LLM may propose, it may not promote.
- **Strategies:** scrollable list with an `ACTIVE` badge. Rename, promote along §5.2, retire, and delete inline, each behind the guard it warrants.
  - Statistics appear on **expand, not hover.** §8.5 originally said hover, and hover was wrong for the same reason §8.2 puts rejection reasons on screen as text: a hover-only panel does not exist on a touch device, to a keyboard, or in a screenshot.
  - Backtest and live are **labelled separately and never combined.** A 68% backtest and a 71% live record are claims about different evidence, and a blended figure would be the most misleading number on the page. A strategy that has never traded live says so rather than showing its backtest in the live slot.
  - Each row reports its **§5.3 promotion gate** status and names every threshold it misses. It reports rather than blocks: the PRD is explicit that anything missing the gate can still be promoted manually, so greying out the control would enforce a rule the spec left to a person.
  - Deleting the **active** strategy is refused — retire it first. Deleting the running strategy would leave the positions it opened with nothing managing them.
- **LLM origination panel:** sits with the strategy list. Shows LLM-originated trades as their own bucket — count, win rate, profit factor, standalone P&L, and the validated/unvalidated split. This is the number that answers "is the LLM layer earning its place," and it is deliberately kept off the Dashboard so the headline win rate stays clean.
  - This is also where the Dashboard's excluded trades live. §8.1 scopes that win rate to validated trades, and the unvalidated bucket is account-wide rather than per-strategy — an unvalidated origination has no setup match, which is exactly why it cannot be attributed to a strategy.
- **Market Pulse:** VIX, the in-house sentiment composite, top sector for the session. The composite is **derived** from its seven components, never a stored copy, so it cannot disagree with the breakdown News prints beneath the same number.
- **Recommended Trades (full):** contract, expiry, setup class, reason, origin, confidence, action. Actions: Execute, Dismiss. Export CSV.
  - **Two actions, not three.** An earlier revision put `Queue` between them — staged for the engine to place on its next run, distinct from executed because nothing had reached a broker yet. It was removed deliberately. Accepting a candidate now means submitting it, so there is no state in which the app has said yes and nothing has been sent, and no second disposition to keep straight against the first. The consequence to weigh before reinstating it: there is no way to stage a candidate for the engine, and in Phase 6 every acceptance goes straight through `RiskManager.approve()`.
  - Execute is disabled while the engine is halted; Dismiss is not, since waving off a candidate opens nothing. The command palette applies the same gate — a surface that still offers to open a position while halted is the surface it happens on by accident.
  - Dismissal is shared with the Dashboard's panel rather than local to either. Two views act on one candidate set, and each holding its own copy would disagree the moment you dismissed on one and looked at the other. A refresh restores dismissals and leaves executed rows alone — those are decisions, not things you waved off.
  - The CSV exports the **whole** candidate set, dismissed rows included: it is a record of what the scanner produced this session, not a copy of what happens to be on screen. A null confidence exports empty rather than as the em dash it renders as, because "—" in a numeric column is a parse error.

### 8.6 Account

Replaces "Wallet." Alpaca is the source of truth; Corollary keeps no ledger.

- Cash, buying power, options buying power, settled vs unsettled. Buying power differs by account and the page says why: Paper is a margin account at 2× cash, while a Cash account can only spend what has *settled*. Options buying power is never the margin figure — options are not marginable, and sizing against equity buying power overstates capacity by 2×.
- **Total equity, reconciled on screen:** cash + the market value of open positions. Derived, not a maintained balance. Position value is marked from the price stream, so it carries the live pill and shows a loading state until the first price arrives rather than briefly presenting cost basis as equity.
- **Cash transfers**, filtered out of the account's activity ledger — deposits and withdrawals are not orders. Signed, netted, with a designed empty state.
- Deposits and withdrawals link out to Alpaca. Corollary initiates neither.

### 8.7 Settings

Replaces the user menu. No authentication while bound to `127.0.0.1`.

- API keys (stored in env, never in the repo). **Presence only** — no value, no mask, no last-four. There is no field on the page that could contain key material.
- Risk limits (§4), editable, with the three definitions of "risk" stated on the page — a percentage ceiling is uninterpretable without them. Raising a limit confirms and quotes the consequence in dollars against current equity; lowering one does not. The engine still enforces; this edits what is stored.
- Notification routing (§10) — every cell editable, with the critical-event confirm described there.
- Data source status, and **feed selection**: the three `ALPACA_*_FEED` variables, with values the current plan cannot serve shown as requiring an upgrade rather than silently failing. Historical bars on IEX draws a warning, because every `min_avg_volume` threshold in a strategy is measured against whatever feed produced them.
- Theme (light / dark). The **one** preference that persists across a restart, deliberately — every other piece of session state, the account mode above all, is required to come up fresh.
- **Sentiment accuracy readout** (§9), with the demotion banner when a source falls below the 52% floor in either window.
- **Configuration audit log** — risk limits, feeds and routing in one log, newest first, each row carrying the value it replaced. One log rather than three: on a bad day the question is simply whether anything changed first.

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

**Channels:** in-app bell and Discord webhook (routed by severity).

**The table below is the shipped default, not an invariant.** Settings (§8.7) can change any cell, including a bell one — the bell receives all events *by default*, and the routing matrix is what decides thereafter. Three events are marked **critical** (`Order rejected`, `Daily loss halt`, `Engine error / dead-man's switch`): silencing one of those on *every* channel is still permitted, but it passes through a confirm that names what stops arriving, because a rule-9 alert routed nowhere is how the engine ends up halted and silent. Routing changes are audit-logged alongside risk limits.

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

**The bell is a panel anchored to the header icon, not a page.** A notification you have to navigate to is one you miss while watching a position, and there is no `/notifications` route among the app's screens. It carries an unread badge scoped to the account on screen, and opening it marks nothing read — clearing is an explicit action, because glancing at a badge is not reading the feed. Each notification belongs to the book it happened in; an engine fault belongs to neither and appears in both.

Severity is a property of the event, and it observes the same split the rest of the interface does: a rejected order, a loss halt and a dead connection are `error`; a **stop loss firing is `caution`, never `error`**, because a stop doing exactly what it was told to do is not a system failure.

Discord messages use rich embeds — a fill renders as a formatted block with contract, price, quantity, and P&L.

Implemented behind a `Notifier` interface with pluggable channels, so adding SMS or push later is configuration rather than code.

---

## 11. Build order

**Phase 1 — Shell.** Every page renders, navigation works, design system applied, Ctrl+K command palette, light/dark. Mock data. *Done when: you can click through the whole app and it feels real.* **Complete.** All seven pages are built, the palette runs page/recommendation/engine commands, and the notification bell has a feed behind it. Flatten keeps its confirm even when reached by typing three letters into the palette.

An eighth route catches everything else. Without it an unknown URL rendered the header, the account badge and the bell over an empty `<main>` — every control present and nothing beneath them, which on a terminal that otherwise always shows numbers reads as a crash rather than a wrong address. The page says nothing lives there, **says the engine is unaffected**, and lists every real destination. That middle sentence is the reason it is worth designing: a blank screen here should never be ambiguous about whether the engine is still standing. It is neutral, not `error` — mistyping a URL is not a system fault.

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